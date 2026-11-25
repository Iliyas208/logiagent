"""LogiAgent API: auth and roles, request intake agent, manager queue, support agent, payments, admin log.

Run: uvicorn app.main:app --reload   (from the backend/ folder), then open http://localhost:8000
"""
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Literal

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Request as HttpRequest
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import billing, config, db
from .agents import intake, support
from .agents.geo import rank_carriers
from .db import AuthToken, Carrier, Company, LogEntry, Request, User

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if db._engine is None:
        db.init()
    config.stripe_key()  # fail fast on a live key
    yield


app = FastAPI(title="LogiAgent API", version="0.2.0", lifespan=lifespan)


# ---------- auth ----------

def current_user(authorization: str = Header(default=""), s: Session = Depends(db.get_session)) -> User:
    token = authorization.removeprefix("Bearer ").strip()
    row = s.get(AuthToken, token) if token else None
    if not row:
        raise HTTPException(401, "Войдите в систему")
    return s.get(User, row.user_id)


def role(*allowed: str):
    def check(user: User = Depends(current_user)) -> User:
        if user.role not in allowed and user.role != "admin":
            raise HTTPException(403, "Недостаточно прав для этого раздела")
        return user
    return check


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, s: Session = Depends(db.get_session)):
    user = s.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not db.check_password(body.password, user.password_hash):
        raise HTTPException(401, "Неверная почта или пароль")
    token = secrets.token_urlsafe(32)
    s.add(AuthToken(token=token, user_id=user.id))
    s.commit()
    return {"token": token, "user": user_out(user)}


@app.post("/api/logout")
def logout(authorization: str = Header(default=""), s: Session = Depends(db.get_session)):
    row = s.get(AuthToken, authorization.removeprefix("Bearer ").strip())
    if row:
        s.delete(row)
        s.commit()
    return {"ok": True}


def user_out(u: User) -> dict:
    return {"email": u.email, "name": u.name, "role": u.role}


def company_out(c: Company) -> dict:
    p = config.PLANS[c.plan]
    return {"name": c.name, "plan": c.plan, "plan_name": p["name"], "used": c.used, "limit": p["limit"]}


@app.get("/api/me")
def me(user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    return {"user": user_out(user), "company": company_out(s.get(Company, user.company_id))}


@app.get("/api/status")
def status():
    return {"agent": "llm" if config.llm_enabled() else "rules", "provider": config.provider(), "model": config.model_name(),
            "threshold": config.threshold(), "payments": billing.mode()}


# ---------- requests (intake agent) ----------

def request_out(r: Request) -> dict:
    return {"id": r.id, "text": r.text, "origin": r.origin, "destination": r.destination, "weight_t": r.weight_t,
            "date": r.date, "deadline": r.deadline, "vehicle": r.vehicle, "vehicle_guess": r.vehicle_guess,
            "address": r.address, "cargo": r.cargo, "confidence": r.confidence, "status": r.status,
            "reason": r.reason, "agent_mode": r.agent_mode, "carriers": r.carriers, "km": r.km, "eta": r.eta,
            "kind": r.kind, "doc": r.doc, "created_at": r.created_at.isoformat(timespec="minutes")}


class RequestIn(BaseModel):
    text: str = Field(min_length=8, max_length=3000)


@app.post("/api/requests")
def create_request(body: RequestIn, user: User = Depends(role("client", "owner")), s: Session = Depends(db.get_session)):
    company = s.get(Company, user.company_id)
    limit = config.PLANS[company.plan]["limit"]
    if company.used >= limit:
        raise HTTPException(402, f"Лимит тарифа {config.PLANS[company.plan]['name']} исчерпан ({limit} заявок). Смените тариф.")
    result = intake.process(body.text, s.scalars(select(Carrier)).all())
    f = result["fields"]
    r = Request(company_id=company.id, created_by=user.id, text=body.text, origin=f["origin"],
                destination=f["destination"], weight_t=f["weight_t"], date=f["date"], deadline=f["deadline"],
                vehicle=f["vehicle"], vehicle_guess=result["vehicle_guess"], address=f["address"], cargo=f["cargo"],
                confidence=result["confidence"], status=result["status"], reason=result["reason"],
                agent_mode=result["mode"], carriers=result["carriers"], km=result["km"])
    s.add(r)
    s.flush()
    company.used += 1
    action = f"Создана заявка № {r.id}" if r.status == "auto" else f"Заявка № {r.id} передана менеджеру: {r.reason}"
    s.add(LogEntry(company_id=company.id, agent="Агент заявок", action=action[:300], confidence=r.confidence,
                   result="auto" if r.status == "auto" else "manual"))
    s.commit()
    return {"request": request_out(r), "missing": result["missing"], "company": company_out(company)}


@app.get("/api/requests")
def list_requests(user: User = Depends(current_user), s: Session = Depends(db.get_session)):
    q = select(Request).where(Request.company_id == user.company_id).order_by(Request.id.desc())
    if user.role == "client":
        q = q.where((Request.created_by == user.id) | (Request.agent_mode == "seed"), Request.kind == "request")
    return [request_out(r) for r in s.scalars(q.limit(50))]


# ---------- manager queue ----------

@app.get("/api/queue")
def queue(user: User = Depends(role("manager")), s: Session = Depends(db.get_session)):
    rows = s.scalars(select(Request).where(Request.company_id == user.company_id, Request.status == "review").order_by(Request.id))
    return [request_out(r) for r in rows]


class ResolveIn(BaseModel):
    action: Literal["confirm", "accept_doc", "return"]
    fields: dict[str, str] = {}


EDITABLE = {"origin", "destination", "weight_t", "date", "vehicle", "address"}


@app.post("/api/requests/{rid}/resolve")
def resolve(rid: int, body: ResolveIn, user: User = Depends(role("manager")), s: Session = Depends(db.get_session)):
    r = s.get(Request, rid)
    if not r or r.company_id != user.company_id or r.status != "review":
        raise HTTPException(404, "Заявка не найдена в очереди")
    if body.action == "return":
        r.status = "returned"
        action = f"Заявка № {r.id} возвращена клиенту на уточнение"
    else:
        if body.action == "accept_doc" and r.doc:
            r.weight_t = r.doc.get("weight_t", r.weight_t)
        for key, value in body.fields.items():
            if key in EDITABLE and value.strip():
                if key == "weight_t":
                    try:
                        value = float(value.replace(",", "."))
                    except ValueError:
                        raise HTTPException(422, "Вес укажите числом, например 12 или 0,8")
                setattr(r, key, value)
        missing = [k for k in ("origin", "destination", "weight_t", "date", "vehicle") if getattr(r, k) in (None, "")]
        if missing:
            raise HTTPException(422, "Заполните пустые поля или верните заявку клиенту")
        r.status = "confirmed"
        r.km, r.carriers = rank_carriers(s.scalars(select(Carrier)).all(), r.origin, r.destination, r.vehicle)
        action = f"Заявка № {r.id} подтверждена менеджером"
    s.add(LogEntry(company_id=r.company_id, agent=f"Менеджер · {user.name}", action=action, result="manual"))
    s.commit()
    return request_out(r)


# ---------- support agent ----------

class SupportIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[dict] = []


@app.post("/api/support")
def support_chat(body: SupportIn, user: User = Depends(role("client", "owner")), s: Session = Depends(db.get_session)):
    q = select(Request).where(Request.company_id == user.company_id, Request.kind == "request")
    if user.role == "client":
        q = q.where((Request.created_by == user.id) | (Request.agent_mode == "seed"))
    requests = list(s.scalars(q.order_by(Request.id)))

    def escalate(reason: str):
        s.add(LogEntry(company_id=user.company_id, agent="Агент поддержки", result="manual",
                       action=f"Вопрос передан менеджеру: {reason}"[:300]))

    result = support.answer(body.message, body.history, requests, escalate)
    if not result["escalated"]:
        s.add(LogEntry(company_id=user.company_id, agent="Агент поддержки", result="auto",
                       confidence=result["confidence"], action=f"Ответ на вопрос «{body.message[:60]}»"))
    s.commit()
    return result


# ---------- billing ----------

@app.get("/api/billing")
def billing_info(user: User = Depends(role("owner")), s: Session = Depends(db.get_session)):
    return {"company": company_out(s.get(Company, user.company_id)), "plans": config.PLANS, "mode": billing.mode()}


class CheckoutIn(BaseModel):
    plan: Literal["start", "business", "pro"]


@app.post("/api/billing/checkout")
def checkout(body: CheckoutIn, user: User = Depends(role("owner")), s: Session = Depends(db.get_session)):
    company = s.get(Company, user.company_id)
    if body.plan == company.plan:
        raise HTTPException(409, "Этот тариф уже подключён")
    if config.PLANS[body.plan]["limit"] < company.used:
        raise HTTPException(409, "Лимит этого тарифа меньше уже использованных заявок")
    try:
        result = billing.start_checkout(s, company, body.plan)
    except stripe.StripeError as e:
        raise HTTPException(502, f"Stripe не принял запрос: {e.user_message or e}")
    s.commit()
    return {**result, "company": company_out(company)}


class ConfirmIn(BaseModel):
    session_id: str


@app.post("/api/billing/confirm")
def confirm(body: ConfirmIn, user: User = Depends(role("owner")), s: Session = Depends(db.get_session)):
    company = s.get(Company, user.company_id)
    try:
        payment = billing.confirm(s, company, body.session_id)
    except stripe.StripeError as e:
        raise HTTPException(502, f"Stripe не ответил: {e.user_message or e}")
    if not payment:
        raise HTTPException(404, "Платёж не найден")
    s.commit()
    return {"status": payment.status, "plan": payment.plan, "company": company_out(company)}


class DemoPayIn(BaseModel):
    plan: Literal["business", "pro"]
    card: str


@app.post("/api/billing/demo-pay")
def demo_pay(body: DemoPayIn, user: User = Depends(role("owner")), s: Session = Depends(db.get_session)):
    if billing.mode() != "demo":
        raise HTTPException(409, "Подключён Stripe: оплата идёт через Stripe Checkout")
    company = s.get(Company, user.company_id)
    if body.plan == company.plan:
        raise HTTPException(409, "Этот тариф уже подключён")
    error = billing.demo_pay(s, company, body.plan, body.card)
    if error:
        raise HTTPException(422, error)
    s.commit()
    return {"status": "paid", "plan": body.plan, "company": company_out(company)}


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: HttpRequest, s: Session = Depends(db.get_session)):
    if not config.stripe_webhook_secret():
        raise HTTPException(404, "Webhook не настроен")
    try:
        billing.handle_webhook(s, await request.body(), request.headers.get("stripe-signature", ""))
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Неверная подпись Stripe")
    s.commit()
    return {"ok": True}


# ---------- admin ----------

@app.get("/api/admin/overview")
def overview(user: User = Depends(role("admin")), s: Session = Depends(db.get_session)):
    company = s.get(Company, user.company_id)
    counts = dict(s.execute(select(Request.status, func.count()).where(Request.company_id == company.id)
                            .group_by(Request.status)).all())
    auto = counts.get("auto", 0) + counts.get("transit", 0)
    manual = counts.get("review", 0) + counts.get("confirmed", 0) + counts.get("returned", 0)
    log_rows = s.scalars(select(LogEntry).where(LogEntry.company_id == company.id).order_by(LogEntry.id.desc()).limit(40))
    return {
        "company": company_out(company),
        "metrics": {"total": auto + manual, "auto": auto, "manual": manual, "queue": counts.get("review", 0)},
        "log": [{"at": e.at.strftime("%d.%m %H:%M"), "agent": e.agent, "action": e.action,
                 "confidence": e.confidence, "result": e.result} for e in log_rows],
    }


@app.post("/api/admin/reset")
def reset(user: User = Depends(role("admin"))):
    """Recreate the demo database (keeps the current login)."""
    db.Base.metadata.drop_all(db._engine)
    db.init(str(db._engine.url.render_as_string(hide_password=False)))
    with db._Session() as s:
        token = secrets.token_urlsafe(32)
        u = s.scalar(select(User).where(User.email == user.email))
        s.add(AuthToken(token=token, user_id=u.id))
        s.commit()
    return {"ok": True, "token": token}


# ---------- frontend ----------

@app.get("/")
def index():
    return FileResponse(config.FRONTEND_DIR / "index.html")
