"""API tests. The LLM and Stripe are replaced with fakes, so the tests need no keys and no network."""
import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.agents import intake, rules, support


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "rules")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db.init(f"sqlite:///{tmp_path / 'test.db'}")
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(c, who):
    r = c.post("/api/login", json={"email": f"{who}@logiagent.test", "password": db.DEMO_PASSWORD})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------- rules fallback ----------

def test_rules_extract_full_request():
    f = rules.extract("Нужно отправить 12 тонн стройматериалов из Алматы в Астану до 25 ноября. "
                      "Нужна фура с тентом, разгрузка на ул. Кабанбай батыра, 12.", date(2026, 11, 18))
    assert (f["origin"], f["destination"], f["weight_t"]) == ("Алматы", "Астана", 12)
    assert f["date"] == "2026-11-25" and f["deadline"]
    assert f["vehicle_type"] == "Тентованная фура, 20 т"
    assert f["unload_address"].startswith("ул. Кабанбай батыра, 12")


def test_rules_kg_and_next_year():
    f = rules.extract("800 кг мебели из Шымкента в Тараз 5 января", date(2026, 11, 18))
    assert f["weight_t"] == 0.8 and f["date"] == "2027-01-05"


# ---------- auth and roles ----------

def test_login_and_roles(client):
    assert client.post("/api/login", json={"email": "client@logiagent.test", "password": "wrong"}).status_code == 401
    assert client.get("/api/me").status_code == 401
    h = login(client, "client")
    assert client.get("/api/me", headers=h).json()["user"]["role"] == "client"
    assert client.get("/api/queue", headers=h).status_code == 403
    assert client.get("/api/admin/overview", headers=h).status_code == 403
    assert client.get("/api/admin/overview", headers=login(client, "admin")).status_code == 200


# ---------- intake agent ----------

def test_complete_request_is_created_automatically(client):
    h = login(client, "client")
    r = client.post("/api/requests", headers=h, json={"text": "12 тонн из Алматы в Астану до 25 ноября, тент, ул. Абая, 5"}).json()
    req = r["request"]
    assert req["status"] == "auto" and req["agent_mode"] == "rules"
    assert req["confidence"] >= 80 and len(req["carriers"]) == 3 and req["km"] > 1000
    assert r["company"]["used"] == 313


def test_incomplete_request_goes_to_manager(client):
    h = login(client, "client")
    req = client.post("/api/requests", headers=h, json={"text": "Привезите груз в Караганду побыстрее"}).json()["request"]
    assert req["status"] == "review" and req["confidence"] < 80
    queue = client.get("/api/queue", headers=login(client, "manager")).json()
    assert req["id"] in [q["id"] for q in queue]


def test_limit_blocks_new_requests(client):
    with db._Session() as s:
        s.get(db.Company, 1).plan, s.get(db.Company, 1).used = "start", 50
        s.commit()
    r = client.post("/api/requests", headers=login(client, "client"), json={"text": "5 т из Алматы в Тараз завтра"})
    assert r.status_code == 402


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; returns queued responses in order."""
    responses = []
    calls = []

    def __init__(self, *a, **k):
        self.messages = self

    def create(self, **kwargs):
        FakeAnthropic.calls.append(kwargs)
        return FakeAnthropic.responses.pop(0)


def text_response(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, stop_details=None, content=[SimpleNamespace(type="text", text=text)])


def test_llm_extraction_used_and_capped_by_completeness(client, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "llm")
    monkeypatch.setattr(intake.anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.calls = []
    FakeAnthropic.responses = [text_response(json.dumps({
        "origin": "Шымкент", "destination": "Тараз", "weight_t": 12, "date": "2026-11-28", "deadline": False,
        "vehicle_type": "Тентованная фура, 20 т", "unload_address": None, "cargo": "цемент",
        "confidence": 70, "missing": ["адрес"], "comment": "Адрес выгрузки указан как «у рынка»."}))]
    req = client.post("/api/requests", headers=login(client, "client"),
                      json={"text": "12 т цемента из Шымкента в Тараз 28 ноября, тент, выгрузка у рынка."}).json()["request"]
    assert req["agent_mode"].startswith("llm:")
    assert req["status"] == "review" and req["confidence"] == 70
    assert req["reason"] == "Адрес выгрузки указан как «у рынка»."
    sent = FakeAnthropic.calls[0]
    assert sent["output_config"]["format"]["type"] == "json_schema"


def test_llm_failure_falls_back_to_rules(client, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "llm")

    class Broken(FakeAnthropic):
        def create(self, **kwargs):
            raise intake.anthropic.APIConnectionError(request=None)
    monkeypatch.setattr(intake.anthropic, "Anthropic", Broken)
    req = client.post("/api/requests", headers=login(client, "client"),
                      json={"text": "12 тонн из Алматы в Астану до 25 ноября, тент, ул. Абая, 5"}).json()["request"]
    assert req["agent_mode"] == "rules (LLM недоступна)" and req["status"] == "auto"


# ---------- manager ----------

def test_manager_fills_missing_field_and_confirms(client):
    h = login(client, "manager")
    bad = client.post("/api/requests/1051/resolve", headers=h, json={"action": "confirm"})
    assert bad.status_code == 422
    ok = client.post("/api/requests/1051/resolve", headers=h, json={"action": "confirm", "fields": {"date": "2026-12-01"}})
    assert ok.json()["status"] == "confirmed" and ok.json()["carriers"]
    doc = client.post("/api/requests/1048/resolve", headers=h, json={"action": "accept_doc"}).json()
    assert doc["weight_t"] == 12.8


# ---------- support agent ----------

def test_support_rules_answer(client):
    r = client.post("/api/support", headers=login(client, "client"), json={"message": "где заявка 1042?"}).json()
    assert "в пути" in r["reply"] and r["mode"] == "rules"


def test_support_llm_uses_tools(client, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "llm")
    monkeypatch.setattr(support.anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.calls = []
    tool_call = SimpleNamespace(type="tool_use", id="tu_1", name="get_request", input={"request_id": 1042})
    FakeAnthropic.responses = [
        SimpleNamespace(stop_reason="tool_use", stop_details=None, content=[tool_call]),
        text_response("Заявка № 1042 в пути, прибытие завтра до 18:00."),
    ]
    r = client.post("/api/support", headers=login(client, "client"), json={"message": "где мой груз 1042?"}).json()
    assert r["tools"] == [{"name": "get_request", "input": {"request_id": 1042}}]
    assert "1042" in r["reply"]
    tool_result = FakeAnthropic.calls[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1" and "в пути" in tool_result["content"]


def test_support_tool_cannot_read_other_company():
    other = SimpleNamespace(id=9999, origin="A", destination="B", weight_t=1, date=None, status="auto", eta=None, reason=None)
    out = json.loads(support.run_tool("get_request", {"request_id": 1042}, [other], lambda r: None))
    assert "error" in out


# ---------- billing ----------

def test_demo_payment_changes_plan(client):
    h = login(client, "owner")
    assert client.post("/api/billing/checkout", headers=h, json={"plan": "pro"}).json()["mode"] == "demo"
    bad = client.post("/api/billing/demo-pay", headers=h, json={"plan": "pro", "card": "5555 5555 5555 4444"})
    assert bad.status_code == 422
    ok = client.post("/api/billing/demo-pay", headers=h, json={"plan": "pro", "card": "4242 4242 4242 4242"}).json()
    assert ok["company"]["plan"] == "pro" and ok["company"]["limit"] == 3000


def test_stripe_checkout_and_confirm(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    from app import billing
    created = {}

    def fake_create(**params):
        created.update(params)
        return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")

    def fake_retrieve(sid):
        return SimpleNamespace(to_dict=lambda: {"id": sid, "status": "complete", "payment_status": "paid",
                                                "metadata": {"company_id": "1", "plan": "pro"}})
    monkeypatch.setattr(billing.stripe.checkout.Session, "create", staticmethod(fake_create))
    monkeypatch.setattr(billing.stripe.checkout.Session, "retrieve", staticmethod(fake_retrieve))
    h = login(client, "owner")
    r = client.post("/api/billing/checkout", headers=h, json={"plan": "pro"}).json()
    assert r["mode"] == "stripe" and r["url"].startswith("https://checkout.stripe.com/")
    assert created["mode"] == "subscription" and created["line_items"][0]["price_data"]["unit_amount"] == 9900
    c = client.post("/api/billing/confirm", headers=h, json={"session_id": "cs_test_1"}).json()
    assert c["status"] == "paid" and c["company"]["plan"] == "pro"
    again = client.post("/api/billing/confirm", headers=h, json={"session_id": "cs_test_1"}).json()
    assert again["status"] == "paid"


def test_live_stripe_key_is_rejected(monkeypatch):
    from app import config
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_123")
    with pytest.raises(RuntimeError):
        config.stripe_key()


# ---------- Groq (free provider) ----------

class FakeHttpResponse:
    def __init__(self, data, code=200):
        self.status_code, self._data, self.text = code, data, json.dumps(data)

    def json(self):
        return self._data


def test_groq_extraction(client, monkeypatch):
    from app.agents import groq
    monkeypatch.setenv("AGENT_MODE", "auto")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake")
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        content = __import__("json").dumps({
            "origin": "Алматы", "destination": "Астана", "weight_t": 12, "date": "2026-11-25", "deadline": True,
            "vehicle_type": "Тентованная фура, 20 т", "unload_address": "ул. Абая, 5", "cargo": "стройматериалы",
            "confidence": 95, "missing": [], "comment": ""})
        return FakeHttpResponse({"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(groq.httpx, "post", fake_post)
    assert client.get("/api/status").json()["provider"] == "groq"
    req = client.post("/api/requests", headers=login(client, "client"),
                      json={"text": "12 тонн из Алматы в Астану до 25 ноября, тент, ул. Абая, 5"}).json()["request"]
    assert req["agent_mode"] == "llm:openai/gpt-oss-120b" and req["status"] == "auto"
    assert sent[0]["response_format"] == {"type": "json_object"} and sent[0]["model"] == "openai/gpt-oss-120b"


def test_groq_support_tool_loop(client, monkeypatch):
    from app.agents import groq
    monkeypatch.setenv("AGENT_MODE", "groq")
    replies = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_request", "arguments": "{\"request_id\": 1042}"}}]}}]},
        {"choices": [{"message": {"content": "Заявка № 1042 в пути."}}]},
    ]
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return FakeHttpResponse(replies.pop(0))
    monkeypatch.setattr(groq.httpx, "post", fake_post)
    r = client.post("/api/support", headers=login(client, "client"), json={"message": "где 1042?"}).json()
    assert r["reply"] == "Заявка № 1042 в пути." and r["tools"][0]["name"] == "get_request"
    tool_msg = sent[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1" and "в пути" in tool_msg["content"]


def test_groq_error_falls_back_to_rules(client, monkeypatch):
    from app.agents import groq
    monkeypatch.setenv("AGENT_MODE", "groq")
    monkeypatch.setattr(groq.httpx, "post", lambda *a, **k: FakeHttpResponse({"error": "invalid key"}, 401))
    req = client.post("/api/requests", headers=login(client, "client"),
                      json={"text": "12 тонн из Алматы в Астану до 25 ноября, тент, ул. Абая, 5"}).json()["request"]
    assert req["agent_mode"] == "rules (LLM недоступна)"
