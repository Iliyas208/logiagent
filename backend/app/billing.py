"""Plan payments: Stripe Checkout in TEST mode, or a built-in demo payment when no Stripe key is set."""
import stripe
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .db import Company, LogEntry, Payment

TEST_CARD = "4242424242424242"


def mode() -> str:
    return "stripe-test" if config.stripe_key() else "demo"


def apply_plan(s: Session, company: Company, plan: str, note: str):
    old = config.PLANS[company.plan]["name"]
    company.plan = plan
    s.add(LogEntry(company_id=company.id, agent="Оплата", result="auto",
                   action=f"Тариф изменён: {old} → {config.PLANS[plan]['name']} ({note})"))


def start_checkout(s: Session, company: Company, plan: str) -> dict:
    p = config.PLANS[plan]
    if p["price"] == 0:
        s.add(Payment(company_id=company.id, plan=plan, amount_usd=0, provider="free", status="paid"))
        apply_plan(s, company, plan, "бесплатный тариф")
        return {"mode": "free", "plan": plan}
    if mode() == "demo":
        return {"mode": "demo", "plan": plan}

    stripe.api_key = config.stripe_key()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{
            "price_data": {"currency": "usd", "unit_amount": p["price"] * 100, "recurring": {"interval": "month"},
                           "product_data": {"name": f"LogiAgent {p['name']}: {p['limit']} заявок в месяц"}},
            "quantity": 1,
        }],
        success_url=f"{config.public_url()}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}#checkout",
        cancel_url=f"{config.public_url()}/#pricing",
        client_reference_id=str(company.id),
        metadata={"company_id": str(company.id), "plan": plan},
    )
    s.add(Payment(company_id=company.id, plan=plan, amount_usd=p["price"], provider="stripe",
                  session_id=session.id, status="pending"))
    return {"mode": "stripe", "plan": plan, "url": session.url}


def complete_stripe_session(s: Session, data: dict) -> Payment | None:
    """Mark a Checkout Session as paid and switch the plan. Safe to call twice (return page + webhook)."""
    payment = s.scalar(select(Payment).where(Payment.session_id == data.get("id")))
    if not payment:
        return None
    paid = data.get("status") == "complete" and data.get("payment_status") in ("paid", "no_payment_required")
    if paid and payment.status != "paid":
        payment.status = "paid"
        company = s.get(Company, payment.company_id)
        apply_plan(s, company, payment.plan, f"Stripe, тестовый платёж ${payment.amount_usd}")
    return payment


def confirm(s: Session, company: Company, session_id: str) -> Payment | None:
    stripe.api_key = config.stripe_key()
    data = stripe.checkout.Session.retrieve(session_id).to_dict()
    if str((data.get("metadata") or {}).get("company_id")) != str(company.id):
        return None
    return complete_stripe_session(s, data)


def demo_pay(s: Session, company: Company, plan: str, card: str) -> str | None:
    """Simulated payment for running without Stripe keys. Accepts only Stripe's public test card."""
    if card.replace(" ", "") != TEST_CARD:
        return "В демо-режиме принимается только тестовая карта 4242 4242 4242 4242."
    p = config.PLANS[plan]
    s.add(Payment(company_id=company.id, plan=plan, amount_usd=p["price"], provider="demo", status="paid"))
    apply_plan(s, company, plan, f"демо-платёж ${p['price']}")
    return None


def handle_webhook(s: Session, payload: bytes, signature: str) -> bool:
    event = stripe.Webhook.construct_event(payload, signature, config.stripe_webhook_secret())
    if event["type"] == "checkout.session.completed":
        complete_stripe_session(s, event["data"]["object"].to_dict())
        return True
    return False
