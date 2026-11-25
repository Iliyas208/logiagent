"""City directory, road distance estimate and carrier ranking."""
import math
import re

# name, stem regex (any case form), lat, lon
CITIES = [
    ("Алматы", r"алмат", 43.24, 76.89), ("Астана", r"астан", 51.17, 71.45),
    ("Шымкент", r"шымкент", 42.32, 69.59), ("Караганда", r"караганд", 49.80, 73.10),
    ("Актобе", r"актобе|актюбинск", 50.28, 57.17), ("Атырау", r"атырау", 47.09, 51.92),
    ("Павлодар", r"павлодар", 52.29, 76.97), ("Костанай", r"костана", 53.21, 63.62),
    ("Тараз", r"тараз", 42.90, 71.37), ("Усть-Каменогорск", r"усть-каменогорск|оскемен", 49.95, 82.61),
    ("Семей", r"семе[йя]|семипалатинск", 50.41, 80.23), ("Актау", r"актау", 43.65, 51.17),
    ("Кызылорда", r"кызылорд", 44.85, 65.51), ("Уральск", r"уральск", 51.23, 51.37),
    ("Петропавловск", r"петропавловск", 54.87, 69.15), ("Туркестан", r"туркестан", 43.30, 68.25),
]

# market rate for a full vehicle, tenge per km
RATES = [("Газель", 120), ("Малотоннаж", 180), ("Среднетоннаж", 240), ("Рефриж", 400), ("Изотерм", 400)]


def normalize_city(name: str | None) -> str | None:
    """Map any spelling or case form ("Алматы", "из Алматы", "Караганду") to the directory name."""
    if not name:
        return None
    low = name.lower()
    for city, stem, _, _ in CITIES:
        if re.search(stem, low):
            return city
    return name.strip() or None


def distance_km(a: str | None, b: str | None) -> int | None:
    pa = next((c for c in CITIES if c[0] == a), None)
    pb = next((c for c in CITIES if c[0] == b), None)
    if not pa or not pb or pa is pb:
        return None
    r = math.pi / 180
    dlat, dlon = (pb[2] - pa[2]) * r, (pb[3] - pa[3]) * r
    h = math.sin(dlat / 2) ** 2 + math.cos(pa[2] * r) * math.cos(pb[2] * r) * math.sin(dlon / 2) ** 2
    return round(2 * 6371 * math.asin(math.sqrt(h)) * 1.22)  # 1.22 = road vs. straight line


def vehicle_for_weight(weight_t: float) -> str:
    if weight_t <= 1.5:
        return "Газель, 1,5 т"
    if weight_t <= 5:
        return "Малотоннажный, 5 т"
    if weight_t <= 10:
        return "Среднетоннажный, 10 т"
    return "Тентованная фура, 20 т"


def rank_carriers(carriers, origin, destination, vehicle):
    """Top-3 carriers by a mix of price (60%) and reliability (40%). Returns (km, list) or (None, None)."""
    km = distance_km(origin, destination)
    if not km:
        return None, None
    rate = next((r for key, r in RATES if vehicle and key in vehicle), 330)
    base = km * rate
    ranked = sorted(
        ({"name": c.name, "price": round(base * c.rate_k / 1000) * 1000, "reliability": c.reliability,
          "score": c.rate_k * 0.6 + (1 - c.reliability) * 2} for c in carriers),
        key=lambda x: x["score"],
    )
    return km, [{k: v for k, v in c.items() if k != "score"} for c in ranked[:3]]
