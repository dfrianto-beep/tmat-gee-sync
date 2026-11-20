import os
import json
import math
import requests
import ee
from datetime import datetime


# ==============================================================================
# 1. INISIALISASI GOOGLE EARTH ENGINE
# ==============================================================================
def init_gee():
    ee_key_json = os.environ.get("EE_KEY_JSON")
    if ee_key_json:
        key_dict = json.loads(ee_key_json)
        credentials = ee.ServiceAccountCredentials(
            key_dict["client_email"], key_data=ee_key_json
        )
        ee.Initialize(credentials)
    else:
        ee.Initialize(project="ee-dfrianto")


# ==============================================================================
# 2. FUNGSI PEMBERSIHAN & PENYERAGAMAN DATA
# ==============================================================================
def safe_float(val):
    if val is None or val == "" or str(val).strip().lower() in ["nan", "null", "none"]:
        return None
    try:
        num = float(val)
        return None if math.isnan(num) or math.isinf(num) else round(num, 4)
    except (ValueError, TypeError):
        return None


def normalize_date_str(date_str):
    """Menyeragamkan semua format tanggal menjadi 'YYYY-MM-DD'."""
    if not date_str or str(date_str).strip() in ["N/A", "None", "null"]:
        return ""
    date_str = str(date_str).strip().split(" ")[0].split("T")[0]
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def normalize_water_level_unit(val):
    """
    Memastikan satuan TMAT dalam meter (negatif di bawah permukaan tanah).
    Jika terbaca dalam sentimeter (nilai absolut > 15), otomatis dikonversi ke meter.
    """
    if val is None:
        return None
    if abs(val) > 15.0:
        val = round(val / 100.0, 4)
    if val > 0:
        val = -val
    return round(val, 4)


def extract_records_from_payload(payload):
    """Mengekstrak daftar record stasiun dari berbagai kemungkinan struktur JSON API."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Cek kunci array umum
        for key in ["data", "features", "results", "items", "records", "rows"]:
            val = payload.get(key)
            if isinstance(val, list):
                # Jika format GeoJSON FeatureCollection
                if val and isinstance(val[0], dict) and "properties" in val[0]:
                    extracted = []
                    for f in val:
                        props = dict(f.get("properties") or {})
                        coords = (f.get("geometry") or {}).get("coordinates") or []
                        if len(coords) >= 2:
                            props.setdefault("longitude", coords[0])
                            props.setdefault("latitude", coords[1])
                        extracted.append(props)
                    return extracted
                return val
            # Jika nested dict seperti {"data": {"data": [...]}}
            if isinstance(val, dict):
                for subkey in ["data", "items", "results", "rows"]:
                    if isinstance(val.get(subkey), list):
                        return val[subkey]
    return []


# ==============================================================================
# 3. PARSING RECORD STASIUN (SIPALAGA, SIMATAG, & KONSESI)
# ==============================================================================
def parse_station(item, kategori):
    lat = safe_float(item.get("latitude") or item.get("lat") or item.get("y"))
    lon = safe_float(item.get("longitude") or item.get("lon") or item.get("lng") or item.get("x"))

    # Validasi koordinat Indonesia
    if lat is None or lon is None or not (-12.0 <= lat <= 7.0 and 94.0 <= lon <= 142.0):
        return None, False

    source = str(item.get("source") or kategori).strip().lower()
    current = item.get("current") or {}
    detail = item.get("detail") or {}

    # 1. Penyeragaman Water Level (Meter)
    water_level = None
    if source == "sipalaga":
        water_level = safe_float(item.get("rs_water"))
    else:
        water_level = (
            safe_float(current.get("water_level"))
            or safe_float(detail.get("WaterLevel"))
            or safe_float(item.get("water_level"))
            or safe_float(item.get("rs_water"))
            or safe_float(item.get("tmat"))
        )

    water_level = normalize_water_level_unit(water_level)
    is_valid_wl = water_level is not None

    # 2. Penyeragaman Tanggal & Waktu Pencatatan
    tanggal_data = normalize_date_str(
        current.get("data_date") or item.get("data_date") or item.get("tanggal")
    )
    tgl_catat = normalize_date_str(
        current.get("tanggal") or detail.get("Tanggal") or tanggal_data
    )
    jam_catat = str(current.get("jam") or detail.get("Jam") or "").strip()

    if not tanggal_data and tgl_catat:
        tanggal_data = tgl_catat

    waktu_ukur = (
        f"{tgl_catat} {jam_catat}".strip()
        if jam_catat and jam_catat != "N/A"
        else tgl_catat
    )

    # 3. Penyeragaman Riwayat (History)
    history_clean = []
    raw_hist = item.get("history") or []

    if raw_hist:
        for h in raw_hist:
            wl_h = normalize_water_level_unit(
                safe_float(h.get("water_level") or h.get("wl"))
            )
            tgl_h = normalize_date_str(h.get("date") or h.get("tanggal"))
            if wl_h is not None and tgl_h:
                history_clean.append({"tgl": tgl_h, "wl": wl_h})

    elif source == "sipalaga":
        raw_iot = detail.get("ListIOTSensorWaterLevel") or []
        offset = safe_float(item.get("ref_offset"))
        daily_map = {}
        for row in raw_iot:
            tgl_h = normalize_date_str(row.get("Tanggal"))
            raw_wl = safe_float(row.get("WaterLevel"))
            if tgl_h and raw_wl is not None:
                wl_m = (
                    round((raw_wl - offset) / 100.0, 4)
                    if offset is not None
                    else normalize_water_level_unit(raw_wl)
                )
                daily_map[tgl_h] = wl_m
        for tgl_k, wl_v in sorted(daily_map.items()):
            history_clean.append({"tgl": tgl_k, "wl": wl_v})

    # Fallback jika array history kosong tetapi bacaan terbaru tersedia
    if not history_clean and is_valid_wl and tanggal_data:
        history_clean.append({"tgl": tanggal_data, "wl": water_level})

    # Urutkan riwayat dari terlama ke terbaru & batasi maks 180 hari terakhir agar payload aman
    history_clean = sorted(history_clean, key=lambda x: x["tgl"])[-180:]

    # 4. Identitas & Atribut Wilayah
    nama_stasiun = (
        str(item.get("nama") or "").strip()
        or str(item.get("lokasi") or "").strip()
        or str(item.get("kode_titik") or "").strip()
        or str(item.get("code") or "").strip()
    )

    status_str = (
        str(item.get("status") or "").strip()
        or str(current.get("status_label") or "").strip()
        or str(detail.get("Status") or "").strip()
    )

    properties = {
        "kategori": kategori.upper(),
        "station_id": str(item.get("station_id") or item.get("id") or ""),
        "nama_stasiun": nama_stasiun,
        "kode_perusahaan": str(item.get("kode_perusahaan") or "").strip(),
        "kode_titik": str(item.get("kode_titik") or "").strip(),
        "source": source,
        "provinsi": str(item.get("provinsi") or "").strip(),
        "kabupaten": str(item.get("kabupaten") or "").strip(),
        "kecamatan": str(item.get("kecamatan") or "").strip(),
        "latitude": lat,
        "longitude": lon,
        "water_level": water_level if is_valid_wl else -9999.0,
        "is_valid_wl": 1 if is_valid_wl else 0,
        "status": status_str,
        "tanggal": tanggal_data,
        "waktu_ukur": waktu_ukur,
        "synced_at": str(item.get("synced_at") or item.get("updated_at") or "").strip(),
        "history_count": len(history_clean),
        "history_json": json.dumps(history_clean, separators=(",", ":")),
    }

    geom = ee.Geometry.Point([lon, lat])
    return ee.Feature(geom, properties), is_valid_wl


# ==============================================================================
# 4. EKSEKUSI FETCH API & UPLOAD KE GEE ASSET
# ==============================================================================
def process_endpoint(url, kategori, asset_id, task_description):
    print(f"\n[{kategori}] Mengambil data dari {url}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=90)
    resp.raise_for_status()
    payload = resp.json()

    raw_data = extract_records_from_payload(payload)
    print(f"[{kategori}] Total record diterima dari API: {len(raw_data)}")

    if not raw_data:
        preview = str(payload)[:350]
        print(f"[{kategori}] Info: API mengembalikan 0 record (aset lama dipertahankan). Preview respons: {preview}")
        return

    features = []
    valid_wl_count = 0

    for item in raw_data:
        feat, is_valid = parse_station(item, kategori)
        if feat is not None:
            features.append(feat)
            if is_valid:
                valid_wl_count += 1

    print(
        f"[{kategori}] Stasiun siap upload: {len(features)} titik | Water Level valid: {valid_wl_count} titik"
    )

    if not features:
        print(f"[{kategori}] Info: Tidak ada koordinat valid pada respons API (aset lama dipertahankan).")
        return

    fc = ee.FeatureCollection(features)
    task = ee.batch.Export.table.toAsset(
        collection=fc,
        description=task_description,
        assetId=asset_id,
        overwrite=True,
    )
    task.start()
    print(f"[{kategori}] Task GEE berhasil dikirim! Task ID: {task.id}")


def main():
    init_gee()

    targets = [
        {
            "url": "https://opsroom.sipongidata.my.id/api/tmat/masyarakat",
            "kategori": "MASYARAKAT",
            "asset_id": "projects/ee-dfrianto/assets/TMAT_Harian/TMAT_Masyarakat_Harian",
            "task_desc": "Update_TMAT_Masyarakat_Harian",
        },
        {
            "url": "https://opsroom.sipongidata.my.id/api/tmat/konsesi",
            "kategori": "KONSESI",
            "asset_id": "projects/ee-dfrianto/assets/TMAT_Harian/TMAT_Konsesi_Harian",
            "task_desc": "Update_TMAT_Konsesi_Harian",
        },
    ]

    for t in targets:
        try:
            process_endpoint(t["url"], t["kategori"], t["asset_id"], t["task_desc"])
        except Exception as e:
            print(f"[{t['kategori']}] Peringatan gagal akses endpoint: {e}")


if __name__ == "__main__":
    main()
