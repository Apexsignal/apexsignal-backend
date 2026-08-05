"""
sms.py — Odeslání potvrzovací SMS s odkazem na potvrzení rezervace.

DŮLEŽITÉ: appka tady implementuje SMSbrana.cz SMS Connect API podle
veřejně známého (hash-based) schématu jejich HTTP rozhraní, ale v tomhle
prostředí appka neměla přístup k živému účtu SMSbrana ani se jí
nepodařilo stáhnout aktuální PDF dokumentaci (fetch vrátil jen prázdnou
stránku/XML chybu bez textu) — POZOR, appka to garantovaně neodeslala
a neověřila proti reálnému API. Před nasazením do provozu:
  1) založit účet na smsbrana.cz, zjistit si login/heslo pro SMS Connect,
  2) nastavit SMS_SENDER_BACKEND=console a projít si testovací rezervaci
     (appka jen vypíše, co by odeslala, žádný kredit se neutratí),
  3) přepnout na SMS_SENDER_BACKEND=smsbrana, poslat si SMS na vlastní
     číslo a ověřit skutečné doručení + přesné parametry podle aktuální
     dokumentace SMSbrana (pokud se liší, upravit jen SmsBranaSender níže).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from abc import ABC, abstractmethod

import requests

from .config import settings

logger = logging.getLogger("massage_booking.sms")

SMSBRANA_URL = "https://api.smsbrana.cz/smsconnect/http.asp"


class SmsSendError(Exception):
    pass


class SmsSender(ABC):
    @abstractmethod
    def send(self, phone: str, message: str) -> None:
        ...


class ConsoleSmsSender(SmsSender):
    """Pro lokální vývoj/test — appka SMS jen zaloguje, nic reálně neodešle."""

    def send(self, phone: str, message: str) -> None:
        logger.info("SMS (dry-run) na %s: %s", phone, message)


class SmsBranaSender(SmsSender):
    def __init__(self, login: str, password: str) -> None:
        self._login = login
        self._password = password

    def send(self, phone: str, message: str) -> None:
        if not self._login or not self._password:
            raise SmsSendError(
                "SMSBRANA_LOGIN/SMSBRANA_PASSWORD nejsou nastavené — appka nemůže odeslat SMS."
            )
        timestamp = str(int(time.time()))
        salt = secrets.token_hex(8)
        auth = hashlib.md5(f"{self._password}{timestamp}{salt}".encode("utf-8")).hexdigest()
        params = {
            "action": "send_sms",
            "login": self._login,
            "time": timestamp,
            "salt": salt,
            "auth": auth,
            "number": phone,
            "message": message,
        }
        try:
            response = requests.get(SMSBRANA_URL, params=params, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise SmsSendError(f"SMSbrana.cz nedostupná: {exc}") from exc

        # Odpověď je jednoduché XML <result><err>N</err>...</result>, err=0 = OK.
        if "<err>0</err>" not in response.text:
            raise SmsSendError(f"SMSbrana.cz vrátila chybu: {response.text.strip()}")


def get_sms_sender() -> SmsSender:
    if settings.sms_sender_backend == "console":
        return ConsoleSmsSender()
    return SmsBranaSender(settings.smsbrana_login, settings.smsbrana_password)


def send_confirmation_sms(phone: str, confirm_token: str) -> None:
    if not settings.public_base_url:
        raise SmsSendError(
            "PUBLIC_BASE_URL není nastavená — appka neví, jaký odkaz do SMS poslat."
        )
    link = f"{settings.public_base_url}/confirm/{confirm_token}"
    message = f"Potvrzeni rezervace masaze: {link} (platnost {settings.booking_confirm_ttl_minutes} min)"
    get_sms_sender().send(phone, message)
