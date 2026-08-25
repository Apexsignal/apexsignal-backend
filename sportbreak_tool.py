"""
sportbreak_tool.py — osobní appka pro Davida (ne pro klienty appky): vložit
zkopírovaný text tiketu z Tipsportu a nechat ho appku rovnou zapsat na
SportBreak.cz, bez ručního přepisování polí ve formuláři appky.

Mechanika (přihlašovací formulář, pole ticketComponents[N][...], zamčení
match-fields po uložení) zreverzeovaná ručně v chatu — viz CLAUDE.md,
sekce SportBreak.cz. SportBreak po uložení zamyká date/home/away/tip/
course/matchUrl/state každé nohy kombinace — jednou uložené jde jen
doplnit confidence/betOffice/service/textAnalysis, chybu v zápase samotném
oprava nevezme (appka na to narazila živě, viz commit historie).

Přístup jen appka umí přes ADMIN_TASK_KEY (stejný klíč appka používá pro
/admin/* endpointy) — appka SportBreak heslo nikde neukládá, appka ho jen
tato appka pošle přímo do žádosti na sportbreak.cz.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

SPORTBREAK_BASE = "https://sportbreak.cz"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

SPORT_OPTIONS = [
    ("football", "Fotbal"), ("hockey", "Hokej"), ("tennis", "Tenis"), ("basketball", "Basket"),
    ("table-tennis", "Stolní tenis"), ("darts", "Šipky"), ("athletics", "Atletika"),
    ("alpineSkiing", "Alpské lyžování"), ("american-football-rugby", "Americký fotbal"),
    ("badminton", "Badminton"), ("bandy", "Bandy"), ("baseball", "Basebal"), ("biathlon", "Biatlon"),
    ("box", "Box"), ("cycling", "Cyklistika"), ("horse-racing", "Dostihy"), ("e-sports", "Esporty"),
    ("floorball", "Florbal"), ("futsal", "Futsal"), ("golf", "Golf"), ("handball", "Házená"),
    ("kriket", "Kriket"), ("martial-arts", "MMA"), ("motorsport", "Motosport"), ("rugby", "Rugby"),
    ("skiJumping", "Skoky na lyžích"), ("snooker", "Snooker"), ("social-betting", "Společenské sázky"),
    ("squash", "Squash"), ("chess", "Šachy"), ("waterPolo", "Vodní pólo"), ("volleyball", "Volejbal"),
    ("winter-sport", "Zimní sporty"), ("others", "Ostatní"),
]

COUNTRY_OPTIONS = [
    ("AF", "Afrika"), ("AL", "Albánie"), ("DZ", "Alžírsko"), ("AD", "Andorra"), ("AO", "Angola"),
    ("AR", "Argentina"), ("AM", "Arménie"), ("AU", "Austrálie"), ("AZ", "Ázerbajdžán"), ("BS", "Bahamy"),
    ("BH", "Bahrajn"), ("BD", "Bangladéš"), ("BB", "Barbados"), ("MM", "Barma"), ("BE", "Belgie"),
    ("BZ", "Belize"), ("BY", "Bělorusko"), ("BJ", "Benin"), ("BM", "Bermudy"), ("BO", "Bolívie"),
    ("BA", "Bosna-Hercegovina"), ("BW", "Botswana"), ("BR", "Brazílie"), ("BN", "Brunej"),
    ("BT", "Brútán"), ("BG", "Bulharsko"), ("BF", "Burkina Faso"), ("BI", "Burundi"), ("TD", "Čad"),
    ("ME", "Černá Hora"), ("CZ", "Česko"), ("CL", "Chile"), ("HR", "Chorvatsko"), ("CN", "Čína"),
    ("CK", "Cookovy ostrovy"), ("DK", "Dánsko"), ("DM", "Dominika"), ("DO", "Dominikánská Republika"),
    ("EG", "Egypt"), ("EC", "Ekvádor"), ("EE", "Estonsko"), ("ET", "Etiopie"), ("EU", "Evropa"),
    ("FO", "Faerské ostrovy"), ("FJ", "Fidži"), ("PH", "Filipíny"), ("FI", "Finsko"), ("FR", "Francie"),
    ("GA", "Gabon"), ("GM", "Gambie"), ("GH", "Ghana"), ("GI", "Gibraltar"), ("GD", "Grenada"),
    ("GE", "Gruzie"), ("GU", "Guam"), ("GT", "Guatemala"), ("GN", "Guinea"), ("GY", "Guyana"),
    ("HT", "Haiti"), ("HN", "Honduras"), ("HK", "Hongkong"), ("IN", "Indie"), ("ID", "Indonésie"),
    ("IQ", "Irák"), ("IR", "Írán"), ("IE", "Irsko"), ("IS", "Island"), ("IT", "Itálie"),
    ("IL", "Izrael"), ("JM", "Jamajka"), ("JP", "Japonsko"), ("YE", "Jemen"),
    ("ZA", "Jihoafrická republika"), ("SUS", "Jižní Amerika"), ("KR", "Jižní Korea"),
    ("JO", "Jordánsko"), ("KY", "Kajmanské ostrovy"), ("KH", "Kambodža"), ("CM", "Kamerun"),
    ("CA", "Kanada"), ("CV", "Kapverdy"), ("QA", "Katar"), ("KZ", "Kazachstán"), ("KE", "Keňa"),
    ("KP", "KLDR"), ("CO", "Kolumbie"), ("KM", "Komory"), ("CD", "Kongo"), ("XK", "Kosovo"),
    ("CR", "Kostarika"), ("CU", "Kuba"), ("KW", "Kuvajt"), ("CY", "Kypr"), ("KG", "Kyrgyzstán"),
    ("LA", "Laos"), ("LS", "Lesotho"), ("LB", "Libanon"), ("LR", "Libérie"), ("LY", "Libye"),
    ("LI", "Lichtenštejnsko"), ("LT", "Litva"), ("LV", "Lotyšsko"), ("LU", "Lucembursko"),
    ("MO", "Macao"), ("MG", "Madagaskar"), ("HU", "Maďarsko"), ("MY", "Malajsie"), ("MW", "Malawi"),
    ("MV", "Maledivy"), ("ML", "Mali"), ("MT", "Malta"), ("MA", "Maroko"), ("MQ", "Martinik"),
    ("MU", "Mauricius"), ("MR", "Mauritánie"), ("MX", "Mexiko"), ("MD", "Moldavsko"), ("MC", "Monako"),
    ("MN", "Mongolsko"), ("MZ", "Mosambik"), ("DE", "Německo"), ("NP", "Nepál"), ("NG", "Nigérie"),
    ("NI", "Nikaragua"), ("NL", "Nizozemí"), ("NO", "Norsko"), ("NC", "Nová Kaledonie"),
    ("NZ", "Nový Zéland"), ("OM", "Omán"), ("OT", "Ostatní"), ("PK", "Pákistán"), ("PS", "Palestina"),
    ("PA", "Panama"), ("PG", "Papua-Nová Guinea"), ("PY", "Paraguay"), ("PE", "Peru"),
    ("CI", "Pobřeží slonoviny"), ("PL", "Polsko"), ("PR", "Portoriko"), ("PT", "Portugalsko"),
    ("AT", "Rakousko"), ("GR", "Řecko"), ("GQ", "Rovníková Guinea"), ("RO", "Rumunsko"),
    ("RU", "Rusko"), ("RW", "Rwanda"), ("SB", "Šalomounovy ostrovy"), ("SV", "Salvador"),
    ("WS", "Samoa"), ("SM", "San Marino"), ("SA", "Saudska arabie"), ("SN", "Senegal"),
    ("NA", "Severní Amerika"), ("MK", "Severní Makedonie"), ("SC", "Seychelly"), ("SL", "Sierra Leone"),
    ("SG", "Singapur"), ("SK", "Slovensko"), ("SI", "Slovinsko"), ("SO", "Somalsko"),
    ("ES", "Španělsko"), ("AE", "Spojené arabské emiráty"), ("RS", "Srbsko"), ("LK", "Srí Lanka"),
    ("CF", "Středoafrická republika"), ("SD", "Súdán"), ("KN", "Sv. Kryštof"), ("LC", "Sv. Lucie"),
    ("VC", "Sv. Vincenc a Grenadiny"), ("SE", "Švédsko"), ("WW", "Svět"), ("CH", "Švýcarsko"),
    ("SY", "Sýrie"), ("TJ", "Tádžikistán"), ("TZ", "Tanzanie"), ("TW", "Tchaj-wan"), ("TH", "Thajsko"),
    ("TG", "Togo"), ("TO", "Tonga"), ("TT", "Trinidad a Tobago"), ("TN", "Tunisko"), ("TR", "Turecko"),
    ("UG", "Uganda"), ("UA", "Ukrajina"), ("UY", "Uruguay"), ("US", "USA"), ("UZ", "Uzbekistan"),
    ("VA", "Vatikán"), ("GB", "Velká Británie"), ("VE", "Venezuela"), ("VN", "Vietnam"),
    ("TL", "Východní Timor"), ("ZM", "Zambie"), ("ZW", "Zimbabwe"),
]


def _check_key(key: Optional[str]):
    expected = os.environ.get("ADMIN_TASK_KEY")
    if not expected or key != expected:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící klíč")


# =====================================================================
# Parsing vloženého textu — appka kopíruje bloky přímo ze stránky
# Tipsportu, takže appka počítá s tímhle opakujícím se tvarem:
#   [Dnes|Zítra|DD. M. RRRR (zkr)]      <- volitelné, appka bez data bere "Dnes"
#   HH:MM
#   Tým A - Tým B
#   <popis trhu>: <výběr>
#   <kurz>
#   Aktuální kurz
#   <kurz>
# a na konci volitelně "Uloženo s celkovým kurzem" + číslo, pak řádky
# s tipsport.cz odkazy — ty ale nejsou nutně ve stejném pořadí jako
# zápasy nahoře, appka je proto páruje podle jmen týmů ve slugu URL.
# =====================================================================

_DATE_EXPLICIT_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_SLUG_SPORT_RE = re.compile(r"/(fotbal|tenis)-([a-z0-9-]+?)(?:/\d+)?(?:/co-se-sazi)?/?$")


def _slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def _match_url_for_leg(home: str, away: str, urls: list[str]) -> tuple[str, str]:
    home_tokens = [t for t in _slugify(home).split("-") if len(t) > 2]
    away_tokens = [t for t in _slugify(away).split("-") if len(t) > 2]
    best_url, best_sport, best_score = "", "", -1
    for url in urls:
        m = _SLUG_SPORT_RE.search(url.split("?")[0])
        sport_word = m.group(1) if m else ("tenis" if "/tenis-" in url else "fotbal" if "/fotbal-" in url else "")
        score = sum(1 for t in home_tokens if t in url) + sum(1 for t in away_tokens if t in url)
        if score > best_score:
            best_score = score
            best_url = url
            best_sport = "tennis" if sport_word == "tenis" else "football"
    return best_url, (best_sport or "football")


def _resolve_date(label: Optional[str], today):
    if label is None:
        return today
    low = label.strip().lower()
    if low == "dnes":
        return today
    if low == "zítra":
        return today + timedelta(days=1)
    m = _DATE_EXPLICIT_RE.search(label)
    if m:
        d, mo, y = m.groups()
        return datetime(int(y), int(mo), int(d)).date()
    return today


def parse_pasted_ticket(text: str, today=None) -> list[dict]:
    if today is None:
        today = datetime.now().date()

    raw_lines = [l.strip() for l in text.strip().splitlines()]
    lines = [l for l in raw_lines if l != ""]

    urls = [l for l in lines if l.startswith("http")]
    body = [l for l in lines if not l.startswith("http")]

    # appka ustřihne "Uloženo s celkovým kurzem" + číslo z konce — appka
    # kurz kombinace vždycky spočítá sama ze zadaných výběrů, nepřebírá ho
    if len(body) >= 2 and body[-2].lower().startswith("uloženo s celkovým kurzem"):
        body = body[:-2]

    legs = []
    i = 0
    guard = 0
    while i < len(body) and guard < 500:
        guard += 1
        date_label = None
        if body[i].lower() in ("dnes", "zítra") or _DATE_EXPLICIT_RE.search(body[i]):
            date_label = body[i]
            i += 1
        if i >= len(body) or not _TIME_RE.match(body[i]):
            i += 1
            continue
        time_str = body[i]
        i += 1
        if i >= len(body) or " - " not in body[i]:
            continue
        teams_line = body[i]
        i += 1
        home, _, away = teams_line.partition(" - ")

        tip = body[i] if i < len(body) else ""
        i += 1
        course = body[i] if i < len(body) else ""
        i += 1
        if i < len(body) and body[i].lower() == "aktuální kurz":
            i += 2  # appka přeskočí nálepku i druhé (živé, ne sázené) číslo

        match_date = _resolve_date(date_label, today)
        url, sport = _match_url_for_leg(home.strip(), away.strip(), urls)

        legs.append({
            "sport": sport,
            "date": f"{match_date.isoformat()}T{time_str}",
            "country": "",
            "league": "",
            "home": home.strip(),
            "away": away.strip(),
            "tip": tip.strip(),
            "course": course.strip(),
            "matchUrl": url,
        })

    return legs


# =====================================================================
# SportBreak klient — přihlášení, zjištění service, odeslání tiketu.
# Stejná mechanika appka ověřila ručně přes /cs/a/tiket/pridani.
# =====================================================================

def _sb_login(session: requests.Session, email: str, password: str) -> bool:
    session.headers.update({"User-Agent": UA})
    session.get(f"{SPORTBREAK_BASE}/cs/", timeout=15)
    login_data = {
        "email": email,
        "password": password,
        "_do": "signInForm-form-submit",
        "_submit": "Přihlásit se k odběru",
    }
    session.post(
        f"{SPORTBREAK_BASE}/cs/",
        data=login_data,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{SPORTBREAK_BASE}/cs/"},
        timeout=15,
    )
    r = session.get(f"{SPORTBREAK_BASE}/cs/nastaveni", timeout=15)
    return "Můj účet" in r.text or "Odhlásit" in r.text


def _sb_service_id(session: requests.Session) -> Optional[str]:
    r = session.get(f"{SPORTBREAK_BASE}/cs/a/tiket/pridani", timeout=15)
    m = re.search(r'name="service"[^>]*>(.*?)</select>', r.text, re.S)
    if not m:
        return None
    opts = re.findall(r'<option[^>]*value="([^"]*)"[^>]*>', m.group(1))
    return opts[0] if opts else None


def _sb_submit_ticket(session: requests.Session, service: str, confidence: int, legs: list[dict]) -> requests.Response:
    data = {
        "confidence": str(confidence),
        "betOffice": "Tipsport",
        "service": service,
        "textAnalysis": "",
        "_do": "ticketManipulateForm-form-submit",
        "_submit": "ULOŽIT",
    }
    for i, leg in enumerate(legs):
        for key in ("sport", "date", "country", "league", "home", "away", "tip", "course", "matchUrl"):
            data[f"ticketComponents[{i}][{key}]"] = leg.get(key, "")

    return session.post(
        f"{SPORTBREAK_BASE}/cs/a/tiket/pridani",
        data=data,
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{SPORTBREAK_BASE}/cs/a/tiket/pridani"},
        timeout=20,
    )


# =====================================================================
# Routy
# =====================================================================

@router.get("/admin/sportbreak", response_class=HTMLResponse)
def sportbreak_page(key: str = ""):
    _check_key(key)
    return HTMLResponse(_render_page(key))


@router.post("/admin/sportbreak/parse")
def sportbreak_parse(request: Request, payload: dict = Body(...)):
    _check_key(request.headers.get("X-Admin-Key"))
    text = payload.get("text", "")
    legs = parse_pasted_ticket(text)
    return JSONResponse({"legs": legs})


@router.post("/admin/sportbreak/submit")
def sportbreak_submit(request: Request, payload: dict = Body(...)):
    _check_key(request.headers.get("X-Admin-Key"))
    email = payload.get("email", "").strip()
    password = payload.get("password", "")
    confidence = int(payload.get("confidence", 10))
    legs = payload.get("legs", [])
    if not email or not password:
        raise HTTPException(status_code=400, detail="Chybí e-mail nebo heslo k SportBreaku")
    if not legs:
        raise HTTPException(status_code=400, detail="Žádné zápasy k odeslání")

    session = requests.Session()
    if not _sb_login(session, email, password):
        return JSONResponse({"ok": False, "message": "Přihlášení na SportBreak selhalo — zkontroluj e-mail/heslo."})

    service = _sb_service_id(session)
    if not service:
        return JSONResponse({"ok": False, "message": "Na účtu appka nenašla žádnou service (sázkové poradenství)."})

    r = _sb_submit_ticket(session, service, confidence, legs)
    ok = r.status_code == 200 and '"redirect"' in r.text
    return JSONResponse({
        "ok": ok,
        "message": "Tiket uložen na SportBreak." if ok else f"SportBreak vrátil neočekávanou odpověď (HTTP {r.status_code}).",
        "raw": r.text[:300] if not ok else "",
    })


def _render_page(key: str) -> str:
    sport_opts = "".join(f'<option value="{v}">{t}</option>' for v, t in SPORT_OPTIONS)
    country_opts = "".join(f'<option value="{v}">{t}</option>' for v, t in COUNTRY_OPTIONS)

    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SportBreak nahrávač</title>
<meta name="robots" content="noindex, nofollow">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f1115; color: #e6e8ec; margin: 0; padding: 24px; }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #8a90a0; font-size: 13px; margin-bottom: 24px; }}
  section {{ background: #171a21; border: 1px solid #262b36; border-radius: 10px; padding: 18px; margin-bottom: 16px; }}
  label {{ display: block; font-size: 12px; color: #9aa1b1; margin-bottom: 4px; }}
  input[type=text], input[type=password], input[type=email], input[type=number],
  input[type=datetime-local], textarea, select {{
    width: 100%; background: #0f1115; border: 1px solid #2c3140; color: #e6e8ec;
    border-radius: 6px; padding: 8px 10px; font-size: 13px; font-family: inherit;
  }}
  textarea {{ min-height: 220px; resize: vertical; font-family: ui-monospace, monospace; }}
  .row {{ display: flex; gap: 12px; }}
  .row > div {{ flex: 1; }}
  button {{ background: #3d6bff; color: white; border: none; border-radius: 6px;
           padding: 10px 18px; font-size: 14px; cursor: pointer; font-weight: 600; }}
  button:hover {{ background: #2f56d6; }}
  button.secondary {{ background: #2c3140; }}
  button.secondary:hover {{ background: #3a4155; }}
  button:disabled {{ opacity: 0.5; cursor: default; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }}
  th {{ text-align: left; color: #9aa1b1; font-weight: 500; padding: 4px 6px; }}
  td {{ padding: 3px 6px; vertical-align: top; }}
  td input, td select {{ font-size: 12px; padding: 5px 6px; }}
  .msg {{ margin-top: 14px; padding: 10px 14px; border-radius: 6px; font-size: 13px; display: none; }}
  .msg.ok {{ display: block; background: #123a24; color: #6fe3a0; border: 1px solid #1e5c3a; }}
  .msg.err {{ display: block; background: #3a1717; color: #ff9d9d; border: 1px solid #5c1e1e; }}
  .hidden {{ display: none; }}
  .legcount {{ color: #8a90a0; font-size: 12px; margin-top: 6px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>SportBreak nahrávač</h1>
  <div class="sub">Vlož zkopírovaný tiket z Tipsportu, appka ho rozparsuje a zapíše na SportBreak.</div>

  <section>
    <div class="row">
      <div>
        <label>SportBreak e-mail</label>
        <input type="email" id="sb-email" placeholder="d.voves@seznam.cz" autocomplete="username">
      </div>
      <div>
        <label>SportBreak heslo</label>
        <input type="password" id="sb-password" placeholder="heslo" autocomplete="current-password">
      </div>
      <div style="flex: 0 0 100px;">
        <label>Vklad (1-10)</label>
        <input type="number" id="sb-confidence" min="1" max="10" value="10">
      </div>
    </div>
  </section>

  <section>
    <label>Vložený text tiketu (zápasy + odkazy)</label>
    <textarea id="paste-text" placeholder="Dnes&#10;21:00&#10;&#10;Tym A - Tym B&#10;Vysledek zapasu: Tym A&#10;1.50&#10;Aktualni kurz&#10;1.50&#10;&#10;https://www.tipsport.cz/kurzy/zapas/..."></textarea>
    <div style="margin-top: 10px;">
      <button id="btn-parse">Rozpoznat zápasy</button>
    </div>
  </section>

  <section id="review-section" class="hidden">
    <label style="margin-bottom: 10px;">Zkontroluj/oprav před odesláním (appka pole nechává upravitelná — hádá zemi/ligu jen omezeně)</label>
    <div id="legs-table"></div>
    <div class="legcount" id="leg-count"></div>
    <div style="margin-top: 14px; display: flex; gap: 10px;">
      <button id="btn-submit">Odeslat na SportBreak</button>
      <button class="secondary" id="btn-reset">Zrušit</button>
    </div>
  </section>

  <div class="msg" id="msg"></div>
</div>

<template id="sport-opts">{sport_opts}</template>
<template id="country-opts">{country_opts}</template>

<script>
const KEY = {key!r};
let legs = [];

function showMsg(text, ok) {{
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
}}

document.getElementById('btn-parse').addEventListener('click', async () => {{
  const text = document.getElementById('paste-text').value;
  document.getElementById('msg').className = 'msg';
  const btn = document.getElementById('btn-parse');
  btn.disabled = true;
  try {{
    const res = await fetch('/admin/sportbreak/parse', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Admin-Key': KEY}},
      body: JSON.stringify({{text}}),
    }});
    const data = await res.json();
    legs = data.legs || [];
    renderTable();
    document.getElementById('review-section').classList.toggle('hidden', legs.length === 0);
    if (legs.length === 0) showMsg('Appka v textu nenašla žádný rozpoznatelný zápas.', false);
  }} catch (e) {{
    showMsg('Chyba při rozpoznávání: ' + e, false);
  }} finally {{
    btn.disabled = false;
  }}
}});

function renderTable() {{
  const sportOpts = document.getElementById('sport-opts').innerHTML;
  const countryOpts = document.getElementById('country-opts').innerHTML;
  let html = '<table><tr><th>Sport</th><th>Datum</th><th>Stát</th><th>Liga</th>' +
             '<th>Domácí</th><th>Hosté</th><th>Tip</th><th>Kurz</th><th>Odkaz</th></tr>';
  legs.forEach((leg, i) => {{
    html += `<tr>
      <td><select data-i="${{i}}" data-k="sport">${{sportOpts}}</select></td>
      <td><input type="datetime-local" data-i="${{i}}" data-k="date" value="${{leg.date}}"></td>
      <td><select data-i="${{i}}" data-k="country" style="width:110px"><option value="">—</option>${{countryOpts}}</select></td>
      <td><input type="text" data-i="${{i}}" data-k="league" value="${{esc(leg.league)}}" style="width:120px"></td>
      <td><input type="text" data-i="${{i}}" data-k="home" value="${{esc(leg.home)}}" style="width:110px"></td>
      <td><input type="text" data-i="${{i}}" data-k="away" value="${{esc(leg.away)}}" style="width:110px"></td>
      <td><input type="text" data-i="${{i}}" data-k="tip" value="${{esc(leg.tip)}}" style="width:150px"></td>
      <td><input type="number" step="0.01" data-i="${{i}}" data-k="course" value="${{leg.course}}" style="width:60px"></td>
      <td><input type="text" data-i="${{i}}" data-k="matchUrl" value="${{esc(leg.matchUrl)}}" style="width:80px" title="${{esc(leg.matchUrl)}}"></td>
    </tr>`;
  }});
  html += '</table>';
  document.getElementById('legs-table').innerHTML = html;
  document.getElementById('leg-count').textContent = legs.length + ' noh(a/y) kombinace';

  document.querySelectorAll('#legs-table [data-k]').forEach(el => {{
    const i = +el.dataset.i, k = el.dataset.k;
    if (el.tagName === 'SELECT') el.value = legs[i][k] || '';
    el.addEventListener('change', () => {{ legs[i][k] = el.value; }});
    el.addEventListener('input', () => {{ legs[i][k] = el.value; }});
  }});
}}

function esc(s) {{
  return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}}

document.getElementById('btn-reset').addEventListener('click', () => {{
  legs = [];
  document.getElementById('review-section').classList.add('hidden');
  document.getElementById('paste-text').value = '';
  document.getElementById('msg').className = 'msg';
}});

document.getElementById('btn-submit').addEventListener('click', async () => {{
  const email = document.getElementById('sb-email').value.trim();
  const password = document.getElementById('sb-password').value;
  const confidence = +document.getElementById('sb-confidence').value || 10;
  if (!email || !password) {{ showMsg('Vyplň e-mail a heslo k SportBreaku.', false); return; }}
  const missing = legs.some(l => !l.home || !l.away || !l.course || !l.country);
  if (missing) {{ showMsg('U některé nohy chybí tým, kurz nebo stát — appka to bez toho neodešle.', false); return; }}

  const btn = document.getElementById('btn-submit');
  btn.disabled = true;
  btn.textContent = 'Odesílám...';
  try {{
    const res = await fetch('/admin/sportbreak/submit', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Admin-Key': KEY}},
      body: JSON.stringify({{email, password, confidence, legs}}),
    }});
    const data = await res.json();
    showMsg(data.message, data.ok);
    if (data.ok) {{
      legs = [];
      document.getElementById('review-section').classList.add('hidden');
      document.getElementById('paste-text').value = '';
    }}
  }} catch (e) {{
    showMsg('Chyba při odesílání: ' + e, false);
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'Odeslat na SportBreak';
  }}
}});
</script>
</body>
</html>"""
