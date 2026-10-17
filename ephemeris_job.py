"""
Dreamscape ephemeris job
------------------------
Runs every 3 hours (GitHub Actions). Every step is an idempotent upsert, so
re-runs are safe. Each run:

  1. (Re)computes natal charts for profiles whose natal_computed_at is null
  2. Fills the global ephemeris cache: today (UTC) + LOOKAHEAD_DAYS ahead
  3. Builds each user's local-day context (Chaldean rulers, Moon natal house)
     for their *own* time zone: local today + tomorrow
  4. Pulls NOAA Kp-index and GOES X-ray flares for the last 7 days
  5. Computes the exact-time sky + synastry for any dream log that lacks it

Env:
  SUPABASE_DB_URL   Postgres connection string (use the Session pooler URL)
  EPHE_PATH         folder with sepl_18.se1, semo_18.se1, seas_18.se1
"""

import os
import math
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
import swisseph as swe

# ---------------------------------------------------------------- config
DB_URL = os.environ["SUPABASE_DB_URL"]
swe.set_ephe_path(os.environ.get("EPHE_PATH", "./ephe"))

LOOKAHEAD_DAYS = 14          # future days cached for "Cosmic Clock" alerts later
TRANSIT_ORB = 5.0            # sky-to-sky aspects
SYNASTRY_ORB = 5.0           # transit-to-natal aspects
UTC = dt.timezone.utc

BODIES = {
    "sun": swe.SUN, "moon": swe.MOON, "mercury": swe.MERCURY, "venus": swe.VENUS,
    "mars": swe.MARS, "jupiter": swe.JUPITER, "saturn": swe.SATURN,
    "uranus": swe.URANUS, "neptune": swe.NEPTUNE, "pluto": swe.PLUTO,
    "chiron": swe.CHIRON, "true_node": swe.TRUE_NODE,
}
# deg/day below which a planet counts as stationed
STATION_SPEED = {"mercury": 0.10, "venus": 0.06, "mars": 0.04, "jupiter": 0.012,
                 "saturn": 0.008, "uranus": 0.004, "neptune": 0.003,
                 "pluto": 0.002, "chiron": 0.004}
SIGNS = ["aries", "taurus", "gemini", "cancer", "leo", "virgo", "libra",
         "scorpio", "sagittarius", "capricorn", "aquarius", "pisces"]
ASPECTS = {"conjunction": 0, "sextile": 60, "square": 90, "trine": 120, "opposition": 180}
PHASES = ["new_moon", "waxing_crescent", "first_quarter", "waxing_gibbous",
          "full_moon", "waning_gibbous", "last_quarter", "waning_crescent"]
CHALDEAN = ["saturn", "jupiter", "mars", "sun", "venus", "mercury", "moon"]
WEEKDAY_RULER = ["moon", "mars", "mercury", "jupiter", "venus", "saturn", "sun"]  # Mon..Sun
FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED
POINT_ORDER = list(BODIES) + ["asc", "mc", "dsc", "ic"]   # matches enum order (for a < b checks)


# ---------------------------------------------------------------- math helpers
def to_jd(t: dt.datetime) -> float:
    t = t.astimezone(UTC)
    return swe.julday(t.year, t.month, t.day,
                      t.hour + t.minute / 60 + t.second / 3600)


def from_jd(jd: float) -> dt.datetime:
    y, m, d, h = swe.revjul(jd)
    return dt.datetime(y, m, d, tzinfo=UTC) + dt.timedelta(hours=h)


def sign_of(lon: float):
    i = int(lon // 30) % 12
    return SIGNS[i], round(lon - i * 30, 5)


def sep(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def positions_at(jd: float) -> dict:
    out = {}
    for name, code in BODIES.items():
        xx, _ = swe.calc_ut(jd, code, FLAGS)
        lon, lat, speed = xx[0] % 360, xx[1], xx[3]
        sign, deg = sign_of(lon)
        out[name] = {
            "longitude": round(lon, 6), "latitude": round(lat, 6),
            "speed": round(speed, 6), "sign": sign, "degree_in_sign": deg,
            "retro": speed < 0 and name not in ("sun", "moon"),
            "stationary": abs(speed) < STATION_SPEED.get(name, 0),
        }
    return out


def aspect_between(lon_a, spd_a, lon_b, spd_b, orb_limit):
    """Return (aspect, orb, applying) or None. Applying = orb shrinking over next hour."""
    s_now = sep(lon_a, lon_b)
    s_next = sep(lon_a + spd_a / 24, lon_b + spd_b / 24)
    for name, angle in ASPECTS.items():
        orb = abs(s_now - angle)
        if orb <= orb_limit:
            return name, round(orb, 3), abs(s_next - angle) < orb
    return None


def moon_phase(sun_lon, moon_lon):
    elong = (moon_lon - sun_lon) % 360
    illum = (1 - math.cos(math.radians(elong))) / 2 * 100
    phase = PHASES[int(((elong + 22.5) % 360) // 45)]
    return round(elong, 3), round(illum, 2), phase


def house_of(lon, cusps):
    for i in range(12):
        start, end = cusps[i], cusps[(i + 1) % 12]
        if (lon - start) % 360 < (end - start) % 360:
            return i + 1
    return 12


def next_event(jd_start, lat, lon, rise=True):
    rsmi = swe.CALC_RISE if rise else swe.CALC_SET
    res, tret = swe.rise_trans(jd_start, swe.SUN, rsmi, (float(lon), float(lat), 0.0))
    return tret[0] if res == 0 else None   # None = polar day/night


def planetary_hours(local_date, lat, lon, tz):
    """Chaldean planetary hours from sunrise to next sunrise."""
    midnight = dt.datetime.combine(local_date, dt.time(0), ZoneInfo(tz))
    rise = next_event(to_jd(midnight), lat, lon, True)
    if rise is None:
        return None
    sset = next_event(rise, lat, lon, False)
    rise2 = next_event(sset, lat, lon, True)
    day_ruler = WEEKDAY_RULER[local_date.weekday()]
    start_idx = CHALDEAN.index(day_ruler)
    hours, d_len, n_len = [], (sset - rise) / 12, (rise2 - sset) / 12
    for n in range(24):
        is_night = n >= 12
        s = rise + n * d_len if not is_night else sset + (n - 12) * n_len
        e = s + (n_len if is_night else d_len)
        hours.append({"n": n + 1, "start": from_jd(s).isoformat(),
                      "end": from_jd(e).isoformat(),
                      "ruler": CHALDEAN[(start_idx + n) % 7], "is_night": is_night})
    return {"sunrise": from_jd(rise), "sunset": from_jd(sset), "next_sunrise": from_jd(rise2),
            "day_ruler": day_ruler, "night_ruler": hours[12]["ruler"], "hours": hours}


def hour_ruler_at(when_utc, lat, lon, tz):
    """Planetary hour ruler at an instant. Before sunrise belongs to the previous day."""
    local = when_utc.astimezone(ZoneInfo(tz)).date()
    for d in (local, local - dt.timedelta(days=1)):
        ph = planetary_hours(d, lat, lon, tz)
        if not ph:
            return None, None
        for h in ph["hours"]:
            if parse_ts(h["start"]) <= when_utc < parse_ts(h["end"]):
                return ph["day_ruler"], h["ruler"]
    return None, None


# ---------------------------------------------------------------- 1. natal
def compute_natal(cur, p):
    tz = ZoneInfo(p["birth_tz"])
    btime = p["birth_time"] or dt.time(12, 0)          # noon chart if unknown
    born = dt.datetime.combine(p["birth_date"], btime, tz)
    jd = to_jd(born)
    pos = positions_at(jd)
    cusps, ascmc = swe.houses(jd, float(p["birth_lat"]), float(p["birth_lon"]),
                              p["house_system"].encode())
    cusps = list(cusps)[:12]
    angles = {"asc": ascmc[0], "mc": ascmc[1],
              "dsc": (ascmc[0] + 180) % 360, "ic": (ascmc[1] + 180) % 360}

    points = {k: {"longitude": v["longitude"], "speed": v["speed"], "retro": v["retro"]}
              for k, v in pos.items()}
    if p["birth_time_known"]:
        for k, v in angles.items():
            points[k] = {"longitude": round(v, 6), "speed": 0.0, "retro": False}

    uid = p["user_id"]
    for t in ("natal_points", "natal_house_cusps", "natal_aspects"):
        cur.execute(f"delete from public.{t} where user_id = %s", (uid,))

    for name, v in points.items():
        sign, deg = sign_of(v["longitude"])
        house = house_of(v["longitude"], cusps) if p["birth_time_known"] else None
        cur.execute("""insert into public.natal_points
            (user_id, point, longitude, sign, degree_in_sign, house, is_retrograde)
            values (%s,%s,%s,%s,%s,%s,%s)""",
            (uid, name, v["longitude"], sign, deg, house, v["retro"]))

    if p["birth_time_known"]:
        for i, c in enumerate(cusps, 1):
            cur.execute("""insert into public.natal_house_cusps (user_id, house, longitude, sign)
                values (%s,%s,%s,%s)""", (uid, i, round(c, 6), sign_of(c)[0]))

    names = sorted(points, key=POINT_ORDER.index)
    natal_aspects = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            hit = aspect_between(points[a]["longitude"], 0, points[b]["longitude"], 0, 8.0)
            if hit:
                natal_aspects.append({"a": a, "b": b, "aspect": hit[0], "orb": hit[1]})
                cur.execute("""insert into public.natal_aspects
                    (user_id, point_a, point_b, aspect, orb) values (%s,%s,%s,%s,%s)""",
                    (uid, a, b, hit[0], hit[1]))

    chart = {"computed_for_utc": born.astimezone(UTC).isoformat(),
             "house_system": p["house_system"], "birth_time_known": p["birth_time_known"],
             "points": points, "cusps": cusps if p["birth_time_known"] else None,
             "aspects": natal_aspects}
    cur.execute("""update public.profiles set natal_chart = %s, natal_computed_at = now()
                   where user_id = %s""", (Json(chart), uid))


# ---------------------------------------------------------------- 2. global ephemeris
def fill_ephemeris(cur, day: dt.date):
    instant = dt.datetime.combine(day, dt.time(0), UTC)
    pos = positions_at(to_jd(instant))
    elong, illum, phase = moon_phase(pos["sun"]["longitude"], pos["moon"]["longitude"])
    cur.execute("""
        insert into public.ephemeris_daily
          (day, computed_for_utc, sun_moon_elongation, moon_illumination, moon_phase,
           moon_longitude, moon_sign, engine_version)
        values (%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (day) do update set
          sun_moon_elongation = excluded.sun_moon_elongation,
          moon_illumination = excluded.moon_illumination, moon_phase = excluded.moon_phase,
          moon_longitude = excluded.moon_longitude, moon_sign = excluded.moon_sign,
          engine_version = excluded.engine_version, computed_at = now()""",
        (day, instant, elong, illum, phase, pos["moon"]["longitude"],
         pos["moon"]["sign"], swe.version))

    for body, v in pos.items():
        cur.execute("""
            insert into public.ephemeris_positions
              (day, body, longitude, latitude, speed_deg_day, sign, degree_in_sign,
               is_retrograde, is_stationary)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (day, body) do update set
              longitude = excluded.longitude, latitude = excluded.latitude,
              speed_deg_day = excluded.speed_deg_day, sign = excluded.sign,
              degree_in_sign = excluded.degree_in_sign,
              is_retrograde = excluded.is_retrograde, is_stationary = excluded.is_stationary""",
            (day, body, v["longitude"], v["latitude"], v["speed"], v["sign"],
             v["degree_in_sign"], v["retro"], v["stationary"]))

    cur.execute("delete from public.ephemeris_aspects where day = %s", (day,))
    names = list(BODIES)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            hit = aspect_between(pos[a]["longitude"], pos[a]["speed"],
                                 pos[b]["longitude"], pos[b]["speed"], TRANSIT_ORB)
            if hit:
                cur.execute("""insert into public.ephemeris_aspects
                    (day, body_a, body_b, aspect, orb, is_applying)
                    values (%s,%s,%s,%s,%s,%s)""", (day, a, b, *hit))


# ---------------------------------------------------------------- 3. per-user local day
def fill_user_day(cur, p, cusps, local_date):
    lat, lon, tz = float(p["current_lat"]), float(p["current_lon"]), p["current_tz"]
    ph = planetary_hours(local_date, lat, lon, tz)
    if ph is None:
        return
    moon = positions_at(to_jd(ph["sunrise"]))["moon"]
    house = house_of(moon["longitude"], cusps) if cusps else None
    cur.execute("""
        insert into public.user_daily_context
          (user_id, local_date, tz, sunrise, sunset, next_sunrise, day_ruler, night_ruler,
           planetary_hours, moon_sign_at_sunrise, moon_natal_house)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (user_id, local_date) do update set
          tz = excluded.tz, sunrise = excluded.sunrise, sunset = excluded.sunset,
          next_sunrise = excluded.next_sunrise, day_ruler = excluded.day_ruler,
          night_ruler = excluded.night_ruler, planetary_hours = excluded.planetary_hours,
          moon_sign_at_sunrise = excluded.moon_sign_at_sunrise,
          moon_natal_house = excluded.moon_natal_house, computed_at = now()""",
        (p["user_id"], local_date, tz, ph["sunrise"], ph["sunset"], ph["next_sunrise"],
         ph["day_ruler"], ph["night_ruler"], Json(ph["hours"]), moon["sign"], house))


# ---------------------------------------------------------------- 4. NOAA space weather
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
FLARE_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json"
FLARE_RANK = {"A": 0, "B": 1, "C": 2, "M": 3, "X": 4}


def flare_score(c):
    try:
        return FLARE_RANK[c[0].upper()] * 100 + float(c[1:] or 0)
    except (KeyError, ValueError, IndexError):
        return -1


def parse_ts(s):
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def fill_space_weather(cur):
    by_day = {}
    kp_rows = requests.get(KP_URL, timeout=30).json()
    if kp_rows and isinstance(kp_rows[0], list):          # header-row format
        hdr = kp_rows[0]
        kp_rows = [dict(zip(hdr, r)) for r in kp_rows[1:]]
    for r in kp_rows:
        t = parse_ts(r.get("time_tag"))
        kp = float(r.get("Kp") or r.get("kp_index") or 0)
        by_day.setdefault(t.date(), {"kp": [], "flares": []})["kp"].append(
            {"time_tag": t.isoformat(), "kp": kp})

    for f in requests.get(FLARE_URL, timeout=30).json():
        cls = f.get("max_class") or f.get("current_class")
        tm = f.get("max_time") or f.get("begin_time")
        if not cls or not tm:
            continue
        t = parse_ts(tm)
        by_day.setdefault(t.date(), {"kp": [], "flares": []})["flares"].append(
            {"class": cls, "max_time": t.isoformat()})

    for day, d in by_day.items():
        kps = [k["kp"] for k in d["kp"]]
        kp_max = max(kps) if kps else None
        g = None if kp_max is None else max(0, min(5, int(kp_max) - 4))
        top = max(d["flares"], key=lambda x: flare_score(x["class"]), default=None)
        cur.execute("""
            insert into public.environment_daily
              (day, kp_max, kp_mean, kp_readings, g_storm_level,
               flare_count, flare_max_class, flare_max_time, flares)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (day) do update set
              kp_max = excluded.kp_max, kp_mean = excluded.kp_mean,
              kp_readings = excluded.kp_readings, g_storm_level = excluded.g_storm_level,
              flare_count = excluded.flare_count, flare_max_class = excluded.flare_max_class,
              flare_max_time = excluded.flare_max_time, flares = excluded.flares,
              updated_at = now()""",
            (day, kp_max, round(sum(kps) / len(kps), 2) if kps else None, Json(d["kp"]), g,
             len(d["flares"]), top["class"] if top else None,
             top["max_time"] if top else None, Json(d["flares"])))


# ---------------------------------------------------------------- 5. dream snapshots
def snapshot_dream(cur, log, p, natal, cusps):
    wake = log["wake_time"]
    pos = positions_at(to_jd(wake))
    _, illum, phase = moon_phase(pos["sun"]["longitude"], pos["moon"]["longitude"])
    day_ruler, hour_ruler = hour_ruler_at(wake, float(p["current_lat"]),
                                          float(p["current_lon"]), p["current_tz"])
    cur.execute("""select kp_readings from public.environment_daily where day = %s""",
                (wake.astimezone(UTC).date(),))
    row = cur.fetchone()
    kp = None
    if row and row["kp_readings"]:
        before = [k for k in row["kp_readings"] if parse_ts(k["time_tag"]) <= wake]
        kp = before[-1]["kp"] if before else None

    cur.execute("""insert into public.dream_sky
        (log_id, computed_for_utc, moon_phase, moon_illumination, moon_sign,
         moon_natal_house, day_ruler, planetary_hour_ruler, kp_at_wake)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (log["log_id"], wake, phase, illum, pos["moon"]["sign"],
         house_of(pos["moon"]["longitude"], cusps) if cusps else None,
         day_ruler, hour_ruler, kp))

    for body, v in pos.items():
        cur.execute("""insert into public.dream_transit_positions
            (log_id, body, longitude, sign, degree_in_sign, is_retrograde, natal_house)
            values (%s,%s,%s,%s,%s,%s,%s)""",
            (log["log_id"], body, v["longitude"], v["sign"], v["degree_in_sign"], v["retro"],
             house_of(v["longitude"], cusps) if cusps else None))

    for tbody, tv in pos.items():
        for npoint, nv in natal.items():
            hit = aspect_between(tv["longitude"], tv["speed"], float(nv["longitude"]), 0,
                                 SYNASTRY_ORB)
            if not hit:
                continue
            nh = nv["house"]
            # 8th/12th flag: natal point sits in that house, or transit is on that cusp
            cusp8 = bool(cusps) and sep(tv["longitude"], cusps[7]) <= SYNASTRY_ORB
            cusp12 = bool(cusps) and sep(tv["longitude"], cusps[11]) <= SYNASTRY_ORB
            cur.execute("""insert into public.dream_synastry_hits
                (log_id, transit_body, natal_point, aspect, orb, is_applying,
                 natal_point_house, hits_8th_house, hits_12th_house)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing""",
                (log["log_id"], tbody, npoint, *hit, nh, nh == 8 or cusp8, nh == 12 or cusp12))


# ---------------------------------------------------------------- main
def main():
    today = dt.datetime.now(UTC).date()
    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 1. natal
            cur.execute("select * from public.profiles where natal_computed_at is null")
            for p in cur.fetchall():
                compute_natal(cur, p)
            conn.commit()

            # 2. global sky (yesterday through lookahead)
            for i in range(-1, LOOKAHEAD_DAYS + 1):
                fill_ephemeris(cur, today + dt.timedelta(days=i))
            conn.commit()

            # 4. space weather (before dreams, so kp_at_wake can resolve)
            try:
                fill_space_weather(cur)
                conn.commit()
            except Exception as e:                       # NOAA outage shouldn't block the rest
                conn.rollback()
                print(f"space weather skipped: {e}")

            # 3 + 5. per user
            cur.execute("select * from public.profiles where natal_computed_at is not null")
            for p in cur.fetchall():
                uid = p["user_id"]
                cur.execute("select house, longitude from public.natal_house_cusps "
                            "where user_id = %s order by house", (uid,))
                cusps = [float(r["longitude"]) for r in cur.fetchall()] or None
                cur.execute("select point, longitude, house from public.natal_points "
                            "where user_id = %s", (uid,))
                natal = {r["point"]: r for r in cur.fetchall()}

                local_today = dt.datetime.now(ZoneInfo(p["current_tz"])).date()
                for d in (local_today, local_today + dt.timedelta(days=1)):
                    fill_user_day(cur, p, cusps, d)

                cur.execute("""select d.log_id, d.wake_time from public.dream_logs d
                               left join public.dream_sky s using (log_id)
                               where d.user_id = %s and s.log_id is null""", (uid,))
                for log in cur.fetchall():
                    snapshot_dream(cur, log, p, natal, cusps)
                conn.commit()


if __name__ == "__main__":
    main()
