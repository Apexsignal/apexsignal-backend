"""
rate_limiter.py — appka chrání /auth/login a /auth/register proti hrubé
síle (brute-force zkoušení hesel, hromadná registrace, zjišťování,
které e-maily jsou zaregistrované).

Appka tohle záměrně řeší čistě v paměti procesu — žádná nová závislost,
žádná DB tabulka navíc. Ztráta počítadel při restartu je tolerovatelná
(appka jen "zapomene" dosavadní pokusy), na rozdíl od tiketové historie,
kde by to vadilo. Na víc instancí webové služby najednou by tohle chtělo
přesunout do DB/Redis — pro appku v rozsahu jedné instance (Render free
tier) je in-memory v pořádku.

Appka sleduje NEÚSPĚŠNÉ pokusy ve dvou rovinách zároveň:
- podle e-mailu — chrání JEDEN konkrétní účet před zkoušením hesel
- podle IP adresy — chrání před zkoušením MNOHA různých e-mailů z jednoho
  zdroje (sprej/enumerace), i kdyby každý jednotlivý účet dostal jen
  pár pokusů
"""
from __future__ import annotations

import time
from collections import defaultdict

WINDOW_SECONDS = 15 * 60     # appka sleduje pokusy v klouzavém 15minutovém okně
MAX_ATTEMPTS_PER_EMAIL = 5   # po 5 neúspěšných pokusech na TENHLE e-mail appka dočasně odmítá další
MAX_ATTEMPTS_PER_IP = 20     # po 20 neúspěšných pokusech z JEDNÉ IP (klidně na různé e-maily) appka odmítá taky

_failed_attempts: dict[str, list[float]] = defaultdict(list)


def _prune(key: str) -> None:
    """Appka zahodí pokusy starší než WINDOW_SECONDS — tím lockout sám 'vyprší', appka nepotřebuje žádný separátní časovač."""
    cutoff = time.time() - WINDOW_SECONDS
    _failed_attempts[key] = [t for t in _failed_attempts[key] if t > cutoff]


def is_locked_out(email: str, ip: str) -> bool:
    email_key, ip_key = f"email:{email.strip().lower()}", f"ip:{ip}"
    _prune(email_key)
    _prune(ip_key)
    return len(_failed_attempts[email_key]) >= MAX_ATTEMPTS_PER_EMAIL or len(_failed_attempts[ip_key]) >= MAX_ATTEMPTS_PER_IP


def record_failed_attempt(email: str, ip: str) -> None:
    now = time.time()
    _failed_attempts[f"email:{email.strip().lower()}"].append(now)
    _failed_attempts[f"ip:{ip}"].append(now)


def record_success(email: str, ip: str) -> None:
    """Appka po úspěchu vyčistí počítadlo pro TENHLE e-mail — IP nechá sledovanou dál (mohla zkoušet i jiné účty)."""
    _failed_attempts.pop(f"email:{email.strip().lower()}", None)
