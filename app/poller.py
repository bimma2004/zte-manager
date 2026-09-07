"""
poller.py
---------
Untuk setiap OLT: bulkwalk beberapa kolom SNMP (nama, serial, rx_power, dst),
gabungkan (join) hasilnya berdasarkan index SNMP yang sama, lalu upsert ke
tabel `onus`. Dijalankan berkala oleh scheduler (lihat main.py), dan bisa
dipicu manual lewat endpoint /api/olt/{id}/refresh.

Karena search & cek redaman di web UI HANYA baca dari DB (bukan live SNMP),
proses yang "lambat" (SNMP walk ke device) terjadi di background sini,
bukan saat user menunggu hasil pencarian.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app import snmp_client
from app.config import AppConfig, OltConfig, OidProfile
from app.models import Onu, PollLog

log = logging.getLogger("poller")


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def poll_one_olt(olt: OltConfig, profile: OidProfile, session_factory) -> int:
    """Poll satu OLT, upsert semua ONU-nya. Return jumlah ONU yang berhasil dipoll."""
    target = snmp_client.SnmpTarget(
        host=olt.host, community=olt.snmp_community, version=olt.snmp_version
    )

    session = session_factory()
    poll_log = PollLog(olt_id=olt.id, started_at=dt.datetime.utcnow(), status="running")
    session.add(poll_log)
    session.commit()

    try:
        # 1. Walk semua kolom yang dikonfigurasi, paralel.
        column_names = list(profile.columns.keys())
        walk_tasks = [
            snmp_client.bulkwalk(target, profile.full_oid(col)) for col in column_names
        ]
        walk_results = await asyncio.gather(*walk_tasks, return_exceptions=True)

        per_column: dict[str, dict[str, str]] = {}
        for col, res in zip(column_names, walk_results):
            if isinstance(res, Exception):
                log.warning("OLT %s: gagal walk kolom '%s': %s", olt.id, col, res)
                per_column[col] = {}
            else:
                per_column[col] = res

        # 2. Join berdasarkan index SNMP (suffix OID setelah base+column).
        #    Pakai kolom "onu_name" atau "serial_number" sebagai anchor index,
        #    yang mana saja yang tersedia & tidak kosong.
        anchor_col = "onu_name" if per_column.get("onu_name") else "serial_number"
        anchor_oid_full = profile.full_oid(anchor_col)
        indices = {
            snmp_client.index_suffix(oid, anchor_oid_full)
            for oid in per_column.get(anchor_col, {})
        }

        # bangun lookup: {column: {index: value}}
        lookup: dict[str, dict[str, str]] = {}
        for col, values in per_column.items():
            col_oid_full = profile.full_oid(col)
            lookup[col] = {
                snmp_client.index_suffix(oid, col_oid_full): val
                for oid, val in values.items()
            }

        count = 0
        for idx in indices:
            name = lookup.get("onu_name", {}).get(idx, "")
            serial = lookup.get("serial_number", {}).get(idx, "")
            desc = lookup.get("onu_description", {}).get(idx, "")
            onu_type = lookup.get("onu_type", {}).get(idx, "")
            status_raw = lookup.get("oper_state", {}).get(idx, "")
            distance_raw = lookup.get("distance_m", {}).get(idx, "")
            rx_raw = lookup.get("rx_power", {}).get(idx, "")

            rx_power = _to_float(rx_raw)
            if rx_power is not None:
                rx_power *= profile.rx_power_scale

            distance = _to_float(distance_raw)

            row = (
                session.query(Onu)
                .filter(Onu.olt_id == olt.id, Onu.raw_index == idx)
                .one_or_none()
            )
            if row is None:
                row = Onu(olt_id=olt.id, raw_index=idx)
                session.add(row)

            row.olt_name = olt.name
            row.name = name
            row.description = desc
            row.serial_number = serial
            row.onu_type = onu_type
            row.rx_power_dbm = rx_power
            row.distance_m = distance
            row.status_raw = status_raw
            row.is_online = status_raw == profile.status_ok_value
            row.last_polled_at = dt.datetime.utcnow()
            count += 1

        session.commit()

        poll_log.finished_at = dt.datetime.utcnow()
        poll_log.onu_count = count
        poll_log.status = "ok"
        session.commit()
        return count

    except Exception as e:  # noqa: BLE001
        session.rollback()
        poll_log.finished_at = dt.datetime.utcnow()
        poll_log.status = "error"
        poll_log.error_message = str(e)[:1000]
        session.commit()
        log.exception("Poll OLT %s gagal", olt.id)
        raise
    finally:
        session.close()


async def poll_all(config: AppConfig, session_factory) -> dict[str, int]:
    results: dict[str, int] = {}
    for olt in config.olts:
        profile = config.oid_profiles.get(olt.profile)
        if profile is None:
            log.error("OLT %s: profile '%s' tidak ditemukan di config", olt.id, olt.profile)
            continue
        try:
            results[olt.id] = await poll_one_olt(olt, profile, session_factory)
        except Exception:  # noqa: BLE001
            results[olt.id] = -1
    return results
