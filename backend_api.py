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
from pydantic import BaseModel, Field, field_validator

from probability_model import (
    TicketGenerator, MatchInput, Sport, MarketType, Ticket, SelectionCandidate, evaluate_selection_outcome,
)
import data_provider
import ai_reviewer
import db
import auth
import rate_limiter
import ticket_telegram
import email_service
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
import stripe

# Appka zpracovává zápasy SOUBĚŽNĚ (víc vláken najednou), ne jeden po
# druhém — viz _build_football_matches. Volání čekají hlavně na síť
# (API-Football, Open-Meteo), ne na CPU appky, takže vlákna appce reálně
# zkrátí celkový čas bez zvýšení spotřeby denní kvóty API (appka udělá
# stejný POČET volání, jen je nedělá postupně). 8 vláken je kompromis —
# víc by zase mohlo narazit na limit požadavků za minutu u API-Football.
FIXTURE_ENRICHMENT_WORKERS = 8

# Logger setup
logger = logging.getLogger("apexsignal")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ApexSignal API", version="0.1.0")

# Appka teď bere skutečné platby, takže appka backend nesmí volat
# libovolná cizí stránka — jen appce vlastní frontend (Netlify).
ALLOWED_ORIGINS = [
    "https://apexsignal-app.netlify.app",
    "https://cheerful-tarsier-f89a91.netlify.app",  # starší doména appky, appka ji nechává pro jistotu funkční
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
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

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@")[-1] or len(v) > 255:
            raise ValueError("Zadej platný e-mail")
        return v


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
    try:
        email_service.send_welcome_email(req.email)
    except Exception as e:
        print(f"[register] Uvítací e-mail se nepodařilo odeslat: {e}")
    return AuthResponse(token=auth.create_token(user_id), user_id=user_id, email=req.email, is_new_user=True)


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
    Appka ověří Google ID token appka appce (podpis appka i cílový klient
    appka appka appka ověří knihovnou google-auth, appka appce nedůvěřuje
    ničemu, co appka nedostane přímo od Google) a podle e-mailu z něj buď
    najde existující účet, nebo appka založí nový — appka novým Google
    účtům nastaví náhodné, nikdy nepoužité heslo (appka appka appce
    ho nikdy neřekne), appka běžné přihlášení heslem appce zůstane
    funkční, pokud si ho appka appka appka někdy appka nastaví přes
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
        try:
            email_service.send_welcome_email(email)
        except Exception as e:
            print(f"[google_auth] Uvítací e-mail se nepodařilo odeslat: {e}")
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
        frontend_url = os.environ.get("FRONTEND_URL", "https://cheerful-tarsier-f89a91.netlify.app")
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
    risk_level: int = Field(ge=0, le=100)
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=5)  # Horizont: 1-5 dní (už ne konkrétní data)


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
    selections: list[SelectionResponse]

    @classmethod
    def from_domain(cls, ticket: Ticket, ticket_id: Optional[int] = None, status: str = "pending",
                     live_alert: Optional[str] = None, actual_stake_amount: Optional[float] = None,
                     actual_odds: Optional[float] = None, actual_profit_loss: Optional[float] = None,
                     created_at: Optional[str] = None) -> "TicketResponse":
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
        self._last_batch_match_ids: dict[int, list[int]] = {}  # user_id -> match_ids z posledního generování
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
        """Vrátí všechny match_ids z uložených tiketů — pro vyloučení duplikátů."""
        rows = db.fetch_ticket_rows(user_id=user_id)
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
        self._last_batch_match_ids[user_id] = match_ids

    def get_last_batch(self, user_id: int) -> list[int]:
        return self._last_batch_match_ids.get(user_id, [])


repo = Repo()
ticket_generator = TicketGenerator()


# =====================================================================
# Tokenový systém — viz ApexSignal Tokenomika & Tokenový Model. Stripe
# napojení přijde v dalším kroku; tahle appka zatím jen řídí zůstatek a
# uplatňování kódů (viz db.py: user_tokens/token_transactions/redeem_codes).
# =====================================================================
TOKEN_KC_VALUE = 20  # 1 token = 20 Kč — appka to appce i frontendu drží na jednom místě
TOKEN_COSTS = {"kratky": 6, "stredni": 11, "boost": 30}  # ceny podle potenciálu výhry (kurzu), ne podle spolehlivosti — BOOST je nejdražší
TOKEN_PACKAGES = [12, 24, 60]  # předvolby k nákupu (v tokenech) — nejmenší pokryje aspoň 2 krátké tikety
MIN_CUSTOM_TOKENS = 1
MAX_CUSTOM_TOKENS = 5000  # pojistka proti překlepu/zneužití při vlastní částce

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


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


def _charge_tokens_for_ticket(user_id: int, ticket_type: str) -> None:
    cost = TOKEN_COSTS.get(ticket_type, 0)
    if cost > 0:
        db.adjust_tokens(user_id, -cost, f"UNLOCK_{ticket_type.upper()}")


class RedeemCodeRequest(BaseModel):
    code: str


@app.get("/tokens/balance")
def get_token_balance_endpoint(user_id: int = Depends(get_current_user_id)):
    return {"balance": db.get_token_balance(user_id)}


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
    }


class CreateCheckoutSessionRequest(BaseModel):
    tokens: int


@app.post("/payments/create-checkout-session")
def create_checkout_session(req: CreateCheckoutSessionRequest, user_id: int = Depends(get_current_user_id)):
    if req.tokens < MIN_CUSTOM_TOKENS or req.tokens > MAX_CUSTOM_TOKENS:
        raise HTTPException(status_code=400, detail=f"Počet tokenů musí být mezi {MIN_CUSTOM_TOKENS} a {MAX_CUSTOM_TOKENS}")
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Platby zatím nejsou nastavené")

    price_kc = req.tokens * TOKEN_KC_VALUE
    frontend_url = os.environ.get("FRONTEND_URL", "https://cheerful-tarsier-f89a91.netlify.app")
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


@app.post("/payments/webhook")
async def stripe_webhook(request: Request):
    """
    Appka tokeny připisuje TADY (server-side, po ověřeném webhooku), ne
    hned po přesměrování na success_url — ten frontend uživatel může
    zavřít/obejít, kdežto webhook appka dostane přímo od Stripe a jde mu
    věřit jen po ověření podpisu (STRIPE_WEBHOOK_SECRET).
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

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        if db.mark_stripe_event_if_new(event["id"]):
            metadata = session.get("metadata") or {}
            user_id = int(metadata.get("user_id", 0))
            tokens = int(metadata.get("tokens", 0))
            if user_id and tokens:
                db.adjust_tokens(user_id, tokens, f"STRIPE_PAYMENT:{session['id']}")

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
    tokens: int
    max_uses: int = 1
    expires_in_days: Optional[int] = None
    note: str = ""
    code: Optional[str] = None  # vlastní text kódu (např. "BOOST") — jinak appka vygeneruje náhodný


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
    db.create_redeem_code(code, req.tokens, req.max_uses, expires_at, req.note)
    return {"code": code, "tokens": req.tokens, "max_uses": req.max_uses, "expires_at": expires_at.isoformat() if expires_at else None}


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


def _build_football_matches(provider, raw_fixtures: list[dict]) -> list[MatchInput]:
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

    print(f"[enrich-odds] {len(events)} events z the-odds-api, {matched_count}/{len(matches)} zápasů napárováno")


def _fetch_candidate_matches(sports: list[Sport], time_frame_days: int) -> list[MatchInput]:
    """
    Vrátí zápasy v daném horziontu dnů (bez konkrétních dat).
    Důvod: horizont (1-4 dny) je jednodušší, přirozený a bez chyb
    oproti parsování konkrétního YYYY-MM-DD data.
    
    Filtrování na ligy dostupné na Tipsportu (podle league_id) se děje
    už v data_provider.py (TIPSPORT_LEAGUE_IDS, aplikováno v
    get_upcoming_matches) — zde se NEDUPLIKUJE, aby nedocházelo k
    rozporu mezi dvěma nezávislými seznamy ID.
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


# =====================================================================
# REST endpointy — Generátor tiketů
# =====================================================================
@app.post("/tickets/generate", response_model=TicketPairResponse)
def generate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    _check_token_balance(user_id, req.risk_level)
    all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    exclude_ids = repo.get_all_saved_match_ids(user_id)  # Všechny již vsazené zápasy
    
    # Vyfiltruj zápasy které už jsou v uložených tiketu
    matches = [m for m in all_matches if m.match_id not in exclude_ids]
    
    # FILTR: Odstranit zápasy v MINULOSTI (NEW!)
    matches = _filter_future_matches(matches, buffer_minutes=5)
    
    # FALLBACK: Pokud málo zápasů → rozšíř horizont
    if len(matches) < 3:
        print(f"[generate_tickets] ⚠️ Málo budoucích zápasů ({len(matches)}), rozšiřuji horizont na +3 dny...")
        all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in exclude_ids]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        print(f"[generate_tickets] Po rozšíření: {len(matches)} zápasů")
    
    result = ticket_generator.generate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
        pool_filter=_pool_filter_for_risk(req.risk_level),
    )

    # Zápasů může být dost (>= 3), a přesto z nich nevzejde tiket — třeba
    # když jde zrovna o dávku zápasů s malým vzorkem dat (nový tým, začátek
    # sezóny/poháru), kde appka žádnému nedá dost jasnou důvěru (viz
    # MIN_SELECTION_PROBABILITY). Dřívější fallback na širší horizont se
    # díval jen na POČET zápasů, ne na to, jestli z nich vůbec vzešel
    # tiket — appka proto zkusí širší okno ještě jednou, tentokrát podle
    # skutečného výsledku generování.
    if result["safe"] is None:
        print(f"[generate_tickets] ⚠️ Tiket se z {len(matches)} zápasů nesestavil, zkouším horizont +3 dny navíc...")
        all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in exclude_ids]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        print(f"[generate_tickets] Po rozšíření: {len(matches)} zápasů")
        result = ticket_generator.generate(
            matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
            pool_filter=_pool_filter_for_risk(req.risk_level),
        )

    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(user_id, used_ids)

    if result["safe"] is not None:
        _charge_tokens_for_ticket(user_id, result["safe"].ticket_type)

    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=TicketResponse.from_domain(result["aggressive"]) if result["aggressive"] else None,
    )


@app.post("/tickets/regenerate", response_model=TicketPairResponse)
def regenerate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    _check_token_balance(user_id, req.risk_level)
    all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    previous_ids = repo.get_last_batch(user_id)
    exclude_ids = repo.get_all_saved_match_ids(user_id)  # Všechny již vsazené zápasy
    
    # Vyfiltruj: vyloučit poslední batch + všechny uložené
    combined_exclude = set(previous_ids) | set(exclude_ids)
    matches = [m for m in all_matches if m.match_id not in combined_exclude]
    
    # FILTR: Odstranit zápasy v MINULOSTI (NEW!)
    matches = _filter_future_matches(matches, buffer_minutes=5)
    
    # FALLBACK: Pokud málo zápasů → rozšíř horizont
    if len(matches) < 3:
        print(f"[regenerate_tickets] ⚠️ Málo budoucích zápasů ({len(matches)}), rozšiřuji horizont na +3 dny...")
        all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in combined_exclude]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        print(f"[regenerate_tickets] Po rozšíření: {len(matches)} zápasů")
    
    result = ticket_generator.regenerate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days, list(previous_ids),
        pool_filter=_pool_filter_for_risk(req.risk_level),
    )

    # Viz stejná poznámka v generate_tickets — dost zápasů neznamená dost
    # KVALITNÍCH kandidátů, takže appka zkusí širší horizont podle
    # skutečného výsledku, ne jen podle počtu zápasů.
    if result["safe"] is None:
        print(f"[regenerate_tickets] ⚠️ Tiket se z {len(matches)} zápasů nesestavil, zkouším horizont +3 dny navíc...")
        all_matches = _fetch_candidate_matches(req.sports, req.time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in combined_exclude]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        print(f"[regenerate_tickets] Po rozšíření: {len(matches)} zápasů")
        result = ticket_generator.regenerate(
            matches, req.risk_level, req.sports, req.market_types, req.time_frame_days, list(previous_ids),
            pool_filter=_pool_filter_for_risk(req.risk_level),
        )

    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(user_id, used_ids)

    if result["safe"] is not None:
        _charge_tokens_for_ticket(user_id, result["safe"].ticket_type)

    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=TicketResponse.from_domain(result["aggressive"]) if result["aggressive"] else None,
    )


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
            league=s.league, country=s.country, kickoff_date=s.kickoff_date,
        ) for s in req.selections
    ]
    ticket = Ticket(
        ticket_type=req.ticket_type, selections=domain_selections,
        total_odds=req.total_odds, combined_probability=req.combined_probability,
        recommended_stake_pct=req.recommended_stake_pct,
    )
    ticket_id = repo.save_ticket(user_id, ticket)
    
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


@app.get("/tickets/saved", response_model=list[TicketResponse])
def list_saved_tickets(user_id: int = Depends(get_current_user_id)):
    """
    Appka před vrácením historie zkusí dosettlovat tikety uživatele, co
    jsou ještě 'pending' — takže i bez čekání na cron (/tickets/settle)
    uvidíš čerstvý stav, hned jak si historii otevřeš PO skončení zápasů.

    user_id appka bere VÝHRADNĚ z přihlašovacího tokenu — nikdy ne z
    parametru v URL, jinak by si kdokoli mohl jen změnit číslo v adrese
    a prohlížet si cizí tikety.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    pending_rows = [row for row in repo.get_saved_tickets(user_id) if row["status"] == "pending"]

    def _settle_row(row):
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        return row["ticket_id"], _try_settle_ticket(provider, row["ticket"], selection_ids)

    # Appka víc nevyřešených tiketů appka řeší SOUBĚŽNĚ — appka to dřív
    # dělala jeden po druhém, což při víc rozehraných tiketech zbytečně
    # natahovalo dobu, než se Historie vůbec zobrazila.
    with ThreadPoolExecutor(max_workers=4) as executor:
        for ticket_id, new_status in executor.map(_settle_row, pending_rows):
            if new_status is not None:
                repo.set_ticket_status(ticket_id, new_status)
                repo.set_live_alert(ticket_id, None)

    saved_rows = repo.get_saved_tickets(user_id)
    print(f"[DEBUG] /tickets/saved: Backend vrací CELKEM {len(saved_rows)} tiketů pro user_id={user_id}")
    pending_in_response = [r for r in saved_rows if r["status"] == "pending"]
    print(f"[DEBUG] /tickets/saved: Z toho PENDING: {len(pending_in_response)}")
    
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

    outcome = evaluate_selection_outcome(selection, result["home_goals"], result["away_goals"])
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
    risk_level: int = Field(ge=0, le=100)
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=5)
    exclude_match_ids: list[int] = []


@app.post("/tickets/replace-selection")
def replace_selection(req: TicketGenerateRequestWithExclude, user_id: int = Depends(get_current_user_id)):
    """Vygeneruje nový tiket bez vyloučených zápasů — používá se po kliknutí ✕ u výběru."""
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    matches = [m for m in matches if m.match_id not in set(req.exclude_match_ids)]
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
    Projde pending tikety uživatele a vyhodnotí je podle výsledků z API-Football.
    Volá se automaticky při otevření záložky Historie.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    pending_rows = db.fetch_ticket_rows(user_id=user_id, status="pending")
    settled = 0

    for row in pending_rows:
        ticket_id = row["ticket_id"]
        selections = row.get("selections", [])
        if not selections:
            continue

        all_won = True
        any_lost = False
        all_finished = True

        for sel in selections:
            match_id = sel.get("match_id")
            if not match_id or match_id == 0:
                all_finished = False
                continue
            try:
                fixture = provider.get_fixture_result(str(match_id))
                if not fixture:
                    all_finished = False
                    continue

                # API-Football vrací skóre v fixture["goals"]["home"] / ["away"]
                # Status: FT = konec, 1H/2H/HT = probíhá, NS = nezačal
                status = fixture.get("fixture", {}).get("status", {}).get("short", "NS")
                if status not in ("FT", "AET", "PEN"):
                    all_finished = False
                    continue

                home_score = fixture.get("goals", {}).get("home")
                away_score = fixture.get("goals", {}).get("away")

                if home_score is None or away_score is None:
                    all_finished = False
                    continue

                sel_won = _evaluate_selection(sel, {"home_score": home_score, "away_score": away_score})
                if sel_won is None:
                    all_finished = False
                elif not sel_won:
                    any_lost = True
                    all_won = False

                # Ulož výsledek jednotlivého výběru — hledej id různými způsoby
                sel_id = sel.get("id") or sel.get("selection_id")
                if sel_id:
                    sel_result = "won" if sel_won is True else "lost" if sel_won is False else "pending"
                    db.update_selection_result(int(sel_id), sel_result)
                else:
                    pass  # Selection bez ID - nelze uložit
            
            except Exception as e:
                all_finished = False

        if all_finished:
            new_status = "won" if (all_won and not any_lost) else "lost"
            db.update_ticket_status(ticket_id, new_status)
            if row.get("actual_stake_amount"):
                db.update_ticket_profit_loss(
                    ticket_id,
                    row["actual_stake_amount"],
                    row.get("actual_odds") or row.get("total_odds", 1),
                    new_status
                )
            settled += 1

    return {"settled": settled, "checked": len(pending_rows)}


def _evaluate_selection(sel: dict, result: dict) -> Optional[bool]:
    """Vyhodnotí jeden výběr podle výsledku zápasu. Vrátí True/False/None (neznámý)."""
    market = sel.get("market_type", "")
    selection = sel.get("selection", "")
    home_score = result.get("home_score")
    away_score = result.get("away_score")

    if home_score is None or away_score is None:
        return None

    total_goals = home_score + away_score

    if market == "match_winner":
        if selection == "home":
            return home_score > away_score
        elif selection == "away":
            return away_score > home_score
        elif selection in ("draw", "x"):
            return home_score == away_score

    elif market == "over_goals":
        try:
            line = float(selection.replace("over_", "").replace("under_", "").replace("over ", "").replace("under ", ""))
            if selection.startswith("under"):
                return total_goals < line
            return total_goals > line
        except Exception:
            return None

    elif market == "btts":
        return home_score > 0 and away_score > 0

    return None


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
# vygeneruje krátký + střední tiket, v úterý a pátek navíc i BOOST, uloží
# je do historie zadaného účtu a pošle je jako obrázky do Telegramu.
# Volá se z vnějšku (naplánovaná úloha), ne appka sama ze sebe — proto
# appka autorizaci řeší sdíleným tajným klíčem (ADMIN_TASK_KEY), ne
# přihlašovacím tokenem konkrétního uživatele.
# =====================================================================
DAILY_TICKETS_MARKETS = [MarketType.MATCH_WINNER, MarketType.OVER_GOALS]
DAILY_TICKETS_SPORTS = [Sport.FOOTBALL]
# Kč — appka tohle zaznamená jako "reálně vsazeno" u KAŽDÉHO auto-generovaného
# tiketu. Rozpětí (ne pevná částka) appka volí náhodně, ať výkladní skříň
# (viz /showcase/tickets) vypadá jako opravdové sázení různých lidí, ne jako
# jeden bot se stále stejnou částkou.
DAILY_TICKETS_STAKE_CHOICES = [200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000]


def _generate_one_ticket_for_cron(
    user_id: int, risk_level: int, sports: list[Sport], market_types: list[MarketType], time_frame_days: int,
) -> Optional[Ticket]:
    """Stejná logika jako /tickets/generate (fetch → vyluč už použité →
    vyfiltruj minulé zápasy → fallback na širší horizont, pokud je málo
    zápasů NEBO se z nich nepovede sestavit tiket), jen bez závislosti na
    přihlášeném uživateli z requestu."""
    exclude_ids = repo.get_all_saved_match_ids(user_id)

    all_matches = _fetch_candidate_matches(sports, time_frame_days)
    matches = [m for m in all_matches if m.match_id not in exclude_ids]
    matches = _filter_future_matches(matches, buffer_minutes=5)

    if len(matches) < 3:
        all_matches = _fetch_candidate_matches(sports, time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in exclude_ids]
        matches = _filter_future_matches(matches, buffer_minutes=5)

    result = ticket_generator.generate(
        matches, risk_level, sports, market_types, time_frame_days,
        pool_filter=_pool_filter_for_risk(risk_level),
    )

    if result["safe"] is None:
        all_matches = _fetch_candidate_matches(sports, time_frame_days + 3)
        matches = [m for m in all_matches if m.match_id not in exclude_ids]
        matches = _filter_future_matches(matches, buffer_minutes=5)
        result = ticket_generator.generate(
            matches, risk_level, sports, market_types, time_frame_days,
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
            }
            for s in ticket.selections
        ],
    }


@app.post("/admin/daily-tickets")
def run_daily_tickets(request: Request):
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

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

    # Úterý=1, pátek=4 — BOOST appka posílá jen 2x týdně (5denní horizont
    # se denně z velké části překrývá se včerejším, denní odesílání by
    # bylo skoro identické, jen s jinou kombinací nohou). Appka teď na
    # kratky/stredni generuje víc kusů denně (ne jen 1+1) — appka z toho
    # staví veřejnou "výkladní skříň" vyhraných tiketů (/showcase/tickets),
    # čím víc tiketů denně, tím víc má appka co ukázat.
    today_prague = datetime.now(ZoneInfo("Europe/Prague"))
    today_start_utc_naive = today_prague.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    plan = [("kratky", 20, 2, 6), ("stredni", 50, 2, 6)]
    if today_prague.weekday() in (1, 4):
        plan.append(("boost", 80, 5, 1))

    results = []
    generated_today: list[tuple[Ticket, int]] = []
    for label, risk_level, days, target_count in plan:
        already_today = db.count_tickets_since(target_user_id, label, today_start_utc_naive)
        to_generate = target_count - already_today
        if to_generate <= 0:
            results.append({"type": label, "status": "already_generated_today", "count": already_today})
            continue

        for _ in range(to_generate):
            # 12+ tiketů v jednom běhu appce zvyšuje šanci, že jedno
            # generování narazí na dočasný výpadek/rate-limit u
            # externího API — appka to zaloguje a zkusí další typ, místo
            # aby appka jednou chybou shodila CELÝ zbytek běhu (500).
            try:
                ticket = _generate_one_ticket_for_cron(
                    target_user_id, risk_level, DAILY_TICKETS_SPORTS, DAILY_TICKETS_MARKETS, days,
                )
            except Exception as e:
                print(f"[daily-tickets] {label}: generování selhalo: {e}")
                results.append({"type": label, "status": "generation_error", "error": str(e)})
                break
            if ticket is None:
                results.append({"type": label, "status": "failed_to_generate"})
                break  # appka pro tenhle typ zjevně došly použitelné zápasy, další pokus by zase selhal

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

    return {"date": today_prague.isoformat(), "settled": settled_count, "results": results}


class AdminSeedShowcaseRequest(BaseModel):
    ticket_type: str
    selections: list[SaveSelectionRequest]
    total_odds: float
    combined_probability: float = 0.0
    recommended_stake_pct: float = 0.0
    stake_amount: float
    created_at: Optional[str] = None  # ISO datetime — appka zachová reálné datum starší výhry


@app.post("/admin/showcase/seed")
def admin_seed_showcase(req: AdminSeedShowcaseRequest, request: Request):
    """
    Appka tímhle ručně přidá do výkladní skříně (/showcase/tickets) starší
    JIŽ VYHRANÉ tikety appky (nejčastěji z testovacích účtů) — appka je
    zkopíruje pod DAILY_TICKETS_USER_ID účet se zachovaným datem, appka
    nemění nic na tom, co se reálně stalo (appka tikety zkopíruje 1:1,
    jen appka je fyzicky přesune pod účet, ze kterého veřejná appka čte).
    """
    admin_key_expected = os.environ.get("ADMIN_TASK_KEY")
    if not admin_key_expected or request.headers.get("X-Admin-Key") != admin_key_expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")

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
            league=s.league, country=s.country, kickoff_date=s.kickoff_date,
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
    repo.set_ticket_status(ticket_id, "won")
    return {"ticket_id": ticket_id, "status": "seeded"}


@app.get("/showcase/tickets")
def showcase_tickets(limit: int = 20):
    """
    Veřejná "výkladní skříň" appky — bez přihlášení appka vrátí poslední
    VYHRANÉ tikety ze svého vlastního denního automatického generování
    (DAILY_TICKETS_USER_ID, viz /admin/daily-tickets), NIKDY tikety
    běžných uživatelů appky. Slouží jako sociální důkaz na hlavní
    obrazovce appky pro nové návštěvníky.
    """
    limit = max(1, min(limit, 50))
    target_user_id_raw = os.environ.get("DAILY_TICKETS_USER_ID")
    if not target_user_id_raw:
        return {"tickets": []}
    target_user_id = int(target_user_id_raw)

    rows = repo.get_saved_tickets(target_user_id)
    won_rows = [r for r in rows if r["status"] == "won"]
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
                "model": "claude-sonnet-4-6",
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
                "model": "claude-sonnet-4-6",
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
            "model": "claude-sonnet-4-6",
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


