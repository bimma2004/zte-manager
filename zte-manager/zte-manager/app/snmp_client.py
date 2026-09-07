"""
snmp_client.py
---------------
Wrapper tipis di atas binary net-snmp (snmpbulkwalk / snmpget).

Kenapa pakai binary net-snmp, bukan library python murni (pysnmp)?
- snmpbulkwalk (C, compiled) jauh lebih cepat untuk walk tabel besar
  (ratusan/ribuan ONU) dibanding pysnmp yang murni Python.
- Lebih stabil untuk GETBULK terhadap OLT ZTE yang kadang "rewel" soal
  ukuran paket SNMP.

Prasyarat di server:
    sudo apt-get install snmp
"""

import asyncio
import shlex
from dataclasses import dataclass


@dataclass
class SnmpTarget:
    host: str
    community: str
    version: str = "2c"  # "1", "2c"


class SnmpError(RuntimeError):
    pass


async def _run(cmd: list[str], timeout: float = 20.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise SnmpError(f"Timeout menjalankan: {' '.join(shlex.quote(c) for c in cmd)}")

    if proc.returncode != 0:
        raise SnmpError(stderr.decode(errors="replace").strip() or "snmp command failed")

    return stdout.decode(errors="replace")


def _parse_walk_output(raw: str) -> dict[str, str]:
    """
    Parse output snmpbulkwalk/snmpwalk (format -Oqn: 'OID VALUE') jadi dict
    {oid_suffix_terakhir: value}. Kita pakai -Oqn (numeric OID, quick print)
    supaya parsing stabil tanpa tergantung MIB terpasang.
    """
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or " " not in line:
            continue
        oid, _, value = line.partition(" ")
        value = value.strip()
        # snmpwalk kadang membungkus string values dengan tanda kutip
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        result[oid.strip()] = value
    return result


async def bulkwalk(target: SnmpTarget, oid: str, timeout: float = 25.0) -> dict[str, str]:
    """
    Jalankan snmpbulkwalk pada satu OID table.
    Return dict {full_oid: value}.
    """
    cmd = [
        "snmpbulkwalk",
        "-v", target.version,
        "-c", target.community,
        "-Oqn",          # quick numeric output, gampang diparse
        "-Cr50",         # max-repetitions, percepat bulk walk
        "-r", "1",       # retries
        "-t", "3",       # per-request timeout (detik)
        target.host,
        oid,
    ]
    raw = await _run(cmd, timeout=timeout)
    return _parse_walk_output(raw)


async def get(target: SnmpTarget, oids: list[str], timeout: float = 10.0) -> dict[str, str]:
    """snmpget untuk beberapa OID sekaligus (max disarankan ~4-8 per call)."""
    cmd = [
        "snmpget",
        "-v", target.version,
        "-c", target.community,
        "-Oqn",
        "-r", "1",
        "-t", "3",
        target.host,
        *oids,
    ]
    raw = await _run(cmd, timeout=timeout)
    return _parse_walk_output(raw)


def last_index(oid: str) -> str:
    """Ambil komponen index terakhir dari sebuah OID (setelah base+column)."""
    return oid.rsplit(".", 1)[-1]


def index_suffix(oid: str, column_oid_full: str) -> str:
    """
    Ambil BAGIAN INDEX dari sebuah OID hasil walk, yaitu sisa setelah
    prefix kolomnya dibuang. Ini dipakai untuk menggabungkan (join) beberapa
    kolom SNMP (nama, serial, rx_power, dst) berdasarkan index yang sama -
    lebih robust daripada menghitung index secara matematis (board*base+offset)
    karena langsung ambil dari device apa adanya.
    """
    prefix = column_oid_full.lstrip(".")
    oid = oid.lstrip(".")
    if oid.startswith(prefix + "."):
        return oid[len(prefix) + 1:]
    return oid
