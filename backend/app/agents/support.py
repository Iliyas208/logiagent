"""Support agent: answers customers about their shipments.

With an API key Claude runs a tool loop: it decides itself which tools to call (request status,
request list, knowledge base search, escalation to a manager). Without a key a rule-based lookup answers.
"""
import json
import logging
import re

import anthropic

from .. import config
from . import groq, rules

log = logging.getLogger(__name__)
MAX_STEPS = 6

SYSTEM = """Ты агент поддержки транспортной компании LogiAgent. Отвечаешь клиенту на русском, коротко и по делу (1–3 предложения).
Данные о заявках и ответы на общие вопросы бери только из инструментов. Ничего не выдумывай: если данных нет, скажи об этом.
Если клиент недоволен, просит человека, или ты не можешь ответить по данным, вызови escalate_to_manager и скажи клиенту, что вопрос передан менеджеру.
Клиент видит только свои заявки; номера заявок четырёхзначные, например 1042."""

TOOLS = [
    {"name": "get_request", "strict": True,
     "description": "Статус и детали одной заявки клиента: маршрут, вес, дата, статус, ожидаемое прибытие, причина проверки.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "integer", "description": "Номер заявки, например 1042"}},
                      "required": ["request_id"], "additionalProperties": False}},
    {"name": "list_my_requests",
     "description": "Список всех заявок этого клиента с маршрутами и статусами.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "search_faq", "strict": True,
     "description": "Поиск в базе знаний компании: тарифы, сроки доставки, документы, страхование груза, отмена заявки, оплата.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"], "additionalProperties": False}},
    {"name": "escalate_to_manager", "strict": True,
     "description": "Передать вопрос клиента менеджеру с кратким описанием причины.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                      "required": ["reason"], "additionalProperties": False}},
]

FAQ = [
    ("Сроки доставки", "Алматы — Астана занимает 1–2 дня. Срок по другим маршрутам считается из расстояния: около 600 км в сутки."),
    ("Документы", "Для перевозки нужны товарно-транспортная накладная и доверенность получателя. Накладную можно загрузить в кабинете."),
    ("Страхование груза", "Груз застрахован на сумму до 10 млн тенге. Для дорогого груза менеджер оформляет отдельный полис."),
    ("Отмена заявки", "Заявку можно отменить бесплатно до назначения машины. После назначения взимается 10% стоимости."),
    ("Оплата перевозки", "Перевозку оплачивают по счёту после доставки, в течение 5 рабочих дней."),
    ("Тарифы сервиса", "Тарифы: Старт — 50 заявок в месяц бесплатно, Business — 500 заявок за $29, Pro — 3000 заявок за $99."),
]

STATUS_RU = {"auto": "создана автоматически, ждёт подтверждения перевозчика", "review": "на проверке у менеджера",
             "confirmed": "подтверждена менеджером, ждёт погрузки", "returned": "возвращена клиенту на уточнение",
             "transit": "в пути"}


def search_faq(query: str) -> list[dict]:
    words = {w[:5] for w in re.findall(r"[а-яёa-z]+", query.lower()) if len(w) > 2}
    scored = []
    for title, text in FAQ:
        doc = {w[:5] for w in re.findall(r"[а-яёa-z]+", (title + " " + text).lower())}
        hits = len(words & doc)
        if hits:
            scored.append((hits, title, text))
    scored.sort(reverse=True)
    return [{"title": t, "text": x} for _, t, x in scored[:2]]


def request_info(r) -> dict:
    return {"id": r.id, "route": f"{r.origin or '?'} → {r.destination or '?'}", "weight_t": r.weight_t,
            "date": r.date, "status": STATUS_RU.get(r.status, r.status), "eta": r.eta, "review_reason": r.reason}


def run_tool(name: str, args: dict, requests: list, escalate) -> str:
    if name == "get_request":
        r = next((x for x in requests if x.id == args.get("request_id")), None)
        return json.dumps(request_info(r) if r else {"error": "Заявка не найдена среди заявок этого клиента"}, ensure_ascii=False)
    if name == "list_my_requests":
        return json.dumps([request_info(r) for r in requests], ensure_ascii=False)
    if name == "search_faq":
        return json.dumps(search_faq(args.get("query", "")) or [{"error": "Ничего не найдено"}], ensure_ascii=False)
    if name == "escalate_to_manager":
        escalate(args.get("reason", ""))
        return json.dumps({"ok": True, "message": "Вопрос передан менеджеру, он ответит в течение 30 минут"}, ensure_ascii=False)
    return json.dumps({"error": f"Неизвестный инструмент {name}"}, ensure_ascii=False)


def clean_history(history: list) -> list:
    """Keep the last text turns, starting with a user turn and alternating roles."""
    out = []
    for m in (history or [])[-10:]:
        role, content = m.get("role"), str(m.get("content", ""))[:2000]
        if role not in ("user", "assistant") or not content:
            continue
        if not out and role != "user":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    if out and out[-1]["role"] == "user":
        out.pop()  # the new question is appended by the caller
    return out


def answer(question: str, history: list, requests: list, escalate) -> dict:
    used = []
    if config.llm_enabled():
        try:
            if config.provider() == "groq":
                return _groq_answer(question, history, requests, escalate, used)
            return _llm_answer(question, history, requests, escalate, used)
        except (anthropic.APIError, groq.GroqError) as e:
            log.warning("LLM unavailable, falling back to rules: %s", e)
            mode = "rules (LLM недоступна)"
    else:
        mode = "rules"
    text, conf = rules.answer(question, requests)
    return {"reply": text, "confidence": conf, "tools": [], "mode": mode, "escalated": False}


def _groq_answer(question, history, requests, escalate, used) -> dict:
    escalated = {"flag": False}

    def do_escalate(reason):
        escalated["flag"] = True
        escalate(reason)

    def tool(name, args):
        used.append({"name": name, "input": args})
        return run_tool(name, args, requests, do_escalate)

    messages = clean_history(history) + [{"role": "user", "content": question[:2000]}]
    reply = groq.tool_loop(SYSTEM, messages, TOOLS, tool, MAX_STEPS)
    if not reply:
        reply = "Не смог ответить сразу. Вопрос передан менеджеру."
        if not escalated["flag"]:
            do_escalate("Агент не сформировал ответ")
    return {"reply": reply, "confidence": None if escalated["flag"] else 95, "tools": used,
            "mode": f"llm:{config.model_name()}", "escalated": escalated["flag"]}


def _llm_answer(question, history, requests, escalate, used) -> dict:
    client = anthropic.Anthropic()
    escalated = {"flag": False}

    def do_escalate(reason):
        escalated["flag"] = True
        escalate(reason)

    messages = clean_history(history) + [{"role": "user", "content": question[:2000]}]
    response = None
    for _ in range(MAX_STEPS):
        response = client.messages.create(
            model=config.claude_model(), max_tokens=4000, system=SYSTEM, tools=TOOLS,
            messages=messages, output_config={"effort": "low"},
        )
        if response.stop_reason == "refusal":
            do_escalate("Агент поддержки отказался отвечать")
            return {"reply": "Этот вопрос я передал менеджеру, он ответит вам в течение 30 минут.",
                    "confidence": None, "tools": used, "mode": f"llm:{config.claude_model()}", "escalated": True}
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            used.append({"name": call.name, "input": call.input})
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": run_tool(call.name, call.input or {}, requests, do_escalate)})
        messages.append({"role": "user", "content": results})
    reply = "\n".join(b.text for b in (response.content if response else []) if b.type == "text").strip()
    if not reply:
        reply = "Не смог ответить сразу. Вопрос передан менеджеру."
        if not escalated["flag"]:
            do_escalate("Агент не сформировал ответ")
    return {"reply": reply, "confidence": None if escalated["flag"] else 95, "tools": used,
            "mode": f"llm:{config.claude_model()}", "escalated": escalated["flag"]}
