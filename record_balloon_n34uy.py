"""
Position recorder for N34UY, the research balloon operating near Black Canyon
Fire, 2026-08-07. Unlike the 3 NASA jets, N34UY has no live ADS-B presence
(checked -- not visible on airplanes.live), so this polls the local FireFlight
backend's /api/plan endpoint instead, which pulls fresh track points from
AeroAPI's own actual_track field each call. Polled every 3 minutes (much
slower than the jets' 15s recorder) since a balloon moves slowly and this
avoids adding meaningful load to the already-quota-constrained AeroAPI key.

Writes one JSONL line per NEW track point seen (dedup by timestamp, since
each /api/plan call returns the full track history, not just the latest
point) to track_logs/N34UY_0807_live_recording.jsonl:
  {"ts": <unix>, "tail": "N34UY", "lat":.., "lon":.., "alt_ft":..}

Considered landed/ended once status stops being "En Route", or after
MAX_RUNTIME_SEC as a safety backstop.
"""
import json
import time
import urllib.request
import os

TAIL = "N34UY"
POLL_SEC = 180  # 3 min
MAX_RUNTIME_SEC = 6 * 3600  # 6hr safety backstop
BACKEND_URL = f"http://localhost:8934/api/plan?tail={TAIL}"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "track_logs")
os.makedirs(OUT_DIR, exist_ok=True)
outfile = open(os.path.join(OUT_DIR, f"{TAIL}_0807_live_recording.jsonl"), "a")

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def poll():
    try:
        req = urllib.request.Request(BACKEND_URL, headers={"User-Agent": "FireFlight-recorder"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("error"):
            log(f"poll error: {data['error']}")
            return None, None
        return data.get("status"), data.get("actual_track") or []
    except Exception as e:
        log(f"poll error: {e}")
        return None, None

log(f"Recording started for {TAIL}, polling every {POLL_SEC}s via local backend, writing to {OUT_DIR}")

seen_ts = set()
start = time.time()
try:
    while True:
        if time.time() - start > MAX_RUNTIME_SEC:
            log("Max runtime reached, stopping.")
            break

        status, track = poll()
        if track:
            new_count = 0
            for pt in track:
                ts, lat, lon, alt = pt[0], pt[1], pt[2], pt[3]
                if ts in seen_ts:
                    continue
                seen_ts.add(ts)
                outfile.write(json.dumps({"ts": ts, "tail": TAIL, "lat": lat, "lon": lon, "alt_ft": alt}) + "\n")
                new_count += 1
            if new_count:
                outfile.flush()
                log(f"status={status}, +{new_count} new points ({len(seen_ts)} total)")

        if status and status != "En Route":
            log(f"status changed to '{status}' -- stopping.")
            break

        time.sleep(POLL_SEC)
finally:
    outfile.close()
    log("Recorder exiting, file closed.")
