"""
Periodic snapshot recorder for Black Canyon and Widemouth 2 fire conditions
(NIFC incident data + NWS weather), 2026-08-07/08. Fire/weather data doesn't
change fast, so this polls every 15 minutes -- much slower than the aircraft
recorders -- and appends one JSON line per fire per poll to
track_logs/<fire>_conditions_log.jsonl.

Fields recorded per snapshot: incident size/containment/behavior/personnel
(from NIFC WFIGS), plus temp/RH/wind/visibility/textDescription from the
nearest NWS observation station, plus any active NWS alerts for that point.
"""
import json
import time
import urllib.request
import urllib.parse
import os

FIRES = {
    "black_canyon": {"name": "Black Canyon", "point": (39.3874, -111.3052)},
    "widemouth_2": {"name": "Widemouth 2", "point": (38.7336, -112.5230)},
}
POLL_SEC = 900  # 15 min
MAX_RUNTIME_SEC = 8 * 3600  # 8hr safety backstop
UA = {"User-Agent": "FireFlight (research, aphilp1@gmail.com)"}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_logs")
os.makedirs(OUT_DIR, exist_ok=True)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def nifc_incident(name):
    try:
        where = urllib.parse.quote(f"IncidentName='{name}'")
        url = (f"https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
               f"WFIGS_Incident_Locations_Current/FeatureServer/0/query?where={where}&outFields=*&f=json")
        d = get_json(url, UA)
        feats = d.get("features", [])
        if not feats:
            return {}
        a = feats[0]["attributes"]
        return {
            "acres": a.get("IncidentSize"),
            "pct_contained": a.get("PercentContained"),
            "behavior": a.get("FireBehaviorGeneral"),
            "personnel": a.get("TotalIncidentPersonnel"),
            "modified": a.get("ModifiedOnDateTime_dt"),
        }
    except Exception as e:
        log(f"nifc error for {name}: {e}")
        return {}

# cache grid/station lookups (these don't change)
_grid_cache = {}
_station_cache = {}

def nws_grid(lat, lon):
    key = (round(lat, 4), round(lon, 4))
    if key in _grid_cache:
        return _grid_cache[key]
    d = get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", UA)
    p = d["properties"]
    val = (p["gridId"], p["gridX"], p["gridY"])
    _grid_cache[key] = val
    return val

def nws_station(gridId, gx, gy):
    key = (gridId, gx, gy)
    if key in _station_cache:
        return _station_cache[key]
    d = get_json(f"https://api.weather.gov/gridpoints/{gridId}/{gx},{gy}/stations", UA)
    feats = d.get("features", [])
    val = feats[0]["properties"]["stationIdentifier"] if feats else None
    _station_cache[key] = val
    return val

def weather_snapshot(lat, lon):
    try:
        gridId, gx, gy = nws_grid(lat, lon)
        station = nws_station(gridId, gx, gy)
        obs = {}
        if station:
            d = get_json(f"https://api.weather.gov/stations/{station}/observations/latest", UA)
            p = d.get("properties", {})
            def c2f(c):
                return round(c * 9 / 5 + 32, 1) if c is not None else None
            ws = p.get("windSpeed", {}).get("value")
            obs = {
                "station": station,
                "obs_time": p.get("timestamp"),
                "temp_f": c2f(p.get("temperature", {}).get("value")),
                "rh_pct": p.get("relativeHumidity", {}).get("value"),
                "wind_mph": round(ws * 2.237, 1) if ws else None,
                "wind_dir": p.get("windDirection", {}).get("value"),
                "visibility_m": p.get("visibility", {}).get("value"),
                "text": p.get("textDescription"),
            }
        alerts_d = get_json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}", UA)
        alerts = [f["properties"]["event"] for f in alerts_d.get("features", [])]
        obs["active_alerts"] = alerts
        return obs
    except Exception as e:
        log(f"weather error: {e}")
        return {}

log(f"Fire conditions recorder started for {list(FIRES.keys())}, polling every {POLL_SEC}s")

files = {k: open(os.path.join(OUT_DIR, f"{k}_conditions_log.jsonl"), "a") for k in FIRES}

start = time.time()
try:
    while True:
        if time.time() - start > MAX_RUNTIME_SEC:
            log("Max runtime reached, stopping.")
            break
        for key, info in FIRES.items():
            lat, lon = info["point"]
            snap = {"ts": int(time.time()), "fire": info["name"]}
            snap.update(nifc_incident(info["name"]))
            snap["weather"] = weather_snapshot(lat, lon)
            files[key].write(json.dumps(snap) + "\n")
            files[key].flush()
            log(f"{info['name']}: {snap.get('acres')} ac, {snap.get('pct_contained')}% contained, "
                f"{snap.get('behavior')}, wx={snap['weather'].get('temp_f')}F/{snap['weather'].get('rh_pct')}%RH")
            time.sleep(3)  # small stagger between the two fires' API calls
        time.sleep(POLL_SEC)
finally:
    for f in files.values():
        f.close()
    log("Recorder exiting, files closed.")
