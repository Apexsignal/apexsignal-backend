"""
Vyrenderuje appka vygenerovaný tiket jako přehledný JPG obrázek ("sázenku")
a pošle ho do Telegramu přes Bot API. Samostatný nástroj pro odesílání
tiketů z chatu do Telegramu — není součástí backend_api.py, appka to
nevolá automaticky.

Použití:
    python3 scripts/telegram_ticket.py ticket.json

kde ticket.json je jeden ticket objekt (např. response["safe"] z
POST /tickets/generate) nebo {"selections": [...], "ticket_type": ...,
"total_odds": ..., "combined_probability": ..., "recommended_stake_pct": ...}.

Bot token a chat_id se čtou z proměnných prostředí TELEGRAM_BOT_TOKEN a
TELEGRAM_CHAT_ID (viz scratchpad/.render_env).
"""
import json
import os
import sys
import textwrap
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")

WIDTH = 900
PADDING = 32
BG = (18, 22, 30)
CARD_BG = (28, 34, 46)
ACCENT = (255, 176, 32)
TEXT = (235, 238, 242)
SUBTEXT = (150, 160, 175)
GREEN = (86, 214, 130)
LINE = (48, 56, 70)

TICKET_LABELS = {
    "kratky": "KRÁTKÝ TIKET", "stredni": "STŘEDNÍ TIKET", "boost": "BOOST TIKET",
    "dlouhy": "BOOST TIKET",  # starší název pro BOOST, pořád v uložených datech
}


def wrap(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_ticket(ticket: dict) -> Image.Image:
    f_title = ImageFont.truetype(FONT_BOLD, 34)
    f_h2 = ImageFont.truetype(FONT_BOLD, 22)
    f_body = ImageFont.truetype(FONT_REGULAR, 20)
    f_small = ImageFont.truetype(FONT_REGULAR, 16)
    f_odds = ImageFont.truetype(FONT_BOLD, 22)

    selections = ticket.get("selections", [])
    row_h = 92
    header_h = 150
    footer_h = 130
    height = header_h + len(selections) * row_h + footer_h

    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    label = TICKET_LABELS.get(ticket.get("ticket_type", ""), "TIKET")
    draw.text((PADDING, PADDING), "ApexSignal", font=f_title, fill=ACCENT)
    draw.text((PADDING, PADDING + 44), label, font=f_h2, fill=TEXT)

    total_odds = ticket.get("total_odds", 0)
    stake_pct = ticket.get("recommended_stake_pct", 0)
    draw.text(
        (WIDTH - PADDING, PADDING + 10), f"kurz {total_odds:.2f}",
        font=f_title, fill=GREEN, anchor="ra",
    )
    draw.text(
        (WIDTH - PADDING, PADDING + 54), f"doporučený vklad {stake_pct:.1f}%",
        font=f_small, fill=SUBTEXT, anchor="ra",
    )

    y = header_h
    draw.line([(PADDING, y - 10), (WIDTH - PADDING, y - 10)], fill=LINE, width=2)

    for i, s in enumerate(selections):
        row_top = y + i * row_h
        card_rect = [PADDING, row_top + 6, WIDTH - PADDING, row_top + row_h - 6]
        draw.rounded_rectangle(card_rect, radius=12, fill=CARD_BG)

        teams = f"{s.get('home_team', '')} – {s.get('away_team', '')}"
        teams_lines = wrap(draw, teams, f_body, WIDTH - 2 * PADDING - 180)
        ty = row_top + 14
        for line in teams_lines[:2]:
            draw.text((PADDING + 20, ty), line, font=f_body, fill=TEXT)
            ty += 26

        league = f"{s.get('league', '')} · {s.get('kickoff_date', '')} {s.get('kickoff_time', '')}".strip()
        draw.text((PADDING + 20, row_top + row_h - 32), league, font=f_small, fill=SUBTEXT)

        sel_odds = s.get("odds", 0)
        draw.text(
            (WIDTH - PADDING - 20, row_top + 14), f"{sel_odds:.2f}",
            font=f_odds, fill=GREEN, anchor="ra",
        )
        prob = s.get("probability", 0) * 100
        selection_txt = s.get("selection", "")
        draw.text(
            (WIDTH - PADDING - 20, row_top + 44), f"{selection_txt} ({prob:.0f}%)",
            font=f_small, fill=SUBTEXT, anchor="ra",
        )

    footer_y = height - footer_h + 20
    draw.line([(PADDING, footer_y - 14), (WIDTH - PADDING, footer_y - 14)], fill=LINE, width=2)
    combined_prob = ticket.get("combined_probability", 0) * 100
    draw.text(
        (PADDING, footer_y), f"Kombinovaná pravděpodobnost: {combined_prob:.1f}%",
        font=f_body, fill=TEXT,
    )
    draw.text(
        (PADDING, footer_y + 32), f"{len(selections)} výběrů · vygenerováno {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        font=f_small, fill=SUBTEXT,
    )

    return img


def send_to_telegram(image_path: str, caption: str = ""):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": f},
            timeout=30,
        )
    resp.raise_for_status()
    return resp.json()


def main():
    if len(sys.argv) != 2:
        print("Použití: python3 telegram_ticket.py ticket.json")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        ticket = json.load(f)

    img = render_ticket(ticket)
    out_path = "/tmp/ticket_render.jpg"
    img.save(out_path, "JPEG", quality=92)

    caption = ticket.get("summary", "")
    result = send_to_telegram(out_path, caption=caption[:1024])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
