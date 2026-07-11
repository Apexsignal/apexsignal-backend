"""
AUTO-SETTLE Module - Automatické nastavení výsledků zápasů z API-Football
"""

import logging
import asyncio
import aiohttp
import os
from typing import Optional
from probability_model import Selection, MarketType
import db

logger = logging.getLogger("apexsignal")


async def auto_settle_tickets():
    """
    Background job: Každou hodinu zkontroluj finished zápasy a auto-settle tikety.
    """
    logger.info("🔄 AUTO-SETTLE: Starting ticket settlement check...")
    
    try:
        from repository import TicketRepository
        repo = TicketRepository()
        
        # Najdi všechny pending tikety
        all_users = db.get_all_users()
        
        for user in all_users:
            try:
                pending_tickets = repo.get_saved_tickets(user["user_id"])
                pending_tickets = [t for t in pending_tickets if t["status"] == "pending"]
                
                for ticket_data in pending_tickets:
                    ticket_obj = ticket_data["ticket"]
                    
                    # Projdi všechny selectiony v tiketu
                    all_settled = True
                    should_be_won = True
                    
                    for selection in ticket_obj.selections:
                        if selection.result and selection.result != "pending":
                            # Už má result
                            if selection.result == "lost":
                                should_be_won = False
                            continue
                        
                        # Stáhni výsledek ze API
                        result = await evaluate_selection_from_api(selection)
                        
                        if result is None:
                            # Zápas ještě není skončen
                            all_settled = False
                        elif result == "lost":
                            should_be_won = False
                            db.update_selection_result(selection.id, "lost")
                        elif result == "won":
                            db.update_selection_result(selection.id, "won")
                    
                    # Pokud jsou všechny zápasy settled
                    if all_settled:
                        final_status = "won" if should_be_won else "lost"
                        db.update_ticket_status(ticket_data["ticket_id"], final_status)
                        logger.info(f"✅ Auto-settled ticket {ticket_data['ticket_id']}: {final_status}")
            except Exception as e:
                logger.error(f"❌ Error processing user {user.get('user_id')}: {e}")
        
        logger.info("✅ AUTO-SETTLE: Check complete")
    except Exception as e:
        logger.error(f"❌ AUTO-SETTLE ERROR: {e}")


async def evaluate_selection_from_api(selection: Selection) -> Optional[str]:
    """
    Evaluuj jednu selection proti API výsledkům.
    Returns: "won", "lost", nebo None (zápas ještě není skončen)
    """
    try:
        # Stáhni výsledek ze API
        fixture_data = await get_fixture_result(selection.match_id)
        
        if not fixture_data:
            return None
        
        # Zkontroluj jestli je zápas skončen
        fixture_status = fixture_data.get("fixture", {}).get("status", {}).get("short", "")
        if fixture_status != "FT":  # FT = Full Time
            return None
        
        # Vyhodnoť selection
        score = fixture_data.get("score", {})
        home_score = score.get("fulltime", {}).get("home") or 0
        away_score = score.get("fulltime", {}).get("away") or 0
        
        if selection.market_type == MarketType.MATCH_WINNER:
            if selection.selection == "home":
                return "won" if home_score > away_score else "lost"
            elif selection.selection == "away":
                return "won" if away_score > home_score else "lost"
            else:  # draw
                return "won" if home_score == away_score else "lost"
        
        elif selection.market_type == MarketType.OVER_GOALS:
            total = home_score + away_score
            threshold = float(selection.selection.split("_")[1])
            return "won" if total > threshold else "lost"
        
        elif selection.market_type == MarketType.BTTS:
            return "won" if (home_score > 0 and away_score > 0) else "lost"
        
        return None
    except Exception as e:
        logger.error(f"❌ Evaluate selection error: {e}")
        return None


async def get_fixture_result(match_id: str) -> Optional[dict]:
    """Stáhni výsledek jednoho zápasu z API-Football"""
    try:
        api_key = os.getenv("API_FOOTBALL_KEY", "")
        if not api_key:
            logger.warning("⚠️ API_FOOTBALL_KEY not set")
            return None
        
        headers = {"x-apisports-key": api_key}
        url = f"https://v3.football.api-sports.io/fixtures?id={match_id}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("response"):
                        return data["response"][0]
        return None
    except Exception as e:
        logger.error(f"❌ Get fixture result error: {e}")
        return None
