"""
ApexSignal — Backend API
Modul: backend_api.py

REST + WebSocket vrstva spojující:
    - probability_model.TicketGenerator  (Generátor tiketů)
    - momentum_filter.MomentumFilter      (Live Signal Engine)
    - data_provider                       (zdroj dat ze sportovního API)

Spuštění (dev):
    pip install fastapi uvicorn
    uvicorn backend_api:app --reload

Pozn.: Repository vrstva (`Repo`) je zde implementována jako in-memory
náhrada za reálnou DB. V produkci nahraď voláními na schéma
v `apexsignal_schema.sql` (např. přes SQLAlchemy / asyncpg).
"""

from __future__ import annotations

from typing import Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from probability_model import (
    TicketGenerator, MatchInput, Sport, MarketType, Ticket, SelectionCandidate,
)
from momentum_filter import MomentumFilter, MatchSnapshot, MomentumSignal
import data_provider


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


# =====================================================================
# Pydantic schémata (request/response kontrakty)
# =====================================================================
class TicketGenerateRequest(BaseModel):
    user_id: int
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

    @classmethod
    def from_domain(cls, c: SelectionCandidate) -> "SelectionResponse":
        return cls(
            match_id=c.match_id, home_team=c.home_team, away_team=c.away_team,
            market_type=c.market_type, selection=c.selection,
            probability=round(c.probability, 4), odds=c.odds,
        )


class TicketResponse(BaseModel):
    ticket_id: Optional[int]
    ticket_type: str
    total_odds: float
    combined_probability: float
    selections: list[SelectionResponse]

    @classmethod
    def from_domain(cls, ticket: Ticket, ticket_id: Optional[int] = None) -> "TicketResponse":
        return cls(
            ticket_id=ticket_id,
            ticket_type=ticket.ticket_type,
            total_odds=ticket.total_odds,
            combined_probability=ticket.combined_probability,
            selections=[SelectionResponse.from_domain(s) for s in ticket.selections],
        )


class TicketPairResponse(BaseModel):
    safe: Optional[TicketResponse]
    aggressive: Optional[TicketResponse]


class LiveSignalResponse(BaseModel):
    match_id: int
    market: str
    odds: float
    reasoning: str
    recommended_stake_pct: float
    signal_type: str
    is_real_pressure: bool
    sent_at: datetime

    @classmethod
    def from_domain(cls, s: MomentumSignal) -> "LiveSignalResponse":
        return cls(
            match_id=s.match_id, market=s.market, odds=s.odds,
            reasoning=s.reasoning, recommended_stake_pct=s.recommended_stake_pct,
            signal_type=s.signal_type.value, is_real_pressure=s.is_real_pressure,
            sent_at=datetime.utcnow(),
        )


class LiveStatTickRequest(BaseModel):
    """Vstupní data pro jednu minutu zápasu — typicky volá interní poller,
    který stahuje live data z `data_provider.get_live_match_stats()`."""
    minute: int
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


# =====================================================================
# In-memory repository (placeholder za DB dle apexsignal_schema.sql)
# =====================================================================
class Repo:
    def __init__(self):
        self._tickets: dict[int, dict] = {}
        self._next_ticket_id = 1
        self._momentum_filters: dict[int, MomentumFilter] = {}
        self._last_batch_match_ids: dict[int, list[int]] = {}  # user_id -> match_ids z posledního generování

    def save_ticket(self, user_id: int, ticket: Ticket) -> int:
        ticket_id = self._next_ticket_id
        self._next_ticket_id += 1
        self._tickets[ticket_id] = {"user_id": user_id, "ticket": ticket, "is_saved": True}
        return ticket_id

    def get_saved_tickets(self, user_id: int) -> list[tuple[int, Ticket]]:
        return [
            (tid, row["ticket"]) for tid, row in self._tickets.items()
            if row["user_id"] == user_id and row["is_saved"]
        ]

    def get_momentum_filter(self, match_id: int) -> MomentumFilter:
        if match_id not in self._momentum_filters:
            self._momentum_filters[match_id] = MomentumFilter(match_id=match_id)
        return self._momentum_filters[match_id]

    def set_last_batch(self, user_id: int, match_ids: list[int]) -> None:
        self._last_batch_match_ids[user_id] = match_ids

    def get_last_batch(self, user_id: int) -> list[int]:
        return self._last_batch_match_ids.get(user_id, [])


repo = Repo()
ticket_generator = TicketGenerator()


# =====================================================================
# Pomocné funkce — stahují zápasy pro každý sport a skládají MatchInput
# =====================================================================
def _build_football_matches(provider, raw_fixtures: list[dict]) -> list[MatchInput]:
    matches: list[MatchInput] = []
    for raw in raw_fixtures:
        fixture = data_provider.adapt_api_football_fixture(raw)
        home_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["home_team_id"]))
        away_stats = data_provider.adapt_api_football_team_stats(provider.get_team_statistics(Sport.FOOTBALL, fixture["away_team_id"]))
        odds = data_provider.adapt_api_football_odds(provider.get_pre_match_odds(fixture["id"]))
        matches.append(data_provider.normalize_to_match_input(Sport.FOOTBALL, fixture, home_stats, away_stats, odds))
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
            continue  # chybí klíč nebo provider pro tenhle sport — tiše vynecháme z poolu

        raw_items = provider.get_upcoming_matches(sport, time_frame_days)
        sport_matches = builders[sport](provider, raw_items)
        _enrich_with_market_odds(sport_matches, sport)
        matches.extend(sport_matches)
    return matches


# =====================================================================
# REST endpointy — Generátor tiketů
# =====================================================================
@app.post("/tickets/generate", response_model=TicketPairResponse)
def generate_tickets(req: TicketGenerateRequest):
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    result = ticket_generator.generate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days
    )
    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(req.user_id, used_ids)

    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=TicketResponse.from_domain(result["aggressive"]) if result["aggressive"] else None,
    )


@app.post("/tickets/regenerate", response_model=TicketPairResponse)
def regenerate_tickets(req: TicketGenerateRequest):
    matches = _fetch_candidate_matches(req.sports, req.time_frame_days)
    previous_ids = repo.get_last_batch(req.user_id)
    result = ticket_generator.regenerate(
        matches, req.risk_level, req.sports, req.market_types, req.time_frame_days, previous_ids
    )
    used_ids = [s.match_id for t in result.values() if t for s in t.selections]
    repo.set_last_batch(req.user_id, used_ids)

    return TicketPairResponse(
        safe=TicketResponse.from_domain(result["safe"]) if result["safe"] else None,
        aggressive=TicketResponse.from_domain(result["aggressive"]) if result["aggressive"] else None,
    )


class SaveTicketRequest(BaseModel):
    user_id: int
    ticket_type: str
    selections: list[SelectionResponse]
    total_odds: float
    combined_probability: float


@app.post("/tickets/save")
def save_ticket(req: SaveTicketRequest):
    domain_selections = [
        SelectionCandidate(
            match_id=s.match_id, home_team=s.home_team, away_team=s.away_team,
            sport=Sport.FOOTBALL,  # zjednodušeno — v reálu si sport ponese SelectionResponse
            market_type=s.market_type, selection=s.selection,
            probability=s.probability, odds=s.odds,
        ) for s in req.selections
    ]
    ticket = Ticket(
        ticket_type=req.ticket_type, selections=domain_selections,
        total_odds=req.total_odds, combined_probability=req.combined_probability,
    )
    ticket_id = repo.save_ticket(req.user_id, ticket)
    return {"ticket_id": ticket_id, "status": "saved"}


@app.get("/tickets/saved", response_model=list[TicketResponse])
def list_saved_tickets(user_id: int):
    return [TicketResponse.from_domain(t, tid) for tid, t in repo.get_saved_tickets(user_id)]


# =====================================================================
# Live Signal Engine — ingest + WebSocket distribuce
# =====================================================================
class ConnectionManager:
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
        )

        mf = repo.get_momentum_filter(match_id)
        signal = mf.ingest(snapshot)
        if signal is not None:
            response = LiveSignalResponse.from_domain(signal)
            await ws_manager.broadcast(match_id, response.model_dump(mode="json"))
            signals_sent += 1

    return {"checked_fixtures": len(live_fixtures), "signals_sent": signals_sent}


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
    )
    signal = mf.ingest(snapshot)
    if signal is None:
        return None

    response = LiveSignalResponse.from_domain(signal)
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


@app.get("/health")
def health():
    return {"status": "ok"}
