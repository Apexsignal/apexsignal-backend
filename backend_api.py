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

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import os
import random
import secrets
import io
import json
import requests
import aiohttp
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from probability_model import (
    TicketGenerator, MatchInput, Sport, MarketType, Ticket, SelectionCandidate, evaluate_selection_outcome,
    MarketEvaluator, SPORT_MARKETS, MIN_GAMES_PLAYED_FOR_FORM_SENSITIVE_MARKETS,
    edge_capped_model_probability, kelly_stake_fraction, set_calibration_curve,
    TIPSPORT_UNAVAILABLE_COUNTRIES, TIPSPORT_UNAVAILABLE_MATCH_IDS,
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
#
# Appka tohle SNÍŽILA z 40 na 20 (2026-08-05), a pak z 20 na 10
# (2026-08-07) — appka na Renderu jede na starter plánu (512 MB RAM) a
# i po prvním snížení appka živě zaznamenala TŘI OOM pády za sebou na
# 2denním okně (přes Render API, /admin/lost-tickets-report vyšetřování).
# 10 souběžných vláken je pořád víc než appčin rate limiter stejně pustí
# najednou (~12 req/s, ale KAŽDÉ vlákno dělá ~10-12 SEKVENČNÍCH volání na
# zápas, takže 10 vláken = max ~10 zápasů rozpracovaných najednou, ne 10
# jednotlivých požadavků) — appka radši dál obětuje rychlost za stabilitu,
# dokud nemá vyšší plán (viz i MAX_FIXTURES_PER_REQUEST a dávkové
# zpracování v _build_football_matches níž — všechny tři appka ladila
# společně na stejný cíl).
#
# 2026-09-13: appka i po snížení MAX_FIXTURES_PER_REQUEST (180→150→100)
# pořád živě zaznamenala OOM (viz stejné datum v probability_model.py) —
# appka jde dál a snižuje i souběžnost obohacování z 10 na 5 vláken.
# Pomaleji, ale míň zápasů rozpracovaných zároveň = nižší špička paměti
# v jednu chvíli. Cena: generování appce potrvá zhruba 2x déle.
FIXTURE_ENRICHMENT_WORKERS = 5

# Dixon-Coles zafitovaná útočná/obranná síla CELÉ ligy (2026-08-06) — appka
# to zkusila jako přesnější náhradu heuristického odhadu z posledních
# zápasů dvou týmů (_estimate_expected_goals v data_provider.py). VYPNUTO
# defaultně — appka tenhle přepínač staví jako bezpečnou volbu, kterou
# uživatel může kdykoliv vrátit zpátky, kdyby nový model generoval míň
# tiketů nebo horší výsledky (appka to sama NEROZHODUJE, jen nabízí, viz
# GET /admin/test-dixon-coles pro ruční ověření PŘED zapnutím naostro).
# Appka pro ligu/tým, kde nemá dost odehraných zápasů, automaticky
# spadne zpátky na starý heuristický odhad (viz data_provider.
# get_dixon_coles_strengths) — appka tímhle přepínačem NIKDY negeneruje
# míň tiketů, jen případně přesnější xG tam, kde má dost dat.
#
# Appka to čte z DB (app_settings, přes db.get_setting), NE z pevné env
# proměnné — appka chtěla umět tlačítkem v appce (POST /admin/set-dixon-coles)
# přepnout OKAMŽITĚ, bez ručního zásahu na Renderu a bez redeploye. Env
# proměnná DIXON_COLES_ENABLED appce zůstává jako výchozí hodnota, POKUD
# appka v DB ještě žádnou nemá uloženou (první spuštění po tomhle commitu).
DIXON_COLES_SETTING_KEY = "dixon_coles_enabled"


def _is_dixon_coles_enabled() -> bool:
    env_default = "true" if os.environ.get("DIXON_COLES_ENABLED", "false").strip().lower() == "true" else "false"
    try:
        value = db.get_setting(DIXON_COLES_SETTING_KEY, default=env_default)
    except Exception:
        value = env_default
    return (value or "false").strip().lower() == "true"


# Appčina vlastní kalibrační křivka (viz set_calibration_curve v
# probability_model.py) — appka ji přepočítá na appčin pokyn
# (/admin/recompute-calibration-curve), appka ji uloží sem a appka ji
# načte na začátku KAŽDÉHO generování (_load_calibration_curve níž),
# stejný vzor appka už používá u Dixon-Coles přepínače výš.
CALIBRATION_CURVE_SETTING_KEY = "calibration_curve_v1"
CALIBRATION_BUCKET_MIN_SAMPLES = 15  # appka appce zamítne koš s míň pozorováními — moc šumu na to, aby appka věřila korekci


def _load_calibration_curve() -> None:
    try:
        raw = db.get_setting(CALIBRATION_CURVE_SETTING_KEY)
        curve = {int(k): float(v) for k, v in json.loads(raw).items()} if raw else {}
    except Exception as e:
        print(f"[calibration] Nepodařilo se načíst kalibrační křivku, appka jede bez korekce: {e}")
        curve = {}
    set_calibration_curve(curve)


# E-maily účtů appky, co appka pouští k appce-interním přepínačům (zatím
# jen Dixon-Coles model), BEZ nutnosti X-Admin-Key (ten appka nesmí dát
# do frontendu, viz /settings/dixon-coles níže — X-Admin-Key otevírá i
# nebezpečné admin endpointy jako mazání účtů). Appka to bere z ENV
# proměnné (čárkou oddělený seznam), ať appka rozšíření na DALŠÍ účty
# zvládne jen změnou proměnné na Renderu, bez zásahu do kódu — appka
# defaultně nechává aspoň majitelův účet, ať appka funguje i BEZ ruční
# konfigurace na Renderu.
ADMIN_APP_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_APP_EMAILS", "d.voves@seznam.cz").split(",")
    if e.strip()
}


def _is_admin_app_user(user_id: int) -> bool:
    try:
        user = db.get_user_by_id(user_id)
    except Exception:
        return False
    email = (user or {}).get("email", "").strip().lower()
    return bool(email) and email in ADMIN_APP_EMAILS


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

# appka appce (2026-09-14) chtěla zabránit AI botům appku trénovat na
# appky datech (výsledky, tikety) a obecným scraperům je kopírovat —
# CORS appce nic nedá, chrání jen prohlížeč appky (Origin hlavička), ne
# přímé volání skriptem/botem, co Origin vůbec neposílá. Appka proto
# přidává dvě samostatné vrstvy: blokuje podle User-Agent (známí
# AI/scraper boti dostanou rovnou 403) a jednoduchý rate limit na IP
# appky (moc rychlé volání appka odmítne, i beze jména v User-Agentu).
_BLOCKED_USER_AGENT_SUBSTRINGS = [
    # AI trénovací/crawler boti — appky známá jména.
    "gptbot", "chatgpt-user", "ccbot", "anthropic-ai", "claudebot", "claude-web",
    "google-extended", "googleother", "bytespider", "bingbot-ai", "cohere-ai",
    "perplexitybot", "omgilibot", "omgili", "diffbot", "youbot", "meta-externalagent",
    "facebookbot", "applebot-extended", "timpibot", "bytedance",
    # obecné scrapery/HTTP knihovny — appka je appce rovnou blokuje,
    # appka appky reálný appky prohlížeč appky takhle appku nepředstaví.
    "scrapy", "python-requests", "python-urllib", "curl/", "wget/", "go-http-client",
    "node-fetch", "axios/", "java/", "libwww-perl", "httpclient",
]

# appka appce jednoduchý in-memory rate limit — appky zvlášť appka
# appky (stejný přístup jako rate_limiter.py, žádná nová závislost).
from collections import defaultdict as _defaultdict, deque as _deque

_PUBLIC_RATE_WINDOW_SECONDS = 60
_PUBLIC_RATE_MAX_REQUESTS = 30  # appka appku appce appku appky 30 appky/minutu appku appku appku IP appku appky
_public_request_log: dict[str, "_deque[float]"] = _defaultdict(_deque)


@app.middleware("http")
async def _block_bots_and_rate_limit(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/public/"):
        return await call_next(request)

    user_agent = (request.headers.get("user-agent") or "").lower()
    if any(needle in user_agent for needle in _BLOCKED_USER_AGENT_SUBSTRINGS):
        return JSONResponse(status_code=403, content={"detail": "Přístup zamítnut."})

    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    now = time.time()
    log = _public_request_log[client_ip]
    while log and log[0] < now - _PUBLIC_RATE_WINDOW_SECONDS:
        log.popleft()
    if len(log) >= _PUBLIC_RATE_MAX_REQUESTS:
        return JSONResponse(status_code=429, content={"detail": "Příliš mnoho požadavků, zkus to za chvíli znovu."})
    log.append(now)

    return await call_next(request)

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
    device_seen: Optional[bool] = None  # appka appce pošle True, když appka v localStorage appky
    # už NAJDE appčinu značku z PŘEDCHOZÍ registrace na stejném zařízení/prohlížeči — appka appce
    # to jen zaznamená pro admin přehled (viz multi_account_flag), appka na tom nic neblokuje.

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


_lead_submissions: dict[str, list[float]] = {}
LEAD_MAX_PER_IP_PER_HOUR = 5


def _lead_rate_limited(ip: str) -> bool:
    """Appka nábor-formulář nechává veřejný bez přihlášení — tenhle prostý
    in-memory limit (5 odeslání/hodinu z jedné IP) appku chrání proti
    zaplavení appčina Telegramu spamem, bez nutnosti nové DB tabulky."""
    now = time.time()
    cutoff = now - 3600
    hits = [t for t in _lead_submissions.get(ip, []) if t > cutoff]
    _lead_submissions[ip] = hits
    if len(hits) >= LEAD_MAX_PER_IP_PER_HOUR:
        return True
    hits.append(now)
    return False


class LeadApplyRequest(BaseModel):
    name: str
    phone: str
    email: str
    gdpr_consent: bool
    answers: dict[str, str] = {}


@app.post("/leads/apply")
def submit_lead_application(req: LeadApplyRequest, request: Request):
    """Příjem odpovědí z náborové stránky (kvíz + kontakt). Appka je
    NIKAM neukládá do DB — jen je pošle appčinu vlastnímu Telegramu
    (TELEGRAM_CHAT_ID), stejně jako appka posílá upozornění na nové
    registrace. Bez GDPR souhlasu appka žádost odmítne rovnou na vstupu."""
    if not req.gdpr_consent:
        raise HTTPException(status_code=400, detail="Je potřeba souhlas se zpracováním osobních údajů.")
    if not req.name.strip() or not req.phone.strip() or not req.email.strip():
        raise HTTPException(status_code=400, detail="Chybí jméno, telefon nebo e-mail.")

    client_ip = _client_ip(request)
    if _lead_rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="Příliš mnoho odeslání, zkus to prosím později.")

    lines = [
        "📋 Nová poptávka z náborové stránky",
        f"Jméno: {req.name.strip()}",
        f"Telefon: {req.phone.strip()}",
        f"E-mail: {req.email.strip()}",
    ]
    for key, val in req.answers.items():
        lines.append(f"{key}: {val}")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id:
        try:
            _send_telegram_message(int(chat_id), "\n".join(lines))
        except Exception as e:
            print(f"[leads] Nepodařilo se poslat Telegram upozornění: {e}")

    try:
        email_service.send_lead_followup_email(req.email.strip(), req.name.strip())
    except Exception as e:
        print(f"[leads] Nepodařilo se poslat navazující e-mail: {e}")

    return {"status": "received"}


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


def _notify_owner_new_seller_application(user_id: int, seller_code: str, display_name: str) -> None:
    """Appka appce pošle upozornění na appčin vlastní Telegram
    (TELEGRAM_CHAT_ID), když se přes /seller/apply zaregistruje NOVÝ
    prodejce a čeká na schválení — appka to volá jen při první žádosti,
    ne při opakovaném volání se stejným účtem."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        return
    try:
        user = db.get_user_by_id(user_id)
        email = user["email"] if user else "?"
        _send_telegram_message(
            int(chat_id),
            f"🧑‍💼 Nová žádost o prodejce: {display_name} ({email})\n"
            f"Kód: {seller_code}\nSchval na apexsignal.cz/admin-prodejci",
        )
    except Exception as e:
        print(f"[seller/apply] Nepodařilo se poslat Telegram upozornění: {e}")


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
    db.set_registration_ip(user_id, client_ip)
    if req.device_seen:
        db.set_multi_account_flag(user_id, True)
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

    # Uvítací dárek (jen když appka výš zjistila platného doporučitele)
    # appka dá až PO ověření e-mailu (viz /auth/verify-email) — jinak by
    # šlo dokola zakládat účty s vymyšlenými e-maily jen kvůli
    # opakovanému dárku. Účet appka založí a přihlásí hned (appka nechce
    # novému uživateli blokovat přihlášení), jen generování zůstane bez
    # tokenů, dokud e-mail nepotvrdí (a nemá-li appka platný referral kód,
    # zůstane bez tokenů úplně — appka teď dává tokeny zdarma jen za
    # doporučení, ne za pouhou registraci).
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
    free_tokens_granted = False
    if not already_verified:
        # Appka dárek dá až tady (přesně jednou — token je jednorázový,
        # consume_email_verification_token ho hned označí jako použitý),
        # a JEN když se účet zaregistroval přes kamarádův kód/odkaz
        # (referred_by_user_id nastavené) — appka žádné tokeny zdarma jen
        # tak za ověření e-mailu nedává (2026-09-10, uživatelovo přání).
        try:
            if db.get_referred_by(user_id):
                db.adjust_tokens(user_id, REFERRAL_CODE_GIFT_TOKENS, "REFERRAL_CODE_GIFT")
                free_tokens_granted = True
        except Exception as e:
            print(f"[verify_email] Nepodařilo se přidat uvítací dárek: {e}")
    return {"status": "E-mail potvrzen", "free_tokens_granted": free_tokens_granted}


@app.get("/referral/my-code")
def get_my_referral_code(user_id: int = Depends(get_current_user_id)):
    code = db.get_or_create_referral_code(user_id)
    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal.cz")
    # Appka appce posílá odkaz na LANDING PAGE (ne rovnou appku samotnou),
    # ať cizí/skeptický návštěvník nejdřív appku pochopí, než ho appka
    # hodí rovnou na registraci — landing page appce ?ref= sama propíše
    # do všech tlačítek vedoucích do appky (viz index.html).
    return {"code": code, "link": f"{frontend_url}/?ref={code}"}


class TrackReferralClickRequest(BaseModel):
    code: str


@app.post("/referral/track-click")
def track_referral_click(req: TrackReferralClickRequest):
    """appka (2026-09-17, uživatelovo přání "appka neví, jestli sdílený
    odkaz vůbec někdo otevřel") — appka tohle volá landing page SAMA, hned
    při načtení stránky s ?ref=KÓD v URL, ať appka referrerovi umí ukázat
    nejen kolik lidí se ZAREGISTROVALO, ale i kolik jich odkaz OTEVŘELO
    (viz /referral/membership-progress → link_clicks). Záměrně bez
    přihlášení (návštěvník landing page se ještě nepřihlásil) a záměrně
    appka mlčky nic neudělá u neznámého kódu — appka to bere jako čistou
    analytiku, ne appka appce nechce přes tenhle endpoint dovolit
    zjišťovat, jaké kódy appka vůbec zná."""
    code = req.code.strip().upper()
    if code and db.get_user_id_by_referral_code(code) is not None:
        db.record_referral_link_click(code)
    return {"status": "ok"}


class DeclarationRequest(BaseModel):
    has_ico: bool
    ico: Optional[str] = None

    @field_validator("ico")
    @classmethod
    def validate_ico(cls, v: Optional[str], info) -> Optional[str]:
        has_ico = info.data.get("has_ico")
        v = (v or "").strip() or None
        if has_ico and not v:
            raise ValueError("Zadej IČO")
        if v and len(v) > 32:
            raise ValueError("IČO je moc dlouhé")
        return v


@app.post("/referral/accept-declaration")
def accept_referral_declaration(req: DeclarationRequest, user_id: int = Depends(get_current_user_id)):
    """
    Appka appce nesmí připsat ŽÁDNOU provizi za doporučení, dokud appka
    (referrer) tohle neodešle — viz REFERRAL_DECLARATION_TEXT_* a gate
    v _process_referral_membership_commission. Appka appce uloží PŘESNÝ
    text, co appka odsouhlasila (ne jen has_ico bool), ať appka má
    doklad k výplatě.
    """
    text = REFERRAL_DECLARATION_TEXT_HAS_ICO if req.has_ico else REFERRAL_DECLARATION_TEXT_NO_ICO
    db.submit_referral_declaration(user_id, req.has_ico, req.ico, text)
    return {"status": "accepted", "has_ico": req.has_ico}


@app.get("/referral/membership-progress")
def get_membership_referral_progress(user_id: int = Depends(get_current_user_id)):
    """Appka appce ukáže vydělané provize za doporučené placené členství
    (viz _process_referral_membership_commission) — kolik celkem, kolik
    už appka vyplatila (paid_out) a kolik ještě čeká na výplatu, plus
    seznam jednotlivých plateb, ať appka uživateli umí ukázat, KDO a za
    JAKÝ tarif mu vydělal."""
    totals = db.sum_referral_membership_earnings(user_id)
    earnings = db.get_referral_membership_earnings(user_id)
    invited = db.get_invited_summary(user_id)
    declaration = db.get_referral_declaration(user_id)
    referral_code = db.get_or_create_referral_code(user_id)
    return {
        "total_kc": totals["total_kc"],
        "paid_out_kc": totals["paid_out_kc"],
        "pending_kc": totals["pending_kc"],
        "commission_pct": REFERRAL_MEMBERSHIP_COMMISSION_PCT,
        "earnings": earnings,
        "has_pending_payout_request": db.has_pending_payout_request(user_id),
        "link_clicks": db.get_referral_link_click_count(referral_code),
        "registered_total": invited["registered_total"],
        "paying_total": invited["paying_total"],
        "active_now": invited["active_now"],
        "has_declaration": declaration is not None,
        "declaration_has_ico": declaration["has_ico"] if declaration else None,
        "declaration_ico": declaration["ico"] if declaration else None,
    }


class PayoutRequestRequest(BaseModel):
    full_name: str
    account_number: str
    ico: Optional[str] = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 255:
            raise ValueError("Zadej celé jméno a příjmení")
        return v

    @field_validator("account_number")
    @classmethod
    def validate_account_number(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 64:
            raise ValueError("Zadej platné číslo účtu")
        return v

    @field_validator("ico")
    @classmethod
    def validate_ico(cls, v: str) -> str:
        # Appka appce vyžaduje IČO kvůli faktuře/účetnictví na výplatu
        # (uživatelovo přání 2026-09-09: "potřebuji těch lidi IČO..jakoby
        # fakturu") — appka nekontroluje formát proti registru
        # ekonomických subjektů, jen appce nedovolí odeslat prázdné pole.
        v = v.strip()
        if not v or len(v) > 32:
            raise ValueError("Zadej IČO")
        return v


@app.post("/referral/request-payout")
def request_referral_payout(req: PayoutRequestRequest, user_id: int = Depends(get_current_user_id)):
    """
    Appka appce dovolí požádat o výplatu nasbírané provize za doporučení
    — appka zadá jméno a číslo účtu, appka appce založí žádost a hned
    pošle appce (uživateli appky, Davidovi) upozornění na Telegram s
    fakturačními údaji. Appka samotnou platbu NEPOSÍLÁ automaticky —
    appka appce jen zjednoduší, aby appka nemusela sama zjišťovat, kdo
    kolik chce a kam poslat (appka to appce pošle ručně bankovním
    převodem, pak appka appce označí přes /admin/referral-membership/
    payout-requests/{id}/mark-paid).
    """
    declaration = db.get_referral_declaration(user_id)
    if not declaration:
        raise HTTPException(status_code=400, detail="Nejdřív odešli čestné prohlášení k výplatě provize.")
    ico = (req.ico or "").strip() or declaration.get("ico")
    if declaration["has_ico"] and not ico:
        raise HTTPException(status_code=400, detail="Zadej IČO — ve svém prohlášení jsi uvedl, že IČO máš.")
    if db.has_pending_payout_request(user_id):
        raise HTTPException(status_code=409, detail="Už máš jednu žádost o výplatu čekající na vyřízení.")
    totals = db.sum_referral_membership_earnings(user_id)
    pending_kc = totals["pending_kc"]
    if pending_kc <= 0:
        raise HTTPException(status_code=400, detail="Nemáš žádnou nevyplacenou provizi.")

    request_id = db.create_payout_request(user_id, req.full_name, req.account_number, ico, pending_kc)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id:
        try:
            user = db.get_user_by_id(user_id)
            _send_telegram_message(
                int(chat_id),
                "💸 Nová žádost o výplatu provize za doporučení\n"
                f"Od: {user['email'] if user else user_id}\n"
                f"Jméno: {req.full_name}\n"
                f"IČO: {ico or '— (příležitostný příjem dle §10, appka nesráží daň)'}\n"
                f"Číslo účtu: {req.account_number}\n"
                f"Částka: {pending_kc} Kč\n"
                f"Žádost #{request_id}",
            )
        except Exception as e:
            print(f"[referral_payout] Nepodařilo se poslat Telegram upozornění: {e}")

    return {"status": "requested", "request_id": request_id, "requested_kc": pending_kc}


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
    ref: Optional[str] = None  # doporučovací kód z ?ref= — appka ho appce pošle jen při registraci, stejně jako /auth/register
    device_seen: Optional[bool] = None  # viz RegisterRequest.device_seen


@app.post("/auth/google", response_model=AuthResponse)
def google_auth(req: GoogleAuthRequest, request: Request):
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
        db.set_registration_ip(user_id, _client_ip(request))
        if req.device_seen:
            db.set_multi_account_flag(user_id, True)
        is_new_user = True
        # Doporučovací systém — appka referred_by nastaví jen TADY, při
        # registraci, stejně jako u /auth/register (viz tam). Neplatný/
        # neznámý kód appka jen tiše ignoruje.
        if req.ref:
            try:
                referrer_id = db.get_user_id_by_referral_code(req.ref)
                if referrer_id:
                    db.set_referred_by(user_id, referrer_id)
            except Exception as e:
                print(f"[google_auth] Nepodařilo se přiřadit doporučitele ({req.ref}): {e}")
        # Google appce v idinfo posílá vlastní "email_verified" příznak —
        # appka mu věří (appka o tomhle tokenu už výš ověřila podpis i
        # cílového klienta), takže tu appka nepotřebuje appky vlastní
        # ověřovací e-mail: Google účet appka ověří hned. Appka žádné
        # tokeny zdarma jen tak nedává — jen když se účet zaregistroval
        # přes kamarádův kód/odkaz (stejná podmínka jako /auth/verify-email).
        if idinfo.get("email_verified"):
            db.set_email_verified(user_id)
            if db.get_referred_by(user_id):
                try:
                    db.adjust_tokens(user_id, REFERRAL_CODE_GIFT_TOKENS, "REFERRAL_CODE_GIFT")
                except Exception as e:
                    print(f"[google_auth] Nepodařilo se přidat uvítací dárek: {e}")
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
    more_candidates_available: bool = False  # appka (2026-09-17) appce naznačí,
    # že po uložení tohohle tiketu ještě zbývají kandidáti na další —
    # appka to appce ukáže jako proaktivní nabídku "vygenerovat další"
    # (viz _run_generate_job), appka defaultuje na False, ať to endpointy,
    # co ho nepočítají (regenerate, replace-selection...), neřeší.


# =====================================================================
# Repository — tikety persistované v PostgreSQL (viz db.py).
# =====================================================================
class Repo:
    FLAT_STAKE_PCT = 2.0          # srovnávací vklad "rovných X % na každý tiket bez ohledu na Kelly"
    CALIBRATION_BUCKET_WIDTH_PCT = 10

    def __init__(self):
        self._last_batch_match_ids: dict[int, set[int]] = {}  # user_id -> match_ids ze VŠECH zatím nabídnutých (ne nutně uložených) tiketů od posledního uložení
        self._replace_selection_count: dict[int, int] = {}  # user_id -> kolikrát appka appce dovolila /tickets/replace-selection od posledního /tickets/generate (viz reset_replace_count)
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

    REPLACE_SELECTION_FREE_LIMIT = 1  # appka appce dovolí vyměnit nohu tiketu ZDARMA jen tolikrát
    # na jedno generování — appka bez týhle pojistky appce dovolila /tickets/replace-selection
    # volat neomezeně (appka to nikdy nezpoplatňovala, viz _charge_tokens_for_ticket), takže si
    # appka mohla postupně projet celý appčin denní pool zápasů bez zaplacení tokenů
    # (uživatelovo zjištění 2026-09-12).

    def reset_replace_count(self, user_id: int) -> None:
        self._replace_selection_count.pop(user_id, None)

    def try_consume_replace_selection(self, user_id: int) -> bool:
        """Appka appce vrátí True (a spotřebuje appčinu jednu výměnu zdarma),
        jen dokud appka nepřekročila REPLACE_SELECTION_FREE_LIMIT — appka appce
        vrátí False, když appka appce už appku vyčerpala."""
        used = self._replace_selection_count.get(user_id, 0)
        if used >= self.REPLACE_SELECTION_FREE_LIMIT:
            return False
        self._replace_selection_count[user_id] = used + 1
        return True


repo = Repo()
ticket_generator = TicketGenerator()


# =====================================================================
# Tokenový systém — viz ApexSignal Tokenomika & Tokenový Model. Stripe
# napojení přijde v dalším kroku; tahle appka zatím jen řídí zůstatek a
# uplatňování kódů (viz db.py: user_tokens/token_transactions/redeem_codes).
# =====================================================================
TOKEN_KC_VALUE = 30  # 1 token = 30 Kč — appka to appce i frontendu drží na jednom místě
TOKEN_COSTS = {"kratky": 10}  # 300 Kč při TOKEN_KC_VALUE=30 — appka BOOST i STŘEDNÍ přestala nabízet úplně

# Appka žádné tokeny zdarma jen za registraci/ověření e-mailu NEDÁVÁ
# (2026-09-10, uživatelovo přání: "Uplne bych zrusil strukturu ze nekdo
# dostane neco zdarma za tokeny" — appka dřív dávala FREE_TRIAL_TOKENS
# úplně každému, což šlo zneužít zakládáním e-mailů). Jediný způsob, jak
# appka dá tokeny zdarma, je REFERRAL_CODE_GIFT_TOKENS — jednorázový
# dárek tomu, kdo se zaregistruje přes kamarádův odkaz/kód (viz
# _redeem_referral_code), doporučitel sám nedostává nic (čistě
# jednosměrný dárek, uživatelovo přání). Fixní číslo místo
# TOKEN_COSTS["kratky"] přímo by appce mohlo tiše rozjet zkušební dárek,
# kdyby se cena krátkého tiketu někdy změnila — appka to chce takhle
# svázané schválně.
REFERRAL_CODE_GIFT_TOKENS = TOKEN_COSTS["kratky"]
TOKEN_PACKAGES = [12, 24, 60]  # předvolby k nákupu (v tokenech) — nejmenší pokryje aspoň 2 krátké tikety
MIN_CUSTOM_TOKENS = 1

# Provize za doporučení placeného ČLENSTVÍ (2026-09-09, uživatelovo
# přání — nahrazuje dřívější "slevu na měsíční členství", co fungovala
# jen pro referrery s vlastním aktivním měsíčním tarifem). Tenhle systém
# dá REÁLNÉ peníze úplně KAŽDÉMU referrerovi, ne jen tomu, kdo sám platí
# — a to při KAŽDÉ platbě kamaráda, i při každém dalším obnovení, ne jen
# první platbě (appka appce vede zůstatek v Kč, appka appku vyplácí ručně
# bankovním převodem, stejně jako u prodejců — viz
# referral_membership_earnings/record_referral_membership_earning).
# Odstupňováno podle appčina uvážení rizika (delší/dražší tarif appka
# odmění vyšším procentem — 2026-09-09, uživatel chtěl 50 % napříč, appka
# navrhla odstupňování kvůli nákladům, uživatel to přijal):
REFERRAL_MEMBERSHIP_COMMISSION_PCT: dict[str, float] = {
    "weekly": 0.30,
    "monthly": 0.50,
}

# Appka žádnou provizi za doporučení nesmí připsat, dokud referrer
# neodešle čestné prohlášení (2026-09-11, uživatelovo přání) — appka to
# vynucuje v _process_referral_membership_commission (žádná provize se
# ani nezaloží, appka to appce jasně řekne v appce, ať appka ví, proč jí
# "chybí" peníze). Appka appce ukládá CELÝ text, co appka odsouhlasila
# (ne jen checkbox), ať appka má doklad — appka appce dovolí prohlášení
# znovu odeslat (např. při doplnění IČO později).
REFERRAL_DECLARATION_TEXT_HAS_ICO = (
    "Čestně prohlašuji, že k výplatě provize za doporučení v appce ApexSignal mám platné IČO "
    "a na vyplácenou částku appce (David Novik, IČO 05010276) vystavím fakturu."
)
REFERRAL_DECLARATION_TEXT_NO_ICO = (
    "Čestně prohlašuji, že nemám IČO a provize za doporučení v appce ApexSignal je pro mě "
    "příležitostným příjmem dle § 10 zákona č. 586/1992 Sb., o daních z příjmů. Zavazuji se tento "
    "příjem sám přiznat a zdanit ve svém daňovém přiznání. Beru na vědomí, že ApexSignal "
    "(David Novik, IČO 05010276) z vyplacené částky nesráží ani neodvádí žádnou daň ani pojistné."
)
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
CHANNEL_PRICE_KC = 990

# =====================================================================
# Provizní systém pro prodejce — appka jim dá 3 pevné Stripe Payment
# Linky, jeden za tarif (týden/2 týdny/měsíc — appce nejde přednastavit
# libovolnou částku, viz debata s uživatelem). Prodejce pošle klientovi
# ten, na kterém se domluvili, i se svým seller_code v client_reference_id.
# Klíč je celá Kč částka, hodnota (appčin podíl, podíl prodejce) — obojí
# v Kč. Přeceněno z původních 500-3000 Kč (appka je nabízela jako "různé
# cenové hladiny", všechny ale byly technicky měsíční — uživatel
# 2026-08-25 chtěl skutečné odlišné fakturační období, viz SELLER_TIER_WEEKS
# níž) na 1500/2600/4000 Kč (týden/2 týdny/měsíc), stejná struktura jako
# appčin vlastní kanál. Provize je 50 % napříč VŠEMI tarify (uživatel
# 2026-08-26: "nech 50% vzdycky agentovi provizi" — dřív bylo 30 % u
# týdne/2 týdnů a jen měsíc měl 50 %, teď sjednoceno na 50 % všude).
# =====================================================================
SELLER_COMMISSION_TIERS: dict[int, tuple[int, int]] = {
    1500: (750, 750),    # 1 týden — 50 % prodejci
    2600: (1300, 1300),  # 2 týdny — 50 % prodejci
    4000: (2000, 2000),  # měsíc (4 týdny) — 50 % prodejci
}
# Kolik týdnů appka strhává za daný tarif — appka na to při vytváření
# Stripe Price appka nastaví recurring.interval="week"/interval_count
# podle tohohle čísla (měsíc appka appce zjednodušila na "4 týdny", ať
# appka drží jeden jednotný fakturační rytmus, ne měsíc+týden zvlášť).
SELLER_TIER_WEEKS: dict[int, int] = {1500: 1, 2600: 2, 4000: 4}
SELLER_PAYMENT_LINKS_SETTING_KEY = "seller_payment_links_v1"


def _ticket_type_for_risk_level(risk_level: int) -> str:
    """Appka řídí typ tiketu jen podle risk_level, stejně jako
    TicketGenerator.generate — appka musí znát typ (a tedy cenu v
    tokenech) JEŠTĚ PŘED samotným generováním, ať zbytečně neplýtvá API
    kvótou na tiket, který si uživatel stejně nemůže dovolit odemknout.
    Appka "stredni" přestala nabízet úplně (30denní ROI −37 %, viz
    historie) — celý rozsah pod BOOSTem appka teď staví jako kratky."""
    if risk_level <= 60:
        return "kratky"
    return "boost"


H2H_BLOWOUT_MARGIN = 3  # rozdíl gólů, od kterého appka vzájemný zápas počítá jako "blowout"
H2H_BLOWOUT_MIN_COUNT = 2  # appka (2026-09-16, uživatelovo přání "zmírnit")
                              # vyžaduje aspoň TOLIK blowoutů z posledních
                              # 6 vzájemných zápasů, ne jeden jediný — viz
                              # get_h2h_blowout_count. Jeden ojedinělý
                              # výbuch appce zbytečně zahazoval i jinak
                              # zdravé favority (dnešní příklad: Hapoel
                              # Tel Aviv i Rapid Vienna appka vyřadila jen
                              # kvůli jednomu starému výsledku), opakovaný
                              # vzorec (2+) ale pořád appce chytí přesně
                              # ten typ nevyrovnané dvojice, co appku
                              # 2026-09-05 praštil do očí (Waldhof
                              # Mannheim).


def _filter_h2h_volatile_candidates(pool: list[SelectionCandidate]) -> list[SelectionCandidate]:
    """
    Vyřadí MATCH_WINNER/DOUBLE_CHANCE kandidáty, jejichž poslední vzájemné
    zápasy obsahují ASPOŇ H2H_BLOWOUT_MIN_COUNT výrazných "blowoutů"
    (rozdíl gólů >= H2H_BLOWOUT_MARGIN) — přidáno 2026-09-05 po živém
    případu (Waldhof Mannheim 1X, model 75 %, ve skutečnosti prohráli):
    appka model počítá jen z aktuální formy/xG, ne z toho, jak nevyrovnaně
    tihle dva konkrétní týmy proti sobě historicky hráli (5:2, 1:3 vedle
    řady remíz) — takový zápas je nepředvídatelnější, než číslo samo
    ukazuje. Over góly/BTTS appka schválně nezahrnuje, tam volatilita
    skóre není na škodu.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    volatile_match_ids: dict[int, bool] = {}
    filtered = []
    for c in pool:
        if c.market_type not in (MarketType.MATCH_WINNER, MarketType.DOUBLE_CHANCE):
            filtered.append(c)
            continue
        if c.match_id not in volatile_match_ids:
            is_volatile = False
            try:
                fixture = provider.get_fixture_result(str(c.match_id))
                teams = fixture.get("teams", {})
                home_id, away_id = teams.get("home", {}).get("id"), teams.get("away", {}).get("id")
                if home_id and away_id:
                    blowout_count = provider.get_h2h_blowout_count(home_id, away_id, margin_threshold=H2H_BLOWOUT_MARGIN)
                    is_volatile = blowout_count is not None and blowout_count >= H2H_BLOWOUT_MIN_COUNT
            except Exception:
                is_volatile = False  # appka radši propustí kandidáta, než aby kvůli tomu selhalo celé generování
            volatile_match_ids[c.match_id] = is_volatile
        if not volatile_match_ids[c.match_id]:
            filtered.append(c)
    return filtered


def _pool_filter_for_risk(risk_level: int):
    """
    AI kontrola čerstvých zpráv (viz ai_reviewer.review_candidates) je
    zdaleka nejpomalejší krok generování — appka na ni čeká, protože
    prochází web pro KAŽDÉHO kandidáta. U krátkého a středního tiketu
    (nižší kurz, méně riskantní) appka tenhle krok přeskočí a spolehne
    se jen na statistický model — u BOOSTu (dlouhá kombinace, appka na
    ni neuplatňuje kontrolu kladného edge) je to naopak jediná pojistka
    proti zastaralým datům, tam kontrola zůstává.

    H2H volatilita (viz _filter_h2h_volatile_candidates) běží pro VŠECHNY
    risk_level — je levná (jen pro pár kandidátů, co už prošly prahem) a
    řeší jiný problém než AI kontrola zpráv.
    """
    if risk_level > 60:
        def combined(pool: list[SelectionCandidate]) -> list[SelectionCandidate]:
            return ai_reviewer.review_candidates(_filter_h2h_volatile_candidates(pool))
        return combined
    return _filter_h2h_volatile_candidates


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


def _redeem_referral_code(user_id: int, code: str) -> Optional[dict]:
    """
    Appka appce dá REFERRAL_CODE_GIFT_TOKENS, když appce zadá kamarádův
    unikátní referral kód — jednosměrný dárek (2026-09-10, uživatelovo
    přání), doporučitel sám nedostává nic. Appka appce vrátí None, když
    kód nepatří žádnému účtu, je appky vlastní, nebo appka už dřív kód
    od NĚKOHO uplatnila (db.set_referred_by appce dovolí nastavit
    referred_by jen JEDNOU za život účtu — appka na tom rovnou staví
    idempotenci, ať appka nemusí appce vést zvlášť další tabulku).
    """
    referrer_id = db.get_user_id_by_referral_code(code)
    if not referrer_id or referrer_id == user_id:
        return None
    if not db.set_referred_by(user_id, referrer_id):
        return None
    new_balance = db.adjust_tokens(user_id, REFERRAL_CODE_GIFT_TOKENS, "REFERRAL_CODE_GIFT")
    return {"tokens_granted": REFERRAL_CODE_GIFT_TOKENS, "new_balance": new_balance}


def _process_referral_membership_commission(
    referred_user_id: int, plan_type: str, payment_kc: int, stripe_ref: str,
) -> None:
    """
    Appka tohle volá při KAŽDÉ úspěšně zaplacené faktuře doporučeného
    kamarádova neomezeného generování — první platbě (checkout.session.
    completed) i každém dalším automatickém obnovení (invoice.payment_
    succeeded) — na rozdíl od jednorázového uvítacího dárku za kód
    (_redeem_referral_code, ten appka dá jen JEDNOU) tady appka referrerovi platí OPAKOVANĚ,
    dokud kamarád zůstává platícím členem (uživatelovo přání 2026-09-09).
    Idempotence appka nechává na DB (stripe_ref UNIQUE, viz
    record_referral_membership_earning) — appka appku klidně zavolá i
    vícekrát na stejnou platbu (např. retry webhooku), appka to prostě
    přeskočí. Best-effort, chyba tady nesmí shodit zpracování platby.
    """
    try:
        referrer_id = db.get_referred_by(referred_user_id)
        if not referrer_id:
            return
        if not db.has_referral_declaration(referrer_id):
            # Appka referrerovi žádnou provizi nepřipíše, dokud appka
            # neodešle čestné prohlášení (uživatelovo přání 2026-09-11) —
            # appka o tuhle platbu referrera prostě připraví, appka mu to
            # v appce jasně ukáže (viz has_declaration v
            # /referral/membership-progress) a řekne, co má udělat.
            return
        pct = REFERRAL_MEMBERSHIP_COMMISSION_PCT.get(plan_type)
        if not pct:
            return
        commission_kc = round(payment_kc * pct)
        is_new = db.record_referral_membership_earning(
            referrer_id, referred_user_id, stripe_ref, plan_type, payment_kc, pct, commission_kc,
        )
        if not is_new:
            return
        try:
            referrer = db.get_user_by_id(referrer_id)
            if referrer and referrer.get("email"):
                plan_label = "týdenní členství" if plan_type == "weekly" else "měsíční členství"
                email_service.send_referral_membership_commission_email(referrer["email"], commission_kc, plan_label)
        except Exception as e:
            print(f"[referral_membership] Notifikační e-mail se nepodařilo odeslat (referrer_id={referrer_id}): {e}")
    except Exception as e:
        print(f"[referral_membership] Zpracování provize selhalo (referred_user_id={referred_user_id}): {e}")


def _charge_tokens_for_ticket(user_id: int, ticket_type: str) -> None:
    # Neomezený tarif appka nestrhává v tokenech — appka si tenhle pokus
    # už započítala do denního stropu v _check_token_balance.
    if _has_active_unlimited(user_id):
        return
    cost = TOKEN_COSTS.get(ticket_type, 0)
    if cost > 0:
        db.adjust_tokens(user_id, -cost, f"UNLOCK_{ticket_type.upper()}")


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
        "unlimited_weekly_plans_kc": UNLIMITED_WEEKLY_PLANS,
        "unlimited_weekly_daily_cap": UNLIMITED_WEEKLY_DAILY_CAP,
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
            success_url=f"{frontend_url}/app/?payment=success",
            cancel_url=f"{frontend_url}/app/?payment=cancelled",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    return {"checkout_url": session.url}


UNLIMITED_GENERATION_DAILY_CAP = 10

# Neomezené generování — self-serve tarif pro běžné uživatele. Klíč =
# počet měsíců v jednom fakturačním cyklu, hodnota = celková cena v Kč
# za CELÉ období (ne za měsíc). Delší cykly (3/6/12 měsíců) appka
# 2026-09-07 na uživatelovo přání úplně zrušila — appka teď nabízí jen
# 1 týden (viz UNLIMITED_WEEKLY_PLANS) a 1 měsíc.
UNLIMITED_GENERATION_PLANS: dict[int, int] = {
    1: 9900,
}

# Týdenní varianta (2026-09-07, uživatelovo přání) — appka ji schválně
# cení NAD poměrnou týdenní cenou měsíčního tarifu (9900/4,33 ≈ 2287 Kč),
# ať se 4 týdny za sebou (4×2490 = 9960 Kč) nikdy nevyplatí víc než rovnou
# měsíc — týdenní má appku jen "ochutnat", ne nahradit měsíční závazek.
# Nižší denní strop (5 místo 10) je druhá záměrná brzda ze stejného
# důvodu — uživatel: "ten den ani víc jak 5 generování nejde asi appka
# víc tiketů nenajde", takže appka tím nikoho reálně neomezuje, jen
# jasně odlišuje tarify.
UNLIMITED_WEEKLY_PLANS: dict[int, int] = {
    1: 2490,
}
UNLIMITED_WEEKLY_DAILY_CAP = 5


class UnlimitedCheckoutRequest(BaseModel):
    months: int = 1
    weeks: Optional[int] = None

    @field_validator("months")
    @classmethod
    def validate_months(cls, v: int) -> int:
        if v not in UNLIMITED_GENERATION_PLANS:
            raise ValueError(f"Neplatná délka předplatného, appka umí jen: {sorted(UNLIMITED_GENERATION_PLANS)}")
        return v

    @field_validator("weeks")
    @classmethod
    def validate_weeks(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in UNLIMITED_WEEKLY_PLANS:
            raise ValueError(f"Neplatná délka týdenního předplatného, appka umí jen: {sorted(UNLIMITED_WEEKLY_PLANS)}")
        return v


@app.post("/payments/create-unlimited-checkout-session")
def create_unlimited_checkout_session(req: UnlimitedCheckoutRequest, user_id: int = Depends(get_current_user_id)):
    """Na rozdíl od nákupu tokenů tady appka záměrně nevolá
    _require_generation_enabled — tenhle nákup je přesně to, co má
    generování uživateli odemknout, takže by ho nemělo dávat smysl
    podmiňovat tím, že generování je už odemčené.

    mode="subscription" (ne jednorázová platba) — Stripe strhává platbu
    sám podle `interval_count` (1 týden nebo 1 měsíc). Datum konce appka
    nenastavuje napevno na +N dní, ale prodlužuje ho webhook
    (checkout.session.completed / invoice.payment_succeeded) podle
    skutečně zaplaceného období (`current_period_end`) — díky tomu appka
    nemusí nijak zvlášť ošetřovat delší cykly, webhook funguje beze změny."""
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    if req.weeks:
        weeks = req.weeks
        total_price_kc = UNLIMITED_WEEKLY_PLANS[weeks]
        product_name = "Neomezené generování na týden — ApexSignal"
        interval, interval_count = "week", weeks
        metadata = {
            "user_id": str(user_id), "unlimited_generation": "1", "weeks": str(weeks),
            # Webhook (checkout.session.completed i každé pozdější
            # invoice.payment_succeeded) tohle musí zopakovat appce zpátky
            # do set_unlimited_until — jinak by po prvním obnovení strop
            # tiše spadl na plný měsíční (viz db.set_unlimited_until).
            "daily_cap_override": str(UNLIMITED_WEEKLY_DAILY_CAP),
        }
    else:
        months = req.months
        total_price_kc = UNLIMITED_GENERATION_PLANS[months]
        product_name = "Neomezené generování na měsíc — ApexSignal"
        interval, interval_count = "month", months
        metadata = {"user_id": str(user_id), "unlimited_generation": "1", "months": str(months)}

    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "czk",
                    "product_data": {"name": product_name},
                    "unit_amount": total_price_kc * 100,
                    "recurring": {"interval": interval, "interval_count": interval_count},
                },
                "quantity": 1,
            }],
            metadata=metadata,
            # Metadata appka musí zopakovat i sem — appka jinak zůstane jen
            # na Checkout Session, ale pozdější webhooky (obnovení,
            # zrušení) appce posílají přímo objekt Subscription, ne Session.
            subscription_data={"metadata": metadata},
            success_url=f"{frontend_url}/app/?payment=success",
            cancel_url=f"{frontend_url}/app/?payment=cancelled",
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
            return_url=os.environ.get("APP_URL", "https://apexsignal.cz/app/"),
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
    #   2) Neomezené generování (2490 Kč/týden nebo 9900 Kč/měsíc, viz
    #      UNLIMITED_WEEKLY_PLANS/UNLIMITED_GENERATION_PLANS) — přes
    #      /payments/create-unlimited-checkout-session, vázané na přihlášený user_id
    #      appky (metadata.unlimited_generation == "1"), appka drží
    #      stav přímo na users.unlimited_until.
    # Appka je rozlišuje podle metadata.unlimited_generation, ne podle
    # mode — obě appka teď mají mode="subscription".
    if event_type == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        user_id = int(metadata.get("user_id", 0)) or None
        seller_code = (obj.get("client_reference_id") or "").strip()

        if seller_code and (seller := db.get_seller_by_code(seller_code)):
            # Prodejecká platba (viz SELLER_COMMISSION_TIERS) — appka ji
            # pozná podle client_reference_id, appka ji MUSÍ odchytit dřív
            # než větev pro appčin vlastní kanál níž, protože obě appka
            # posílá jako mode="subscription" přes pevný Payment Link.
            amount_total_kc = (obj.get("amount_total") or 0) // 100
            tier = SELLER_COMMISSION_TIERS.get(amount_total_kc)
            if not tier:
                print(f"[stripe] Prodejecká platba s neznámou částkou {amount_total_kc} Kč (session {obj.get('id')})")
            else:
                our_cut_kc, seller_cut_kc = tier
                email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
                is_new = db.record_seller_earning(
                    seller["id"], obj["id"], email, amount_total_kc, our_cut_kc, seller_cut_kc,
                )
                # Appka appce hned zapíše i ŽIVÝ stav předplatného (kdy
                # končí, čí je) — appka to appce potřebuje, aby uměla na
                # invoice.payment_succeeded/customer.subscription.* níž
                # poznat, že jde o prodejcova klienta, a zapsat mu i
                # provizi za každé DALŠÍ obnovení, ne jen za tuhle první
                # platbu (nahlásil uživatel 2026-08-26 — appka dřív
                # obnovení vůbec neřešila).
                stripe_subscription_id = obj.get("subscription")
                if stripe_subscription_id:
                    period_end = None
                    try:
                        sub = stripe.Subscription.retrieve(stripe_subscription_id)
                        period_end = _period_end_from_subscription(sub)
                    except Exception as e:
                        print(f"[stripe] nepodařilo se načíst prodejcovo předplatné {stripe_subscription_id}: {e}")
                    db.upsert_seller_subscription(
                        stripe_subscription_id, seller["id"], email, amount_total_kc,
                        our_cut_kc, seller_cut_kc, "active", period_end,
                    )
                if is_new and seller.get("telegram_chat_id"):
                    try:
                        _send_telegram_message(
                            seller["telegram_chat_id"],
                            f"✅ Nová platba!\n{email or 'e-mail neznámý'} zaplatil {amount_total_kc} Kč.\n"
                            f"Tvůj podíl: {seller_cut_kc} Kč.\n\nNezapomeň ho přidat do svého kanálu.",
                        )
                    except Exception as e:
                        print(f"[stripe] Nepodařilo se poslat Telegram notifikaci prodejci {seller['id']}: {e}")
        elif user_id and metadata.get("unlimited_generation") == "1":
            stripe_subscription_id = obj.get("subscription")
            period_end = None
            if stripe_subscription_id:
                try:
                    sub = stripe.Subscription.retrieve(stripe_subscription_id)
                    period_end = _period_end_from_subscription(sub)
                except Exception as e:
                    print(f"[stripe] nepodařilo se načíst nové neomezené předplatné {stripe_subscription_id}: {e}")
            raw_cap = metadata.get("daily_cap_override")
            db.set_unlimited_until(
                user_id,
                period_end or datetime.now(timezone.utc) + timedelta(days=30),
                stripe_customer_id=obj.get("customer"),
                daily_cap_override=int(raw_cap) if raw_cap else None,
            )
            plan_type = "weekly" if metadata.get("weeks") else "monthly"
            amount_paid_kc = (obj.get("amount_total") or 0) // 100
            _process_referral_membership_commission(user_id, plan_type, amount_paid_kc, obj["id"])
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

    elif event_type.startswith("customer.subscription."):
        sub_metadata = obj.get("metadata") or {}
        seller_sub = db.get_seller_subscription(obj["id"])
        if seller_sub is not None:
            # Prodejcův klient appka MUSÍ odchytit dřív než větve pro
            # appčino vlastní neomezené generování/kanál níž — appka na
            # 'deleted' appce zapíše status 'canceled', ať appka umí
            # klientovi na dashboardu ukázat, že už neplatí, ale appka mu
            # (stejně jako u neomezeného appky) přístup neutne uprostřed
            # zaplaceného období.
            db.upsert_seller_subscription(
                obj["id"], seller_sub["seller_id"], seller_sub["client_email"],
                seller_sub["tier_price_kc"], seller_sub["our_cut_kc"], seller_sub["seller_cut_kc"],
                obj.get("status", "canceled"), _period_end_from_subscription(obj),
            )
        elif sub_metadata.get("unlimited_generation") == "1":
            # Obnovení appka řeší přes invoice.payment_succeeded níž (tam
            # appka ví jistě, že platba prošla) — tady appka jen zrcadlí
            # aktivní stav, kdyby přišel dřív. Zrušení appka neřeší
            # zkrácením — přístup nechá doběhnout do konce už zaplaceného
            # období, neutne ho hned.
            sub_user_id = int(sub_metadata.get("user_id", 0)) or None
            if sub_user_id and obj.get("status") in ("active", "trialing"):
                period_end = _period_end_from_subscription(obj)
                raw_cap = sub_metadata.get("daily_cap_override")
                if period_end:
                    db.set_unlimited_until(
                        sub_user_id, period_end, stripe_customer_id=obj.get("customer"),
                        daily_cap_override=int(raw_cap) if raw_cap else None,
                    )
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
            seller_sub = db.get_seller_subscription(subscription_id) if subscription_id else None
            if seller_sub is not None:
                # Prodejcův klient appka MUSÍ odchytit dřív než appčiny
                # vlastní branche níž. billing_reason appka appce
                # rozliší, jestli je tahle faktura ta úplně PRVNÍ (appka
                # ji už zapsala výš v checkout.session.completed —
                # dvakrát appka stejnou platbu nezapíše, ale radši se
                # tomu appka vyhne rovnou) nebo skutečné automatické
                # OBNOVENÍ (subscription_cycle) — jen za to appka appce
                # zapíše novou provizi (uživatelovo přání 2026-08-26,
                # appka dřív žádnou provizi za obnovení nezapisovala).
                billing_reason = obj.get("billing_reason")
                if event_type == "invoice.payment_succeeded" and billing_reason != "subscription_create":
                    invoice_id = obj.get("id")
                    is_new = db.record_seller_renewal_earning(
                        seller_sub["seller_id"], invoice_id, seller_sub["client_email"],
                        seller_sub["tier_price_kc"], seller_sub["our_cut_kc"], seller_sub["seller_cut_kc"],
                    )
                    if is_new:
                        try:
                            with db.get_cursor() as cur:
                                cur.execute("SELECT telegram_chat_id FROM sellers WHERE id = %s", (seller_sub["seller_id"],))
                                row = cur.fetchone()
                            if row and row.get("telegram_chat_id"):
                                _send_telegram_message(
                                    row["telegram_chat_id"],
                                    f"✅ Obnovené předplatné!\n{seller_sub['client_email'] or 'e-mail neznámý'} "
                                    f"zaplatil {seller_sub['tier_price_kc']} Kč.\nTvůj podíl: {seller_sub['seller_cut_kc']} Kč.",
                                )
                        except Exception as e:
                            print(f"[stripe] Nepodařilo se poslat Telegram notifikaci o obnovení prodejci {seller_sub['seller_id']}: {e}")
                db.upsert_seller_subscription(
                    subscription_id, seller_sub["seller_id"], seller_sub["client_email"],
                    seller_sub["tier_price_kc"], seller_sub["our_cut_kc"], seller_sub["seller_cut_kc"],
                    sub.get("status", "active") if sub is not None else "active",
                    _period_end_from_subscription(sub) if sub is not None else None,
                )
            elif sub is not None:
                sub_metadata = sub.get("metadata") or {}
                if sub_metadata.get("unlimited_generation") == "1":
                    # Tohle je ten skutečný měsíční obnovovací moment —
                    # každá úspěšná faktura prodlouží unlimited_until na
                    # nové zaplacené období.
                    if event_type == "invoice.payment_succeeded":
                        inv_user_id = int(sub_metadata.get("user_id", 0)) or None
                        period_end = _period_end_from_subscription(sub)
                        raw_cap = sub_metadata.get("daily_cap_override")
                        if inv_user_id and period_end:
                            db.set_unlimited_until(
                                inv_user_id, period_end, stripe_customer_id=sub.get("customer"),
                                daily_cap_override=int(raw_cap) if raw_cap else None,
                            )
                        # Provize za doporučení appka zapíše jen na SKUTEČNÉ
                        # obnovení (billing_reason != subscription_create) —
                        # tu úplně první platbu appka už odbavila výš v
                        # checkout.session.completed, jinak by appka
                        # doporučiteli připsala dvojnásobek za jednu platbu.
                        if inv_user_id and obj.get("billing_reason") != "subscription_create":
                            plan_type = "weekly" if sub_metadata.get("weeks") else "monthly"
                            amount_paid_kc = (obj.get("amount_paid") or 0) // 100
                            _process_referral_membership_commission(inv_user_id, plan_type, amount_paid_kc, obj["id"])
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
    if result["ok"]:
        return result
    # Appky admin kódy (redeem_code výš) a kamarádovy OSOBNÍ referral
    # kódy (get_or_create_referral_code) appka schválně drží ve stejném
    # poli "Zadej kód" — appka to appce zkusí jako druhou možnost, až
    # když první selže, ať appka nemusí appce na frontendu dělat dvě
    # oddělená pole. Viz _redeem_referral_code.
    referral_result = _redeem_referral_code(user_id, code)
    if referral_result:
        return {
            "ok": True, "tokens": referral_result["tokens_granted"], "new_balance": referral_result["new_balance"],
            "message": f"Získal jsi {referral_result['tokens_granted']} tokenů od kamaráda!",
        }
    raise HTTPException(status_code=400, detail=result["error"])


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


@app.post("/admin/sellers/create-payment-links")
def admin_create_seller_payment_links(request: Request):
    """
    Appka JEDNORÁZOVĚ vytvoří 3 pevné Stripe Payment Linky (podle
    SELLER_COMMISSION_TIERS — týden/2 týdny/měsíc) sdílené VŠEMI
    prodejci — appka je nerozlišuje samostatnými odkazy na prodejce,
    ale přes client_reference_id, který si každý prodejce připojí do
    URL sám (viz GET /seller/dashboard). Appka výsledné URL uloží do
    app_settings, ať tohle nemusí volat podruhé — volání appku
    PŘEPÍŠE staré odkazy novými (SELLER_PAYMENT_LINKS_SETTING_KEY drží
    jen poslední sadu).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    frontend_url = os.environ.get("FRONTEND_URL", "https://apexsignal-tickets.netlify.app")
    links: dict[str, str] = {}
    try:
        for tier_kc in SELLER_COMMISSION_TIERS:
            weeks = SELLER_TIER_WEEKS.get(tier_kc, 4)
            label = "týden" if weeks == 1 else ("měsíc" if weeks == 4 else f"{weeks} týdny")
            price = stripe.Price.create(
                currency="czk",
                unit_amount=tier_kc * 100,
                recurring={"interval": "week", "interval_count": weeks},
                product_data={"name": f"ApexSignal — prodejcem doporučený přístup ({tier_kc} Kč / {label})"},
            )
            payment_link = stripe.PaymentLink.create(
                line_items=[{"price": price.id, "quantity": 1}],
                after_completion={"type": "redirect", "redirect": {"url": f"{frontend_url}/?payment=success"}},
            )
            links[str(tier_kc)] = payment_link.url
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    db.set_setting(SELLER_PAYMENT_LINKS_SETTING_KEY, json.dumps(links))
    return {"links": links}


@app.get("/admin/sellers/check-payment-links")
def admin_check_seller_payment_links(request: Request):
    """Diagnostika appka appce se přímo zeptá Stripe, jakou cenu má
    KAŽDÝ uložený prodejecký odkaz reálně nastavenou — appka appce to
    potřebuje po hlášení appky (uživatel), že klik na 1500 Kč appce
    otevřel platbu na 4000 Kč. Read-only, nic nemění."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    raw_links = db.get_setting(SELLER_PAYMENT_LINKS_SETTING_KEY)
    stored_links = json.loads(raw_links) if raw_links else {}

    # Stripe.PaymentLink.retrieve() bere API ID (plink_...), ne veřejný
    # URL slug (co appka ukládá appce do payment_links) — appka proto
    # musí projít celý appčin seznam Payment Linků a spárovat podle URL.
    try:
        all_links = stripe.PaymentLink.list(limit=100, expand=["data.line_items.data.price"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stripe chyba: {e}")

    by_url = {pl["url"]: pl for pl in all_links["data"]}

    results = []
    for tier_kc, url in stored_links.items():
        entry = {"expected_tier_kc": tier_kc, "url": url}
        pl = by_url.get(url)
        if pl is None:
            entry["error"] = "Tenhle odkaz appka v appčině Stripe účtu nenašla vůbec (smazaný/jiný účet?)."
        else:
            entry["active"] = pl.get("active")
            entry["line_items"] = [
                {
                    "price_id": it["price"]["id"],
                    "unit_amount_kc": it["price"]["unit_amount"] / 100 if it["price"].get("unit_amount") else None,
                    "currency": it["price"]["currency"],
                    "recurring": it["price"].get("recurring"),
                    "product": it["price"].get("product"),
                }
                for it in pl["line_items"]["data"]
            ]
        results.append(entry)

    return {"links": results}


class CreateSellerRequest(BaseModel):
    email: str
    display_name: str


@app.post("/admin/sellers/create")
def admin_create_seller(req: CreateSellerRequest, request: Request):
    """Appka appce označí existující účet (musí se už dřív zaregistrovat
    normálně přes appku) jako prodejce a přidělí mu seller_code."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    user = db.get_user_by_email(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="Účet s tímhle e-mailem appka nenašla — musí se nejdřív zaregistrovat.")

    seller_code = secrets.token_hex(4)
    seller_id = db.create_seller(user["id"], seller_code, req.display_name.strip())
    return {"seller_id": seller_id, "seller_code": seller_code, "display_name": req.display_name.strip()}


class SellerApplyRequest(BaseModel):
    display_name: str


@app.post("/seller/apply")
def seller_apply(req: SellerApplyRequest, user_id: int = Depends(get_current_user_id)):
    """Samoobslužná varianta /admin/sellers/create — přihlášený uživatel
    appce založí prodejecký účet sám, appka nemusí volat admin příkaz
    ručně za appku při každém novém prodejci. display_name appka zatím
    neověřuje (appka to zobrazí jen v appčině dashboardu jemu samotnému,
    nikde veřejně) — appka je nechá měnit i opakovaným voláním."""
    display_name = req.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Chybí jméno.")
    if len(display_name) > 120:
        raise HTTPException(status_code=400, detail="Jméno je moc dlouhé.")

    existing = db.get_seller_by_user_id_any(user_id)
    seller_code = existing["seller_code"] if existing else secrets.token_hex(4)
    # Nový prodejce čeká na ruční schválení appkou (viz db.create_seller);
    # ON CONFLICT appka nemění active, takže tenhle default appka platí
    # jen při první registraci.
    seller_id = db.create_seller(user_id, seller_code, display_name, active=False)
    is_approved = bool(existing and existing.get("active"))
    if not existing:
        _notify_owner_new_seller_application(user_id, seller_code, display_name)
    return {
        "seller_id": seller_id,
        "seller_code": seller_code,
        "display_name": display_name,
        "approved": is_approved,
    }


@app.get("/seller/dashboard")
def seller_dashboard(user_id: int = Depends(get_current_user_id)):
    """Appka sem prodejce pustí, jen když appka jeho user_id má
    zapsané v sellers — jinak 403. Appka appce vrátí jeho osobní odkazy
    (appčiny sdílené Payment Linky + jeho vlastní client_reference_id)
    a celou historii jeho zaznamenaných plateb i součet provize."""
    seller = db.get_seller_by_user_id_any(user_id)
    if not seller:
        raise HTTPException(status_code=403, detail="Tenhle účet není appce registrovaný jako prodejce.")
    if not seller.get("active"):
        # Čeká na ruční schválení (viz /admin/sellers/approve) — appka
        # zatím nedává odkazy ani provizní data, jen stav čekání.
        return {
            "approved": False,
            "display_name": seller["display_name"],
            "seller_code": seller["seller_code"],
        }

    raw_links = db.get_setting(SELLER_PAYMENT_LINKS_SETTING_KEY)
    base_links = json.loads(raw_links) if raw_links else {}
    my_links = {
        tier_kc: f"{url}?client_reference_id={seller['seller_code']}"
        for tier_kc, url in base_links.items()
    }

    earnings = db.get_seller_earnings(seller["id"])
    total_seller_kc = sum(e["seller_cut_kc"] for e in earnings)
    # total_clients appka počítá z PŘEDPLATNÝCH, ne z jednotlivých plateb —
    # jeden klient appce teď může vygenerovat víc řádků v earnings (první
    # platba + každé obnovení), takže len(earnings) by appce klienty
    # zdvojoval, jakmile appka někomu obnovilo předplatné (viz oprava
    # 2026-08-26).
    subscriptions = db.list_seller_subscriptions(seller["id"])
    total_clients = len(subscriptions)

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@")
    telegram_link_url = f"https://t.me/{bot_username}?start=seller_{seller['seller_code']}" if bot_username else None

    return {
        "approved": True,
        "seller_code": seller["seller_code"],
        "display_name": seller["display_name"],
        "payment_links": my_links,
        "tier_weeks": SELLER_TIER_WEEKS,
        "tier_commission_kc": {tier_kc: seller_cut for tier_kc, (_, seller_cut) in SELLER_COMMISSION_TIERS.items()},
        "telegram_linked": bool(seller.get("telegram_chat_id")),
        "telegram_link_url": telegram_link_url,
        # "Obohacení" appčina základního modelu (tikety + provize) — když
        # má prodejce navíc appčino existující Neomezené generování,
        # appka mu dovolí generovat si tikety sám, ne jen dostávat appčiny.
        "can_self_generate": _has_active_unlimited(user_id),
        "total_clients": total_clients,
        "total_earned_kc": total_seller_kc,
        "clients": [
            {
                "client_email": s["client_email"],
                "tier_price_kc": s["tier_price_kc"],
                "status": s["status"],
                "current_period_end": s["current_period_end"].isoformat() if s["current_period_end"] else None,
            }
            for s in subscriptions
        ],
        "earnings": [
            {
                "client_email": e["client_email"],
                "tier_price_kc": e["tier_price_kc"],
                "seller_cut_kc": e["seller_cut_kc"],
                "paid_at": e["paid_at"].isoformat() if e["paid_at"] else None,
            }
            for e in earnings
        ],
    }


@app.get("/admin/sellers/overview")
def admin_sellers_overview(request: Request):
    """Appka appce (uživateli) ukáže VŠECHNY aktivní prodejce najednou —
    kolik jich appka má, kdo je aktivní (propojený Telegram), kolik
    klientů kdo přivedl a kolik appka komu dluží — na rozdíl od
    /seller/dashboard, co ukáže jen appce přihlášenému JEDNOMU prodejci
    jeho vlastní čísla. Vzniklo 2026-08-26 na uživatelovo přání, appka to
    potřebuje dřív, než nábor agentů poroste nad pár lidí sledovaných
    ručně."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    sellers = db.list_sellers_overview()
    total_sellers_kc = sum(s["total_seller_kc"] for s in sellers)
    total_our_kc = sum(s["total_our_kc"] for s in sellers)
    total_clients = sum(s["total_clients"] for s in sellers)
    active_clients = sum(s["active_clients"] for s in sellers)

    subs = db.list_all_seller_client_subscriptions()

    return {
        "seller_count": len(sellers),
        "total_clients": total_clients,
        "active_clients": active_clients,
        "total_seller_kc": total_sellers_kc,
        "total_our_kc": total_our_kc,
        "sellers": [
            {
                "seller_code": s["seller_code"],
                "display_name": s["display_name"],
                "email": s["email"],
                "telegram_linked": s["telegram_chat_id"] is not None,
                "total_clients": s["total_clients"],
                "active_clients": s["active_clients"],
                "total_seller_kc": s["total_seller_kc"],
                "total_our_kc": s["total_our_kc"],
                "created_at": s["created_at"].isoformat() if s["created_at"] else None,
                "last_paid_at": s["last_paid_at"].isoformat() if s["last_paid_at"] else None,
            }
            for s in sellers
        ],
        # Appka appce (adminovi) rovnou ukáže, komu nejdřív skončí
        # předplatné — seřazeno appkou už v db.list_all_seller_client_
        # subscriptions (nejbližší konec první), appka na frontendu nic
        # netřídí znovu.
        "clients": [
            {
                "seller_code": c["seller_code"],
                "seller_name": c["seller_name"],
                "client_email": c["client_email"],
                "tier_price_kc": c["tier_price_kc"],
                "status": c["status"],
                "current_period_end": c["current_period_end"].isoformat() if c["current_period_end"] else None,
            }
            for c in subs
        ],
        "pending": [
            {
                "seller_code": p["seller_code"],
                "display_name": p["display_name"],
                "email": p["email"],
                "created_at": p["created_at"].isoformat() if p["created_at"] else None,
            }
            for p in db.list_pending_sellers()
        ],
        "leads": [
            {
                "id": l["id"],
                "full_name": l["full_name"],
                "age": l["age"],
                "city": l["city"],
                "experience": l["experience"],
                "start_when": l["start_when"],
                "income_goal": l["income_goal"],
                "can_work_online": l["can_work_online"],
                "contact": l["contact"],
                "phone": l["phone"],
                "created_at": l["created_at"].isoformat() if l["created_at"] else None,
            }
            for l in db.list_seller_leads()
        ],
    }


@app.get("/admin/referral-suspicious-ip-matches")
def admin_referral_suspicious_ip_matches(request: Request):
    """Appka appce (adminovi) ukáže páry referrer→doporučený, co appka
    zaregistrovala ze STEJNÉ IP adresy — čistě informativní (viz
    db.list_suspicious_referral_ip_matches), appka na tom nic
    neblokuje. Appka to appce dá appce jen jako podnět k ruční kontrole."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")
    return {
        "matches": db.list_suspicious_referral_ip_matches(),
        "multi_account_flagged": db.list_multi_account_flagged_users(),
    }


@app.get("/admin/referral-membership/overview")
def admin_referral_membership_overview(request: Request):
    """Appka appce (adminovi) ukáže VŠECHNY referrery, co appce vydělali
    provizi za doporučené placené členství — kolik komu appka dluží
    (pending_kc) a kolik už vyplatila, stejný vzor jako
    /admin/sellers/overview. Appka vyplácí ručně, tenhle přehled je jen
    pro appku (uživatele), ať ví, komu poslat peníze."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    overview = db.list_referral_membership_overview()
    earnings = db.list_all_referral_membership_earnings()
    return {
        "referrer_count": len(overview),
        "total_referred": sum(r["total_referred"] or 0 for r in overview),
        "total_paying_referred": sum(r["paying_referred"] or 0 for r in overview),
        "total_pending_kc": sum(r["pending_kc"] or 0 for r in overview),
        "total_paid_out_kc": sum(r["paid_out_kc"] or 0 for r in overview),
        "referrers": [
            {
                "referrer_user_id": r["referrer_user_id"],
                "email": r["email"],
                "total_referred": r["total_referred"] or 0,
                "paying_referred": r["paying_referred"] or 0,
                "total_payments": r["total_payments"],
                "total_kc": r["total_kc"],
                "paid_out_kc": r["paid_out_kc"] or 0,
                "pending_kc": r["pending_kc"] or 0,
            }
            for r in overview
        ],
        "earnings": [
            {
                "referrer_email": e["referrer_email"],
                "referred_email": e["referred_email"],
                "plan_type": e["plan_type"],
                "payment_kc": e["payment_kc"],
                "commission_kc": e["commission_kc"],
                "paid_out": e["paid_out"],
                "created_at": e["created_at"].isoformat() if e["created_at"] else None,
            }
            for e in earnings
        ],
    }


@app.post("/admin/referral-membership/mark-paid")
def admin_referral_membership_mark_paid(referrer_user_id: int, request: Request):
    """Appka appce (adminovi) označí VŠECHNY nevyplacené provize daného
    referrera jako vyplacené — appka tohle zavolá AŽ PO tom, co reálně
    pošle peníze bankovním převodem (appka sama nic nevyplácí)."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    paid_kc = db.mark_referral_membership_paid(referrer_user_id)
    return {"referrer_user_id": referrer_user_id, "marked_paid_kc": paid_kc}


@app.get("/admin/referral-membership/payout-requests")
def admin_list_payout_requests(request: Request, status: Optional[str] = None):
    """Appka appce (adminovi) ukáže žádosti o výplatu provize — appka
    appce defaultně vrátí VŠECHNY (nejnovější první), appka appce nechá
    filtrovat přes ?status=pending, ať appka vidí jen to, co ještě čeká
    na vyřízení."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    requests_ = db.list_payout_requests(status=status)
    return {
        "requests": [
            {
                "id": r["id"], "referrer_user_id": r["referrer_user_id"], "email": r["email"],
                "full_name": r["full_name"], "account_number": r["account_number"], "ico": r.get("ico"),
                "requested_kc": r["requested_kc"], "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None,
            }
            for r in requests_
        ],
    }


class ManualDeclarationRequest(BaseModel):
    email: str
    has_ico: bool
    ico: Optional[str] = None
    note: str = ""


@app.post("/admin/referral/manual-declaration")
def admin_set_manual_declaration(req: ManualDeclarationRequest, request: Request):
    """Appka appce (adminovi) dovolí ručně zapsat čestné prohlášení za
    referrera, co NENÍ daňový rezident ČR (appka mu v appce standardní
    formulář se zákonem §10 vůbec nenabídne, appka appce místo toho pošle
    e-mail a appka to s ním vyřeší individuálně mimo appku) — appka
    appce použije stejnou tabulku (referral_payout_declarations), takže
    appce se pak commission gate v _process_referral_membership_commission
    chová úplně stejně, appka jen appce text prohlášení nahradí appčinou
    poznámkou o tom, jak to bylo dohodnuté."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    user = db.get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=404, detail="Uživatel s tímhle e-mailem nenalezen")
    if req.has_ico and not (req.ico or "").strip():
        raise HTTPException(status_code=400, detail="Zadej IČO")

    text = "Ručně zpracováno adminem — referrer mimo ČR / individuální dohoda."
    if req.note.strip():
        text += f" Poznámka: {req.note.strip()}"
    db.submit_referral_declaration(user["id"], req.has_ico, (req.ico or "").strip() or None, text)
    return {"status": "ok", "user_id": user["id"]}


@app.post("/admin/referral-membership/payout-requests/{request_id}/mark-paid")
def admin_mark_payout_request_paid(request_id: int, request: Request):
    """Appka appce (adminovi) označí KONKRÉTNÍ žádost o výplatu jako
    vyplacenou — appka zavolá AŽ PO tom, co appka reálně pošle peníze
    bankovním převodem. Appka appce zároveň označí i podkladové
    jednotlivé provize (referral_membership_earnings) jako paid_out, ať
    appka nezůstane v nekonzistentním stavu (viz db.mark_payout_request_paid)."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    result = db.mark_payout_request_paid(request_id)
    if not result:
        raise HTTPException(status_code=404, detail="Žádost nenalezena nebo už byla vyřízena")
    return {"request_id": request_id, "referrer_user_id": result["referrer_user_id"], "paid_kc": result["requested_kc"]}


@app.get("/admin/channel-subscribers")
def admin_channel_subscribers(request: Request):
    """Appka appce (adminovi) ukáže VŠECHNY přímé předplatitele Telegram
    kanálu (990 Kč/měsíc), seřazené podle toho, komu nejdřív skončí
    předplatné — dřív appka tohle měla jen pro klienty přivedené přes
    prodejce (/admin/sellers/overview), pro přímé odběratele appka
    žádný takový přehled neměla."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    subs = db.list_channel_subscriptions()
    return {
        "subscribers": [
            {
                "email": s["email"],
                "status": s["status"],
                "current_period_end": s["current_period_end"].isoformat() if s["current_period_end"] else None,
                "created_at": s["created_at"].isoformat() if s["created_at"] else None,
            }
            for s in subs
        ],
    }


class SellerApproveRequest(BaseModel):
    seller_code: str
    approve: bool


@app.post("/admin/sellers/approve")
def admin_sellers_approve(req: SellerApproveRequest, request: Request):
    """Zapíná/vypíná prodejce — schvaluje nové registrace z /seller/apply
    (viz db.create_seller — nový prodejce tam vzniká s active=False,
    dokud ho tenhle endpoint neschválí)."""
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    seller_code = req.seller_code.strip()
    ok = db.set_seller_active(seller_code, req.approve)
    if not ok:
        raise HTTPException(status_code=404, detail="Prodejce s tímhle kódem appka nenašla.")

    if req.approve:
        try:
            seller = db.get_seller_by_code_any(seller_code)
            if seller and seller.get("user_email"):
                email_service.send_seller_approved_email(seller["user_email"], "https://apexsignal.cz/prodejce")
        except Exception as e:
            print(f"[sellers/approve] Nepodařilo se poslat e-mail o schválení: {e}")

    return {"seller_code": seller_code, "approved": req.approve}


class SellerLeadRequest(BaseModel):
    full_name: str
    age: Optional[int] = None
    city: Optional[str] = None
    experience: Optional[str] = None
    start_when: Optional[str] = None
    income_goal: Optional[str] = None
    can_work_online: Optional[bool] = None
    contact: str
    phone: Optional[str] = None


@app.post("/leads/seller-application")
def submit_seller_lead(req: SellerLeadRequest):
    """Veřejný náborový formulář (/kariera) — schválně nepožaduje
    přihlášení, zájemce ještě nemusí mít účet appky vůbec. Appka jen
    uloží žádost a pošle admin Telegramu upozornění; appka se pak sama
    zájemci ozve, žádný self-serve krok navíc."""
    full_name = req.full_name.strip()
    contact = req.contact.strip()
    phone = (req.phone or "").strip() or None
    if not full_name:
        raise HTTPException(status_code=400, detail="Chybí jméno.")
    if not contact:
        raise HTTPException(status_code=400, detail="Chybí kontakt (e-mail nebo Telegram).")
    if not phone:
        raise HTTPException(status_code=400, detail="Chybí telefonní číslo.")
    if len(full_name) > 160 or len(contact) > 255 or len(phone) > 40:
        raise HTTPException(status_code=400, detail="Jméno, kontakt nebo telefon je moc dlouhý.")

    lead_id = db.create_seller_lead(
        full_name=full_name,
        age=req.age,
        city=(req.city or "").strip() or None,
        experience=(req.experience or "").strip() or None,
        start_when=(req.start_when or "").strip() or None,
        income_goal=(req.income_goal or "").strip() or None,
        can_work_online=req.can_work_online,
        contact=contact,
        phone=phone,
    )

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id:
        try:
            online_txt = "ano" if req.can_work_online else ("ne" if req.can_work_online is False else "?")
            _send_telegram_message(
                int(chat_id),
                "🧑‍💼 Nová přihláška do náboru obchodníků\n"
                f"Jméno: {full_name}\n"
                f"Věk: {req.age or '?'}\n"
                f"Město: {req.city or '?'}\n"
                f"Zkušenosti: {req.experience or '?'}\n"
                f"Kdy může začít: {req.start_when or '?'}\n"
                f"Chce vydělávat: {req.income_goal or '?'}\n"
                f"Umí online/oslovovat lidi: {online_txt}\n"
                f"Telefon: {phone}\n"
                f"Kontakt: {contact}\n"
                "Přehled: apexsignal.cz/admin-prodejci",
            )
        except Exception as e:
            print(f"[leads] Nepodařilo se poslat Telegram upozornění: {e}")

    return {"status": "received", "lead_id": lead_id}


# =====================================================================
# Pomocné funkce — stahují zápasy pro každý sport a skládají MatchInput
# =====================================================================
def _enrich_one_fixture(provider, raw: dict, standings_cache: dict, standings_lock, dixon_coles_enabled: bool = False) -> Optional[MatchInput]:
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

    # Dixon-Coles (2026-08-06) — appka VYPNUTO defaultně (DIXON_COLES_ENABLED),
    # bezpečný přepínač na vyzkoušení bez rizika, že appka přestane
    # generovat tikety. Zapnuté: appka zkusí zafitovanou útočnou/obrannou
    # sílu CELÉ ligy (viz data_provider.get_dixon_coles_strengths) místo
    # heuristického odhadu ze dvou týmů. Bez dost dat appka funkce vrátí
    # None a normalize_to_match_input tiše spadne zpátky na starý odhad
    # (dixon_coles_xg zůstane None) — appka NIKDY negeneruje o nic míň
    # tiketů kvůli tomuhle přepínači, jen appka může použít přesnější xG.
    dixon_coles_xg = None
    if dixon_coles_enabled and league_id and fixture.get("season"):
        try:
            strengths = data_provider.get_dixon_coles_strengths(provider, int(league_id), int(fixture["season"]))
            if strengths:
                dixon_coles_xg = data_provider.dixon_coles_expected_goals(
                    fixture["home_team_id"], fixture["away_team_id"], strengths,
                )
        except Exception as e:
            print(f"[dixon-coles] Výpočet pro {fixture['home_team']} vs {fixture['away_team']} selhal: {e}")
        if dixon_coles_xg:
            # Heuristický odhad appka vidí v "[enrich]" logu o pár řádků výš
            # (avg_goals za posledních 10 zápasů) — appka ho tu nepočítá
            # znovu (musela by duplikovat celou home_factor/away_factor
            # logiku z normalize_to_match_input), stačí porovnat ručně.
            print(f"[dixon-coles] {fixture['home_team']} vs {fixture['away_team']}: DC xG=({dixon_coles_xg[0]}, {dixon_coles_xg[1]})")

    return data_provider.normalize_to_match_input(
        Sport.FOOTBALL, fixture, home_stats, away_stats, odds, home_form, away_form, weather,
        home_injury_count=home_injury_count, away_injury_count=away_injury_count,
        home_rest_days=home_rest_days, away_rest_days=away_rest_days,
        home_dead_rubber=home_dead_rubber, away_dead_rubber=away_dead_rubber,
        data_availability=data_availability, dixon_coles_xg=dixon_coles_xg,
    )


FIXTURE_ENRICHMENT_BATCH_SIZE = 20  # appka (2026-08-07, viz FIXTURE_ENRICHMENT_WORKERS
# výš) zpracovává zápasy po DÁVKÁCH, ne všechny najednou — appka dřív
# appka pouhým jedním voláním executor.submit() na VŠECHNY zápasy (klidně
# 200-400) rovnou vytvořila stejný počet Future objektů a živě k nim
# patřících syrových dat zápasu (raw fixture dict, appka ho drží celý po
# dobu čekání ve frontě) — i při jen 10 souběžně BĚŽÍCÍCH vláknech tohle
# appce v paměti drželo naráz VŠECHNY zbylé nezpracované zápasy. Appka
# teď frontu drží krátkou (max BATCH_SIZE čekajících) a mezi dávkami
# uvolní paměť (gc.collect()), ať appka nemá špičku paměti úměrnou
# CELÉMU oknu, ale jen jedné dávce.
#
# 2026-09-13: appka snížila z 40 na 20 (souběžně se snížením
# FIXTURE_ENRICHMENT_WORKERS 10→5 výš) — appka i po MAX_FIXTURES_PER_REQUEST=150
# živě zaznamenala další OOM, takže appka dál snižuje špičku paměti za
# cenu pomalejšího generování.


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

    # Appka zjistí přepínač JEDNOU na celý běh (ne pro každý zápas zvlášť)
    # — jeden DB dotaz místo desítek souběžných ve vláknech níž.
    dixon_coles_enabled = _is_dixon_coles_enabled()
    _load_calibration_curve()

    _progress_set_total(request_id, len(raw_fixtures))

    for batch_start in range(0, len(raw_fixtures), FIXTURE_ENRICHMENT_BATCH_SIZE):
        batch = raw_fixtures[batch_start:batch_start + FIXTURE_ENRICHMENT_BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=FIXTURE_ENRICHMENT_WORKERS) as executor:
            future_to_idx = {
                executor.submit(_enrich_one_fixture, provider, raw, standings_cache, standings_lock, dixon_coles_enabled): idx
                for idx, raw in enumerate(batch, start=batch_start + 1)
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
        # Appka uvolní paměť PO KAŽDÉ dávce, ne až na konci celé funkce —
        # appka to udělala kvůli živě potvrzeným OOM pádům na širokém
        # okně (2026-08-07), viz FIXTURE_ENRICHMENT_BATCH_SIZE výš.
        gc.collect()

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
    matched_pairs: list[tuple[MatchInput, dict]] = []
    for match in matches:
        event = data_provider.find_matching_odds_event(events, match.home_team, match.away_team, match.kickoff_date)
        if not event:
            continue
        matched_count += 1
        matched_pairs.append((match, event))
        adapted = data_provider.adapt_odds_api_event(event)
        if adapted["favorite_win_market_odds"]:
            match.favorite_win_market_odds = adapted["favorite_win_market_odds"]
            match.favorite_odds_verified = True
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

    if sport == Sport.FOOTBALL and matched_pairs:
        _enrich_shortlist_with_extra_markets(matched_pairs)

    _enrich_with_oddspapi(matches, sport)


MAX_EXTRA_MARKET_SHORTLIST = 15  # appka dvojtip/poločas tahá přes dotaz NA
# KAŽDÝ zápas zvlášť (viz _enrich_shortlist_with_extra_markets) — dražší
# na kredity the-odds-api (appka má jen 500/měsíc zdarma) než hromadný
# dotaz na celou ligu. 15 zápasů × 2 trhy = 30 kreditů na jedno
# generování — appka to drží nízko, ne pro celý pool.


def _enrich_shortlist_with_extra_markets(matched_pairs: list[tuple[MatchInput, "dict"]]) -> None:
    """
    Dvojtip (double_chance) a poločasové góly (totals_h1) appka NEDOSTANE
    z hromadného /odds dotazu výš (appka to živě ověřila — vrací
    INVALID_MARKET) — appka je musí tahat zvlášť přes dotaz na KONKRÉTNÍ
    zápas (/events/{id}/odds, viz OddsAPIProvider.get_event_odds), a ten
    appka volá jen pro malou shortlist (viz MAX_EXTRA_MARKET_SHORTLIST),
    ne pro celý pool zápasů. matched_pairs appka dostane z
    _enrich_with_market_odds — jen zápasy, co appka už napárovala na
    the-odds-api event (bez event_id appka nemá na co se ptát).
    """
    try:
        odds_provider = data_provider.OddsAPIProvider()
    except RuntimeError:
        return

    # Appka shortlist řadí podle SOUČTU očekávaných gólů (home_xg + away_xg),
    # ne podle výkopu — appka dřív brala prostě nejbližší zápasy podle času,
    # což s omezeným rozpočtem (jen 15 zápasů) klidně "prošvihlo" ofenzivní
    # zápas za 3 dny ve prospěch nudné 0:0 nudy za hodinu. Appka teď utratí
    # kredity tam, kde appčin vlastní model už tuší nejvíc gólů — přesně
    # tam, kde appka nejvíc čeká, že se poločasové góly/dvojtip vyplatí
    # (nahlásil uživatel 2026-08-05).
    shortlist = sorted(
        matched_pairs, key=lambda p: p[0].home_expected_goals + p[0].away_expected_goals, reverse=True,
    )[:MAX_EXTRA_MARKET_SHORTLIST]
    matched_count = 0
    for match, event in shortlist:
        event_id, sport_key = event.get("id"), event.get("sport_key")
        if not event_id or not sport_key:
            continue
        raw = odds_provider.get_event_odds(sport_key, event_id, markets="double_chance,totals_h1")
        if not raw:
            continue
        adapted = data_provider.adapt_odds_api_extra_markets(raw)
        if adapted["double_chance_odds"]:
            match.double_chance_odds.update(adapted["double_chance_odds"])
        if adapted["ht_over_threshold"] is not None:
            match.ht_over_goals_odds[adapted["ht_over_threshold"]] = adapted["ht_over_odds"]
            if adapted.get("ht_under_odds") is not None:
                match.ht_under_goals_odds[adapted["ht_over_threshold"]] = adapted["ht_under_odds"]
        if adapted["market_implied_probabilities"]:
            match.market_implied_probabilities.update(adapted["market_implied_probabilities"])
            matched_count += 1

    print(f"[enrich-odds-extra] {matched_count}/{len(shortlist)} shortlist zápasů má dvojtip/poločas")


ODDSPAPI_MAX_SHORTLIST = 10  # appka má na OddsPapi jen 250 requestů/měsíc
# ZDARMA a jeden /odds dotaz je navíc velký (~8 MB na zápas) — mnohem
# menší strop než MAX_EXTRA_MARKET_SHORTLIST u the-odds-api.


def _enrich_with_oddspapi(matches: list[MatchInput], sport: Sport) -> None:
    """
    Poslední záchrana za API-Football + the-odds-api (_enrich_with_market_odds
    výše) — appka OddsPapi volá JEN pro zápasy, co ani jeden z předchozích
    dvou zdrojů nenapároval na ŽÁDNÝ reálný tržní kurz (chybí
    data_availability["market_odds"]). Bez tohohle appka pro tyhle zápasy
    (typicky česká liga, Peru, mimosezónní evropské kvalifikace) MUSELA
    cenu vymýšlet z vlastního modelu (favorite_odds fallback v
    data_provider.normalize_to_match_input) — živě potvrzený problém
    důvěryhodnosti (uživatel nahlásil rozdíly 2-50 % proti reálným
    Tipsport cenám u zápasu Vlašim/PAOK/Alianza, 2026-08-05).

    Appka utrácí omezenou měsíční kvótu (ODDSPAPI_MAX_SHORTLIST) tam, kde
    model čeká nejvíc gólů — stejné řazení jako u
    _enrich_shortlist_with_extra_markets.
    """
    if sport != Sport.FOOTBALL:
        return
    try:
        odds_provider = data_provider.OddsPapiProvider()
    except RuntimeError:
        return

    uncovered = [m for m in matches if not m.data_availability.get("market_odds")]
    if not uncovered:
        return
    shortlist = sorted(
        uncovered, key=lambda m: m.home_expected_goals + m.away_expected_goals, reverse=True,
    )[:ODDSPAPI_MAX_SHORTLIST]

    matched_count = 0
    for match in shortlist:
        tournament_id = data_provider.ODDSPAPI_TOURNAMENT_IDS.get(match.league_id)
        if tournament_id is None:
            continue
        fixture = odds_provider.find_matching_fixture(tournament_id, match.home_team, match.away_team, match.kickoff_date)
        if fixture is None:
            continue
        raw = odds_provider.get_odds(fixture["fixtureId"])
        if not raw:
            continue
        adapted = data_provider.adapt_oddspapi_odds(raw)

        if adapted["favorite_win_market_odds"]:
            match.favorite_win_market_odds = adapted["favorite_win_market_odds"]
            match.favorite_odds_verified = True
        if adapted["market_implied_probabilities"]:
            match.market_implied_probabilities.update(adapted["market_implied_probabilities"])
            match.market_odds_bookmaker_count = adapted.get("bookmaker_count")
            match.data_availability["market_odds"] = True
            matched_count += 1

        if adapted["over_threshold"] is not None:
            threshold = adapted["over_threshold"]
            match.market_implied_probabilities[f"{MarketType.OVER_GOALS.value}:over_{threshold}"] = adapted["over_probability"]
            match.over_goals_odds[threshold] = adapted["over_odds"]
            if adapted.get("under_odds") is not None:
                match.market_implied_probabilities[f"{MarketType.OVER_GOALS.value}:under_{threshold}"] = adapted["under_probability"]
                match.under_goals_odds[threshold] = adapted["under_odds"]

        if adapted["double_chance_odds"]:
            match.double_chance_odds.update(adapted["double_chance_odds"])
        if adapted["ht_over_threshold"] is not None:
            match.ht_over_goals_odds[adapted["ht_over_threshold"]] = adapted["ht_over_odds"]
            if adapted.get("ht_under_odds") is not None:
                match.ht_under_goals_odds[adapted["ht_over_threshold"]] = adapted["ht_under_odds"]

    print(f"[enrich-odds-oddspapi] {matched_count}/{len(shortlist)} zápasů bez kurzu dostalo OddsPapi kurz")


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
    all_saved_ids = set(repo.get_all_saved_match_ids(user_id))
    exclude_ids = all_saved_ids | set(repo.get_last_batch(user_id))

    # Uživatel, co si UŽ nějaký tiket uložil, a chce další, appka pustí
    # o trochu volnější dolní hranici kurzu u krátkého (1.80 místo 1.90,
    # viz TicketGenerator.generate/RELAXED_MIN_ODDS_HARD) — 2026-08-25,
    # uživatelovo přání ("kdyz uz ma clovek ulozeny tiket a chce dalsi
    # aby to vzalo i 1,8"). PRVNÍ tiket appka pořád drží na tvrdém dnu
    # 1.90 — appka slevuje jen na "druhý a další" příležitost.
    allow_relaxed_min_odds = bool(all_saved_ids)

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
        pool_for_peek, time_frame_for_peek, markets_for_peek = matches, req.time_frame_days, req.market_types
        result = ticket_generator.generate(
            matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
            pool_filter=_pool_filter_for_risk(req.risk_level),
            allow_relaxed_min_odds=allow_relaxed_min_odds,
        )
        # Appka appce na Renderu jede na starter plánu (512 MB RAM, appka
        # na něj naráží OOM při širokém okně kandidátů) — uvolní paměť po
        # obohaceném poolu zápasů hned po každém pokusu o generování, ne
        # až na konci celé funkce (viz stejná pojistka u OOM opravy výše).
        gc.collect()

        # Appka dřív tohle rozšíření dělala TICHOU — uživatel si vybral "1 den"
        # a dostal zpátky tiket se zápasy klidně 4 dny dopředu, aniž by o tom
        # appka řekla jediné slovo. Teď to appka pořád zkusí (ať appka nenechá
        # uživatele zbytečně čekat na "nic nenašla", i když je opravdu ochotná
        # nabídnout aspoň něco), ale VŽDY to uživateli řekne přes horizon_note,
        # co appka reálně udělala.
        if result["safe"] is None:
            wider_days = req.time_frame_days + 1
            all_wider_matches = _fetch_candidate_matches(req.sports, wider_days, request_id=req.request_id)
            all_wider_matches = [m for m in all_wider_matches if m.match_id not in exclude_ids]
            all_wider_matches = _filter_future_matches(all_wider_matches, buffer_minutes=5)
            # appka (2026-08-18) zjistila, že tady chyběl stejný
            # _filter_within_days, co appka má u PŮVODNÍHO (užšího) okna
            # výš — bez něj mohl horizon_note slíbit "zápasy až za
            # {wider_days} dní", ale appka klidně nabídla zápas o den
            # dál (nahlásil uživatel — tiket na 3 dny obsahoval zápas
            # za 4). Appka to teď hlídá stejně přísně jako užší okno.
            all_wider_matches = _filter_within_days(all_wider_matches, wider_days)
            wider_result = ticket_generator.generate(
                all_wider_matches, req.risk_level, req.sports, req.market_types, wider_days,
                pool_filter=_pool_filter_for_risk(req.risk_level),
                allow_relaxed_min_odds=allow_relaxed_min_odds,
            )
            gc.collect()
            if wider_result["safe"] is not None:
                result = wider_result
                pool_for_peek, time_frame_for_peek = all_wider_matches, wider_days
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
                        allow_relaxed_min_odds=allow_relaxed_min_odds,
                    )
                    gc.collect()
                    if markets_result["safe"] is not None:
                        result = markets_result
                        pool_for_peek, time_frame_for_peek, markets_for_peek = all_wider_matches, wider_days, all_markets
                        horizon_note = (
                            f"Appka v tvém vybraném časovém rámci ({req.time_frame_days} "
                            f"{'den' if req.time_frame_days == 1 else 'dny'}) a zvolených trzích nenašla žádnou "
                            f"kombinaci s dostatečnou důvěrou, tak zkusila i ostatní trhy (např. Under góly) — "
                            f"nabízí zápasy až za {wider_days} dní."
                        )

        used_ids = [s.match_id for t in result.values() if t for s in t.selections]
        repo.set_last_batch(user_id, used_ids)
        repo.reset_replace_count(user_id)  # nové generování appce dá zase jednu výměnu zdarma

        if result["safe"] is not None:
            _charge_tokens_for_ticket(user_id, result["safe"].ticket_type)

        # appka (2026-09-17, uživatelovo přání: appka v pozadí ví, jestli
        # zbyli další kandidáti, tak ať appka proaktivně nabídne další
        # generování) — appka "nakoukne", jestli by ze ZBYLÝCH zápasů (po
        # odečtení právě nalezeného tiketu) šla poskládat ještě jedna
        # kombinace. Žádné nové síťové volání — appka jen znovu pustí
        # generátor (čistě CPU) na už stažená a obohacená data, takže tenhle
        # "peek" appku nic nestojí na API-Football/the-odds-api rozpočtu.
        more_candidates_available = False
        if result["safe"] is not None:
            try:
                remaining_matches = [m for m in pool_for_peek if m.match_id not in used_ids]
                peek_result = ticket_generator.generate(
                    remaining_matches, req.risk_level, req.sports, markets_for_peek, time_frame_for_peek,
                    pool_filter=_pool_filter_for_risk(req.risk_level),
                    allow_relaxed_min_odds=True,
                )
                more_candidates_available = peek_result["safe"] is not None
            except Exception:
                more_candidates_available = False
            gc.collect()

        return TicketPairResponse(
            safe=TicketResponse.from_domain(result["safe"], horizon_note=horizon_note) if result["safe"] else None,
            aggressive=TicketResponse.from_domain(result["aggressive"], horizon_note=horizon_note) if result["aggressive"] else None,
            more_candidates_available=more_candidates_available,
        )
    except Exception:
        raise


def _run_regenerate_job(user_id: int, req: TicketGenerateRequest) -> TicketPairResponse:
    previous_ids = repo.get_last_batch(user_id)
    exclude_ids = repo.get_all_saved_match_ids(user_id)  # Všechny již vsazené zápasy
    combined_exclude = set(previous_ids) | set(exclude_ids)
    allow_relaxed_min_odds = bool(exclude_ids)  # viz stejná poznámka v _run_generate_job

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
            allow_relaxed_min_odds=allow_relaxed_min_odds,
        )
        gc.collect()  # viz stejná pojistka v _run_generate_job (OOM na starter plánu)

        # Viz stejná poznámka v generate_tickets — appka rozšíření pořád
        # zkusí, ale vždycky to řekne přes horizon_note.
        if result["safe"] is None:
            wider_days = req.time_frame_days + 1
            all_wider_matches = _fetch_candidate_matches(req.sports, wider_days, request_id=req.request_id)
            all_wider_matches = [m for m in all_wider_matches if m.match_id not in combined_exclude]
            all_wider_matches = _filter_future_matches(all_wider_matches, buffer_minutes=5)
            # Stejná oprava jako v _run_generate_job (appka 2026-08-18) —
            # bez tohohle filtru mohl horizon_note slíbit "za wider_days
            # dní", ale appka klidně nabídla zápas o den dál.
            all_wider_matches = _filter_within_days(all_wider_matches, wider_days)
            wider_result = ticket_generator.regenerate(
                all_wider_matches, req.risk_level, req.sports, req.market_types, wider_days, list(previous_ids),
                pool_filter=_pool_filter_for_risk(req.risk_level),
                allow_relaxed_min_odds=allow_relaxed_min_odds,
            )
            gc.collect()
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
                        allow_relaxed_min_odds=allow_relaxed_min_odds,
                    )
                    gc.collect()
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
        repo.reset_replace_count(user_id)  # nové generování appce dá zase jednu výměnu zdarma

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
            import traceback
            print(f"[generate-job] {request_id} selhalo: {e}\n{traceback.format_exc()}")
            _results_store(request_id, {"status": "error", "detail": "Generování se nepovedlo, zkus to znovu."})
        # Appka tu záměrně NEMAŽE _GENERATION_PROGRESS hned po doběhnutí
        # (dřív tu bylo _progress_clear(request_id)) — když je generování
        # rychlé (zápasy/statistiky ještě teplé v mezipaměti z předchozího
        # generování), appka dřív stihla smazat záznam DŘÍV, než frontend
        # poslal svůj další dotaz (pollGenerationProgress, každých 900 ms) —
        # frontend pak dostal "known:false" navěky a neukázal procenta,
        # jen dekorativní konzoli (nahlášeno uživatelem). Appka nechává
        # záznam ležet a spoléhá na TTL úklid v _progress_set_total (10 min),
        # ať frontend i po dokončení aspoň JEDNOU uvidí finální 100 %.

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
    ticket_type: str = "kratky"
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


@app.get("/admin/all-markets-calibration")
def admin_all_markets_calibration(request: Request):
    """
    Diagnostika (2026-08-09) — appka na žádost uživatele rozšiřuje stejný
    rozbor, co appka udělala pro over/under góly (/admin/goals-market-calibration),
    i na ZBYLÉ trhy (match_winner, double_chance, btts, over_cards) — appka
    předtím slabinu hledala jen tam, kam ji nasměroval konkrétní dnešní
    problém, tohle appce ukáže, jestli má slabá místa i jinde, ne jen dohadem.
    Rozpad appka dělá podle trhu+selekce (appka X2/1X u dvojtipu čte odděleně
    — appka je jinak asymetricky přesná) a podle ligy (appka appce zamlčí
    ligy s <5 vzorky, ať appku nemate šumem u trhů s malým objemem). Stejný
    'model_minus_market_gap' signál jako appka má u gólů — kladné číslo u
    PROHRANÝCH výběrů znamená appčin model byl sebejistější než trh.
    Read-only, nic neukládá.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT market_type, selection, result, model_probability, market_probability, league
              FROM ticket_selections
             WHERE result IN ('won', 'lost') AND market_type IN
                   ('match_winner', 'double_chance', 'btts', 'over_cards')
            """
        )
        rows = cur.fetchall()

    by_market: dict[str, dict] = {}
    by_league: dict[str, dict] = {}
    for row in rows:
        key = f"{row['market_type']}:{row['selection']}"
        acc = by_market.setdefault(key, {"won": 0, "lost": 0, "model_minus_market_gaps": []})
        acc[row["result"]] += 1
        if row["model_probability"] is not None and row["market_probability"] is not None:
            acc["model_minus_market_gaps"].append(float(row["model_probability"]) - float(row["market_probability"]))

        league_key = f"{row['market_type']}:{row['league'] or 'neznámá'}"
        lacc = by_league.setdefault(league_key, {"won": 0, "lost": 0})
        lacc[row["result"]] += 1

    market_out = []
    for key, acc in by_market.items():
        total = acc["won"] + acc["lost"]
        gaps = acc["model_minus_market_gaps"]
        market_out.append({
            "trh_a_selekce": key,
            "won": acc["won"], "lost": acc["lost"],
            "win_rate_pct": round(acc["won"] / total * 100, 1) if total else 0.0,
            "pocet": total,
            "prumerny_rozdil_model_minus_trh_pct": round(sum(gaps) / len(gaps) * 100, 1) if gaps else None,
        })
    market_out.sort(key=lambda x: x["pocet"], reverse=True)

    league_out = []
    for key, acc in by_league.items():
        total = acc["won"] + acc["lost"]
        if total < 5:
            continue
        league_out.append({
            "trh_a_liga": key, "won": acc["won"], "lost": acc["lost"],
            "win_rate_pct": round(acc["won"] / total * 100, 1), "pocet": total,
        })
    league_out.sort(key=lambda x: x["win_rate_pct"])

    return {
        "podle_trhu_a_selekce": market_out,
        "podle_ligy_min_5_vzorky": league_out,
        "poznamka": (
            "'prumerny_rozdil_model_minus_trh_pct' kladné číslo = appčin vlastní "
            "model byl v průměru sebejistější než trh u PROHRANÝCH výběrů — "
            "vysoké kladné číslo napříč víc vzorky je varovný signál systematické "
            "přeceněnosti, ne smůla na jednom zápase."
        ),
    }


@app.get("/admin/goals-market-calibration")
def admin_goals_market_calibration(request: Request):
    """
    Diagnostika (2026-08-09) — appka na žádost uživatele rozebírá KONKRÉTNĚ
    over_goals/under_goals (appčiny historicky nejslabší trhy podle
    /admin/win-loss-report), rozpadlé podle KONKRÉTNÍHO prahu (over_1.5 vs
    over_2.5 atd.) — appka to /admin/calibration-report nemá, ten kalibruje
    jen podle appkou odhadnuté pravděpodobnosti napříč VŠEMI trhy dohromady.
    Appka navíc pro každý koš spočítá průměrný rozdíl model_probability
    minus market_probability, ať appka pozná, jestli appka soustavně věří
    vlastnímu modelu víc, než by měla vůči trhu. Read-only, nic neukládá.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT market_type, selection, result, probability, model_probability, market_probability, league
              FROM ticket_selections
             WHERE result IN ('won', 'lost') AND market_type IN ('over_goals', 'under_goals')
            """
        )
        rows = cur.fetchall()

    by_threshold: dict[str, dict] = {}
    by_league: dict[str, dict] = {}
    for row in rows:
        key = f"{row['market_type']}:{row['selection']}"
        acc = by_threshold.setdefault(key, {"won": 0, "lost": 0, "model_minus_market_gaps": []})
        acc[row["result"]] += 1
        if row["model_probability"] is not None and row["market_probability"] is not None:
            acc["model_minus_market_gaps"].append(float(row["model_probability"]) - float(row["market_probability"]))

        league_key = f"{row['market_type']}:{row['league'] or 'neznámá'}"
        lacc = by_league.setdefault(league_key, {"won": 0, "lost": 0})
        lacc[row["result"]] += 1

    threshold_out = []
    for key, acc in sorted(by_threshold.items()):
        total = acc["won"] + acc["lost"]
        gaps = acc["model_minus_market_gaps"]
        threshold_out.append({
            "trh_a_prah": key,
            "won": acc["won"], "lost": acc["lost"],
            "win_rate_pct": round(acc["won"] / total * 100, 1) if total else 0.0,
            "pocet": total,
            "prumerny_rozdil_model_minus_trh_pct": round(sum(gaps) / len(gaps) * 100, 1) if gaps else None,
        })
    threshold_out.sort(key=lambda x: x["pocet"], reverse=True)

    league_out = []
    for key, acc in by_league.items():
        total = acc["won"] + acc["lost"]
        if total < 3:  # appka appce zamlčí ligy s příliš málo vzorky, ať to appku nemate šumem
            continue
        league_out.append({
            "trh_a_liga": key, "won": acc["won"], "lost": acc["lost"],
            "win_rate_pct": round(acc["won"] / total * 100, 1), "pocet": total,
        })
    league_out.sort(key=lambda x: x["win_rate_pct"])

    return {
        "podle_prahu": threshold_out,
        "podle_ligy_min_3_vzorky": league_out,
        "poznamka": (
            "'prumerny_rozdil_model_minus_trh_pct' kladné číslo = appčin vlastní "
            "model byl v průměru sebejistější než trh u PROHRANÝCH výběrů — "
            "vysoké kladné číslo je varovný signál systematické přeceněnosti "
            "modelu u toho konkrétního prahu, ne jen smůla na jednom zápase."
        ),
    }


@app.post("/admin/recompute-calibration-curve")
def admin_recompute_calibration_curve(request: Request):
    """
    Appka appce spočítá appčinu VLASTNÍ kalibrační korekční křivku a
    uloží ji do app_settings (viz set_calibration_curve v
    probability_model.py) — od příštího generování appka appce posune
    finální pravděpodobnost směrem k tomu, co appka v daném koši
    historicky OPRAVDU vyhrává, místo aby appka slepě věřila
    de-vigovanému číslu.

    Na rozdíl od /admin/calibration-report appka tady bucketuje podle
    'probability' (appkou FINÁLNĚ použité/zobrazené číslo — tržní, pokud
    appka ho má, jinak model), ne podle 'model_probability' — appka
    koriguje přesně to číslo, co appka i klientovi ukazuje a na co appka
    staví vklad. Koše s míň než CALIBRATION_BUCKET_MIN_SAMPLES appka do
    křivky vůbec neuloží (příliš málo dat, korekce by byla jen šum) —
    appka pro ně zůstane beze změny (fallback na nekorigovanou hodnotu).

    Appka tohle appce nikdy nespouští automaticky — appka to appce nechá
    jako vědomé rozhodnutí appky, kdy je appka spustí (appka doporučuje
    ne dřív, než appka nasbírá dost čerstvých, spolehlivě vyhodnocených
    dat — viz oprava settlement 30. 7.).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT probability, result
              FROM ticket_selections
             WHERE result IN ('won', 'lost') AND probability IS NOT NULL
            """
        )
        rows = cur.fetchall()

    buckets: dict[int, dict] = {}
    for row in rows:
        p = row["probability"]
        bucket = max(0, min(100, int(round(float(p) * 20)) * 5))
        acc = buckets.setdefault(bucket, {"won": 0, "lost": 0})
        acc[row["result"]] += 1

    curve: dict[str, float] = {}
    skipped_low_sample = []
    for bucket, acc in sorted(buckets.items()):
        total = acc["won"] + acc["lost"]
        if total < CALIBRATION_BUCKET_MIN_SAMPLES:
            skipped_low_sample.append({"bucket": bucket, "count": total})
            continue
        curve[str(bucket)] = round(acc["won"] / total * 100, 1)

    db.set_setting(CALIBRATION_CURVE_SETTING_KEY, json.dumps(curve))
    set_calibration_curve({int(k): v for k, v in curve.items()})  # appka appce ať rovnou platí i v týhle běžící instanci

    return {
        "curve_saved": curve,
        "skipped_low_sample_buckets": skipped_low_sample,
        "min_samples_required": CALIBRATION_BUCKET_MIN_SAMPLES,
    }


@app.get("/admin/win-loss-report")
def admin_win_loss_report(request: Request, since: Optional[str] = None, until: Optional[str] = None):
    """
    Appka tímhle appce spočítá kompletní statistiku úspěšnosti napříč
    VŠEMI účty appky (appčiny vlastní i klientské) — kolik tiketů/výběrů
    appka vyhrála/prohrála, rozpad podle typu tiketu a podle typu trhu.
    Appka počítá jen vyhodnocené (won/lost), pending appka do procent
    nezapočítává (nemá smysl, výsledek appka ještě nezná).

    since/until (YYYY-MM-DD, appka je bere jako UTC) appka appce nechala
    volitelné — appce umožní srovnat konkrétní období (např. "jak appka
    vypadala týden 10. 7." vs. "posledních 10 dní"), beze změny výchozího
    chování (bez parametrů appka pořád počítá úplně celou historii).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    date_clause = ""
    date_params: list = []
    if since:
        date_clause += " AND t.created_at >= %s"
        date_params.append(since)
    if until:
        date_clause += " AND t.created_at < %s"
        date_params.append(until)

    with db.get_cursor() as cur:
        cur.execute(f"SELECT status, COUNT(*) AS c FROM tickets t WHERE true{date_clause} GROUP BY status", date_params)
        by_status = {r["status"]: r["c"] for r in cur.fetchall()}

        # Appka zisk/ztrátu nedrží jako sloupec — appka ho dopočítává za
        # běhu ze sázky/kurzu/výsledku (viz Repo._compute_actual_profit_loss),
        # appka to tady dělá stejně, v Pythonu, ne v SQL.
        cur.execute(
            f"""
            SELECT ticket_type, status, total_odds, actual_stake_amount, actual_odds
              FROM tickets t
             WHERE status IN ('won', 'lost'){date_clause}
            """,
            date_params,
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
            f"""
            SELECT ts.market_type, ts.result, COUNT(*) AS c
              FROM ticket_selections ts
              JOIN tickets t ON t.id = ts.ticket_id
             WHERE ts.result IN ('won', 'lost'){date_clause}
             GROUP BY ts.market_type, ts.result
            """,
            date_params,
        )
        by_market_rows = cur.fetchall()

        cur.execute(
            f"""
            SELECT COUNT(*) AS c
              FROM ticket_selections ts
              JOIN tickets t ON t.id = ts.ticket_id
             WHERE ts.result IN ('won', 'lost'){date_clause}
            """,
            date_params,
        )
        total_selections = cur.fetchone()["c"]
        cur.execute(
            f"""
            SELECT COUNT(*) AS c
              FROM ticket_selections ts
              JOIN tickets t ON t.id = ts.ticket_id
             WHERE ts.result = 'won'{date_clause}
            """,
            date_params,
        )
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


@app.get("/admin/lost-tickets-report")
def admin_lost_tickets_report(request: Request, days: int = 7):
    """
    Diagnostika (2026-08-06) — appka na žádost uživatele rozebírá PROHRANÉ
    tikety za posledních `days` dní: ke KAŽDÉMU appka najde konkrétní
    nohu/nohy, co ho prohrály (result='lost'), a k tomu spočítá statistiku
    podle typu trhu (kde appka nejvíc prohrává). Read-only, nic neukládá.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT t.id AS ticket_id, t.ticket_type, t.total_odds, t.actual_stake_amount,
                   t.actual_odds, t.created_at, u.email
              FROM tickets t
              JOIN users u ON u.id = t.user_id
             WHERE t.status = 'lost' AND t.created_at >= now() - (%s || ' days')::interval
             ORDER BY t.created_at DESC
            """,
            (days,),
        )
        lost_tickets = cur.fetchall()

        ticket_ids = [t["ticket_id"] for t in lost_tickets]
        losing_selections_by_ticket: dict[int, list[dict]] = {}
        market_loss_count: dict[str, int] = {}
        if ticket_ids:
            cur.execute(
                """
                SELECT ticket_id, home_team, away_team, market_type, selection, odds,
                       model_probability, market_probability, league, kickoff_date
                  FROM ticket_selections
                 WHERE ticket_id = ANY(%s) AND result = 'lost'
                """,
                (ticket_ids,),
            )
            for row in cur.fetchall():
                losing_selections_by_ticket.setdefault(row["ticket_id"], []).append(dict(row))
                m = row["market_type"] or "neznámý"
                market_loss_count[m] = market_loss_count.get(m, 0) + 1

    tickets_out = []
    total_staked = 0.0
    total_lost_kc = 0.0
    for t in lost_tickets:
        stake = float(t.get("actual_stake_amount") or 0)
        total_staked += stake
        total_lost_kc += stake
        tickets_out.append({
            "ticket_id": t["ticket_id"],
            "email": t["email"],
            "ticket_type": t["ticket_type"],
            "odds": t.get("actual_odds") or t["total_odds"],
            "stake_kc": stake,
            "created_at": t["created_at"].isoformat() if t["created_at"] else None,
            "losing_legs": losing_selections_by_ticket.get(t["ticket_id"], []),
        })

    return {
        "days": days,
        "lost_tickets_count": len(lost_tickets),
        "total_staked_kc": round(total_staked, 0),
        "total_lost_kc": round(total_lost_kc, 0),
        "losses_by_market_type": dict(sorted(market_loss_count.items(), key=lambda kv: kv[1], reverse=True)),
        "tickets": tickets_out,
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


class AdminPageAuthRequest(BaseModel):
    password: str


@app.post("/admin/page-auth")
def admin_page_auth(req: AdminPageAuthRequest):
    """
    Appka appce dřív dala X-Admin-Key přímo natvrdo do zdrojáku
    admin-prodejci.html, aby appka nemusela nic zadávat — uživatel na to
    upozornil, že to znamená, že KDOKOLI si klíč přečte ve zdrojovém kódu
    stránky, i beze zadání hesla. Tenhle endpoint appka místo toho ověří
    heslo appka na appčině serveru — skutečné heslo ANI admin klíč se tak
    nikde ve statickém kódu appky stránky neobjeví, appka klíč pošle zpět
    jen tomu, kdo appce pošle správné heslo.
    """
    expected_password = os.environ.get("ADMIN_PRODEJCI_PASSWORD")
    admin_key = os.environ.get("ADMIN_TASK_KEY")
    if not expected_password or not admin_key:
        raise HTTPException(status_code=500, detail="ADMIN_PRODEJCI_PASSWORD nebo ADMIN_TASK_KEY není nastavené")
    if req.password != expected_password:
        raise HTTPException(status_code=403, detail="Špatné heslo.")
    return {"admin_key": admin_key}


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


@app.get("/admin/user-tickets")
def admin_user_tickets(request: Request, email: str):
    """
    Diagnostika (2026-08-06) — appka jinak nemá žádný způsob, jak se
    admin-key přístupem podívat na uložené tikety KONKRÉTNÍHO uživatele
    (/tickets/saved appka gatuje jen přihlašovacím tokenem toho
    uživatele samotného, nic admin-key). Appka to potřebovala kvůli
    zpětnému rozboru "jak appka tenhle konkrétní tiket sestavila" na
    žádost uživatele. Read-only, nic neukládá.
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="Účet s tímhle e-mailem appka nenašla")

    tickets = _list_saved_tickets_for_user(user["id"])
    return {"user_id": user["id"], "email": email, "tickets": tickets}


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

    outcome = evaluate_selection_outcome(
        selection, result["home_goals"], result["away_goals"], total_cards,
        ht_home_goals=result.get("ht_home_goals"), ht_away_goals=result.get("ht_away_goals"),
    )
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
    chování pro v podstatě stejnou operaci (generování tiketu).

    Appka appce tenhle endpoint dřív vůbec nezpoplatňovala (na rozdíl od
    /tickets/generate) — uživatel si tak mohl mačkáním appky "vyměnit
    zápas" projet celý appčin denní pool bez zaplacení jediného tokenu
    (uživatelovo zjištění 2026-09-12). Appka appce teď dovolí JEN
    Repo.REPLACE_SELECTION_FREE_LIMIT (1) výměnu zdarma na jedno
    generování (viz reset_replace_count volané z /tickets/generate a
    /tickets/regenerate) — appka appce zbytek doplní appku appky vlastní
    poznámkou (horizon_note), ať appka appce vidí, že tenhle konkrétní
    tiket appka appce jednou přeskládala."""
    _require_generation_enabled(user_id)
    if not repo.try_consume_replace_selection(user_id):
        raise HTTPException(
            status_code=400,
            detail="Tenhle tiket appka appce už jednou přeskládala zdarma — pro další změnu vygeneruj nový tiket.",
        )
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
        safe=TicketResponse.from_domain(result["safe"], horizon_note="Jedna noha tiketu byla ručně vyměněna.") if result["safe"] else None,
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

            actual_outcome = evaluate_selection_outcome(
                sel_obj, real["home_goals"], real["away_goals"],
                ht_home_goals=real.get("ht_home_goals"), ht_away_goals=real.get("ht_away_goals"),
            )
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
        "nelze_overi
