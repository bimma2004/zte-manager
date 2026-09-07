# ZTE Manager

Tool internal untuk **search pelanggan** dan **cek redaman (RX power)** ONU
dari OLT ZTE (C300 / C320), terinspirasi dari alur kerja ZetSet.id.

## Kenapa cepat (tidak nunggu lama)?

Berbeda dari scraping telnet per-request (`show pon power onu-rx ...` yang
lambat kalau dipanggil live tiap kali search), tool ini pakai pola **poll
lalu cache**:

```
[Background poller]  --SNMP bulkwalk-->  [OLT ZTE]
        |
        v
   [SQLite cache]  <-- ditulis tiap poll_interval_seconds (default 5 menit)
        |
        v
   [Web UI search]  --baca cache langsung--> respon instan (<50ms)
```

Jadi proses SNMP yang makan waktu terjadi di background, bukan saat kamu
mengetik di kolom pencarian.

## 1. Instalasi

```bash
# di server (Ubuntu 22.04, contoh: 192.168.112.130)
sudo apt-get update
sudo apt-get install -y snmp python3-venv

cd zte-manager
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. WAJIB: verifikasi OID sebelum dipakai produksi

OID `rx_power` (redaman) berbeda antar firmware ZTE dan **tidak
didokumentasikan resmi**. Jangan langsung percaya nilai default di
`config.example.yaml` — verifikasi dulu ke OLT asli kamu:

```bash
python3 discover_oids.py --host 192.168.10.1 --community <read-community-kamu>
```

Script ini akan mencoba beberapa kandidat OID sekaligus dan menunjukkan mana
yang benar-benar mengembalikan data. Bandingkan angkanya dengan output CLI:

```
ZXAN# show pon power onu-rx gpon-onu_1/1/2:1
Rx power: -19.253(dbm)
```

Kalau value mentah dari SNMP beda skala dengan angka CLI (misal SNMP kasih
`-1925` sementara CLI bilang `-19.25`), berarti perlu `rx_power_scale: 0.01`
di config (sudah didefaultkan segitu, tapi cek ulang).

## 3. Konfigurasi

```bash
cp config.example.yaml config.yaml
nano config.yaml   # isi host OLT, community, profile yang sudah diverifikasi
```

## 4. Jalankan

```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Buka `http://<ip-server>:8090`.

### Jalankan sebagai service (systemd)

Lihat `deploy/zte-manager.service` — copy ke `/etc/systemd/system/`, sesuaikan
path, lalu:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now zte-manager
```

## Struktur project

```
zte-manager/
  app/
    main.py          FastAPI app + endpoint search/refresh
    poller.py         Logika polling SNMP -> cache DB
    snmp_client.py     Wrapper snmpbulkwalk/snmpget (net-snmp CLI)
    config.py          Loader config.yaml
    models.py           Model tabel Onu & PollLog
    database.py          Setup SQLite
  templates/index.html   UI search & redaman
  discover_oids.py        Script verifikasi OID ke OLT asli
  config.example.yaml     Contoh konfigurasi (copy -> config.yaml)
```

## Catatan tambahan

- Semua kolom SNMP di-join berdasarkan **index mentah dari device**
  (bukan dihitung matematis dari board/pon/onu_id), jadi lebih tahan
  terhadap perbedaan konfigurasi slot antar OLT.
- Kalau OLT punya banyak board/slot dan mau ditampilkan per board/pon
  secara eksplisit di UI, kasih tahu saya — bisa ditambahkan parsing index
  ke board/pon/onu_id (formatnya beda antar firmware, perlu contoh index
  asli dari `discover_oids.py` dulu).
- Endpoint `POST /api/olt/{id}/refresh` bisa dipanggil manual (misal dari
  tombol "Refresh sekarang" di UI, atau curl) kalau butuh data terbaru
  di luar jadwal poll.
