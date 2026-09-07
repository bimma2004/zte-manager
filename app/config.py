"""
config.py - load config.yaml jadi struktur Python yang gampang dipakai.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

CONFIG_PATH = os.environ.get("ZTE_MANAGER_CONFIG", "config.yaml")


@dataclass
class OidProfile:
    key: str
    label: str
    base_oid: str
    columns: dict[str, str]
    rx_power_scale: float = 1.0
    status_ok_value: str = "1"

    def full_oid(self, column: str) -> str:
        return self.base_oid + self.columns[column]


@dataclass
class OltConfig:
    id: str
    name: str
    host: str
    snmp_community: str
    snmp_version: str
    profile: str
    boards: list[int] = field(default_factory=list)
    pons_per_board: int = 16


@dataclass
class AppConfig:
    poll_interval_seconds: int
    db_path: str
    timezone: str
    olts: list[OltConfig]
    oid_profiles: dict[str, OidProfile]


def load_config(path: str | None = None) -> AppConfig:
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config tidak ditemukan: {path}. "
            f"Copy config.example.yaml -> config.yaml lalu sesuaikan."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    app_raw = raw.get("app", {})

    profiles: dict[str, OidProfile] = {}
    for key, p in raw.get("oid_profiles", {}).items():
        profiles[key] = OidProfile(
            key=key,
            label=p.get("label", key),
            base_oid=p["base_oid"],
            columns=p["columns"],
            rx_power_scale=p.get("rx_power_scale", 1.0),
            status_ok_value=str(p.get("status_ok_value", "1")),
        )

    olts: list[OltConfig] = []
    for o in raw.get("olts", []):
        olts.append(
            OltConfig(
                id=o["id"],
                name=o.get("name", o["id"]),
                host=o["host"],
                snmp_community=o["snmp_community"],
                snmp_version=str(o.get("snmp_version", "2c")),
                profile=o["profile"],
                boards=o.get("boards", []),
                pons_per_board=o.get("pons_per_board", 16),
            )
        )

    return AppConfig(
        poll_interval_seconds=app_raw.get("poll_interval_seconds", 300),
        db_path=app_raw.get("db_path", "data/zte_manager.db"),
        timezone=app_raw.get("timezone", "Asia/Jakarta"),
        olts=olts,
        oid_profiles=profiles,
    )
