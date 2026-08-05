# Rezervační systém masáží

Samostatná FastAPI appka (vlastní Render služba + vlastní PostgreSQL),
nezávislá na hlavním ApexSignal backendu — bydlí ve stejném repozitáři jen
proto, jak byla založená tahle vývojová větev (`claude/massage-booking-system-ujwmyy`).
Kořenový `render.yaml` a zbytek souborů v kořeni repozitáře appka
nepoužívá ani neupravuje.

## Co appka umí

- Veřejná stránka `/` — klient si vybere masérku, službu a datum, appka mu
  ukáže volné termíny a po vybrání termínu odešle rezervaci.
- Po odeslání appka pošle **SMS s potvrzovacím odkazem** na zadané číslo.
  Rezervace zůstává ve stavu `pending` a termín je REÁLNĚ zamčený, dokud
  klient odkaz neotevře (`GET /confirm/{token}`) — teprve tím se stav
  změní na `confirmed`.
- Pokud klient odkaz neotevře do `BOOKING_CONFIRM_TTL_MINUTES` (výchozí 10
  minut), termín se automaticky uvolní (viz níže "Expirace").
- Admin (chráněno hlavičkou `X-Admin-Key`) spravuje masérky, jejich
  týdenní pracovní dobu, výjimky na konkrétní den (jiná hodina/volno) a
  ruční blokace termínů (dovolená, pauza).
- **Scarcity efekt bez fabrikace:** appka schválně skryje náhodnou část
  volných termínů (konfigurovatelné procento na masérku), aby kalendář
  nepůsobil prázdně. Skryté sloty se tváří jako "obsazeno" BEZ vymyšlené
  identity klienta — appka záměrně NEpřidává falešná jména k neexistujícím
  rezervacím, to by byl klamavý dark pattern (viz diskuze v konverzaci,
  kde vznikla tahle appka).

## Co appka NEumí (zatím) / co je potřeba doplnit

- **SMS gateway (SMSbrana.cz) není v tomhle prostředí otestovaná proti
  živému účtu.** `app/sms.py` implementuje jejich hash-based HTTP API
  podle obecně známého schématu, ale appka si nemohla stáhnout aktuální
  dokumentaci ani nic reálně odeslat. Před ostrým nasazením:
  1. založit účet na smsbrana.cz (nebo zvolit jiný gateway a upravit jen
     `SmsBranaSender` v `app/sms.py`),
  2. nechat `SMS_SENDER_BACKEND=console` a projít si testovací rezervaci
     (appka SMS jen zaloguje, neutratí kredit),
  3. přepnout na `smsbrana`, poslat SMS na vlastní číslo, ověřit doručení
     i přesné parametry API.
- **Reálná jména/pracovní doba masérek** — appka potřebuje od tebe
  seznam masérek + jejich pracovní dobu (přes `/admin/masseuses` a
  `/admin/masseuses/{id}/working-hours`, viz "Admin" níže).
- **Design/branding stránky** — `/` je zatím čistě funkční MVP (tmavé
  pozadí, žádný vlastní vizuál konkrétní značky).

## Nasazení na Render (nová, samostatná služba)

Appka doporučuje ruční založení přes dashboard (jednodušší a bezpečnější
než míchat do stávajícího Blueprintu, který drží živou ApexSignal appku):

1. **Databáze:** Render dashboard → New + → PostgreSQL → libovolný název
   (např. `massage-booking-db`) → zapsat si "Internal Database URL".
2. **Web Service:** Render dashboard → New + → Web Service → připojit
   TENHLE GitHub repozitář (`apexsignal-backend`) → větev
   `claude/massage-booking-system-ujwmyy` (nebo `main`, až se tam appka
   sloučí) → **Root Directory: `massage-booking-system`** (důležité,
   jinak Render nenajde `requirements.txt`/`app/`).
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. **Env vars** u nové webové služby:
   - `DATABASE_URL` — Internal Database URL z kroku 1
   - `ADMIN_TASK_KEY` — vygeneruj: `python3 -c "import secrets; print(secrets.token_hex(32))"`
   - `PUBLIC_BASE_URL` — URL týhle služby, např. `https://massage-booking-system.onrender.com`
     (bez lomítka na konci — appka ji dá do SMS odkazu)
   - `SMS_SENDER_BACKEND=console` na začátek (přepnout na `smsbrana` až po ověření)
   - `SMSBRANA_LOGIN`, `SMSBRANA_PASSWORD` — až budeš mít účet
4. **GitHub Actions expirace:** repo Settings → Secrets and variables →
   Actions:
   - Variable `MASSAGE_BOOKING_BASE_URL` = stejná URL jako `PUBLIC_BASE_URL`
   - Secret `MASSAGE_ADMIN_TASK_KEY` = stejná hodnota jako `ADMIN_TASK_KEY`
   Workflow `.github/workflows/expire-massage-bookings.yml` pak každých
   10 minut uvolní nepotvrzené rezervace po vypršení TTL.

(`render.yaml` v tomhle adresáři je alternativa přes Render Blueprint —
Render ho ale defaultně hledá jen v kořeni repozitáře, takže by chtělo
při zakládání Blueprintu ručně zadat cestu k němu. Ruční postup výše je
spolehlivější.)

## Admin — první nastavení

Všechny admin endpointy vyžadují hlavičku `X-Admin-Key: <ADMIN_TASK_KEY>`.

```bash
BASE=https://massage-booking-system.onrender.com
KEY=... # ADMIN_TASK_KEY

# 1) Založit masérku
curl -X POST $BASE/admin/masseuses -H "X-Admin-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name": "Klára"}'
# -> {"id": 1}

# 2) Nastavit týdenní pracovní dobu (weekday: 0=pondělí .. 6=neděle)
curl -X POST $BASE/admin/masseuses/1/working-hours -H "X-Admin-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"weekday": 0, "start_time": "16:00", "end_time": "22:00"}'

# 3) Výjimka na konkrétní den (jiná hodina, nebo volno)
curl -X POST $BASE/admin/masseuses/1/schedule-overrides -H "X-Admin-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"date": "2026-08-15", "day_off": true}'

# 4) Ruční blokace konkrétního rozsahu (dovolená, pauza)
curl -X POST $BASE/admin/masseuses/1/block -H "X-Admin-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"start_at": "2026-08-10T18:00:00+02:00", "end_at": "2026-08-10T19:00:00+02:00", "reason": "pauza"}'

# 5) Založit službu (typ masáže + délka)
curl -X POST $BASE/admin/services -H "X-Admin-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name": "Klasická masáž", "duration_minutes": 60, "price_kc": 1200}'

# 6) Přehled rezervací
curl $BASE/admin/bookings -H "X-Admin-Key: $KEY"
```

## Lokální vývoj

```bash
cd massage-booking-system
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # doplnit DATABASE_URL na lokální/testovací Postgres
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```
