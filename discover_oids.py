#!/usr/bin/env python3
"""
discover_oids.py
-----------------
Script diagnostik BERDIRI SENDIRI (gak butuh DB/FastAPI) untuk memverifikasi
OID SNMP yang benar terhadap OLT ZTE kamu yang sebenarnya, SEBELUM dipakai
di config.yaml produksi.

Kenapa perlu ini?
OID redaman (rx_power) terutama berbeda-beda tergantung firmware ZTE
(C300 vs C320 v2.1 vs v2.2+), dan tidak ada dokumentasi resmi publik dari
ZTE. Nilai default di config.example.yaml adalah hasil riset komunitas,
BUKAN jaminan 100% cocok dengan OLT kamu.

Cara pakai:
  1. Pastikan `snmp` sudah terinstall: sudo apt-get install snmp
  2. Pastikan SNMP read community sudah aktif di OLT ("show snmp community"
     atau setup sesuai panduan Komdigi/internal kamu)
  3. Jalankan:
       python3 discover_oids.py --host 192.168.10.1 --community public

  4. Bandingkan hasil "rx_power kandidat" dengan hasil CLI asli, contoh:
       ZXAN# show pon power onu-rx gpon-onu_1/1/2:1
       Rx power: -19.253(dbm)

  5. Cari ONU yang sama di output script ini (lihat kolom "index" &
     "value mentah"), lalu hitung scale factor:
       scale = nilai_asli_dbm / nilai_mentah_snmp
     Biasanya scale = 0.01 (nilai mentah dalam 1/100 dBm) atau -0.01/0.001,
     tergantung firmware.
  6. Update rx_power (dan rx_power_scale) di config.yaml sesuai temuan.
"""

import argparse
import asyncio
import sys

sys.path.insert(0, ".")
from app import snmp_client  # noqa: E402

# Kandidat-kandidat OID rx_power & info dasar ONU dari berbagai firmware ZTE
# yang ditemukan dari riset (community-sourced). Script ini akan mencoba
# SEMUA kandidat dan menunjukkan mana yang benar-benar mengembalikan data.
CANDIDATES = {
    "C300 / C320 firmware lama (base .1012.3)": {
        "onu_name": ".1.3.6.1.4.1.3902.1012.3.28.1.1.2",
        "serial_number": ".1.3.6.1.4.1.3902.1012.3.28.1.1.5",
        "oper_state": ".1.3.6.1.4.1.3902.1012.3.50.11.2.1.8",
        "distance_m": ".1.3.6.1.4.1.3902.1012.3.11.4.1.2",
        "rx_power_candidate_1": ".1.3.6.1.4.1.3902.1012.3.50.12.1.1.14",
        "rx_power_candidate_2": ".1.3.6.1.4.1.3902.1012.3.50.12.1.1.10",
    },
    "C320 firmware v2.1 (base .1012, index berbeda)": {
        "onu_name": ".1.3.6.1.4.1.3902.1012.3.13.3.1.5",
        "serial_number": ".1.3.6.1.4.1.3902.1012.3.13.3.1.2",
        "description": ".1.3.6.1.4.1.3902.1012.3.13.3.1.11",
        "distance_m": ".1.3.6.1.4.1.3902.1012.3.13.1.1.20",
        "rx_power_candidate_1": ".1.3.6.1.4.1.3902.1012.3.31.4.1.100",
    },
    "C320 firmware v2.2+ (base .1082.500)": {
        "onu_name": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.2",
        "serial_number": ".1.3.6.1.4.1.3902.1082.500.10.2.3.3.1.18",
        "onu_type": ".1.3.6.1.4.1.3902.1082.3.50.11.2.1.17",
        "rx_power_candidate_1": ".1.3.6.1.4.1.3902.1082.500.20.2.2.2.1.10",
        "last_online": ".1.3.6.1.4.1.3902.1082.500.10.2.3.8.1.5",
    },
}


async def probe(target: snmp_client.SnmpTarget, label: str, oids: dict[str, str]):
    print(f"\n{'=' * 70}")
    print(f"PROFIL: {label}")
    print(f"{'=' * 70}")
    any_data = False
    for name, oid in oids.items():
        try:
            data = await snmp_client.bulkwalk(target, oid, timeout=12)
        except snmp_client.SnmpError as e:
            print(f"  [{name:28s}] ERROR: {e}")
            continue

        if not data:
            print(f"  [{name:28s}] (kosong / tidak ada data pada OID ini)")
            continue

        any_data = True
        print(f"  [{name:28s}] {len(data)} entri ditemukan. Contoh 3 pertama:")
        for i, (full_oid, val) in enumerate(data.items()):
            if i >= 3:
                break
            idx = snmp_client.index_suffix(full_oid, oid)
            print(f"      index={idx:20s} value_mentah={val}")

    if not any_data:
        print("  -> Profil ini sepertinya TIDAK COCOK dengan OLT ini (semua OID kosong).")
    else:
        print("  -> Profil ini KEMUNGKINAN COCOK. Cocokkan value di atas dengan CLI OLT.")


async def main():
    ap = argparse.ArgumentParser(description="Diagnostik OID SNMP untuk ZTE OLT")
    ap.add_argument("--host", required=True, help="IP OLT")
    ap.add_argument("--community", required=True, help="SNMP read community")
    ap.add_argument("--version", default="2c", choices=["1", "2c"])
    ap.add_argument(
        "--profile",
        default=None,
        help="Nama profil spesifik untuk dicoba saja (default: coba semua)",
    )
    args = ap.parse_args()

    target = snmp_client.SnmpTarget(host=args.host, community=args.community, version=args.version)

    print(f"Menguji konektivitas dasar SNMP ke {args.host} ...")
    try:
        # sysDescr sebagai basic connectivity check
        basic = await snmp_client.get(target, ["1.3.6.1.2.1.1.1.0"])
        print(f"sysDescr: {list(basic.values())[0] if basic else '(tidak ada respon)'}")
    except snmp_client.SnmpError as e:
        print(f"GAGAL konek SNMP: {e}")
        print("Cek: community string, ACL SNMP di OLT, dan firewall/routing.")
        return

    profiles = CANDIDATES
    if args.profile:
        profiles = {k: v for k, v in CANDIDATES.items() if args.profile.lower() in k.lower()}
        if not profiles:
            print(f"Profil '{args.profile}' tidak ditemukan. Pilihan: {list(CANDIDATES.keys())}")
            return

    for label, oids in profiles.items():
        await probe(target, label, oids)

    print("\n\nLANGKAH SELANJUTNYA:")
    print("1. Lihat profil mana yang punya data (bukan kosong semua).")
    print("2. Cocokkan salah satu 'index' + value rx_power di atas dengan:")
    print("     ZXAN# show pon power onu-rx gpon-onu_<board>/1/<pon>:<onu_id>")
    print("3. Update config.yaml dengan base_oid & suffix kolom yang cocok.")


if __name__ == "__main__":
    asyncio.run(main())
