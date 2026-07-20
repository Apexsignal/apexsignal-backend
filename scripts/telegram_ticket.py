"""
Ruční odeslání jednoho uloženého tiketu do Telegramu z příkazové řádky —
appka vyrenderuje JPG "sázenku" a pošle ji přes Bot API. Skutečná
render/send logika je ve sdíleném modulu `ticket_telegram.py` (ten samý
appka používá i pro automatické denní odesílání z backend_api.py).

Použití:
    python3 scripts/telegram_ticket.py ticket.json

kde ticket.json je jeden ticket objekt (např. response["safe"] z
POST /tickets/generate, nebo jeden záznam z GET /tickets/saved).

Bot token a chat_id appka čte z proměnných prostředí TELEGRAM_BOT_TOKEN a
TELEGRAM_CHAT_ID.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ticket_telegram import send_ticket_to_telegram


def main():
    if len(sys.argv) != 2:
        print("Použití: python3 telegram_ticket.py ticket.json")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        ticket = json.load(f)

    result = send_ticket_to_telegram(ticket)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
