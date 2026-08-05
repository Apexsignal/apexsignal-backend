"""
config.py — Načtení konfigurace z proměnných prostředí.

Appka je samostatná služba (vlastní Render web service + vlastní Postgres),
NEsdílí runtime ani DB s ApexSignal backendem — jen bydlí ve stejném
repozitáři kvůli tomu, jak byla založená tahle vývojová větev.
"""
from __future__ import annotations

import os


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} není nastavená proměnná prostředí — bez ní appka nemůže "
            "bezpečně běžet. Nastav ji na Renderu (nebo lokálně v .env)."
        )
    return value


class Settings:
    @property
    def database_url(self) -> str:
        url = _require("DATABASE_URL")
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    @property
    def admin_task_key(self) -> str:
        return os.environ.get("ADMIN_TASK_KEY", "")

    @property
    def public_base_url(self) -> str:
        # URL, na kterou appka staví potvrzovací odkaz v SMS, např.
        # https://masaz-booking.onrender.com — BEZ lomítka na konci.
        return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

    @property
    def booking_confirm_ttl_minutes(self) -> int:
        return int(os.environ.get("BOOKING_CONFIRM_TTL_MINUTES", "10"))

    @property
    def slot_granularity_minutes(self) -> int:
        return int(os.environ.get("SLOT_GRANULARITY_MINUTES", "30"))

    @property
    def scarcity_min_ratio(self) -> float:
        return float(os.environ.get("SCARCITY_MIN_RATIO", "0.2"))

    @property
    def scarcity_max_ratio(self) -> float:
        return float(os.environ.get("SCARCITY_MAX_RATIO", "0.4"))

    @property
    def timezone_name(self) -> str:
        return os.environ.get("APP_TIMEZONE", "Europe/Prague")

    # --- SMSbrana.cz (SMS Connect) ---
    @property
    def smsbrana_login(self) -> str:
        return os.environ.get("SMSBRANA_LOGIN", "")

    @property
    def smsbrana_password(self) -> str:
        return os.environ.get("SMSBRANA_PASSWORD", "")

    @property
    def sms_sender_backend(self) -> str:
        # "smsbrana" (výchozí) nebo "console" (jen loguje zprávu — pro lokální
        # vývoj/testování bez utracení kreditu za SMS)
        return os.environ.get("SMS_SENDER_BACKEND", "smsbrana")


settings = Settings()
