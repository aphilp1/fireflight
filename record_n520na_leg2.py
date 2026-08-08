"""
Continuation recorder for N520NA's second leg (KPVU->KBOI, departed
2026-08-08T03:26:38Z). Its original recorder instance already self-stopped
once it detected the FIRST landing at KPVU (by design -- a tail is dropped
once landed and never resumed). This is a fresh instance scoped to just
N520NA for the new leg, appending to the same track_logs file so the day's
record stays continuous across both legs. Same landed-detection/poll logic
as record_black_canyon_flights.py.
"""
import json
import time
import urllib.request
import os

TAIL = "N520NA"
POLL_SEC = 15
LANDED_STREAK_NEEDED = 3
MAX_RUNTIME_SEC = 4 * 3600

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_logs")
outfile = open(os.path.join(OUT_DIR, f"{TAIL}_0807_live_recording.jsonl"), "a")

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def poll():
    url = f"https://api.airplanes.live/v2/reg/{TAIL}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FireFlight-recorder"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ac_list = data.get("ac") or []
        if not ac_list:
            return None
        a = ac_list[0]
        return {"ts": int(time.time()), "tail": TAIL, "lat": a.get("lat"), "lon": a.get("lon"),
                "alt_baro": a.get("alt_baro"), "gs": a.get("gs"), "track": a.get("track")}
    except Exception as e:
        log(f"poll error: {e}")
        return None

log(f"Recording N520NA leg 2 (KPVU->KBOI), polling every {POLL_SEC}s")

landed_streak = 0
start = time.time()
try:
    while True:
        if time.time() - start > MAX_RUNTIME_SEC:
            log("Max runtime reached, stopping.")
            break
        rec = poll()
        if rec is not None:
            outfile.write(json.dumps(rec) + "\n")
            outfile.flush()
            gs = rec.get("gs") or 0
            alt = rec.get("alt_baro") or 0
            if isinstance(alt, str):
                alt = 0
            if gs < 30 and alt < 500:
                landed_streak += 1
                if landed_streak >= LANDED_STREAK_NEEDED:
                    log("N520NA appears landed. Stopping.")
                    break
            else:
                landed_streak = 0
        time.sleep(POLL_SEC)
finally:
    outfile.close()
    log("Recorder exiting, file closed.")
