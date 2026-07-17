"""
ApexSignal — Backend API
Modul: backend_api.py

REST + WebSocket vrstva spojující:
    - probability_model.TicketGenerator  (Generátor tiketů)
    - momentum_filter.MomentumFilter      (Live Signal Engine)
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
from datetime import datetime

import os
import json
import requests
import aiohttp
import asyncio
import logging
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from probability_model import (
    TicketGenerator, MatchInput, Sport, MarketType, Ticket, SelectionCandidate, evaluate_selection_outcome,
)
from momentum_filter import MomentumFilter, MatchSnapshot, MomentumSignal, SignalType
import data_provider
import ai_reviewer
import db
import auth
import rate_limiter

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

# Povolí volání z frontendu hostovaného jinde (Netlify, lokální vývoj...).
# Pro produkci doporučuju zúžit allow_origins na konkrétní doménu tvého
# Netlify webu místo "*" — jakmile budeš znát finální URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# =====================================================================
# Pydantic schémata (request/response kontrakty)
# =====================================================================
class TicketGenerateRequest(BaseModel):
    risk_level: int = Field(ge=0, le=100)
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=4)  # Horizont: 1-4 dny (už ne konkrétní data)


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
    live_alert: Optional[str] = None   # viz _check_ticket_contradictions — appka sem píše, když živý signál odporuje výběru
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


class LiveSignalResponse(BaseModel):
    match_id: int
    home_team: str
    away_team: str
    minute: int
    market: str
    odds: Optional[float]
    reasoning: str
    recommended_stake_pct: float
    signal_type: str
    is_real_pressure: bool
    team_side: str
    sent_at: datetime

    @classmethod
    def from_domain(cls, s: MomentumSignal, home_team: str = "Domácí", away_team: str = "Hosté", minute: int = 0) -> "LiveSignalResponse":
        return cls(
            match_id=s.match_id, home_team=home_team, away_team=away_team, minute=minute,
            market=s.market, odds=s.odds,
            reasoning=s.reasoning, recommended_stake_pct=s.recommended_stake_pct,
            signal_type=s.signal_type.value, is_real_pressure=s.is_real_pressure,
            team_side=s.team_side, sent_at=datetime.utcnow(),
        )


class LiveStatTickRequest(BaseModel):
    """Vstupní data pro jednu minutu zápasu — typicky volá interní poller,
    který stahuje live data z `data_provider.get_live_match_stats()`."""
    minute: int
    home_team: str = "Domácí"
    away_team: str = "Hosté"
    home_possession: int
    away_possession: int
    home_shots_on_target: int
    away_shots_on_target: int
    home_dangerous_attacks: int
    away_dangerous_attacks: int
    home_corners: int = 0
    away_corners: int = 0
    red_cards_home: int = 0
    red_cards_away: int = 0
    home_goals: int = 0
    away_goals: int = 0


# =====================================================================
# Repository — tikety persistované v PostgreSQL (viz db.py), live signály
# záměrně dál jen v paměti procesu (transientní stav běžících zápasů,
# jeho ztráta při restartu je tolerovatelná — na rozdíl od tiketové
# historie, kde by ztráta dat zničila smysl track recordu a kalibrace).
# =====================================================================
class Repo:
    SIGNAL_FOLLOWUP_MINUTES = 15  # jak dlouho appka čeká na "trefu", než signál vyhodnotí jako miss
    FLAT_STAKE_PCT = 2.0          # srovnávací vklad "rovných X % na každý tiket bez ohledu na Kelly"
    CALIBRATION_BUCKET_WIDTH_PCT = 10

    def __init__(self):
        self._momentum_filters: dict[int, MomentumFilter] = {}
        self._last_batch_match_ids: dict[int, list[int]] = {}  # user_id -> match_ids z posledního generování
        self._signal_log: list[dict] = []  # historie odeslaných entry signálů + jejich výsledek (live signály, v paměti)
        db.ensure_schema()

    # --- Tikety: persistované, viz db.py -------------------------------

    def save_ticket(self, user_id: int, ticket: Ticket) -> int:
        return db.insert_ticket(user_id, ticket)

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

    def get_tickets_for_match(self, match_id: int) -> list[tuple[int, Ticket]]:
        """Všechny uložené, ještě nevyhodnocené tikety obsahující výběr na
        tenhle zápas — používá se pro křížovou kontrolu s živými signály
        (viz _check_ticket_contradictions)."""
        rows = db.fetch_ticket_rows(status="pending")
        return [(r["ticket_id"], r["ticket"]) for r in rows if any(s.match_id == match_id for s in r["ticket"].selections)]

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

    # --- Live signály: záměrně v paměti (viz docstring třídy) ----------

    def get_momentum_filter(self, match_id: int) -> MomentumFilter:
        if match_id not in self._momentum_filters:
            self._momentum_filters[match_id] = MomentumFilter(match_id=match_id)
        return self._momentum_filters[match_id]

    def set_last_batch(self, user_id: int, match_ids: list[int]) -> None:
        self._last_batch_match_ids[user_id] = match_ids

    def get_last_batch(self, user_id: int) -> list[int]:
        return self._last_batch_match_ids.get(user_id, [])

    def log_signal(self, match_id: int, team_side: str, minute: int, momentum_score: float, team_goals_at_signal: int) -> None:
        """Zapíše nový entry signál jako 'pending' — výsledek se doplní později přes update_signal_outcomes."""
        self._signal_log.append({
            "match_id": match_id, "team_side": team_side, "fired_at_minute": minute,
            "momentum_score": momentum_score, "team_goals_at_signal": team_goals_at_signal,
            "outcome": "pending",
        })

    def update_signal_outcomes(self, match_id: int, current_minute: int, home_goals: int, away_goals: int) -> None:
        """
        Pro všechny dosud nevyhodnocené signály tohoto zápasu zkontroluje,
        jestli straně, na kterou appka signál poslala, přibyl gól od chvíle
        signálu (= hit), nebo jestli už uplynulo SIGNAL_FOLLOWUP_MINUTES
        bez gólu (= miss). Volá se při každém pollu pro daný zápas.
        """
        current_goals = {"home": home_goals, "away": away_goals}
        for entry in self._signal_log:
            if entry["match_id"] != match_id or entry["outcome"] != "pending":
                continue
            if current_goals[entry["team_side"]] > entry["team_goals_at_signal"]:
                entry["outcome"] = "hit"
            elif current_minute - entry["fired_at_minute"] > self.SIGNAL_FOLLOWUP_MINUTES:
                entry["outcome"] = "miss"

    def get_track_record(self) -> dict:
        resolved = [e for e in self._signal_log if e["outcome"] != "pending"]
        hits = sum(1 for e in resolved if e["outcome"] == "hit")
        total = len(resolved)
        return {
            "total_resolved": total,
            "hits": hits,
            "misses": total - hits,
            "hit_rate_pct": round(hits / total * 100, 1) if total else None,
            "pending": sum(1 for e in self._signal_log if e["outcome"] == "pending"),
        }


repo = Repo()
ticket_generator = TicketGenerator()


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
    
    # FILTR: Skip zápasy s špatnými daty - games=1, games=0
    if home_fallback or away_fallback:
        print(f"[SKIP] {fixture['home_team']} vs {fixture['away_team']} - fallback data, ignoruji")
        return None
    odds = data_provider.adapt_api_football_odds(provider.get_pre_match_odds(fixture["id"]))
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
                pass  # Zápas se nepodařilo načíst, přeskoč
        
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
    napárované na zápas podle přesné shody jména týmu/hráče. Tichá no-op,
    pokud ODDSAPI_KEY není nastaven — appka pak běží jen na vlastním odhadu.
    """
    try:
        odds_provider = data_provider.OddsAPIProvider()
    except RuntimeError:
        return

    events = odds_provider.get_odds(sport)
    by_pair = {(e["home_team"], e["away_team"]): e for e in events}
    totals_market = {
        Sport.FOOTBALL: MarketType.OVER_GOALS, Sport.HOCKEY: MarketType.OVER_GOALS,
        Sport.BASKETBALL: MarketType.OVER_POINTS, Sport.TENNIS: MarketType.OVER_GAMES,
    }[sport]

    for match in matches:
        event = by_pair.get((match.home_team, match.away_team))
        if not event:
            continue
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


def _fetch_candidate_matches(sports: list[Sport], time_frame_days: int) -> list[MatchInput]:
    """
    Vrátí zápasy v daném horziontu dnů (bez konkrétních dat).
    Důvod: horizont (1-4 dny) je jednodušší, přirozený a bez chyb
    oproti parsování konkrétního YYYY-MM-DD data.
    
    Filtruje jen ligy dostupné na Tipsportu (podle league_id)!
    Data_provider.get_upcoming_matches() vrátí přesně zápasy v tomto rozsahu.
    """
    # Liga IDs dostupné na Tipsportu
    TIPSPORT_LEAGUE_IDS = {
        149535,  # MS 2026
        3152,    # Copa Libertadores
        9002,    # Evropský superpohár
        120,     # Česká Chance Liga
        118,     # 1. anglická liga
        39,      # 1. brazilská liga
        35488,   # 1. čínská liga
        24350,   # 1. estonská liga
        123,     # 1. finská liga
        33,      # 1. irská liga
        126,     # 1. islandská liga
        137652,  # 1. jihokorejská liga
        69075,   # 1. kazašská liga
        131,     # 1. norská liga
        144,     # 1. švédská liga
        50668,   # 2. argentinská liga
        50188,   # 2. brazilská liga
        87227,   # USA - USL Championship
        50,      # 2. norská liga
    }
    
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

        raw_items = provider.get_upcoming_matches(sport, time_frame_days)
        sport_matches = builders[sport](provider, raw_items)
        
        # Filtruj jen zápasy z lig dostupných na Tipsportu - podle league_id!
        if sport == Sport.FOOTBALL:
            sport_matches = [
                m for m in sport_matches 
                if m.league_id in TIPSPORT_LEAGUE_IDS
            ]
        
        _enrich_with_market_odds(sport_matches, sport)
        matches.extend(sport_matches)
    return matches


# =====================================================================
# REST endpointy — Generátor tiketů
# =====================================================================
@app.post("/tickets/generate", response_model=TicketPairResponse)
def generate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    result = ticket_generator.generate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days,
        pool_filter=ai_reviewer.review_candidates,
    )
    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(user_id, used_ids)

    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=TicketResponse.from_domain(result["aggressive"]) if result["aggressive"] else None,
    )


@app.post("/tickets/regenerate", response_model=TicketPairResponse)
def regenerate_tickets(req: TicketGenerateRequest, user_id: int = Depends(get_current_user_id)):
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    previous_ids = repo.get_last_batch(user_id)
    result = ticket_generator.regenerate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days, previous_ids,
        pool_filter=ai_reviewer.review_candidates,
    )
    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(user_id, used_ids)

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


@app.get("/tickets/saved", response_model=list[TicketResponse])
def list_saved_tickets(user_id: int = Depends(get_current_user_id)):
    """
    Appka před vrácením historie zkusí dosettlovat tikety uživatele, co
    jsou ještě 'pending' — takže i bez čekání na cron (/tickets/settle)
    uvidíš čerstvý stav, hned jak si historii otevřeš PO skončení zápasů.
    Případné upozornění na rozpor s živým signálem (viz
    _check_ticket_contradictions) appka jednou vyhodnoceným tiketům
    smaže — po skončení zápasu už není co sledovat.

    user_id appka bere VÝHRADNĚ z přihlašovacího tokenu — nikdy ne z
    parametru v URL, jinak by si kdokoli mohl jen změnit číslo v adrese
    a prohlížet si cizí tikety.
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    pending_count = 0
    for row in repo.get_saved_tickets(user_id):
        if row["status"] == "pending":
            pending_count += 1
            ticket_id = row["ticket_id"]
            selection_ids = [s.get("id") for s in row.get("selections", [])]
            new_status = _try_settle_ticket(provider, row["ticket"], selection_ids)
            if new_status is not None:
                repo.set_ticket_status(ticket_id, new_status)
                repo.set_live_alert(ticket_id, None)

    saved_rows = repo.get_saved_tickets(user_id)
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
    
    leg_results: list[Optional[bool]] = []
    for i, selection in enumerate(ticket.selections):
        try:
            raw_result = provider.get_fixture_result(selection.match_id)
            result = data_provider.adapt_fixture_result(raw_result)
            print(f"  [{i}] {selection.home_team} vs {selection.away_team}: finished={result.get('is_finished')}, goals={result.get('home_goals')}-{result.get('away_goals')}")
        except Exception as e:
            print(f"  [{i}] API ERROR: {str(e)}")
            leg_results.append(None)
            if selection_ids and i < len(selection_ids):
                db.update_selection_result(selection_ids[i], "pending")
                print(f"      → saved pending (API error) id={selection_ids[i]}")
            continue
        
        # Určit výsledek nebo pending
        if not result["is_finished"] or result["home_goals"] is None:
            print(f"      → Match NOT finished, saving pending")
            leg_results.append(None)
            if selection_ids and i < len(selection_ids):
                db.update_selection_result(selection_ids[i], "pending")
                print(f"      → saved pending id={selection_ids[i]}")
            continue
        
        outcome = evaluate_selection_outcome(selection, result["home_goals"], result["away_goals"])
        leg_results.append(outcome)
        print(f"      → Match finished, outcome={outcome}")
        
        # Ulož výsledek zápasu v DB
        if selection_ids and i < len(selection_ids):
            selection_id = selection_ids[i]
            result_str = "won" if outcome is True else "lost" if outcome is False else "pending"
            db.update_selection_result(selection_id, result_str)
            print(f"      → saved {result_str} id={selection_id}")

    if any(r is False for r in leg_results):
        return "lost"
    if leg_results and all(r is True for r in leg_results):
        return "won"
    return None  # zápas(y) ještě neskončily, nebo appka trh neumí vyhodnotit čistě ze skóre


@app.post("/tickets/settle")
def settle_tickets():
    """
    Projde VŠECHNY dosud nevyhodnocené uložené tikety (napříč uživateli)
    a zkusí je vyhodnotit — viz _try_settle_ticket. V produkci tohle
    pustíš na časovač (stejně jako /live-signals/poll), např. jednou
    denně ráno po odehraných zápasech — tak se historie udržuje aktuální
    i bez toho, aby si uživatel appku zrovna otevřel. (Appka navíc totéž
    dělá i při čtení historie přes /tickets/saved, viz tam — takže i bez
    cronu se stav osvěží ve chvíli, kdy si uživatel historii prohlédne.)
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    settled = 0
    # get_pending_tickets vrací jen (ticket_id, ticket), potřebuji i selection_ids
    # Proto si je vezmu z DB query přímo
    all_pending = db.fetch_ticket_rows(status="pending")
    for row in all_pending:
        ticket_id = row["ticket_id"]
        ticket = row["ticket"]
        selection_ids = [s.get("id") for s in row.get("selections", [])]
        new_status = _try_settle_ticket(provider, ticket, selection_ids)
        if new_status is not None:
            repo.set_ticket_status(ticket_id, new_status)
            settled += 1

    return {"settled_this_run": settled, "still_pending": len(repo.get_pending_tickets())}


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


# =====================================================================
# Live Signal Engine — ingest + WebSocket distribuce
# =====================================================================
class ConnectionManager:
    """
    Spojení klíčuje per match_id (kdo sleduje konkrétní zápas), ale navíc
    udržuje speciální kanál GLOBAL_CHANNEL — appka v `index.html` neví
    dopředu, jaké zápasy jsou zrovna live, takže se připojuje na "všechno
    najednou" a filtruje si signály sama podle sportu/kategorie. Broadcast
    proto vždy posílá na obě místa: per-match i globální.
    """
    GLOBAL_CHANNEL = -1  # sentinel "match_id", nikdy se nestřetne s reálným ID

    def __init__(self):
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, match_id: int, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(match_id, []).append(ws)

    def disconnect(self, match_id: int, ws: WebSocket):
        if match_id in self._connections and ws in self._connections[match_id]:
            self._connections[match_id].remove(ws)

    async def broadcast(self, match_id: int, payload: dict):
        for ws in self._connections.get(match_id, []):
            await ws.send_json(payload)
        if match_id != self.GLOBAL_CHANNEL:
            for ws in self._connections.get(self.GLOBAL_CHANNEL, []):
                await ws.send_json(payload)


ws_manager = ConnectionManager()


@app.post("/live-signals/poll")
async def poll_live_fixtures():
    """
    Jeden cyklus pollování: stáhne všechny právě běžící zápasy z API-Football,
    pro každý vezme aktuální minutové statistiky, propustí je MomentumFilterem
    a každý vzniklý signál rozešle přes WebSocket.

    V produkci tohle nevoláš ručně — pustíš to na časovač (např. APScheduler
    uvnitř aplikace, nebo externí cron jako cron-job.org, co bude tento
    endpoint volat každých ~60 s, podle rate limitu tvého RapidAPI plánu).
    """
    provider = data_provider.get_provider(Sport.FOOTBALL)
    live_fixtures = provider.get_live_fixtures()
    signals_sent = 0

    for raw_fixture in live_fixtures:
        match_id = raw_fixture["fixture"]["id"]
        minute = raw_fixture["fixture"]["status"].get("elapsed") or 0

        live_raw = provider.get_live_match_stats(match_id)
        live_data = data_provider.adapt_api_football_live_stats(minute, live_raw)

        snapshot = MatchSnapshot(
            minute=live_data["minute"],
            home_possession=live_data["possession"]["home"], away_possession=live_data["possession"]["away"],
            home_shots_on_target=live_data["shots_on_target"]["home"], away_shots_on_target=live_data["shots_on_target"]["away"],
            home_dangerous_attacks=live_data["dangerous_attacks"]["home"], away_dangerous_attacks=live_data["dangerous_attacks"]["away"],
            home_corners=live_data["corners"]["home"], away_corners=live_data["corners"]["away"],
            red_cards_home=live_data["red_cards"]["home"], red_cards_away=live_data["red_cards"]["away"],
            home_goals=live_data["goals"]["home"], away_goals=live_data["goals"]["away"],
        )

        mf = repo.get_momentum_filter(match_id)
        signal = mf.ingest(snapshot)

        # Bez ohledu na to, jestli teď vznikl nový signál, appka zkontroluje
        # výsledky dřívějších pending signálů na tomhle zápase (padl gól? /
        # vypršel čas na "trefu"?) — drží track record aktuální každý poll.
        repo.update_signal_outcomes(match_id, minute, snapshot.home_goals, snapshot.away_goals)

        if signal is not None:
            home_team = raw_fixture["teams"]["home"]["name"]
            away_team = raw_fixture["teams"]["away"]["name"]
            if signal.signal_type == SignalType.ENTRY:
                market_verdict = _enrich_with_live_odds_and_check_market(provider, signal)
                if market_verdict is False:
                    continue  # živý trh se hýbe opačným směrem než náš signál — appka ho nepošle

                ai_note = ai_reviewer.review_live_signal(signal, home_team, away_team)
                if ai_note:
                    signal.reasoning += f" AI kontrola čerstvých zpráv: {ai_note}"

                team_goals_now = snapshot.home_goals if signal.team_side == "home" else snapshot.away_goals
                repo.log_signal(match_id, signal.team_side, minute, signal.momentum_score_team, team_goals_now)

            _check_ticket_contradictions(signal, minute)

            response = LiveSignalResponse.from_domain(signal, home_team, away_team, minute)
            await ws_manager.broadcast(match_id, response.model_dump(mode="json"))
            signals_sent += 1

    return {"checked_fixtures": len(live_fixtures), "signals_sent": signals_sent}


def _signal_contradicts_selection(selection: SelectionCandidate, signal: MomentumSignal) -> bool:
    """
    Porovná živý signál s konkrétním uloženým tiketovým výběrem na STEJNÝ
    zápas. Appka to nehlásí jako "tiket je špatně" — jen že se realita
    zápasu právě rozchází s tím, na čem byl tiket postaven, a stojí za to
    se na zápas mrknout.
    """
    if signal.signal_type == SignalType.CASHOUT:
        return True  # červená karta apod. — vždy stojí za upozornění, bez ohledu na trh výběru
    if selection.market_type == MarketType.MATCH_WINNER:
        if selection.selection == "home" and signal.team_side == "away":
            return True
        if selection.selection == "away" and signal.team_side == "home":
            return True
        if selection.selection == "draw":
            return True  # jakýkoli směrový tlak je v rozporu s předpokladem remízy
    return False  # over_goals/btts: živý tlak spíš POTVRZUJE, ne odporuje výběru


def _check_ticket_contradictions(signal: MomentumSignal, minute: int) -> None:
    """
    Projde uložené (a ještě nevyhodnocené) tikety, co obsahují výběr na
    tenhle konkrétní zápas, a pokud živý signál odporuje některému z nich,
    zapíše na ten tiket krátké upozornění — appka ho ukáže, hned jak si
    uživatel historii/tikety otevře (viz /tickets/saved), bez potřeby
    push notifikací nebo per-uživatelského WebSocketu.
    """
    for ticket_id, ticket in repo.get_tickets_for_match(signal.match_id):
        for selection in ticket.selections:
            if selection.match_id != signal.match_id:
                continue
            if _signal_contradicts_selection(selection, signal):
                side_label = "domácí" if signal.team_side == "home" else "hosté"
                repo.set_live_alert(
                    ticket_id,
                    f"[{minute}'] Živý signál ({side_label}, {signal.market}) je v rozporu s výběrem "
                    f"\"{selection.market_type.value} {selection.selection}\" na {selection.home_team} vs "
                    f"{selection.away_team}. Appka tiket nemění, jen na to upozorňuje — mrkni se na zápas.",
                )
                break  # jeden rozpor na tiket stačí, appka ho nepřepisuje dalšími výběry ze stejného zápasu


@app.get("/live-signals/track-record")
def get_track_record():
    """
    Agregovaná úspěšnost živých signálů: kolik z odeslaných entry signálů
    skutečně vedlo ke gólu dané strany do SIGNAL_FOLLOWUP_MINUTES minut.
    Pozn.: žije jen v paměti procesu — restart serveru track record vynuluje.
    """
    return repo.get_track_record()


# Baseline kurzu při PRVNÍM pozorování pro daný (zápas, strana) — proti
# němu appka porovnává každé další pozorování, aby poznala, jestli se
# trh hýbe ve prospěch signálu, nebo proti němu. Žije jen po dobu běhu
# procesu (restart serveru ho vynuluje) — to je v pořádku, baseline má
# smysl jen v rámci jednoho live zápasu.
_live_odds_baseline: dict[tuple, float] = {}
MARKET_CONFIRM_SHORTEN_PCT = 0.03    # kurz se zkrátil o 3 %+ -> trh signál potvrzuje
MARKET_DISAGREE_LENGTHEN_PCT = 0.03  # kurz se prodloužil o 3 %+ -> trh nesouhlasí, appka signál potlačí


def _enrich_with_live_odds_and_check_market(provider, signal) -> Optional[bool]:
    """
    Doplní signal.odds reálnou live cenou na trh 'Next Goal' z API-Football
    a porovná ji s první zaznamenanou cenou pro tenhle zápas+stranu:

    - kurz se mezitím o MARKET_CONFIRM_SHORTEN_PCT a víc zkrátil -> trh
      signál potvrzuje, appka k odůvodnění přidá poznámku, vrací True.
    - kurz se naopak o MARKET_DISAGREE_LENGTHEN_PCT a víc prodloužil ->
      trh nesouhlasí, appka signál nepošle, vrací False.
    - cokoli jiného (žádná data, malý pohyb, první pozorování) -> appka
      signál pošle normálně beze změny, vrací None.

    Bez živých kurzů (beta endpoint /odds/live nedostupný na tvém účtu)
    appka tiše vrací None — tahle vrstva NIKDY appku neshodí ani nezablokuje
    základní fungování čistě na matematice.
    """
    try:
        live_odds_raw = provider.get_live_odds(signal.match_id)
        odds = data_provider.adapt_live_odds_for_signal(live_odds_raw, signal.team_side)
        if data_provider.is_live_market_blocked(live_odds_raw):
            signal.reasoning += " Pozn.: bookmaker zrovna live sázení na tenhle zápas pozastavil, kurz se za pár sekund může změnit."
    except Exception:
        odds = None

    if odds is None:
        return None

    signal.odds = odds
    key = (signal.match_id, signal.team_side)
    baseline = _live_odds_baseline.get(key)
    if baseline is None:
        _live_odds_baseline[key] = odds
        return None  # první pozorování pro tenhle zápas+stranu, nemáme s čím srovnat

    movement_pct = (baseline - odds) / baseline  # kladné = kurz se zkrátil
    if movement_pct >= MARKET_CONFIRM_SHORTEN_PCT:
        signal.reasoning += (
            f" Živý kurz se od začátku tlaku zkrátil o {round(movement_pct * 100, 1)} % "
            f"— trh tlak potvrzuje nezávisle na našem modelu."
        )
        return True
    if movement_pct <= -MARKET_DISAGREE_LENGTHEN_PCT:
        return False
    return None


@app.post("/live-signals/ingest/{match_id}", response_model=Optional[LiveSignalResponse])
async def ingest_live_tick(match_id: int, tick: LiveStatTickRequest):
    """
    Alternativa k /live-signals/poll pro manuální/testovací vstup jedné minuty
    dat (např. když chceš nahrát historický zápas krok po kroku, nebo máš
    vlastní zdroj dat místo API-Football).
    """
    mf = repo.get_momentum_filter(match_id)
    snapshot = MatchSnapshot(
        minute=tick.minute,
        home_possession=tick.home_possession, away_possession=tick.away_possession,
        home_shots_on_target=tick.home_shots_on_target, away_shots_on_target=tick.away_shots_on_target,
        home_dangerous_attacks=tick.home_dangerous_attacks, away_dangerous_attacks=tick.away_dangerous_attacks,
        home_corners=tick.home_corners, away_corners=tick.away_corners,
        red_cards_home=tick.red_cards_home, red_cards_away=tick.red_cards_away,
        home_goals=tick.home_goals, away_goals=tick.away_goals,
    )
    signal = mf.ingest(snapshot)
    if signal is None:
        return None

    response = LiveSignalResponse.from_domain(signal, tick.home_team, tick.away_team, tick.minute)
    await ws_manager.broadcast(match_id, response.model_dump(mode="json"))
    return response


@app.websocket("/ws/live-signals/{match_id}")
async def live_signals_ws(websocket: WebSocket, match_id: int):
    """Frontend se připojí sem a dostává push notifikace v reálném čase."""
    await ws_manager.connect(match_id, websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive ping z klienta, payload neřešíme
    except WebSocketDisconnect:
        ws_manager.disconnect(match_id, websocket)


@app.websocket("/ws/live-signals")
async def live_signals_ws_global(websocket: WebSocket):
    """
    Globální verze bez match_id — appka v index.html neví dopředu, jaké
    zápasy jsou zrovna live, takže se připojuje sem a dostává VŠECHNY
    signály napříč zápasy (filtr na sport/kategorii řeší sama na klientovi).
    """
    await ws_manager.connect(ws_manager.GLOBAL_CHANNEL, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws_manager.GLOBAL_CHANNEL, websocket)


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
        pool_filter=ai_reviewer.review_candidates,
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


@app.get("/admin/clear-cache")
def clear_cache_get():
    """Vymaže celou API cache — jednorázové použití po opravě formátu dat. GET pro snadné volání z prohlížeče."""
    count = db.cache_clear_all()
    return {"deleted": count, "status": "cache cleared"}


@app.delete("/admin/cache")
def clear_cache(user_id: int = Depends(get_current_user_id)):
    """Vymaže celou API cache — použij po nasazení oprav formátu dat."""
    count = db.cache_clear_all()
    return {"deleted": count, "status": "cache cleared"}


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


