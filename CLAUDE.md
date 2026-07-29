# ApexSignal — kontext pro pokračující konverzaci

Tenhle soubor slouží k tomu, aby nová konverzace (nové okno Claude Code) mohla
navázat tam, kde skončila ta předchozí, aniž by musela znovu objevovat
architekturu a rozhodnutí od nuly. Neobsahuje žádná hesla ani API klíče —
proč a kde jsou uložené místo toho, popisuje sekce "Přístupy a klíče" níže.

## Co ApexSignal je

Česká SaaS appka na fotbalové sázkařské tipy (predikce zápasů, ne live
sázení). Model: appka denně vygeneruje tikety (kombinace více zápasů) na
základě vlastního pravděpodobnostního modelu + tržních kurzů, a rozešle je
platícím odběratelům na Telegram. Provozovatel: David Novik, IČO 05010276.

- **Produkce appky:** https://apexsignal.cz (frontend), backend na
  `https://apexsignal-backend.onrender.com`
- **Transparentní účet:** `/transparentni-ucet` — veřejná stránka se
  skutečnou historií appčiných tiketů (žádné vymyšlené čísla), účel je
  budovat důvěru.

## Repozitáře

1. **`apexsignal-backend`** (tenhle repo) — FastAPI backend na Renderu
   (`srv-d8puije7r5hc7399afh0`), single web service. Auto-deploy z branch
   `main` (push do `main` = během pár minut live na Renderu). Vývojová
   branch v aktuální session: `claude/marketing-window-bdtg3r`.
   Databáze: PostgreSQL na Renderu (free plán — pozor, appka podle Renderovy
   politiky časem expiruje, viz komentář v `render.yaml`).
2. **`apexsignal-app`** (`/workspace/apexsignal-app` v této session,
   GitHub: `apexsignal/apexsignal-app`) — frontend, prakticky celá appka
   je v jednom souboru `index.html` (React bez buildu, JSX přes Babel
   v prohlížeči, jeden minifikovaný `<script>` tag). Nasazuje se ale
   **přímo přes Netlify CLI/MCP** (`mcp__Netlify__netlify-deploy-services-updater`,
   site ID `6039a72d-adb0-4efa-b0a2-798e2f7f8e63`), NE automaticky z GitHub
   pushe — push do `main` appku historizuje, ale nenasazuje. Nasazení =
   spustit `npx @netlify/mcp` příkaz, který ten nástroj vrátí.

   **Důležité pro editaci `index.html`:** je to jeden obří minifikovaný
   řádek. Po každé úpravě ověřit syntaxi PŘED nasazením:
   ```
   python3 -c "import re; c=open('index.html',encoding='utf-8').read(); \
   s=re.findall(r'<script(?![^>]*src)[^>]*>(.*?)</script>', c, re.S); \
   open('/tmp/verify.js','w',encoding='utf-8').write(s[0])"
   node --check /tmp/verify.js && echo OK
   ```
   Nikdy needitovat string-replace naslepo — najít přesné hranice přes
   Python `.find()` na skutečném obsahu souboru (i jednotlivé emoji mají
   různá unicode kódování, tichý no-op replace je snadná chyba).

## Aktuální stav ceníku (živě, k 2026-07-29)

Appka prošla nedávno větší přeceňovací změnou (viz commit log níže):

- **500 Kč/měsíc** — pasivní odběr (appka pošle 1 krátký + 1 střední tiket
  denně na Telegram). Dřív 2 500 Kč.
- **5 000 Kč/měsíc** — "Neomezené generování", cíleno na lidi co chtějí
  vlastní tipsterský byznys/reselling. Strop 10 generování/den (soft cap,
  `UNLIMITED_GENERATION_DAILY_CAP`), mimo tokenový systém i mimo globální
  zámek generování.
- **Tokeny na jednotlivé tikety:** krátký = 10 tokenů (200 Kč), střední =
  15 tokenů (300 Kč), 1 token pořád = 20 Kč (`TOKEN_KC_VALUE`).
- **BOOST tiket byl kompletně zrušen** — jak z denní automatiky, tak
  z klientského generování, tokenového ceníku i landing page. Uživatel na
  tom trval opakovaně a explicitně ("Boost uplne vyhod at to neni videt").
  Historické BOOST tikety v historii jednotlivých uživatelů zůstávají
  zobrazené a správně popsané (transparentnost > úklid) — **tohle nebylo
  s uživatelem výslovně potvrzené, jen můj odhad, co by chtěl.**
- Jedno místo pravdy pro ceny: `_app_equivalent_monthly_kc()` a
  `TOKEN_COSTS` v `backend_api.py`, `/tokens/prices` endpoint. Landing page
  (transparency_page.py) i appka (index.html) z něj čtou / jsou s ním ručně
  sesynchronizované — při další změně ceny hledat všechny 3 místa.

## Klíčové architektonické mechanismy

- **Denní generování je JEDNO volání, broadcast všem:** `/admin/daily-tickets`
  vygeneruje tikety jednou, `/admin/client-tickets-send` je rozešle všem
  platícím Telegram odběratelům ve smyčce (`db.get_paid_telegram_subscribers()`)
  — bez dalších API volání na příjemce. Náklady na API-Football tedy škálují
  s POČTEM DNÍ generování, ne s počtem odběratelů — dokud appka nepustí
  širší self-serve generování (což je přesně nová "5000 Kč neomezené"
  featura).
- **Práh kvality 65 %:** `FALLBACK_THRESHOLDS` v `probability_model.py`
  zkouší {70 %, 65 %} pro krátký/střední (65 % je tvrdé dno, uživatel to
  chtěl takhle: "Ok dame tedy limit 65% maximalne. Vse niz uz ne.").
  Kandidát musí splňovat práh na OBOU číslech — `model_probability` i na
  zobrazovaném `probability` (které je tržní, pokud je k dispozici, jinak
  model) — jinak appka uměla zobrazit klientovi tiket s 53 % i když interní
  model tvrdil 70+ % (opraveno v PR #110, byl to skutečný, dřív skrytý,
  problém důvěryhodnosti).
- **`MAX_FIXTURES_PER_REQUEST=150`** v `data_provider.py` se dělí počtem
  dnů v okně hledání (`per_day_limit = max(150 // len(dates), 1)`) — širší
  vícedenní hledání dostane poměrně méně zápasů na den než jednodenní.
  Neimplementovaná, ale identifikovaná optimalizace zdarma (bez extra
  nákladů na API).
- **Neomezené generování:** `users.unlimited_until` (TIMESTAMPTZ) +
  atomický denní čítač (`daily_generations_count`/`daily_generations_date`,
  jeden `UPDATE ... RETURNING` s `CASE`, kvůli race conditions). Aktivuje
  se Stripe webhookem (`checkout.session.completed`, metadata
  `unlimited_generation=="1"`) nebo ručně přes `POST /admin/set-unlimited`.

## Stojící pravidla od uživatele (nezapomenout)

- **"Nejdriv semnou konzultuj vzdy nez neco budes menit!!!"** — vždy
  nejdřív konzultovat, než se něco změní. (V praxi: pokud přijde jasný,
  přímý příkaz, ten už JE konzultace/schválení — neptat se znovu na to samé.)
- **SportBreak.cz nahrávání tiketů musí zůstat manuální, human-in-the-loop.**
  Uživatel explicitně: "Automaticky to nejde protoze ja davam realne sazky
  na tipsportu." — appka nesmí sama automatizovat nahrávání reálných sázek,
  jen na pokyn a s konkrétními zápasy, co uživatel vloží.
- Komunikace vždy česky.
- Piš jen commentáře, kde je fakt potřeba vysvětlit PROČ (skrytá invariant,
  workaround), ne CO kód dělá.

## Přístupy a klíče (BEZ hodnot — jen kde je hledat)

Appka záměrně NEUKLÁDÁ žádné živé secrets do repozitáře (viz i vlastní
komentář appky v `render.yaml`: "vlož své klíče přímo tam, NIKDY do kódu
na GitHubu"). Nová konverzace si je musí získat z těchhle míst:

| Co | Kde to je | Env var / identifikátor |
|---|---|---|
| Databáze | Render dashboard (auto, `fromDatabase`) | `DATABASE_URL` |
| API-Football (fotbal/hokej/basket) | dashboard.api-sports.io → Render | `APISPORTS_KEY` |
| Tenis (appka tenis nepoužívá, viz níže) | api-tennis.com → Render | `APITENNIS_KEY` |
| Živé kurzy | the-odds-api.com → Render | `ODDSAPI_KEY` |
| AI kontrola tiketů | console.anthropic.com → Render | `ANTHROPIC_API_KEY` |
| Podpis přihlašovacích tokenů | Render (NIKDY neměnit po nasazení) | `SECRET_KEY` |
| Admin endpointy (denní cron atd.) | Render + GitHub Actions repo secret (musí sedět) | `ADMIN_TASK_KEY` |
| Google přihlášení | Render | `GOOGLE_CLIENT_ID` |
| Stripe (LIVE mode, ne test!) | Stripe dashboard → Render | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` |
| Stripe Payment Link (kanál 500 Kč) | Stripe dashboard → Render | `STRIPE_CHANNEL_PAYMENT_LINK_URL` |
| Telegram bot | @BotFather → Render | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHAT_ID_WIFE`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` |
| Účty appky pro cron úlohy | Render (ID appčiných účtů v DB) | `DAILY_TICKETS_USER_ID`, `TRANSPARENCY_USER_ID`, `TEST3_USER_ID` |
| Netlify (frontend deploy) | předkonfigurováno v MCP nástroji, není potřeba token ručně | site ID `6039a72d-adb0-4efa-b0a2-798e2f7f8e63` |
| **SportBreak.cz** (ruční nahrávání reálných tiketů) | appka login zná: `apexsignal02@seznam.cz` — **heslo appka do repa neukládá**, sdělí ho uživatel přímo v chatu nové konverzaci, až ho bude appka potřebovat | — |

Pozn.: appka je "fotbal only" byznys rozhodnutí (uživatel: "Ne bude jen
fotbal žadnej tenis") — `APITENNIS_KEY` v kódu existuje, ale appka ho
aktivně nevyužívá pro produkční tikety.

## Rozpracované / otevřené věci k 2026-07-29

- Uživatel zvažoval koupi API-Football plánu "Mega" (150 000 req/den,
  $39+DPH) kvůli rozšíření na 300 zápasů/den — řekl "Ja pak koupím tohle!!",
  **není potvrzené, jestli to už koupil.**
- Debata "co appka může appka ještě predikovat / na čem appka může appka
  ještě vydělat" (sportovní i nesportovní nápady) — appka to nechala
  otevřené, uživatel odmítl první nesázkařský nápad (sledování cen) bez
  náhrady: "Ne to se mi nelibi." Žádný závěr, žádný úkol z toho neplyne,
  dokud appka neřekne dál.
- Historické BOOST tikety v historii jednotlivých účtů zůstávají
  zobrazené (viz sekce ceníku výše) — nebylo explicitně potvrzené.

## Poslední větší commity (nejnovější nahoře, `main`)

```
77d9e5b (#116) Přecenit kratky/stredni na 200/300 Kč (10/15 tokenů)
6ad7742 (#115) Hotfix: opravit KeyError('boost') na /transparentni-ucet
993d3dc (#114) Aktualizovat ceny (500/5000 Kč), odstranit BOOST z tokenového ceníku
8a9695e (#113) Přidat /admin/set-unlimited pro ruční správu neomezeného tarifu
e4ec2b6 (#112) Přidat neomezený tarif (5000 Kč/měsíc, strop 10 generování/den)
4b8c31b (#111) Zrušit BOOST v denní automatice, rozšířit okno kratky/stredni na 2 dny
ce7b498 (#110) Vyžadovat práh i na zobrazovaném (tržním) čísle, ne jen na modelu
dc83da6 (#109) Zvednout minimální fallback práh kratky/stredni z 60 % na 65 %
```

Poznámka k gitu: appka opakovaně narazila na `mergeable_state: dirty` po
squash-mergi ze stejné dlouhověké branch (PR #108, #116 — stejná třída
problému). Fix: `git fetch origin main && git reset --hard origin/main`,
znovu aplikovat jen zamýšlený diff, `git push --force-with-lease`.

## Frontend (`apexsignal-app`) — poslední živě ověřený commit

`7e92e2c` — "Odstranit BOOST z výběru délky tiketu a vysvětlujících textů".
Ověřeno živě na apexsignal.cz. Je pushnutý na `origin/main` tohoto repa.
