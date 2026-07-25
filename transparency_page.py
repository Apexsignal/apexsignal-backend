"""
transparency_page.py — veřejná HTML podoba transparentního účtu.

Appka tuhle stránku servíruje na /transparentni-ucet a bere pro ni
naprosto stejná data jako JSON endpoint /public/transparency — tedy i
stejné maskování: u nevyhodnocených tiketů se konkrétní zápasy
NEUKAZUJÍ, jen počet tipů, kurz a vklad.

Stránka je schválně serverem vykreslené HTML, ne aplikace v JavaScriptu:
crawler, náhled odkazu na Facebooku i sdílení na fóru musí vidět skutečná
čísla, ne prázdnou stránku. Ze stejného důvodu je celé CSS inline —
stránka je jeden soubor bez jediného dalšího požadavku.
"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Optional

SITE_URL = "https://apexsignal.cz/transparentni-ucet"

TICKET_TYPE_LABELS = {
    "kratky": "Krátký",
    "stredni": "Střední",
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


def _fmt_date(raw) -> str:
    """ISO řetězec (nebo datetime) na český zápis. Když se to nepovede,
    appka radši vrátí prázdno než rozbité datum."""
    if not raw:
        return ""
    dt = raw
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if not isinstance(dt, datetime):
        return ""
    return f"{dt.day}. {MONTHS_CS[dt.month - 1]} {dt.year}"


def _fmt_num(value: Optional[float], decimals: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")
    if decimals == 0:
        formatted = formatted.split(",")[0]
    return f"{formatted}{suffix}"


def _fmt_signed(value: Optional[float], suffix: str = "") -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{_fmt_num(value, 0 if float(value).is_integer() else 2, suffix)}"


def _selection_row(sel: dict) -> str:
    teams = f"{escape(str(sel.get('home_team') or ''))} — {escape(str(sel.get('away_team') or ''))}"
    league = escape(str(sel.get("league") or ""))
    pick = escape(str(sel.get("selection") or ""))
    market = escape(str(sel.get("market_type") or ""))
    odds = _fmt_num(sel.get("odds"), 2)
    return (
        '<li class="sel">'
        f'<div class="sel-match"><span class="sel-teams">{teams}</span>'
        f'<span class="sel-league">{league}</span></div>'
        f'<div class="sel-pick"><span class="sel-market">{market}</span>'
        f'<span class="sel-choice">{pick}</span></div>'
        f'<div class="sel-odds">{odds}</div>'
        "</li>"
    )


def _ticket_card(ticket: dict) -> str:
    status = ticket.get("status") or "pending"
    resolved = status in ("won", "lost")
    type_label = TICKET_TYPE_LABELS.get(ticket.get("ticket_type"), ticket.get("ticket_type") or "—")
    status_label = STATUS_LABELS.get(status, status)
    date = _fmt_date(ticket.get("created_at"))
    odds = _fmt_num(ticket.get("total_odds"), 2)
    stake = _fmt_num(ticket.get("stake"), 0, " Kč") if ticket.get("stake") is not None else "—"
    count = ticket.get("selection_count") or 0

    profit_html = ""
    if resolved and ticket.get("profit") is not None:
        profit = float(ticket["profit"])
        cls = "up" if profit > 0 else ("down" if profit < 0 else "")
        profit_html = f'<div class="t-cell"><span class="t-label">Zisk</span><span class="t-val {cls}">{_fmt_signed(profit, " Kč")}</span></div>'

    if resolved and ticket.get("selections"):
        body = '<ul class="sels">' + "".join(_selection_row(s) for s in ticket["selections"]) + "</ul>"
    else:
        body = (
            '<div class="masked">'
            f"<strong>{count}</strong> "
            f'{"tip" if count == 1 else "tipy" if count < 5 else "tipů"} — skryto do vyhodnocení'
            "<span>Zápasy odhalím, až tiket skončí. Do té doby si ho nikdo nemůže okopírovat "
            "a já si nemůžu zpětně vymyslet, co jsem tvrdil.</span>"
            "</div>"
        )

    return (
        f'<article class="ticket {status}">'
        '<header class="t-head">'
        f'<div class="t-id"><span class="t-type">{escape(type_label)}</span>'
        f'<span class="t-date">{escape(date)}</span></div>'
        f'<span class="t-status {status}">{escape(status_label)}</span>'
        "</header>"
        f"{body}"
        '<footer class="t-foot">'
        f'<div class="t-cell"><span class="t-label">Kurz</span><span class="t-val">{odds}</span></div>'
        f'<div class="t-cell"><span class="t-label">Vklad</span><span class="t-val">{stake}</span></div>'
        f"{profit_html}"
        "</footer>"
        "</article>"
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
    roi_cls = "up" if (roi or 0) > 0 else ("down" if (roi or 0) < 0 else "")
    profit = stats.get("total_profit")
    profit_cls = "up" if (profit or 0) > 0 else ("down" if (profit or 0) < 0 else "")

    return (
        '<div class="readout">'
        f'<div class="cell"><span class="cell-label">ROI</span>'
        f'<span class="cell-val {roi_cls}">{_fmt_signed(roi, " %")}</span></div>'
        f'<div class="cell"><span class="cell-label">Úspěšnost</span>'
        f'<span class="cell-val">{_fmt_num(stats.get("win_rate_pct"), 1, " %")}</span></div>'
        f'<div class="cell"><span class="cell-label">Zisk / ztráta</span>'
        f'<span class="cell-val {profit_cls}">{_fmt_signed(profit, " Kč")}</span></div>'
        f'<div class="cell"><span class="cell-label">Vyhodnoceno</span>'
        f'<span class="cell-val">{stats.get("resolved_count", 0)}</span>'
        f'<span class="cell-sub">z toho {stats.get("won_count", 0)} výher</span></div>'
        f'<div class="cell"><span class="cell-label">Vsazeno celkem</span>'
        f'<span class="cell-val">{_fmt_num(stats.get("total_staked"), 0, " Kč")}</span></div>'
        "</div>"
    )


def render_page(tickets: list[dict], stats: Optional[dict]) -> str:
    """Celá stránka jako jeden řetězec — bez šablonovacího enginu, appka
    kvůli jedné stránce nepotřebuje další závislost."""
    resolved_count = (stats or {}).get("resolved_count") or 0
    roi = (stats or {}).get("roi_pct")

    if resolved_count:
        description = (
            f"Veřejný účet ApexSignalu: {resolved_count} vyhodnocených tiketů, "
            f"ROI {_fmt_signed(roi, ' %')}. Všechny tikety včetně prohraných."
        )
    else:
        description = "Veřejný účet ApexSignalu — všechny fotbalové tikety včetně prohraných, bez mazání."

    cards = "".join(_ticket_card(t) for t in tickets) or (
        '<p class="empty">Zatím tu není žádný tiket. Jakmile model vybere první, objeví se tady.</p>'
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
<style>
:root {{
  --paper:#F4F6F8; --raise:#FFFFFF; --ink:#111820; --muted:#5C6875;
  --line:#DCE2E9; --line-soft:#E9EDF2; --signal:#2340C8;
  --up:#0F7A5A; --down:#A8323C;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --paper:#0E1319; --raise:#151C24; --ink:#E6EBF1; --muted:#8A97A6;
    --line:#232D38; --line-soft:#1A222B; --signal:#7E9AFF;
    --up:#3FBF95; --down:#FF7A85;
  }}
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
  font-size:16px; line-height:1.6; -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:840px; margin:0 auto; padding:48px 20px 96px; display:flex; flex-direction:column; gap:44px; }}
h1,h2 {{ margin:0; letter-spacing:-0.02em; text-wrap:balance; }}
h1 {{ font-size:clamp(1.8rem,4.5vw,2.5rem); line-height:1.1; }}
h2 {{ font-size:1.2rem; }}
p {{ margin:0; max-width:66ch; }}
a {{ color:var(--signal); text-underline-offset:2px; }}
a:focus-visible {{ outline:2px solid var(--signal); outline-offset:3px; border-radius:2px; }}
.mono, .cell-val, .cell-label, .cell-sub, .t-type, .t-date, .t-status, .t-label, .t-val,
.sel-odds, .eyebrow, footer {{
  font-family:ui-monospace,"SF Mono",SFMono-Regular,"Cascadia Mono",Menlo,Consolas,monospace;
}}
.eyebrow {{ font-size:0.7rem; letter-spacing:0.14em; text-transform:uppercase; color:var(--muted); margin:0; }}
header.top {{ display:flex; flex-direction:column; gap:14px; }}
.lede {{ color:var(--muted); font-size:1.05rem; }}

.rules {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }}
.rule {{ background:var(--raise); border:1px solid var(--line); border-radius:4px; padding:16px 18px; display:flex; flex-direction:column; gap:6px; }}
.rule strong {{ font-size:0.95rem; }}
.rule span {{ font-size:0.88rem; color:var(--muted); }}

.readout {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:4px; overflow:hidden; }}
.cell {{ background:var(--raise); padding:15px 16px; display:flex; flex-direction:column; gap:4px; }}
.cell-label {{ font-size:0.63rem; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); }}
.cell-val {{ font-size:1.4rem; font-variant-numeric:tabular-nums; letter-spacing:-0.02em; line-height:1.1; }}
.cell-sub {{ font-size:0.72rem; color:var(--muted); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }}

.empty-stats {{ background:var(--raise); border:1px solid var(--line); border-radius:4px; padding:20px; display:flex; flex-direction:column; gap:6px; }}
.empty-stats span {{ color:var(--muted); font-size:0.9rem; }}
.empty {{ color:var(--muted); }}

.tickets {{ display:flex; flex-direction:column; gap:12px; }}
.ticket {{ background:var(--raise); border:1px solid var(--line); border-radius:4px; overflow:hidden; }}
.t-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:13px 16px; border-bottom:1px solid var(--line-soft); flex-wrap:wrap; }}
.t-id {{ display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }}
.t-type {{ font-size:0.72rem; letter-spacing:0.09em; text-transform:uppercase; font-weight:700; }}
.t-date {{ font-size:0.74rem; color:var(--muted); }}
.t-status {{ font-size:0.66rem; letter-spacing:0.09em; text-transform:uppercase; padding:3px 9px; border-radius:2px; border:1px solid var(--line); color:var(--muted); }}
.t-status.won {{ color:var(--up); border-color:var(--up); }}
.t-status.lost {{ color:var(--down); border-color:var(--down); }}

.sels {{ list-style:none; margin:0; padding:0; }}
.sel {{ display:grid; grid-template-columns:1fr auto auto; gap:6px 16px; padding:12px 16px; border-bottom:1px solid var(--line-soft); align-items:center; }}
.sel:last-child {{ border-bottom:none; }}
.sel-match {{ display:flex; flex-direction:column; gap:2px; min-width:0; }}
.sel-teams {{ font-size:0.92rem; }}
.sel-league {{ font-size:0.76rem; color:var(--muted); }}
.sel-pick {{ display:flex; flex-direction:column; gap:2px; text-align:right; }}
.sel-market {{ font-size:0.72rem; color:var(--muted); }}
.sel-choice {{ font-size:0.88rem; }}
.sel-odds {{ font-size:0.95rem; font-variant-numeric:tabular-nums; min-width:52px; text-align:right; }}

.masked {{ padding:16px; display:flex; flex-direction:column; gap:5px; font-size:0.92rem; }}
.masked span {{ font-size:0.84rem; color:var(--muted); }}

.t-foot {{ display:flex; gap:24px; padding:12px 16px; border-top:1px solid var(--line-soft); flex-wrap:wrap; }}
.t-cell {{ display:flex; flex-direction:column; gap:2px; }}
.t-label {{ font-size:0.62rem; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); }}
.t-val {{ font-size:0.95rem; font-variant-numeric:tabular-nums; }}

footer {{ border-top:1px solid var(--line); padding-top:20px; font-size:0.75rem; color:var(--muted); line-height:1.8; display:flex; flex-direction:column; gap:10px; }}

@media (max-width:560px) {{
  .sel {{ grid-template-columns:1fr auto; }}
  .sel-pick {{ text-align:left; }}
  .wrap {{ gap:36px; padding-top:32px; }}
}}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <p class="eyebrow">ApexSignal — veřejný účet</p>
  <h1>Všechny tikety. Včetně těch, co nevyšly.</h1>
  <p class="lede">Tohle je kompletní záznam fotbalového modelu, na kterém ApexSignal stojí.
  Nic se odsud nemaže a nic se nepřikrášluje — čísla níž si můžeš kdykoli přepočítat sám.</p>
</header>

<section class="rules">
  <div class="rule">
    <strong>Jde sem každý tiket</strong>
    <span>Výhry i prohry, automaticky, bez mého zásahu. Když má model špatný týden, uvidíš to tady dřív než kdekoli jinde.</span>
  </div>
  <div class="rule">
    <strong>Tipy jsou skryté do konce zápasu</strong>
    <span>U nevyhodnoceného tiketu vidíš jen počet tipů, kurz a vklad. Po skončení se výběry odhalí natrvalo — nejde je zpětně měnit.</span>
  </div>
</section>

<section>
  {_stats_block(stats)}
</section>

<section>
  <h2>Historie tiketů</h2>
  <div class="tickets">
    {cards}
  </div>
</section>

<footer>
  <span>ApexSignal je nástroj na rozhodování, ne sázková kancelář. Tikety si sázíš sám, kde chceš.
  Model se plete a plést se bude — žádný tip tu není jistota.</span>
  <span><strong>18+. Hazardní hraní může být návykové. Sázej jen to, co si můžeš dovolit prohrát.</strong>
  Pomoc při problémech s hraním: <a href="https://www.hazardni-hrani.cz">hazardni-hrani.cz</a>,
  Národní linka pro odvykání 800 350 000.</span>
</footer>

</div>
</body>
</html>"""
