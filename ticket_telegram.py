"""
Sdílená logika pro vyrenderování tiketu jako JPG "sázenky" a odeslání do
Telegramu — používá jak `scripts/telegram_ticket.py` (ruční odeslání z
příkazové řádky), tak `backend_api.py` (automatické denní odesílání).

Fonty appka bere z `assets/fonts/` (nakopírované do repozitáře), aby appka
nezávisela na tom, jaké fonty má nainstalované OS kontejneru na Renderu —
DejaVu Sans obvykle na Linuxu je, ale appka na to nechce sázet.
"""
import os
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_FONT_DIR = os.path.join(_THIS_DIR, "assets", "fonts")
_SYSTEM_FONT_DIR = "/usr/share/fonts/truetype/dejavu"

FONT_DIR = _BUNDLED_FONT_DIR if os.path.isdir(_BUNDLED_FONT_DIR) else _SYSTEM_FONT_DIR
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
    header_h = 120
    footer_h = 90
    height = header_h + len(selections) * row_h + footer_h

    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    label = TICKET_LABELS.get(ticket.get("ticket_type", ""), "TIKET")
    draw.text((PADDING, PADDING), "ApexSignal", font=f_title, fill=ACCENT)
    draw.text((PADDING, PADDING + 44), label, font=f_h2, fill=TEXT)

    total_odds = ticket.get("total_odds", 0)
    draw.text(
        (WIDTH - PADDING, PADDING + 10), f"kurz {total_odds:.2f}",
        font=f_title, fill=GREEN, anchor="ra",
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
    draw.text(
        (PADDING, footer_y), f"{len(selections)} výběrů · vygenerováno {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        font=f_body, fill=TEXT,
    )

    watermark_text = f"ApexSignal · #{ticket.get('ticket_id')}" if ticket.get("ticket_id") else "ApexSignal"
    img = add_watermark(img, watermark_text)

    return img


def add_watermark(base_img: Image.Image, text: str) -> Image.Image:
    """Jemný opakující se diagonální vodoznak — nezabrání sdílení, ale
    kdo obrázek přeposílá dál, je z něj dohledatelný."""
    tile = Image.new("RGBA", (320, 160), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(tile)
    tfont = ImageFont.truetype(FONT_REGULAR, 16)
    tdraw.text((10, 70), text, font=tfont, fill=(255, 255, 255, 34))
    tile = tile.rotate(24, expand=True)

    overlay = Image.new("RGBA", base_img.size, (0, 0, 0, 0))
    for y in range(0, base_img.height, tile.height):
        for x in range(0, base_img.width, tile.width):
            overlay.alpha_composite(tile, (x, y))

    return Image.alpha_composite(base_img.convert("RGBA"), overlay).convert("RGB")


def send_ticket_to_telegram(ticket: dict, bot_token: str = None, chat_id: str = None) -> dict:
    """Vyrenderuje tiket a rovnou ho pošle do Telegramu. Token/chat_id appka
    vezme z argumentů, jinak z proměnných prostředí TELEGRAM_BOT_TOKEN /
    TELEGRAM_CHAT_ID."""
    token = bot_token or os.environ["TELEGRAM_BOT_TOKEN"]
    chat = chat_id or os.environ["TELEGRAM_CHAT_ID"]

    img = render_ticket(ticket)
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    buf.seek(0)

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    resp = requests.post(
        url,
        data={"chat_id": chat},
        files={"photo": ("ticket.jpg", buf, "image/jpeg")},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()
