import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import os
import re
import math
import datetime

# --- /api/plan?tail=XXXX ---------------------------------------------------
# Added 2026-08-05 to power FireFlight.html's existing tail-number "TRACK" box
# (auto-build a mission: filed plan + TFRs + fires, no manual paste/chat needed).
#
# Data flow:
#   1. AeroAPI (flightaware, key in .env, gitignored) -> filed route text,
#      origin/destination airport codes, altitude/speed, times/status.
#   2. AeroAPI /airports/{icao} -> real lat/lon for origin + destination.
#   3. extract_route_points() -> best-effort EXTRA points parsed directly out
#      of the route text (VOR-radial-distance like "DLN295035", or literal
#      lat/lon like "4610N/11505W"). This is a true-radial approximation with
#      NO magnetic variation correction -- good enough to widen the TFR/fire
#      search box (esp. for round trips where origin==destination and a plain
#      box would miss the actual target area entirely) and to draw a rough
#      route line, but it is NOT real fix-by-fix plotting. Named intersection
#      fixes that aren't airports/VORs (e.g. "BARNR") still can't be resolved
#      at all -- that needs the full FAA NASR navaid/fix database, which this
#      backend does not have loaded. If a future session wants exact plotting,
#      that's the missing piece, not a re-architecture.
#   4. tfr.faa.gov WFS layer TFR:V_TFR_LOC -> real TFR polygons in that box
#      (this endpoint has no CORS header, which is why this has to run
#      server-side and can never be called directly from the browser).
# -----------------------------------------------------------------------------

PORT = 8934

def load_env():
    env = {}
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    env[k.strip()] = v.strip()
    return env

ENV = load_env()
AEROAPI_KEY = ENV.get('AEROAPI_KEY', '')

def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))

def _flight_recency_key(f):
    """Best-effort 'how recent is this flight' sort key. AeroAPI's list endpoint
    sometimes nulls out actual_on/actual_off for a 'Diverted' row (seen 2026-08-06,
    N53W's KTRK leg -- Diverted with both fields None even though the same
    fa_flight_id had a real actual_off when queried directly earlier that day), so
    fall back to scheduled_off, then to the epoch timestamp FlightAware embeds in
    fa_flight_id itself (e.g. 'N53W-1786031890-sw-4611p') which is always present."""
    for field in ('actual_on', 'actual_off', 'scheduled_off'):
        v = f.get(field)
        if v:
            return v
    m = re.search(r'-(\d{9,11})-', f.get('fa_flight_id') or '')
    if m:
        return datetime.datetime.utcfromtimestamp(int(m.group(1))).isoformat()
    return ''

def _parse_iso(ts):
    """Parse an AeroAPI timestamp ('...Z' or naive from _flight_recency_key's
    fa_flight_id fallback) into a tz-aware UTC datetime, or None if unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except ValueError:
        return None

def pick_best_flight(flights):
    """AeroAPI's flights/{ident} list is NOT reliably 'current flight first' -- for a
    busy commercial tail it can return future-scheduled flights ahead of the one
    actually airborne right now. Found 2026-08-06 debugging N285SY: index 0 was a
    flight scheduled 3 days out while the real in-progress flight was buried at
    index 11. Priority: currently airborne > most recently resolved (arrived OR
    diverted) > soonest upcoming scheduled."""
    en_route = [f for f in flights if (f.get('status') or '').startswith('En Route')]
    if en_route:
        en_route.sort(key=lambda f: f.get('actual_off') or '', reverse=True)
        return en_route[0]
    # A flight that actually happened -- landed at the filed destination OR diverted
    # elsewhere -- is usually more relevant than a hypothetical future scheduled one.
    # Fixed 2026-08-06 after QXE2154 landed and the code jumped straight to a
    # 'Scheduled' flight for the NEXT DAY instead of showing the one that had just
    # landed minutes earlier. Extended same day after N53W diverted off its filed
    # KTRK route (near Boise) -- 'Diverted' wasn't in this bucket at all, so the app
    # fell through to an unrelated 'Arrived' flight from two days prior instead.
    resolved = [f for f in flights if f.get('actual_on') or (f.get('status') or '').startswith('Diverted')]
    resolved_best = None
    if resolved:
        resolved.sort(key=_flight_recency_key, reverse=True)
        resolved_best = resolved[0]
    scheduled = [f for f in flights if (f.get('status') or '').startswith('Scheduled')]
    scheduled_best = None
    if scheduled:
        scheduled.sort(key=lambda f: f.get('scheduled_off') or '9999')
        scheduled_best = scheduled[0]
    # 'Resolved always wins' breaks down when the resolved flight is stale -- found
    # 2026-08-07 tracking N529NA/N520NA ahead of a NASA multi-aircraft mission: each
    # had an 'Arrived' flight from the day before outranking TODAY's 'Scheduled'
    # flight departing within the hour. Fix: when both a resolved and a scheduled
    # candidate exist, pick whichever one's timestamp is closer to right now --
    # a flight landing/diverting minutes ago still wins over a same-day scheduled
    # flight hours out (preserves the original QXE2154 fix), but an imminent
    # scheduled departure now correctly wins over a day-old arrival.
    if resolved_best and scheduled_best:
        now = datetime.datetime.now(datetime.timezone.utc)
        r_time = _parse_iso(_flight_recency_key(resolved_best))
        s_time = _parse_iso(scheduled_best.get('scheduled_off'))
        if r_time is not None and s_time is not None:
            r_dist = abs((now - r_time).total_seconds())
            s_dist = abs((now - s_time).total_seconds())
            return scheduled_best if s_dist < r_dist else resolved_best
        return resolved_best
    if resolved_best:
        return resolved_best
    if scheduled_best:
        return scheduled_best
    return flights[0]

def _borrow_route_if_missing(chosen, flights):
    """AeroAPI sometimes splits one real takeoff into two records -- the originally
    filed flight (with the real route) gets marked 'Arrived'/closed early, while a
    separate route-less 'adhoc' record picks up the live En Route tracking. Found
    2026-08-07 on N806NA's actual ER-2 departure: the adhoc En Route record (the one
    pick_best_flight correctly prefers for live status) had route=None, which broke
    corridor fires/TFR filtering downstream. If the chosen flight has no route, borrow
    one from whichever OTHER flight in the list departed closest in time to it and
    does have route text -- same physical flight, just a data-linking quirk on
    AeroAPI's side, not a different mission."""
    if chosen.get('route'):
        return chosen
    chosen_off = _parse_iso(chosen.get('actual_off') or chosen.get('scheduled_off'))
    if chosen_off is None:
        return chosen
    candidates = [f for f in flights if f is not chosen and f.get('route')]
    if not candidates:
        return chosen
    def _dist(f):
        t = _parse_iso(f.get('actual_off') or f.get('scheduled_off'))
        return abs((chosen_off - t).total_seconds()) if t else float('inf')
    donor = min(candidates, key=_dist)
    if _dist(donor) > 3600:
        return chosen
    merged = dict(chosen)
    for field in ('route', 'filed_altitude', 'filed_airspeed', 'filed_ete'):
        if not merged.get(field):
            merged[field] = donor.get(field)
    return merged

def get_aeroapi_flight(tail):
    url = 'https://aeroapi.flightaware.com/aeroapi/flights/' + urllib.parse.quote(tail)
    data = http_get_json(url, headers={'x-apikey': AEROAPI_KEY})
    flights = data.get('flights', [])
    if not flights:
        return None
    f = pick_best_flight(flights)
    f = _borrow_route_if_missing(f, flights)
    return {
        'ident': f.get('ident'),
        'registration': f.get('registration'),
        'aircraft_type': f.get('aircraft_type'),
        'status': f.get('status'),
        'origin': f.get('origin'),
        'destination': f.get('destination'),
        'route': f.get('route'),
        'filed_altitude_ft': (f.get('filed_altitude') or 0) * 100,
        'filed_airspeed_kts': f.get('filed_airspeed'),
        'filed_ete_sec': f.get('filed_ete'),
        'scheduled_off': f.get('scheduled_off'),
        'estimated_off': f.get('estimated_off'),
        'actual_off': f.get('actual_off'),
        'scheduled_on': f.get('scheduled_on'),
        'actual_on': f.get('actual_on'),
        'fa_flight_id': f.get('fa_flight_id'),
    }

def _perp_dist(pt, a, b):
    """Perpendicular distance from pt to the line a-b, in the same lat/lon-ish
    units as the inputs (lon scaled by cos(lat) so it's roughly isotropic)."""
    mlat = math.cos(math.radians(a[1])) or 1e-9
    ax, ay = 0.0, 0.0
    bx, by = (b[2] - a[2]) * mlat, (b[1] - a[1])
    px, py = (pt[2] - a[2]) * mlat, (pt[1] - a[1])
    seg_len2 = bx * bx + by * by
    if seg_len2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (px * bx + py * by) / seg_len2))
    projx, projy = ax + t * bx, ay + t * by
    return math.hypot(px - projx, py - projy)

def _douglas_peucker(pts, epsilon):
    """Classic recursive line simplification: keeps points where the path
    actually curves, drops points that lie close to a straight line between
    their neighbors. Unlike uniform-stride decimation, this preserves detail
    through turns and thins hard on straight legs -- fixes the 'sometimes
    blocky, sometimes smooth' look reported 2026-08-07, which turned out to be
    the OLD stride-based thinning discarding points blind to curvature (worst
    exactly during the heavy maneuvering seen near Black Canyon/Widemouth 2)."""
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    max_dist, max_idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = _perp_dist(pts[i], a, b)
        if d > max_dist:
            max_dist, max_idx = d, i
    if max_dist > epsilon:
        left = _douglas_peucker(pts[:max_idx + 1], epsilon)
        right = _douglas_peucker(pts[max_idx:], epsilon)
        return left[:-1] + right
    return [a, b]

def get_aeroapi_track(fa_flight_id):
    """Full real position history for a flight (in-progress or completed) via
    AeroAPI's own track endpoint -- same one used to backfill N520NA's missed
    8/5 flight earlier tonight. Simplified with Douglas-Peucker (epsilon tuned
    in degrees, ~0.003 =~ 200m) rather than blind uniform-stride thinning, so
    turns stay smooth-looking regardless of total point count while straight
    legs get thinned hard. Capped at MAX_TRACK_PTS as a payload-size backstop
    (tightening epsilon if still over) -- normally never hit."""
    import datetime
    if not fa_flight_id:
        return []
    try:
        url = 'https://aeroapi.flightaware.com/aeroapi/flights/' + urllib.parse.quote(fa_flight_id) + '/track'
        data = http_get_json(url, headers={'x-apikey': AEROAPI_KEY})
        positions = data.get('positions', [])
        pts = []
        for p in positions:
            ts = p.get('timestamp')
            if not ts:
                continue
            epoch = int(datetime.datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ')
                        .replace(tzinfo=datetime.timezone.utc).timestamp())
            pts.append([epoch, p.get('latitude'), p.get('longitude'), round((p.get('altitude') or 0) * 100)])
        MAX_TRACK_PTS = 600
        epsilon = 0.003
        simplified = _douglas_peucker(pts, epsilon)
        while len(simplified) > MAX_TRACK_PTS and epsilon < 0.5:
            epsilon *= 2
            simplified = _douglas_peucker(pts, epsilon)
        return simplified
    except Exception:
        return []

_AIRPORT_COORDS_CACHE = {}

def get_airport_coords(icao):
    """Airport coordinates never change, so cache them permanently in-memory --
    every /api/plan request was re-fetching origin AND destination airport coords
    fresh every time (2 of the 4 AeroAPI calls per request), which was a real
    contributor to the 429s hit repeatedly 2026-08-07 tracking 3 simultaneous
    NASA aircraft on a 5-min poll. Restart the server to clear (rare enough not
    to matter -- airports aren't moving)."""
    if not icao:
        return None
    if icao in _AIRPORT_COORDS_CACHE:
        return _AIRPORT_COORDS_CACHE[icao]
    try:
        url = 'https://aeroapi.flightaware.com/aeroapi/airports/' + urllib.parse.quote(icao)
        data = http_get_json(url, headers={'x-apikey': AEROAPI_KEY})
        lat, lon = data.get('latitude'), data.get('longitude')
        if lat is not None and lon is not None:
            _AIRPORT_COORDS_CACHE[icao] = (lat, lon)
            return (lat, lon)
    except Exception:
        pass
    return None

def dest_point(lat, lon, bearing_deg, dist_nm):
    import math
    R = 3440.065
    br = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(dist_nm / R) + math.cos(lat1) * math.sin(dist_nm / R) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(dist_nm / R) * math.cos(lat1),
                              math.cos(dist_nm / R) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))

def extract_route_points(route_text):
    """Best-effort extra points from filed route text. Approximate (true-radial, no
    magnetic variation) -- good enough to widen the TFR/fire search box and give a rough
    plotted line, but NOT a substitute for real fix-by-fix plotting (that needs full FAA
    NASR navaid/fix data, not present here). Labeled clearly so the frontend can say so."""
    points = []
    if not route_text:
        return points
    for m in re.finditer(r'\b([A-Z]{3})(\d{3})(\d{3})\b', route_text):
        ident, radial, dist = m.group(1), int(m.group(2)), int(m.group(3))
        coords = get_airport_coords(ident) or get_airport_coords('K' + ident)
        if coords:
            lat, lon = dest_point(coords[0], coords[1], radial, dist)
            points.append({'lat': lat, 'lon': lon,
                            'label': ident + ' R-%03d/%d (approx, true radial)' % (radial, dist)})
    for m in re.finditer(r'(\d{3,4})([NS])/(\d{4,5})([EW])', route_text):
        lat_raw, ns, lon_raw, ew = m.groups()
        lat = int(lat_raw[:-2]) + int(lat_raw[-2:]) / 60.0
        lon = int(lon_raw[:-2]) + int(lon_raw[-2:]) / 60.0
        if ns == 'S':
            lat = -lat
        if ew == 'W':
            lon = -lon
        points.append({'lat': lat, 'lon': lon, 'label': m.group(0) + ' (literal coord in route)'})
    return points

def haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def point_to_segment_mi(plat, plon, alat, alon, blat, blon):
    """Approximate point-to-segment distance in miles using a local flat
    projection (accurate enough at the ~100mi scale this is used for)."""
    mlat = math.cos(math.radians(alat)) or 1e-9
    ax, ay = 0.0, 0.0
    bx, by = (blon - alon) * mlat, (blat - alat)
    px, py = (plon - alon) * mlat, (plat - alat)
    dx, dy = bx - ax, by - ay
    len2 = dx * dx + dy * dy
    t = 0.0 if len2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
    cx, cy = ax + t * dx, ay + t * dy
    clat, clon = alat + cy, alon + cx / mlat
    return haversine_mi(plat, plon, clat, clon)

def dist_to_route_mi(lat, lon, route):
    """Min distance in miles from a point to any segment of the route polyline(s).
    A (None, None) entry marks a break between independently-plotted line segments
    (e.g. filed route vs actual flown track) -- a segment must never bridge across
    a break. Concatenating two unrelated polylines end-to-end used to silently
    create a fake long diagonal 'flown' segment connecting the filed destination
    straight to wherever the actual track happened to start. Found 2026-08-08 on
    N520NA's KPVU->KBOI leg: that fake segment ran straight across Idaho/Oregon,
    sweeping in fires 80-100mi from Boise that had nothing to do with where the
    plane actually went or currently is."""
    valid_pts = [p for p in route if p[0] is not None]
    if not valid_pts:
        return float('inf')
    best = float('inf')
    for a, b in zip(route, route[1:]):
        if a[0] is None or b[0] is None:
            continue
        d = point_to_segment_mi(lat, lon, a[0], a[1], b[0], b[1])
        if d < best:
            best = d
    if best == float('inf'):
        best = min(haversine_mi(lat, lon, p[0], p[1]) for p in valid_pts)
    return best

def get_tfrs_near_route(route, corridor_mi):
    url = ('https://tfr.faa.gov/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature'
           '&typeName=TFR:V_TFR_LOC&maxFeatures=300&outputFormat=application/json&srsname=EPSG:4326')
    data = http_get_json(url)
    out = []
    for feat in data.get('features', []):
        geom = feat.get('geometry') or {}
        coords = geom.get('coordinates')
        if not coords:
            continue
        ring = coords[0] if geom.get('type') == 'Polygon' else None
        if not ring:
            continue
        # nearest ring point to the route line, within the corridor -- real
        # distance-to-route, not a loose bounding-box guess (2026-08-06, per Alex:
        # "only show fires and associated TFRs for 100 miles on other side of the
        # filed flight path")
        min_dist = min(dist_to_route_mi(p[1], p[0], route) for p in ring)
        if min_dist > corridor_mi:
            continue
        props = feat.get('properties', {})
        out.append({
            'id': props.get('NOTAM_KEY', '').split('-')[0],
            'name': props.get('TITLE', ''),
            'ring': [[p[1], p[0]] for p in ring[:-1]],  # drop closing dup, [lat,lon]
            'dist_mi': round(min_dist, 1),
        })
    return out

def build_mission_plan(tail):
    fl = get_aeroapi_flight(tail)
    if fl is None:
        return {'error': 'No AeroAPI flight record found for ' + tail}
    origin = fl.get('origin') or {}
    dest = fl.get('destination') or {}
    o_coords = get_airport_coords(origin.get('code_icao') or origin.get('code'))
    d_coords = get_airport_coords(dest.get('code_icao') or dest.get('code'))
    if o_coords:
        origin['latitude'], origin['longitude'] = o_coords
    if d_coords:
        dest['latitude'], dest['longitude'] = d_coords
    route_pts = extract_route_points(fl.get('route') or '')
    fl['route_points'] = route_pts
    actual_track = get_aeroapi_track(fl.get('fa_flight_id'))
    fl['actual_track'] = actual_track

    # ordered filed-plan line: origin -> route points -> destination.
    route_line = []
    if o_coords:
        route_line.append(o_coords)
    route_line.extend([(p['lat'], p['lon']) for p in route_pts])
    if d_coords:
        route_line.append(d_coords)

    # corridor centerline = filed route UNION actual flown track (2026-08-06, per
    # Alex: "100 miles on either side of flight plan and or actual path" -- a fire
    # near where it actually flew must count even if that deviated from the filed
    # line, same reasoning as the FCA/MSO/DTA hold points found off the straight
    # filed route on earlier missions this week). Downsample the actual track for
    # this distance-calc pass only (doesn't affect the full-resolution track baked
    # into the mission) -- O(points x TFR-ring-points) needs to stay bounded.
    actual_pts = [(p[1], p[2]) for p in actual_track]
    if len(actual_pts) > 60:
        step = max(1, len(actual_pts) // 60)
        actual_pts = actual_pts[::step]
    # (None, None) is a break marker, not a real point -- keeps dist_to_route_mi
    # from bridging a fake segment between the filed route and the actual track
    # (they're independent polylines, not one continuous flown path).
    BREAK = (None, None)
    corridor_line = route_line + ([BREAK] if route_line and actual_pts else []) + actual_pts
    if not corridor_line and (o_coords or d_coords):
        corridor_line = [c for c in [o_coords, d_coords] if c]

    CORRIDOR_MI = 100
    tfrs = []
    box = None
    if corridor_line:
        lats = [p[0] for p in corridor_line if p[0] is not None]
        lons = [p[1] for p in corridor_line if p[1] is not None]
        pad_lat = CORRIDOR_MI / 69.0  # ~69 mi/degree latitude
        pad_lon = CORRIDOR_MI / (69.0 * max(0.2, math.cos(math.radians(sum(lats) / len(lats)))))
        min_lat, max_lat = min(lats) - pad_lat, max(lats) + pad_lat
        min_lon, max_lon = min(lons) - pad_lon, max(lons) + pad_lon
        box = '%.4f,%.4f,%.4f,%.4f' % (min_lon, min_lat, max_lon, max_lat)
        try:
            tfrs = get_tfrs_near_route(corridor_line, CORRIDOR_MI)
        except Exception:
            tfrs = []
    fl['box'] = box
    fl['route_line'] = [[p[0], p[1]] for p in route_line]
    fl['corridor_route'] = [[p[0], p[1]] for p in corridor_line]
    fl['corridor_mi'] = CORRIDOR_MI
    fl['tfrs'] = tfrs
    return fl

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/plan':
            qs = urllib.parse.parse_qs(parsed.query)
            tail = (qs.get('tail') or [''])[0].strip().upper()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                if not tail:
                    result = {'error': 'No tail number provided'}
                elif not AEROAPI_KEY:
                    result = {'error': 'AEROAPI_KEY not configured in .env'}
                else:
                    result = build_mission_plan(tail)
            except Exception as e:
                result = {'error': str(e)}
            self.wfile.write(json.dumps(result).encode('utf-8'))
            return
        super().do_GET()

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

with ThreadingHTTPServer(("", PORT), QuietHandler) as httpd:
    httpd.serve_forever()
