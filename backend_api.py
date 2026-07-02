"""
ApexSignal — Backend API
Modul: backend_api.py
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Request
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

FIXTURE_ENRICHMENT_WORKERS = 8

app = FastAPI(title="ApexSignal API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user_id(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme)) -> int:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Chybí přihlašovací token")
    user_id = auth.verify_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token je neplatný nebo vypršel — přihlas se znovu")
    return user_id


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


def _client_ip(request: Request) -> str:
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
    return AuthResponse(token=auth.create_token(user_id), user_id=user_id, email=req.email)


@app.post("/auth/login", response_model=AuthResponse)
def login(req: LoginRequest, request: Request):
    client_ip = _client_ip(request)
    if rate_limiter.is_locked_out(req.email, client_ip):
        raise HTTPException(status_code=429, detail="Příliš mnoho pokusů o přihlášení. Zkus to znovu za chvíli.")
    user = db.get_user_by_email(req.email)
    if not user or not auth.verify_password(req.password, user["password_hash"]):
        rate_limiter.record_failed_attempt(req.email, client_ip)
        raise HTTPException(status_code=401, detail="Špatný e-mail nebo heslo")
    rate_limiter.record_success(req.email, client_ip)
    return AuthResponse(token=auth.create_token(user["id"]), user_id=user["id"], email=user["email"])


class TicketGenerateRequest(BaseModel):
    risk_level: int = Field(ge=0, le=100)
    sports: list[Sport]
    market_types: list[MarketType]
    time_frame_days: int = Field(ge=1, le=5)


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

    @classmethod
    def from_domain(cls, c: SelectionCandidate) -> "SelectionResponse":
        return cls(
            match_id=c.match_id, home_team=c.home_team, away_team=c.away_team,
            market_type=c.market_type, selection=c.selection,
            probability=round(c.probability, 4), odds=c.odds,
            model_probability=round(c.model_probability, 4),
            market_probability=round(c.market_probability, 4) if c.market_probability is not None else None,
            edge=c.edge, reasoning=c.reasoning, data_quality=c.data_quality,
        )


class TicketResponse(BaseModel):
    ticket_id: Optional[int]
    ticket_type: str
    total_odds: float
    combined_probability: float
    recommended_stake_pct: float
    summary: str
    status: str
    live_alert: Optional[str] = None
    actual_stake_amount: Optional[float] = None
    actual_odds: Optional[float] = None
    actual_profit_loss: Optional[float] = None
    selections: list[SelectionResponse]

    @classmethod
    def from_domain(cls, ticket: Ticket, ticket_id: Optional[int] = None, status: str = "pending",
                     live_alert: Optional[str] = None, actual_stake_amount: Optional[float] = None,
                     actual_odds: Optional[float] = None, actual_profit_loss: Optional[float] = None) -> "TicketResponse":
        return cls(
            ticket_id=ticket_id, ticket_type=ticket.ticket_type, total_odds=ticket.total_odds,
            combined_probability=ticket.combined_probability, recommended_stake_pct=ticket.recommended_stake_pct,
            summary=ticket.summary, status=status, live_alert=live_alert,
            actual_stake_amount=actual_stake_amount, actual_odds=actual_odds, actual_profit_loss=actual_profit_loss,
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
            market=s.market, odds=s.odds, reasoning=s.reasoning,
            recommended_stake_pct=s.recommended_stake_pct, signal_type=s.signal_type.value,
            is_real_pressure=s.is_real_pressure, team_side=s.team_side, sent_at=datetime.utcnow(),
        )


class LiveStatTickRequest(BaseModel):
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


class Repo:
    SIGNAL_FOLLOWUP_MINUTES = 15
    FLAT_STAKE_PCT = 2.0
    CALIBRATION_BUCKET_WIDTH_PCT = 10

    def __init__(self):
        self._momentum_filters: dict[int, MomentumFilter] = {}
        self._last_batch_match_ids: dict[int, list[int]] = {}
        self._signal_log: list[dict] = []
        db.ensure_schema()

    def save_ticket(self, user_id: int, ticket: Ticket) -> int:
        return db.insert_ticket(user_id, ticket)

    def set_actual_stake(self, ticket_id: int, stake_amount: float, odds: float) -> bool:
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
        won = sum(1 for r in resolved if r["status"] == "won")
        cumulative = 0.0
        history = []
        for r in sorted(resolved, key=lambda row: row["ticket_id"]):
            pl = self._compute_actual_profit_loss(r)
            cumulative += pl
            history.append({"ticket_id": r["ticket_id"], "profit_loss": pl, "cumulative_profit_loss": round(cumulative, 2)})
        return {
            "total_tickets_staked": len(staked_rows), "total_resolved": len(resolved),
            "pending": len(staked_rows) - len(resolved),
            "win_rate_pct": round(won / len(resolved) * 100, 1) if resolved else None,
            "total_staked": round(total_staked, 2), "total_profit_loss": round(total_pl, 2),
            "roi_pct": round(total_pl / total_staked * 100, 1) if total_staked else None,
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
            "total_resolved": total, "won": won, "lost": total - won,
            "win_rate_pct": round(won / total * 100, 1) if total else None,
            "pending": sum(1 for r in rows if r.get("status", "pending") == "pending"),
        }

    def get_calibration_report(self, user_id: int) -> dict:
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
        return {"total_resolved": len(resolved), "brier_score": round(brier_sum / len(resolved), 4), "buckets": buckets}

    def get_roi_report(self, user_id: int) -> dict:
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
        }

    def get_momentum_filter(self, match_id: int) -> MomentumFilter:
        if match_id not in self._momentum_filters:
            self._momentum_filters[match_id] = MomentumFilter(match_id=match_id)
        return self._momentum_filters[match_id]

    def set_last_batch(self, user_id: int, match_ids: list[int]) -> None:
        self._last_batch_match_ids[user_id] = match_ids

    def get_last_batch(self, user_id: int) -> list[int]:
        return self._last_batch_match_ids.get(user_id, [])

    def log_signal(self, match_id: int, team_side: str, minute: int, momentum_score: float, team_goals_at_signal: int) -> None:
        self._signal_log.append({
            "match_id": match_id, "team_side": team_side, "fired_at_minute": minute,
            "momentum_score": momentum_score, "team_goals_at_signal": team_goals_at_signal,
            "outcome": "pending",
        })

    def update_signal_outcomes(self, match_id: int, current_minute: int, home_goals: int, away_goals: int) -> None:
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
            "total_resolved": total, "hits": hits, "misses": total - hits,
            "hit_rate_pct": round(hits / total * 100, 1) if total else None,
            "pending": sum(1 for e in self._signal_log if e["outcome"] == "pending"),
        }


repo = Repo()
ticket_generator = TicketGenerator()


def _enrich_one_fixture(provider, raw: dict, standings_cache: dict, standings_lock) -> Optional[MatchInput]:
    fixture = data_provider.adapt_api_football_fixture(raw)
    league_id = fixture.get("league_id")
    home_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["home_team_id"], league_id))
    away_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["away_team_id"], league_id))
    odds = data_provider.adapt_api_football_odds(provider.get_pre_match_odds(fixture["id"]))
    data_availability: dict = {"market_odds": bool(odds.get("market_implied_probabilities"))}
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
    try:
        injuries_raw = provider.get_injuries(fixture["id"])
        home_injury_count = data_provider.adapt_injuries(injuries_raw, fixture["home_team"])
        away_injury_count = data_provider.adapt_injuries(injuries_raw, fixture["away_team"])
        data_availability["injuries"] = True
    except Exception:
        home_injury_count, away_injury_count = 0, 0
        data_availability["injuries"] = False
    home_dead_rubber, away_dead_rubber = False, False
    data_availability["standings_motivation"] = False
    if league_id:
        try:
            with standings_lock:
                cached_standings = standings_cache.get(league_id)
            if cached_standings is None:
                cached_standings = provider.get_standings(league_id, fixture.get("season"))
                with standings_lock:
                    standings_cache[league_id] = cached_standings
            home_dead_rubber = data_provider.adapt_standings_for_motivation(cached_standings, fixture["home_team"])
            away_dead_rubber = data_provider.adapt_standings_for_motivation(cached_standings, fixture["away_team"])
            data_availability["standings_motivation"] = bool(cached_standings)
        except Exception:
            pass
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
    standings_cache: dict = {}
    standings_lock = threading.Lock()
    matches: list[MatchInput] = []
    print(f"[tickets/generate] appka souběžně zpracovává {len(raw_fixtures)} zápasů...")
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
                print(f"[tickets/generate] hotovo {len(matches)}/{len(raw_fixtures)}")
            except Exception as exc:
                print(f"[tickets/generate] zápas {idx} selhal: {exc}")
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
        ))
    return matches


def _build_basketball_matches(provider, raw_games: list[dict]) -> list[MatchInput]:
    matches: list[MatchInput] = []
    for raw in raw_games:
        g = data_provider.adapt_apisports_game(raw)
        home = data_provider.adapt_apisports_basketball_team_stats(provider.get_team_statistics(Sport.BASKETBALL, g["home_team_id"]))
        away = data_provider.adapt_apisports_basketball_team_stats(provider.get_team_statistics(Sport.BASKETBALL, g["away_team_id"]))
        total_points = home["points_avg"] + away["points_avg"]
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
            home_win_probability=win_prob, expected_total_games=22.0, expected_total_aces=14.0,
        ))
    return matches


def _enrich_with_market_odds(matches: list[MatchInput], sport: Sport) -> None:
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
        _enrich_with_market_odds(sport_matches, sport)
        matches.extend(sport_matches)
    return matches


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


class SaveTicketRequest(BaseModel):
    ticket_type: str
    selections: list[SelectionResponse]
    total_odds: float
    combined_probability: float
    recommended_stake_pct: float = 0.0


@app.post("/tickets/save")
def save_ticket(req: SaveTicketRequest, user_id: int = Depends(get_current_user_id)):
    domain_selections = [
        SelectionCandidate(
            match_id=s.match_id, home_team=s.home_team, away_team=s.away_team,
            sport=Sport.FOOTBALL, market_type=s.market_type, selection=s.selection,
            probability=s.probability, odds=s.odds, model_probability=s.model_probability,
            market_probability=s.market_probability, reasoning=s.reasoning, data_quality=s.data_quality,
        ) for s in req.selections
    ]
    ticket = Ticket(
        ticket_type=req.ticket_type, selections=domain_selections,
        total_odds=req.total_odds, combined_probability=req.combined_probability,
        recommended_stake_pct=req.recommended_stake_pct,
    )
    ticket_id = repo.save_ticket(user_id, ticket)
    return {"ticket_id": ticket_id, "status": "saved"}


@app.get("/tickets/saved", response_model=list[TicketResponse])
def list_saved_tickets(user_id: int = Depends(get_current_user_id)):
    provider = data_provider.get_provider(Sport.FOOTBALL)
    for row in repo.get_saved_tickets(user_id):
        if row["status"] == "pending":
            new_status = _try_settle_ticket(provider, row["ticket"])
            if new_status is not None:
                repo.set_ticket_status(row["ticket_id"], new_status)
                repo.set_live_alert(row["ticket_id"], None)
    return [
        TicketResponse.from_domain(
            row["ticket"], row["ticket_id"], row["status"], row["live_alert"],
            row["actual_stake_amount"], row["actual_odds"], row["actual_profit_loss"],
        )
        for row in repo.get_saved_tickets(user_id)
    ]


class StakeRequest(BaseModel):
    stake_amount: float
    odds: float


@app.post("/tickets/{ticket_id}/stake")
def set_ticket_stake(ticket_id: int, req: StakeRequest, user_id: int = Depends(get_current_user_id)):
    owner_id = db.get_ticket_owner(ticket_id)
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Tiket nenalezen")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Tenhle tiket není tvůj")
    repo.set_actual_stake(ticket_id, req.stake_amount, req.odds)
    return {"ticket_id": ticket_id, "status": "stake_recorded"}


@app.get("/tickets/real-results")
def get_real_results(user_id: int = Depends(get_current_user_id)):
    return repo.get_real_results_report(user_id)


def _try_settle_ticket(provider, ticket: Ticket) -> Optional[str]:
    leg_results: list[Optional[bool]] = []
    for selection in ticket.selections:
        try:
            raw_result = provider.get_fixture_result(selection.match_id)
            result = data_provider.adapt_fixture_result(raw_result)
        except Exception:
            leg_results.append(None)
            continue
        if not result["is_finished"] or result["home_goals"] is None:
            leg_results.append(None)
            continue
        leg_results.append(evaluate_selection_outcome(selection, result["home_goals"], result["away_goals"]))
    if any(r is False for r in leg_results):
        return "lost"
    if leg_results and all(r is True for r in leg_results):
        return "won"
    return None


@app.post("/tickets/settle")
def settle_tickets():
    provider = data_provider.get_provider(Sport.FOOTBALL)
    settled = 0
    for ticket_id, ticket in repo.get_pending_tickets():
        new_status = _try_settle_ticket(provider, ticket)
        if new_status is not None:
            repo.set_ticket_status(ticket_id, new_status)
            settled += 1
    return {"settled_this_run": settled, "still_pending": len(repo.get_pending_tickets())}


@app.get("/tickets/track-record")
def get_ticket_track_record(user_id: int = Depends(get_current_user_id)):
    return repo.get_ticket_track_record(user_id)


@app.get("/tickets/calibration")
def get_ticket_calibration(user_id: int = Depends(get_current_user_id)):
    return repo.get_calibration_report(user_id)


@app.get("/tickets/roi")
def get_ticket_roi(user_id: int = Depends(get_current_user_id)):
    return repo.get_roi_report(user_id)


class ConnectionManager:
    GLOBAL_CHANNEL = -1

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
        repo.update_signal_outcomes(match_id, minute, snapshot.home_goals, snapshot.away_goals)
        if signal is not None:
            home_team = raw_fixture["teams"]["home"]["name"]
            away_team = raw_fixture["teams"]["away"]["name"]
            if signal.signal_type == SignalType.ENTRY:
                market_verdict = _enrich_with_live_odds_and_check_market(provider, signal)
                if market_verdict is False:
                    continue
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
    if signal.signal_type == SignalType.CASHOUT:
        return True
    if selection.market_type == MarketType.MATCH_WINNER:
        if selection.selection == "home" and signal.team_side == "away":
            return True
        if selection.selection == "away" and signal.team_side == "home":
            return True
        if selection.selection == "draw":
            return True
    return False


def _check_ticket_contradictions(signal: MomentumSignal, minute: int) -> None:
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
                    f"{selection.away_team}.",
                )
                break


@app.get("/live-signals/track-record")
def get_track_record():
    return repo.get_track_record()


_live_odds_baseline: dict[tuple, float] = {}
MARKET_CONFIRM_SHORTEN_PCT = 0.03
MARKET_DISAGREE_LENGTHEN_PCT = 0.03


def _enrich_with_live_odds_and_check_market(provider, signal) -> Optional[bool]:
    try:
        live_odds_raw = provider.get_live_odds(signal.match_id)
        odds = data_provider.adapt_live_odds_for_signal(live_odds_raw, signal.team_side)
        if data_provider.is_live_market_blocked(live_odds_raw):
            signal.reasoning += " Pozn.: bookmaker zrovna live sázení pozastavil."
    except Exception:
        odds = None
    if odds is None:
        return None
    signal.odds = odds
    key = (signal.match_id, signal.team_side)
    baseline = _live_odds_baseline.get(key)
    if baseline is None:
        _live_odds_baseline[key] = odds
        return None
    movement_pct = (baseline - odds) / baseline
    if movement_pct >= MARKET_CONFIRM_SHORTEN_PCT:
        signal.reasoning += f" Živý kurz se zkrátil o {round(movement_pct * 100, 1)} % — trh potvrzuje."
        return True
    if movement_pct <= -MARKET_DISAGREE_LENGTHEN_PCT:
        return False
    return None


@app.post("/live-signals/ingest/{match_id}", response_model=Optional[LiveSignalResponse])
async def ingest_live_tick(match_id: int, tick: LiveStatTickRequest):
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
    await ws_manager.connect(match_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(match_id, websocket)


@app.websocket("/ws/live-signals")
async def live_signals_ws_global(websocket: WebSocket):
    await ws_manager.connect(ws_manager.GLOBAL_CHANNEL, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws_manager.GLOBAL_CHANNEL, websocket)


# =====================================================================
# Tipsport Scraper — testovací endpointy
# =====================================================================
@app.get("/tipsport/debug")
def debug_tipsport():
    """Ukáže přesně co Tipsport API vrátí — pro diagnostiku."""
    import requests
    url = "https://www.tipsport.cz/rest/offer/v2/offer"
    params = {"limit": 5, "offset": 0, "categoryId": 149535}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.tipsport.cz/kurzy/fotbal-16",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        return {
            "status_code": resp.status_code,
            "keys": list(resp.json().keys()) if resp.status_code == 200 else None,
            "raw_sample": str(resp.text)[:500],
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/tipsport/test")
def test_tipsport_scraper():
    """Test Tipsport scraperu — vrátí zápasy MS 2026 přímo z Tipsport API."""
    try:
        import tipsport_scraper
        matches = tipsport_scraper.get_matches_for_competition(149535)
        return {
            "status": "ok",
            "competition": "MS 2026 - Kanada+Mexiko+USA - zápasy",
            "count": len(matches),
            "matches": matches[:5]
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/tipsport/today")
def get_tipsport_today():
    """Vrátí všechny dnešní fotbalové zápasy z Tipsportu."""
    try:
        import tipsport_scraper
        matches = tipsport_scraper.get_todays_football_matches()
        return {
            "status": "ok",
            "count": len(matches),
            "matches": matches
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/health")
def health():
    return {"status": "ok"}
