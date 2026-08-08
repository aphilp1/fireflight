"""
Live position recorder for the NASA aircraft converging on Black Canyon Fire,
2026-08-07. Independent of AeroAPI (which has a tight request quota already
strained by the 5-min status poll) -- uses airplanes.live's free ADS-B feed
instead, polled every 15s per tail, same pattern as the ad-hoc N520NA
8/5 recorder that was built after nearly losing that flight's live track.

Writes one JSONL file per tail to track_logs/, one line per poll:
  {"ts": <unix>, "tail": "N806NA", "lat":.., "lon":.., "alt_baro":.., "gs":.., "track":..}

A tail is considered landed (and stops being polled) once ground speed stays
under 30kt AND baro altitude stays under 500ft for 3 consecutive polls (~45s).
Recording ends entirely once all tails have landed, or after MAX_RUNTIME_SEC
as a safety backstop.
"""
import json
import time
import urllib.request
import os

TAILS = ["N806NA", "N520NA", "N529NA"]
POLL_SEC = 15
LANDED_STREAK_NEEDED = 3
MAX_RUNTIME_SEC = 6 * 3600  # 6hr safety backstop

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_logs")
os.makedirs(OUT_DIR, exist_ok=True)

landed_streak = {t: 0 for t in TAILS}
landed = {t: False for t in TAILS}
files = {t: open(os.path.join(OUT_DIR, f"{t}_0807_live_recording.jsonl"), "a") for t in TAILS}

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def poll(tail):
    # NASA's broadcast callsign (e.g. "NASA806") differs from the tail/registration
    # (N806NA) -- /callsign/ returns nothing for these. /reg/ is what FireFlight's own
    # app uses for exactly this reason; verified working directly 2026-08-07 15:43.
    url = f"https://api.airplanes.live/v2/reg/{tail}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FireFlight-recorder"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ac_list = data.get("ac") or []
        if not ac_list:
            return None
        a = ac_list[0]
        return {
            "ts": int(time.time()),
            "tail": tail,
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "alt_baro": a.get("alt_baro"),
            "gs": a.get("gs"),
            "track": a.get("track"),
        }
    except Exception as e:
        log(f"{tail}: poll error: {e}")
        return None

log(f"Recording started for {TAILS}, polling every {POLL_SEC}s, writing to {OUT_DIR}")

start = time.time()
try:
    while True:
        if time.time() - start > MAX_RUNTIME_SEC:
            log("Max runtime reached, stopping.")
            break
        if all(landed.values()):
            log("All tails landed, stopping.")
            break

        for tail in TAILS:
            if landed[tail]:
                continue
            rec = poll(tail)
            if rec is None:
                continue
            files[tail].write(json.dumps(rec) + "\n")
            files[tail].flush()

            gs = rec.get("gs") or 0
            alt = rec.get("alt_baro") or 0
            if isinstance(alt, str):  # airplanes.live uses "ground" as a string sometimes
                alt = 0
            if gs < 30 and alt < 500:
                landed_streak[tail] += 1
                if landed_streak[tail] >= LANDED_STREAK_NEEDED:
                    landed[tail] = True
                    log(f"{tail}: appears landed (gs<30, alt<500 for {LANDED_STREAK_NEEDED} polls). Stopped recording.")
            else:
                landed_streak[tail] = 0

        time.sleep(POLL_SEC)
finally:
    for f in files.values():
        f.close()
    log("Recorder exiting, files closed.")
