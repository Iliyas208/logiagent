"""Database models and demo seed data (SQLAlchemy 2; SQLite by default, PostgreSQL via DATABASE_URL)."""
import hashlib
import os
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    plan: Mapped[str] = mapped_column(String(20), default="business")
    used: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # client | manager | owner | admin
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    password_hash: Mapped[str] = mapped_column(String(200))


class AuthToken(Base):
    __tablename__ = "auth_tokens"
    token: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Carrier(Base):
    __tablename__ = "carriers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    rate_k: Mapped[float] = mapped_column(Float)       # price multiplier vs. market rate
    reliability: Mapped[float] = mapped_column(Float)  # share of on-time deliveries, 0..1


class Request(Base):
    __tablename__ = "requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    text: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str | None] = mapped_column(String(100), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(100), nullable=True)
    weight_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    date: Mapped[str | None] = mapped_column(String(20), nullable=True)      # YYYY-MM-DD
    deadline: Mapped[bool] = mapped_column(default=False)                    # "до" date
    vehicle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vehicle_guess: Mapped[bool] = mapped_column(default=False)
    address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cargo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))  # auto | review | confirmed | returned | transit
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    agent_mode: Mapped[str] = mapped_column(String(40), default="rules")
    carriers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    eta: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="request")   # request | doc
    doc: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # waybill fields for kind=doc


class LogEntry(Base):
    __tablename__ = "log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    agent: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(300))
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(20))  # auto | manual


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    plan: Mapped[str] = mapped_column(String(20))
    amount_usd: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(20))  # stripe | demo | free
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20))    # pending | paid
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


_engine = None
_Session = None


def init(url: str | None = None):
    """Create the engine, tables and demo data. Called on startup and in tests."""
    global _engine, _Session
    url = url or config.database_url()
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    _engine = create_engine(url, **kwargs)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    with _Session() as s:
        if not s.scalar(select(Company).limit(1)):
            seed(s)
            s.commit()


def get_session():
    with _Session() as s:
        yield s


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(8).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    salt = stored.split("$", 1)[0]
    return hash_password(password, salt) == stored


DEMO_PASSWORD = "demo123"
DEMO_USERS = [
    ("client@logiagent.test", "ООО «Север-Транс»", "client"),
    ("manager@logiagent.test", "Айдана", "manager"),
    ("owner@logiagent.test", "Владелец", "owner"),
    ("admin@logiagent.test", "Диас", "admin"),
]


def seed(s: Session):
    company = Company(id=1, name="ООО «Север-Транс»", plan="business", used=312)
    s.add(company)
    s.flush()
    for email, name, role in DEMO_USERS:
        s.add(User(email=email, name=name, role=role, company_id=1, password_hash=hash_password(DEMO_PASSWORD)))
    for name, k, rel in [("ТОО «Қазына Транс»", 1.00, .94), ("ИП «Степной ветер»", .95, .81),
                         ("ТОО «АстанаЛогистик»", 1.08, .96), ("ТОО «Жол Сервис»", 1.02, .88)]:
        s.add(Carrier(name=name, rate_k=k, reliability=rel))
    common = dict(company_id=1, agent_mode="seed")
    s.add_all([
        Request(id=1042, status="transit", confidence=93, eta="завтра до 18:00", origin="Алматы", destination="Астана",
                weight_t=12, date="2026-11-20", deadline=True, vehicle="Тентованная фура, 20 т",
                address="ул. Кабанбай батыра, 12", text="12 т стройматериалов из Алматы в Астану", **common),
        Request(id=1048, status="review", confidence=61, kind="doc", reason="Вес в накладной больше на 0,8 т",
                origin="Алматы", destination="Астана", weight_t=12, date="2026-11-25", vehicle="Тентованная фура, 20 т",
                address="ул. Кабанбай батыра, 12", text="12 т стройматериалов из Алматы в Астану",
                doc={"origin": "Алматы", "destination": "Астана", "weight_t": 12.8, "date": "2026-11-25",
                     "address": "ул. Кабанбай батыра, 12"}, **common),
        Request(id=1051, status="review", confidence=54, reason="Не указана дата доставки",
                text="Перевезти 8 тонн муки из Шымкента в Караганду, срочно.", origin="Шымкент",
                destination="Караганда", weight_t=8, vehicle="Среднетоннажный, 10 т", vehicle_guess=True, **common),
        Request(id=1055, status="review", confidence=68, reason="Неоднозначный адрес выгрузки",
                text="12 т цемента из Шымкента в Тараз 28 ноября, тент, выгрузка у рынка.", origin="Шымкент",
                destination="Тараз", weight_t=12, date="2026-11-28", vehicle="Тентованная фура, 20 т", **common),
    ])
    for agent, action, conf, res in [
        ("Агент заявок", "Создана заявка № 1050", 91, "auto"),
        ("Агент заявок", "Не указана дата в заявке № 1051", 54, "manual"),
        ("Агент поддержки", "Ответ о статусе заявки № 1042", 97, "auto"),
        ("Агент документов", "Накладная № 1048 не совпала с заявкой по весу", 61, "manual"),
        ("Агент заявок", "Создана заявка № 1052", 94, "auto"),
    ]:
        s.add(LogEntry(company_id=1, agent=agent, action=action, confidence=conf, result=res))

