"""
transparency_page.py — veřejná HTML podoba transparentního účtu.

Appka tuhle stránku servíruje na /transparentni-ucet a bere pro ni
naprosto stejná data jako JSON endpoint /public/transparency — tedy i
stejné maskování: u nevyhodnocených tiketů appka ukazuje zápasy hned,
ale samotnou sázku (trh, výběr, kurz té nohy) až po vyhodnocení.

Výsledky appka vykazuje v JEDNOTKÁCH (1 tiket = 1 jednotka vkladu), ne
v korunách. Uložené částky u automaticky generovaných tiketů jsou
náhodně přiřazené (viz DAILY_TICKETS_STAKE_CHOICES v backend_api.py), ne
skutečně vsazené peníze — publikovat je jako reálný vklad by bylo
tvrzení, které neodpovídá skutečnosti. Úspěšnost a ROI tím netrpí:
obojí je vlastností VÝBĚRŮ, ne velikosti sázky, a v jednotkách je to
navíc přesně ta podoba, ve které si výkonnost přepočítává sázkařská
komunita sama.

Stránka je serverem vykreslené HTML, ne aplikace v JavaScriptu: crawler,
náhled odkazu na Facebooku i sdílení na fóru musí vidět skutečná čísla,
ne prázdnou stránku. Ze stejného důvodu je všechno inline — jeden
soubor bez jediného dalšího požadavku.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from html import escape
from typing import Optional

from probability_model import market_label, selection_label

SITE_URL = "https://apexsignal.cz/transparentni-ucet"

TICKET_TYPE_LABELS = {
    "kratky": "Krátký",
    "stredni": "Střední",
    "dlouhy": "Dlouhý",
    "boost": "BOOST",
}

STATUS_LABELS = {
    "won": "Výhra",
    "lost": "Prohra",
    "void": "Zrušeno",
    "cashed_out": "Vybráno",
    "pending": "Čeká na výsledek",
    "live": "Probíhá",
}

MONTHS_CS = [
    "ledna", "února", "března", "dubna", "května", "června",
    "července", "srpna", "září", "října", "listopadu", "prosince",
]

# V databázi jsou trhy i výběry uložené jako kódy ('over_goals',
# 'over_1.5'). Na veřejné stránce nemají co dělat — čte ji člověk, ne API.
# Popisky appka bere z probability_model (market_label/selection_label),
# ať appka nemá dvě nezávislé kopie, co se snadno rozejdou (viz i
# ticket_telegram.py, který ze stejného zdroje čte pro sázenku).
_market_label = market_label
_selection_label = selection_label


def _parse_dt(raw) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _fmt_date(raw) -> str:
    dt = _parse_dt(raw)
    return f"{dt.day}. {MONTHS_CS[dt.month - 1]} {dt.year}" if dt else ""


def _fmt_short_date(raw) -> str:
    dt = _parse_dt(raw)
    return f"{dt.day}. {dt.month}." if dt else ""


def _fmt_num(value: Optional[float], decimals: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{decimals}f}".replace(".", ",") + suffix


def _fmt_signed(value: Optional[float], decimals: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + _fmt_num(value, decimals, suffix)


def _trend_class(value: Optional[float]) -> str:
    if value is None or value == 0:
        return ""
    return "up" if value > 0 else "down"


# ---------------------------------------------------------------------
# Graf kumulativního zisku
#
# Jedna řada, takže žádná legenda — nadpis říká, co se kreslí. Osa x je
# pořadí tiketu (ne datum): u výnosové křivky je podstatné, po kolika
# sázkách se kam došlo, ne že mezi dvěma z nich byly tři dny pauza.
# ---------------------------------------------------------------------
CHART_W, CHART_H = 720, 250
PAD_L, PAD_R, PAD_T, PAD_B = 46, 16, 18, 28


def _nice_step(span: float) -> float:
    """Krok mřížky na 1/2/5 × mocnina deseti, ať popisky vycházejí kulatě."""
    if span <= 0:
        return 1.0
    raw = span / 4
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 0.1
    for mult in (1, 2, 5, 10):
        if raw <= magnitude * mult:
            return magnitude * mult
    return magnitude * 10


def _chart_bounds(curve: list[dict]) -> tuple[float, float, float]:
    """
    Rozsah svislé osy a krok mřížky. Spodní hranice se drží TĚSNĚ u
    nejnižší hodnoty (ne zaokrouhlená dolů na násobek kroku) — když
    křivka klesne jen k −1, zaokrouhlení by udělalo osu od −20 a čtvrtina
    grafu by byla prázdná. Popisky se pak kreslí jen na násobcích kroku,
    které do rozsahu spadnou.

    Appka tenhle výpočet drží na JEDNOM místě, protože ho potřebuje SVG
    i skript pro najetí myší — kdyby se rozešly, ukazovátko by sedělo
    jinde než křivka.
    """
    values = [p["units"] for p in curve]
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    if hi == lo:
        hi = lo + 1
    step = _nice_step(hi - lo)
    hi = math.ceil(hi / step) * step
    pad = (hi - lo) * 0.04
    return lo - pad, hi, step


def _chart_svg(curve: list[dict]) -> str:
    if len(curve) < 2:
        return (
            '<p class="chart-empty">Graf se objeví, jakmile budou vyhodnocené aspoň dva tikety.</p>'
        )

    lo, hi, step = _chart_bounds(curve)
    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B

    def sx(i: int) -> float:
        return PAD_L + (plot_w * i / (len(curve) - 1))

    def sy(v: float) -> float:
        return PAD_T + plot_h * (1 - (v - lo) / (hi - lo))

    pts = [(sx(i), sy(p["units"])) for i, p in enumerate(curve)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{PAD_L:.1f},{sy(0):.1f} " + line + f" {pts[-1][0]:.1f},{sy(0):.1f}"

    # Mřížka a popisky osy y — jen na násobcích kroku uvnitř rozsahu.
    grid = []
    v = math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        y = sy(v)
        cls = "grid-zero" if abs(v) < 1e-9 else "grid-line"
        grid.append(f'<line class="{cls}" x1="{PAD_L}" y1="{y:.1f}" x2="{CHART_W - PAD_R}" y2="{y:.1f}"/>')
        grid.append(
            f'<text class="axis-label" x="{PAD_L - 8}" y="{y + 3.5:.1f}" text-anchor="end">{_fmt_num(v, 0)}</text>'
        )
        v += step

    last_x, last_y = pts[-1]
    final = curve[-1]["units"]
    label_anchor = "end" if last_x > CHART_W - 90 else "start"
    label_dx = -10 if label_anchor == "end" else 10

    return f"""<figure class="chart">
  <svg viewBox="0 0 {CHART_W} {CHART_H}" role="img"
       aria-label="Kumulativní zisk v jednotkách po {len(curve)} vyhodnocených tiketech, konečná hodnota {_fmt_signed(final, 1)} jednotky."
       preserveAspectRatio="none">
    <defs>
      <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.20"/>
        <stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/>
      </linearGradient>
    </defs>
    {''.join(grid)}
    <polygon class="area" points="{area}"/>
    <polyline class="line" points="{line}"/>
    <line class="crosshair" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T + plot_h}" style="opacity:0"/>
    <circle class="hover-dot" r="4.5" style="opacity:0"/>
    <circle class="endpoint" cx="{last_x:.1f}" cy="{last_y:.1f}" r="4.5"/>
    <text class="endpoint-label" x="{last_x + label_dx:.1f}" y="{last_y - 10:.1f}" text-anchor="{label_anchor}">{_fmt_signed(final, 1)} j.</text>
    <text class="axis-label" x="{PAD_L}" y="{CHART_H - 8}">1. tiket</text>
    <text class="axis-label" x="{CHART_W - PAD_R}" y="{CHART_H - 8}" text-anchor="end">{len(curve)}. tiket</text>
    <rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{plot_w}" height="{plot_h}" fill="transparent"/>
  </svg>
  <div class="tooltip" hidden></div>
  <figcaption>Kumulativní zisk při vkladu jedné jednotky na každý tiket. Kreslí se jen vyhodnocené tikety, v pořadí, v jakém padaly.</figcaption>
</figure>"""


RESULT_ICONS = {"won": "✓", "lost": "✗", "pending": "•"}

# Appka u prohraného tiketu chce ukázat, že to bylo třeba jen o JEDNU
# nohu, ne že model netrefil skoro nic — proto appka per-výběr výsledek
# (won/lost/pending), co appka stejně už ukládá do DB, teď zobrazuje i
# tady, ne jen jako souhrnný status celého tiketu.
RESULT_LABELS = {
    "won": "Tenhle výběr vyšel",
    "lost": "Tenhle výběr nevyšel",
    "pending": "Appka tenhle výběr nedokázala vyhodnotit ze skóre",
}


def _selection_row(sel: dict) -> str:
    teams = f"{escape(str(sel.get('home_team') or ''))} — {escape(str(sel.get('away_team') or ''))}"
    league = f'<span class="sel-league">{escape(str(sel.get("league") or ""))}</span>'

    # U nevyhodnocených tiketů appka posílá market_type/selection jako
    # None schválně (viz public_transparency v backend_api.py) — zápas
    # appka ukazuje hned, sázku samotnou (trh, výběr, kurz té nohy) až
    # po vyhodnocení, ať si ji nikdo nemůže dřív okopírovat.
    masked = sel.get("market_type") is None and sel.get("selection") is None
    if masked:
        result_span = (
            '<span class="sel-result pending" role="img" aria-label="Sázka zatím skrytá" '
            'title="Sázka zatím skrytá">•</span>'
        )
        pick_html = '<span class="sel-market">Sázka</span><span class="sel-choice">skrytá</span>'
        odds_html = "—"
    else:
        result = sel.get("result") or "pending"
        icon = RESULT_ICONS.get(result, "")
        label = escape(RESULT_LABELS.get(result, ""))
        result_span = f'<span class="sel-result {result}" role="img" aria-label="{label}" title="{label}">{icon}</span>'
        pick_html = (
            f'<span class="sel-market">{escape(_market_label(sel.get("market_type")))}</span>'
            f'<span class="sel-choice">{escape(_selection_label(sel.get("selection")))}</span>'
        )
        odds_html = _fmt_num(sel.get("odds"))

    return (
        '<li class="sel">'
        f"{result_span}"
        f'<div class="sel-match"><span class="sel-teams">{teams}</span>{league}</div>'
        f'<div class="sel-pick">{pick_html}</div>'
        f'<div class="sel-odds">{odds_html}</div>'
        "</li>"
    )


def _ticket_card(ticket: dict) -> str:
    status = ticket.get("status") or "pending"
    resolved = status in ("won", "lost")
    type_label = TICKET_TYPE_LABELS.get(ticket.get("ticket_type"), ticket.get("ticket_type") or "—")
    units = ticket.get("profit_units")

    if ticket.get("selections"):
        body = '<ul class="sels">' + "".join(_selection_row(s) for s in ticket["selections"]) + "</ul>"
    else:
        body = '<p class="lede" style="padding:16px">Appka k tomuhle tiketu nemá uložené zápasy.</p>'

    units_cell = ""
    if resolved and units is not None:
        units_cell = (
            f'<div class="t-cell"><span class="t-label">Výsledek</span>'
            f'<span class="t-val {_trend_class(units)}">{_fmt_signed(units)} j.</span></div>'
        )

    return (
        f'<article class="ticket {status}">'
        '<header class="t-head">'
        f'<div class="t-id"><span class="t-type">{escape(type_label)}</span>'
        f'<span class="t-date">{escape(_fmt_date(ticket.get("created_at")))}</span></div>'
        f'<span class="t-status {status}">{escape(STATUS_LABELS.get(status, status))}</span>'
        "</header>"
        f"{body}"
        '<footer class="t-foot">'
        f'<div class="t-cell"><span class="t-label">Kurz</span><span class="t-val">{_fmt_num(ticket.get("total_odds"))}</span></div>'
        f'<div class="t-cell"><span class="t-label">Vklad</span><span class="t-val">1 j.</span></div>'
        f"{units_cell}"
        "</footer>"
        "</article>"
    )


def _ticket_groups(tickets: list[dict]) -> str:
    """
    Místo jednoho dlouhého seznamu appka tikety rozdělí do tří
    rozklikávacích skupin (výhry/prohry/čekající) — počet appka napíše
    rovnou do záhlaví skupiny, takže nic neschovává (viz "Dost bylo
    tipérů, co mažou prohry" v hero textu výš), jen appka nenutí
    návštěvníka scrollovat přes desítky karet naráz, kde prohry vizuálně
    "křičí" i když je součet v plusu.
    """
    won = [t for t in tickets if t.get("status") == "won"]
    lost = [t for t in tickets if t.get("status") == "lost"]
    other = [t for t in tickets if t.get("status") not in ("won", "lost")]

    groups = [("won", "Výhry", won), ("lost", "Prohry", lost), ("pending", "Čekající", other)]
    blocks = [
        '<details class="ticket-group">'
        f'<summary><span class="tg-dot {status_class}"></span>{escape(label)}'
        f'<span class="tg-count">{len(group)}</span></summary>'
        f'<div class="tickets">{"".join(_ticket_card(t) for t in group)}</div>'
        "</details>"
        for status_class, label, group in groups
        if group
    ]
    return "".join(blocks) or (
        '<p class="lede">Zatím tu není žádný tiket. Jakmile model vybere první, objeví se tady.</p>'
    )


def _stats_block(stats: Optional[dict]) -> str:
    if not stats or not stats.get("resolved_count"):
        return (
            '<div class="empty-stats">'
            "<strong>Zatím žádný vyhodnocený tiket.</strong>"
            "<span>Účet právě začal běžet. Čísla se tu objeví, jakmile skončí první zápasy — "
            "a zůstanou tu i když vyjdou špatně.</span>"
            "</div>"
        )
    roi = stats.get("roi_pct")
    profit = stats.get("units_profit")
    return (
        '<div class="readout">'
        f'<div class="cell"><span class="cell-label">ROI</span>'
        f'<span class="cell-val {_trend_class(roi)}">{_fmt_signed(roi, 1, " %")}</span>'
        f'<span class="cell-sub">na vsazenou jednotku</span></div>'
        f'<div class="cell"><span class="cell-label">Úspěšnost</span>'
        f'<span class="cell-val">{_fmt_num(stats.get("win_rate_pct"), 1, " %")}</span>'
        f'<span class="cell-sub">{stats.get("won_count", 0)} z {stats.get("resolved_count", 0)} tiketů</span></div>'
        f'<div class="cell"><span class="cell-label">Zisk</span>'
        f'<span class="cell-val {_trend_class(profit)}">{_fmt_signed(profit, 1)}</span>'
        f'<span class="cell-sub">jednotek celkem</span></div>'
        f'<div class="cell"><span class="cell-label">Vyhodnoceno</span>'
        f'<span class="cell-val">{stats.get("resolved_count", 0)}</span>'
        f'<span class="cell-sub">tiketů</span></div>'
        "</div>"
    )


STYLE = """
:root{--paper:#F4F6F8;--raise:#FFF;--ink:#111820;--muted:#5C6875;--line:#DCE2E9;
--line-soft:#E9EDF2;--accent:#4338CA;--accent-glow:rgb(67 56 202 / 14%);--up:#00897B;--down:#C0392B}
@media (prefers-color-scheme:dark){:root{--paper:#0E1319;--raise:#151C24;--ink:#E6EBF1;--muted:#8A97A6;
--line:#232D38;--line-soft:#1A222B;--accent:#8B7CF6;--accent-glow:rgb(139 124 246 / 18%);--up:#0FA08C;--down:#EC5F57}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-size:16px;line-height:1.6;
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:840px;margin:0 auto;padding:48px 20px 96px;display:flex;flex-direction:column;gap:44px}
h1,h2{margin:0;letter-spacing:-.02em;text-wrap:balance}
h1{font-size:clamp(1.8rem,4.5vw,2.5rem);line-height:1.1}
h2{font-size:1.25rem}
p{margin:0;max-width:66ch}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:2px}
.mono,.cell-val,.cell-label,.cell-sub,.t-type,.t-date,.t-status,.t-label,.t-val,.sel-odds,.eyebrow,
.axis-label,.endpoint-label,.tooltip,footer{font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
.eyebrow{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0}
section{display:flex;flex-direction:column;gap:18px}
.lede{color:var(--muted);font-size:1.05rem}
.history-recap{font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace;
font-size:.95rem;color:var(--muted);margin:-6px 0 4px;padding:10px 14px;background:var(--raise);
border:1px solid var(--line);border-radius:10px;width:fit-content}
.history-recap strong{color:var(--ink)}
.up{color:var(--up)}.down{color:var(--down)}
header{position:relative;padding-top:8px}
header::before{content:"";position:absolute;top:-40px;left:-20px;right:-20px;height:280px;z-index:-1;
pointer-events:none;background:radial-gradient(60% 100% at 15% 0%,var(--accent-glow),transparent 70%)}
.hero-stat{margin-top:22px;padding:20px 22px;background:var(--raise);border:1px solid var(--line);
border-radius:6px;display:flex;flex-direction:column;gap:2px;position:relative;overflow:hidden}
.hero-stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent)}
.hero-label{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
.hero-val{font-size:clamp(2rem,7vw,2.8rem);font-weight:700;letter-spacing:-.03em;line-height:1.05;
font-variant-numeric:tabular-nums;font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
.hero-sub{font-size:.82rem;color:var(--muted)}
.offers{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.offer{background:var(--raise);border:1px solid var(--accent);box-shadow:0 0 0 1px var(--accent);border-radius:6px;padding:22px;
display:flex;flex-direction:column;gap:12px}
.offer-price{font-size:1.6rem;font-weight:700;letter-spacing:-.02em;
font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
.offer-price small{font-size:.78rem;font-weight:400;color:var(--muted);letter-spacing:0}
.offer p{font-size:.9rem;color:var(--muted);flex:1}
.btn{display:block;text-align:center;padding:12px 18px;border-radius:5px;text-decoration:none;
font-size:.94rem;font-weight:600;border:1px solid var(--accent);color:var(--accent);background:transparent}
.btn:hover{background:var(--accent-glow)}
.btn-fill{background:var(--accent);color:#fff;border-color:var(--accent)}
@media (prefers-color-scheme:dark){.btn-fill{color:#0E1319}}
.badge-soon{display:inline-block;font-size:.95rem;font-weight:700;letter-spacing:.02em;
color:var(--muted);background:var(--line-soft);padding:4px 10px;border-radius:4px}
.offer-steps{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:7px;
font-size:.82rem;color:var(--muted);counter-reset:step}
.offer-steps li{counter-increment:step;padding-left:24px;position:relative}
.offer-steps li::before{content:counter(step);position:absolute;left:0;top:0;width:16px;height:16px;
border-radius:50%;background:var(--accent-glow);color:var(--accent);font-size:.66rem;font-weight:700;
display:flex;align-items:center;justify-content:center;line-height:1}
.resend{background:var(--raise);border:1px solid var(--line);border-radius:6px;padding:22px}
.resend-form{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}
.resend-form input{flex:1;min-width:220px;padding:11px 14px;border-radius:5px;border:1px solid var(--line);
background:var(--paper);color:var(--ink);font-size:.94rem;font-family:inherit}
.resend-form input:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.resend-form button{flex-shrink:0;cursor:pointer}
.resend-form button:disabled{opacity:.6;cursor:default}
.resend-status{font-size:.86rem;margin-top:10px}
.resend-status.ok{color:var(--up)}
.resend-status.err{color:var(--down)}
.resend-alt{margin-top:16px;padding-top:16px;border-top:1px solid var(--line-soft);
font-size:.88rem;color:var(--muted)}
.resend-tg{font-weight:600}
.readout{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:4px;overflow:hidden}
.cell{background:var(--raise);padding:15px 16px;display:flex;flex-direction:column;gap:4px}
.cell-label{font-size:.63rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.cell-val{font-size:1.45rem;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.1}
.cell-sub{font-size:.72rem;color:var(--muted)}
.chart{margin:0;background:var(--raise);border:1px solid var(--line);border-radius:4px;padding:16px 16px 10px;position:relative}
.chart svg{width:100%;height:250px;display:block;overflow:visible}
.grid-line{stroke:var(--line-soft);stroke-width:1}
.grid-zero{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.55}
.area{fill:url(#areaFill);stroke:none}
.line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.endpoint,.hover-dot{fill:var(--accent);stroke:var(--raise);stroke-width:2}
.endpoint-label{fill:var(--ink);font-size:12px;font-weight:700}
.axis-label{fill:var(--muted);font-size:11px}
.crosshair{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3}
.hit{cursor:crosshair}
.tooltip{position:absolute;pointer-events:none;background:var(--ink);color:var(--paper);font-size:.74rem;
padding:6px 9px;border-radius:3px;white-space:nowrap;transform:translate(-50%,-140%);line-height:1.5}
.chart figcaption{font-size:.78rem;color:var(--muted);margin-top:10px;font-family:inherit}
.chart-empty{color:var(--muted);font-size:.9rem}
.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.rule{background:var(--raise);border:1px solid var(--line);border-radius:4px;padding:16px 18px;display:flex;flex-direction:column;gap:6px}
.rule strong{font-size:.95rem}.rule span{font-size:.88rem;color:var(--muted)}
.how{display:grid;grid-template-columns:1fr;gap:10px}
.how-step{background:var(--raise);border:1px solid var(--line);border-radius:4px;padding:18px;
display:flex;flex-direction:column;gap:6px;position:relative;min-width:0}
.how-icon{width:30px;height:30px;color:var(--accent)}
.how-icon svg{width:100%;height:100%}
.how-num{position:absolute;top:16px;right:16px;width:20px;height:20px;border-radius:50%;
background:var(--accent-glow);color:var(--accent);font-size:.68rem;font-weight:700;
display:flex;align-items:center;justify-content:center;
font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace}
.how-step strong{font-size:.95rem;margin-top:2px}.how-step span:last-child{font-size:.85rem;color:var(--muted)}
.how-arrow-wrap{display:none}
@media (min-width:760px){
  .how{grid-template-columns:1fr auto 1fr auto 1fr auto 1fr;align-items:stretch}
  .how-arrow-wrap{display:flex;align-items:center;justify-content:center;width:18px;color:var(--muted)}
  .how-arrow{width:16px;height:16px}
}
.empty-stats{background:var(--raise);border:1px solid var(--line);border-radius:4px;padding:20px;display:flex;flex-direction:column;gap:6px}
.empty-stats span{color:var(--muted);font-size:.9rem}
.tickets{display:flex;flex-direction:column;gap:12px}
.ticket-groups{display:flex;flex-direction:column;gap:12px}
.ticket-group{background:var(--raise);border:1px solid var(--line);border-radius:4px;overflow:hidden}
.ticket-group summary{cursor:pointer;padding:15px 16px;font-weight:700;font-size:.95rem;
list-style:none;display:flex;align-items:center;gap:10px;user-select:none}
.ticket-group summary::-webkit-details-marker{display:none}
.ticket-group summary::after{content:"▾";margin-left:auto;color:var(--muted);font-size:.75rem;font-weight:400}
.ticket-group[open] summary::after{content:"▴"}
.ticket-group[open] summary{border-bottom:1px solid var(--line-soft)}
.ticket-group .tickets{padding:14px 16px}
.tg-dot{width:9px;height:9px;border-radius:50%;background:var(--muted);flex-shrink:0}
.tg-dot.won{background:var(--up)}
.tg-dot.lost{background:var(--down)}
.tg-count{color:var(--muted);font-weight:400;font-size:.85rem}
.ticket{background:var(--raise);border:1px solid var(--line);border-radius:4px;overflow:hidden}
.t-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:13px 16px;
border-bottom:1px solid var(--line-soft);flex-wrap:wrap}
.t-id{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.t-type{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;font-weight:700}
.t-date{font-size:.74rem;color:var(--muted)}
.t-status{font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;padding:3px 9px;border-radius:2px;
border:1px solid var(--line);color:var(--muted)}
.t-status.won{color:var(--up);border-color:var(--up)}
.t-status.lost{color:var(--down);border-color:var(--down)}
.sels{list-style:none;margin:0;padding:0}
.sel{display:grid;grid-template-columns:auto 1fr auto auto;gap:6px 14px;padding:12px 16px;
border-bottom:1px solid var(--line-soft);align-items:center}
.sel:last-child{border-bottom:none}
.sel-result{width:18px;text-align:center;font-size:.95rem;font-weight:700;line-height:1}
.sel-result.won{color:var(--up)}
.sel-result.lost{color:var(--down)}
.sel-result.pending{color:var(--muted)}
.sel-match{display:flex;flex-direction:column;gap:2px;min-width:0}
.sel-teams{font-size:.92rem}.sel-league{font-size:.76rem;color:var(--muted)}
.sel-pick{display:flex;flex-direction:column;gap:2px;text-align:right}
.sel-market{font-size:.72rem;color:var(--muted)}.sel-choice{font-size:.88rem}
.sel-odds{font-size:.95rem;font-variant-numeric:tabular-nums;min-width:52px;text-align:right}
.t-foot{display:flex;flex-direction:row;gap:24px;padding:12px 16px;
border-top:1px solid var(--line-soft);flex-wrap:wrap}
.t-cell{display:flex;flex-direction:column;gap:2px}
.t-label{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.t-val{font-size:.95rem;font-variant-numeric:tabular-nums}
/* Jen patička STRÁNKY. Holý selektor footer{} by sahal i do
   <footer class="t-foot"> uvnitř každého tiketu a přebil mu směr flexu. */
.wrap>footer{border-top:1px solid var(--line);padding-top:20px;font-size:.75rem;color:var(--muted);
line-height:1.8;display:flex;flex-direction:column;gap:10px}
@media (max-width:560px){.sel{grid-template-columns:auto 1fr auto}.sel-pick{text-align:left}
.wrap{gap:36px;padding-top:32px}.chart svg{height:200px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

SCRIPT = """
(function(){
  var fig=document.querySelector('.chart'); if(!fig) return;
  var svg=fig.querySelector('svg'), hit=fig.querySelector('.hit');
  if(!svg||!hit) return;
  var data=JSON.parse(document.getElementById('curve-data').textContent);
  var tip=fig.querySelector('.tooltip'), cross=fig.querySelector('.crosshair'), dot=fig.querySelector('.hover-dot');
  var padL=%PAD_L%, padT=%PAD_T%, plotW=%PLOT_W%, plotH=%PLOT_H%, lo=%LO%, hi=%HI%, n=data.length;
  function show(e){
    var r=svg.getBoundingClientRect();
    var vx=(e.clientX-r.left)/r.width*%CHART_W%;
    var i=Math.round((vx-padL)/plotW*(n-1));
    if(i<0)i=0; if(i>n-1)i=n-1;
    var p=data[i];
    var x=padL+plotW*i/(n-1), y=padT+plotH*(1-(p.units-lo)/(hi-lo));
    cross.setAttribute('x1',x); cross.setAttribute('x2',x); cross.style.opacity=1;
    dot.setAttribute('cx',x); dot.setAttribute('cy',y); dot.style.opacity=1;
    tip.hidden=false;
    tip.textContent=(i+1)+'. tiket · '+(p.units>0?'+':'')+p.units.toFixed(2).replace('.',',')+' j.';
    tip.style.left=(x/%CHART_W%*r.width)+'px';
    tip.style.top=(y/%CHART_H%*r.height+16)+'px';
  }
  function hide(){ cross.style.opacity=0; dot.style.opacity=0; tip.hidden=true; }
  hit.addEventListener('mousemove',show);
  hit.addEventListener('mouseleave',hide);
  hit.addEventListener('touchmove',function(e){ if(e.touches[0]) show(e.touches[0]); },{passive:true});
  hit.addEventListener('touchend',hide);
})();
"""


RESEND_SCRIPT = """
(function(){
  var form=document.getElementById('resend-form'); if(!form) return;
  var status=document.getElementById('resend-status');
  var input=document.getElementById('resend-email');
  var btn=form.querySelector('button[type=submit]');
  var cancelBtn=document.getElementById('cancel-sub-btn');
  if(cancelBtn){
    cancelBtn.addEventListener('click', function(){
      var email=(input.value||'').trim();
      if(!email){ input.reportValidity(); return; }
      cancelBtn.disabled=true;
      status.hidden=false;
      status.className='resend-status';
      status.textContent='Otevírám správu předplatného…';
      fetch('%BACKEND_URL%/payments/billing-portal',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email:email})
      }).then(function(r){
        return r.json().catch(function(){return {};}).then(function(body){
          if(!r.ok) throw new Error(body.detail||'Nepodařilo se otevřít správu předplatného.');
          return body;
        });
      }).then(function(body){
        window.location.href=body.portal_url;
      }).catch(function(err){
        status.className='resend-status err';
        status.textContent=err.message||'Nepodařilo se otevřít správu předplatného.';
        cancelBtn.disabled=false;
      });
    });
  }
  form.addEventListener('submit', function(e){
    e.preventDefault();
    var email=(input.value||'').trim();
    if(!email) return;
    btn.disabled=true;
    status.hidden=false;
    status.className='resend-status';
    status.textContent='Odesílám…';
    fetch('%BACKEND_URL%/telegram/resend-link',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:email})
    }).then(function(r){
      return r.json().catch(function(){return {};}).then(function(body){
        if(!r.ok) throw new Error(body.detail||'Nepodařilo se odeslat, zkus to prosím znovu.');
        return body;
      });
    }).then(function(){
      status.className='resend-status ok';
      status.textContent='Hotovo — koukni do e-mailu, odkaz dorazí během chvíle.';
      form.reset();
    }).catch(function(err){
      status.className='resend-status err';
      status.textContent=err.message||'Nepodařilo se odeslat, zkus to prosím znovu.';
    }).finally(function(){
      btn.disabled=false;
    });
  });
})();
"""


def _script_for(curve: list[dict]) -> str:
    """Do JS appka propašuje jen rozměry a rozsah os — samotná data jdou
    zvlášť v <script type="application/json">, ať se nemusí escapovat."""
    if len(curve) < 2:
        return ""
    lo, hi, _step = _chart_bounds(curve)
    repl = {
        "%PAD_L%": str(PAD_L), "%PAD_T%": str(PAD_T),
        "%PLOT_W%": str(CHART_W - PAD_L - PAD_R), "%PLOT_H%": str(CHART_H - PAD_T - PAD_B),
        "%LO%": f"{lo:.4f}", "%HI%": f"{hi:.4f}",
        "%CHART_W%": str(CHART_W), "%CHART_H%": str(CHART_H),
    }
    out = SCRIPT
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


# Inline SVG appka drží ve stejném stylu (viewBox 24×24, jen obrys), jako
# appka sama používá jinde ve svém UI — appka nechce tahat žádnou externí
# ikonovou knihovnu jen kvůli čtyřem obrázkům, tahle stránka musí zůstat
# jeden soubor bez dalšího požadavku (viz docstring modulu).
_ICON_ATTRS = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
HOW_STEPS = [
    (
        "Sbíráme data",
        "Kurzy, statistiky týmů, forma, historie vzájemných zápasů — z nich model odvodí očekávaný "
        "počet gólů (xG) pro každý tým.",
        f'<svg {_ICON_ATTRS}><ellipse cx="12" cy="5.5" rx="8" ry="3"/>'
        '<path d="M4 5.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/>'
        '<path d="M4 11.5v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"/></svg>',
    ),
    (
        "Model spočítá pravděpodobnost",
        "Z xG obou týmů, přes Poissonovo rozdělení (matematický model počtu gólů), spočítá "
        "pravděpodobnost každého výsledku — výhry, přesného skóre, karet, i jestli skórují oba týmy.",
        f'<svg {_ICON_ATTRS}><path d="M3 17h3.5l2.5-10 4 14 2.5-9 2 5H21"/></svg>',
    ),
    (
        "Porovnáme s kurzem",
        "Kurz sázkové kanceláře přepočítáme na pravděpodobnost, očištěnou o její marži, a srovnáme "
        "ji s tím, co spočítal model.",
        f'<svg {_ICON_ATTRS}><path d="M12 3v18"/><path d="M5 7h14"/>'
        '<path d="M5 7l-3.2 6.2a3.2 3.2 0 0 0 6.4 0z"/>'
        '<path d="M19 7l-3.2 6.2a3.2 3.2 0 0 0 6.4 0z"/></svg>',
    ),
    (
        "Vybereme jen výhodu",
        "Rozdílu se říká edge. Tip appka pošle, jen když model dané možnosti věří víc než trh — "
        "jinak radši žádný tip, než tip bez výhody.",
        f'<svg {_ICON_ATTRS}><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.8"/>'
        '<circle cx="12" cy="12" r="1" fill="currentColor" stroke="none"/></svg>',
    ),
]

HOW_ARROW = (
    '<svg class="how-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 12h15"/><path d="M13 6l6 6-6 6"/></svg>'
)


def _how_it_works() -> str:
    cards = []
    for i, (title, body, icon) in enumerate(HOW_STEPS):
        if i:
            cards.append(f'<div class="how-arrow-wrap" aria-hidden="true">{HOW_ARROW}</div>')
        cards.append(
            '<div class="how-step">'
            f'<span class="how-icon">{icon}</span>'
            f'<span class="how-num">{i + 1}</span>'
            f'<strong>{escape(title)}</strong>'
            f'<span>{escape(body)}</span>'
            "</div>"
        )
    return "".join(cards)


def render_page(
    tickets: list[dict],
    stats: Optional[dict],
    equity_curve: Optional[list[dict]] = None,
    app_equivalent_kc: Optional[int] = None,
    app_generation_enabled: bool = True,
) -> str:
    curve = equity_curve or []
    resolved_count = (stats or {}).get("resolved_count") or 0
    roi = (stats or {}).get("roi_pct")

    if resolved_count:
        description = (
            f"Veřejný účet ApexSignalu: {resolved_count} vyhodnocených tiketů, "
            f"ROI {_fmt_signed(roi, 1, ' %')} na vsazenou jednotku. Všechny tikety včetně prohraných."
        )
    else:
        description = "Veřejný účet ApexSignalu — všechny fotbalové tikety včetně prohraných, bez mazání."

    cards = _ticket_groups(tickets)

    # Datum appka bere z PRVNÍHO bodu křivky (curve je chronologicky
    # seřazená), ne z aktuálního data — jinak by "od" ukazovalo pořád
    # dnešek místo skutečného začátku měřeného období. Sdíleno mezi
    # history_recap a hero_stat, ať appka nepočítá dvakrát to samé.
    since = _fmt_date(curve[0]["date"]) if curve else ""
    since_note = f" od {since}" if since else ""

    win_rate = (stats or {}).get("win_rate_pct")
    history_recap = ""
    if resolved_count:
        history_recap = (
            '<p class="history-recap">'
            f'ROI <strong class="{_trend_class(roi)}">{_fmt_signed(roi, 1, " %")}</strong> · '
            f'Úspěšnost <strong>{_fmt_num(win_rate, 1, " %")}</strong> na {resolved_count} vyhodnocených tiketech{since_note}'
            "</p>"
        )

    units_profit = (stats or {}).get("units_profit")
    hero_stat = ""
    if resolved_count and units_profit is not None:
        hero_stat = (
            '<div class="hero-stat">'
            '<span class="hero-label">Zisk k dnešnímu dni</span>'
            f'<span class="hero-val {_trend_class(units_profit)}">{_fmt_signed(units_profit, 1)} jednotek</span>'
            f'<span class="hero-sub">při vkladu 1 jednotky na každý z {resolved_count} vyhodnocených tiketů{since_note}</span>'
            "</div>"
        )

    payment_link = os.environ.get("STRIPE_CHANNEL_PAYMENT_LINK_URL", "").strip()
    app_url = os.environ.get("APP_URL", "https://apexsignal.cz").strip()
    # Stránka běží na Netlify (apexsignal.cz), appka samotná na Renderu —
    # relativní URL by tu mířila na Netlify, kde tahle cesta neexistuje,
    # proto formulář dole musí volat backend na jeho vlastní doméně.
    backend_url = os.environ.get("BACKEND_PUBLIC_URL", "https://apexsignal-backend.onrender.com").strip()

    compare_line = ""
    if app_equivalent_kc:
        formatted_kc = f"{app_equivalent_kc:,.0f}".replace(",", " ")
        compare_line = (
            f"Přes appku a tokeny by tenhle měsíční balík vyšel na {formatted_kc} Kč. "
            "V kanálu ho máš za zlomek ceny."
        )

    if app_generation_enabled:
        app_offer_price = 'Tokeny <small>200–300 Kč / tiket</small>'
        app_offer_body = (
            "Generuješ si sám — vybereš jen krátký, nebo střední tiket, appka sama najde "
            "ligy a zápasy. Platíš jen za to, co si necháš vygenerovat."
        )
    else:
        # Registrace a přihlášení appka nechává OTEVŘENÉ i bez peněz na
        # fotbalová data — zamčené je jen samotné generování tiketů (viz
        # _require_generation_enabled v backend_api.py). Tenhle CTA proto
        # nesmí být zamčené tlačítko, i když appka "Aplikace" nabídku
        # zobrazuje jako ještě nedokončenou — jinak appka lidem brání
        # udělat přesně tu jednu věc (vstoupit, zaregistrovat se), kterou
        # appka chce, aby mohli.
        app_offer_price = '<span class="badge-soon">Generování připravujeme</span>'
        app_offer_body = (
            "Registrace a přihlášení fungují už teď. Appka jen ještě testuje a doostřuje "
            "samotné generování tiketů, ať ho nespustí dřív, než na to bude spolehlivé — "
            "uvnitř appky to uvidíš stejně napsané."
        )
    app_offer_cta = f'<a class="btn" href="{escape(app_url)}">Otevřít appku</a>'

    offers = f"""<section>
  <h2>Tři cesty, jeden model</h2>
  <p class="lede">Liší se jen tím, kdo dělá výběr a kolik toho chceš — appka, ty sám, nebo appka bez omezení.</p>
  <div class="offers">
    <div class="offer primary">
      <span class="eyebrow">Kanál na Telegramu</span>
      <div class="offer-price">990 Kč <small>/ měsíc</small></div>
      <p>Každé ráno appka pošle 1 tiket — hlavně krátký, občas (když appka najde
      opravdovou hodnotu) radši ten hodnotnější, střední. Nic si sám negeneruješ,
      appka pošle výběr rovnou na Telegram. {compare_line}</p>
      <ol class="offer-steps">
        <li>Zaplatíš kartou přes Stripe — bez zakládání účtu.</li>
        <li>Hned ti přijde e-mail s odkazem na propojení Telegramu.</li>
        <li>Klikneš na odkaz a od dalšího rána ti chodí tikety.</li>
      </ol>
      <a class="btn btn-fill" href="{escape(payment_link) if payment_link else '#'}">Aktivovat kanál</a>
    </div>
    <div class="offer">
      <span class="eyebrow">Aplikace — appka za pokus</span>
      <div class="offer-price">{app_offer_price}</div>
      <p>{app_offer_body}</p>
      {app_offer_cta}
    </div>
    <div class="offer">
      <span class="eyebrow">Neomezené generování</span>
      <div class="offer-price">4 990 Kč <small>/ měsíc</small></div>
      <p>Generuješ si sám, kolikrát chceš (strop 10× denně proti zneužití),
      bez placení za jednotlivý tiket. Cílené na lidi, co appku chtějí
      používat naplno — vlastní analýzu zápasů, nebo i vlastní tipsterský
      byznys (založíš si skupinu a tikety, co appka vygeneruje, prodáváš dál sám).</p>
      {app_offer_cta}
    </div>
  </div>
</section>"""

    curve_json = json.dumps(
        [{"units": p["units"]} for p in curve], ensure_ascii=False, separators=(",", ":")
    )

    return f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transparentní účet — ApexSignal</title>
<meta name="description" content="{escape(description)}">
<meta property="og:type" content="website">
<meta property="og:title" content="Transparentní účet — ApexSignal">
<meta property="og:description" content="{escape(description)}">
<meta property="og:url" content="{SITE_URL}">
<meta name="twitter:card" content="summary">
<meta name="robots" content="index, follow">
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">

<header>
  <p class="eyebrow">ApexSignal — veřejný účet</p>
  <h1>Dost bylo tipérů, co mažou prohry.</h1>
  <p class="lede">Tady je jen matika. Model vybírá zápasy, appka ukazuje úplně všechno — výhry
  i prohry, natrvalo, beze změny. Žádná falešná jistota, žádné emoce. Výsledky jsou v jednotkách:
  každý tiket počítám jako jeden stejně velký vklad, ať jsou čísla porovnatelná a nezávisí na tom,
  kolik kdo sází.</p>
  {hero_stat}
</header>

<section>
  <h2>Jak model vybírá tipy</h2>
  <p class="lede">Čtyři kroky, žádná magie.</p>
  <div class="how">{_how_it_works()}</div>
</section>

<section>
  <h2>Jak si model vede</h2>
  {_stats_block(stats)}
  {_chart_svg(curve)}
</section>

<section class="rules">
  <div class="rule">
    <strong>Jde sem každý tiket</strong>
    <span>Výhry i prohry, automaticky. Když má model špatný týden, uvidíš to tady dřív než kdekoli jinde.</span>
  </div>
  <div class="rule">
    <strong>Sázka je skrytá do konce zápasu</strong>
    <span>Zápasy vidíš hned. Skrytá zůstává jen samotná sázka — trh, výběr, kurz té nohy — dokud appka tiket nevyhodnotí, pak ji odhalí natrvalo.</span>
  </div>
  <div class="rule">
    <strong>Appka se pořád vyvíjí</strong>
    <span>Model i appka samotná měly cestou chyby a technické problémy — appka je průběžně opravuje. Historii appka zpětně neupravuje ani nemaže — vidíš ji takovou, jaká skutečně byla.</span>
  </div>
</section>

{offers}

<section class="resend">
  <h2>Ztratil jsi odkaz, nebo máš dotaz?</h2>
  <p class="lede">Zaplatil jsi kanál, ale párovací odkaz na Telegram vypršel nebo se ztratil? Napiš e-mail,
  kterým jsi platil, a pošleme nový.</p>
  <form class="resend-form" id="resend-form">
    <input type="email" id="resend-email" name="email" placeholder="e-mail, kterým jsi platil" required autocomplete="email">
    <button type="submit" class="btn btn-fill">Poslat nový odkaz</button>
    <button type="button" class="btn" id="cancel-sub-btn">Spravovat / zrušit předplatné</button>
  </form>
  <p class="resend-status" id="resend-status" role="status" hidden></p>
  <p class="resend-alt">Nebo máš jiný dotaz? <a class="resend-tg" href="https://t.me/D1990V05" target="_blank" rel="noopener noreferrer">Napiš mi rovnou na Telegram →</a></p>
</section>

<section>
  <h2>Historie tiketů</h2>
  <p class="lede">Zápasy appka ukazuje hned u každého tiketu. Skrytá zůstává jen samotná sázka — trh, konkrétní
  výběr a kurz té jedné nohy — dokud appka tiket nevyhodnotí. Jde tak dopředu ověřit, na co appka sází, ale
  nejde si to okopírovat, ani appka nemůže zpětně tvrdit něco jiného, než na co skutečně vsadila.</p>
  {history_recap}
  <div class="ticket-groups">{cards}</div>
</section>

<footer>
  <span>ApexSignal je nástroj na rozhodování, ne sázková kancelář. Tikety si sázíš sám, kde chceš.
  Model se plete a plést se bude — žádný tip tu není jistota.</span>
  <span><strong>18+. Hazardní hraní může být návykové. Sázej jen to, co si můžeš dovolit prohrát.</strong>
  Pomoc při problémech s hraním: <a href="https://www.hazardni-hrani.cz">hazardni-hrani.cz</a>,
  Národní linka pro odvykání 800 350 000.</span>
</footer>

</div>
<script id="curve-data" type="application/json">{curve_json}</script>
<script>{_script_for(curve)}</script>
<script>{RESEND_SCRIPT.replace("%BACKEND_URL%", backend_url)}</script>
</body>
</html>"""
