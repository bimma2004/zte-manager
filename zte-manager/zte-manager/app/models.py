from __future__ import annotations

import datetime as dt

from sqlalchemy import String, Float, Integer, DateTime, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Olt(Base):
    """
    Daftar OLT yang dipantau. Disimpan di DB (bukan cuma config.yaml) supaya
    bisa ditambah/dihapus dari UI tanpa perlu edit file & restart server.
    """

    __tablename__ = "olts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    host: Mapped[str] = mapped_column(String(64))
    snmp_community: Mapped[str] = mapped_column(String(128))
    snmp_version: Mapped[str] = mapped_column(String(8), default="2c")
    profile: Mapped[str] = mapped_column(String(64))  # key ke oid_profiles di config.yaml
    boards: Mapped[str] = mapped_column(String(255), default="")  # contoh: "1,2,3,4"
    pons_per_board: Mapped[int] = mapped_column(Integer, default=16)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class Onu(Base):
    """
    Satu baris = satu ONU/pelanggan pada satu OLT, hasil polling SNMP
    terakhir. Tabel ini yang dibaca untuk fitur "search pelanggan" &
    "cek redaman" -> selalu baca dari sini (cache), bukan live ke OLT,
    supaya responnya instan.
    """

    __tablename__ = "onus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    olt_id: Mapped[str] = mapped_column(String(64), index=True)
    olt_name: Mapped[str] = mapped_column(String(128))

    board: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pon: Mapped[int | None] = mapped_column(Integer, nullable=True)
    onu_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # index mentah dari SNMP, dipakai untuk join antar kolom saat polling
    raw_index: Mapped[str] = mapped_column(String(128), index=True)

    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    serial_number: Mapped[str] = mapped_column(String(64), default="", index=True)
    onu_type: Mapped[str] = mapped_column(String(64), default="")

    rx_power_dbm: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    status_raw: Mapped[str] = mapped_column(String(32), default="")
    is_online: Mapped[bool] = mapped_column(default=False)

    last_polled_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )

    __table_args__ = (
        Index("ix_onu_olt_raw_index", "olt_id", "raw_index", unique=True),
    )


class PollLog(Base):
    __tablename__ = "poll_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    olt_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    onu_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running/ok/error
    error_message: Mapped[str] = mapped_column(String(1024), default="")
