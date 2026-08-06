import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import os
import re
import math

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

def pick_best_flight(flights):
    """AeroAPI's flights/{ident} list is NOT reliably 'current flight first' -- for a
    busy commercial tail it can return future-scheduled flights ahead of the one
    actually airborne right now. Found 2026-08-06 debugging N285SY: index 0 was a
    flight scheduled 3 days out while the real in-progress flight was buried at
    index 11. Priority: currently airborne > soonest upcoming scheduled > most
    recently arrived."""
    en_route = [f for f in flights if (f.get('status') or '').startswith('En Route')]
    if en_route:
        en_route.sort(key=lambda f: f.get('actual_off') or '', reverse=True)
        return en_route[0]
    # A flight that actually happened (has a real actual_on) is always more relevant
    # than a hypothetical future scheduled one -- fixed 2026-08-06 after QXE2154 landed
    # and the code jumped straight to a "Scheduled" flight for the NEXT DAY instead of
    # showing the one that had just landed minutes earlier.
    arrived = [f for f in flights if f.get('actual_on')]
    if arrived:
        arrived.sort(key=lambda f: f.get('actual_on') or '', reverse=True)
        return arrived[0]
    scheduled = [f for f in flights if (f.get('status') or '').startswith('Scheduled')]
    if scheduled:
        scheduled.sort(key=lambda f: f.get('scheduled_off') or '9999')
        return scheduled[0]
    return flights[0]

def get_aeroapi_flight(tail):
    url = 'https://aeroapi.flightaware.com/aeroapi/flights/' + urllib.parse.quote(tail)
    data = http_get_json(url, headers={'x-apikey': AEROAPI_KEY})
    flights = data.get('flights', [])
    if not flights:
        return None
    f = pick_best_flight(flights)
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

def get_aeroapi_track(fa_flight_id):
    """Full real position history for a flight (in-progress or completed) via
    AeroAPI's own track endpoint -- same one used to backfill N520NA's missed
    8/5 flight earlier tonight. Downsampled to ~300 points to keep the mission
    payload reasonable."""
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
        target = 300
        if len(pts) > target:
            step = max(1, len(pts) // target)
            pts = [p for i, p in enumerate(pts) if i % step == 0 or i == len(pts) - 1]
        return pts
    except Exception:
        return []

def get_airport_coords(icao):
    if not icao:
        return None
    try:
        url = 'https://aeroapi.flightaware.com/aeroapi/airports/' + urllib.parse.quote(icao)
        data = http_get_json(url, headers={'x-apikey': AEROAPI_KEY})
        lat, lon = data.get('latitude'), data.get('longitude')
        if lat is not None and lon is not None:
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
    """Min distance in miles from a point to any segment of the route polyline."""
    if len(route) < 2:
        return haversine_mi(lat, lon, route[0][0], route[0][1]) if route else float('inf')
    return min(point_to_segment_mi(lat, lon, a[0], a[1], b[0], b[1])
               for a, b in zip(route, route[1:]))

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
    corridor_line = route_line + actual_pts
    if not corridor_line and (o_coords or d_coords):
        corridor_line = [c for c in [o_coords, d_coords] if c]

    CORRIDOR_MI = 100
    tfrs = []
    box = None
    if corridor_line:
        lats = [p[0] for p in corridor_line]
        lons = [p[1] for p in corridor_line]
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
