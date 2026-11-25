"""Settings from environment variables (backend/.env is loaded if present).

Values are read on every call, so tests can change them with monkeypatch.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"
load_dotenv(BACKEND_DIR / ".env")

PLANS = {
    "start": {"name": "Старт", "price": 0, "limit": 50,
              "features": ["Агент заявок", "Журнал решений", "Поддержка по почте"]},
    "business": {"name": "Business", "price": 29, "limit": 500,
                 "features": ["Все три агента", "Сверка накладных", "Очередь для менеджера", "Метрики и журнал"]},
    "pro": {"name": "Pro", "price": 99, "limit": 3000,
            "features": ["Всё из Business", "Несколько менеджеров", "Приоритетная обработка", "Выгрузка отчётов"]},
}


def database_url() -> str:
    return os.getenv("DATABASE_URL") or f"sqlite:///{BACKEND_DIR / 'logiagent.db'}"


def threshold() -> int:
    return int(os.getenv("CONFIDENCE_THRESHOLD", "80"))


def claude_model() -> str:
    return os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"


def groq_model() -> str:
    return os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"


def provider() -> str | None:
    """Which LLM runs the agents: "claude", "groq" or None (rule-based fallback)."""
    mode = os.getenv("AGENT_MODE", "auto").lower()
    if mode == "rules":
        return None
    if mode in ("claude", "llm"):
        return "claude"
    if mode == "groq":
        return "groq"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "claude"
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    return None


def llm_enabled() -> bool:
    return provider() is not None


def model_name() -> str:
    return groq_model() if provider() == "groq" else claude_model()


def stripe_key() -> str:
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if key and not key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError("STRIPE_SECRET_KEY must be a TEST key (sk_test_...). Live keys are not allowed in this project.")
    return key


def stripe_webhook_secret() -> str:
    return os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()


def public_url() -> str:
    return os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
