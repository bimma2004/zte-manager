from __future__ import annotations

import asyncio
import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_

from app.config import load_config
from app.database import make_engine, make_session_factory
from app.models import Onu, PollLog
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


@app.on_event("startup")
async def on_startup():
    # Poll sekali saat startup biar cache langsung terisi, lalu jadwalkan berkala.
    asyncio.create_task(poll_all(config, SessionFactory))
    scheduler.add_job(
        poll_all,
        "interval",
        seconds=config.poll_interval_seconds,
        args=[config, SessionFactory],
        id="poll_all",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = SessionFactory()
    try:
        last_polls = (
            session.query(PollLog)
            .order_by(PollLog.started_at.desc())
            .limit(len(config.olts) or 1)
            .all()
        )
        total_onu = session.query(Onu).count()
        online_onu = session.query(Onu).filter(Onu.is_online.is_(True)).count()
    finally:
        session.close()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "olts": config.olts,
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


@app.post("/api/olt/{olt_id}/refresh")
async def refresh_olt(olt_id: str):
    """Trigger polling manual untuk satu OLT (kalau perlu data paling baru sekarang juga)."""
    olt = next((o for o in config.olts if o.id == olt_id), None)
    if olt is None:
        raise HTTPException(status_code=404, detail="OLT tidak ditemukan di config")
    profile = config.oid_profiles.get(olt.profile)
    if profile is None:
        raise HTTPException(status_code=500, detail=f"Profile '{olt.profile}' tidak ada di config")

    count = await poll_one_olt(olt, profile, SessionFactory)
    return {"olt_id": olt_id, "onu_count": count, "polled_at": dt.datetime.utcnow().isoformat()}


@app.get("/api/olts")
async def list_olts():
    return [
        {"id": o.id, "name": o.name, "host": o.host, "profile": o.profile}
        for o in config.olts
    ]


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
