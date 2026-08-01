"""
Vyrenderování appčina "proof karty" obrázku — denní/týdenní organický obsah
pro sociální sítě, postavený VÝHRADNĚ z appčiných vlastních dat (stejný
zdroj jako `/public/transparency` a `/transparentni-ucet`), žádné cizí
záběry ani vymyšlená čísla.

Vizuální styl navazuje na appčin web (transparency_page.py — tmavé pozadí,
fialový accent, mono písmo na čísla), ne na oranžové sázenky posílané do
Telegramu (ticket_telegram.py) — tenhle obrázek uvidí cizí lidi na
Instagramu poprvé, má tedy vypadat jako appčin web, ne jako appčin
interní produkt pro už platící odběratele. Fonty a `wrap()` helper appka
bere z ticket_telegram.py, aby appka nemusela znovu řešit, odkud fonty
jsou (bundled DejaVu Sans v assets/fonts/, appka nechce záviset na tom,
co má OS kontejneru nainstalované).
"""
import io
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from ticket_telegram import FONT_REGULAR, FONT_BOLD

WIDTH = 1080
# Appka výšku plátna dopočítává z obsahu (stejný vzor jako ticket_telegram.
# render_ticket), jen ji ořízne do rozsahu, co Instagram u feed postů
# nekrotuje: 1:1 (čtverec) až 4:5 (nejvyšší povolený portrait poměr).
MIN_HEIGHT = WIDTH        # 1080 — poměr 1:1
MAX_HEIGHT = int(WIDTH * 1.25)  # 1350 — poměr 4:5
PADDING = 64

BG = (14, 19, 25)
RAISE = (21, 28, 36)
LINE = (35, 45, 56)
INK = (230, 235, 241)
MUTED = (138, 151, 166)
ACCENT = (139, 124, 246)
UP = (15, 160, 140)
DOWN = (236, 95, 87)


def _fmt_signed(value, decimals=1, suffix=""):
    # Appka nahrazuje desetinnou tečku čárkou JEN v číselné části — appka
    # dřív dělala replace() na celý včetně suffixu, takže " j." vycházelo
    # jako " j,".
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    number = f"{value:.{decimals}f}".replace(".", ",")
    return f"{sign}{number}{suffix}"


def _fmt_num(value, decimals=1, suffix=""):
    if value is None:
        return "—"
    number = f"{value:.{decimals}f}".replace(".", ",")
    return f"{number}{suffix}"


def fetch_public_stats(base_url: str) -> dict:
    """Appka bere STEJNÁ data jako veřejná stránka transparentního účtu —
    žádné vlastní přepočty, ať se čísla na webu a v postu nikdy nerozejdou."""
    resp = requests.get(f"{base_url.rstrip('/')}/public/transparency", timeout=20)
    resp.raise_for_status()
    return resp.json()


def _draw_eyebrow(draw, xy, text, font, fill):
    # PIL neumí letter-spacing nativně — appka to simuluje mezerami mezi
    # znaky, aby uppercase eyebrow label vypadal jako appčin web (tam to
    # dělá CSS letter-spacing:.14em).
    spaced = " ".join(list(text))
    draw.text(xy, spaced, font=font, fill=fill)


def render_proof_card(data: dict) -> Image.Image:
    stats = data.get("stats") or {}
    equity_curve = data.get("equity_curve") or []

    f_eyebrow = ImageFont.truetype(FONT_REGULAR, 22)
    f_headline = ImageFont.truetype(FONT_BOLD, 56)
    f_hero_label = ImageFont.truetype(FONT_REGULAR, 24)
    f_hero_val = ImageFont.truetype(FONT_BOLD, 108)
    f_hero_sub = ImageFont.truetype(FONT_REGULAR, 26)
    f_cell_label = ImageFont.truetype(FONT_REGULAR, 22)
    f_cell_val = ImageFont.truetype(FONT_BOLD, 46)
    f_cell_sub = ImageFont.truetype(FONT_REGULAR, 22)
    f_footer = ImageFont.truetype(FONT_REGULAR, 24)
    f_wordmark = ImageFont.truetype(FONT_BOLD, 30)

    resolved = stats.get("resolved_count") or 0
    if resolved == 0:
        headline = "Účet právě začal běžet.\nČísla přibydou po prvních\nvyhodnocených tiketech."
    else:
        headline = "Bez emocí.\nJen čísla, co sedí."
    headline_lines = headline.split("\n")
    has_spark = len(equity_curve) >= 2

    # Appka nejdřív spočítá, jak vysoký bude celý blok obsahu, a podle toho
    # zvolí výšku plátna mezi MIN_HEIGHT/MAX_HEIGHT (necentruje na pevnou
    # výšku — bez equity křivky by jinak zbyla velká prázdná plocha dole).
    hero_h = 220
    spark_h = 140
    cell_h = 150
    content_h = (
        56  # eyebrow
        + len(headline_lines) * 66 + 24
        + hero_h + 28
        + (spark_h + 28 if has_spark else 0)
        + cell_h + 40
        + 2 + 24 + 40  # footer: dělící čára + mezera + řádek textu
    )
    canvas_h = max(MIN_HEIGHT, min(MAX_HEIGHT, content_h + 2 * PADDING))
    y = PADDING + max(0, (canvas_h - 2 * PADDING - content_h) // 2)

    img = Image.new("RGB", (WIDTH, canvas_h), BG)
    draw = ImageDraw.Draw(img)

    _draw_eyebrow(draw, (PADDING, y), "APEXSIGNAL · TRANSPARENTNÍ ÚČET", f_eyebrow, ACCENT)
    y += 56

    for line in headline_lines:
        draw.text((PADDING, y), line, font=f_headline, fill=INK)
        y += 66
    y += 24

    # hero stat — ROI
    hero_rect = [PADDING, y, WIDTH - PADDING, y + hero_h]
    draw.rounded_rectangle(hero_rect, radius=14, fill=RAISE)
    draw.rectangle([PADDING, y, PADDING + 6, y + hero_h], fill=ACCENT)
    inner_x = PADDING + 40
    draw.text((inner_x, y + 26), "ROI NA VSAZENOU JEDNOTKU", font=f_hero_label, fill=MUTED)
    roi = stats.get("roi_pct")
    roi_color = UP if (roi or 0) >= 0 else DOWN
    draw.text((inner_x, y + 60), _fmt_signed(roi, 1, " %"), font=f_hero_val, fill=roi_color)
    draw.text((inner_x, y + 176), "veřejně dohledatelné na apexsignal.cz", font=f_hero_sub, fill=MUTED)
    y += hero_h + 28

    # equity curve sparkline (pokud appka má aspoň 2 body)
    if has_spark:
        spark_rect = [PADDING, y, WIDTH - PADDING, y + spark_h]
        draw.rounded_rectangle(spark_rect, radius=14, fill=RAISE)
        pad_in = 24
        values = [pt["units"] for pt in equity_curve]
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        plot_w = (WIDTH - 2 * PADDING) - 2 * pad_in
        plot_h = spark_h - 2 * pad_in
        points = []
        for i, v in enumerate(values):
            px = PADDING + pad_in + (i / (len(values) - 1)) * plot_w
            py = y + pad_in + plot_h - ((v - lo) / span) * plot_h
            points.append((px, py))
        draw.line(points, fill=UP, width=4, joint="curve")
        for pt in (points[0], points[-1]):
            draw.ellipse([pt[0] - 5, pt[1] - 5, pt[0] + 5, pt[1] + 5], fill=UP)
        y += spark_h + 28

    # readout grid — úspěšnost / zisk
    gap = 20
    cell_w = (WIDTH - 2 * PADDING - gap) / 2
    win_rate = stats.get("win_rate_pct")
    won = stats.get("won_count") or 0
    profit = stats.get("units_profit")

    cells = [
        ("ÚSPĚŠNOST", _fmt_num(win_rate, 1, " %"), f"{won} z {resolved} tiketů", INK),
        ("ZISK", _fmt_signed(profit, 1, " j."), "jednotek celkem", UP if (profit or 0) >= 0 else DOWN),
    ]
    for i, (label, val, sub, val_color) in enumerate(cells):
        cx = PADDING + i * (cell_w + gap)
        rect = [cx, y, cx + cell_w, y + cell_h]
        draw.rounded_rectangle(rect, radius=14, fill=RAISE, outline=LINE, width=1)
        draw.text((cx + 24, y + 22), label, font=f_cell_label, fill=MUTED)
        draw.text((cx + 24, y + 56), val, font=f_cell_val, fill=val_color)
        draw.text((cx + 24, y + 110), sub, font=f_cell_sub, fill=MUTED)
    y += cell_h + 40

    # footer
    draw.line([(PADDING, y), (WIDTH - PADDING, y)], fill=LINE, width=2)
    y += 24
    draw.text((PADDING, y), "ApexSignal", font=f_wordmark, fill=INK)
    draw.text(
        (WIDTH - PADDING, y + 4),
        f"vygenerováno {datetime.now().strftime('%d.%m.%Y')}",
        font=f_footer, fill=MUTED, anchor="ra",
    )

    return img


def render_proof_card_jpeg_bytes(data: dict, quality: int = 92) -> bytes:
    img = render_proof_card(data)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
