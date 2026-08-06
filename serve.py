import http.server
import socketserver
import urllib.request
import urllib.parse
import json
import os
import re

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
    scheduled = [f for f in flights if (f.get('status') or '').startswith('Scheduled')]
    if scheduled:
        scheduled.sort(key=lambda f: f.get('scheduled_off') or '9999')
        return scheduled[0]
    arrived = [f for f in flights if f.get('actual_on')]
    if arrived:
        arrived.sort(key=lambda f: f.get('actual_on') or '', reverse=True)
        return arrived[0]
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

def get_tfrs_in_box(min_lon, min_lat, max_lon, max_lat):
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
        # bbox overlap check against the mission box
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        t_min_lon, t_max_lon, t_min_lat, t_max_lat = min(lons), max(lons), min(lats), max(lats)
        overlaps = not (t_max_lon < min_lon or t_min_lon > max_lon or t_max_lat < min_lat or t_min_lat > max_lat)
        if not overlaps:
            continue
        props = feat.get('properties', {})
        out.append({
            'id': props.get('NOTAM_KEY', '').split('-')[0],
            'name': props.get('TITLE', ''),
            'ring': [[p[1], p[0]] for p in ring[:-1]],  # drop closing dup, [lat,lon]
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
    pts = [c for c in [o_coords, d_coords] if c is not None]
    pts.extend([(p['lat'], p['lon']) for p in route_pts])
    fl['route_points'] = route_pts
    tfrs = []
    box = None
    if pts:
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        pad_lat, pad_lon = 1.0, 1.3
        min_lat, max_lat = min(lats) - pad_lat, max(lats) + pad_lat
        min_lon, max_lon = min(lons) - pad_lon, max(lons) + pad_lon
        box = '%.4f,%.4f,%.4f,%.4f' % (min_lon, min_lat, max_lon, max_lat)
        try:
            tfrs = get_tfrs_in_box(min_lon, min_lat, max_lon, max_lat)
        except Exception as e:
            tfrs = []
    fl['box'] = box
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
