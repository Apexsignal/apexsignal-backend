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
   branch v aktuální session: `claude/navazani-na-konverzaci-aikq9n` (na
   ni appka commituje, pak fast-forward pushne i do `main` — obě branch
   se vždy drží ve stejném stavu).
   Databáze: PostgreSQL na Renderu (placený plán `basic_256mb`, zálohy
   zapnuté — ověřeno v session z 2026-07-30).
2. **`apexsignal-app`** (klon appka drží lokálně v `/tmp/apexsignal-app`
   v rámci session, GitHub: `apexsignal/apexsignal-app`) — frontend,
   prakticky celá appka je v jednom souboru `index.html` (React bez
   buildu, JSX přes Babel v prohlížeči, jeden minifikovaný `<script>`
   tag). **Push na GitHub appce dlouhodobě nejde** (403, `add_repo`
   s `access=push` appce nikdy neprošlo přes MCP tool-approval gate —
   pokud to nová konverzace nezkusí a nevyjde jí to samé, nezdržovat se
   tím, rovnou pracovat s lokálním klonem). Nasazuje se **přímo přes
   Netlify CLI/MCP** (`mcp__...__netlify-deploy-services-updater`,
   operace `deploy-site`), NE přes GitHub push. Aktuální Netlify účet:
   `apexsignal03@seznam.cz`, **site ID `8caab98f-dbc0-4984-921b-846eb71d0c89`**
   (starý účet/site ID `6039a72d-...` došly kredity, appka na něj
   koncem července 2026 přestala mít i přístup — pokud se objeví staré
   ID v historii, je to ten mrtvý účet, ne tenhle). Nasazení = zavolat
   `deploy-site` s tímhle site ID, nástroj vrátí jednorázový
   `npx @netlify/mcp@latest --site-id ... --proxy-path "..."` příkaz —
   proxy-path token má krátkou platnost, spustit ho hned, ne uložit na
   později.

   **Důležité pro editaci `index.html`:** je to jeden obří minifikovaný
   řádek (soubor má ~170 řádků celkem, ale ten jeden se scriptem má
   desítky tisíc znaků — Read/Edit tool na něj běžně narazí na limit,
   appka ho musí editovat přes Python string-replace v Bash, ne přes
   Edit tool). Po každé úpravě ověřit syntaxi PŘED nasazením:
   ```
   python3 -c "import re; c=open('index.html',encoding='utf-8').read(); \
   s=re.findall(r'<script(?![^>]*src)[^>]*>(.*?)</script>', c, re.S); \
   open('/tmp/verify.js','w',encoding='utf-8').write(s[0])"
   node --check /tmp/verify.js && echo OK
   ```
   Nikdy needitovat string-replace naslepo — najít přesné hranice přes
   Python `.find()`/`.count()` (ověřit počet výskytů == 1) na skutečném
   obsahu souboru, ne z paměti/odhadu. Identifikátory jsou minifikované
   na 1-2 písmena (`e,t,a,n,o,r,l,i,c,s,d,m,p,u,v,g,y,f,h,k,b,R,E,x,z,w,
   K,V,B,D,F,W,L,H,j,O,I,P,$,U,Z,G,N,M...` — spousta se opakuje v různých
   scope, kolize hrozí hlavně uvnitř stejné funkce) — nové identifikátory
   se proto pojmenovávají celými slovy (`genProgress`, `formatSelection`,
   `pollGenerationProgress`, `MARKET_LABELS_CS`...), ať nekoliduje a je
   to čitelné.

## Aktuální stav ceníku (živě, k 2026-07-30 večer)

- **990 Kč/měsíc** — Telegram kanál, pasivní odběr (appka pošle hlavně
  krátký tiket denně, každý 3. den zkusí střední a pošle ho MÍSTO
  krátkého jen když najde skutečnou hodnotu). `CHANNEL_PRICE_KC`
  v `backend_api.py` — **POZOR, tohle je JEN zobrazovací číslo.**
  Skutečnou částku drží samostatný Stripe Payment Link
  (`STRIPE_CHANNEL_PAYMENT_LINK_URL` na Renderu), appka pro něj žádnou
  Checkout Session nevytváří. Když se cena mění, je potřeba vytvořit
  NOVÝ Payment Link (přes `POST /admin/create-channel-payment-link`,
  endpoint vrátí novou URL) a pak **ručně přepsat env var na Renderu** —
  nová konverzace na Render dashboard nemá přístup (žádný Render API
  token v session), takže jen předá URL a uživatel ji sám vloží. Env var
  se navíc bez nového deploye/restartu procesu neaplikuje (viz "Render
  gotcha" níže).
- **4 990 Kč/měsíc** — "Neomezené generování". `UNLIMITED_GENERATION_PRICE_KC`
  v `backend_api.py` — na rozdíl od kanálu appka pro něj vytváří Stripe
  Checkout Session DYNAMICKY při každém nákupu, takže změna týhle
  konstanty rovnou mění i reálně účtovanou částku, žádný ruční krok
  navíc není potřeba. Strop 10 generování/den (`UNLIMITED_GENERATION_DAILY_CAP`),
  mimo tokenový systém i mimo globální zámek generování.
- **Tokeny na jednotlivé tikety:** krátký = 10 tokenů (200 Kč), střední =
  15 tokenů (300 Kč), 1 token pořád = 20 Kč (`TOKEN_KC_VALUE`).
- **Promo kód `token500`** — jednorázově přidá 500 tokenů na účet, max
  1× na účet (vynuceno přes DB tabulku `redeem_code_uses`, kód se
  založil přes `POST /admin/tokens/create-code`).
- **BOOST tiket byl kompletně zrušen** — jak z denní automatiky, tak
  z klientského generování, tokenového ceníku i landing page. Uživatel na
  tom trval opakovaně a explicitně ("Boost uplne vyhod at to neni videt").
  Historické BOOST tikety v historii jednotlivých uživatelů zůstávají
  zobrazené a správně popsané (transparentnost > úklid) — **tohle nebylo
  s uživatelem výslovně potvrzené, jen odhad, co by chtěl.**
- Jedno místo pravdy pro ceny: `CHANNEL_PRICE_KC`, `UNLIMITED_GENERATION_PRICE_KC`,
  `TOKEN_COSTS`/`TOKEN_KC_VALUE` v `backend_api.py`, appka je posílá ven
  přes `/tokens/prices` endpoint. Landing page (transparency_page.py)
  i appka (index.html) mají svoje vlastní hardcodované kopie ceníkového
  textu (landing page vždy, appka jen u neomezeného — cenu kanálu appka
  natahuje dynamicky z `/tokens/prices`) — **při další změně ceny hledat
  990/4990 Kč ve všech třech souborech**, žádnou automatickou synchronizaci
  mezi nimi appka nedrží.

## Klíčové architektonické mechanismy

- **Denní generování je JEDNO volání, broadcast všem:** `/admin/daily-tickets`
  vygeneruje tikety jednou, `/admin/client-tickets-send` je rozešle všem
  platícím Telegram odběratelům ve smyčce (`db.get_paid_telegram_subscribers()`)
  — bez dalších API volání na příjemce. Náklady na API-Football tedy škálují
  s POČTEM DNÍ generování, ne s počtem odběratelů — u self-serve generování
  (neomezený tarif i tokeny) ale KAŽDÝ klik reálně volá API-Football, takže
  tam už náklady škálují s provozem.
- **Práh kvality 65 %:** `FALLBACK_THRESHOLDS` v `probability_model.py`
  zkouší {70 %, 65 %} pro krátký/střední (65 % je tvrdé dno, uživatel to
  chtěl takhle: "Ok dame tedy limit 65% maximalne. Vse niz uz ne.").
  Kandidát musí splňovat práh na OBOU číslech — `model_probability` i na
  zobrazovaném `probability` (které je tržní, pokud je k dispozici, jinak
  model) — jinak appka uměla zobrazit klientovi tiket s 53 % i když interní
  model tvrdil 70+ % (opraveno v PR #110, byl to skutečný, dřív skrytý,
  problém důvěryhodnosti).
- **API-Football běží na placeném plánu Mega** (potvrzeno koupené, viz
  commit `7053b08`) — rate limiter zvednutý na 15 req/s, `MAX_FIXTURES_PER_REQUEST`
  zvednutý na 400 v `data_provider.py`. Fetch teď jde přes všechny dny
  okna a doplňuje zápasy postupně až do stropu 400 (`5c41cf6` — dřív se
  strop dělil počtem dnů předem, což ochuzovalo širší okna zbytečně).
  `/tickets/generate` a `/tickets/regenerate` navíc nejdřív fetchují jen
  UŽIVATELEM zvolené (užší) okno a širší okno (+3 dny) zkusí, jen když
  se v užším nenajde dost kandidátů (`e1a8bc9`) — rychlejší generování
  v běžném případě.
- **Skutečný progress bar generování, ne jen dekorace:** `GET
  /tickets/generate-progress?request_id=...` (`7ece82c`) čte
  in-memory `_GENERATION_PROGRESS` dict (aktualizovaný z fixture-obohacovací
  smyčky), frontend ho pollovaně dotazuje přes `pollGenerationProgress`
  a ukazuje reálné "Zpracováno X/Y zápasů" % — nad starou dekorativní
  "Matrix" konzolí, kterou appka na uživatelovo přání nechala ("Udelej
  to co je vic top").
- **`MAX_FIXTURES_PER_REQUEST`** se teď plní postupně po dnech, dokud se
  nenaplní strop (400) — viz výše, nahrazuje starý model, kde se strop
  dělil počtem dnů předem.
- **Neomezené generování:** `users.unlimited_until` (TIMESTAMPTZ) +
  atomický denní čítač (`daily_generations_count`/`daily_generations_date`,
  jeden `UPDATE ... RETURNING` s `CASE`, kvůli race conditions). Aktivuje
  se Stripe webhookem (`checkout.session.completed`, metadata
  `unlimited_generation=="1"`) nebo ručně přes `POST /admin/set-unlimited`.
- **Generování je potvrzeně globálně odemknuté pro všechny reálné účty**
  (`CLIENT_TICKET_GENERATION_ENABLED` defaultuje na true), ne jen pro
  testovací/allowlistované — ověřeno živě přes čerstvě založený účet.
- **Settlement (vyhodnocení tiketů) teď běží pro VŠECHNY účty, ne jen
  appčiny cronové:** dřív `/tickets/save` zkusil vyhodnotit tiket JEDNOU,
  hned při uložení (zápas skoro nikdy neskončil, takže to prakticky nikdy
  nevyhodnotilo nic) a jen appčiny vlastní účty (`DAILY_TICKETS_USER_ID`
  atd.) měly opakované dosettlování uvnitř `run_daily_tickets`. Nový
  `POST /admin/settle-all-pending` (admin-key, žádný filtr na uživatele)
  prochází VŠECHNY pending tikety a zkouší je vyhodnotit — zapojený do
  `.github/workflows/daily-tickets.yml` jako poslední krok (`if: always()`).
- **`kickoff_time` se při ukládání tiketu už neztrácí:** `SaveSelectionRequest`
  dřív ten field vůbec neměl, takže ho Pydantic tiše zahodil, i když ho
  frontend posílal — bez něj `_try_settle_ticket` nemohl spolehlivě poznat,
  že zápas už skončil. Opraveno (`e99dcf7`), doplněno do obou míst, co
  `SelectionCandidate` staví (`/tickets/save` i `/admin/showcase/seed`).
- **`_last_batch_match_ids` (per-uživatel set nabídnutých-ale-neuložených
  zápasů, kvůli deduplikaci mezi opakovanými generováními) se dřív nikdy
  nečistil** — přestože vlastní komentář kódu tvrdil, že ano. Hromadění
  bez konce postupně vyloučilo čím dál víc zápasů z dalšího generování.
  Opraveno `Repo.clear_last_batch()`, voláno po každém `/tickets/save`
  (`cb73668`).
- **`/tickets/replace-selection` mělo méně bezpečnostních pojistek než
  sourozenecké endpointy** — chybělo `_filter_future_matches`,
  `_filter_within_days`, `_require_generation_enabled`, a exclude-set
  nezahrnoval historii uložených tiketů. Sjednoceno (`20c2e38`, `7e261e2`).

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
- **GitHub Actions denní cron (`daily-tickets.yml`) zůstává PLNĚ
  automatizovaný, bez manuálního schválení mezi generováním a odesláním
  reálným odběratelům.** Uživatel to výslovně zvážil a potvrdil: "Nech to
  plně automatizované." — appka na to explicitně nemá přidávat gate,
  pokud znovu neřekne jinak.

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
| Stripe Payment Link (kanál, 990 Kč) | Stripe dashboard → Render, appka umí vygenerovat nový přes `POST /admin/create-channel-payment-link` (jen konstantu `CHANNEL_PRICE_KC` NESTAČÍ změnit) | `STRIPE_CHANNEL_PAYMENT_LINK_URL` |
| Telegram bot | @BotFather → Render | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHAT_ID_WIFE`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET` |
| Účty appky pro cron úlohy | Render (ID appčiných účtů v DB) | `DAILY_TICKETS_USER_ID`, `TRANSPARENCY_USER_ID`, `TEST3_USER_ID` |
| Netlify (frontend deploy) | předkonfigurováno v MCP nástroji, není potřeba token ručně | site ID `8caab98f-dbc0-4984-921b-846eb71d0c89` (účet `apexsignal03@seznam.cz`) — **starý site ID `6039a72d-...` je mrtvý/bez přístupu, neuvažovat ho** |
| **SportBreak.cz** (ruční nahrávání reálných tiketů) | appka login zná: `apexsignal02@seznam.cz` — **heslo appka do repa neukládá**, sdělí ho uživatel přímo v chatu nové konverzaci, až ho bude appka potřebovat | — |

Pozn.: appka je "fotbal only" byznys rozhodnutí (uživatel: "Ne bude jen
fotbal žadnej tenis") — `APITENNIS_KEY` v kódu existuje, ale appka ho
aktivně nevyužívá pro produkční tikety.

## Rozpracované / otevřené věci k 2026-07-30

- **API-Football plán "Mega" je POTVRZENĚ koupený** (dřív nejisté, teď
  appka na něm reálně běží — rate limiter 15 req/s, viz sekce mechanismů
  výše).
- Testovací účet `d.voves@seznam.cz` (heslo appka do repa neukládá, viz
  konvence výše) je založený a nahraný 1000 tokeny, aby si na něm mohl
  uživatel sám zkoušet generování ("Jedu naplno. Budu sam generovat.").
  Případný další reset přes `POST /admin/provision-account` s
  `reset=true`.
- Starý Netlify účet (site ID `6039a72d-...`) zůstává nevyřešený úklidový
  item na uživatelově straně — appka ho jen přestala používat, ale nijak
  ho neruší/nemaže.
- Appka má drobnou, neškodnou redundanci: appčiny vlastní cronové účty
  (`DAILY_TICKETS_USER_ID`, `TRANSPARENCY_USER_ID`) se teď settlují 2×
  denně — jednou vlastním krokem uvnitř `run_daily_tickets`, podruhé
  novým globálním `/admin/settle-all-pending`. Nabídnuto zjednodušit,
  uživatel se k tomu zatím nevyjádřil, není potřeba to řešit samo od sebe.
- Debata "co appka může ještě predikovat / na čem appka může ještě
  vydělat" (sportovní i nesportovní nápady) — appka to nechala otevřené,
  uživatel odmítl první nesázkařský nápad (sledování cen) bez náhrady:
  "Ne to se mi nelibi." Žádný závěr, žádný úkol z toho neplyne, dokud
  uživatel neřekne dál.
- Historické BOOST tikety v historii jednotlivých účtů zůstávají
  zobrazené (viz sekce ceníku výše) — nebylo explicitně potvrzené.

## Poslední větší commity (nejnovější nahoře, `main`)

```
7e261e2 Doplnit _require_generation_enabled i do /tickets/replace-selection
20c2e38 Sjednotit /tickets/replace-selection s bezpečnostními pojistkami generate
e99dcf7 Opravit ztrácející se kickoff_time při ukládání + doplnit settlement pro všechny účty
71fa4cd Rozšířit /admin/candidate-pool-preview o rozpad podle typu trhu
bcf76b2 Přidat /admin/create-channel-payment-link — přecenění kanálu na Stripe
0cb8e77 Přecenit kanál na 990 Kč a neomezené generování na 4990 Kč
cb73668 Opravit _last_batch_match_ids — nikdy se nečistil, jen se hromadil
7ece82c Přidat GET /tickets/generate-progress — skutečný postup generování
e1a8bc9 Nefetchovat širší okno předem — jen na neúspěch (rychlejší generování)
c0b90af Opravit _filter_within_days — nešlo obejít zvolené časové okno
8c4c43b Přidat /admin/provision-account — založit/resetovat účet a nahrát tokeny
448466d Přidat /admin/candidate-pool-preview — čistě informativní přehled
5c41cf6 400 zápasů jako strop, který se doplňuje po dnech, ne dělí předem
7053b08 Přeladit generování na Mega plán API-Football (15 req/s, 150k req/den)
a6d9c1b Opravit zobrazení výběrů (raw kódy) a přidat Under gólů jako vlastní trh
ff3b7a9 Zapnout Under góly se skutečným tržním kurzem místo dopočítaného
```

Poznámka k gitu: appka opakovaně narazila na `mergeable_state: dirty` po
squash-mergi ze stejné dlouhověké branch (PR #108, #116 — stejná třída
problému). Fix: `git fetch origin main && git reset --hard origin/main`,
znovu aplikovat jen zamýšlený diff, `git push --force-with-lease`. V této
session appka místo squash-mergovaných PR commituje a pushuje rovnou
(fast-forward) na `main` z pracovní branch, takže tenhle problém
nenastal.

## Frontend (`apexsignal-app`) — poslední živě ověřený stav

Lokální klon appka drží v `/tmp/apexsignal-app` (dočasný, session-specific
— nová konverzace si ho musí znovu naklonovat/ověřit, že tam je). Push na
GitHub appce nejde (viz sekce Repozitáře výše), takže appka na tenhle klon
nemůže spoléhat jako na trvalý zdroj pravdy mezi sessions — jediný trvalý
stav frontendu je to, co je nasazené na Netlify (`apexsignal.cz`).

Této session appka do `index.html` přidala/opravila: `formatSelection()` +
`MARKET_LABELS_CS`/`SELECTION_LABELS_CS` (oprava syrových `market_type`/
`selection` kódů zobrazených místo českého textu), chip "Under gólů" do
"Typ trhu" pickeru (fotbal i hokej), skutečný progress bar generování
(`pollGenerationProgress`, `GET /tickets/generate-progress`) nad starou
dekorativní "Matrix" konzolí, a text neomezeného tarifu na 4 990 Kč.
Nasazeno přes Netlify (`8caab98f-dbc0-4984-921b-846eb71d0c89`), živě
ověřeno funkční (API-level, vizuální/browserové ověření v této session
nešlo — outbound navigace na apexsignal.cz i checkout.stripe.com padala
na `net::ERR_CONNECTION_RESET`, sandboxové omezení téhle session, ne
chyba appky).
