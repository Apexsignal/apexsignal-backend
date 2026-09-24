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
   branch se mění podle toho, co appce zadá orchestrující systém na
   začátku session (aktuálně `claude/app-purpose-question-kq24hv`, dřív
   např. `claude/navazani-na-konverzaci-aikq9n`) — appka na ni commituje,
   pak fast-forward pushne i do `main` (obě branch se vždy drží ve
   stejném stavu). **Nespoléhat na konkrétní jméno zapsané tady, řídit se
   tím, co appka dostane na začátku aktuální session.**
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
   Netlify** (NE přes GitHub push). **Aktuální, ověřený stav k 2026-08-18:**
   MCP nástroj `mcp__...__netlify-deploy-services-updater` v této session
   nebyl dostupný vůbec (nenašel ho ani `ToolSearch`) — nová konverzace
   ať nejdřív zkusí jeho existenci ověřit, ale pokud chybí, nezdržovat se
   a jít rovnou na fallback: **Netlify CLI přes `npx netlify-cli deploy`**
   s `NETLIFY_AUTH_TOKEN` v env a `--site <ID>`. Token appka nemá uložený
   trvale nikde (uživatel ho vygeneruje na
   `https://app.netlify.com/user/applications#personal-access-tokens`
   a pošle v chatu, až bude appka nasazovat) — cokoliv uloženého ve
   scratchpadu z dřívějška je nespolehlivé, ověřit vždycky přes
   `GET https://api.netlify.com/api/v1/sites` s tím tokenem, jestli mezi
   vrácenými sites vůbec `apexsignalapp`/`apexsignal.cz` je, NEŘÍDIT SE
   slepě starým ID v tomhle souboru.
   **Skutečný, živě ověřený site (2026-08-18): jméno `apexsignalapp`,
   site ID `4a1b79c9-f2ca-4a57-a5ff-df02c2c6bc57`, custom doména
   `apexsignal.cz`.** Všechna dřívější ID v tomhle souboru
   (`8caab98f-dbc0-4984-921b-846eb71d0c89`, i starší `6039a72d-...`)
   jsou zastaralá/neplatná — appka je nekontrolovala živě přes API a jen
   je opisovala z minulé session. Účet appka nezjišťovala (token se
   ověřuje sám, ne přes e-mail), takže `apexsignal03@seznam.cz` neber
   jako jistotu, jen jako poslední známý odhad.

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
- **`get_all_saved_match_ids` dřív vylučovalo z nabízení jen zápasy z
  tiketů se `status='pending'`** — ale appka tiket označí `lost` hned,
  jakmile prohraje JEDNA noha (parlay), i když ostatní nohy ještě vůbec
  neproběhly. Takový tiket appka z výluky ztratila a jeho ještě
  neodehraný zápas mohla nabídnout do DALŠÍHO, samostatného tiketu —
  appka tak "vsadila" na stejný reálný zápas dvakrát, což by appka
  v realitě nikdy neudělala (druhá sázka na tiket, co ještě neskončil).
  **Skutečný incident 2026-09-24 na `test2@test.cz` (=`TRANSPARENCY_USER_ID`,
  veřejná stránka `/transparentni-ucet`):** appka takhle omylem duplicitně
  vsadila na 25 různých zápasů napříč 22 tikety, což zkreslovalo veřejnou
  statistiku (61,5 % úspěšnost místo skutečných 66,7 % po opravě). Opraveno
  — filtr teď běží podle toho, jestli je KONKRÉTNÍ noha ještě
  nerozhodnutá (`result='pending'`), ne podle stavu celého tiketu
  (`1ad0d3c`). Těch 22 duplicitních tiketů appka na uživatelův pokyn
  (s heslem) smazala přes nový `POST /admin/delete-tickets` (admin-key,
  bere explicitní seznam `ticket_ids` — appka ho nechala v kódu jako
  trvalý nástroj pro podobný jednorázový úklid příště, ne jednorázový
  hack). **Pokud se na jiném účtu (hlavně TRANSPARENCY_USER_ID/
  DAILY_TICKETS_USER_ID) znovu objeví tikety se stejným `match_id`
  vícekrát — to už by se PO téhle opravě nemělo dít u NOVĚ generovaných
  tiketů, ale staré/historické případy mohly appce uniknout, protože
  appka kontrolovala jen test2.**

## Stojící pravidla od uživatele (nezapomenout)

- **BEZPEČNOSTNÍ POJISTKA (zavedeno 2026-09-24, po incidentu — appka
  omylem smazala VŠECHNY proměnné prostředí na Renderu hromadným PUT
  příkazem při testování Instagram OAuth, appka appku appky appku appku
  na několik minut úplně shodila).** Než appka udělá COKOLIV z tohohle:
  - zápis/mazání proměnných prostředí (env vars) na Renderu nebo jinde,
  - mazání souborů/složek mimo appčin scratchpad,
  - jakoukoliv hromadnou/destruktivní operaci (bulk PUT, force push,
    DROP, apod.),
  - manipulaci s API klíči/hesly/tajnými hodnotami,

  appka se MUSÍ nejdřív zeptat a počkat na **heslo appky appka** —
  konkrétní heslo appka appce NEZAPISUJE sem do repozitáře (uživatel to
  výslovně řekl v chatu, appka si ho pamatuje jen v rámci konverzace,
  ne natrvalo v kódu). Appka zápis/mazání BEZ tohohle hesla neprovede,
  ani při jasném příkazu na obsah — tohle pravidlo stojí NAD běžnou
  konzultační výjimkou níž. Test/čtecí (GET, read-only) volání appka
  dál smí dělat volně, jen ZÁPISY/MAZÁNÍ appka gatuje heslem.
- **"Nejdriv semnou konzultuj vzdy nez neco budes menit!!!"** — vždy
  nejdřív konzultovat, než se něco změní. (V praxi: pokud přijde jasný,
  přímý příkaz na OBSAH změny, ten už JE konzultace/schválení pro tu
  změnu samotnou — neptat se znovu na to samé. ALE i po jasném příkazu
  na obsah appka počká na výslovné potvrzení PŘED samotným nasazením
  (push na `main`, Netlify deploy) — uživatel to 2026-07-31 explicitně
  upřesnil: "priste se semnou porad nez to nasadis". Připravit/otestovat
  změnu jde rovnou, publikovat ji naživo ne bez potvrzení.)
  **Znovu potvrzeno 2026-09-24:** appka tohle sama porušila u dvou
  drobných výkonnostních oprav (`gc.collect()` v cronu, sdílený
  OddsAPIProvider) — nasadila je rovnou na základě obecného "najdi vše
  a oprav to" / otázky "nedá se to ještě zlepšit?", bez samostatného
  "mám to i nasadit?" těsně před pushem. Uživatel to označil za chybu
  a explicitně potvrdil, že se appka má PTÁT ZNOVU těsně před každým
  pushem na `main`/nasazením, i když už předtím dostala jasné svolení
  k opravě samotné — příprava a nasazení jsou dvě oddělené věci, co
  appka nesmí slučovat do jednoho souhlasu.
- **SportBreak.cz nahrávání tiketů musí zůstat manuální, human-in-the-loop.**
  Uživatel explicitně: "Automaticky to nejde protoze ja davam realne sazky
  na tipsportu." — appka nesmí sama automatizovat nahrávání reálných sázek,
  jen na pokyn a s konkrétními zápasy, co uživatel vloží.
- Komunikace vždy česky.
- Piš jen commentáře, kde je fakt potřeba vysvětlit PROČ (skrytá invariant,
  workaround), ne CO kód dělá.
- **ZRUŠENO 2026-08-26 — GitHub Actions denní cron (`daily-tickets.yml`)
  už NENÍ plně automatizovaný.** Uživatel dřív výslovně chtěl "nech to
  plně automatizované", ale 2026-08-26 to obrátil: "Ano pridej schvalovani
  mnou! Kazdy tiket! Chci videt driv nez se posle." Cron teď dnešní tiket
  jen vygeneruje a pošle appce Telegram upozornění, že čeká na schválení
  — samotné odeslání (`/admin/client-tickets-send`) appka VYNUCUJE jen po
  ručním schválení na `/admin-prodejci` (nová karta "Dnešní tiket",
  tlačítko "Schválit a odeslat"; backend to hlídá přes
  `/admin/daily-ticket/approve` vázané na konkrétní `ticket_id` a dnešní
  datum — schválení platí jen pro ten jeden den, ne napořád). Pokud
  uživatel řekne "vrať to zpátky na automatiku", jde jen o smazání kroku
  z `client_tickets_send` a přidání zpátky kroku do workflow.

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
| Netlify (frontend deploy) | MCP nástroj (pokud dostupný) NEBO Netlify CLI + Personal Access Token, co pošle uživatel v chatu (`https://app.netlify.com/user/applications#personal-access-tokens`) | site `apexsignalapp`, ID `4a1b79c9-f2ca-4a57-a5ff-df02c2c6bc57` (`apexsignal.cz`) — **ověřeno živě 2026-08-18 přes `GET /api/v1/sites`, VŽDY takhle ověřit znovu, nespoléhat na ID zapsaná v historii tohoto souboru** |
| **SportBreak.cz** (ruční nahrávání reálných tiketů) | appka login zná: `apexsignal02@seznam.cz` — **heslo appka do repa neukládá**, sdělí ho uživatel přímo v chatu nové konverzaci, až ho bude appka potřebovat | — |
| OddsPapi (třetí zdroj kurzů) | oddspapi.io dashboard → Render | `ODDSPAPI_KEY` (free plán, 250 req/měsíc — appka umí ověřit zůstatek přes `GET /admin/test-oddspapi`) |
| Python verze buildu (Render appku bez tohohle defaultuje na nejnovější Python, což appce naposledy rozbilo build — viz incident 2026-09-24) | `render.yaml` (literální hodnota, NE `sync: false`) → Render | `PYTHON_VERSION` (aktuálně `3.12.4`) |
| Instagram OAuth (appka si staví vlastní obchozí flow, viz `/admin/instagram/oauth-start`) | developers.facebook.com → Render | `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET` |
| Render API token (appka s ním umí zapisovat JEDNOTLIVÉ env vars, spouštět deploy, číst memory metriky) | Render dashboard → avatar vpravo nahoře → Account Settings → API Keys → Create API Key — appka ho NEUKLÁDÁ natrvalo nikde, dostane ho v chatu, až ho bude potřebovat | NENÍ to env var appky samotné appky — je to jiný typ tokenu (Render account-level, appka ho použije jen když ho uživatel pošle v chatu) |

Pozn.: appka je "fotbal only" byznys rozhodnutí (uživatel: "Ne bude jen
fotbal žadnej tenis") — `APITENNIS_KEY` v kódu existuje, ale appka ho
aktivně nevyužívá pro produkční tikety.

### Živě ověřený kontrolní seznam proměnných prostředí (2026-09-24, 15:40)

Appka si POUZE JMÉNA (bez hodnot!) živě stáhla přes Render API
(`GET /v1/services/{id}/env-vars`) — přesně tohle appka chtěla mít
zapsané POTÉ, co jí incident z rána (hromadné smazání všech env vars)
stál celé dopoledne rekonstrukce. Když se něco podobného stane znovu,
nová konverzace TOHLE porovná se skutečným stavem na Renderu (stejným
voláním) — chybějící klíč pozná OKAMŽITĚ, bez hádání, co vůbec mělo
existovat:

```
ADMIN_TASK_KEY, ANTHROPIC_API_KEY, APISPORTS_KEY, DAILY_TICKETS_USER_ID,
DATABASE_URL, FRONTEND_URL, GOOGLE_CLIENT_ID, INSTAGRAM_APP_ID,
INSTAGRAM_APP_SECRET, ODDSAPI_KEY, ODDSPAPI_KEY, PYTHON_VERSION,
SECRET_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN,
TELEGRAM_BOT_USERNAME, TELEGRAM_CHAT_ID, TELEGRAM_CHAT_ID_WIFE,
TELEGRAM_WEBHOOK_SECRET, TEST3_USER_ID, TRANSPARENCY_USER_ID
```

(22 proměnných — `APITENNIS_KEY` a `STRIPE_CHANNEL_PAYMENT_LINK_URL`
appka záměrně nemá nastavené, viz poznámky výš, to není chyba/mezera.)

### Poslední ověřeně fungující stav appky (záloha, 2026-09-24 15:40)

Uživatel po ránu s incidentem env proměnných výslovně požádal appku
o zálohu fungujícího stavu ("Tohle uz nechci nikdy zazit"). Appka nemá
kam nahrát binární zálohu celé appky (jen appčin GitHub repo drží kód),
takže appka jako zálohu uložila **přesný commit SHA** posledního stavu,
co appka OSOBNĚ živě ověřila v produkci — na téhle verzi appka nechala
uživatele vygenerovat a uložit skutečný tiket, žádná simulace:

**`df4323b` (`df4323bc71655cf4968ed0ed0fb4a6c26915c7d1`) na `main`.**

Návrat k tomuhle stavu (appka tím vrátí POUZE kód — env proměnné a DB
appka tímhle NEOPRAVÍ, jen kód appky):

```
git fetch origin main
git reset --hard df4323b
git push origin main --force-with-lease
```

**Appka tohle NEPOUŽIJE bez hesla appky (force push = hromadná/destruktivní
operace, viz bezpečnostní pojistka výš) — a jen pro doopravdy havarijní
situaci, ne jako rutinu.**

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
1ad0d3c Opravit vylučování rozehraných zápasů + přidat /admin/delete-tickets
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

Dřívější session do `index.html` přidala/opravila: `formatSelection()` +
`MARKET_LABELS_CS`/`SELECTION_LABELS_CS` (oprava syrových `market_type`/
`selection` kódů zobrazených místo českého textu), chip "Under gólů" do
"Typ trhu" pickeru (fotbal i hokej), skutečný progress bar generování
(`pollGenerationProgress`, `GET /tickets/generate-progress`) nad starou
dekorativní "Matrix" konzolí, a text neomezeného tarifu na 4 990 Kč.

**Session 2026-08-18 — CZ/EN/RU přepínač jazyků (appka i landing i právní
stránky):** appka měla z dřívějška rozdělaný CZ/EN přepínač (běží přes
globální monkey-patch `React.createElement`, který každý stringový
child/`placeholder`/`title`/`alt` prohání přes `translateText()` —
najde ho ve slovníku `TRANSLATIONS_CS_EN`/`TRANSLATIONS_CS_RU`, nebo
zkusí `translateDynamic`/`translateDynamicRu` s regexy pro texty
s proměnnými čísly/procenty). appka doplnila `TRANSLATIONS_CS_RU`
(stejných 347 klíčů jako EN verze, jen ruské hodnoty),
`translateDynamicRu` (stejných ~40 pravidel), `SELECTION_LABELS_RU`/
`MARKET_LABELS_RU` pro `formatSelection()`, třetí tlačítko "Русский"
do přepínače v nastavení účtu (ozubené kolečko) a přepočet Kč→EUR
(`CZK_EUR_RATE=24.5`, funkce `czkToEurLabel()`) u cenových textů v EN
i RU verzi (např. "990 Kč" → "990 крон (~40 €)"). `/privacy` a `/terms`
appka zjistila, že NEJSOU součástí React appky (žádný `TRANSLATIONS_CS_*`
klíč pro jejich plný text v `index.html`) — jsou to samostatné statické
soubory (`privacy.html`, `terms.html` v kořeni Netlify site), appka jim
proto udělala VLASTNÍ, nezávislý CZ/EN/RU přepínač (čistý JS,
`display:none`/`active` třídy, `localStorage["apexsignal_lang"]` sdílený
klíč s appkou, ale žádná společná logika). Před nasazením appka
otestovala `translateText`/`formatSelection`/`czkToEurLabel` izolovaně
v Node (mimo prohlížeč, se stubovaným `localStorage`) a ověřila syntaxi
přes `node --check` — vizuální/browserové ověření v appce zase nešlo
(stejné síťové omezení jako v předchozích sessions).

**Nasazení a objevená chyba v tomhle souboru:** appka zjistila, že MCP
nástroj `netlify-deploy-services-updater` z dřívějška v týhle session
vůbec není dostupný, a uložený `NETLIFY_TOKEN` ve scratchpadu patřil
k jinému (neplatnému) účtu — `deploy --site 8caab98f-...` padalo na
"Project not found". Uživatel vygeneroval nový Personal Access Token
přímo na Netlify, appka jím zavolala `GET /api/v1/sites` a zjistila
skutečný, živý site: **jméno `apexsignalapp`, ID
`4a1b79c9-f2ca-4a57-a5ff-df02c2c6bc57`, doména `apexsignal.cz`** — ID
`8caab98f-dbc0-4984-921b-846eb71d0c89` zapsané v tomhle souboru z minulé
session bylo od začátku špatně/zastaralé. Nasazeno přes
`npx netlify-cli deploy --site 4a1b79c9-f2ca-4a57-a5ff-df02c2c6bc57 --prod`,
živě ověřeno přes `curl` (přítomnost `TRANSLATIONS_CS_RU` na `/`,
"Русский" na `/privacy` i `/terms`, všechny tři routy vrací 200).
