"""Rule-based fallback for when no LLM is available. It is NOT an AI agent; the UI labels its results as rules."""
import re
from datetime import date, timedelta

from .geo import CITIES

MONTHS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("июн", 6), ("июл", 7), ("август", 8),
          ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12), ("ма", 5)]


def _make_date(d: int, m: int, y: int | None, today: date) -> str | None:
    year = y or today.year
    try:
        dt = date(year, m, d)
        if y is None and dt < today:
            dt = date(year + 1, m, d)
    except ValueError:
        return None
    return dt.isoformat()


def extract(text: str, today: date | None = None) -> dict:
    today = today or date.today()
    t = " " + re.sub(r"\s+", " ", text) + " "
    low = t.lower()
    f = {"origin": None, "destination": None, "weight_t": None, "date": None, "deadline": False,
         "vehicle_type": None, "unload_address": None, "cargo": None}

    found = []
    for name, stem, _, _ in CITIES:
        m = re.search(stem, low)
        if m:
            found.append({"name": name, "i": m.start(), "role": None})
    found.sort(key=lambda c: c["i"])
    for c in found:
        before = low[max(0, c["i"] - 5):c["i"]]
        if re.search(r"(из|от)\s$", before) and not f["origin"]:
            c["role"] = "origin"
            f["origin"] = c["name"]
        elif re.search(r"(в|во|до|на)\s$", before) and not f["destination"]:
            c["role"] = "destination"
            f["destination"] = c["name"]
    rest = [c["name"] for c in found if not c["role"]]
    if not f["origin"] and rest and rest[0] != f["destination"]:
        f["origin"] = rest.pop(0)
    if not f["destination"] and rest:
        f["destination"] = rest.pop(0)
    if f["origin"] == f["destination"]:
        f["destination"] = None

    wm = re.search(r"(\d+(?:[.,]\d+)?)\s*(тонн[а-яё]*|тн|т(?![а-яё])|кг|килограмм[а-яё]*)", t, re.I)
    no_w = t
    if wm:
        v = float(wm.group(1).replace(",", "."))
        if wm.group(2).lower().startswith("к"):
            v /= 1000
        f["weight_t"] = round(v, 2)
        no_w = t.replace(wm.group(0), " ")

    dm = re.search(r"(до\s+)?(\d{1,2})\s+(январ|феврал|марта?|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-яё]*", no_w, re.I)
    dn = re.search(r"(до\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", no_w)
    if dm:
        stem = dm.group(3).lower()
        month = next(n for s, n in MONTHS if stem.startswith(s))
        f["date"] = _make_date(int(dm.group(2)), month, None, today)
        f["deadline"] = bool(dm.group(1))
    elif dn and 1 <= int(dn.group(3)) <= 12:
        y = int(dn.group(4)) if dn.group(4) else None
        if y and y < 100:
            y += 2000
        f["date"] = _make_date(int(dn.group(2)), int(dn.group(3)), y, today)
        f["deadline"] = bool(dn.group(1))
    elif "послезавтра" in low:
        f["date"] = (today + timedelta(days=2)).isoformat()
    elif "завтра" in low:
        f["date"] = (today + timedelta(days=1)).isoformat()

    if re.search(r"реф|холодил|заморож", low):
        f["vehicle_type"] = "Рефрижератор"
    elif "изотерм" in low:
        f["vehicle_type"] = "Изотермический фургон"
    elif "тент" in low:
        f["vehicle_type"] = "Тентованная фура, 20 т"
    elif re.search(r"бортов|открыт", low):
        f["vehicle_type"] = "Бортовая платформа"
    elif "контейнер" in low:
        f["vehicle_type"] = "Контейнеровоз"

    am = re.search(r"(ул\.|улица|пр-т|пр\.|проспект|мкр\.?|микрорайон)\s*([^.;!?\n]+)", t, re.I)
    if am:
        f["unload_address"] = re.sub(r"\s+", " ", f"{am.group(1)} {am.group(2).strip()}")[:120]
    return f


def answer(question: str, requests: list) -> tuple[str, int]:
    """Support answer without an LLM: looks up a 4-digit request number. Returns (text, confidence)."""
    m = re.search(r"(\d{4})", question)
    if not m:
        ids = ", ".join(f"№ {r.id}" for r in requests) or "нет"
        return f"Укажите номер заявки, и я скажу, где груз. Ваши заявки: {ids}.", 72
    r = next((x for x in requests if x.id == int(m.group(1))), None)
    if not r:
        return f"Заявку № {m.group(1)} не нашёл. Проверьте номер; если вопрос сложный, передам его менеджеру.", 40
    route = f"{r.origin or '?'} → {r.destination or '?'}"
    texts = {
        "transit": f"Заявка № {r.id} ({route}): машина в пути, прибытие {r.eta or 'по графику'}.",
        "auto": f"Заявка № {r.id} ({route}) создана. Перевозчик подтвердит погрузку в течение часа.",
        "review": f"Заявка № {r.id} на проверке у менеджера: {(r.reason or 'нужно уточнение').lower()}. Ответим в течение 30 минут.",
        "confirmed": f"Заявка № {r.id} ({route}) подтверждена менеджером и ждёт погрузки.",
        "returned": f"По заявке № {r.id} менеджер просит уточнить данные: {(r.reason or '').lower()}.",
    }
    return texts.get(r.status, f"Статус заявки № {r.id}: {r.status}."), 97
