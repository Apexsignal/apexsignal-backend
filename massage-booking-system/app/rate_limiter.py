"""
rate_limiter.py — appka veřejný rezervační formulář posílá SMS na LIBOVOLNÉ
zadané telefonní číslo, což je samo o sobě zneužitelné jako "SMS bombing"
(někdo zadá cizí číslo a nechá appku spamovat oběť potvrzovacími SMS,
navíc appce roste účet za odeslané zprávy). Appka proto sleduje počet
rezervačních požadavků ve dvou rovinách zároveň — podle telefonního čísla
(chrání konkrétní číslo/oběť) a podle IP adresy (chrání proti jednomu
zdroji zkoušejícímu spoustu různých čísel).

In-memory, stejně jako v ApexSignal backendu — appka běží jako jedna
instance, restart si "odpustí" počítadla, což tady nevadí.
"""
from __future__ import annotations

import time
from collections import defaultdict

WINDOW_SECONDS = 60 * 60      # klouzavé okno jedné hodiny
MAX_REQUESTS_PER_PHONE = 3    # max. 3 rezervační SMS na stejné číslo za hodinu
MAX_REQUESTS_PER_IP = 10      # max. 10 rezervačních požadavků z jedné IP za hodinu

_requests: dict[str, list[float]] = defaultdict(list)


def _prune(key: str) -> None:
    cutoff = time.time() - WINDOW_SECONDS
    _requests[key] = [t for t in _requests[key] if t > cutoff]


def is_rate_limited(phone: str, ip: str) -> bool:
    phone_key, ip_key = f"phone:{phone}", f"ip:{ip}"
    _prune(phone_key)
    _prune(ip_key)
    return len(_requests[phone_key]) >= MAX_REQUESTS_PER_PHONE or len(_requests[ip_key]) >= MAX_REQUESTS_PER_IP


def record_request(phone: str, ip: str) -> None:
    now = time.time()
    _requests[f"phone:{phone}"].append(now)
    _requests[f"ip:{ip}"].append(now)
