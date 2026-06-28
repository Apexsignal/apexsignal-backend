"""
auth.py — jednoduchá autentizace appky (e-mail + heslo).

Appka záměrně nepoužívá žádnou externí knihovnu navíc (bcrypt, PyJWT) —
hashování heslel přes hashlib.pbkdf2_hmac (NIST/OWASP doporučený postup,
součást standardní knihovny Pythonu) a podepisované přihlašovací tokeny
přes hmac+hashlib (stejný princip jako JWT — payload + podpis — jen bez
závislosti navíc, kterou appka v tomhle vývojovém prostředí beze sítě
nemůže ověřit, že se vůbec nainstaluje). Pro appku tohoto rozsahu (osobní
nástroj, ne firma se statisíci uživatelů) je to přiměřené řešení.

Appka NIKDY nedůvěřuje user_id, co by si klient poslal sám v těle
požadavku — viz get_current_user_id v backend_api.py. Identitu appka
vždy odvozuje z podepsaného tokenu, jinak by si kdokoli mohl jen tak
"být" jiný uživatel a vidět jeho tikety.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

PBKDF2_ITERATIONS = 260_000  # OWASP doporučená dolní hranice pro PBKDF2-SHA256 (2023+)
TOKEN_VALIDITY_SECONDS = 60 * 60 * 24 * 30  # 30 dní — appka je osobní nástroj, ne banka


def _get_secret_key() -> bytes:
    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        raise RuntimeError(
            "SECRET_KEY není nastavená — appka bez ní nemůže bezpečně podepisovat "
            "přihlašovací tokeny. Vygeneruj si dlouhý náhodný řetězec (např. "
            "`python3 -c \"import secrets; print(secrets.token_hex(32))\"`) a vlož "
            "ho jako env var SECRET_KEY webové službě."
        )
    return secret.encode("utf-8")


def hash_password(password: str) -> str:
    """Vrátí 'salt$hash' jako jeden string — appka to uloží do jednoho DB sloupce."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Porovná heslo s uloženým hashem. hmac.compare_digest = porovnání v konstantním čase, ať appka neuteče timing útokem."""
    try:
        salt, hex_digest = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hex_digest)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def create_token(user_id: int) -> str:
    """
    Vrátí token tvaru 'payload.podpis' (stejný princip jako JWT). Payload
    nese user_id a čas expirace, appka ho nijak nešifruje — neukládej do
    něj nic citlivého, jen identifikátor.
    """
    payload = {"user_id": user_id, "exp": int(time.time()) + TOKEN_VALIDITY_SECONDS}
    payload_b64 = _b64encode(json.dumps(payload).encode("utf-8"))
    signature = hmac.new(_get_secret_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64encode(signature)}"


def verify_token(token: str) -> Optional[int]:
    """Vrátí user_id, pokud je token platný (podpis sedí) a nevypršel. Jinak None."""
    try:
        payload_b64, signature_b64 = token.split(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(_get_secret_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(signature_b64, _b64encode(expected_signature)):
        return None  # podpis nesedí — appka tokenu nevěří

    try:
        payload = json.loads(_b64decode(payload_b64))
    except Exception:
        return None

    if payload.get("exp", 0) < time.time():
        return None  # token vypršel
    return payload.get("user_id")
