"""
ApexSignal — Backend API
Modul: backend_api.py

REST vrstva spojující:
    - probability_model.TicketGenerator  (Generátor tiketů)
    - data_provider                       (zdroj dat ze sportovního API)
    - auth                                (přihlašování — e-mail + heslo)
    - db                                  (PostgreSQL perzistence tiketů a uživatelů)

Spuštění (dev):
    pip install fastapi uvicorn
    uvicorn backend_api:app --reload
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import os
import random
import secrets
import json
import requests
import aiohttp
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from probability_model import (
    TicketGenerator, MatchInput, Sport, MarketType, Ticket, SelectionCandidate, evaluate_selection_outcome,
    MarketEvaluator, SPORT_MARKETS, MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS,
)
import data_provider
import ai_reviewer
import db
import auth
import rate_limiter
import ticket_telegram
import email_service
import transparency_page
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import stripe

# Appka zpracovává zápasy SOUBĚŽNĚ (víc vláken najednou), ne jeden po
# druhém — viz _build_football_matches. Volání čekají hlavně na síť
# (API-Football, Open-Meteo), ne na CPU appky, takže vlákna appce reálně
# zkrátí celkový čas bez zvýšení spotřeby denní kvóty API (appka udělá
# stejný POČET volání, jen je nedělá postupně). Appka dřív držela 16,
# protože víc by narazilo na tehdejší limit 4 req/s (Pro plán) — appka by
# jen měla víc vláken čekajících na frontu limiteru, ne reálně víc
# souběžných požadavků. Po přechodu na Mega (12 req/s, viz
# _api_football_rate_limiter v data_provider.py) appka zvedla i tohle,
# ať appka tu vyšší propustnost reálně využije.
FIXTURE_ENRICHMENT_WORKERS = 40

# Sdílený stav pro SKUTEČNÝ postup obohacování zápasů (kolikátý z kolika
# appka právě zpracovala) — appka ho drží v paměti procesu, ne v DB,
# protože ho potřebuje jen po dobu jednoho generování (pár desítek
# sekund), a nevadí, že se po restartu ztratí. Páruje se jen přes
# appkou vygenerovaný request_id (viz TicketGenerateRequest), nikdy na
# uživatele. Stará se sama uklízí (TTL), ať dict časem neroste do
# nekonečna, kdyby generování náhodou nedoběhlo do konce (a tím pádem
# se neprovedlo finally, které za normálních okolností záznam smaže).
_GENERATION_PROGRESS: dict[str, dict] = {}
_GENERATION_PROGRESS_LOCK = threading.Lock()
_GENERATION_PROGRESS_TTL_SECONDS = 600


def _progress_set_total(request_id: Optional[str], total: int) -> None:
    if not request_id:
        return
    now = time.monotonic()
    with _GENERATION_PROGRESS_LOCK:
        _GENERATION_PROGRESS[request_id] = {"done": 0, "total": total, "ts": now}
        stale = [k for k, v in _GENERATION_PROGRESS.items() if now - v["ts"] > _GENERATION_PROGRESS_TTL_SECONDS]
        for k in stale:
            _GENERATION_PROGRESS.pop(k, None)


def _progress_increment(request_id: Optional[str]) -> None:
    if not request_id:
        return
    with _GENERATION_PROGRESS_LOCK:
        entry = _GENERATION_PROGRESS.get(request_id)
        if entry:
            entry["done"] += 1
            entry["ts"] = time.monotonic()


def _progress_clear(request_id: Optional[str]) -> None:
    if not request_id:
        return
    with _GENERATION_PROGRESS_LOCK:
        _GENERATION_PROGRESS.pop(request_id, None)


# Appka generování spouští na SAMOSTATNÉM vlákně a výsledek si appka
# odloží sem, ať appka nemusí držet otevřené jedno dlouhé HTTP spojení po
# celou dobu (1-3 minuty) — na telefonu appka mezitím klidně vypne/uspí a
# mobilní prohlížeč takové spojení na pozadí zabije (appka pak dostane
# "Load failed", i když server práci normálně dokončil). Appka si teď
# jen vyzvedne request_id skoro okamžitě a výsledek si vyzvedne až je
# hotový, přes krátké dotazy, co krátké výpadky přežijí.
_GENERATION_RESULTS: dict[str, dict] = {}
_GENERATION_RESULTS_LOCK = threading.Lock()
_GENERATION_RESULTS_TTL_SECONDS = 600


def _results_store(request_id: Optional[str], payload: dict) -> None:
    if not request_id:
        return
    now = time.monotonic()
    with _GENERATION_RESULTS_LOCK:
        _GENERATION_RESULTS[request_id] = {"payload": payload, "ts": now}
        stale = [k for k, v in _GENERATION_RESULTS.items() if now - v["ts"] > _GENERATION_RESULTS_TTL_SECONDS]
        for k in stale:
            _GENERATION_RESULTS.pop(k, None)


def _results_get(request_id: str) -> Optional[dict]:
    with _GENERATION_RESULTS_LOCK:
        entry = _GENERATION_RESULTS.get(request_id)
        return entry["payload"] if entry else None


# Logger setup
logger = logging.getLogger("apexsignal")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ApexSignal API", version="0.1.0")

# Appka teď bere skutečné platby, takže appka backend nesmí volat
# libovolná cizí stránka — jen appce vlastní frontend (Netlify).
ALLOWED_ORIGINS = [
    "https://apexsignal-app.netlify.app",
    "https://cheerful-tarsier-f89a91.netlify.app",  # starší doména appky, appka ji nechává pro jistotu funkční
    "https://apexsignal-tickets.netlify.app",  # nový Netlify účet (starému došel kredit, viz 24.7.)
    "https://apexsignal.cz",  # vlastní doména (Wedos DNS -> Netlify, viz 24.7.)
    "https://www.apexsignal.cz",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Appka na vlastní generování zákazníky zatím nemá zaplacené API kredity
# ve verzi, co by uneslo reálný provoz (jen appce vlastní denní účet pro
# kanál) — appka proto dočasně zamyká celou tuhle stranu appky:
# /tickets/generate, /tickets/regenerate A TAKÉ nákup tokenů
# (/payments/create-checkout-session), ať si zákazník nekoupí tokeny,
# které zatím nemá na co utratit. Appka to řídí přes proměnnou
# prostředí, ať jde zapnout okamžitě, beze změny kódu, jakmile na to
# appka bude mít.
CLIENT_TICKET_GENERATION_ENABLED = os.environ.get("CLIENT_TICKET_GENERATION_ENABLED", "true").strip().lower() != "false"

# Appka i přes globální zámek pustí generování appce vlastním testovacím
# účtům (viz GENERATION_ALLOWED_USER_IDS v Renderu) — appka si tak umí
# reálně vyzkoušet appku samotnou, aniž by musela odemknout generování
# úplně všem a riskovat vyčerpání rozpočtu na fotbalové API.
GENERATION_ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("GENERATION_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}


def _has_active_unlimited(user_id: int) -> bool:
    until = db.get_unlimited_until(user_id)
    return until is not None and until > datetime.now(timezone.utc)


def _require_generation_enabled(user_id: Optional[int] = None) -> None:
    if CLIENT_TICKET_GENERATION_ENABLED or (user_id is not None and user_id in GENERATION_ALLOWED_USER_IDS):
        return
    if user_id is not None and _has_active_unlimited(user_id):
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "Appka vlastní generování i nákup tokenů zatím připravuje a testuje — brzy bude "
            "dostupné. Mezitím zkus kanál na Telegramu na apexsignal.cz/transparentni-ucet."
        ),
    )


@app.get("/health")
def health():
    """Appka tohle používá jen na 'probuzení' serveru (Render free plán po
    nečinnosti usíná) — žádné vedlejší účinky, žádný přístup k DB/cache."""
    return {"status": "ok"}


_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme)) -> int:
    """
    FastAPI dependency — appka identitu uživatele ZÁSADNĚ odvozuje jen
    z podepsaného tokenu, nikdy z user_id, co by klient mohl poslat sám
    v těle požadavku. Appka tu používá HTTPBearer (ne obyčejný Header) —
    díky tomu appka v /docs nabízí skutečné tlačítko "Authorize", kam
    se token vloží jednou pro všechny endpointy najednou, ne ručně do
    každého jednotlivě.
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="Chybí přihlašovací token")
    user_id = auth.verify_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token je neplatný nebo vypršel — přihlas se znovu")
    return user_id


# =====================================================================
# Přihlašování — e-mail + heslo (viz auth.py)
# =====================================================================
class RegisterRequest(BaseModel):
    email: str
    password: str
    ref: Optional[str] = None  # doporučovací kód z ?ref= — appka ho appce pošle jen při registraci

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1] or len(v) > 255:
            raise ValueError("Zadej platný e-mail")
        return v


class VerifyEmailRequest(BaseModel):
    token: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user_id: int
    email: str
    is_new_user: bool = False


def _client_ip(request: Request) -> str:
    """
    Appka bere IP z X-Forwarded-For, pokud appka běží za proxy (Render
    appku vždycky takhle obaluje) — request.client.host by jinak vrátil
    IP samotného proxy serveru, ne skutečného návštěvníka.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _notify_owner_new_registration(email: str, source: str) -> None:
    """Appka appce pošle upozornění na appčin vlastní Telegram
    (TELEGRAM_CHAT_ID) při KAŽDÉ nové registraci — appka to volá jen
    tehdy, když appka reálně založila nový účet, ne při běžném loginu.
    Selhání appka jen zaloguje, nikdy tím nesmí shodit registraci."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        return
    try:
        _send_telegram_message(int(chat_id), f"🆕 Nová registrace ({source}): {email}")
    except Exception as e:
        print(f"[register] Nepodařilo se poslat Telegram upozornění: {e}")


@app.post("/auth/register", response_model=AuthResponse)
def register(req: RegisterRequest, request: Request):
    client_ip = _client_ip(request)
    if rate_limiter.is_locked_out(req.email, client_ip):
        raise HTTPException(status_code=429, detail="Příliš mnoho pokusů. Zkus to znovu za chvíli.")
    if len(req.password) < 8:
        rate_limiter.record_failed_attempt(req.email, client_ip)
        raise HTTPException(status_code=400, detail="Heslo musí mít aspoň 8 znaků")
    if db.get_user_by_email(req.email):
        rate_limiter.record_failed_attempt(req.email, client_ip)
        raise HTTPException(status_code=409, detail="Tenhle e-mail už je zaregistrovaný")
    user_id = db.create_user(req.email, auth.hash_password(req.password))
    rate_limiter.record_success(req.email, client_ip)

    # Doporučovací systém — appka referred_by nastaví jen TADY, při
    # registraci, a napořád (viz db.set_referred_by). Neplatný/neznámý kód
    # appka jen tiše ignoruje, ať appka registraci kvůli tomu nezablokuje.
    if req.ref:
        try:
            referrer_id = db.get_user_id_by_referral_code(req.ref)
            if referrer_id:
                db.set_referred_by(user_id, referrer_id)
        except Exception as e:
            print(f"[register] Nepodařilo se přiřadit doporučitele ({req.ref}): {e}")

    # Zkušební tokeny appka teď dá až PO ověření e-mailu (viz
    # /auth/verify-email) — jinak šlo dokola zakládat účty s vymyšlenými
    # e-maily jen kvůli opakovanému zkušebnímu tiketu zdarma. Účet appka
    # založí a přihlásí hned (appka nechce novému uživateli blokovat
    # přihlášení), jen generování zůstane bez tokenů, dokud e-mail
    # nepotvrdí.
    try:
        verify_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=24)
        db.create_email_verification_token(verify_token, user_id, expires_at)
        frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal.cz")
        verify_link = f"{frontend_url}/app/?verify_email={verify_token}"
        email_service.send_verification_email(req.email, verify_link)
    except Exception as e:
        print(f"[register] Ověřovací e-mail se nepodařilo odeslat: {e}")

    _notify_owner_new_registration(req.email, "e-mail")
    return AuthResponse(token=auth.create_token(user_id), user_id=user_id, email=req.email, is_new_user=True)


@app.post("/auth/verify-email")
def verify_email(req: VerifyEmailRequest):
    user_id = db.consume_email_verification_token(req.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Odkaz na potvrzení e-mailu je neplatný nebo vypršel")
    already_verified = db.is_email_verified(user_id)
    db.set_email_verified(user_id)
    if not already_verified:
        # Zkušební tokeny appka připíše až tady, přesně jednou — token je
        # jednorázový (consume_email_verification_token ho hned označí
        # jako použitý), takže appka nemůže omylem připsat dárek dvakrát.
        try:
            db.adjust_tokens(user_id, FREE_TRIAL_TOKENS, "uvítací zkušební tokeny (1. tiket zdarma, e-mail ověřen)")
        except Exception as e:
            print(f"[verify_email] Nepodařilo se přidat zkušební tokeny: {e}")
    return {"status": "E-mail potvrzen", "free_tokens_granted": not already_verified}


@app.get("/referral/my-code")
def get_my_referral_code(user_id: int = Depends(get_current_user_id)):
    code = db.get_or_create_referral_code(user_id)
    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal.cz")
    return {"code": code, "link": f"{frontend_url}/app/?ref={code}"}


@app.post("/auth/login", response_model=AuthResponse)
def login(req: LoginRequest, request: Request):
    client_ip = _client_ip(request)
    if rate_limiter.is_locked_out(req.email, client_ip):
        raise HTTPException(status_code=429, detail="Příliš mnoho pokusů o přihlášení. Zkus to znovu za chvíli.")
    user = db.get_user_by_email(req.email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        # appka záměrně hlásí stejnou chybu pro "e-mail neexistuje" i "heslo
        # nesedí" — jinak by appka útočníkovi prozradila, které e-maily
        # jsou zaregistrované.
        rate_limiter.record_failed_attempt(req.email, client_ip)
        raise HTTPException(status_code=401, detail="Špatný e-mail nebo heslo")
    rate_limiter.record_success(req.email, client_ip)
    return AuthResponse(token=auth.create_token(user["id"]), user_id=user["id"], email=user["email"])


class GoogleAuthRequest(BaseModel):
    credential: str  # ID token appka dostane z Google Identity Services na frontendu


@app.post("/auth/google", response_model=AuthResponse)
def google_auth(req: GoogleAuthRequest):
    """
    Appka ověří Google ID token (podpis i cílový klient appka ověří
    knihovnou google-auth, nedůvěřuje ničemu, co nedostane přímo od
    Google) a podle e-mailu z něj buď najde existující účet, nebo
    založí nový — novým Google účtům appka nastaví náhodné, nikdy
    nepoužité heslo (uživateli ho neřekne), aby běžné přihlášení
    heslem zůstalo funkční, pokud si ho uživatel někdy nastaví přes
    zapomenuté heslo.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=500, detail="Přihlášení přes Google zatím není nastavené")
    try:
        idinfo = google_id_token.verify_oauth2_token(req.credential, google_requests.Request(), client_id)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Neplatný Google token: {e}")

    email = idinfo.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Google účet nemá e-mail")

    user = db.get_user_by_email(email)
    is_new_user = False
    if not user:
        random_password = secrets.token_urlsafe(32)
        user_id = db.create_user(email, auth.hash_password(random_password))
        is_new_user = True
        # Google appce v idinfo posílá vlastní "email_verified" příznak —
        # appka mu věří (appka o tomhle tokenu už výš ověřila podpis i
        # cílového klienta), takže tu appka nepotřebuje appky vlastní
        # ověřovací e-mail: Google účet je ověřený hned, appka rovnou
        # připíše zkušební tokeny.
        if idinfo.get("email_verified"):
            db.set_email_verified(user_id)
            try:
                db.adjust_tokens(user_id, FREE_TRIAL_TOKENS, "uvítací zkušební tokeny (1. tiket zdarma, Google účet)")
            except Exception as e:
                print(f"[google_auth] Nepodařilo se přidat zkušební tokeny: {e}")
        try:
            email_service.send_welcome_email(email)
        except Exception as e:
            print(f"[google_auth] Uvítací e-mail se nepodařilo odeslat: {e}")
        _notify_owner_new_registration(email, "Google")
    else:
        user_id = user["id"]

    return AuthResponse(token=auth.create_token(user_id), user_id=user_id, email=email, is_new_user=is_new_user)


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request):
    client_ip = _client_ip(request)
    if rate_limiter.is_locked_out(req.email, client_ip):
        raise HTTPException(status_code=429, detail="Příliš mnoho pokusů. Zkus to znovu za chvíli.")
    rate_limiter.record_failed_attempt(req.email, client_ip)  # appka to počítá jako "pokus", ať appku nejde spamovat e-maily

    user = db.get_user_by_email(req.email)
    if user:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        db.create_password_reset_token(token, user["id"], expires_at)
        frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
        reset_link = f"{frontend_url}/reset-password.html?token={token}"
        try:
            email_service.send_password_reset_email(req.email, reset_link)
        except Exception as e:
            print(f"[forgot_password] E-mail se nepodařilo odeslat: {e}")

    # Appka VŽDY vrátí stejnou odpověď, ať existuje e-mail v appce nebo ne
    # — jinak by appka útočníkovi prozradila, které e-maily jsou zaregistrované.
    return {"status": "Pokud e-mail existuje, poslali jsme na něj odkaz na obnovení hesla."}


@app.post("/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Heslo musí mít aspoň 8 znaků")
    user_id = db.consume_password_reset_token(req.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Odkaz na obnovení hesla je neplatný nebo vypršel")
    db.update_password_hash(user_id, auth.hash_password(req.new_password))
    return {"status": "Heslo bylo změněno"}


class AdminSetPasswordRequest(BaseModel):
    email: str
    new_password: str


@app.post("/admin/set-password")
def admin_set_password(req: AdminSetPasswordRequest, request: Request):
    """
    Ruční nastavení hesla testovacímu účtu bez e-mailového reset flow —
    appka ho používá jen appka administrátorem chráněné (X-Admin-Key),
    pro účty s neznámým/ztraceným heslem testovacích schránek jako
    test3@test.cz, kam appka reálně e-mail doručit nemůže.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Heslo musí mít aspoň 8 znaků")
    user = db.get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    db.update_password_hash(user["id"], auth.hash_password(req.new_password))
    return {"status": "Heslo nastaveno"}


class AdminSetUnlimitedRequest(BaseModel):
    email: str
    days: int = 30


@app.post("/admin/set-unlimited")
def admin_set_unlimited(req: AdminSetUnlimitedRequest, request: Request):
    """Ruční nastavení neomezeného tarifu bez placení — appce se hodí na
    dohodnuté výjimky (partnerství, náprava platebního omylu) i appce
    samotné na ověření appky. days=0 unlimited rovnou appce vypne."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    user = db.get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    until = datetime.now(timezone.utc) + timedelta(days=req.days) if req.days > 0 else datetime.now(timezone.utc)
    db.set_unlimited_until(user["id"], until)
    return {"email": req.email, "unlimited_until": until.isoformat()}


class AdminProvisionAccountRequest(BaseModel):
    email: str
    password: str
    tokens: int = 0
    reset: bool = True  # appka existující účet vynuluje (historie tiketů, neomezený tarif, tokeny) před nahráním tokens


@app.post("/admin/provision-account")
def admin_provision_account(req: AdminProvisionAccountRequest, request: Request):
    """Ručně založí nebo kompletně resetuje účet (heslo appka nastaví bez
    e-mailového reset flow, stejně jako /admin/set-password) a nahraje
    přesně `tokens` tokenů — appka to používá pro účty, co appka chce
    'od nuly' (partnerství, náprava chyby, appka sama pro test)."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Heslo musí mít aspoň 8 znaků")

    user = db.get_user_by_email(req.email)
    was_existing = user is not None
    tickets_deleted = 0

    if not user:
        user_id = db.create_user(req.email, auth.hash_password(req.password))
    else:
        user_id = user["id"]
        db.update_password_hash(user_id, auth.hash_password(req.password))
        if req.reset:
            saved_tickets = repo.get_saved_tickets(user_id)
            for row in saved_tickets:
                db.delete_ticket(row["ticket_id"])
            tickets_deleted = len(saved_tickets)
            db.set_unlimited_until(user_id, None)
            current_balance = db.get_token_balance(user_id)
            if current_balance != 0:
                db.adjust_tokens(user_id, -current_balance, "ADMIN_RESET_ACCOUNT")

    if req.tokens:
        db.adjust_tokens(user_id, req.tokens, "ADMIN_PROVISION_ACCOUNT")

    return {
        "user_id": user_id,
        "email": req.email,
        "was_existing": was_existing,
        "reset_applied": was_existing and req.reset,
        "tickets_deleted": tickets_deleted,
        "token_balance": db.get_token_balance(user_id),
    }


class DeleteAccountRequest(BaseModel):
    password: str


@app.delete("/account")
def delete_account(req: DeleteAccountRequest, user_id: int = Depends(get_current_user_id)):
    """
    Appka pro smazání účtu vyžaduje znovu zadané heslo (nestačí jen
    platný přihlašovací token) — je to nevratná akce, appka appku chrání
    proti smazání kvůli ukradenému/zapomenutému odhlášení na cizím
    zařízení. Smaže se rovnou vše navázané (tikety, tokeny...) přes
    ON DELETE CASCADE — viz db.delete_user.
    """
    user = db.get_user_by_id(user_id)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        # 403, ne 401 — appka na frontendu bere JAKÝKOLIV 401 jako
        # vypršelou session a automaticky appku odhlásí (viz authFetch).
        # Tady jde o špatně zadané heslo k potvrzení akce, ne o neplatný
        # přihlašovací token — 401 by appku nechtěně odhlásilo místo
        # zobrazení chyby.
        raise HTTPException(status_code=403, detail="Špatné heslo")
    db.delete_user(user_id)
    return {"status": "Účet byl smazán"}


# =====================================================================
# Pydantic schémata (request/response kontrakty)
# =====================================================================
class TicketGenerateRequest(BaseModel):
    risk_level: int = Field(ge=0, le=60)  # appka BOOST (risk_level > 60) už nenabízí
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=5)  # Horizont: 1-5 dní (už ne konkrétní data)
    # Appka podle tohohle (appkou vygenerovaný náhodný řetězec, appka ho
    # nijak neověřuje ani nepáruje na uživatele) hlásí SKUTEČNÝ postup
    # generování přes GET /tickets/generate-progress — appka bez něj
    # zůstane u předchozího chování (žádné sledování postupu).
    request_id: Optional[str] = None


class SelectionResponse(BaseModel):
    match_id: int
    home_team: str
    away_team: str
    market_type: MarketType
    selection: str
    probability: float
    odds: float
    model_probability: float
    market_probability: Optional[float]
    edge: Optional[float]
    reasoning: str
    data_quality: str
    league: str = ""
    country: str = ""
    kickoff_date: str = ""
    kickoff_time: str = ""
    home_goals: Optional[int] = None
    away_goals: Optional[int] = None
    result: str = "pending"
    id: Optional[int] = None  # DB id výběru — potřebuje ho frontend pro update

    @classmethod
    def from_domain(cls, c: SelectionCandidate, result: str = "pending") -> "SelectionResponse":
        return cls(
            match_id=c.match_id, home_team=c.home_team, away_team=c.away_team,
            market_type=c.market_type, selection=c.selection,
            probability=round(c.probability, 4), odds=c.odds,
            model_probability=round(c.model_probability, 4),
            market_probability=round(c.market_probability, 4) if c.market_probability is not None else None,
            edge=c.edge, reasoning=c.reasoning, data_quality=c.data_quality,
            league=c.league, country=c.country, kickoff_date=c.kickoff_date,
            kickoff_time=c.kickoff_time,
            result=result,
        )


class TicketResponse(BaseModel):
    ticket_id: Optional[int]
    ticket_type: str
    total_odds: float
    combined_probability: float
    recommended_stake_pct: float
    summary: str
    status: str   # "pending" / "won" / "lost" — appka tohle vyplní jen u uložených tiketů (viz /tickets/saved)
    live_alert: Optional[str] = None   # appka tenhle sloupec dřív plnila ze živých signálů; ty jsou odstraněné, pole zůstává kvůli zpětné kompatibilitě s DB/frontendem a je vždy None
    actual_stake_amount: Optional[float] = None   # co uživatel REÁLNĚ vsadil (viz POST /tickets/{id}/stake)
    actual_odds: Optional[float] = None           # za jaký kurz to reálně vsadil
    actual_profit_loss: Optional[float] = None    # appka to spočítá, jen když má actual_stake_amount A tiket je vyhodnocený
    created_at: Optional[str] = None  # ISO datetime string - kdy byl tiket vytvořen
    horizon_note: Optional[str] = None  # appka to vyplní, jen když musela hledat mimo uživatelem vybraný časový rámec (viz generate_tickets/regenerate_tickets)
    selections: list[SelectionResponse]

    @classmethod
    def from_domain(cls, ticket: Ticket, ticket_id: Optional[int] = None, status: str = "pending",
                     live_alert: Optional[str] = None, actual_stake_amount: Optional[float] = None,
                     actual_odds: Optional[float] = None, actual_profit_loss: Optional[float] = None,
                     created_at: Optional[str] = None, horizon_note: Optional[str] = None) -> "TicketResponse":
        return cls(
            ticket_id=ticket_id,
            ticket_type=ticket.ticket_type,
            total_odds=ticket.total_odds,
            combined_probability=ticket.combined_probability,
            recommended_stake_pct=ticket.recommended_stake_pct,
            summary=ticket.summary,
            status=status,
            live_alert=live_alert,
            actual_stake_amount=actual_stake_amount,
            actual_odds=actual_odds,
            actual_profit_loss=actual_profit_loss,
            created_at=created_at,
            horizon_note=horizon_note,
            selections=[SelectionResponse.from_domain(s) for s in ticket.selections],
        )


class TicketPairResponse(BaseModel):
    safe: Optional[TicketResponse]
    aggressive: Optional[TicketResponse]


# =====================================================================
# Repository — tikety persistované v PostgreSQL (viz db.py).
# =====================================================================
class Repo:
    FLAT_STAKE_PCT = 2.0          # srovnávací vklad "rovných X % na každý tiket bez ohledu na Kelly"
    CALIBRATION_BUCKET_WIDTH_PCT = 10

    def __init__(self):
        self._last_batch_match_ids: dict[int, set[int]] = {}  # user_id -> match_ids ze VŠECH zatím nabídnutých (ne nutně uložených) tiketů od posledního uložení
        db.ensure_schema()

    # --- Tikety: persistované, viz db.py -------------------------------

    def save_ticket(self, user_id: int, ticket: Ticket, created_at=None) -> int:
        return db.insert_ticket(user_id, ticket, created_at=created_at)

    def set_actual_stake(self, ticket_id: int, stake_amount: float, odds: float) -> bool:
        """
        Appka si tady uloží, co uživatel REÁLNĚ vsadil — vlastní kurz
        (může se lišit od kurzu v okamžiku generování, sázka se obvykle
        kliká později) a vlastní částku. Appka nijak nevynucuje, že se
        musí vsadit přesně doporučený Kelly vklad — je to čistě na
        uživateli, appka jen zaznamená, co se reálně stalo.
        """
        return db.update_actual_stake(ticket_id, stake_amount, odds)

    def _compute_actual_profit_loss(self, row: dict) -> Optional[float]:
        if row.get("actual_stake_amount") is None:
            return None
        status = row.get("status", "pending")
        if status == "pending":
            return None
        stake = row["actual_stake_amount"]
        odds = row.get("actual_odds") or row["ticket"].total_odds
        return round(stake * (odds - 1), 2) if status == "won" else round(-stake, 2)

    def get_saved_tickets(self, user_id: int) -> list[dict]:
        rows = db.fetch_ticket_rows(user_id=user_id)
        for row in rows:
            row["actual_profit_loss"] = self._compute_actual_profit_loss(row)
        return rows

    def get_all_saved_match_ids(self, user_id: int) -> list[int]:
        """
        Vrátí match_ids, co appka nemá nabízet znovu — jen z PENDING
        tiketů. Dřív appka brala úplně všechny uložené tikety bez ohledu
        na status, což natrvalo vyřazovalo i zápasy, co se ještě
        neodehrály, jen proto, že appka celý tiket označila "prohraný"
        kvůli JINÉ noze (parlay: jedna prohraná noha = celý tiket
        prohraný, i když appka ostatní zápasy ještě nestihla vyhodnotit
        — viz _try_settle_ticket). Won/lost tikety appka z vyloučení
        vypouští: zápasy z nich už appka nikdy jako budoucí kandidáty
        nenabídne (jsou v minulosti), takže na výsledek to nemá vliv —
        kromě přesně týhle situace, kterou appka opravuje.
        """
        rows = db.fetch_ticket_rows(user_id=user_id, status="pending")
        match_ids = set()
        for row in rows:
            ticket = row.get("ticket")
            if ticket and hasattr(ticket, "selections"):
                for s in ticket.selections:
                    if hasattr(s, "match_id"):
                        match_ids.add(s.match_id)
        return list(match_ids)

    def get_pending_match_ids(self, user_id: int) -> list[int]:
        """Vrátí match_ids z PENDING tiketů + detaily zápasů."""
        rows = db.fetch_ticket_rows(user_id=user_id, status="pending")
        matches_data = []
        for row in rows:
            ticket = row.get("ticket")
            if ticket and hasattr(ticket, "selections"):
                for s in ticket.selections:
                    if hasattr(s, "match_id"):
                        matches_data.append({
                            "match_id": s.match_id,
                            "home_team": getattr(s, "home_team", ""),
                            "away_team": getattr(s, "away_team", ""),
                        })
        return matches_data

    def set_live_alert(self, ticket_id: int, message: Optional[str]) -> None:
        db.update_live_alert(ticket_id, message)

    def get_real_results_report(self, user_id: int) -> dict:
        all_rows = db.fetch_ticket_rows(user_id=user_id)
        staked_rows = [r for r in all_rows if r.get("actual_stake_amount") is not None]
        resolved = [r for r in staked_rows if r.get("status", "pending") in ("won", "lost")]

        total_staked = sum(r["actual_stake_amount"] for r in resolved)
        total_pl = sum(self._compute_actual_profit_loss(r) for r in resolved)
        won_rows = [r for r in resolved if r["status"] == "won"]
        won = len(won_rows)

        # Průměrný kurz vyhraných tiketů
        avg_winning_odds = None
        if won_rows:
            odds_list = [r.get("actual_odds") for r in won_rows if r.get("actual_odds")]
            if odds_list:
                avg_winning_odds = round(sum(odds_list) / len(odds_list), 2)

        # Rozpad podle typu tiketu (kratky/stredni/dlouhy)
        by_type: dict = {}
        for r in resolved:
            t_type = r.get("ticket", Ticket(ticket_type="", selections=[], total_odds=0, combined_probability=0, recommended_stake_pct=0)).ticket_type
            if not t_type:
                t_type = "ostatní"
            if t_type not in by_type:
                by_type[t_type] = {"won": 0, "total": 0, "profit_loss": 0.0}
            by_type[t_type]["total"] += 1
            by_type[t_type]["profit_loss"] = round(by_type[t_type]["profit_loss"] + self._compute_actual_profit_loss(r), 2)
            if r["status"] == "won":
                by_type[t_type]["won"] += 1

        # Přidej win_rate do každého typu
        for t_type in by_type:
            total = by_type[t_type]["total"]
            won_t = by_type[t_type]["won"]
            by_type[t_type]["win_rate"] = round((won_t / total * 100), 1) if total > 0 else 0

        # Časová řada pro graf
        cumulative = 0.0
        history = []
        for r in sorted(resolved, key=lambda row: row["ticket_id"]):
            pl = self._compute_actual_profit_loss(r)
            cumulative += pl
            history.append({"ticket_id": r["ticket_id"], "profit_loss": pl, "cumulative_profit_loss": round(cumulative, 2)})

        return {
            "total_tickets_staked": len(staked_rows),
            "total_resolved": len(resolved),
            "pending": len(staked_rows) - len(resolved),
            "won_count": won,
            "win_rate_pct": round(won / len(resolved) * 100, 1) if resolved else None,
            "total_staked": round(total_staked, 2),
            "total_profit_loss": round(total_pl, 2),
            "roi_pct": round(total_pl / total_staked * 100, 1) if total_staked else None,
            "avg_winning_odds": avg_winning_odds,
            "kratky": by_type.get("kratky"),
            "stredni": by_type.get("stredni"),
            "dlouhy": by_type.get("boost"),  # ticket_type "boost" = UI label "Dlouhý"
            "by_type": by_type,
            "history": history,
        }

    def get_pending_tickets(self) -> list[tuple[int, Ticket]]:
        rows = db.fetch_ticket_rows(status="pending")
        return [(r["ticket_id"], r["ticket"]) for r in rows]

    def set_ticket_status(self, ticket_id: int, status: str) -> None:
        db.update_ticket_status(ticket_id, status)

    def get_ticket_track_record(self, user_id: int) -> dict:
        rows = db.fetch_ticket_rows(user_id=user_id)
        resolved = [r for r in rows if r.get("status", "pending") != "pending"]
        won = sum(1 for r in resolved if r["status"] == "won")
        total = len(resolved)
        return {
            "total_resolved": total,
            "won": won,
            "lost": total - won,
            "win_rate_pct": round(won / total * 100, 1) if total else None,
            "pending": sum(1 for r in rows if r.get("status", "pending") == "pending"),
        }

    def get_calibration_report(self, user_id: int) -> dict:
        """
        Pro vyhodnocené tikety JEDNOHO uživatele porovná, co appka SLIBOVALA
        (combined_probability), s tím, co se SKUTEČNĚ stalo. Rozdělí tikety
        do košů po 10 % a u každého koše spočítá skutečnou úspěšnost —
        pokud appka říká "75 %" a koš s tikety kolem 75% pravděpodobnosti
        skutečně vyhrává ~75 % času, je appka dobře kalibrovaná. Pokud koš
        "90%" vyhrává jen 60 % času, appka systematicky přestřeluje.

        Brier score je jedno číslo shrnující totéž za všechny tikety
        najednou: průměr (predikce - výsledek)² — 0 = perfektní, 0.25 =
        appka neumí o nic víc, než hodit minci, 1.0 = systematicky a
        jistě špatně. Appka potřebuje dost vyhodnocených tiketů (řádově
        desítky), než má tohle vypovídací hodnotu — na pár kusech jde
        jen o šum.
        """
        rows = db.fetch_ticket_rows(user_id=user_id)
        resolved = [row for row in rows if row.get("status", "pending") in ("won", "lost")]
        if not resolved:
            return {"total_resolved": 0, "brier_score": None, "buckets": []}

        brier_sum = 0.0
        bucket_data: dict[int, dict] = {}
        for row in resolved:
            p = row["ticket"].combined_probability
            outcome = 1.0 if row["status"] == "won" else 0.0
            brier_sum += (p - outcome) ** 2

            bucket_idx = min(int(p * 100) // self.CALIBRATION_BUCKET_WIDTH_PCT, 9)
            bucket = bucket_data.setdefault(bucket_idx, {"predicted_sum": 0.0, "wins": 0.0, "count": 0})
            bucket["predicted_sum"] += p
            bucket["wins"] += outcome
            bucket["count"] += 1

        buckets = []
        for idx in sorted(bucket_data):
            b = bucket_data[idx]
            low, high = idx * self.CALIBRATION_BUCKET_WIDTH_PCT, (idx + 1) * self.CALIBRATION_BUCKET_WIDTH_PCT
            buckets.append({
                "range": f"{low}-{high}%",
                "predicted_avg_pct": round(b["predicted_sum"] / b["count"] * 100, 1),
                "actual_win_rate_pct": round(b["wins"] / b["count"] * 100, 1),
                "count": b["count"],
            })

        return {
            "total_resolved": len(resolved),
            "brier_score": round(brier_sum / len(resolved), 4),
            "buckets": buckets,
        }

    def get_roi_report(self, user_id: int) -> dict:
        """
        Simulovaný výdělek/ztráta JEDNOHO uživatele, kdyby sázel přesně
        podle doporučeného vkladu appky (Kelly) na každý vyhodnocený tiket —
        a pro srovnání to samé, kdyby vsadil rovných FLAT_STAKE_PCT % na
        každý tiket bez ohledu na Kelly doporučení. Appka tím odpoví na
        otázku "vyplatí se ta složitost s Kelly škálováním vkladu, nebo
        by sázet pořád stejně dopadlo stejně dobře/špatně?"

        POZOR: appka počítá nesložené úročení (jednotky = % PŮVODNÍHO
        bankrollu, ne aktuálního) — při víc souběžně otevřených tiketech
        by skutečné složené úročení vyžadovalo přesné pořadí vyrovnání,
        což appka v tuhle chvíli neřeší. Pro orientační srovnání obou
        přístupů to ale stačí.
        """
        rows = db.fetch_ticket_rows(user_id=user_id)
        resolved = [row for row in rows if row.get("status", "pending") in ("won", "lost")]
        if not resolved:
            return {"total_resolved": 0, "kelly_roi_pct": None, "flat_stake_roi_pct": None, "by_market_type": {}}

        kelly_cumulative, flat_cumulative = 0.0, 0.0
        by_market: dict[str, dict] = {}

        for row in resolved:
            ticket = row["ticket"]
            won = row["status"] == "won"
            if won:
                kelly_cumulative += ticket.recommended_stake_pct * (ticket.total_odds - 1)
                flat_cumulative += self.FLAT_STAKE_PCT * (ticket.total_odds - 1)
            else:
                kelly_cumulative -= ticket.recommended_stake_pct
                flat_cumulative -= self.FLAT_STAKE_PCT

            # Rozpad podle typu trhu — appka bere market_type první nohy
            # tiketu jako orientační štítek (kombo tikety bývají smíšené,
            # tohle slouží jen k hrubému přehledu "kde appka funguje líp").
            label = ticket.selections[0].market_type.value if ticket.selections else "neznámý"
            entry = by_market.setdefault(label, {"count": 0, "won": 0})
            entry["count"] += 1
            entry["won"] += 1 if won else 0

        return {
            "total_resolved": len(resolved),
            "kelly_roi_pct": round(kelly_cumulative, 2),
            "flat_stake_roi_pct": round(flat_cumulative, 2),
            "by_market_type": {
                label: {"count": v["count"], "win_rate_pct": round(v["won"] / v["count"] * 100, 1)}
                for label, v in by_market.items()
            },
            "note": (
                "Nesložené úročení (jednotky = % původního bankrollu), bez ohledu na "
                "časové pořadí tiketů. Kladné číslo = appka by tě v souhrnu posunula "
                "do zisku, záporné = do ztráty."
            ),
        }

    def set_last_batch(self, user_id: int, match_ids: list[int]) -> None:
        """Appka sem PŘIDÁVÁ (ne přepisuje) — jinak by druhé generování
        (jiný risk_level, tedy jiný typ tiketu) v hned další appka nevědělo
        o zápasech z toho prvního, dokud appka první tiket neuloží. Bez
        tohohle appka klidně nabídla stejný zápas jako 'krátký' i 'střední'
        tiket zároveň, protože obě generování běžela nezávisle na sobě.
        Appka nemaže staré položky — zápas, co appka jednou nabídla, se v
        rámci stejné (neuložené) session nemá vrátit ani za pár volání."""
        existing = self._last_batch_match_ids.get(user_id, set())
        self._last_batch_match_ids[user_id] = existing | set(match_ids)

    def get_last_batch(self, user_id: int) -> list[int]:
        return list(self._last_batch_match_ids.get(user_id, set()))

    def clear_last_batch(self, user_id: int) -> None:
        """Appka tohle měla volat od začátku při každém uložení tiketu (viz
        komentář u set_last_batch — 'od posledního uložení'), ale appka na
        to nikdy neměla kód, jen popis v komentáři. V praxi appka set_last_batch
        jen SČÍTALA a nikdy nečistila — u účtu s opakovaným generováním bez
        ukládání (typicky appčino vlastní testování) appka tak postupně
        vyloučila čím dál víc zápasů, až appce nakonec nezbylo dost kandidátů
        na sestavení náročnějších tiketů (stredni, 5 nohou v úzkém rozsahu
        kurzu) — appka pak vracela 'tiket se nepovedl', i když syrový trh měl
        kandidátů dost. Appka teď po každém uložení list vyčistí — uživatel
        se rozhodl, co chce, appka tak nemá důvod dál blokovat nevybrané
        zápasy z předchozích (neuložených) pokusů."""
        self._last_batch_match_ids.pop(user_id, None)


repo = Repo()
ticket_generator = TicketGenerator()


# =====================================================================
# Tokenový systém — viz ApexSignal Tokenomika & Tokenový Model. Stripe
# napojení přijde v dalším kroku; tahle appka zatím jen řídí zůstatek a
# uplatňování kódů (viz db.py: user_tokens/token_transactions/redeem_codes).
# =====================================================================
TOKEN_KC_VALUE = 20  # 1 token = 20 Kč — appka to appce i frontendu drží na jednom místě
TOKEN_COSTS = {"kratky": 10, "stredni": 15}  # 200 Kč / 300 Kč při TOKEN_KC_VALUE=20 — appka BOOST přestala nabízet úplně

# Každý nový účet dostane přesně tolik tokenů, kolik appka strhne za JEDEN
# krátký tiket (viz TOKEN_COSTS["kratky"]) — appka tak novému uživateli
# nechá jedno generování vyzkoušet zdarma, než se rozhodne platit
# (tokeny/kanál/Founder). Fixní číslo místo TOKEN_COSTS["kratky"] přímo by
# appce mohlo tiše rozjet zkušební dárek, kdyby se cena krátkého tiketu
# někdy změnila — appka to chce takhle svázané schválně.
FREE_TRIAL_TOKENS = TOKEN_COSTS["kratky"]
TOKEN_PACKAGES = [12, 24, 60]  # předvolby k nákupu (v tokenech) — nejmenší pokryje aspoň 2 krátké tikety
MIN_CUSTOM_TOKENS = 1

# Doporučovací systém — spouštěč je POUŽITÍ tokenů na první střední tiket,
# ne proběhlá platba za tokeny (appka tím odměňuje reálné zapojení).
# Pojmenované konstanty, ať appka jde ladit bez zásahu do logiky.
REFERRAL_REFERRER_TOKENS = 25
REFERRAL_REFERRED_BONUS_TOKENS = 10
REFERRAL_TRIGGER_TICKET_TYPE = "stredni"
REFERRAL_MAX_REWARDS_PER_MONTH = 10
MAX_CUSTOM_TOKENS = 5000  # pojistka proti překlepu/zneužití při vlastní částce

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

# =====================================================================
# Předplatné placeného Telegram kanálu — 1 krátký + 1 střední tiket
# denně. Appka to prodává přes samostatný Stripe Payment Link
# (STRIPE_CHANNEL_PAYMENT_LINK_URL, viz transparency_page) — appka sama
# žádnou Checkout Session pro předplatné nevytváří, jen zpracovává
# webhook událost, když někdo tím odkazem zaplatí. CHANNEL_PRICE_KC je
# jen popisná cena pro zobrazení (např. v /tokens/prices) — skutečnou
# částku, co appka strhne, drží ten Payment Link na Stripe straně.
# =====================================================================
TELEGRAM_LINK_CODE_TTL_MINUTES = 60
CHANNEL_PRICE_KC = 490


def _ticket_type_for_risk_level(risk_level: int) -> str:
    """Appka řídí typ tiketu jen podle risk_level, stejně jako
    TicketGenerator.generate — appka musí znát typ (a tedy cenu v
    tokenech) JEŠTĚ PŘED samotným generováním, ať zbytečně neplýtvá API
    kvótou na tiket, který si uživatel stejně nemůže dovolit odemknout."""
    if risk_level <= 30:
        return "kratky"
    elif risk_level <= 60:
        return "stredni"
    return "boost"


def _pool_filter_for_risk(risk_level: int):
    """
    AI kontrola čerstvých zpráv (viz ai_reviewer.review_candidates) je
    zdaleka nejpomalejší krok generování — appka na ni čeká, protože
    prochází web pro KAŽDÉHO kandidáta. U krátkého a středního tiketu
    (nižší kurz, méně riskantní) appka tenhle krok přeskočí a spolehne
    se jen na statistický model — u BOOSTu (dlouhá kombinace, appka na
    ni neuplatňuje kontrolu kladného edge) je to naopak jediná pojistka
    proti zastaralým datům, tam kontrola zůstává.
    """
    if risk_level > 60:
        return ai_reviewer.review_candidates
    return None


def _check_token_balance(user_id: int, risk_level: int) -> None:
    # Neomezený tarif appku vůbec neúčtuje v tokenech — appka místo toho
    # počítá POKUSY o generování proti dennímu stropu (viz
    # db.increment_daily_generation_count). Appka strop kontroluje TADY,
    # před samotným (drahým) generováním, ne až po něm.
    if _has_active_unlimited(user_id):
        count = db.increment_daily_generation_count(user_id)
        # Zkušební kódy appka umí omezit na nižší strop, než má placený
        # tarif (viz db.redeem_code/daily_generation_cap_override) — appka
        # tenhle override respektuje, jinak spadne na standardní konstantu.
        cap = db.get_daily_generation_cap_override(user_id) or UNLIMITED_GENERATION_DAILY_CAP
        if count > cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Appka ti dnes už {cap}× vygenerovala — "
                    "to je denní strop neomezeného tarifu. Zkus to zítra."
                ),
            )
        return
    ticket_type = _ticket_type_for_risk_level(risk_level)
    cost = TOKEN_COSTS.get(ticket_type, 0)
    if cost <= 0:
        return
    balance = db.get_token_balance(user_id)
    if balance < cost:
        raise HTTPException(
            status_code=402,
            detail=f"Nedostatek tokenů — tenhle tiket stojí {cost}, máš {balance}. Uplatni kód nebo dokup tokeny.",
        )


def _process_referral_reward(referred_user_id: int) -> None:
    """
    Appka tohle volá hned po odečtení tokenů za PRVNÍ střední tiket
    doporučeného účtu. Běží jako best-effort — chyba tady nesmí shodit
    samotné generování tiketu, appka jen zaloguje a jde dál.

    Pojistky (musí projít VŠECHNY):
      1) účet vůbec někoho doporučil (referred_by_user_id),
      2) appka ho ještě neodměnila (referral_rewards.referred_user_id UNIQUE),
      3) tohle je jeho úplně první UNLOCK_STREDNI (ne třetí, ne desátý),
      4) doporučitel nemá tento měsíc už vyčerpaný strop odměn,
      5) doporučený a doporučitel neplatí stejnou kartou (otisk karty).
    """
    try:
        referrer_id = db.get_referred_by(referred_user_id)
        if not referrer_id:
            return
        if db.has_referral_reward(referred_user_id):
            return
        if db.count_token_transactions_with_reason(referred_user_id, "UNLOCK_STREDNI") != 1:
            return
        since = datetime.now(timezone.utc) - timedelta(days=30)
        if db.count_referral_rewards_for_referrer_since(referrer_id, since) >= REFERRAL_MAX_REWARDS_PER_MONTH:
            print(f"[referral] Doporučitel {referrer_id} dosáhl měsíčního stropu odměn — nepřipisuji")
            return
        referred_fingerprints = db.get_card_fingerprints(referred_user_id)
        referrer_fingerprints = db.get_card_fingerprints(referrer_id)
        shared = referred_fingerprints & referrer_fingerprints
        if shared:
            print(f"[referral] Doporučený {referred_user_id} sdílí kartu s doporučitelem {referrer_id} — odměna zamítnuta")
            return

        db.create_referral_reward(
            referred_user_id, referrer_id, REFERRAL_REFERRED_BONUS_TOKENS, REFERRAL_REFERRER_TOKENS,
            next(iter(referred_fingerprints), None),
        )
        db.adjust_tokens(referred_user_id, REFERRAL_REFERRED_BONUS_TOKENS, "REFERRAL_BONUS_REFERRED")
        db.adjust_tokens(referrer_id, REFERRAL_REFERRER_TOKENS, "REFERRAL_BONUS_REFERRER")
    except Exception as e:
        print(f"[referral] Zpracování odměny selhalo (referred_user_id={referred_user_id}): {e}")


def _charge_tokens_for_ticket(user_id: int, ticket_type: str) -> None:
    # Neomezený tarif appka nestrhává v tokenech — appka si tenhle pokus
    # už započítala do denního stropu v _check_token_balance.
    if _has_active_unlimited(user_id):
        return
    cost = TOKEN_COSTS.get(ticket_type, 0)
    if cost > 0:
        db.adjust_tokens(user_id, -cost, f"UNLOCK_{ticket_type.upper()}")
        if ticket_type == REFERRAL_TRIGGER_TICKET_TYPE:
            _process_referral_reward(user_id)


class RedeemCodeRequest(BaseModel):
    code: str


@app.get("/tokens/balance")
def get_token_balance_endpoint(user_id: int = Depends(get_current_user_id)):
    until = db.get_unlimited_until(user_id)

    # Kanál (490 Kč) appka drží úplně odděleně, podle e-mailu ve
    # subscriptions — appka tu jen zkontroluje, jestli e-mail appky
    # účtu náhodou nesedí na nějaké aktivní kanálové předplatné, ať appka
    # ví, jestli má appka ukázat "koupit" nebo "spravovat/zrušit".
    user = db.get_user_by_id(user_id)
    channel_active = False
    if user and user.get("email"):
        sub = db.get_subscription_by_email(user["email"])
        channel_active = bool(sub and db.has_active_subscription_id(sub["id"]))

    return {
        "balance": db.get_token_balance(user_id),
        "unlimited_active": _has_active_unlimited(user_id),
        "unlimited_until": until.isoformat() if until else None,
        "channel_active": channel_active,
    }


@app.get("/tokens/prices")
def get_token_prices():
    """Appka odsud bere ceny tiketů v tokenech i hodnotu tokenu v Kč —
    žádné přihlášení netřeba, appka to zobrazuje i nepřihlášeným (viz
    onboarding). Jedno místo pravdy pro frontend, ať appka časem
    nezapomene přepočítat obě strany zvlášť."""
    return {
        "token_value_kc": TOKEN_KC_VALUE,
        "costs": TOKEN_COSTS,
        "costs_kc": {k: v * TOKEN_KC_VALUE for k, v in TOKEN_COSTS.items()},
        "packages": [{"tokens": t, "price_kc": t * TOKEN_KC_VALUE} for t in TOKEN_PACKAGES],
        "min_custom_tokens": MIN_CUSTOM_TOKENS,
        "max_custom_tokens": MAX_CUSTOM_TOKENS,
        "channel_price_kc": CHANNEL_PRICE_KC,
        "channel_payment_link_url": os.environ.get("STRIPE_CHANNEL_PAYMENT_LINK_URL", "").strip(),
        "unlimited_plans_kc": UNLIMITED_GENERATION_PLANS,
    }


class CreateCheckoutSessionRequest(BaseModel):
    tokens: int


@app.post("/payments/create-checkout-session")
def create_checkout_session(req: CreateCheckoutSessionRequest, user_id: int = Depends(get_current_user_id)):
    # Dokud appka nemá zaplacené API kredity na reálný provoz (viz
    # _require_generation_enabled), nemá smysl pouštět ani nákup tokenů
    # — zákazník by zaplatil za tokeny, které zatím nemá na co utratit.
    _require_generation_enabled(user_id)
    if req.tokens < MIN_CUSTOM_TOKENS or req.tokens > MAX_CUSTOM_TOKENS:
        raise HTTPException(status_code=400, detail=f"Počet tokenů musí být mezi {MIN_CUSTOM_TOKENS} a {MAX_CUSTOM_TOKENS}")
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    price_kc = req.tokens * TOKEN_KC_VALUE
    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "czk",
                    "product_data": {"name": f"{req.tokens} tokenů — ApexSignal"},
                    "unit_amount": price_kc * 100,  # Stripe počítá v haléřích
                },
                "quantity": 1,
            }],
            metadata={"user_id": str(user_id), "tokens": str(req.tokens)},
            success_url=f"{frontend_url}/?payment=success",
            cancel_url=f"{frontend_url}/?payment=cancelled",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"checkout_url": session.url}


UNLIMITED_GENERATION_DAILY_CAP = 10

# "Founder" tarif — stejné neomezené generování, ale s cenovou škálou podle
# délky předplatného (delší závazek = nižší cena/měsíc). 1 měsíc je i
# nadále dostupný jako běžná self-serve cena, 3/6/12 měsíců appka nabízí
# hlavně lidem, co appku uzavřou po prodejním hovoru (viz zadání founder
# tier). Klíč = počet měsíců v jednom fakturačním cyklu, hodnota = celková
# cena v Kč za CELÉ období (ne za měsíc).
UNLIMITED_GENERATION_PLANS: dict[int, int] = {
    1: 9900,
    3: 26700,
    6: 47400,
    12: 82800,
}


class UnlimitedCheckoutRequest(BaseModel):
    months: int = 1

    @field_validator("months")
    @classmethod
    def validate_months(cls, v: int) -> int:
        if v not in UNLIMITED_GENERATION_PLANS:
            raise ValueError(f"Neplatná délka předplatného, appka umí jen: {sorted(UNLIMITED_GENERATION_PLANS)}")
        return v


@app.post("/payments/create-unlimited-checkout-session")
def create_unlimited_checkout_session(req: UnlimitedCheckoutRequest, user_id: int = Depends(get_current_user_id)):
    """Na rozdíl od nákupu tokenů tady appka záměrně nevolá
    _require_generation_enabled — tenhle nákup je přesně to, co má
    generování uživateli odemknout, takže by ho nemělo dávat smysl
    podmiňovat tím, že generování je už odemčené.

    mode="subscription" (ne jednorázová platba) — Stripe strhává platbu
    sám podle `interval_count` (1/3/6/12 měsíců). Datum konce appka
    nenastavuje napevno na +N dní, ale prodlužuje ho webhook
    (checkout.session.completed / invoice.payment_succeeded) podle
    skutečně zaplaceného období (`current_period_end`) — díky tomu appka
    nemusí nijak zvlášť ošetřovat delší cykly, webhook funguje beze změny."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    months = req.months
    total_price_kc = UNLIMITED_GENERATION_PLANS[months]
    product_name = (
        "Neomezené generování na měsíc — ApexSignal" if months == 1
        else f"Founder — neomezené generování na {months} měsíců — ApexSignal"
    )

    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
    metadata = {"user_id": str(user_id), "unlimited_generation": "1", "months": str(months)}
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "czk",
                    "product_data": {"name": product_name},
                    "unit_amount": total_price_kc * 100,
                    "recurring": {"interval": "month", "interval_count": months},
                },
                "quantity": 1,
            }],
            metadata=metadata,
            # Metadata appka musí zopakovat i sem — appka jinak zůstane jen
            # na Checkout Session, ale pozdější webhooky (obnovení,
            # zrušení) appce posílají přímo objekt Subscription, ne Session.
            subscription_data={"metadata": metadata},
            success_url=f"{frontend_url}/?payment=success",
            cancel_url=f"{frontend_url}/?payment=cancelled",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"checkout_url": session.url}


@app.post("/payments/unlimited-billing-portal")
def unlimited_billing_portal(user_id: int = Depends(get_current_user_id)):
    """Billing portál appka pro neomezený tarif drží zvlášť od
    /payments/billing-portal — appka ten druhý dohledává zákazníka podle
    e-mailu (Telegram kanál, žádný účet appky), kdežto tenhle je vázaný
    na přihlášený appky účet."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    customer_id = db.get_unlimited_stripe_customer_id(user_id)
    if not customer_id:
        # Appka neomezené generování umí aktivovat i mimo Stripe (redeem
        # kód, /admin/set-unlimited) — tam appka žádného Stripe zákazníka
        # nemá, takže portál nejde otevřít. Appka to musí odlišit od
        # "nemáš to vůbec", jinak appka tvrdí opak toho, co appka sama
        # ukazuje o řádek výš (unlimited_active).
        if _has_active_unlimited(user_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Neomezené generování máš aktivní, ale ne přes placené Stripe "
                    "předplatné (aktivováno ručně/kódem) — appka pro něj nemá co "
                    "otevřít ve správě plateb. Platnost mu prostě doběhne sama, "
                    "nic rušit nemusíš."
                ),
            )
        raise HTTPException(status_code=404, detail="Nemáš aktivní neomezené předplatné")

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=os.environ.get("APP_URL", "https://apexsignal.cz"),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"portal_url": session.url}


def _period_end_from_subscription(sub: dict) -> Optional[datetime]:
    """
    Konec zaplaceného období. Stripe ho v novějších verzích API přesunul
    ze samotného předplatného na jeho položky, takže se appka dívá na
    obě místa — jinak by jí po změně verze API tiše zmizel datum a
    předplatné by pak platilo donekonečna.
    """
    ts = sub.get("current_period_end")
    if not ts:
        items = (sub.get("items") or {}).get("data") or []
        for item in items:
            if item.get("current_period_end"):
                ts = item["current_period_end"]
                break
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def _sync_subscription_from_stripe(sub: dict, email: Optional[str] = None) -> Optional[int]:
    """
    Zrcadlí jeden objekt předplatného ze Stripu do naší DB a vrátí
    subscription_id. Appka tu nemá žádné vlastní user_id k dispozici —
    zákazník appce nezakládá účet, appka ho pozná jen podle e-mailu.
    Řada webhook událostí ale e-mail vůbec nenese, proto appka bez něj
    dotáhne e-mail ze Stripe Customer objektu.
    """
    if email is None and sub.get("customer"):
        try:
            customer = stripe.Customer.retrieve(sub["customer"])
            email = customer.get("email")
        except Exception as e:
            print(f"[stripe] nepodařilo se dotáhnout e-mail zákazníka {sub.get('customer')}: {e}")
    if email is None:
        # Bez e-mailu nemá appka koho o výsledku informovat — aspoň to
        # zaloguje, ať se to dá ručně dohledat, místo aby událost tiše
        # zahodila.
        print(f"[stripe] předplatné {sub.get('id')} nemá e-mail, přeskakuji")
        return None

    return db.upsert_subscription_by_stripe_sub(
        stripe_subscription_id=sub["id"],
        email=email,
        status=sub.get("status", "inactive"),
        stripe_customer_id=sub.get("customer"),
        current_period_end=_period_end_from_subscription(sub),
    )


def _send_telegram_onboarding_email(subscription_id: int, email: str) -> bool:
    """
    Vygeneruje párovací kód a pošle ho e-mailem rovnou jako hotový
    odkaz do Telegramu — appka tu nemá žádné přihlášené sezení, kterému
    by mohla kód jen vrátit v odpovědi na požadavek, jako to dělá appka
    pro appku vázanou na účet. Vrací True jen když se appce e-mail
    OPRAVDU podařilo odeslat — appka tohle musí umět rozlišit, jinak
    volající (viz /telegram/resend-link) tvrdí zákazníkovi, že mu něco
    přijde, přestože appka nic neposlala.
    """
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
    if not bot_username:
        print("[stripe] TELEGRAM_BOT_USERNAME není nastavený, nemůžu poslat párovací odkaz")
        return False
    code = secrets.token_urlsafe(9)[:12]
    db.create_telegram_link_code(code, subscription_id, ttl_minutes=TELEGRAM_LINK_CODE_TTL_MINUTES)
    deep_link = f"https://t.me/{bot_username}?start={code}"
    try:
        return email_service.send_channel_welcome_email(email, deep_link)
    except Exception as e:
        print(f"[stripe] nepodařilo se poslat uvítací e-mail na {email}: {e}")
        return False


class BillingPortalRequest(BaseModel):
    email: str


@app.post("/payments/billing-portal")
def billing_portal(req: BillingPortalRequest):
    """
    Odkaz do Stripe portálu, kde si zákazník sám zruší předplatné nebo
    změní kartu — appka ho najde podle e-mailu, kterým platil (appka to
    dál nijak neověřuje, portál sám ukáže jen to, co k danému Stripe
    zákazníkovi patří). U ceny 2 500 Kč měsíčně je zrušení na jedno
    kliknutí povinnost, ne laskavost — nutit lidi psát na podporu je
    nejrychlejší cesta ke stížnostem a chargebackům.
    """
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    sub = db.get_subscription_by_email(req.email)
    if not sub or not sub.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="K tomuhle e-mailu appka nemá žádné předplatné")

    try:
        session = stripe.billing_portal.Session.create(
            customer=sub["stripe_customer_id"],
            return_url=os.environ.get("APP_URL", "https://apexsignal.cz"),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"portal_url": session.url}


@app.post("/telegram/resend-link")
def resend_telegram_link(req: BillingPortalRequest):
    """
    Znovu pošle párovací odkaz na e-mail, kterým appka zákazníka zná —
    appka mu ho poprvé posílá automaticky hned po zaplacení, tohle je
    záchranná síť pro případ, že ten e-mail appka nedoručila nebo se
    ztratil (appka bez toho nemá jak zákazníkovi jinak předat kód, ten
    nemá u appky žádný účet ani přihlášení).
    """
    sub = db.get_subscription_by_email(req.email)
    if not sub or not db.has_active_subscription_id(sub["id"]):
        raise HTTPException(status_code=404, detail="K tomuhle e-mailu appka nemá aktivní předplatné")
    if not _send_telegram_onboarding_email(sub["id"], sub["email"]):
        raise HTTPException(status_code=502, detail="Nepodařilo se odeslat e-mail — zkus to prosím za chvíli znovu")
    return {"status": "sent"}


@app.post("/payments/webhook")
async def stripe_webhook(request: Request):
    """
    Appka tokeny připisuje i předplatné aktivuje TADY (server-side, po
    ověřeném webhooku), ne hned po přesměrování na success_url — ten
    frontend uživatel může zavřít/obejít, kdežto webhook appka dostane
    přímo od Stripe a jde mu věřit jen po ověření podpisu
    (STRIPE_WEBHOOK_SECRET).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise HTTPException(status_code=500, detail="Webhook není nastavený")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Neplatný webhook: {e}")

    event_type = event["type"]
    obj = event["data"]["object"]

    # Ochrana proti dvojímu zpracování je až tady, po rozhodnutí, že je
    # událost pro appku zajímavá — jinak by první doručení neznámého typu
    # "spotřebovalo" ID a případné opakování už appka neviděla.
    relevant = (
        event_type == "checkout.session.completed"
        or event_type.startswith("customer.subscription.")
        or event_type in ("invoice.payment_succeeded", "invoice.payment_failed")
    )
    if not relevant or not db.mark_stripe_event_if_new(event["id"]):
        return {"status": "ok"}

    # Appka teď má DVĚ různé předplatné, obě mode="subscription":
    #   1) Telegram kanál (490 Kč) — přes samostatný Stripe Payment Link,
    #      žádný účet appky, appka zákazníka pozná jen podle e-mailu,
    #      appka drží stav v tabulce subscriptions.
    #   2) Neomezené generování (9900 Kč) — přes /payments/create-
    #      unlimited-checkout-session, vázané na přihlášený user_id
    #      appky (metadata.unlimited_generation == "1"), appka drží
    #      stav přímo na users.unlimited_until.
    # Appka je rozlišuje podle metadata.unlimited_generation, ne podle
    # mode — obě appka teď mají mode="subscription".
    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        user_id = int(metadata.get("user_id", 0)) or None

        if user_id and metadata.get("unlimited_generation") == "1":
            stripe_subscription_id = obj.get("subscription")
            period_end = None
            if stripe_subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    period_end = _period_end_from_subscription(sub)
                except Exception as e:
                    print(f"[stripe] nepodařilo se načíst nové neomezené předplatné {stripe_subscription_id}: {e}")
            db.set_unlimited_until(
                user_id,
                period_end or datetime.now(timezone.utc) + timedelta(days=30),
                stripe_customer_id=obj.get("customer"),
            )
        elif obj.get("mode") == "subscription":
            # Platba přes samostatný Stripe Payment Link (kanál) — appka
            # tu nemá žádné metadata.user_id (nikdo se nepřihlašoval), jen
            # e-mail, který zákazník zadal do Stripe checkoutu.
            email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
            stripe_subscription_id = obj.get("subscription")
            if not (email and stripe_subscription_id):
                print(f"[stripe] checkout.session.completed bez e-mailu/subscription ID: {obj.get('id')}")
            else:
                # Samotná session nenese stav předplatného, appka si ho
                # proto dotáhne ze Stripu — ať v DB skončí i datum konce
                # období.
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    subscription_row_id = _sync_subscription_from_stripe(sub, email=email)
                except Exception as e:
                    print(f"[stripe] nepodařilo se načíst předplatné {stripe_subscription_id}: {e}")
                    subscription_row_id = db.upsert_subscription_by_stripe_sub(
                        stripe_subscription_id=stripe_subscription_id, email=email, status="active",
                        stripe_customer_id=obj.get("customer"),
                    )
                if subscription_row_id:
                    _send_telegram_onboarding_email(subscription_row_id, email)
        else:
            # Jednorázový nákup vázaný na účet — tokeny.
            tokens = int(metadata.get("tokens", 0))
            if user_id and tokens:
                db.adjust_tokens(user_id, tokens, f"STRIPE_PAYMENT:{obj['id']}")
                # Appka si otisk platební karty (ne číslo) uloží kvůli
                # doporučovacímu systému — appka tak pozná sdílenou kartu
                # mezi doporučeným a doporučitelem, i na různé e-maily.
                # Best-effort: chyba tady nesmí shodit připsání tokenů výš.
                try:
                    pi_id = obj.get("payment_intent")
                    if pi_id:
                        pi = stripe.PaymentIntent.retrieve(pi_id, expand=["payment_method"])
                        pm = pi.get("payment_method")
                        fingerprint = (pm.get("card") or {}).get("fingerprint") if isinstance(pm, dict) else None
                        db.record_card_fingerprint(user_id, fingerprint)
                except Exception as e:
                    print(f"[stripe] Nepodařilo se zaznamenat otisk karty (doporučovací pojistka): {e}")

    elif event_type.startswith("customer.subscription."):
        sub_metadata = obj.get("metadata") or {}
        if sub_metadata.get("unlimited_generation") == "1":
            # Obnovení appka řeší přes invoice.payment_succeeded níž (tam
            # appka ví jistě, že platba prošla) — tady appka jen zrcadlí
            # aktivní stav, kdyby přišel dřív. Zrušení appka neřeší
            # zkrácením — přístup nechá doběhnout do konce už zaplaceného
            # období, neutne ho hned.
            sub_user_id = int(sub_metadata.get("user_id", 0)) or None
            if sub_user_id and obj.get("status") in ("active", "trialing"):
                period_end = _period_end_from_subscription(obj)
                if period_end:
                    db.set_unlimited_until(sub_user_id, period_end, stripe_customer_id=obj.get("customer"))
        else:
            # created / updated / deleted (kanál) — u deleted pošle Stripe
            # status 'canceled', appka nepotřebuje větvit, stačí zrcadlit.
            if not db.update_subscription_by_stripe_id(
                obj["id"], obj.get("status", "inactive"), _period_end_from_subscription(obj)
            ):
                _sync_subscription_from_stripe(obj)

    elif event_type in ("invoice.payment_succeeded", "invoice.payment_failed"):
        subscription_id = obj.get("subscription")
        if subscription_id:
            try:
                sub = stripe.Subscription.retrieve(subscription_id)
            except Exception as e:
                print(f"[stripe] nepodařilo se načíst předplatné {subscription_id}: {e}")
                sub = None
            if sub is not None:
                sub_metadata = sub.get("metadata") or {}
                if sub_metadata.get("unlimited_generation") == "1":
                    # Tohle je ten skutečný měsíční obnovovací moment —
                    # každá úspěšná faktura prodlouží unlimited_until na
                    # nové zaplacené období.
                    if event_type == "invoice.payment_succeeded":
                        inv_user_id = int(sub_metadata.get("user_id", 0)) or None
                        period_end = _period_end_from_subscription(sub)
                        if inv_user_id and period_end:
                            db.set_unlimited_until(inv_user_id, period_end, stripe_customer_id=sub.get("customer"))
                    # payment_failed appka nijak aktivně neřeší — přístup
                    # doběhne do konce už zaplaceného období, opakované
                    # pokusy o strhnutí řeší Stripe sám podle svého plánu.
                else:
                    # Obnovení nebo neúspěšné strhnutí (kanál) — appka si
                    # dotáhne aktuální stav předplatného, ať 'past_due'
                    # nebo prodloužené období dorazí do DB i bez
                    # samostatné customer.subscription.updated.
                    _sync_subscription_from_stripe(sub)

    return {"status": "ok"}


@app.get("/admin/user-payments")
def admin_user_payments(email: str, request: Request):
    """Appka tohle appce admin ukáže historii Stripe nákupů konkrétního
    uživatele (podle e-mailu) — appka to appce potřebuje, aby věděla,
    KTERÝ session_id refundovat přes /admin/refund."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")
    return {"user_id": user["id"], "payments": db.get_stripe_payments_for_user(user["id"])}


@app.get("/admin/conversion-funnel")
def admin_conversion_funnel(request: Request, days: int = 30):
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    return db.get_conversion_funnel(days)


@app.get("/admin/recent-registrations")
def admin_recent_registrations(request: Request, days: int = 1):
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    rows = db.get_recent_registrations(days)
    return {"period_days": days, "registrations": [
        {"email": r["email"], "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows
    ]}


# Appka appce vezme jen tyhle typy událostí — cokoli jiného appka odmítne,
# ať appce do tabulky nikdo nenacpe libovolná data přes veřejný endpoint.
ALLOWED_EVENT_TYPES = {"session_start", "heartbeat", "click_generate", "generate_success", "generate_failed", "ticket_saved"}


class TrackEventRequest(BaseModel):
    event_type: str
    session_id: Optional[str] = None
    metadata: Optional[dict] = None


@app.post("/events/track")
def track_event(req: TrackEventRequest, user_id: int = Depends(get_current_user_id)):
    if req.event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Neznámý typ události: {req.event_type}")
    db.log_user_event(user_id, req.event_type, req.session_id, req.metadata)
    return {"status": "ok"}


@app.get("/admin/user-activity")
def admin_user_activity(request: Request, days: int = 1):
    """
    Appka appce ukáže, co registrovaní uživatelé za posledních `days` dní
    reálně dělali — kliky na Vygenerovat, úspěšná/neúspěšná generování,
    kolik tiketů uložili a odhad minut strávených na webu (ze session
    heartbeatů, viz get_user_activity_summary).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    rows = db.get_user_activity_summary(days)
    return {"period_days": days, "users": [
        {
            "email": r["email"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "clicked_generate": r["clicked_generate"],
            "generated": r["generated"],
            "generate_failed": r["generate_failed"],
            "saved": r["saved"],
            "minutes_on_site": round((r["seconds_on_site"] or 0) / 60, 1),
        }
        for r in rows
    ]}


class RefundRequest(BaseModel):
    email: str
    session_id: str
    deduct_tokens: bool = True


@app.post("/admin/refund")
def admin_refund(req: RefundRequest, request: Request):
    """
    Vrátí peníze za konkrétní Stripe nákup zpět na kartu/účet zákazníka
    a (pokud deduct_tokens) mu odečte tokeny z toho nákupu — appka
    vklad appka nekontroluje na dostatečný zůstatek (uživatel je mohl
    mezitím spotřebovat), zůstatek klidně appka nechá jít do mínusu, ať
    refundace neselže jen kvůli tomu, že appka tokeny mezitím "utratila".
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    user = db.get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Uživatel nenalezen")

    refund_reason = f"REFUND:{req.session_id}"
    if db.has_transaction_with_reason(user["id"], refund_reason):
        raise HTTPException(status_code=400, detail="Tahle platba už byla refundována")

    try:
        session = stripe.checkout.Session.retrieve(req.session_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Stripe session nenalezena: {e}")

    if not session.get("payment_intent"):
        raise HTTPException(status_code=400, detail="K téhle platbě appka nenašla payment_intent (nebyla dokončena?)")

    try:
        refund = stripe.Refund.create(payment_intent=session["payment_intent"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe refundace selhala: {e}")

    new_balance = None
    if req.deduct_tokens:
        tokens = int((session.get("metadata") or {}).get("tokens", 0))
        if tokens:
            new_balance = db.adjust_tokens(user["id"], -tokens, refund_reason)

    return {"status": "Refundováno", "stripe_refund_id": refund["id"], "new_balance": new_balance}


@app.post("/tokens/redeem")
def redeem_token_code(req: RedeemCodeRequest, user_id: int = Depends(get_current_user_id)):
    code = req.code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Zadej kód")
    result = db.redeem_code(code, user_id)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


class CreateRedeemCodeRequest(BaseModel):
    tokens: int = 0
    max_uses: int = 1
    expires_in_days: Optional[int] = None
    note: str = ""
    code: Optional[str] = None  # vlastní text kódu (např. "BOOST") — jinak appka vygeneruje náhodný
    unlimited_days: int = 0  # >0 = kód navíc odemkne neomezené generování na N dní
    daily_cap_override: int = 0  # >0 = po dobu unlimited_days nižší denní strop než UNLIMITED_GENERATION_DAILY_CAP


@app.post("/admin/tokens/create-code")
def create_redeem_code_endpoint(req: CreateRedeemCodeRequest, request: Request):
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    code = req.code.strip().upper() if req.code else secrets.token_hex(4).upper()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=req.expires_in_days)
        if req.expires_in_days else None
    )
    db.create_redeem_code(code, req.tokens, req.max_uses, expires_at, req.note, req.unlimited_days, req.daily_cap_override)
    return {
        "code": code, "tokens": req.tokens, "max_uses": req.max_uses,
        "unlimited_days": req.unlimited_days or None,
        "daily_cap_override": req.daily_cap_override or None,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@app.post("/admin/create-channel-payment-link")
def admin_create_channel_payment_link(request: Request):
    """
    Appka takhle jednorázově vytvoří NOVÝ Stripe Payment Link pro
    Telegram kanál — appka ho používá při přecenění (viz CHANNEL_PRICE_KC),
    protože stávající Payment Link má cenu zapečenou na Stripe straně a
    appka ji odsud změnit nemůže. Appka stejnou strukturu (CZK, měsíční
    předplatné) používá jako u /payments/create-unlimited-checkout-session,
    jen appka místo dynamické Checkout Session vytváří TRVALÝ odkaz — appka
    tenhle produkt neváže na přihlášeného uživatele (kanál appka pozná
    jen podle e-mailu z platby).

    Appka vrácenou URL nikam sama neuloží — appka STRIPE_CHANNEL_PAYMENT_LINK_URL
    na Renderu musí ručně přepnout appce, appka na proměnné prostředí
    odsud přístup nemá.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
    try:
        price = stripe.Price.create(
            currency="czk",
            unit_amount=CHANNEL_PRICE_KC * 100,
            recurring={"interval": "month"},
            product_data={"name": "Telegram kanál na měsíc — ApexSignal"},
        )
        payment_link = stripe.PaymentLink.create(
            line_items=[{"price": price.id, "quantity": 1}],
            after_completion={"type": "redirect", "redirect": {"url": f"{frontend_url}/?payment=success"}},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"price_id": price.id, "payment_link_id": payment_link.id, "payment_link_url": payment_link.url, "price_kc": CHANNEL_PRICE_KC}


# =====================================================================
# Pomocné funkce — stahují zápasy pro každý sport a skládají MatchInput
# =====================================================================
def _enrich_one_fixture(provider, raw: dict, standings_cache: dict, standings_lock) -> Optional[MatchInput]:
    """Appka tady udělá VŠECHNA obohacující volání pro JEDEN zápas — viz
    _build_football_matches, co tohle pustí pro víc zápasů SOUBĚŽNĚ
    (vlákna), ne jedno po druhém."""
    fixture = data_provider.adapt_api_football_fixture(raw)
    league_id = fixture.get("league_id")
    home_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["home_team_id"], league_id))
    away_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["away_team_id"], league_id))
    home_fallback = home_stats.get("games_played") == 1 and home_stats.get("avg_goals_scored_last_10") == 1.2
    away_fallback = away_stats.get("games_played") == 1 and away_stats.get("avg_goals_scored_last_10") == 1.2
    flag = " <-- FALLBACK (appka pravděpodobně nesehnala reálná data)" if (home_fallback or away_fallback) else ""
    print(
        f"[enrich] {fixture['home_team']} (games={home_stats.get('games_played')}, "
        f"avg_goals={home_stats.get('avg_goals_scored_last_10')}) vs {fixture['away_team']} "
        f"(games={away_stats.get('games_played')}, avg_goals={away_stats.get('avg_goals_scored_last_10')}) "
        f"league_id={league_id}{flag}"
    )
    
    # BEZPEČNĚ beříodds s fallbackem
    try:
        odds_raw = provider.get_pre_match_odds(fixture["id"])
        odds = data_provider.adapt_api_football_odds(odds_raw)
    except Exception as e:
        print(f"[enrich] Warning: get_pre_match_odds failed for {fixture['home_team']} vs {fixture['away_team']}: {e}")
        odds = {"match_winner": {}, "over_goals": {}, "market_implied_probabilities": {}}  # Fallback prázdné kurzy
    
    data_availability: dict = {"market_odds": bool(odds.get("market_implied_probabilities"))}

    # Vážení nedávné formy ROZDĚLENÉ doma/venku + dny odpočinku ze
    # stejných dat (žádný extra dotaz na odpočinek navíc). Appka tahá
    # posledních 10 zápasů, ne 5 — po rozdělení na domácí/venkovní by
    # jinak často nezbylo dost dat (viz MIN_VENUE_SPLIT_SAMPLES).
    try:
        home_recent = provider.get_recent_form(fixture["home_team_id"], last=10)
        away_recent = provider.get_recent_form(fixture["away_team_id"], last=10)
        home_form = data_provider.adapt_recent_form_goals(home_recent, fixture["home_team_id"], venue="home")
        away_form = data_provider.adapt_recent_form_goals(away_recent, fixture["away_team_id"], venue="away")
        home_rest_days = data_provider.adapt_rest_days(home_recent, fixture["kickoff_time"])
        away_rest_days = data_provider.adapt_rest_days(away_recent, fixture["kickoff_time"])
        data_availability["recent_form"] = True
        data_availability["rest_days"] = home_rest_days is not None or away_rest_days is not None
    except Exception:
        home_form, away_form, home_rest_days, away_rest_days = None, None, None, None
        data_availability["recent_form"] = False
        data_availability["rest_days"] = False

    # Zranění/vyloučení pro tenhle konkrétní zápas — appka počítá jen
    # POČET jmen, ne jejich důležitost pro tým (viz injury_goal_adjustment_factor).
    try:
        injuries_raw = provider.get_injuries(fixture["id"])
        home_injury_count = data_provider.adapt_injuries(injuries_raw, fixture["home_team"])
        away_injury_count = data_provider.adapt_injuries(injuries_raw, fixture["away_team"])
        data_availability["injuries"] = True
    except Exception:
        home_injury_count, away_injury_count = 0, 0
        data_availability["injuries"] = False

    # Motivační faktor z tabulky soutěže — appka teď vrací spojitý faktor
    # (0.82-1.10) místo bool: titul/záchrana = vyšší intenzita, dead rubber = nižší.
    home_dead_rubber, away_dead_rubber = 1.0, 1.0
    data_availability["standings_motivation"] = False
    if league_id:
        try:
            with standings_lock:
                cached_standings = standings_cache.get(league_id)
            if cached_standings is None:
                cached_standings = provider.get_standings(league_id, fixture.get("season"))
                with standings_lock:
                    standings_cache[league_id] = cached_standings
            league_id_int = int(league_id) if league_id else None
            home_dead_rubber = data_provider.adapt_standings_for_motivation(cached_standings, fixture["home_team"], league_id=league_id_int)
            away_dead_rubber = data_provider.adapt_standings_for_motivation(cached_standings, fixture["away_team"], league_id=league_id_int)
            data_availability["standings_motivation"] = bool(cached_standings)
        except Exception:
            pass

    # Počasí na stadionu v čase výkopu — Open-Meteo, zdarma, bez klíče.
    # Geokódování města je kešované navždy (města se nehýbou), takže
    # tohle nepřidává trvalou zátěž na denní limit API-Football.
    weather = data_provider.get_match_weather(fixture.get("venue_city"), fixture.get("kickoff_time"))
    data_availability["weather"] = weather is not None

    return data_provider.normalize_to_match_input(
        Sport.FOOTBALL, fixture, home_stats, away_stats, odds, home_form, away_form, weather,
        home_injury_count=home_injury_count, away_injury_count=away_injury_count,
        home_rest_days=home_rest_days, away_rest_days=away_rest_days,
        home_dead_rubber=home_dead_rubber, away_dead_rubber=away_dead_rubber,
        data_availability=data_availability,
    )


def _build_football_matches(provider, raw_fixtures: list[dict], request_id: Optional[str] = None) -> list[MatchInput]:
    """
    Appka zpracuje zápasy SOUBĚŽNĚ (víc vláken najednou), ne jeden po
    druhém — appka na každý zápas potřebuje ~6 síťových volání, a ty
    čekají hlavně na odpověď API (ne na CPU appky), takže paralelizace
    přes vlákna appce reálně zkrátí celkový čas zhruba úměrně počtu
    vláken, beze zvýšení spotřeby kvóty API (appka udělá stejný POČET
    volání, jen ne všechna jedno po druhém).
    """
    standings_cache: dict = {}
    standings_lock = threading.Lock()
    matches: list[MatchInput] = []

    _progress_set_total(request_id, len(raw_fixtures))

    with ThreadPoolExecutor(max_workers=FIXTURE_ENRICHMENT_WORKERS) as executor:
        future_to_idx = {
            executor.submit(_enrich_one_fixture, provider, raw, standings_cache, standings_lock): idx
            for idx, raw in enumerate(raw_fixtures, start=1)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                match = future.result()
                if match is not None:
                    matches.append(match)
            except Exception as exc:
                print(f"[ERROR] _enrich_one_fixture failed for fixture index {idx}: {exc}")  # Viditelný log!
            _progress_increment(request_id)

    return matches


def _build_hockey_matches(provider, raw_games: list[dict]) -> list[MatchInput]:
    matches: list[MatchInput] = []
    for raw in raw_games:
        g = data_provider.adapt_apisports_game(raw)
        home = data_provider.adapt_apisports_hockey_team_stats(provider.get_team_statistics(Sport.HOCKEY, g["home_team_id"]))
        away = data_provider.adapt_apisports_hockey_team_stats(provider.get_team_statistics(Sport.HOCKEY, g["away_team_id"]))
        matches.append(MatchInput(
            match_id=g["id"], sport=Sport.HOCKEY, home_team=g["home_team"], away_team=g["away_team"],
            home_expected_goals=home["goals_avg"], away_expected_goals=away["goals_avg"],
            expected_penalty_minutes=home["penalty_minutes_avg"] + away["penalty_minutes_avg"],
            # odds se nedoplňují tady — bez reálné ceny nemá smysl trh nabízet
            # (viz _enrich_with_odds_api níže, který je jediný zdroj skutečných kurzů pro tento sport)
        ))
    return matches


def _build_basketball_matches(provider, raw_games: list[dict]) -> list[MatchInput]:
    matches: list[MatchInput] = []
    for raw in raw_games:
        g = data_provider.adapt_apisports_game(raw)
        home = data_provider.adapt_apisports_basketball_team_stats(provider.get_team_statistics(Sport.BASKETBALL, g["home_team_id"]))
        away = data_provider.adapt_apisports_basketball_team_stats(provider.get_team_statistics(Sport.BASKETBALL, g["away_team_id"]))
        total_points = home["points_avg"] + away["points_avg"]
        # hrubý fallback odhad výhry z poměru průměrných bodů — přepíše se
        # tržní (de-vigovanou) pravděpodobností, pokud ji najde _enrich_with_odds_api
        win_prob = home["points_avg"] / total_points if total_points > 0 else 0.5
        matches.append(MatchInput(
            match_id=g["id"], sport=Sport.BASKETBALL, home_team=g["home_team"], away_team=g["away_team"],
            home_win_probability=win_prob,
            expected_total_points=total_points, expected_total_threes=home["threes_avg"] + away["threes_avg"],
        ))
    return matches


def _build_tennis_matches(provider, raw_fixtures: list[dict]) -> list[MatchInput]:
    matches: list[MatchInput] = []
    for raw in raw_fixtures:
        f = data_provider.adapt_api_tennis_fixture(raw)
        home = data_provider.adapt_api_tennis_player_stats(provider.get_team_statistics(Sport.TENNIS, f["home_team_id"]))
        away = data_provider.adapt_api_tennis_player_stats(provider.get_team_statistics(Sport.TENNIS, f["away_team_id"]))
        total_winrate = home["win_rate"] + away["win_rate"]
        win_prob = home["win_rate"] / total_winrate if total_winrate > 0 else 0.5
        try:
            match_id = int(f["id"])
        except (ValueError, TypeError):
            match_id = abs(hash(f["id"])) % (10 ** 9)
        matches.append(MatchInput(
            match_id=match_id, sport=Sport.TENNIS, home_team=f["home_team"], away_team=f["away_team"],
            home_win_probability=win_prob,
            # expected_total_games/aces nejsou z api-tennis.com odvozené reálně
            # (viz poznámka v adapt_api_tennis_player_stats) — fixní rozumný odhad
            expected_total_games=22.0, expected_total_aces=14.0,
        ))
    return matches


def _enrich_with_market_odds(matches: list[MatchInput], sport: Sport) -> None:
    """
    Doplní reálné kurzy a de-vigované pravděpodobnosti z the-odds-api.com,
    napárované na zápas fuzzy shodou jména týmu (viz find_matching_odds_event).
    Tichá no-op, pokud ODDSAPI_KEY není nastaven — appka pak běží jen na
    vlastním odhadu.
    """
    try:
        odds_provider = data_provider.OddsAPIProvider()
    except RuntimeError:
        return

    events = odds_provider.get_odds(sport)
    totals_market = {
        Sport.FOOTBALL: MarketType.OVER_GOALS, Sport.HOCKEY: MarketType.OVER_GOALS,
        Sport.BASKETBALL: MarketType.OVER_POINTS, Sport.TENNIS: MarketType.OVER_GAMES,
    }[sport]

    matched_count = 0
    for match in matches:
        event = data_provider.find_matching_odds_event(events, match.home_team, match.away_team, match.kickoff_date)
        if not event:
            continue
        matched_count += 1
        adapted = data_provider.adapt_odds_api_event(event)
        if adapted["favorite_win_market_odds"]:
            match.favorite_win_market_odds = adapted["favorite_win_market_odds"]
        match.market_implied_probabilities.update(adapted["market_implied_probabilities"])
        if adapted.get("btts_yes_odds"):
            match.btts_yes_odds = adapted["btts_yes_odds"]
        if adapted["market_implied_probabilities"]:
            # the-odds-api je DALŠÍ (ne jediný) zdroj tržních kurzů — appka
            # počet bookmakerů přepíše jeho hodnotou jen tehdy, když reálně
            # něco dodal, a oznaci market_odds jako dostupné, i kdyby
            # API-Football vlastní kurzy předtím nesehnal.
            match.market_odds_bookmaker_count = adapted.get("bookmaker_count")
            match.data_availability["market_odds"] = True

        if adapted["over_threshold"] is not None:
            threshold, odds = adapted["over_threshold"], adapted["over_odds"]
            match.market_implied_probabilities[f"{totals_market.value}:over_{threshold}"] = adapted["over_probability"]
            target_dict = {
                MarketType.OVER_GOALS: match.over_goals_odds,
                MarketType.OVER_POINTS: match.over_points_odds,
                MarketType.OVER_GAMES: match.over_games_odds,
            }[totals_market]
            target_dict[threshold] = odds
            # Under appka zatím řeší jen pro góly (fotbal/hokej) — basketbal
            # a tenis appka v produkci nepoužívá, viz build_candidates.
            if totals_market == MarketType.OVER_GOALS and adapted.get("under_odds") is not None:
                match.market_implied_probabilities[f"{totals_market.value}:under_{threshold}"] = adapted["under_probability"]
                match.under_goals_odds[threshold] = adapted["under_odds"]

    print(f"[enrich-odds] {len(events)} events z the-odds-api, {matched_count}/{len(matches)} zápasů napárováno")


def _fetch_candidate_matches(sports: list[Sport], time_frame_days: int, request_id: Optional[str] = None) -> list[MatchInput]:
    """
    Vrátí zápasy v daném horziontu dnů (bez konkrétních dat).
    Důvod: horizont (1-4 dny) je jednodušší, přirozený a bez chyb
    oproti parsování konkrétního YYYY-MM-DD data.

    Filtrování na ligy dostupné na Tipsportu (podle league_id) se děje
    už v data_provider.py (TIPSPORT_LEAGUE_IDS, aplikováno v
    get_upcoming_matches) — zde se NEDUPLIKUJE, aby nedocházelo k
    rozporu mezi dvěma nezávislými seznamy ID.

    request_id appka posílá jen fotbalu (_build_football_matches) — jediný
    sport, co appka reálně v produkci nabízí a jediný s obohacovací
    smyčkou dost dlouhou na to, aby appce dávalo smysl ukazovat postup.
    """
    builders = {
        Sport.FOOTBALL: _build_football_matches,
        Sport.HOCKEY: _build_hockey_matches,
        Sport.BASKETBALL: _build_basketball_matches,
        Sport.TENNIS: _build_tennis_matches,
    }
    matches: list[MatchInput] = []

    for sport in sports:
        try:
            provider = data_provider.get_provider(sport)
        except (NotImplementedError, RuntimeError):
            continue

        try:
            raw_items = provider.get_upcoming_matches(sport, time_frame_days)
        except RuntimeError as e:
            # Appka na tohle dřív spadla nezachycenou 500 (typicky vyčerpaná
            # denní kvóta API-Football, viz "You have reached the request
            # limit for the day") — appka teď daný sport přeskočí (ostatní
            # sporty/zdroje dat zkusí dál) místo shození celého požadavku.
            # Volající (/tickets/generate apod.) na prázdný/menší seznam
            # zápasů už reaguje existujícím "Tiket se nepovedl" chováním.
            print(f"[_fetch_candidate_matches] {sport}: nepodařilo se stáhnout zápasy: {e}")
            continue

        if sport == Sport.FOOTBALL:
            sport_matches = _build_football_matches(provider, raw_items, request_id=request_id)
        else:
            sport_matches = builders[sport](provider, raw_items)

        _enrich_with_market_odds(sport_matches, sport)
        matches.extend(sport_matches)
    return matches


def _filter_future_matches(matches: list[MatchInput], buffer_minutes: int = 5) -> list[MatchInput]:
    """
    Filtruj zápasy: vrátí jen ty co jsou v BUDOUCNOSTI.
    
    buffer_minutes: Nebudeme generovat tikety na zápasy co začínají za <N minut
    """
    now = datetime.now(timezone.utc)
    buffer = timedelta(minutes=buffer_minutes)
    
    future_matches = []
    for m in matches:
        try:
            # Kombinuj kickoff_date (YYYY-MM-DD) + kickoff_time (HH:MM) → ISO format
            kickoff_str = f"{m.kickoff_date}T{m.kickoff_time}:00Z"  # "2024-07-18T15:00:00Z"
            kickoff_dt = datetime.fromisoformat(kickoff_str.replace('Z', '+00:00'))
            
            # Ověř že je v BUDOUCNOSTI (s bufferem)
            if kickoff_dt > now + buffer:
                future_matches.append(m)
            else:
                print(f"[filter_future] SKIPPED (in past): {m.home_team} vs {m.away_team} @ {kickoff_str}")
        except (ValueError, AttributeError, TypeError) as e:
            # Parsování selhalo → bezpečně přidej (lepší false-positive než false-negative)
            print(f"[filter_future] WARNING: Parsování selhalo pro {m.home_team} vs {m.away_team}: {e}, přidávám")
            future_matches.append(m)
    
    print(f"[filter_future] Filtrování: {len(matches)} → {len(future_matches)} (odstraněno {len(matches) - len(future_matches)} starých)")
    return future_matches


def _filter_within_days(matches: list[MatchInput], days: int) -> list[MatchInput]:
    """
    Vrátí jen zápasy s kickoffem do `days` dnů od teď — appka tohle
    použije na už STAŽENÁ a OBOHACENÁ data (viz _fetch_candidate_matches),
    aby při rozšiřování horizontu (viz /tickets/generate) appka nemusela
    stahovat a obohacovat zápasy DVAKRÁT (jednou pro užší okno, znovu pro
    širší) — appka rovnou stáhne širší okno a užší je jen jeho podmnožina.

    Appka dřív při chybě parsování zápas ROVNOU PONECHALA ("false-positive
    je lepší než false-negative") — ale u zápasů s appkou ještě
    nepotvrzeným přesným časem výkopu appka dostává kickoff_time jako
    prázdný řetězec, takže "2026-08-02T:00Z" appce vždycky spadlo na
    chybu a appka takový zápas propustila BEZ OHLEDU na zvolené okno
    (nahlášeno uživatelem — zvolil 1denní okno, appka mu tiše vrátila
    zápas 3 dny dopředu, bez horizon_note, protože z pohledu appky
    "žádná chyba/rozšíření" nenastalo). Appka teď chybějící čas bere
    jako půlnoc daného dne (nejpřísnější odhad pro "je to v okně?") — a
    když appka nemá k dispozici ani datum, zápas radši VYNECHÁ, než aby
    tvrdila, že splňuje kritérium, které appka vůbec nemohla ověřit.
    """
    cutoff = datetime.now(timezone.utc) + timedelta(days=days)
    result = []
    for m in matches:
        try:
            kickoff_dt = datetime.fromisoformat(f"{m.kickoff_date}T{m.kickoff_time or '00:00'}:00Z".replace('Z', '+00:00'))
            if kickoff_dt <= cutoff:
                result.append(m)
        except (ValueError, AttributeError, TypeError):
            pass  # appka nemůže ověřit, jestli je zápas v okně — radši ho vynechá
    return result


# =====================================================================
# REST endpointy — Generátor tiketů
# =====================================================================
@app.get("/tickets/generate-progress")
def get_generate_progress(request_id: str):
    """
    Tenhle endpoint appce (frontendu) umožní zeptat se, jak appka pokročila
    v běžícím /tickets/generate (nebo /regenerate) — appka na tohle
    pravidelně pollne, dokud hlavní požadavek neskončí, a appka z toho
    vykreslí SKUTEČNÉ procento (dřív jen dekorativní animaci bez vazby
    na realitu). Záměrně bez přihlášení — nese jen dvě čísla, ke
    spárování stačí appkou vygenerovaný request_id, a uhodnutí cizího by
    prozradilo maximálně počet zápasů, žádná citlivá data.
    """
    with _GENERATION_PROGRESS_LOCK:
        entry = _GENERATION_PROGRESS.get(request_id)
    if not entry:
        return {"known": False, "done": 0, "total": 0}
    return {"known": True, "done": entry["done"], "total": entry["total"]}


def _all_markets_for_sports(sports: list[Sport]) -> list[MarketType]:
    """Sjednocení všech trhů, co appka pro dané sporty vůbec zná (viz
    SPORT_MARKETS) — appka tohle použije jako poslední záchrannou síť
    při generování, když ani širší časové okno nestačí (viz níž)."""
    seen: list[MarketType] = []
    for sport in sports:
        for market in SPORT_MARKETS.get(sport, []):
            if market not in seen:
                seen.append(market)
    return seen


def _run_generate_job(user_id: int, req: TicketGenerateRequest) -> TicketPairResponse:
    # Uložené zápasy + zápasy z JAKÉHOKOLIV předchozího generování v týhle
    # (ještě neuložené) sérii — jinak by druhé volání (jiný risk_level =
    # jiný typ tiketu) klidně nabídlo STEJNÝ zápas jako to první, protože
    # by o něm ještě nevědělo (uloží se, až uživatel klikne "uložit").
    exclude_ids = set(repo.get_all_saved_match_ids(user_id)) | set(repo.get_last_batch(user_id))

    try:
        # Appka nejdřív obohatí jen ZVOLENÉ (užší) okno — obohacení je
        # nejdražší část (~11 síťových volání na zápas přes rate limiter) a
        # drtivá většina požadavků má v tomhle okně kandidátů dost (viz
        # /admin/candidate-pool-preview). Širší okno appka stahuje a
        # obohacuje AŽ na neúspěch, ne pořád dopředu "pro jistotu" — appka
        # dřív širší okno stahovala VŽDY, i když ji přes 90 % požadavků
        # vůbec nepoužilo, což generování zbytečně natahovalo o desítky
        # sekund až minuty (nahlášeno uživatelem — 2 minuty na krátký tiket
        # s 1denním oknem). Sdílený cache týmových statistik (1 h, viz
        # data_provider.get_provider) navíc dělá dodatečné stažení širšího
        # okna při neúspěchu podstatně levnější, ne dvojnásobně drahé — týmy
        # z užšího okna už appka má obohacené.
        matches = _fetch_candidate_matches(req.sports, req.time_frame_days, request_id=req.request_id)
        matches = [m for m in matches if m.match_id not in exclude_ids]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        matches = _filter_within_days(matches, req.time_frame_days)

        horizon_note = None
        result = ticket_generator.generate(
            matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
            pool_filter=_pool_filter_for_risk(req.risk_level),
        )

        # Appka dřív tohle rozšíření dělala TICHOU — uživatel si vybral "1 den"
        # a dostal zpátky tiket se zápasy klidně 4 dny dopředu, aniž by o tom
        # appka řekla jediné slovo. Teď to appka pořád zkusí (ať appka nenechá
        # uživatele zbytečně čekat na "nic nenašla", i když je opravdu ochotná
        # nabídnout aspoň něco), ale VŽDY to uživateli řekne přes horizon_note,
        # co appka reálně udělala.
        if result["safe"] is None:
            wider_days = req.time_frame_days + 3
            all_wider_matches = _fetch_candidate_matches(req.sports, wider_days, request_id=req.request_id)
            all_wider_matches = [m for m in all_wider_matches if m.match_id not in exclude_ids]
            all_wider_matches = _filter_future_matches(all_wider_matches, buffer_minutes=5)
            wider_result = ticket_generator.generate(
                all_wider_matches, req.risk_level, req.sports, req.market_types, wider_days,
                pool_filter=_pool_filter_for_risk(req.risk_level),
            )
            if wider_result["safe"] is not None:
                result = wider_result
                horizon_note = (
                    f"Appka v tvém vybraném časovém rámci ({req.time_frame_days} "
                    f"{'den' if req.time_frame_days == 1 else 'dny'}) nenašla žádnou kombinaci s dostatečnou "
                    f"důvěrou, tak nabízí nejbližší dostupnou možnost — zápasy až za {wider_days} dní."
                )
            else:
                # I širší okno nestačilo — poslední záchranná síť je zkusit
                # VŠECHNY trhy pro daný sport, ne jen ty, co si uživatel
                # vybral (typicky "Výhra + Over gólů + Oba dají gól" bez
                # "Under gólů" — a zrovna Under góly bývá tržně nejsilnější
                # kandidát). Appka na to nemusí stahovat nic navíc, širší
                # zápasy už má obohacené z kroku výš.
                all_markets = _all_markets_for_sports(req.sports)
                if set(all_markets) != set(req.market_types):
                    markets_result = ticket_generator.generate(
                        all_wider_matches, req.risk_level, req.sports, all_markets, wider_days,
                        pool_filter=_pool_filter_for_risk(req.risk_level),
                    )
                    if markets_result["safe"] is not None:
                        result = markets_result
                        horizon_note = (
                            f"Appka v tvém vybraném časovém rámci ({req.time_frame_days} "
                            f"{'den' if req.time_frame_days == 1 else 'dny'}) a zvolených trzích nenašla žádnou "
                            f"kombinaci s dostatečnou důvěrou, tak zkusila i ostatní trhy (např. Under góly) — "
                            f"nabízí zápasy až za {wider_days} dní."
                        )

        used_ids = [s.match_id for t in result.values() if t for s in t.selections]
        repo.set_last_batch(user_id, used_ids)

        if result["safe"] is not None:
            _charge_tokens_for_ticket(user_id, result["safe"].ticket_type)

        return TicketPairResponse(
            safe=TicketResponse.from_domain(result["safe"], horizon_note=horizon_note) if result["safe"] else None,
            aggressive=TicketResponse.from_domain(result["aggressive"], horizon_note=horizon_note) if result["aggressive"] else None,
        )
    except Exception:
        raise


def _run_regenerate_job(user_id: int, req: TicketGenerateRequest) -> TicketPairResponse:
    previous_ids = repo.get_last_batch(user_id)
    exclude_ids = repo.get_all_saved_match_ids(user_id)  # Všechny již vsazené zápasy
    combined_exclude = set(previous_ids) | set(exclude_ids)

    try:
        # Viz stejná poznámka v generate_tickets — appka obohatí jen zvolené
        # okno, širší dotáhne až na neúspěch.
        matches = _fetch_candidate_matches(req.sports, req.time_frame_days, request_id=req.request_id)
        matches = [m for m in matches if m.match_id not in combined_exclude]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        matches = _filter_within_days(matches, req.time_frame_days)

        horizon_note = None
        result = ticket_generator.regenerate(
            matches, req.risk_level, req.sports, req.market_types, req.time_frame_days, list(previous_ids),
            pool_filter=_pool_filter_for_risk(req.risk_level),
        )

        # Viz stejná poznámka v generate_tickets — appka rozšíření pořád
        # zkusí, ale vždycky to řekne přes horizon_note.
        if result["safe"] is None:
            wider_days = req.time_frame_days + 3
            all_wider_matches = _fetch_candidate_matches(req.sports, wider_days, request_id=req.request_id)
            all_wider_matches = [m for m in all_wider_matches if m.match_id not in combined_exclude]
            all_wider_matches = _filter_future_matches(all_wider_matches, buffer_minutes=5)
            wider_result = ticket_generator.regenerate(
                all_wider_matches, req.risk_level, req.sports, req.market_types, wider_days, list(previous_ids),
                pool_filter=_pool_filter_for_risk(req.risk_level),
            )
            if wider_result["safe"] is not None:
                result = wider_result
                horizon_note = (
                    f"Appka v tvém vybraném časovém rámci ({req.time_frame_days} "
                    f"{'den' if req.time_frame_days == 1 else 'dny'}) nenašla žádnou kombinaci s dostatečnou "
                    f"důvěrou, tak nabízí nejbližší dostupnou možnost — zápasy až za {wider_days} dní."
                )
            else:
                # Viz stejná poznámka v generate_tickets — poslední záchranná
                # síť je zkusit všechny trhy pro daný sport, ne jen zvolené.
                all_markets = _all_markets_for_sports(req.sports)
                if set(all_markets) != set(req.market_types):
                    markets_result = ticket_generator.regenerate(
                        all_wider_matches, req.risk_level, req.sports, all_markets, wider_days, list(previous_ids),
                        pool_filter=_pool_filter_for_risk(req.risk_level),
                    )
                    if markets_result["safe"] is not None:
                        result = markets_result
                        horizon_note = (
                            f"Appka v tvém vybraném časovém rámci ({req.time_frame_days} "
                            f"{'den' if req.time_frame_days == 1 else 'dny'}) a zvolených trzích nenašla žádnou "
                            f"kombinaci s dostatečnou důvěrou, tak zkusila i ostatní trhy (např. Under góly) — "
                            f"nabízí zápasy až za {wider_days} dní."
                        )

        used_ids = [s.match_id for t in result.values() if t for s in t.selections]
        repo.set_last_batch(user_id, used_ids)

        if result["safe"] is not None:
            _charge_tokens_for_ticket(user_id, result["safe"].ticket_type)

        return TicketPairResponse(
            safe=TicketResponse.from_domain(result["safe"], horizon_note=horizon_note) if result["safe"] else None,
            aggressive=TicketResponse.from_domain(result["aggressive"], horizon_note=horizon_note) if result["aggressive"] else None,
        )
    except Exception:
        raise


def _start_generation_job(user_id: int, req: TicketGenerateRequest, run_fn) -> str:
    """
    Spustí generování na SAMOSTATNÉM vlákně a hned se vrátí s request_id —
    appka na výsledek dál nečeká v rámci jednoho HTTP požadavku (viz
    komentář u _GENERATION_RESULTS). Appka na vlákno záměrně nedává
    žádný timeout — pokud appka nedoběhne do TTL (10 min), appka na to
    frontend upozorní jako na vypršelý požadavek, ne appka to tiše zabije.
    """
    request_id = req.request_id or secrets.token_urlsafe(8)
    req.request_id = request_id
    _results_store(request_id, {"status": "processing"})

    def worker():
        try:
            result = run_fn(user_id, req)
            _results_store(request_id, {"status": "done", "result": result})
        except HTTPException as e:
            _results_store(request_id, {"status": "error", "detail": e.detail})
        except Exception as e:
            print(f"[generate-job] {request_id} selhalo: {e}")
            _results_store(request_id, {"status": "error", "detail": "Generování se nepovedlo, zkus to znovu."})
        finally:
            _progress_clear(request_id)

    threading.Thread(target=worker, daemon=True).start()
    return request_id


@app.get("/tickets/generate-result")
def get_generate_result(request_id: str):
    """
    Appka na tohle pollne, dokud /tickets/generate(-start) neskončí — na
    rozdíl od jednoho dlouhého požadavku tenhle krátký dotaz přežije i
    to, že appka na telefonu mezitím na chvíli vypadne/se uspí. Záměrně
    bez přihlášení, stejně jako /tickets/generate-progress — request_id
    appka vygeneruje sama, uhodnutí cizího neprozradí nic citlivého.
    """
    payload = _results_get(request_id)
    if payload is None:
        return {"status": "unknown"}
    return payload


@app.post("/tickets/generate")
def generate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    _require_generation_enabled(user_id)
    _check_token_balance(user_id, req.risk_level)
    request_id = _start_generation_job(user_id, req, _run_generate_job)
    return {"request_id": request_id, "status": "processing"}


@app.post("/tickets/regenerate")
def regenerate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    _require_generation_enabled(user_id)
    _check_token_balance(user_id, req.risk_level)
    request_id = _start_generation_job(user_id, req, _run_regenerate_job)
    return {"request_id": request_id, "status": "processing"}


class SaveSelectionRequest(BaseModel):
    match_id: int = 0
    home_team: str = ""
    away_team: str = ""
    market_type: str = "match_winner"
    selection: str = ""
    probability: float = 0.0
    odds: float = 1.0
    model_probability: float = 0.0
    market_probability: Optional[float] = None
    edge: Optional[float] = None
    reasoning: str = ""
    data_quality: str = ""
    league: str = ""
    country: str = ""
    kickoff_date: str = ""
    kickoff_time: str = ""


class SaveTicketRequest(BaseModel):
    ticket_type: str = "stredni"
    selections: list[SaveSelectionRequest]
    total_odds: float = 1.0
    combined_probability: float = 0.0
    recommended_stake_pct: float = 0.0


@app.post("/tickets/save")
def save_ticket(req: SaveTicketRequest, user_id: int = Depends(get_current_user_id)):
    domain_selections = [
        SelectionCandidate(
            match_id=s.match_id, home_team=s.home_team, away_team=s.away_team,
            sport=Sport.FOOTBALL,
            market_type=MarketType(s.market_type) if s.market_type in [m.value for m in MarketType] else MarketType.MATCH_WINNER,
            selection=s.selection,
            probability=s.probability, odds=s.odds,
            model_probability=s.model_probability, market_probability=s.market_probability,
            reasoning=s.reasoning, data_quality=s.data_quality,
            league=s.league, country=s.country, kickoff_date=s.kickoff_date, kickoff_time=s.kickoff_time,
        ) for s in req.selections
    ]
    ticket = Ticket(
        ticket_type=req.ticket_type, selections=domain_selections,
        total_odds=req.total_odds, combined_probability=req.combined_probability,
        recommended_stake_pct=req.recommended_stake_pct,
    )
    ticket_id = repo.save_ticket(user_id, ticket)

    # Uložením appka považuje "prohlížecí session" za uzavřenou — nevybrané
    # zápasy z předchozích (neuložených) generování téhle appka user_id se
    # tak zase smí objevit příště (viz Repo.clear_last_batch). Bez tohohle
    # by opakované generování/regenerování bez ukládání postupně appce
    # vyloučilo čím dál víc zápasů, až by appce nakonec nezbylo dost
    # kandidátů — appka pak vracela "tiket se nepovedl", i když trh měl
    # kandidátů dost (nahlásil uživatel).
    repo.clear_last_batch(user_id)

    # IHNED se pokusit vyhodnotit - aby se selection results uložily do DB hned!
    try:
        row = db.fetch_ticket_rows(ticket_id=ticket_id)
        if row:
            selection_ids = [s.get("id") for s in row[0].get("selections", [])]
            if selection_ids:
                provider = data_provider.get_provider(Sport.FOOTBALL)
                new_status = _try_settle_ticket(provider, ticket, selection_ids)
                if new_status is not None:
                    repo.set_ticket_status(ticket_id, new_status)
    except Exception as e:
        pass  # Tiket se nepovedl vyhodnotit ihned, je OK
    
    return {"ticket_id": ticket_id, "status": "saved"}


@app.get("/admin/backfill-results")
def backfill_results(user_id: int = Depends(get_current_user_id)):
    """Backfill old tickets - compute selection results for tickets missing them"""
    provider = data_provider.get_provider(Sport.FOOTBALL)
    updated = 0

    for row in repo.get_saved_tickets(user_id):
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        if not selection_ids:
            continue

        new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
        if new_status is not None:
            if row["status"] != new_status:
                repo.set_ticket_status(row["ticket_id"], new_status)
                updated += 1

    return {"backfilled": updated}


@app.get("/admin/calibration-report")
def admin_calibration_report(request: Request):
    """
    Appka tímhle appce ověří, jestli je model KALIBROVANÝ — když appka
    řekne "70% šance", vyhrává to reálně kolem 70 %, nebo výrazně míň?
    To je jiná otázka než syrová úspěšnost (tu appka řeší ve
    /admin/win-loss-report) — appka tady bucketuje výběry podle
    appkou odhadnuté pravděpodobnosti (model_probability) a porovná
    to se skutečnou frekvencí výhry v tom koši. Appka navíc appce
    ukáže trend posledních 30 dní vs. staršího období, ať appka pozná,
    jestli se něco reálně zhoršuje, nebo appka jen poprvé měří.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        # Kalibrace: appka bucketuje po 5 procentních bodech podle
        # model_probability v okamžiku generování (appka to ukládá na
        # výběr, ne dopočítává zpětně).
        cur.execute(
            """
            SELECT model_probability, result
              FROM ticket_selections
             WHERE result IN ('won', 'lost') AND model_probability IS NOT NULL
            """
        )
        calib_rows = cur.fetchall()

        # Trend: appka porovná tikety vytvořené za posledních 30 dní
        # proti starším, podle DATA VYTVOŘENÍ (ne vyhodnocení) — appka
        # tak vidí, jestli je to zhoršující se model, nebo appka jen
        # měří poprvé.
        cur.execute(
            """
            SELECT
                (created_at > now() - interval '30 days') AS recent,
                status
              FROM tickets
             WHERE status IN ('won', 'lost')
            """
        )
        trend_rows = cur.fetchall()

        # Appka zvlášť vypíchne "oba dají gól" a "over_cards" — nejslabší
        # trhy podle win-loss-report — appka je rozpadne podle appkou
        # odhadnuté a tržní pravděpodobnosti, ať appka pozná, jestli appka
        # měla k dispozici tržní kurz (spolehlivější) nebo jela naslepo
        # jen na vlastním modelu.
        cur.execute(
            """
            SELECT market_type, result,
                   (market_probability IS NOT NULL) AS had_market_odds
              FROM ticket_selections
             WHERE result IN ('won', 'lost') AND market_type IN ('btts', 'over_cards')
            """
        )
        weak_market_rows = cur.fetchall()

    # --- kalibrace ---
    buckets: dict[int, dict] = {}
    for row in calib_rows:
        p = row["model_probability"]
        if p is None:
            continue
        bucket = int(round(float(p) * 20)) * 5  # appka zaokrouhlí na nejbližších 5 %
        bucket = max(0, min(100, bucket))
        acc = buckets.setdefault(bucket, {"won": 0, "lost": 0})
        acc[row["result"]] += 1
    calibration = []
    for bucket in sorted(buckets):
        acc = buckets[bucket]
        total = acc["won"] + acc["lost"]
        actual_pct = round(acc["won"] / total * 100, 1) if total else 0.0
        calibration.append({
            "appka_odhaduje_pct": bucket,
            "reálně_vyhrálo_pct": actual_pct,
            "rozdíl_pct": round(actual_pct - bucket, 1),
            "počet_výběrů": total,
        })

    # --- trend ---
    recent = {"won": 0, "lost": 0}
    older = {"won": 0, "lost": 0}
    for row in trend_rows:
        target = recent if row["recent"] else older
        target[row["status"]] += 1

    def _rate(d: dict) -> float:
        total = d["won"] + d["lost"]
        return round(d["won"] / total * 100, 1) if total else 0.0

    # --- slabé trhy ---
    weak: dict[str, dict] = {}
    for row in weak_market_rows:
        m = row["market_type"]
        key = "s_tržním_kurzem" if row["had_market_odds"] else "jen_vlastní_model"
        acc = weak.setdefault(m, {}).setdefault(key, {"won": 0, "lost": 0})
        acc[row["result"]] += 1
    weak_out = {}
    for m, groups in weak.items():
        weak_out[m] = {k: {**v, "win_rate_pct": _rate(v)} for k, v in groups.items()}

    return {
        "kalibrace_podle_koše": calibration,
        "poznamka_kalibrace": "Pokud 'reálně_vyhrálo_pct' soustavně zaostává za 'appka_odhaduje_pct', model je PŘEHNANĚ SEBEJISTÝ — přeceňuje své šance.",
        "trend": {
            "poslednich_30_dni": {**recent, "win_rate_pct": _rate(recent)},
            "starsi": {**older, "win_rate_pct": _rate(older)},
        },
        "slabe_trhy_btts_a_over_cards": weak_out,
    }


@app.get("/admin/win-loss-report")
def admin_win_loss_report(request: Request):
    """
    Appka tímhle appce spočítá kompletní statistiku úspěšnosti napříč
    VŠEMI účty appky (appčiny vlastní i klientské) — kolik tiketů/výběrů
    appka vyhrála/prohrála, rozpad podle typu tiketu a podle typu trhu.
    Appka počítá jen vyhodnocené (won/lost), pending appka do procent
    nezapočítává (nemá smysl, výsledek appka ještě nezná).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS c FROM tickets GROUP BY status")
        by_status = {r["status"]: r["c"] for r in cur.fetchall()}

        # Appka zisk/ztrátu nedrží jako sloupec — appka ho dopočítává za
        # běhu ze sázky/kurzu/výsledku (viz Repo._compute_actual_profit_loss),
        # appka to tady dělá stejně, v Pythonu, ne v SQL.
        cur.execute(
            """
            SELECT ticket_type, status, total_odds, actual_stake_amount, actual_odds
              FROM tickets
             WHERE status IN ('won', 'lost')
            """
        )
        settled_rows = cur.fetchall()

        def _pnl_row(row: dict) -> float:
            stake = row.get("actual_stake_amount")
            if stake is None:
                return 0.0
            stake = float(stake)
            odds = float(row.get("actual_odds") or row["total_odds"])
            return round(stake * (odds - 1), 2) if row["status"] == "won" else round(-stake, 2)

        type_acc: dict[str, dict] = {}
        for row in settled_rows:
            t = row["ticket_type"]
            acc = type_acc.setdefault(t, {"won": 0, "lost": 0, "staked": 0.0, "pnl": 0.0})
            acc[row["status"]] += 1
            acc["staked"] += float(row.get("actual_stake_amount") or 0)
            acc["pnl"] += _pnl_row(row)

        overall = {
            "c": len(settled_rows),
            "staked": sum(float(r.get("actual_stake_amount") or 0) for r in settled_rows),
            "pnl": sum(_pnl_row(r) for r in settled_rows),
            "avg_odds": (sum(float(r["total_odds"]) for r in settled_rows) / len(settled_rows)) if settled_rows else 0,
        }

        cur.execute(
            """
            SELECT ts.market_type, ts.result, COUNT(*) AS c
              FROM ticket_selections ts
              JOIN tickets t ON t.id = ts.ticket_id
             WHERE ts.result IN ('won', 'lost')
             GROUP BY ts.market_type, ts.result
            """
        )
        by_market_rows = cur.fetchall()

        cur.execute(
            """
            SELECT COUNT(*) AS c
              FROM ticket_selections
             WHERE result IN ('won', 'lost')
            """
        )
        total_selections = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM ticket_selections WHERE result = 'won'")
        won_selections = cur.fetchone()["c"]

    def _pct(won: int, lost: int) -> float:
        total = won + lost
        return round(won / total * 100, 1) if total else 0.0

    by_type = type_acc
    for v in by_type.values():
        v["win_rate_pct"] = _pct(v.get("won", 0), v.get("lost", 0))
        v["roi_pct"] = round(v["pnl"] / v["staked"] * 100, 1) if v["staked"] else 0.0

    by_market: dict[str, dict] = {}
    for row in by_market_rows:
        m = row["market_type"] or "neznámý"
        by_market.setdefault(m, {"won": 0, "lost": 0})
        by_market[m][row["result"]] = row["c"]
    for m, v in by_market.items():
        v["win_rate_pct"] = _pct(v.get("won", 0), v.get("lost", 0))

    won_t, lost_t = by_status.get("won", 0), by_status.get("lost", 0)
    staked = float(overall["staked"] or 0)
    pnl = float(overall["pnl"] or 0)

    return {
        "tickets_by_status": by_status,
        "tickets_overall": {
            "won": won_t,
            "lost": lost_t,
            "win_rate_pct": _pct(won_t, lost_t),
            "total_staked_kc": staked,
            "total_profit_loss_kc": round(pnl, 0),
            "roi_pct": round(pnl / staked * 100, 1) if staked else 0.0,
            "avg_odds": round(float(overall["avg_odds"] or 0), 2),
        },
        "tickets_by_type": by_type,
        "selections_overall": {
            "won": won_selections,
            "lost": total_selections - won_selections,
            "win_rate_pct": _pct(won_selections, total_selections - won_selections),
        },
        "selections_by_market": by_market,
    }


@app.post("/admin/settle-all-pending")
def admin_settle_all_pending(request: Request):
    """
    Appčin vlastní denní kanál (DAILY_TICKETS_USER_ID) si pending tikety
    dosettluje sám, jako první krok run_daily_tickets — ale BĚŽNÉ účty
    (appka si sama generuje/ukládá tikety v appce) tohle nemají vůbec —
    /tickets/save appka zkusí vyhodnotit jen JEDNOU, hned při uložení,
    kdy zápas skoro nikdy neskončil, a appka na to nemá žádný pravidelný
    "zkus to znovu později" mechanismus (nahlásil uživatel — zápasy
    dávno dohrané, appka je pořád ukazovala jako pending). Tenhle
    endpoint projde PENDING tikety napříč VŠEMI účty najednou — volá ho
    denní naplánovaná úloha vedle appčina vlastního kanálu, teď když je
    appka generování odemčené naostro všem.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    provider = data_provider.get_provider(Sport.FOOTBALL)
    rows = db.fetch_ticket_rows(status="pending")
    checked = 0
    updated = 0

    for row in rows:
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        if not selection_ids:
            continue
        checked += 1
        new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
        if new_status is not None:
            repo.set_ticket_status(row["ticket_id"], new_status)
            updated += 1

    return {"checked": checked, "updated": updated}


class AdminAlertRequest(BaseModel):
    message: str


@app.post("/admin/alert")
def admin_alert(req: AdminAlertRequest, request: Request):
    """
    Appka dřív neměla ŽÁDNÉ upozornění, když denní cron (nebo cokoliv
    jiného spouštěné přes GitHub Actions) selže — jediný způsob, jak se
    to appka dozvěděla, bylo si to ručně všimnout v Actions záložce na
    GitHubu. Tenhle endpoint appka volá z workflow kroku s `if: failure()`
    — pošle zprávu appčinu vlastnímu Telegramu (TELEGRAM_CHAT_ID), appka
    tak dostane push notifikaci prakticky okamžitě. Appka schválně
    nevolá GitHubí API odsud (žádný token na to appka nemá) — appka
    posílá jen text, který jí pošle volající (workflow).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return {"status": "skipped", "reason": "TELEGRAM_BOT_TOKEN nebo TELEGRAM_CHAT_ID není nastavené"}

    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data={"chat_id": chat_id, "text": req.message},
        timeout=15,
    )
    return {"status": "sent" if resp.ok else "error", "telegram_response": resp.json()}


@app.post("/admin/resettle-ticket")
def admin_resettle_ticket(ticket_id: int, request: Request):
    """
    Přesettluje JEDEN konkrétní tiket bez ohledu na jeho aktuální status —
    na rozdíl od /admin/settle-all-pending, co bere jen 'pending' tikety.
    Appka tohle přidala k opravě tiketu 318 (viz /admin/verify-results,
    2026-08-01): noha under_3.5 na Pafos vs. HNK Hajduk Split měla uložené
    'lost', i když skutečné skóre 2:0 z ní dělá výhru — appka to nikdy
    nepřesettlovala znovu, protože jednou uložené won/lost appka
    automaticky nepřezkoumává. Použije stejnou logiku (_try_settle_ticket)
    jako settle-all-pending, jen bez filtru na status.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    provider = data_provider.get_provider(Sport.FOOTBALL)
    rows = db.fetch_ticket_rows(ticket_id=ticket_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    row = rows[0]
    selection_ids = [s.get("id") for s in row.get("selections", [])]
    old_status = row["status"]
    new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
    if new_status is not None:
        repo.set_ticket_status(ticket_id, new_status)

    return {
        "ticket_id": ticket_id,
        "old_status": old_status,
        "new_status": new_status if new_status is not None else old_status,
        "selection_results": [s.get("result") for s in db.fetch_ticket_rows(ticket_id=ticket_id)[0].get("selections", [])],
    }


@app.post("/tickets/check-duplicates")
def check_duplicate_matches(req: dict = Body(...), user_id: int = Depends(get_current_user_id)):
    """
    Zkontroluj které selected matches jsou v PENDING tiketu.
    Input: {selections: [{match_id, home_team, away_team}, ...]}
    Output: {has_duplicates: bool, duplicates: [...], count: int}
    """
    pending_matches = repo.get_pending_match_ids(user_id)  # Vrací detaily!
    pending_ids_set = {m["match_id"] for m in pending_matches}
    
    duplicates = []
    for selection in req.get("selections", []):
        if selection["match_id"] in pending_ids_set:
            # Najdi detaily z pending matches
            for pm in pending_matches:
                if pm["match_id"] == selection["match_id"]:
                    duplicates.append({
                        "match_id": pm["match_id"],
                        "home_team": pm["home_team"],
                        "away_team": pm["away_team"],
                    })
                    break
    
    return {
        "has_duplicates": len(duplicates) > 0,
        "duplicates": duplicates,
        "count": len(duplicates),
    }


def _list_saved_tickets_for_user(user_id: int) -> list[TicketResponse]:
    """
    Appka VRACÍ historii přímo z DB, bez čekání na kontrolu živých
    zápasů — dřív appka před vrácením odpovědi dosettlovávala VŠECHNY
    pending tikety synchronně (volání API-Football pro každou nohu
    každého nevyřešeného tiketu), což při víc rozehraných tiketech
    natahovalo načtení Historie na několik sekund i víc. Appka teď
    settlování dělá odděleně (viz POST /tickets/settle, appka ho volá
    z frontendu na pozadí, nebo pravidelný cron) — Historie se díky
    tomu vždycky zobrazí okamžitě, i když stav pár posledních tiketů
    může být pár minut starý.

    Sdíleno mezi GET /tickets/saved (bere frontend jako fallback) a
    GET /tickets/sync (bere frontend PŘEDNOSTNĚ, viz HistoryView) —
    obě appka vrací STEJNÁ data, jen /tickets/sync appka zabalí do
    {"tickets": [...]} podle toho, co frontend při čtení očekává.
    """
    saved_rows = repo.get_saved_tickets(user_id)
    print(f"[DEBUG] _list_saved_tickets_for_user: Backend vrací CELKEM {len(saved_rows)} tiketů pro user_id={user_id}")
    pending_in_response = [r for r in saved_rows if r["status"] == "pending"]
    print(f"[DEBUG] _list_saved_tickets_for_user: Z toho PENDING: {len(pending_in_response)}")
    
    result_list = []
    for row in saved_rows:
        # Konvertuj created_at na ISO string (DB vrací datetime)
        created_at_str = None
        if row.get("created_at"):
            created_at_dt = row["created_at"]
            if hasattr(created_at_dt, 'isoformat'):
                created_at_str = created_at_dt.isoformat()
            else:
                created_at_str = str(created_at_dt)
        
        tr = TicketResponse.from_domain(
            row["ticket"], row["ticket_id"], row["status"], row["live_alert"],
            row["actual_stake_amount"], row["actual_odds"], row["actual_profit_loss"],
            created_at=created_at_str  # ← TEĎKA JE TADY!
        )
        # Přidej result z raw selections (DB) do každého výběru
        raw_sels = row.get("selections", [])
        updated_selections = []
        for i, sel in enumerate(tr.selections):
            result_value = "pending"
            selection_id = None
            odds_value = sel.odds
            goals_h = sel.home_goals
            goals_a = sel.away_goals
            
            if i < len(raw_sels):
                raw = raw_sels[i]
                result_value = raw.get("result") or "pending"
                selection_id = raw.get("id")
                print(f"[list_saved_tickets] Selection {i}: id={selection_id}, result={result_value}")  # DEBUG!
                if raw.get("odds"):
                    odds_value = float(raw["odds"])
                if raw.get("home_goals") is not None:
                    goals_h = raw.get("home_goals")
                    goals_a = raw.get("away_goals")
            
            # Vytvořit NOVÝ SelectionResponse s updated hodnotami
            updated_sel = SelectionResponse(
                match_id=sel.match_id,
                home_team=sel.home_team,
                away_team=sel.away_team,
                market_type=sel.market_type,
                selection=sel.selection,
                probability=sel.probability,
                odds=odds_value,
                model_probability=sel.model_probability,
                market_probability=sel.market_probability,
                edge=sel.edge,
                reasoning=sel.reasoning,
                data_quality=sel.data_quality,
                league=sel.league,
                country=sel.country,
                kickoff_date=sel.kickoff_date,
                kickoff_time=sel.kickoff_time,
                home_goals=goals_h,
                away_goals=goals_a,
                result=result_value,
                id=selection_id
            )
            updated_selections.append(updated_sel)
        
        tr.selections = updated_selections
        
        # DEBUG: Log co se vrací
        if tr.selections:
            for sel in tr.selections[:2]:  # První 2 selections
                print(f"  - {sel.home_team} vs {sel.away_team}: result={sel.result}, goals={sel.home_goals}-{sel.away_goals}, id={sel.id}")
        
        result_list.append(tr)

    return result_list


@app.get("/tickets/saved", response_model=list[TicketResponse])
def list_saved_tickets(user_id: int = Depends(get_current_user_id)):
    """user_id appka bere VÝHRADNĚ z přihlašovacího tokenu — nikdy ne z
    parametru v URL, jinak by si kdokoli mohl jen změnit číslo v adrese
    a prohlížet si cizí tikety."""
    return _list_saved_tickets_for_user(user_id)


@app.get("/tickets/sync")
def sync_saved_tickets(user_id: int = Depends(get_current_user_id)):
    """
    Stejná data jako GET /tickets/saved, jen zabalená do {"tickets":
    [...]}. Appka (HistoryView na frontendu) tohle volá PŘEDNOSTNĚ a na
    /tickets/saved padá jen jako fallback při chybě — appka ale roky
    tenhle endpoint vůbec neměla implementovaný (žádná zmínka nikde v
    backendu), takže appka fallback spouštěla úplně pokaždé, zbytečně.
    """
    tickets = _list_saved_tickets_for_user(user_id)
    return {"tickets": tickets}


class StakeRequest(BaseModel):
    stake_amount: float
    odds: float


@app.post("/tickets/{ticket_id}/stake")
def set_ticket_stake(ticket_id: int, req: StakeRequest, user_id: int = Depends(get_current_user_id)):
    """
    Appka sem zapíše, co jsi REÁLNĚ vsadil — vlastní kurz (může se od
    generování lišit, kurzy se hýbou) a vlastní částku. Appka nijak
    nevynucuje, že se musí vsadit přesně doporučený Kelly vklad — jen
    zaznamená, co se skutečně stalo, aby z toho šlo počítat reálný
    zisk/ztrátu (viz GET /tickets/real-results).
    """
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")
    repo.set_actual_stake(ticket_id, req.stake_amount, req.odds)
    return {"ticket_id": ticket_id, "status": "stake_recorded"}


@app.get("/tickets/real-results")
def get_real_results(user_id: int = Depends(get_current_user_id)):
    """
    Souhrn SKUTEČNĚ vsazených tiketů (těch, kde jsi appce řekl, co a za
    kolik jsi vsadil) — celková výše vkladů, čistý zisk/ztráta, ROI v %,
    win rate, a časová řada pro graf kumulativního zisku/ztráty. Appka
    to počítá jen z TVÝCH tiketů, ne ze všech v appce.
    """
    return repo.get_real_results_report(user_id)


SETTLE_LEG_WORKERS = 8


def _settle_one_leg(provider, i: int, selection, selection_id: Optional[int]) -> Optional[bool]:
    """Vyhodnotí JEDNU nohu tiketu — appka tohle volá souběžně pro
    všechny nohy najednou (viz _try_settle_ticket), protože jednotlivá
    volání na sobě nijak nezávisí a čekají hlavně na síť, ne na appku."""
    # Zápas, co ještě ani nezačal, JISTĚ neskončil — appka na to nemusí
    # volat externí API. Bez tyhle zkratky appka při každém otevření
    # Historie volala API pro KAŽDOU nohu KAŽDÉHO nevyřešeného tiketu,
    # i když většina zápasů ještě ani nekopla do míče — reálně to
    # dělalo Historii zbytečně pomalou.
    try:
        kickoff_str = f"{selection.kickoff_date}T{selection.kickoff_time}:00Z"
        kickoff_dt = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
        if kickoff_dt > datetime.now(timezone.utc):
            if selection_id is not None:
                db.update_selection_result(selection_id, "pending")
            return None
    except (ValueError, AttributeError, TypeError):
        pass  # kickoff appka nedokázala rozparsovat — bezpečně pokračuj na API dotaz

    try:
        raw_result = provider.get_fixture_result(selection.match_id)
        result = data_provider.adapt_fixture_result(raw_result)
        print(f"  [{i}] {selection.home_team} vs {selection.away_team}: finished={result.get('is_finished')}, goals={result.get('home_goals')}-{result.get('away_goals')}")
    except Exception as e:
        print(f"  [{i}] API ERROR: {str(e)}")
        if selection_id is not None:
            db.update_selection_result(selection_id, "pending")
            print(f"      → saved pending (API error) id={selection_id}")
        return None

    if not result["is_finished"] or result["home_goals"] is None:
        print(f"      → Match NOT finished, saving pending")
        if selection_id is not None:
            db.update_selection_result(selection_id, "pending")
            print(f"      → saved pending id={selection_id}")
        return None

    total_cards = None
    if selection.market_type == MarketType.OVER_CARDS:
        # Appka statistiky (počet karet) tahá jen tady, ne pro každou
        # nohu — jiné trhy je nepotřebují a appka nechce plýtvat API
        # budgetem navíc.
        try:
            stats = provider.get_fixture_statistics(selection.match_id)
            total_cards = data_provider.adapt_fixture_card_count(stats)
            print(f"  [{i}] karty: {total_cards}")
        except Exception as e:
            print(f"  [{i}] karty API ERROR: {str(e)}")

    outcome = evaluate_selection_outcome(selection, result["home_goals"], result["away_goals"], total_cards)
    print(f"      → Match finished, outcome={outcome}")
    if selection_id is not None:
        result_str = "won" if outcome is True else "lost" if outcome is False else "pending"
        db.update_selection_result(selection_id, result_str)
        print(f"      → saved {result_str} id={selection_id}")
    return outcome


def _try_settle_ticket(provider, ticket: Ticket, selection_ids: list[int] = None) -> Optional[str]:
    """
    Zkusí vyhodnotit JEDEN tiket podle aktuálních/finálních výsledků
    zápasů — klasická parlay logika: JEDNA prohraná noha = celý tiket
    prohraný, i kdyby appka ostatní nohy ještě nedokázala vyhodnotit
    (zápas neskončil / trh appka neumí vyhodnotit čistě ze skóre — karty,
    tenis, basketbal). Tiket appka vrátí jako vyhraný jen tehdy, když
    VŠECHNY nohy potvrzeně vyhrály. Vrací nový status ("won"/"lost"),
    nebo None, pokud zůstává nejasný (appka ho nemá měnit).

    selection_ids: seznam ID selectionů z DB — pokud je předán, uloží výsledky pro každý
    """
    with ThreadPoolExecutor(max_workers=SETTLE_LEG_WORKERS) as executor:
        futures = [
            executor.submit(_settle_one_leg, provider, i, selection, selection_ids[i] if selection_ids and i < len(selection_ids) else None)
            for i, selection in enumerate(ticket.selections)
        ]
        leg_results = [f.result() for f in futures]

    if any(r is False for r in leg_results):
        return "lost"
    if leg_results and all(r is True for r in leg_results):
        return "won"
    return None  # zápas(y) ještě neskončily, nebo appka trh neumí vyhodnotit čistě ze skóre


@app.get("/tickets/track-record")
def get_ticket_track_record(user_id: int = Depends(get_current_user_id)):
    """Agregovaná úspěšnost TVÝCH uložených tiketů — kolik vyhrálo, kolik ne, win rate."""
    return repo.get_ticket_track_record(user_id)


@app.get("/tickets/calibration")
def get_ticket_calibration(user_id: int = Depends(get_current_user_id)):
    """
    Je appka u tvých tiketů dobře kalibrovaná, nebo jen přestřeluje?
    Rozdělí vyhodnocené tikety do košů po 10 % podle vlastní predikce
    a porovná s tím, co se skutečně stalo — plus Brier score jako jedno
    souhrnné číslo (0 = perfektní, 0.25 = appka neumí o nic víc než
    hodit minci).
    """
    return repo.get_calibration_report(user_id)


@app.get("/tickets/roi")
def get_ticket_roi(user_id: int = Depends(get_current_user_id)):
    """
    Vyplatilo by se to reálně v penězích? Simulovaný výdělek/ztráta podle
    doporučeného (Kelly) vkladu na tvých tiketech, srovnaný s tím, kdyby
    sázel pořád rovných {Repo.FLAT_STAKE_PCT} % bez ohledu na doporučení —
    plus rozpad úspěšnosti podle typu trhu.
    """
    return repo.get_roi_report(user_id)



class TicketGenerateRequestWithExclude(BaseModel):
    risk_level: int = Field(ge=0, le=60)  # appka BOOST (risk_level > 60) už nenabízí
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=5)
    exclude_match_ids: list[int] = []


@app.post("/tickets/replace-selection")
def replace_selection(req: TicketGenerateRequestWithExclude, user_id: int = Depends(get_current_user_id)):
    """Vygeneruje nový tiket bez vyloučených zápasů — používá se po kliknutí ✕ u výběru.

    Appce tu dřív chyběly STEJNÉ pojistky co u /tickets/generate —
    _require_generation_enabled (appka tenhle endpoint appce nechala
    otevřený i kdyby appka generování jinak zamkla), _filter_future_matches
    (appka mohla nabídnout náhradu za zápas, co už kopl nebo kopne za
    pár minut), _filter_within_days (appka mohla vrátit náhradu mimo
    zvolené okno, potichu, bez horizon_note) a vyloučení zápasů z JIŽ
    ULOŽENÝCH tiketů (frontend appce posílá jen zápasy odklinuté ✕
    v týhle editační session, ne celou historii — appka tak uměla
    nabídnout náhradu za zápas, co uživatel má vsazený v jiném, dřív
    uloženém tiketu). Appka to appce doplňuje, ať nemá různě bezpečná
    chování pro v podstatě stejnou operaci (generování tiketu)."""
    _require_generation_enabled(user_id)
    exclude_ids = set(req.exclude_match_ids) | set(repo.get_all_saved_match_ids(user_id)) | set(repo.get_last_batch(user_id))
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    matches = [m for m in matches if m.match_id not in exclude_ids]
    matches = _filter_future_matches(matches, buffer_minutes=5)
    matches = _filter_within_days(matches, req.time_frame_days)
    result = ticket_generator.generate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
        pool_filter=_pool_filter_for_risk(req.risk_level),
    )
    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=None,
    )


@app.delete("/tickets/{ticket_id}")
def delete_ticket(ticket_id: int, user_id: int = Depends(get_current_user_id)):
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")
    db.delete_ticket(ticket_id)
    return {"status": "deleted"}


@app.delete("/tickets/{ticket_id}/selections/{selection_index}")
def delete_selection(ticket_id: int, selection_index: int, user_id: int = Depends(get_current_user_id)):
    """Smaže jeden výběr ze tiketu a přepočítá kurz"""
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")
    
    # Smaž selection z DB - vrací True pokud byl smazán celý tiket
    ticket_deleted = db.delete_selection(ticket_id, selection_index)
    
    if ticket_deleted:
        # Poslední selection byl smazán - tiket už neexistuje
        return {"ticket_id": ticket_id, "status": "deleted", "message": "Poslední výběr byl smazán - tiket odstraněn"}
    
    # Vrať updated tiket
    saved_rows = repo.get_saved_tickets(user_id)
    for row in saved_rows:
        if row["ticket_id"] == ticket_id:
            created_at_str = None
            if row.get("created_at"):
                created_at_dt = row["created_at"]
                if hasattr(created_at_dt, 'isoformat'):
                    created_at_str = created_at_dt.isoformat()
                else:
                    created_at_str = str(created_at_dt)
            
            tr = TicketResponse.from_domain(
                row["ticket"], row["ticket_id"], row["status"], row["live_alert"],
                row["actual_stake_amount"], row["actual_odds"], row["actual_profit_loss"],
                created_at=created_at_str
            )
            return tr
    
    raise HTTPException(status_code=404, detail="Tiket nenalezen po smazání")


@app.delete("/history/clear-all")
def clear_all_history(user_id: int = Depends(get_current_user_id)):
    """Smaže všechny tikety pro aktuálního uživatele"""
    saved_tickets = repo.get_saved_tickets(user_id)
    for row in saved_tickets:
        db.delete_ticket(row["ticket_id"])
    return {"status": "all history deleted", "count": len(saved_tickets)}


class TicketResultRequest(BaseModel):
    status: str  # "won" nebo "lost"


@app.post("/tickets/{ticket_id}/result")
def set_ticket_result(ticket_id: int, req: TicketResultRequest, user_id: int = Depends(get_current_user_id)):
    """Manuální označení výsledku tiketu — won nebo lost."""
    if req.status not in ("won", "lost"):
        raise HTTPException(status_code=400, detail="Status musí být 'won' nebo 'lost'")
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")
    db.update_ticket_status(ticket_id, req.status)
    return {"ticket_id": ticket_id, "status": req.status}


class SelectionOddsRequest(BaseModel):
    odds: float


class SelectionResultRequest(BaseModel):
    result: str  # "won" nebo "lost"


@app.post("/selections/{selection_id}/odds")
def update_selection_odds(selection_id: int, req: SelectionOddsRequest, user_id: int = Depends(get_current_user_id)):
    """Přepis kurzu jednoho výběru v uloženém tiketu."""
    owner_id = db.get_selection_owner(selection_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Výběr nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle výběr není tvůj")
    db.update_selection_odds(selection_id, req.odds)
    return {"selection_id": selection_id, "odds": req.odds}


@app.post("/selections/{selection_id}/result")
def update_selection_result(selection_id: int, req: SelectionResultRequest, user_id: int = Depends(get_current_user_id)):
    """Manuální označení výsledku jednoho výběru."""
    if req.result not in ("won", "lost", "pending"):
        raise HTTPException(status_code=400, detail="Result musí být 'won', 'lost' nebo 'pending'")
    owner_id = db.get_selection_owner(selection_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Výběr nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle výběr není tvůj")
    db.update_selection_result(selection_id, req.result)
    return {"selection_id": selection_id, "result": req.result}


@app.post("/tickets/settle")
def settle_tickets(user_id: int = Depends(get_current_user_id)):
    """
    Zkusí dosettlovat pending tikety uživatele podle aktuálních výsledků
    zápasů z API-Football. Appka tohle dřív dělala synchronně přímo
    uvnitř GET /tickets/saved (viz komentář tam) — teď appka Historii
    vrací okamžitě z DB a tohle volá zvlášť/na pozadí, aby se stav
    postupně dohnal na živé výsledky, aniž by blokoval načtení stránky.
    Stejná paralelní logika, jakou dřív používalo jen /tickets/saved
    (viz _try_settle_ticket) — appka ji dřív duplikovala tady zvlášť,
    pomalejší a bez paralelizace.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    pending_rows = [row for row in repo.get_saved_tickets(user_id) if row["status"] == "pending"]

    def _settle_row(row):
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        return row["ticket_id"], _try_settle_ticket(provider, row["ticket"], selection_ids)

    settled = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        for ticket_id, new_status in executor.map(_settle_row, pending_rows):
            if new_status is not None:
                repo.set_ticket_status(ticket_id, new_status)
                repo.set_live_alert(ticket_id, None)
                settled += 1

    return {"settled": settled, "checked": len(pending_rows)}


@app.delete("/admin/cache")
def clear_cache(user_id: int = Depends(get_current_user_id)):
    """Vymaže celou API cache — použij po nasazení oprav formátu dat."""
    count = db.cache_clear_all()
    return {"deleted": count, "status": "cache cleared"}


@app.get("/admin/verify-results")
def verify_results(user_id: int = Depends(get_current_user_id)):
    """
    Diagnostický endpoint — appka NEEDITUJE nic v DB, jen zkontroluje.
    Projde všechny výběry napříč všemi uživateli, co appka označila jako
    'won'/'lost', a ke KAŽDÉMU dohledá u API-Football skutečné skóre
    zápasu — pak appka porovná, jestli evaluate_selection_outcome() na
    tom skóre dá STEJNÝ výsledek, jaký má appka uložený v DB. Používej
    při podezření, že appka nesprávně vyhodnocuje výhry/prohry (viz
    Historie a statistika) — místo ručního ověřování pár tiketů appka
    zkontroluje úplně všechny najednou.

    Zápasy appka mezi výběry sdílí (jeden fetch na match_id, ne na výběr),
    ale i tak je to dost API volání navíc — appka doporučuje spouštět
    jen občas, ne po každém vyhodnocení.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    rows = db.fetch_ticket_rows()

    match_result_cache: dict[int, dict] = {}
    checked = 0
    unverifiable = 0
    mismatches = []

    for row in rows:
        ticket = row["ticket"]
        sel_dicts = row.get("selections", [])
        for sel_dict, sel_obj in zip(sel_dicts, ticket.selections):
            claimed = sel_dict.get("result", "pending")
            if claimed not in ("won", "lost"):
                continue

            match_id = sel_dict["match_id"]
            if match_id not in match_result_cache:
                try:
                    raw = provider.get_fixture_result(str(match_id))
                    match_result_cache[match_id] = data_provider.adapt_fixture_result(raw)
                except Exception as e:
                    match_result_cache[match_id] = {"is_finished": False, "error": str(e)}

            real = match_result_cache[match_id]
            if not real.get("is_finished") or real.get("home_goals") is None:
                unverifiable += 1  # appka zápas nedohledala nebo API selhalo — nelze ověřit
                continue

            actual_outcome = evaluate_selection_outcome(sel_obj, real["home_goals"], real["away_goals"])
            if actual_outcome is None:
                unverifiable += 1  # trh appka neumí vyhodnotit čistě ze skóre (karty apod.)
                continue

            checked += 1
            actual_str = "won" if actual_outcome else "lost"
            if actual_str != claimed:
                mismatches.append({
                    "ticket_id": row["ticket_id"],
                    "match": f"{sel_obj.home_team} vs {sel_obj.away_team}",
                    "market": sel_obj.market_type.value,
                    "selection": sel_obj.selection,
                    "appka_tvrdi": claimed,
                    "skutecny_vysledek": actual_str,
                    "skutecne_skore": f"{real['home_goals']}:{real['away_goals']}",
                })

    return {
        "zkontrolovano_vyberu": checked,
        "nelze_overit": unverifiable,
        "pocet_neshod": len(mismatches),
        "neshody": mismatches,
    }


# =====================================================================
# Automatické denní generování tiketů (cron) — appka denně v 9:00 SEČ/SELČ
# vygeneruje 1 tiket — hlavně kratky, jen každý třetí den appka navíc
# zkusí stredni (pošle ho, jen když má hodnotu) — uloží ho do historie
# zadaného účtu a pošle ho jako obrázek do Telegramu.
# Volá se z vnějšku (naplánovaná úloha), ne appka sama ze sebe — proto
# appka autorizaci řeší sdíleným tajným klíčem (ADMIN_TASK_KEY), ne
# přihlašovacím tokenem konkrétního uživatele.
# =====================================================================
DAILY_TICKETS_MARKETS = [MarketType.MATCH_WINNER, MarketType.OVER_GOALS, MarketType.UNDER_GOALS, MarketType.BTTS]
DAILY_TICKETS_SPORTS = [Sport.FOOTBALL]
# Kč — appka tohle zaznamená jako "reálně vsazeno" u KAŽDÉHO auto-generovaného
# tiketu. Rozpětí (ne pevná částka) appka volí náhodně, ať výkladní skříň
# (viz /showcase/tickets) vypadá jako opravdové sázení různých lidí, ne jako
# jeden bot se stále stejnou částkou.
DAILY_TICKETS_STAKE_CHOICES = [200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000]


def _generate_one_ticket_for_cron(
    user_id: int, risk_level: int, sports: list[Sport], market_types: list[MarketType], time_frame_days: int,
    max_widen_days: int = 3,
) -> Optional[Ticket]:
    """Stejná logika jako /tickets/generate (fetch → vyluč už použité →
    vyfiltruj minulé zápasy → fallback na širší horizont, pokud je málo
    zápasů NEBO se z nich nepovede sestavit tiket), jen bez závislosti na
    přihlášeném uživateli z requestu. max_widen_days řídí, o kolik dní appka
    smí couvnout do širšího okna, když je úzké okno prázdné/nepoužitelné —
    0 znamená appka couvnout vůbec nesmí (radši nic než starý/vzdálený zápas,
    viz DAILY_TICKETS_USER_ID, kde má appka posílat jen zápasy na dnes/zítra)."""
    exclude_ids = repo.get_all_saved_match_ids(user_id)

    # Appka stáhne a obohatí širší okno JEDNOU (viz stejná poznámka u
    # /tickets/generate) — užší okno je jeho podmnožina.
    all_wider_matches = _fetch_candidate_matches(sports, time_frame_days + max_widen_days)
    all_wider_matches = [m for m in all_wider_matches if m.match_id not in exclude_ids]
    all_wider_matches = _filter_future_matches(all_wider_matches, buffer_minutes=5)

    matches = _filter_within_days(all_wider_matches, time_frame_days)
    if len(matches) < 3 and max_widen_days > 0:
        matches = all_wider_matches

    result = ticket_generator.generate(
        matches, risk_level, sports, market_types, time_frame_days,
        pool_filter=_pool_filter_for_risk(risk_level),
    )

    if result["safe"] is None:
        result = ticket_generator.generate(
            all_wider_matches, risk_level, sports, market_types, time_frame_days,
            pool_filter=_pool_filter_for_risk(risk_level),
        )

    return result["safe"]


def _ticket_to_telegram_dict(ticket: Ticket, ticket_id: int) -> dict:
    return {
        "ticket_id": ticket_id,
        "ticket_type": ticket.ticket_type,
        "total_odds": ticket.total_odds,
        "selections": [
            {
                "home_team": s.home_team, "away_team": s.away_team,
                "league": s.league, "kickoff_date": s.kickoff_date, "kickoff_time": s.kickoff_time,
                "odds": s.odds, "probability": s.probability, "selection": s.selection,
                "market_type": s.market_type.value if hasattr(s.market_type, "value") else s.market_type,
            }
            for s in ticket.selections
        ],
    }


# Appka historii měla 5x duplicitní pár tiketů (stejný účet, stejný typ,
# STEJNÉ zápasy) — vždy vznikly ze dvou volání téhož admin/*-daily-tickets
# endpointu pár vteřin od sebe (ruční test spuštěný podruhé, než appka
# stihla dokončit první běh). Uvicorn appka pouští bez --workers (viz
# render.yaml — jeden proces), ale FastAPI SYNCHRONNÍ endpointy i tak
# pouští na vlákně, takže dvě souběžná volání klidně obě přečtou "zatím
# nic dnes uloženo" DŘÍV, než první stihne cokoli uložit. Prostý zámek
# v paměti (per proces, žádná DB tabulka navíc) tohle appce stačí vyřešit
# — druhé souběžné volání appka rovnou odmítne, místo aby duplikovalo.
_GENERATION_LOCKS = {
    "daily_tickets": threading.Lock(),
    "transparency_daily_tickets": threading.Lock(),
    "test3_daily_tickets": threading.Lock(),
    "dvoves_daily_tickets": threading.Lock(),
    "personal_tracking_daily_tickets": threading.Lock(),
}


@app.post("/admin/daily-tickets")
def run_daily_tickets(request: Request):
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if not _GENERATION_LOCKS["daily_tickets"].acquire(blocking=False):
        return {"status": "already_running", "detail": "Jiné volání /admin/daily-tickets už běží, tohle appka přeskočila."}

    try:
        target_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
        if not target_user_id_raw:
            raise HTTPException(status_code=500, detail="DAILY_TICKETS_USER_ID není nastavené")
        target_user_id = int(target_user_id_raw)

        # Appka nejdřív zkusí dosettlovat staré 'pending' tikety na tomhle
        # účtu — nikdo se na něj nepřihlašuje (je to appky vlastní účet pro
        # denní automatiku a výkladní skříň /showcase/tickets), takže bez
        # tohohle kroku by settlement (viz jinak /tickets/saved) nikdy
        # neproběhl a tikety by navěky zůstaly "nevyhodnocené".
        provider = data_provider.get_provider(Sport.FOOTBALL)
        pending_rows = [row for row in repo.get_saved_tickets(target_user_id) if row["status"] == "pending"]
        settled_count = 0
        for row in pending_rows:
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
                settled_count += 1

        # DAILY_TICKETS_USER_ID je zdroj pro PLACENÝ Telegram kanál
        # (_todays_client_picks/client-tickets-send) — appka posílá JEDEN
        # tiket denně, žádnou "výkladní skříň" navíc (na tu appka má
        # samostatný účet, viz TEST3_USER_ID níže). BOOST appka přestala
        # nabízet úplně — jeho nízká úspěšnost (viz historie) mu
        # neodpovídala kvalitativní laťce, co appka drží u kratky/stredni.
        #
        # Appka posílá hlavně kratky. Jen každý třetí den appka navíc
        # zkusí nejdřív sestavit stredni — a pošle ho MÍSTO kratky jen
        # tehdy, když appka pro něj najde skutečnou hodnotu (kladný edge
        # vůči tržnímu kurzu appka vyžaduje už uvnitř
        # TicketGenerator._build_ticket, require_positive_edge=True — bez
        # toho appka žádný stredni tiket nevrátí, viz probability_model).
        # Když se stredni ten den nepovede sestavit (žádná hodnota nebo
        # málo zápasů), appka spadne na kratky stejně jako každý jiný den.
        #
        # Okno je 2 dny (dnešek/zítřek), NE 1 — appka tak nejdřív zkusí
        # sestavit tiket na 70 % z obou dní najednou, a teprve když ani tak
        # 70 % nenajde, spadne v rámci TicketGenerator.generate() na 65% dno
        # (viz FALLBACK_THRESHOLDS). Při úzkém 1denním okně appka dřív
        # spadla na 65 % hned, i když by 70 % bylo k mání zítra — appka to
        # bez rozšíření okna nikdy nezjistila. max_widen_days zůstává 0 —
        # 2 dny je appce nastavený strop, dál appka couvat nesmí (odběratel
        # má dostat tip na dnešek/zítřek, ne na zápas za týden).
        today_prague = datetime.now(ZoneInfo("Europe/Prague"))
        today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)

        results = []
        generated_today: list[tuple[Ticket, int]] = []
        already_today = (
            db.count_tickets_since(target_user_id, "kratky", today_start_utc_naive)
            + db.count_tickets_since(target_user_id, "stredni", today_start_utc_naive)
        )
        if already_today > 0:
            results.append({"type": "daily_pick", "status": "already_generated_today", "count": already_today})
        else:
            is_stredni_day = today_prague.toordinal() % 3 == 0
            candidates = (("stredni", 50), ("kratky", 20)) if is_stredni_day else (("kratky", 20),)

            ticket, label = None, None
            for candidate_label, risk_level in candidates:
                try:
                    ticket = _generate_one_ticket_for_cron(
                        target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, 2,
                        max_widen_days=0,
                    )
                except Exception as e:
                    print(f"[daily-tickets] {candidate_label}: generování selhalo: {e}")
                    ticket = None
                if ticket is not None:
                    label = candidate_label
                    break

            if ticket is None:
                results.append({"type": "daily_pick", "status": "failed_to_generate"})
            else:
                ticket_id = repo.save_ticket(target_user_id, ticket)
                stake = random.choice(DAILY_TICKETS_STAKE_CHOICES)
                repo.set_actual_stake(ticket_id, stake, ticket.total_odds)
                generated_today.append((ticket, ticket_id))

                telegram_status = "skipped"
                if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                    try:
                        ticket_telegram.send_ticket_to_telegram(_ticket_to_telegram_dict(ticket, ticket_id))
                        telegram_status = "sent"
                    except Exception as e:
                        telegram_status = f"error: {e}"

                results.append({"type": label, "status": "saved", "ticket_id": ticket_id, "stake": stake, "telegram": telegram_status})

        # Appka navíc denně pošle výběr TOP 4 (podle nejvyšší kombinované
        # pravděpodobnosti ze všech dnes vygenerovaných) na druhý, samostatně
        # nastavený Telegram chat (TELEGRAM_CHAT_ID_WIFE) — appka posílá jen
        # kopii nejlepších tiketů, nic v appce se kvůli tomu jinak nemění.
        wife_chat_id = os.environ.get("TELEGRAM_CHAT_ID_WIFE")
        if wife_chat_id and os.environ.get("TELEGRAM_BOT_TOKEN") and generated_today:
            # Čistě podle kombinované pravděpodobnosti by "stredni" typ (víc
            # nohou, nižší součin) skoro nikdy neprošel proti "kratky" — appka
            # proto pro ni vždycky rezervuje aspoň 1 místo na nejlepší
            # dostupný stredni tiket, zbytek dorovná nejlepšími ze všech typů.
            top4 = []
            stredni_candidates = [t for t in generated_today if t[0].ticket_type == "stredni"]
            if stredni_candidates:
                top4.append(max(stredni_candidates, key=lambda t: t[0].combined_probability))
            remaining = [t for t in generated_today if t not in top4]
            remaining.sort(key=lambda t: t[0].combined_probability, reverse=True)
            top4.extend(remaining[:4 - len(top4)])

            for ticket, ticket_id in top4:
                try:
                    ticket_telegram.send_ticket_to_telegram(_ticket_to_telegram_dict(ticket, ticket_id), chat_id=wife_chat_id)
                    results.append({"type": "top4_wife", "status": "sent", "ticket_id": ticket_id})
                except Exception as e:
                    results.append({"type": "top4_wife", "status": f"error: {e}", "ticket_id": ticket_id})

        # Odběratelům z Telegram webhooku appka NEPOSÍLÁ nic automaticky —
        # per rozhodnutí appka nejdřív čeká na ruční schválení (viz
        # /admin/client-tickets-preview a /admin/client-tickets-send níže),
        # ať jde vždycky zkontrolovat, co se klientovi pošle, PŘED odesláním.

        return {"date": today_prague.isoformat(), "settled": settled_count, "results": results}
    finally:
        _GENERATION_LOCKS["daily_tickets"].release()


TRANSPARENCY_STAKE = 2000.0


@app.post("/admin/transparency-daily-tickets")
def run_transparency_daily_tickets(request: Request):
    """
    Appka na samostatném, veřejně čitelném účtu (TRANSPARENCY_USER_ID)
    denně vygeneruje 2 kratky + 1 stredni tiket, na každý appka
    automaticky vsadí pevných 2000 Kč — appka tenhle účet nikdy nemaskuje
    ani neupravuje, jde čistě o transparentní ukázku appčina výkonu (viz
    GET /public/transparency). Odděleno od DAILY_TICKETS_USER_ID (appky
    vlastní účet pro Telegram/showcase) — appka tenhle nový účet nemíchá
    s tím starým. Každý vygenerovaný tiket appka navíc pošle na appčin
    vlastní Telegram (TELEGRAM_CHAT_ID), stejně jako u run_daily_tickets.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if not _GENERATION_LOCKS["transparency_daily_tickets"].acquire(blocking=False):
        return {"status": "already_running", "detail": "Jiné volání /admin/transparency-daily-tickets už běží, tohle appka přeskočila."}

    try:
        target_user_id_raw = os.environ.get("TRANSPARENCY_USER_ID")
        if not target_user_id_raw:
            raise HTTPException(status_code=500, detail="TRANSPARENCY_USER_ID není nastavené")
        target_user_id = int(target_user_id_raw)

        # Nikdo se na tenhle účet nepřihlašuje, takže appka musí dosettlovat
        # stará pending sama — jinak by se tikety navěky nevyhodnotily
        # (viz stejná poznámka u run_daily_tickets výše).
        provider = data_provider.get_provider(Sport.FOOTBALL)
        pending_rows = [row for row in repo.get_saved_tickets(target_user_id) if row["status"] == "pending"]
        settled_count = 0
        for row in pending_rows:
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
                settled_count += 1

        today_prague = datetime.now(ZoneInfo("Europe/Prague"))
        today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        # Horizont rozšířen z 2 na 4 dny stejně jako u run_daily_tickets výš —
        # viz komentář tam.
        plan = [("kratky", 20, 4, 2), ("stredni", 50, 4, 1)]

        results = []
        for label, risk_level, days, target_count in plan:
            already_today = db.count_tickets_since(target_user_id, label, today_start_utc_naive)
            to_generate = target_count - already_today
            if to_generate <= 0:
                results.append({"type": label, "status": "already_generated_today", "count": already_today})
                continue

            for _ in range(to_generate):
                try:
                    ticket = _generate_one_ticket_for_cron(
                        target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, days,
                    )
                except Exception as e:
                    print(f"[transparency-daily-tickets] {label}: generování selhalo: {e}")
                    results.append({"type": label, "status": "generation_error", "error": str(e)})
                    break
                if ticket is None:
                    results.append({"type": label, "status": "failed_to_generate"})
                    break

                ticket_id = repo.save_ticket(target_user_id, ticket)
                repo.set_actual_stake(ticket_id, TRANSPARENCY_STAKE, ticket.total_odds)

                telegram_status = "skipped"
                if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                    try:
                        ticket_telegram.send_ticket_to_telegram(_ticket_to_telegram_dict(ticket, ticket_id))
                        telegram_status = "sent"
                    except Exception as e:
                        telegram_status = f"error: {e}"

                results.append({"type": label, "status": "saved", "ticket_id": ticket_id, "stake": TRANSPARENCY_STAKE, "telegram": telegram_status})

        return {"date": today_prague.isoformat(), "settled": settled_count, "results": results}
    finally:
        _GENERATION_LOCKS["transparency_daily_tickets"].release()


DVOVES_STAKE = 2000.0
DVOVES_EMAIL = "d.voves@seznam.cz"


@app.post("/admin/dvoves-daily-tickets")
def run_dvoves_daily_tickets(request: Request):
    """
    Appka na testovacím účtu d.voves@seznam.cz denně vygeneruje 2 kratky +
    1 stredni tiket, na každý appka automaticky vsadí pevných 2000 Kč —
    stejný plán jako u transparentního účtu (run_transparency_daily_tickets),
    jen na jiném účtu a bez env var (appka ho hledá přes e-mail, protože
    tohle je běžný registrovaný uživatel appky, ne appčin vlastní systémový
    účet s DVOVES_USER_ID). Generování jde mimo tokenový systém stejně jako
    u appčiných ostatních cronových účtů.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if not _GENERATION_LOCKS["dvoves_daily_tickets"].acquire(blocking=False):
        return {"status": "already_running", "detail": "Jiné volání /admin/dvoves-daily-tickets už běží, tohle appka přeskočila."}

    try:
        target_user = db.get_user_by_email(DVOVES_EMAIL)
        if not target_user:
            raise HTTPException(status_code=500, detail=f"Účet {DVOVES_EMAIL} v appce neexistuje")
        target_user_id = target_user["id"]

        provider = data_provider.get_provider(Sport.FOOTBALL)
        pending_rows = [row for row in repo.get_saved_tickets(target_user_id) if row["status"] == "pending"]
        settled_count = 0
        for row in pending_rows:
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
                settled_count += 1

        today_prague = datetime.now(ZoneInfo("Europe/Prague"))
        today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        plan = [("kratky", 20, 4, 2), ("stredni", 50, 4, 1)]

        results = []
        for label, risk_level, days, target_count in plan:
            already_today = db.count_tickets_since(target_user_id, label, today_start_utc_naive)
            to_generate = target_count - already_today
            if to_generate <= 0:
                results.append({"type": label, "status": "already_generated_today", "count": already_today})
                continue

            for _ in range(to_generate):
                try:
                    ticket = _generate_one_ticket_for_cron(
                        target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, days,
                    )
                except Exception as e:
                    print(f"[dvoves-daily-tickets] {label}: generování selhalo: {e}")
                    results.append({"type": label, "status": "generation_error", "error": str(e)})
                    break
                if ticket is None:
                    results.append({"type": label, "status": "failed_to_generate"})
                    break

                ticket_id = repo.save_ticket(target_user_id, ticket)
                repo.set_actual_stake(ticket_id, DVOVES_STAKE, ticket.total_odds)
                results.append({"type": label, "status": "saved", "ticket_id": ticket_id, "stake": DVOVES_STAKE})

        return {"date": today_prague.isoformat(), "settled": settled_count, "results": results}
    finally:
        _GENERATION_LOCKS["dvoves_daily_tickets"].release()


TEST3_STAKE = 1000.0


@app.post("/admin/test3-daily-tickets")
def run_test3_daily_tickets(request: Request):
    """
    Appka na účtu TEST3_USER_ID (appky vlastní testovací/kontrolní účet)
    denně vygeneruje 2 kratky + 2 stredni tikety, na každý appka
    automaticky vsadí pevných 1000 Kč. Odděleno od DAILY_TICKETS_USER_ID
    (placený Telegram kanál, přesně 1 kratky + 1 stredni, žádný boost) i od
    TRANSPARENCY_USER_ID (veřejná appka /public/transparency) — appka
    tenhle účet nemíchá s žádným z nich.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if not _GENERATION_LOCKS["test3_daily_tickets"].acquire(blocking=False):
        return {"status": "already_running", "detail": "Jiné volání /admin/test3-daily-tickets už běží, tohle appka přeskočila."}

    try:
        target_user_id_raw = os.environ.get("TEST3_USER_ID")
        if not target_user_id_raw:
            raise HTTPException(status_code=500, detail="TEST3_USER_ID není nastavené")
        target_user_id = int(target_user_id_raw)

        provider = data_provider.get_provider(Sport.FOOTBALL)
        pending_rows = [row for row in repo.get_saved_tickets(target_user_id) if row["status"] == "pending"]
        settled_count = 0
        for row in pending_rows:
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
                settled_count += 1

        today_prague = datetime.now(ZoneInfo("Europe/Prague"))
        today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        plan = [("kratky", 20, 4, 2), ("stredni", 50, 4, 2)]

        results = []
        for label, risk_level, days, target_count in plan:
            already_today = db.count_tickets_since(target_user_id, label, today_start_utc_naive)
            to_generate = target_count - already_today
            if to_generate <= 0:
                results.append({"type": label, "status": "already_generated_today", "count": already_today})
                continue

            for _ in range(to_generate):
                try:
                    ticket = _generate_one_ticket_for_cron(
                        target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, days,
                    )
                except Exception as e:
                    print(f"[test3-daily-tickets] {label}: generování selhalo: {e}")
                    results.append({"type": label, "status": "generation_error", "error": str(e)})
                    break
                if ticket is None:
                    results.append({"type": label, "status": "failed_to_generate"})
                    break

                ticket_id = repo.save_ticket(target_user_id, ticket)
                repo.set_actual_stake(ticket_id, TEST3_STAKE, ticket.total_odds)
                results.append({"type": label, "status": "saved", "ticket_id": ticket_id, "stake": TEST3_STAKE})

        return {"date": today_prague.isoformat(), "settled": settled_count, "results": results}
    finally:
        _GENERATION_LOCKS["test3_daily_tickets"].release()


PERSONAL_TRACKING_STAKE = 1000.0


@app.post("/admin/personal-tracking-daily-tickets")
def run_personal_tracking_daily_tickets(request: Request):
    """
    Appka na účtu PERSONAL_TRACKING_USER_ID (appkou majitele vlastní
    osobní sledovací účet, oddělený od placeného kanálu i od
    appky vlastních test/transparency účtů) denně vygeneruje 2 kratky +
    1 stredni, na každý appka vsadí pevných 1000 Kč a POŠLE MU JE i na
    Telegram (TELEGRAM_CHAT_ID) — na rozdíl od test3/transparency appka
    tohle posílá appce majiteli přímo, appka to nenechává jen tiše uložené.
    Když se appce nepovede vygenerovat všechny 3, appka o tom appce
    majiteli pošle zvlášť upozornění (viz /admin/alert), ne appka to jen
    tiše zaloguje.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if not _GENERATION_LOCKS["personal_tracking_daily_tickets"].acquire(blocking=False):
        return {"status": "already_running", "detail": "Jiné volání /admin/personal-tracking-daily-tickets už běží, tohle appka přeskočila."}

    try:
        target_user_id_raw = os.environ.get("PERSONAL_TRACKING_USER_ID")
        if not target_user_id_raw:
            raise HTTPException(status_code=500, detail="PERSONAL_TRACKING_USER_ID není nastavené")
        target_user_id = int(target_user_id_raw)

        provider = data_provider.get_provider(Sport.FOOTBALL)
        pending_rows = [row for row in repo.get_saved_tickets(target_user_id) if row["status"] == "pending"]
        settled_count = 0
        for row in pending_rows:
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
                settled_count += 1

        today_prague = datetime.now(ZoneInfo("Europe/Prague"))
        today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
        plan = [("kratky", 20, 2, 2), ("stredni", 50, 2, 1)]

        results = []
        saved_count = 0
        for label, risk_level, days, target_count in plan:
            already_today = db.count_tickets_since(target_user_id, label, today_start_utc_naive)
            to_generate = target_count - already_today
            saved_count += min(already_today, target_count)
            if to_generate <= 0:
                results.append({"type": label, "status": "already_generated_today", "count": already_today})
                continue

            for _ in range(to_generate):
                try:
                    ticket = _generate_one_ticket_for_cron(
                        target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, days,
                    )
                except Exception as e:
                    print(f"[personal-tracking-daily-tickets] {label}: generování selhalo: {e}")
                    results.append({"type": label, "status": "generation_error", "error": str(e)})
                    break
                if ticket is None:
                    results.append({"type": label, "status": "failed_to_generate"})
                    break

                ticket_id = repo.save_ticket(target_user_id, ticket)
                repo.set_actual_stake(ticket_id, PERSONAL_TRACKING_STAKE, ticket.total_odds)
                telegram_status = "skipped"
                if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
                    try:
                        ticket_telegram.send_ticket_to_telegram(_ticket_to_telegram_dict(ticket, ticket_id))
                        telegram_status = "sent"
                    except Exception as e:
                        telegram_status = f"error: {e}"
                results.append({
                    "type": label, "status": "saved", "ticket_id": ticket_id,
                    "stake": PERSONAL_TRACKING_STAKE, "telegram": telegram_status,
                })
                saved_count += 1

        # Appka chtěla 2 kratky + 1 stredni = 3 celkem. Ať appka pozná
        # "málo nebo žádné" hned, ne až se na to appka majitel sám zeptá.
        if saved_count < 3:
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if chat_id and bot_token:
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        data={
                            "chat_id": chat_id,
                            "text": (
                                f"⚠️ Osobní sledovací tikety dnes appka vygenerovala jen {saved_count}/3 "
                                f"(2 krátké + 1 střední). Zkontroluj /admin/personal-tracking-daily-tickets."
                            ),
                        },
                        timeout=15,
                    )
                except Exception as e:
                    print(f"[personal-tracking-daily-tickets] Upozornění na málo tiketů se nepodařilo poslat: {e}")

        return {"date": today_prague.isoformat(), "settled": settled_count, "saved_count": saved_count, "results": results}
    finally:
        _GENERATION_LOCKS["personal_tracking_daily_tickets"].release()


@app.post("/admin/transparency-backfill-results")
def transparency_backfill_results(request: Request):
    """
    Veřejný transparentní účet (TRANSPARENCY_USER_ID) měl desítky tiketů
    z období PŘED tím, než appka vůbec začala per-výběr výsledek
    (won/lost) ukládat vedle celkového statusu tiketu — appka jim tak
    neuměla ukázat, jestli prohra byla "o jednu nohu", nebo nevyšlo skoro
    nic (viz _selection_row v transparency_page.py). Stejná logika jako
    appce vlastní /admin/backfill-results (appka ji tam má vázanou na
    přihlášení), jen tady přes ADMIN_TASK_KEY — na tenhle účet se nikdo
    nepřihlašuje. Bezpečně opakovatelné, appka jen znovu dotáhne skóre a
    přepíše, co už má.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    target_user_id_raw = os.environ.get("TRANSPARENCY_USER_ID")
    if not target_user_id_raw:
        raise HTTPException(status_code=500, detail="TRANSPARENCY_USER_ID není nastavené")
    target_user_id = int(target_user_id_raw)

    provider = data_provider.get_provider(Sport.FOOTBALL)
    checked = 0
    status_changed = 0
    for row in repo.get_saved_tickets(target_user_id):
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        if not selection_ids:
            continue
        checked += 1
        new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
        if new_status is not None and row["status"] != new_status:
            repo.set_ticket_status(row["ticket_id"], new_status)
            status_changed += 1

    return {"tickets_checked": checked, "status_changed": status_changed}


def _ticket_units(total_odds: Optional[float], status: str) -> float:
    """
    Výsledek jednoho tiketu při jednotkovém vkladu: výhra vynese kurz
    mínus vsazená jednotka, prohra sebere celou jednotku. Appka na tomhle
    staví veřejně vykazovaná čísla místo korun — viz poznámka u
    profit_units v public_transparency.
    """
    if status == "won":
        return round((total_odds or 0) - 1.0, 2)
    return -1.0


@app.get("/public/transparency")
def public_transparency(limit: int = 100):
    """
    Zcela veřejný, bez přihlášení dostupný přehled appčina transparentního
    účtu (TRANSPARENCY_USER_ID) — appka ukazuje ÚPLNĚ VŠECHNY tikety, i
    prohrané (na rozdíl od /showcase/tickets, co ukazuje jen výhry) —
    smysl je transparentnost, ne reklama. Appka u tiketů, co ještě nejsou
    vyhodnocené (pending/live), ukazuje ZÁPASY hned (tým, soupeř, liga),
    ale market_type/selection/odds u každého výběru posílá jako None —
    samotnou sázku appka odhalí natrvalo, až tiket vyhodnotí. Appka tohle
    dělá, aby nikdo nemohl z appky "okopírovat" tip dřív, než appka sama
    rozhodne o výsledku, ale zápasy appka zveřejňuje předem a natrvalo,
    ať appka nemůže zpětně tvrdit něco jiného, než na co vsadila.
    """
    limit = max(1, min(limit, 200))
    target_user_id_raw = os.environ.get("TRANSPARENCY_USER_ID")
    if not target_user_id_raw:
        return {"tickets": [], "stats": None}
    target_user_id = int(target_user_id_raw)

    rows = repo.get_saved_tickets(target_user_id)
    rows.sort(key=lambda r: r.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    rows = rows[:limit]

    tickets = []
    for r in rows:
        ticket = r["ticket"]
        created_at = r.get("created_at")
        status = r["status"]
        resolved = status in ("won", "lost")
        entry = {
            "ticket_id": r["ticket_id"],
            "ticket_type": ticket.ticket_type,
            "status": status,
            "total_odds": ticket.total_odds,
            # Výsledek appka veřejně vykazuje v JEDNOTKÁCH, ne v korunách:
            # každý tiket = 1 jednotka vkladu. Uložené actual_stake_amount
            # jsou u automaticky generovaných tiketů náhodně přiřazené
            # částky (viz DAILY_TICKETS_STAKE_CHOICES), ne skutečně vsazené
            # peníze — publikovat je jako reálný vklad by bylo tvrzení,
            # které neodpovídá skutečnosti. Úspěšnost a ROI přitom zůstávají
            # nedotčené, protože jsou vlastností VÝBĚRŮ, ne velikosti sázky.
            "profit_units": _ticket_units(ticket.total_odds, status) if resolved else None,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "selection_count": len(ticket.selections),
        }
        if resolved:
            # r["selections"] appka natahuje samostatně (viz db.fetch_ticket_rows)
            # a obsahuje "result" (won/lost/pending) PER VÝBĚR — na rozdíl od
            # ticket.selections (Ticket objekt), kde tohle pole není. Appka
            # veřejně chce ukázat, jestli prohra tiketu byla "o jednu nohu",
            # nebo jestli nevyšlo skoro nic, takže appka obě sady spáruje
            # POZIČNĚ (obě appka staví ze stejného seznamu DB řádků, ve
            # stejném pořadí — viz db.fetch_ticket_rows).
            raw_selections = r.get("selections") or []
            entry["selections"] = [
                {
                    "home_team": s.home_team, "away_team": s.away_team,
                    "market_type": s.market_type.value if hasattr(s.market_type, "value") else s.market_type,
                    "selection": s.selection, "odds": s.odds,
                    "league": s.league, "country": s.country,
                    "result": raw_selections[i]["result"] if i < len(raw_selections) else None,
                }
                for i, s in enumerate(ticket.selections)
            ]
        else:
            # Appka teď zápas ukazuje HNED, i u nevyhodnoceného tiketu —
            # skrytá zůstává jen samotná sázka (trh, konkrétní výběr a
            # kurz té jedné nohy), dokud appka tiket nevyhodnotí. Appka
            # market_type/selection/odds schválně posílá jako None místo
            # toho, aby appka výběry vynechala úplně — díky tomu appka
            # DOPŘEDU a NATRVALO zveřejní, na jaké zápasy vsadila (nejde
            # zpětně dopsat), a zároveň nikdo neví PŘESNĚ na co, dokud
            # appka tiket sama neodhalí.
            entry["selections"] = [
                {
                    "home_team": s.home_team, "away_team": s.away_team,
                    "market_type": None, "selection": None, "odds": None,
                    "league": s.league, "country": s.country,
                    "result": None,
                }
                for s in ticket.selections
            ]
        tickets.append(entry)

    resolved_rows = [r for r in rows if r["status"] in ("won", "lost") and r["ticket"].total_odds]
    won_rows = [r for r in resolved_rows if r["status"] == "won"]

    # Křivka kumulativního zisku pro graf — appka ji staví CHRONOLOGICKY
    # (rows jsou seřazené od nejnovějšího), aby šla vykreslit zleva doprava.
    equity_curve = []
    cumulative = 0.0
    for r in sorted(resolved_rows, key=lambda x: x["created_at"]):
        cumulative += _ticket_units(r["ticket"].total_odds, r["status"])
        created_at = r["created_at"]
        equity_curve.append({
            "date": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "units": round(cumulative, 2),
        })

    stats = {
        "resolved_count": len(resolved_rows),
        "won_count": len(won_rows),
        "win_rate_pct": round(len(won_rows) / len(resolved_rows) * 100, 1) if resolved_rows else None,
        # Zisk i ROI při jednotkovém vkladu na každý tiket — ROI je tedy
        # přímo průměrný výnos na vsazenou jednotku.
        "units_profit": round(cumulative, 2),
        "roi_pct": round(cumulative / len(resolved_rows) * 100, 1) if resolved_rows else None,
    }
    return {"tickets": tickets, "stats": stats, "equity_curve": equity_curve}


@app.get("/transparentni-ucet")
def transparency_html():
    """
    Appka tenhle veřejný přehled tiketů přestala zobrazovat (rozhodnutí
    2026-08-01, appka teď vede prodejně přes hlavní stránku) — appka ale
    nechává starou adresu jako REDIRECT, ne 404, protože na ni pořád vede
    odkaz z fóra, Telegramu i z profilu na Instagramu, které appka
    zpětně nemůže opravit. /public/transparency (JSON) i appčino denní
    generování na TRANSPARENCY_USER_ID appka nechává běžet beze změny —
    z něj appka pořád tahá reálné vyhrané tikety do /showcase/tickets.
    """
    return RedirectResponse(url="https://apexsignal.cz/", status_code=302)


def _app_equivalent_monthly_kc() -> int:
    """
    Kolik by stálo přes appku/tokeny vygenerovat to samé, co appka posílá
    v placeném kanálu za měsíc — appka posílá jen 1 tiket denně, hlavně
    kratky, jen každý třetí den appka navíc zkusí stredni (viz
    run_daily_tickets). Appka pro srovnání počítá s cenou kratky, protože
    to je typ, co appka posílá naprostou většinu dní. Appka to počítá
    živě z TOKEN_COSTS/TOKEN_KC_VALUE, ne jako pevné číslo v textu
    landing page, ať se srovnání samo opraví, když se ceník někdy změní.
    """
    days_per_month = 30
    daily_kc = TOKEN_COSTS["kratky"] * TOKEN_KC_VALUE
    return daily_kc * days_per_month


def _ticket_still_sendable(ticket: Ticket, buffer_minutes: int = 30) -> bool:
    """Appka nepošle jako 'dnešní tip' tiket, kde už kterýkoli výběr vykopl
    (nebo vykopne za míň než buffer_minutes) — appka tikety generuje ráno
    s filtrem na BUDOUCÍ zápasy (viz _filter_future_matches), ale odeslání
    může appka spustit později (ruční doběh, nový odběratel se spáruje až
    večer...) a mezitím už zápas může běžet nebo být odehraný. Poslat
    takový "tip" by bylo zavádějící — appka slibuje kurz předem, ne
    komentář k živému/skončenému zápasu."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=buffer_minutes)
    for s in ticket.selections:
        try:
            kickoff_dt = datetime.fromisoformat(f"{s.kickoff_date}T{s.kickoff_time}:00+00:00")
        except (ValueError, AttributeError, TypeError):
            continue
        if kickoff_dt <= cutoff:
            return False
    return True


def _todays_client_picks(target_user_id: int) -> list[dict]:
    """Vybere z dnešních uložených tiketů appkina automatického účtu
    (target_user_id) 1 nejlepší kratky + 1 nejlepší stredni — stejný
    výběr pro náhled i pro odeslání. Appka BOOST už negeneruje ani
    nenabízí (viz run_daily_tickets)."""
    today_prague = datetime.now(ZoneInfo("Europe/Prague"))
    today_start_utc = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def _created_at_utc(row):
        created_at = row["created_at"]
        # repo.get_saved_tickets appka vrací created_at jako tz-aware (UTC) —
        # na rozdíl od run_daily_tickets výše appka tady srovnává přímo v
        # Pythonu (ne v SQL), takže obě strany musí mít stejnou "aware"
        # podobu, jinak Python porovnání spadne (naive vs aware).
        return created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)

    rows = [r for r in repo.get_saved_tickets(target_user_id) if _created_at_utc(r) >= today_start_utc]

    def _best(ticket_type):
        candidates = [
            r for r in rows
            if r["ticket"].ticket_type == ticket_type and _ticket_still_sendable(r["ticket"])
        ]
        return max(candidates, key=lambda r: r["ticket"].combined_probability) if candidates else None

    return [p for p in (_best("kratky"), _best("stredni")) if p is not None]


@app.get("/admin/candidate-pool-preview")
def candidate_pool_preview(request: Request, time_frame_days: int = 2):
    """Čistě informativní přehled — kolik zápasů appka dnes stáhla a kolik
    z nich prošlo jako kandidát na 70%/65% prahu (stejná čísla, co appka
    jinak jen loguje jako '[kratky] 70%: N kandidátů'). NIC neukládá,
    NIC neposílá — bezpečné volat kdykoli ke kontrole, jestli appka má
    dost zápasů na to, aby denní generování mělo z čeho vybírat. Používá
    stejnou cache jako reálné generování (zápasy 30 min, statistiky
    24 h), takže krátce po skutečném běhu je skoro zadarmo."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    matches = _fetch_candidate_matches(DAILY_TICKETS_SPORTS, time_frame_days)

    candidates_by_threshold = {}
    candidates_by_market_65 = {}
    for threshold in (0.75, 0.73, 0.72, 0.71, 0.70, 0.65):
        pool = []
        for m in matches:
            pool.extend([
                c for c in MarketEvaluator.build_candidates(m, min_prob=threshold)
                if c.market_type in DAILY_TICKETS_MARKETS
            ])
        candidates_by_threshold[f"{int(threshold * 100)}%"] = len(pool)
        if threshold == 0.65:
            for c in pool:
                candidates_by_market_65[c.market_type.value] = candidates_by_market_65.get(c.market_type.value, 0) + 1

    # Dočasná diagnostika (viz nahlášený "appka nic nevygenerovala") —
    # kolik zápasů má appka spolehlivou formu (games_played>=6 NEBO
    # home/away_recent_form_available) a kolik jich bez ní zůstává
    # zavřených pro over góly/BTTS.
    reliable_form_count = 0
    blocked_by_form_count = 0
    for m in matches:
        home_reliable = m.home_games_played >= MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS or m.home_recent_form_available
        away_reliable = m.away_games_played >= MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS or m.away_recent_form_available
        if home_reliable and away_reliable:
            reliable_form_count += 1
        else:
            blocked_by_form_count += 1

    return {
        "time_frame_days": time_frame_days,
        "fixtures_fetched": len(matches),
        "candidates_by_threshold": candidates_by_threshold,
        "candidates_by_market_at_65pct": candidates_by_market_65,
        "reliable_form_count": reliable_form_count,
        "blocked_by_form_count": blocked_by_form_count,
    }


@app.get("/admin/client-tickets-preview")
def client_tickets_preview(request: Request):
    """Ukáže, co by appka DNES poslala odběratelům (bez odeslání) — ke
    kontrole před /admin/client-tickets-send."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    target_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
    if not target_user_id_raw:
        raise HTTPException(status_code=500, detail="DAILY_TICKETS_USER_ID není nastavené")

    picks = _todays_client_picks(int(target_user_id_raw))
    subscriber_count = len(db.get_active_telegram_subscribers())
    return {
        "subscriber_count": subscriber_count,
        "picks": [
            {
                "ticket_id": r["ticket_id"],
                "ticket_type": r["ticket"].ticket_type,
                "total_odds": r["ticket"].total_odds,
                "combined_probability": r["ticket"].combined_probability,
                # Výkop appka do náhledu přidala schválně (viz
                # _ticket_still_sendable) — bez něj nejde ručně ověřit,
                # jestli filtr na "ještě nezačaté zápasy" vůbec dělá něco.
                "selections": [
                    f"{s.home_team} – {s.away_team} ({s.selection}, {s.odds}) @ {s.kickoff_date} {s.kickoff_time} UTC"
                    for s in r["ticket"].selections
                ],
            }
            for r in picks
        ],
    }


@app.post("/admin/client-tickets-send")
def client_tickets_send(request: Request):
    """Po ruční kontrole (viz /admin/client-tickets-preview) rozešle
    dnešní výběr všem aktivním odběratelům z Telegram webhooku."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    target_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
    if not target_user_id_raw:
        raise HTTPException(status_code=500, detail="DAILY_TICKETS_USER_ID není nastavené")

    picks = _todays_client_picks(int(target_user_id_raw))

    # Rozesílka se ptá VÝHRADNĚ na platící (get_paid_telegram_subscribers
    # dělá JOIN na subscriptions). get_active_telegram_subscribers() by
    # vrátilo i všechny, kdo si kdy jen napsali /start — tedy i lidi bez
    # jediné zaplacené koruny.
    recipients = db.get_paid_telegram_subscribers()

    results = []
    for recipient in recipients:
        chat_id = recipient["chat_id"]
        for r in picks:
            try:
                ticket_telegram.send_ticket_to_telegram(_ticket_to_telegram_dict(r["ticket"], r["ticket_id"]), chat_id=chat_id)
                results.append({"chat_id": chat_id, "ticket_id": r["ticket_id"], "status": "sent"})
            except Exception as e:
                results.append({"chat_id": chat_id, "ticket_id": r["ticket_id"], "status": f"error: {e}"})

    return {
        "picks_sent": [r["ticket_id"] for r in picks],
        "recipients": len(recipients),
        "results": results,
    }


@app.post("/admin/telegram-sync")
def admin_telegram_sync(request: Request):
    """
    Uklidí Telegram po lidech, kterým předplatné doběhlo — pošle jim
    zprávu proč a označí je jako neaktivní. Rozesílka je sice vynechá
    i bez tohohle (ptá se na platnost při každém běhu), ale bez zprávy
    by se nikdy nedozvěděli, že jim tikety přestaly chodit záměrně.
    Pouštět stejnou naplánovanou úlohou jako denní tikety.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    lapsed = db.get_lapsed_telegram_chats()
    for row in lapsed:
        _send_telegram_message(row["chat_id"], TELEGRAM_EXPIRED_MESSAGE)
        db.set_telegram_chat_active(row["chat_id"], False)

    return {"deactivated": len(lapsed), "chat_ids": [r["chat_id"] for r in lapsed]}


TELEGRAM_WELCOME_MESSAGE = (
    "Ahoj! 👋 Účet je spárovaný, předplatné máš aktivní.\n\n"
    "Od teď ti sem budu každé ráno posílat 1 tiket 🎫 — hlavně krátký, občas (když appka "
    "najde opravdovou hodnotu) radši ten hodnotnější, střední.\n\n"
    "Appka jen vybírá zápasy a doporučuje tikety podle vlastního modelu — sázku si vždycky "
    "klikáš ty sám, kde chceš (Tipsport, Fortuna...). Je to asistent na rozhodování, ne robot, "
    "co sází místo tebe.\n\n"
    "18+. Hazardní hraní může být návykové — sázej jen to, co můžeš prohrát.\n\n"
    "Ať se daří! ⚽"
)

# Appka tuhle zprávu pošle každému, kdo botovi napíše bez platného
# párovacího kódu. Dřív takový člověk rovnou začal dostávat placené
# tikety — /start stačilo k odběru a nikde se neověřovalo, jestli za ním
# stojí platba.
TELEGRAM_NO_ACCESS_MESSAGE = (
    "Ahoj! 👋 Tenhle kanál je pro předplatitele ApexSignalu.\n\n"
    "Předplatné aktivuješ na apexsignal.cz/transparentni-ucet — po zaplacení appka na tvůj "
    "e-mail rovnou pošle tenhle přesný odkaz s párovacím kódem, stačí na něj kliknout.\n\n"
    "Jak si model vede, si můžeš kdykoli zdarma zkontrolovat tamtéž — jsou tam všechny "
    "tikety včetně prohraných.\n\n"
    "18+"
)

TELEGRAM_LINK_INVALID_MESSAGE = (
    "Tenhle párovací odkaz už neplatí — kódy vydrží hodinu a jdou použít jen jednou.\n\n"
    "Na apexsignal.cz/transparentni-ucet je dole formulář — zadej tam e-mail, kterým jsi "
    "platil, a appka ti hned pošle nový odkaz."
)

TELEGRAM_EXPIRED_MESSAGE = (
    "Předplatné ti skončilo, takže sem další tikety posílat nebudu. 🙏\n\n"
    "Kdykoli ho můžeš obnovit na apexsignal.cz/transparentni-ucet a kanál naskočí zpátky.\n\n"
    "Díky, že jsi to zkusil — a ať se daří."
)

# Appka bot umí reagovat jen na /start a /stav — jakoukoli jinou zprávu
# appka dřív potichu zahazovala, klient nedostal odpověď a appka se o
# tom vůbec nedozvěděla. Tohle je odpověď, kterou dostane MÍSTO ticha.
TELEGRAM_UNRECOGNIZED_MESSAGE = (
    "Tenhle bot je automatický a zprávy sám nečte — jen posílá ranní tikety.\n\n"
    "Tvůj vzkaz appka přeposlala, ozveme se ti co nejdřív. Pro rychlejší odpověď "
    "napiš rovnou na apexsignal@seznam.cz."
)


def _forward_client_message_to_owner(chat_id: int, first_name: Optional[str], username: Optional[str], text: str) -> None:
    """
    Bot appce sám odpovídat neumí, takže appka aspoň přepošle zprávu tam,
    kde ji appka uvidí — appku vlastní Telegram (TELEGRAM_CHAT_ID). Bez
    tohohle appka nemá jak zjistit, že jí vůbec někdo něco napsal.
    """
    owner_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not owner_chat_id:
        print("[telegram] TELEGRAM_CHAT_ID není nastavený, nemám kam přeposlat zprávu klienta")
        return

    subscription_id = db.get_subscription_id_for_chat(chat_id)
    sub = db.get_subscription_by_id(subscription_id) if subscription_id else None
    who = f"předplatitel ({sub['email']})" if sub else "nepárovaný/neznámý chat"
    handle = f"@{username}" if username else "bez uživatelského jména"

    message = (
        f"📩 Zpráva od klienta na Telegram botovi\n"
        f"{first_name or '—'} ({handle}) — {who}\n\n"
        f"„{text}“"
    )
    try:
        _send_telegram_message(int(owner_chat_id), message)
    except Exception as e:
        print(f"[telegram] nepodařilo se přeposlat zprávu klienta: {e}")


def _send_telegram_message(chat_id: int, text: str) -> None:
    """Odeslání jedné textové zprávy. Chybu appka jen zaloguje — když
    selže uvítání, nesmí to shodit celý webhook (Telegram by ho pak
    opakoval dokola)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("[telegram] TELEGRAM_BOT_TOKEN není nastavený, zprávu neposílám")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"[telegram] chyba při odesílání zprávy na {chat_id}: {e}")


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Telegram appce POSTuje sem každou novou zprávu (appka má nastavený
    webhook přes setWebhook, misto rucniho pollovani getUpdates) — appka
    díky tomu umí SAMA (bez zásahu admina) přivítat nového klienta a
    zapsat si jeho chat_id do telegram_subscribers, jakmile appce napíše
    /start. Volitelně zabezpečeno TELEGRAM_WEBHOOK_SECRET (Telegram ho
    posílá zpátky v hlavičce X-Telegram-Bot-Api-Secret-Token).
    """
    secret_expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret_expected and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret_expected:
        raise HTTPException(status_code=403, detail="Neplatný webhook secret")

    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    print(f"[telegram-webhook] chat_id={chat_id} name={chat.get('first_name')!r} text={text!r}")

    if not chat_id:
        return {"ok": True}

    if text.startswith("/start"):
        # "/start ABC123" — kód z odkazu, který si uživatel vygeneroval v
        # appce po zaplacení. Bez kódu (holé /start) appka nemá jak zjistit,
        # kdo to je, takže přístup nedává — jen vysvětlí, jak na to.
        parts = text.split(maxsplit=1)
        code = parts[1].strip() if len(parts) > 1 else ""

        if not code:
            _send_telegram_message(chat_id, TELEGRAM_NO_ACCESS_MESSAGE)
            return {"ok": True}

        linked_subscription_id = db.consume_telegram_link_code(code)
        if linked_subscription_id is None:
            _send_telegram_message(chat_id, TELEGRAM_LINK_INVALID_MESSAGE)
            return {"ok": True}

        # Kód mohl být vydaný ještě v době platného předplatného, ale
        # uplatněný až po jeho konci — appka proto ověřuje platbu znovu
        # tady, ne jen při vydávání kódu.
        if not db.has_active_subscription_id(linked_subscription_id):
            _send_telegram_message(chat_id, TELEGRAM_NO_ACCESS_MESSAGE)
            return {"ok": True}

        db.link_telegram_chat(chat_id, linked_subscription_id, chat.get("first_name"))
        _send_telegram_message(chat_id, TELEGRAM_WELCOME_MESSAGE)
        return {"ok": True}

    if text == "/stav":
        subscription_id = db.get_subscription_id_for_chat(chat_id)
        if subscription_id and db.has_active_subscription_id(subscription_id):
            sub = db.get_subscription_by_id(subscription_id) or {}
            period_end = sub.get("current_period_end")
            konec = period_end.strftime("%-d. %-m. %Y") if hasattr(period_end, "strftime") else "neznámo"
            _send_telegram_message(
                chat_id,
                f"Předplatné je aktivní. Zaplaceno do {konec}.\n\n"
                "Nic dalšího dělat nemusíš — tiket chodí automaticky každé ráno.",
            )
        else:
            _send_telegram_message(chat_id, TELEGRAM_NO_ACCESS_MESSAGE)
        return {"ok": True}

    if text:
        # Cokoli jiného než /start nebo /stav appka dřív potichu
        # zahazovala — klient nedostal odpověď a appka se to nikdy
        # nedozvěděla. Teď aspoň appku upozorní a klientovi řekne, kam
        # se obrátit pro rychlejší odpověď.
        _send_telegram_message(chat_id, TELEGRAM_UNRECOGNIZED_MESSAGE)
        _forward_client_message_to_owner(chat_id, chat.get("first_name"), chat.get("username"), text)

    return {"ok": True}


class AdminSeedShowcaseRequest(BaseModel):
    ticket_type: str
    selections: list[SaveSelectionRequest]
    total_odds: float
    combined_probability: float = 0.0
    recommended_stake_pct: float = 0.0
    stake_amount: float
    created_at: Optional[str] = None  # ISO datetime — appka zachová reálné datum starší výhry
    target_user_id: Optional[int] = None  # výchozí DAILY_TICKETS_USER_ID, appka umožní i jiný cílový účet
    status: str = "won"  # appka defaultně "won" kvůli zpětné kompatibilitě (výkladní skříň ukazuje jen výhry)
    selection_results: Optional[list[str]] = None  # per-výběr výsledek (won/lost/pending), stejné pořadí jako selections


@app.post("/admin/showcase/seed")
def admin_seed_showcase(req: AdminSeedShowcaseRequest, request: Request):
    """
    Appka tímhle ručně přidá starší tiket appky (nejčastěji z testovacích
    účtů) na cílový účet se zachovaným datem — appka nemění nic na tom,
    co se reálně stalo (appka tiket zkopíruje 1:1, jen appka ho fyzicky
    přesune pod jiný účet). Původně jen appky vlastní výkladní skříň
    (/showcase/tickets, DAILY_TICKETS_USER_ID, jen výhry) — target_user_id
    a status appka přidala i pro obnovu omylem smazaných tiketů na
    libovolném účtu, s libovolným výsledkem.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    if req.target_user_id is not None:
        target_user_id = req.target_user_id
    else:
        target_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
        if not target_user_id_raw:
            raise HTTPException(status_code=500, detail="DAILY_TICKETS_USER_ID není nastavené")
        target_user_id = int(target_user_id_raw)

    # "dlouhy" je starší appky vlastní název pro BOOST, pořád se objevuje
    # ve starších uložených datech appky — insert_ticket ho ale nepustí
    # (validace appky zná jen kratky/stredni/boost).
    ticket_type = "boost" if req.ticket_type == "dlouhy" else req.ticket_type

    domain_selections = [
        SelectionCandidate(
            match_id=s.match_id, home_team=s.home_team, away_team=s.away_team,
            sport=Sport.FOOTBALL,
            market_type=MarketType(s.market_type) if s.market_type in [m.value for m in MarketType] else MarketType.MATCH_WINNER,
            selection=s.selection,
            probability=s.probability, odds=s.odds,
            model_probability=s.model_probability, market_probability=s.market_probability,
            reasoning=s.reasoning, data_quality=s.data_quality,
            league=s.league, country=s.country, kickoff_date=s.kickoff_date, kickoff_time=s.kickoff_time,
        ) for s in req.selections
    ]
    ticket = Ticket(
        ticket_type=ticket_type, selections=domain_selections,
        total_odds=req.total_odds, combined_probability=req.combined_probability,
        recommended_stake_pct=req.recommended_stake_pct,
    )
    created_at_dt = datetime.fromisoformat(req.created_at) if req.created_at else None
    ticket_id = repo.save_ticket(target_user_id, ticket, created_at=created_at_dt)
    repo.set_actual_stake(ticket_id, req.stake_amount, req.total_odds)
    repo.set_ticket_status(ticket_id, req.status)

    if req.selection_results:
        row = db.fetch_ticket_rows(ticket_id=ticket_id)
        if row:
            selection_ids = [s.get("id") for s in row[0].get("selections", [])]
            for selection_id, result in zip(selection_ids, req.selection_results):
                if selection_id is not None:
                    db.update_selection_result(selection_id, result)

    return {"ticket_id": ticket_id, "status": "seeded"}


class CopyDailyTicketsRequest(BaseModel):
    target_user_id: int
    stake_amount: float


@app.post("/admin/copy-daily-tickets-to-user")
def copy_daily_tickets_to_user(req: CopyDailyTicketsRequest, request: Request):
    """
    Zkopíruje appkou DNES vygenerované tikety (DAILY_TICKETS_USER_ID —
    ty, co appka posílá i na hlavní Telegram chat) na konkrétní účet
    (např. test3), každý s danou sázkou (stejnou pro každý tiket) a
    stavem 'pending'. Appka duplicity (stejná sada zápasů+tipů, typicky
    z dvojího běhu cronu za den) vynechá — zkopíruje jen jednu kopii
    každé unikátní kombinace.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    source_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
    if not source_user_id_raw:
        raise HTTPException(status_code=500, detail="DAILY_TICKETS_USER_ID není nastavené")
    source_user_id = int(source_user_id_raw)

    today_prague = datetime.now(ZoneInfo("Europe/Prague"))
    today_start_utc = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def _created_at_utc(row):
        created_at = row["created_at"]
        return created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=timezone.utc)

    def _signature(ticket):
        return tuple(sorted((s.match_id, s.market_type.value, s.selection) for s in ticket.selections))

    rows = [r for r in repo.get_saved_tickets(source_user_id) if _created_at_utc(r) >= today_start_utc]

    seen_signatures = set()
    copied = []
    for row in rows:
        sig = _signature(row["ticket"])
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        new_ticket_id = repo.save_ticket(req.target_user_id, row["ticket"])
        repo.set_actual_stake(new_ticket_id, req.stake_amount, row["ticket"].total_odds)
        copied.append({
            "new_ticket_id": new_ticket_id,
            "source_ticket_id": row["ticket_id"],
            "ticket_type": row["ticket"].ticket_type,
            "total_odds": row["ticket"].total_odds,
        })

    return {"copied_count": len(copied), "skipped_duplicates": len(rows) - len(copied), "copied": copied}


ALL_DB_TABLES = [
    "users", "tickets", "ticket_selections", "api_cache", "user_tokens",
    "token_transactions", "redeem_codes", "redeem_code_uses",
    "stripe_events", "password_reset_tokens", "email_verification_tokens", "telegram_subscribers",
    "referral_rewards", "user_card_fingerprints",
]
# api_cache (nacachované odpovědi z the-odds-api/API-Football) a
# password_reset_tokens/email_verification_tokens (krátkodobé, časově
# omezené) appka do výchozí zálohy nezahrnuje — nejsou to reálná
# uživatelská data a jde o jedinou tabulku appky, co bývá dost velká na
# to, aby export shodil web service na Renderově free 512MB RAM limitu
# (OOM restart).
DEFAULT_BACKUP_TABLES = [t for t in ALL_DB_TABLES if t not in ("api_cache", "password_reset_tokens", "email_verification_tokens")]


@app.get("/admin/db-stats")
def admin_db_stats(request: Request):
    """Počet řádků v každé tabulce — appka tímhle před exportem ověří, jestli se dump vejde do paměti."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    counts = {}
    with db.get_cursor() as cur:
        for table in ALL_DB_TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            counts[table] = cur.fetchone()["n"]
    return counts


@app.get("/admin/export-db")
def admin_export_db(request: Request, tables: str = ""):
    """
    Nouzový export databáze jako JSON (appka bere syrové řádky tabulka po
    tabulce). Appka běží na Renderu, kde je Postgres port zablokovaný pro
    spojení zvenčí appčina vývojového prostředí — přímý pg_dump odtud
    nejde, tenhle endpoint appce umožní stáhnout zálohu přes obyčejné
    HTTPS. Výchozí sada appku vynechává 'api_cache' a
    'password_reset_tokens' (viz DEFAULT_BACKUP_TABLES) kvůli paměťovému
    limitu free web service — appka je zahrne jen na výslovné přání přes
    ?tables=api_cache,...
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    requested = [t.strip() for t in tables.split(",") if t.strip()] or DEFAULT_BACKUP_TABLES
    invalid = [t for t in requested if t not in ALL_DB_TABLES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Neznámé tabulky: {invalid}")

    dump = {}
    with db.get_cursor() as cur:
        for table in requested:
            cur.execute(f"SELECT * FROM {table}")
            dump[table] = [dict(row) for row in cur.fetchall()]
    return json.loads(json.dumps(dump, default=str))


@app.get("/showcase/tickets")
def showcase_tickets(limit: int = 20):
    """
    Veřejná "výkladní skříň" appky — bez přihlášení appka vrátí poslední
    VYHRANÉ tikety appčiných dvou vlastních automatických účtů
    (DAILY_TICKETS_USER_ID a TRANSPARENCY_USER_ID), NIKDY tikety běžných
    uživatelů appky. U obou účtů appka posílá skutečně uložený
    actual_stake_amount/actual_profit_loss — u TRANSPARENCY_USER_ID je to
    vždy pevných 2000 Kč (viz TRANSPARENCY_STAKE), u DAILY_TICKETS_USER_ID
    částka náhodně vybraná z DAILY_TICKETS_STAKE_CHOICES (viz komentář
    tam) — appka appka NEVYMÝŠLÍ číslo za běhu, appka jen posílá to, co už
    má uložené v DB. Slouží jako sociální důkaz na hlavní obrazovce appky
    pro nové návštěvníky.
    """
    limit = max(1, min(limit, 50))
    target_ids = [
        int(v) for v in (
            os.environ.get("DAILY_TICKETS_USER_ID"),
            os.environ.get("TRANSPARENCY_USER_ID"),
        )
        if v
    ]
    if not target_ids:
        return {"tickets": []}

    won_rows = []
    for target_user_id in target_ids:
        rows = repo.get_saved_tickets(target_user_id)
        won_rows.extend(r for r in rows if r["status"] == "won")
    won_rows.sort(key=lambda r: r.get("created_at") or datetime.min, reverse=True)
    won_rows = won_rows[:limit]

    tickets = []
    for r in won_rows:
        ticket = r["ticket"]
        created_at = r.get("created_at")
        tickets.append({
            "ticket_type": ticket.ticket_type,
            "total_odds": ticket.total_odds,
            "stake": r.get("actual_stake_amount"),
            "profit": r.get("actual_profit_loss"),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "selections": [
                {
                    "home_team": s.home_team, "away_team": s.away_team,
                    "market_type": s.market_type.value if hasattr(s.market_type, "value") else s.market_type,
                    "selection": s.selection, "odds": s.odds,
                    "league": s.league, "country": s.country,
                }
                for s in ticket.selections
            ],
        })
    return {"tickets": tickets}


class TicketAnalysisResponse(BaseModel):
    selections: list[dict]
    overall: str


@app.post("/tickets/analyze-image", response_model=TicketAnalysisResponse)
async def analyze_ticket_image(
    file: UploadFile,
    user_id: int = Depends(get_current_user_id),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="AI analýza není dostupná — chybí ANTHROPIC_API_KEY.")

    image_bytes = await file.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Obrázek je příliš velký (max 10 MB).")

    import base64
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    # Anthropic přijímá jen přesně tyto MIME typy — normalizujeme, ať nás neodmítne
    raw_ct = (file.content_type or "image/jpeg").lower()
    if "png" in raw_ct:
        content_type = "image/png"
    elif "gif" in raw_ct:
        content_type = "image/gif"
    elif "webp" in raw_ct:
        content_type = "image/webp"
    else:
        content_type = "image/jpeg"  # fallback pro jpg/jpeg a cokoliv jiného

    prompt = """Na obrázku je sázkový tiket. Udělej toto:
1. Přečti všechny výběry na tiketu (zápas, co je vsazeno)
2. Pro každý výběr vyhledej na webu aktuální informace (forma týmů, zranění, vzájemné zápasy)
3. Ke každému výběru napiš hodnocení

Odpověz POUZE validním JSON bez markdownu:
{"selections":[{"match":"Tým A vs Tým B","pick":"co je vsazeno","verdict":"doporučuji nebo riziko nebo vynechat","risk":"nízké nebo střední nebo vysoké","reasoning":"2-3 věty proč, konkrétní fakta"}],"overall":"Celkové shrnutí 2-3 větami."}

Pokud obrázek není sázkový tiket: {"selections":[],"overall":"Na obrázku nebyl rozpoznán sázkový tiket."}"""

    try:
        # Krok 1: Claude přečte obrázek a vytáhne výběry (bez web search — vision + tools nefunguje dohromady)
        resp1 = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": image_b64}},
                    {"type": "text", "text": "Přečti sázkový tiket na obrázku a vypiš POUZE JSON seznam výběrů: [{\"match\":\"Tým A vs Tým B\",\"pick\":\"co je vsazeno (výhra domácích/hostů/remíza/over gólů...)\",\"odds\":\"kurz nebo null\"}]. Žádný jiný text, pouze JSON pole."},
                ]}],
            },
            timeout=30,
        )
        resp1.raise_for_status()
        data1 = resp1.json()
        text1 = "".join(b.get("text","") for b in data1.get("content",[]) if b.get("type")=="text").strip()
        cleaned1 = text1.strip("`").removeprefix("json").strip()
        selections_raw = json.loads(cleaned1)

        if not selections_raw:
            return TicketAnalysisResponse(selections=[], overall="Na obrázku nebyl rozpoznán sázkový tiket.")

        # Krok 2: Claude dohledá informace a ohodnotí každý výběr (s web search, bez obrázku)
        listing = "\n".join(f"{i+1}. {s['match']} — {s['pick']}" for i, s in enumerate(selections_raw))
        resp2 = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": f"""Analyzuj tyto sázkové výběry. Pro každý vyhledej aktuální informace (forma, zranění, vzájemné zápasy) a ohodnoť ho.

{listing}

Odpověz POUZE validním JSON bez markdownu:
{{"selections":[{{"match":"Tým A vs Tým B","pick":"co je vsazeno","verdict":"doporučuji nebo riziko nebo vynechat","risk":"nízké nebo střední nebo vysoké","reasoning":"2-3 věty proč s konkrétními fakty z webu"}}],"overall":"Celkové shrnutí tiketu 2-3 větami."}}"""}],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            timeout=45,
        )
        resp2.raise_for_status()
        data2 = resp2.json()
        text2 = "".join(b.get("text","") for b in data2.get("content",[]) if b.get("type")=="text").strip()
        cleaned2 = text2.strip("`").removeprefix("json").strip()
        result = json.loads(cleaned2)
        return TicketAnalysisResponse(selections=result.get("selections",[]), overall=result.get("overall",""))

    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="AI vrátila neočekávaný formát, zkus to znovu.")
    except requests.HTTPError as exc:
        detail = ""
        try:
            detail = exc.response.json()
        except Exception:
            detail = str(exc)
        raise HTTPException(status_code=502, detail=f"Analýza selhala: {detail}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Analýza selhala: {exc}")


# ═══════════════════════════════════════════════════════════════════
# SIGNÁL - Nová sekce: AI-shrnutí stavu uživatele
# ═══════════════════════════════════════════════════════════════════

@app.post("/signal/generate")
def generate_signal_data(
    period: str = "now",  # "now" / "short" / "long"
    days_back: int = 7,   # 3/7/14 pro "short"
    user_id: int = Depends(get_current_user_id),
):
    """
    Vrátí strukturované JSON data pro Signál.
    - period="now" → live snapshot (zápasy dnes)
    - period="short" → agregace za N dní
    - period="long" → agregace od začátku
    """
    try:
        # Stáhni tiketty uživatele z DB
        tickets = db.fetch_ticket_rows(user_id=user_id)
        if not tickets:
            return {"error": "Žádné tiketty zatím.", "not_started": [], "live": [], "finished_selections": [], "summary": {}}

        # TODO: Implementovat logiku compute_signal_now(), compute_signal_short(), compute_signal_long()
        # Zatím vrátíme fallback

        if period == "now":
            signal_data = _compute_signal_now(tickets)
        elif period == "short":
            signal_data = _compute_signal_short(tickets, days_back)
        elif period == "long":
            signal_data = _compute_signal_long(tickets)
        else:
            raise ValueError(f"Neznámý period: {period}")

        return signal_data

    except Exception as e:
        print(f"[signal/generate ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Chyba při generování Signálu: {e}")


@app.patch("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, req: dict = Body(...), user_id: int = Depends(get_current_user_id)):
    """
    Update ticket total_odds and/or actual_stake_amount.
    Used when user adjusts odds in detail view or at save time.
    """
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")

    db.update_ticket_fields(
        ticket_id,
        total_odds=req.get("total_odds"),
        actual_stake_amount=req.get("actual_stake_amount"),
    )
    return {"ticket_id": ticket_id, "status": "updated"}


@app.post("/signal/text")
def generate_signal_text(
    data: dict = Body(...),
    user_id: int = Depends(get_current_user_id),
):
    """
    Pošle strukturované data do Claude API.
    Claude vrátí plynulý text v češtině.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="Signál není dostupný — chybí ANTHROPIC_API_KEY.")

    try:
        print(f"[signal/text] Received data: {json.dumps(data)[:200]}")  # DEBUG
        
        # Připrav prompt pro Claude
        prompt = _prepare_signal_prompt(data)
        print(f"[signal/text] Prompt length: {len(prompt)}")  # DEBUG

        # Zavolej Claude API
        request_body = {
            "model": "claude-sonnet-5",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        }
        print(f"[signal/text] Sending request to Claude API")  # DEBUG
        
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=request_body,
            timeout=30,
        )
        print(f"[signal/text] Response status: {resp.status_code}")  # DEBUG
        
        resp.raise_for_status()
        result = resp.json()

        # Extrahuj text z odpovědi
        text = "".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text").strip()
        print(f"[signal/text] Generated text length: {len(text)}")  # DEBUG
        return {"text": text}

    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json()
        except Exception:
            detail = str(e)
        print(f"[signal/text HTTP ERROR] {e.response.status_code}: {detail}")  # DEBUG
        raise HTTPException(status_code=502, detail=f"Claude API selhala: {detail}")
    except Exception as e:
        print(f"[signal/text ERROR] {e}")
        raise HTTPException(status_code=500, detail=f"Chyba při generování textu: {e}")


def _compute_signal_now(tickets):
    """Vrátí 'Právě teď' data — live snapshot."""
    return {
        "not_started": [],
        "live": [],
        "finished_selections": [],
        "summary": {"total_open_tickets": 0, "total_staked": 0, "total_potential": 0},
    }


def _compute_signal_short(tickets, days_back):
    """Vrátí agregaci za N dní."""
    return {
        "period": "short",
        "days_back": days_back,
        "staked": 0,
        "profit": 0,
        "win_rate": 0.0,
        "total_tickets": 0,
        "breakdown": {},
    }


def _compute_signal_long(tickets):
    """Vrátí agregaci od začátku."""
    return {
        "period": "long",
        "staked": 0,
        "profit": 0,
        "win_rate": 0.0,
        "total_tickets": 0,
        "breakdown": {},
    }


def _prepare_signal_prompt(data):
    """Připrav prompt pro Claude."""
    # TODO: Formátuj data do promptu
    return "Převypravuj tyhle data o sázení do plynulého českého textu (čistě informativní, bez rad).\n\nData: " + json.dumps(data)


