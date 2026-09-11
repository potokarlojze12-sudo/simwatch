"""
trip.py - how much does it actually cost to go get the thing?

Turns a listing's town name into a driving distance from home, then into a
fuel bill using current Slovenian pump prices. Everything is cached in the
same sqlite file so repeated runs don't hammer the free services.
"""

import os
import re
import json
import time
import math
import requests

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

# Your home. Kept out of the file if HOME_LAT / HOME_LNG are set as
# environment variables - do that before pushing this to a public repo,
# otherwise your address ships with the code.
HOME_LAT = float(os.environ.get("HOME_LAT", 45.935833))   # 45°56'09.0"N
HOME_LNG = float(os.environ.get("HOME_LNG", 14.589500))   # 14°35'22.2"E

CONSUMPTION_L_PER_100 = float(os.environ.get("CONSUMPTION_L_PER_100", 5.9))
FUEL_TYPE = os.environ.get("FUEL_TYPE", "dizel")  # "95","dizel","98","avtoplin-lpg","hvo"
MAX_ONE_WAY_KM = float(os.environ.get("MAX_ONE_WAY_KM", 150))

# Extra round-trip cost when leaving the country (Croatian motorway toll,
# roughly Bregana-Zagreb and back). Set to 0 if you take back roads.
CROSS_BORDER_EXTRA_EUR = 15.0

# Only look at pumps within this radius of home for the price baseline -
# you'd fill up near home, not at the destination.
FUEL_RADIUS_KM = 25
FUEL_CACHE_HOURS = 12

# Motorway pumps were deregulated in March 2026 and now run 10-15% dearer than
# everywhere else. You wouldn't fill up on the A2 for a local errand, so they
# are left out of the price baseline. Flip to False to include them.
EXCLUDE_MOTORWAY = True
FALLBACK_PRICE = {"95": 1.45, "dizel": 1.55, "98": 1.60, "hvo": 1.70,
                  "avtoplin-lpg": 0.85}

GORIVA_LIVE = "https://goriva.si/api/v1/search/?format=json"
GORIVA_SNAPSHOT = ("https://raw.githubusercontent.com/stefanb/goriva-data/"
                   "master/data/search.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"

UA = {"User-Agent": "simwatch/1.0 (personal listing watcher; low volume)"}

COUNTRY_HINT = {"bolha": "Slovenia", "salomon": "Slovenia", "njuskalo": "Croatia"}


# ----------------------------------------------------------------------


def init(con):
    con.execute("CREATE TABLE IF NOT EXISTS geo ("
                " place TEXT PRIMARY KEY, lat REAL, lng REAL, km REAL, ts INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts INTEGER)")
    con.commit()


def haversine(lat1, lng1, lat2, lng2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ----------------------------------------------------------------------
# fuel price
# ----------------------------------------------------------------------


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _price_from_stations(stations):
    """Median price at pumps near home for the configured fuel."""
    near = []
    for s in stations:
        lat, lng = s.get("lat"), s.get("lng")
        if not lat or not lng or not (45 <= lat <= 47):
            continue
        if haversine(HOME_LAT, HOME_LNG, lat, lng) > FUEL_RADIUS_KM:
            continue
        if EXCLUDE_MOTORWAY and (s.get("direction") or "").strip():
            continue
        p = (s.get("prices") or {}).get(FUEL_TYPE)
        if p:
            near.append(float(p))
    # if nothing nearby, fall back to the whole country
    if len(near) < 3:
        near = [float((s.get("prices") or {}).get(FUEL_TYPE)) for s in stations
                if (s.get("prices") or {}).get(FUEL_TYPE)
                and not (EXCLUDE_MOTORWAY and (s.get("direction") or "").strip())]
    return _median(near), len(near)


def fuel_price(con):
    """Current EUR/litre. Live goriva.si first, its GitHub mirror second,
    a hardcoded number last so the bot never dies over a fuel lookup."""
    row = con.execute("SELECT v, ts FROM cache WHERE k='fuel'").fetchone()
    if row and time.time() - row[1] < FUEL_CACHE_HOURS * 3600:
        d = json.loads(row[0])
        return d["price"], d["source"]

    price, source = None, "fallback"
    for url, label in ((GORIVA_LIVE, "goriva.si"), (GORIVA_SNAPSHOT, "goriva.si mirror")):
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            data = r.json()
            stations = data.get("results", data if isinstance(data, list) else [])
            p, n = _price_from_stations(stations)
            if p:
                price, source = p, f"{label} ({n} pumps)"
                break
        except Exception as e:
            print(f"  fuel lookup via {label} failed: {e}")

    if price is None:
        price = FALLBACK_PRICE.get(FUEL_TYPE, 1.50)

    con.execute("INSERT OR REPLACE INTO cache VALUES ('fuel', ?, ?)",
                (json.dumps({"price": price, "source": source}), int(time.time())))
    con.commit()
    return price, source


# ----------------------------------------------------------------------
# where is it
# ----------------------------------------------------------------------

NOISE = re.compile(r"\b(okolica|blizu|center|centru|pri|pošta|poštni|postni|"
                   r"p\.?e\.?|mesto in okolica)\b", re.I)


def clean_place(place):
    place = NOISE.sub(" ", place or "")
    place = re.split(r"[,/(]", place)[0]
    return " ".join(place.split())[:60]


def geocode(con, place, site):
    """Town name -> coordinates + road distance from home. Cached forever;
    towns don't move."""
    place = clean_place(place)
    if not place or len(place) < 2:
        return None

    country = COUNTRY_HINT.get(site, "Slovenia")
    key = f"{country}|{place.lower()}"
    row = con.execute("SELECT lat, lng, km FROM geo WHERE place=?", (key,)).fetchone()
    if row:
        return {"lat": row[0], "lng": row[1], "km": row[2], "place": place}

    try:
        time.sleep(1.1)  # Nominatim asks for max 1 request/second
        r = requests.get(NOMINATIM, headers=UA, timeout=30, params={
            "q": f"{place}, {country}", "format": "json", "limit": 1})
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        print(f"  geocode failed for {place}: {e}")
        return None

    if not hits:
        con.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?)",
                    (key, None, None, None, int(time.time())))
        con.commit()
        return None

    lat, lng = float(hits[0]["lat"]), float(hits[0]["lon"])
    km = road_km(lat, lng)
    con.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?)",
                (key, lat, lng, km, int(time.time())))
    con.commit()
    return {"lat": lat, "lng": lng, "km": km, "place": place}


def road_km(lat, lng):
    """Real driving distance if OSRM answers, otherwise straight line with a
    winding factor - roads are never straight, especially not here."""
    try:
        url = f"{OSRM}/{HOME_LNG},{HOME_LAT};{lng},{lat}?overview=false"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        routes = r.json().get("routes") or []
        if routes:
            return routes[0]["distance"] / 1000.0
    except Exception:
        pass
    return haversine(HOME_LAT, HOME_LNG, lat, lng) * 1.3


# ----------------------------------------------------------------------
# the number you actually care about
# ----------------------------------------------------------------------


def commute(con, place, site):
    """
    Returns None if the location is unknown, or a dict:
      km_one_way, km_round, litres, fuel_eur, extra_eur, total_eur,
      too_far (bool), price_per_litre, source, place
    """
    loc = geocode(con, place, site)
    if not loc or loc["km"] is None:
        return None

    price, source = fuel_price(con)
    km_round = loc["km"] * 2
    litres = km_round * CONSUMPTION_L_PER_100 / 100.0
    fuel_eur = litres * price
    extra = CROSS_BORDER_EXTRA_EUR if COUNTRY_HINT.get(site) == "Croatia" else 0.0

    return {
        "place": loc["place"],
        "km_one_way": loc["km"],
        "km_round": km_round,
        "litres": litres,
        "fuel_eur": fuel_eur,
        "extra_eur": extra,
        "total_eur": fuel_eur + extra,
        "too_far": loc["km"] > MAX_ONE_WAY_KM,
        "price_per_litre": price,
        "source": source,
    }
