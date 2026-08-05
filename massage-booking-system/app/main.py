"""
main.py — FastAPI aplikace: veřejná rezervace + admin správa + potvrzovací
odkaz z SMS.

Spuštění (dev):
    pip install -r requirements.txt
    uvicorn app.main:app --reload
"""
from __future__ import annotations

import logging
from datetime import date as Date, timedelta
from typing import Optional

import psycopg2.errors
from fastapi import Depends, FastAPI, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import availability, db, rate_limiter, sms
from .config import settings
from .schemas import (
    AvailabilityResponse,
    BlockedSlotIn,
    BookingCreateRequest,
    BookingCreateResponse,
    MasseuseCreate,
    MasseuseOut,
    ScheduleOverrideIn,
    ServiceCreate,
    ServiceOut,
    SlotOut,
    WorkingHourIn,
)

logger = logging.getLogger("massage_booking")

app = FastAPI(title="Rezervační systém masáží")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


def require_admin(x_admin_key: str = Header(default="", alias="X-Admin-Key")) -> None:
    if not settings.admin_task_key or x_admin_key != settings.admin_task_key:
        raise HTTPException(status_code=403, detail="Neplatný nebo chybějící X-Admin-Key")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ============================================================
# Veřejné endpointy
# ============================================================

@app.get("/public/masseuses", response_model=list[MasseuseOut])
def list_masseuses():
    with db.get_cursor() as cur:
        cur.execute("SELECT id, name FROM masseuses WHERE active = true ORDER BY name")
        return cur.fetchall()


@app.get("/public/services", response_model=list[ServiceOut])
def list_services():
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, duration_minutes, price_kc FROM services "
            "WHERE active = true ORDER BY duration_minutes"
        )
        return cur.fetchall()


@app.get("/public/availability", response_model=AvailabilityResponse)
def get_availability(
    masseuse_id: int = Query(...),
    service_id: int = Query(...),
    date: Date = Query(...),
):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT scarcity_min_ratio, scarcity_max_ratio FROM masseuses WHERE id = %s AND active = true",
            (masseuse_id,),
        )
        masseuse = cur.fetchone()
        if masseuse is None:
            raise HTTPException(status_code=404, detail="Masérka nenalezena")

        cur.execute("SELECT duration_minutes FROM services WHERE id = %s AND active = true", (service_id,))
        service = cur.fetchone()
        if service is None:
            raise HTTPException(status_code=404, detail="Služba nenalezena")

        slots = availability.get_available_slots(
            cur,
            masseuse_id,
            date,
            service["duration_minutes"],
            float(masseuse["scarcity_min_ratio"]),
            float(masseuse["scarcity_max_ratio"]),
        )

    return AvailabilityResponse(
        masseuse_id=masseuse_id,
        date=date,
        service_id=service_id,
        slots=[SlotOut(start=s.start, end=s.end) for s in slots],
    )


@app.post("/public/bookings", response_model=BookingCreateResponse)
def create_booking(payload: BookingCreateRequest, request: Request):
    ip = _client_ip(request)
    if rate_limiter.is_rate_limited(payload.client_phone, ip):
        raise HTTPException(
            status_code=429, detail="Příliš mnoho rezervačních požadavků, zkus to prosím později."
        )

    slot_start = payload.slot_start
    if slot_start.tzinfo is None:
        slot_start = slot_start.replace(tzinfo=availability.TZ)

    if slot_start <= db.now_utc():
        raise HTTPException(status_code=400, detail="Vybraný termín už je v minulosti")

    with db.get_cursor() as cur:
        cur.execute("SELECT id FROM masseuses WHERE id = %s AND active = true", (payload.masseuse_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Masérka nenalezena")

        cur.execute(
            "SELECT duration_minutes FROM services WHERE id = %s AND active = true", (payload.service_id,)
        )
        service = cur.fetchone()
        if service is None:
            raise HTTPException(status_code=404, detail="Služba nenalezena")
        slot_end = slot_start + timedelta(minutes=service["duration_minutes"])

        windows = availability.get_work_windows(cur, payload.masseuse_id, slot_start.date())
        if not availability.slot_within_windows(slot_start.date(), slot_start, slot_end, windows):
            raise HTTPException(status_code=400, detail="Vybraný termín je mimo pracovní dobu masérky")

        busy = availability.get_busy_ranges(cur, payload.masseuse_id, slot_start.date())
        if availability.overlaps(slot_start, slot_end, busy):
            raise HTTPException(status_code=409, detail="Vybraný termín už není volný")

        confirm_token = db.generate_confirm_token()
        expires_at = db.now_utc() + timedelta(minutes=settings.booking_confirm_ttl_minutes)

        try:
            cur.execute(
                "INSERT INTO bookings "
                "(masseuse_id, service_id, slot_start, slot_end, client_name, client_phone, "
                " status, confirm_token, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id",
                (
                    payload.masseuse_id,
                    payload.service_id,
                    slot_start,
                    slot_end,
                    payload.client_name,
                    payload.client_phone,
                    confirm_token,
                    expires_at,
                ),
            )
            booking_id = cur.fetchone()["id"]
        except psycopg2.errors.UniqueViolation:
            raise HTTPException(
                status_code=409, detail="Vybraný termín si právě rezervuje někdo jiný, zkus prosím jiný slot"
            )

    rate_limiter.record_request(payload.client_phone, ip)

    try:
        sms.send_confirmation_sms(payload.client_phone, confirm_token)
    except sms.SmsSendError as exc:
        logger.error("Odeslání SMS selhalo pro booking %s: %s", booking_id, exc)
        with db.get_cursor() as cur:
            cur.execute("DELETE FROM bookings WHERE id = %s AND status = 'pending'", (booking_id,))
        raise HTTPException(status_code=502, detail="Nepodařilo se odeslat potvrzovací SMS, zkus to prosím znovu")

    return BookingCreateResponse(
        booking_id=booking_id,
        status="pending",
        message=f"Zkontroluj SMS a potvrď rezervaci do {settings.booking_confirm_ttl_minutes} minut.",
    )


CONFIRM_PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#111; color:#eee; display:flex;
       align-items:center; justify-content:center; min-height:100vh; margin:0; padding:24px; }}
.card {{ max-width:420px; text-align:center; background:#1b1b1b; border-radius:16px; padding:32px; }}
h1 {{ font-size:1.4rem; margin-bottom:12px; }}
p {{ color:#bbb; line-height:1.5; }}
</style></head><body><div class="card"><h1>{heading}</h1><p>{message}</p></div></body></html>"""


@app.get("/confirm/{token}", response_class=HTMLResponse)
def confirm_booking(token: str):
    with db.get_cursor() as cur:
        cur.execute("SELECT id, status, expires_at FROM bookings WHERE confirm_token = %s", (token,))
        booking = cur.fetchone()

        if booking is None:
            return CONFIRM_PAGE.format(
                title="Neplatný odkaz",
                heading="Odkaz nenalezen",
                message="Tenhle potvrzovací odkaz neexistuje.",
            )

        if booking["status"] == "confirmed":
            return CONFIRM_PAGE.format(
                title="Už potvrzeno",
                heading="Rezervace je už potvrzená",
                message="Tenhle termín je potvrzený, těšíme se na tebe.",
            )

        if booking["status"] != "pending":
            return CONFIRM_PAGE.format(
                title="Neplatná rezervace",
                heading="Rezervace už neplatí",
                message="Tahle rezervace byla zrušena nebo vypršela.",
            )

        if booking["expires_at"] < db.now_utc():
            cur.execute("UPDATE bookings SET status = 'expired' WHERE id = %s", (booking["id"],))
            return CONFIRM_PAGE.format(
                title="Vypršelo",
                heading="Platnost odkazu vypršela",
                message="Termín mezitím vypršel, zkus si prosím zarezervovat nový.",
            )

        cur.execute(
            "UPDATE bookings SET status = 'confirmed', confirmed_at = %s WHERE id = %s",
            (db.now_utc(), booking["id"]),
        )
        return CONFIRM_PAGE.format(
            title="Potvrzeno", heading="Rezervace potvrzena!", message="Těšíme se na tebe v domluvený čas."
        )


# ============================================================
# Admin endpointy (X-Admin-Key)
# ============================================================

@app.post("/admin/masseuses")
def admin_create_masseuse(payload: MasseuseCreate, _: None = Depends(require_admin)):
    min_ratio = payload.scarcity_min_ratio if payload.scarcity_min_ratio is not None else settings.scarcity_min_ratio
    max_ratio = payload.scarcity_max_ratio if payload.scarcity_max_ratio is not None else settings.scarcity_max_ratio
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO masseuses (name, phone, scarcity_min_ratio, scarcity_max_ratio) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (payload.name, payload.phone, min_ratio, max_ratio),
        )
        return {"id": cur.fetchone()["id"]}


@app.get("/admin/masseuses")
def admin_list_masseuses(_: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, name, phone, active, scarcity_min_ratio, scarcity_max_ratio "
            "FROM masseuses ORDER BY name"
        )
        return cur.fetchall()


@app.patch("/admin/masseuses/{masseuse_id}")
def admin_update_masseuse(
    masseuse_id: int,
    active: Optional[bool] = None,
    phone: Optional[str] = None,
    scarcity_min_ratio: Optional[float] = None,
    scarcity_max_ratio: Optional[float] = None,
    _: None = Depends(require_admin),
):
    updates = {
        "active": active,
        "phone": phone,
        "scarcity_min_ratio": scarcity_min_ratio,
        "scarcity_max_ratio": scarcity_max_ratio,
    }
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Nic k aktualizaci")

    set_clause = ", ".join(f"{key} = %s" for key in updates)
    with db.get_cursor() as cur:
        cur.execute(
            f"UPDATE masseuses SET {set_clause} WHERE id = %s RETURNING id",
            (*updates.values(), masseuse_id),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Masérka nenalezena")
    return {"ok": True}


@app.post("/admin/masseuses/{masseuse_id}/working-hours")
def admin_add_working_hour(masseuse_id: int, payload: WorkingHourIn, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO working_hours (masseuse_id, weekday, start_time, end_time) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (masseuse_id, payload.weekday, payload.start_time, payload.end_time),
        )
        return {"id": cur.fetchone()["id"]}


@app.get("/admin/masseuses/{masseuse_id}/working-hours")
def admin_list_working_hours(masseuse_id: int, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT id, weekday, start_time, end_time FROM working_hours "
            "WHERE masseuse_id = %s ORDER BY weekday, start_time",
            (masseuse_id,),
        )
        return cur.fetchall()


@app.delete("/admin/working-hours/{working_hour_id}")
def admin_delete_working_hour(working_hour_id: int, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM working_hours WHERE id = %s RETURNING id", (working_hour_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Záznam nenalezen")
    return {"ok": True}


@app.post("/admin/masseuses/{masseuse_id}/schedule-overrides")
def admin_upsert_schedule_override(masseuse_id: int, payload: ScheduleOverrideIn, _: None = Depends(require_admin)):
    if not payload.day_off and (not payload.start_time or not payload.end_time):
        raise HTTPException(status_code=400, detail="U pracovního dne musí být start_time i end_time")
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO schedule_overrides (masseuse_id, date, day_off, start_time, end_time) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (masseuse_id, date) DO UPDATE SET "
            "day_off = EXCLUDED.day_off, start_time = EXCLUDED.start_time, end_time = EXCLUDED.end_time "
            "RETURNING id",
            (masseuse_id, payload.date, payload.day_off, payload.start_time, payload.end_time),
        )
        return {"id": cur.fetchone()["id"]}


@app.post("/admin/masseuses/{masseuse_id}/block")
def admin_block_slot(masseuse_id: int, payload: BlockedSlotIn, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO blocked_slots (masseuse_id, start_at, end_at, reason) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (masseuse_id, payload.start_at, payload.end_at, payload.reason),
        )
        return {"id": cur.fetchone()["id"]}


@app.delete("/admin/blocked-slots/{blocked_slot_id}")
def admin_unblock_slot(blocked_slot_id: int, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute("DELETE FROM blocked_slots WHERE id = %s RETURNING id", (blocked_slot_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Blokace nenalezena")
    return {"ok": True}


@app.post("/admin/services")
def admin_create_service(payload: ServiceCreate, _: None = Depends(require_admin)):
    with db.get_cursor() as cur:
        cur.execute(
            "INSERT INTO services (name, duration_minutes, price_kc) VALUES (%s, %s, %s) RETURNING id",
            (payload.name, payload.duration_minutes, payload.price_kc),
        )
        return {"id": cur.fetchone()["id"]}


@app.get("/admin/bookings")
def admin_list_bookings(
    status: Optional[str] = None,
    date: Optional[Date] = None,
    _: None = Depends(require_admin),
):
    query = (
        "SELECT b.id, b.masseuse_id, m.name AS masseuse_name, b.service_id, b.slot_start, b.slot_end, "
        "b.client_name, b.client_phone, b.status, b.created_at, b.expires_at, b.confirmed_at "
        "FROM bookings b JOIN masseuses m ON m.id = b.masseuse_id WHERE 1=1"
    )
    params: list = []
    if status:
        query += " AND b.status = %s"
        params.append(status)
    if date:
        query += " AND b.slot_start::date = %s"
        params.append(date)
    query += " ORDER BY b.slot_start"

    with db.get_cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


@app.post("/admin/expire-pending")
def admin_expire_pending(_: None = Depends(require_admin)):
    """Appka nepotvrzené (pending) rezervace po vypršení TTL uvolní — volá se
    periodicky přes GitHub Actions (viz .github/workflows/expire-massage-bookings.yml)."""
    with db.get_cursor() as cur:
        cur.execute(
            "UPDATE bookings SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at < now() RETURNING id"
        )
        expired_ids = [row["id"] for row in cur.fetchall()]
    return {"expired_count": len(expired_ids), "expired_ids": expired_ids}


# ============================================================
# Minimální veřejná rezervační stránka (bez buildu, vanilla JS)
# ============================================================

BOOKING_PAGE_HTML = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rezervace masáže</title>
<style>
:root { color-scheme: dark; }
body { font-family: system-ui, sans-serif; background:#111; color:#eee; margin:0; padding:24px; }
.wrap { max-width:480px; margin:0 auto; }
h1 { font-size:1.3rem; }
label { display:block; margin-top:16px; font-size:0.9rem; color:#bbb; }
select, input, button { width:100%; padding:10px; margin-top:6px; border-radius:8px;
  border:1px solid #333; background:#1b1b1b; color:#eee; font-size:1rem; box-sizing:border-box; }
.slots { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
.slot { padding:8px 12px; border-radius:8px; border:1px solid #333; cursor:pointer; background:#1b1b1b; }
.slot.selected { background:#5b3; color:#111; border-color:#5b3; }
button { margin-top:20px; background:#5b3; color:#111; font-weight:600; border:none; cursor:pointer; }
button:disabled { opacity:0.5; cursor:default; }
#msg { margin-top:16px; color:#9c9; }
#err { margin-top:16px; color:#e66; }
</style></head><body><div class="wrap">
<h1>Rezervace termínu</h1>

<label>Masérka</label>
<select id="masseuse"></select>

<label>Služba</label>
<select id="service"></select>

<label>Datum</label>
<input type="date" id="date">

<label>Volné termíny</label>
<div class="slots" id="slots"></div>

<label>Jméno (nepovinné)</label>
<input type="text" id="clientName">

<label>Telefon (české číslo)</label>
<input type="tel" id="clientPhone" placeholder="+420 777 123 456">

<button id="submitBtn" disabled>Odeslat rezervaci</button>
<div id="msg"></div>
<div id="err"></div>
</div>
<script>
let selectedSlot = null;

async function loadOptions() {
  const [masseuses, services] = await Promise.all([
    fetch('/public/masseuses').then(r => r.json()),
    fetch('/public/services').then(r => r.json()),
  ]);
  const masseuseSel = document.getElementById('masseuse');
  masseuseSel.innerHTML = masseuses.map(m => `<option value="${m.id}">${m.name}</option>`).join('');
  const serviceSel = document.getElementById('service');
  serviceSel.innerHTML = services.map(s => `<option value="${s.id}">${s.name} (${s.duration_minutes} min)</option>`).join('');
  document.getElementById('date').value = new Date().toISOString().slice(0, 10);
  await loadSlots();
}

async function loadSlots() {
  selectedSlot = null;
  document.getElementById('submitBtn').disabled = true;
  const masseuseId = document.getElementById('masseuse').value;
  const serviceId = document.getElementById('service').value;
  const date = document.getElementById('date').value;
  if (!masseuseId || !serviceId || !date) return;
  const res = await fetch(`/public/availability?masseuse_id=${masseuseId}&service_id=${serviceId}&date=${date}`);
  const data = await res.json();
  const box = document.getElementById('slots');
  if (!data.slots.length) {
    box.innerHTML = '<span style="color:#888">Žádný volný termín tenhle den.</span>';
    return;
  }
  box.innerHTML = data.slots.map(s => {
    const time = new Date(s.start).toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' });
    return `<div class="slot" data-start="${s.start}">${time}</div>`;
  }).join('');
  box.querySelectorAll('.slot').forEach(el => el.addEventListener('click', () => {
    box.querySelectorAll('.slot').forEach(x => x.classList.remove('selected'));
    el.classList.add('selected');
    selectedSlot = el.dataset.start;
    document.getElementById('submitBtn').disabled = false;
  }));
}

document.getElementById('masseuse').addEventListener('change', loadSlots);
document.getElementById('service').addEventListener('change', loadSlots);
document.getElementById('date').addEventListener('change', loadSlots);

document.getElementById('submitBtn').addEventListener('click', async () => {
  const errBox = document.getElementById('err');
  const msgBox = document.getElementById('msg');
  errBox.textContent = '';
  msgBox.textContent = '';
  if (!selectedSlot) return;
  const payload = {
    masseuse_id: parseInt(document.getElementById('masseuse').value, 10),
    service_id: parseInt(document.getElementById('service').value, 10),
    slot_start: selectedSlot,
    client_name: document.getElementById('clientName').value || null,
    client_phone: document.getElementById('clientPhone').value,
  };
  document.getElementById('submitBtn').disabled = true;
  try {
    const res = await fetch('/public/bookings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      errBox.textContent = data.detail || 'Rezervaci se nepodařilo vytvořit.';
      document.getElementById('submitBtn').disabled = false;
      return;
    }
    msgBox.textContent = data.message;
    await loadSlots();
  } catch (e) {
    errBox.textContent = 'Chyba spojení, zkus to prosím znovu.';
    document.getElementById('submitBtn').disabled = false;
  }
});

loadOptions();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def booking_page():
    return BOOKING_PAGE_HTML
