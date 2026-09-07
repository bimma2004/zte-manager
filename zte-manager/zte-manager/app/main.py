from __future__ import annotations

import asyncio
import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Query, HTTPException, Body
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_

from app.config import load_config, OltConfig
from app.database import make_engine, make_session_factory
from app.models import Onu, PollLog, Olt
from app.poller import poll_all, poll_one_olt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

config = load_config()
engine = make_engine(config.db_path)
SessionFactory = make_session_factory(engine)

app = FastAPI(title="ZTE Manager")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

scheduler = AsyncIOScheduler(timezone=config.timezone)


def _seed_olts_from_config_if_empty():
    """
    Kalau tabel `olts` masih kosong dan config.yaml punya daftar OLT (cara
    lama), pindahkan jadi baris DB sekali di awal. Setelah ini, penambahan
    OLT dilakukan lewat UI/API, bukan edit config.yaml lagi.
    """
    session = SessionFactory()
    try:
        if session.query(Olt).count() > 0:
            return
        for o in config.olts:
            session.add(
                Olt(
                    name=o.name,
                    host=o.host,
                    snmp_community=o.snmp_community,
                    snmp_version=o.snmp_version,
                    profile=o.profile,
                    boards=",".join(str(b) for b in o.boards),
                    pons_per_board=o.pons_per_board,
                )
            )
        session.commit()
    finally:
        session.close()


def _olt_row_to_config(row: Olt) -> OltConfig:
    boards = [int(b) for b in row.boards.split(",") if b.strip().isdigit()]
    return OltConfig(
        id=str(row.id),
        name=row.name,
        host=row.host,
        snmp_community=row.snmp_community,
        snmp_version=row.snmp_version,
        profile=row.profile,
        boards=boards,
        pons_per_board=row.pons_per_board,
    )


def _get_all_olt_configs() -> list[OltConfig]:
    session = SessionFactory()
    try:
        rows = session.query(Olt).all()
        return [_olt_row_to_config(r) for r in rows]
    finally:
        session.close()


async def scheduled_poll():
    """Dipanggil scheduler tiap interval - ambil daftar OLT FRESH dari DB."""
    olts = _get_all_olt_configs()
    await poll_all(olts, config.oid_profiles, SessionFactory)


@app.on_event("startup")
async def on_startup():
    _seed_olts_from_config_if_empty()

    # Poll sekali saat startup biar cache langsung terisi, lalu jadwalkan berkala.
    asyncio.create_task(scheduled_poll())
    scheduler.add_job(
        scheduled_poll,
        "interval",
        seconds=config.poll_interval_seconds,
        id="poll_all",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = SessionFactory()
    try:
        olt_rows = session.query(Olt).order_by(Olt.name).all()
        last_polls = (
            session.query(PollLog).order_by(PollLog.started_at.desc()).limit(len(olt_rows) or 1).all()
        )
        total_onu = session.query(Onu).count()
        online_onu = session.query(Onu).filter(Onu.is_online.is_(True)).count()
    finally:
        session.close()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "olts": olt_rows,
            "last_polls": last_polls,
            "total_onu": total_onu,
            "online_onu": online_onu,
        },
    )


@app.get("/api/search")
async def search(q: str = Query("", min_length=0), olt_id: str = Query("")):
    """
    Cari pelanggan berdasarkan nama/serial number/deskripsi.
    Dibaca langsung dari cache DB -> instan, tidak nunggu SNMP.
    """
    session = SessionFactory()
    try:
        query = session.query(Onu)
        if olt_id:
            query = query.filter(Onu.olt_id == olt_id)
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(
                    Onu.name.ilike(like),
                    Onu.serial_number.ilike(like),
                    Onu.description.ilike(like),
                )
            )
        rows = query.order_by(Onu.name).limit(200).all()
        return [_serialize(r) for r in rows]
    finally:
        session.close()


@app.get("/api/onu/{onu_db_id}")
async def onu_detail(onu_db_id: int):
    session = SessionFactory()
    try:
        row = session.query(Onu).filter(Onu.id == onu_db_id).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="ONU tidak ditemukan")
        return _serialize(row)
    finally:
        session.close()


@app.get("/api/profiles")
async def list_profiles():
    """Daftar OID profile yang tersedia di config.yaml, dipakai buat dropdown form Tambah OLT."""
    return [
        {"key": key, "label": p.label} for key, p in config.oid_profiles.items()
    ]


@app.get("/api/olts")
async def list_olts():
    session = SessionFactory()
    try:
        rows = session.query(Olt).order_by(Olt.name).all()
        return [_serialize_olt(r) for r in rows]
    finally:
        session.close()


@app.post("/api/olts")
async def add_olt(payload: dict = Body(...)):
    """
    Tambah OLT baru lewat UI. Body JSON:
    { "name", "host", "snmp_community", "snmp_version", "profile", "boards", "pons_per_board" }
    boards boleh string "1,2,3,4" atau list [1,2,3,4].
    """
    name = str(payload.get("name") or "").strip()
    host = str(payload.get("host") or "").strip()
    community = str(payload.get("snmp_community") or "").strip()
    profile = str(payload.get("profile") or "").strip()

    if not name or not host or not community or not profile:
        raise HTTPException(status_code=400, detail="name, host, snmp_community, dan profile wajib diisi")

    if profile not in config.oid_profiles:
        raise HTTPException(
            status_code=400,
            detail=f"Profile '{profile}' tidak dikenal. Pilihan: {list(config.oid_profiles.keys())}",
        )

    boards_raw = payload.get("boards", "")
    if isinstance(boards_raw, list):
        boards_str = ",".join(str(int(b)) for b in boards_raw)
    else:
        boards_str = ",".join(
            p.strip() for p in str(boards_raw).split(",") if p.strip().isdigit()
        )

    version = str(payload.get("snmp_version") or "2c")
    pons_per_board = int(payload.get("pons_per_board") or 16)

    session = SessionFactory()
    try:
        row = Olt(
            name=name,
            host=host,
            snmp_community=community,
            snmp_version=version,
            profile=profile,
            boards=boards_str,
            pons_per_board=pons_per_board,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        new_id = row.id
        serialized = _serialize_olt(row)
    finally:
        session.close()

    # Langsung poll OLT baru di background biar datanya cepat muncul,
    # gak perlu nunggu siklus jadwal berikutnya.
    async def _poll_new():
        cfg = _olt_row_to_config_by_id(new_id)
        if cfg is None:
            return
        oid_profile = config.oid_profiles.get(cfg.profile)
        if oid_profile is None:
            return
        try:
            await poll_one_olt(cfg, oid_profile, SessionFactory)
        except Exception:  # noqa: BLE001
            log.exception("Poll awal OLT baru %s gagal", new_id)

    asyncio.create_task(_poll_new())

    return serialized


def _olt_row_to_config_by_id(olt_db_id: int) -> OltConfig | None:
    session = SessionFactory()
    try:
        row = session.query(Olt).filter(Olt.id == olt_db_id).one_or_none()
        return _olt_row_to_config(row) if row else None
    finally:
        session.close()


@app.delete("/api/olts/{olt_db_id}")
async def delete_olt(olt_db_id: int):
    """Hapus OLT dari daftar pantau, sekalian bersihkan cache ONU-nya."""
    session = SessionFactory()
    try:
        row = session.query(Olt).filter(Olt.id == olt_db_id).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="OLT tidak ditemukan")
        session.query(Onu).filter(Onu.olt_id == str(olt_db_id)).delete()
        session.delete(row)
        session.commit()
        return {"deleted": True, "id": olt_db_id}
    finally:
        session.close()


@app.post("/api/olt/{olt_db_id}/refresh")
async def refresh_olt(olt_db_id: str):
    """Trigger polling manual untuk satu OLT (kalau perlu data paling baru sekarang juga)."""
    cfg = _olt_row_to_config_by_id(int(olt_db_id))
    if cfg is None:
        raise HTTPException(status_code=404, detail="OLT tidak ditemukan")
    profile = config.oid_profiles.get(cfg.profile)
    if profile is None:
        raise HTTPException(status_code=500, detail=f"Profile '{cfg.profile}' tidak ada di config")

    count = await poll_one_olt(cfg, profile, SessionFactory)
    return {"olt_id": olt_db_id, "onu_count": count, "polled_at": dt.datetime.utcnow().isoformat()}


def _serialize(row: Onu) -> dict:
    return {
        "id": row.id,
        "olt_id": row.olt_id,
        "olt_name": row.olt_name,
        "board": row.board,
        "pon": row.pon,
        "onu_id": row.onu_id,
        "name": row.name,
        "description": row.description,
        "serial_number": row.serial_number,
        "onu_type": row.onu_type,
        "rx_power_dbm": row.rx_power_dbm,
        "distance_m": row.distance_m,
        "is_online": row.is_online,
        "status_raw": row.status_raw,
        "last_polled_at": row.last_polled_at.isoformat() if row.last_polled_at else None,
    }


def _serialize_olt(row: Olt) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "host": row.host,
        "snmp_version": row.snmp_version,
        "profile": row.profile,
        "boards": row.boards,
        "pons_per_board": row.pons_per_board,
    }
