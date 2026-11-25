"""Request intake agent: free-text request -> shipment fields, confidence, carriers.

With an API key the fields are extracted by Claude (structured output). Without one, or if the
API call fails, the rule-based fallback runs and the result is labelled "rules".
"""
import json
import logging
from datetime import date

import anthropic

from .. import config
from . import groq, rules
from .geo import normalize_city, rank_carriers, vehicle_for_weight

log = logging.getLogger(__name__)

VEHICLES = ["Тентованная фура, 20 т", "Рефрижератор", "Изотермический фургон", "Бортовая платформа",
            "Контейнеровоз", "Газель, 1,5 т", "Малотоннажный, 5 т", "Среднетоннажный, 10 т"]
FIELD_NAMES = ["маршрут", "вес", "дата", "тип машины", "адрес"]

SYSTEM = """Ты агент приёма заявок транспортной компании в Казахстане. Клиенты пишут заявки на перевозку свободным текстом. Извлеки поля заявки так, как их указал клиент.

Правила:
- Города пиши в именительном падеже: «из Шымкента» -> «Шымкент».
- Вес переводи в тонны числом: 800 кг -> 0.8.
- Дату пиши как ГГГГ-ММ-ДД. Если год не указан, бери ближайшую будущую дату относительно сегодняшней. deadline = true, если клиент пишет «до <даты>».
- vehicle_type выбирай из списка, только если клиент назвал тип машины или однозначно описал груз (заморозка -> Рефрижератор). Иначе null.
- unload_address: адрес выгрузки с улицей, проспектом или микрорайоном. Описания вроде «у рынка» или «на складе» без адреса адресом не считаются.
- Не угадывай. Если поля нет или оно двусмысленно, ставь null и добавляй его в missing.
- confidence: насколько ты уверен, что заявку можно передать перевозчику без уточнений у клиента, от 0 до 100.
- comment: одно короткое предложение для менеджера о том, чего не хватает, или пустая строка, если всё ясно."""


def _nullable(t):
    return {"anyOf": [{"type": t}, {"type": "null"}]}


SCHEMA = {
    "type": "object",
    "properties": {
        "origin": _nullable("string"),
        "destination": _nullable("string"),
        "weight_t": _nullable("number"),
        "date": _nullable("string"),
        "deadline": {"type": "boolean"},
        "vehicle_type": {"anyOf": [{"type": "string", "enum": VEHICLES}, {"type": "null"}]},
        "unload_address": _nullable("string"),
        "cargo": _nullable("string"),
        "confidence": {"type": "integer"},
        "missing": {"type": "array", "items": {"type": "string", "enum": FIELD_NAMES}},
        "comment": {"type": "string"},
    },
    "required": ["origin", "destination", "weight_t", "date", "deadline", "vehicle_type",
                 "unload_address", "cargo", "confidence", "missing", "comment"],
    "additionalProperties": False,
}


class AgentRefused(Exception):
    pass


def llm_extract(text: str, today: date) -> dict:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.claude_model(),
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Сегодня {today.isoformat()}.\n\nЗаявка клиента:\n{text}"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    if response.stop_reason == "refusal":
        raise AgentRefused(getattr(response.stop_details, "explanation", None) or "refusal")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("the model ran out of tokens")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def _num(v):
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def completeness(f: dict, vehicle_guess: bool) -> tuple[int, list[str]]:
    """Share of the fields a carrier needs, 0-100, and the names of the missing ones."""
    score, missing = 0, []
    if f["origin"] and f["destination"]:
        score += 35
    else:
        score += 15 if (f["origin"] or f["destination"]) else 0
        missing.append("маршрут")
    if f["weight_t"] is not None:
        score += 25
    else:
        missing.append("вес")
    if f["date"]:
        score += 20
    else:
        missing.append("дата")
    if f["vehicle"]:
        score += 6 if vehicle_guess else 10
    else:
        missing.append("тип машины")
    if f["address"]:
        score += 10
    else:
        missing.append("адрес")
    return min(score, 98), missing


def process(text: str, carriers, today: date | None = None) -> dict:
    """Run the agent on one request. Never raises: on any LLM problem it falls back to rules."""
    today = today or date.today()
    raw, mode, llm_conf, comment = None, "rules", None, ""
    if config.llm_enabled():
        try:
            if config.provider() == "groq":
                raw = groq.extract(SYSTEM, f"Сегодня {today.isoformat()}.\n\nЗаявка клиента:\n{text}", SCHEMA)
            else:
                raw = llm_extract(text, today)
            mode = f"llm:{config.model_name()}"
            llm_conf = max(0, min(100, int(raw.get("confidence") or 0)))
            comment = (raw.get("comment") or "").strip()
        except AgentRefused:
            mode = "llm-refused"
            comment = "Агент отказался обрабатывать текст, нужна проверка человеком."
        except (anthropic.APIError, groq.GroqError, ValueError, StopIteration, RuntimeError) as e:
            log.warning("LLM unavailable, falling back to rules: %s", e)
            mode = "rules (LLM недоступна)"
    if raw is None:
        raw = rules.extract(text, today)

    f = {
        "origin": normalize_city(raw.get("origin")),
        "destination": normalize_city(raw.get("destination")),
        "weight_t": _num(raw.get("weight_t")),
        "date": raw.get("date") or None,
        "deadline": bool(raw.get("deadline")),
        "vehicle": raw.get("vehicle_type") or None,
        "address": (raw.get("unload_address") or None),
        "cargo": raw.get("cargo") or None,
    }
    if f["origin"] and f["origin"] == f["destination"]:
        f["destination"] = None
    vehicle_guess = False
    if not f["vehicle"] and f["weight_t"] is not None:
        f["vehicle"], vehicle_guess = vehicle_for_weight(f["weight_t"]), True

    score, missing = completeness(f, vehicle_guess)
    confidence = score if llm_conf is None else min(score, llm_conf)
    if mode == "llm-refused":
        confidence = min(confidence, 30)
    auto = confidence >= config.threshold()

    km, ranked = rank_carriers(carriers, f["origin"], f["destination"], f["vehicle"])
    if auto:
        reason = None
    elif comment:
        reason = comment
    elif missing:
        reason = "Не хватает: " + ", ".join(missing)
    else:
        reason = "Агент не уверен в разборе заявки"
    return {"fields": f, "vehicle_guess": vehicle_guess, "confidence": confidence, "missing": missing,
            "mode": mode, "km": km, "carriers": ranked if auto else None,
            "status": "auto" if auto else "review", "reason": reason}
