#!/usr/bin/env python3
"""Print the touring-outlook prompt filled with today's forecast data.

Print the prompt:
    python3 touring_prompt.py > prompt.txt
Run it on a local Ollama model and print clean reasoning + answer:
    python3 touring_prompt.py --ollama gpt-oss:20b --think high > out.txt
"""
from datetime import datetime, timedelta
from pathlib import Path

import yaml

import snow_email as se
import snow_surface

HERE = Path(__file__).parent
STATION_FT = 9662  # Collins (CLN)
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
ASPECT_WORDS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
                "S": "south", "SW": "southwest", "W": "west", "NW": "northwest"}


def fmt_time(t):
    return f"{t:%a} {t.hour % 12 or 12} {'am' if t.hour < 12 else 'pm'}"


def periods(rows):
    """Group consecutive hours into readable spans, e.g. 'Thu 1 pm to 2 pm, Thu 10 pm to 11 pm'."""
    spans, cur = [], [rows[0]]
    for r in rows[1:]:
        if r["time"] - cur[-1]["time"] == timedelta(hours=1):
            cur.append(r)
        else:
            spans.append(cur)
            cur = [r]
    spans.append(cur)
    out = []
    for sp in spans:
        w = sum(r["water"] for r in sp)
        a, b = sp[0]["time"], sp[-1]["time"]
        span = fmt_time(a) if a == b else f"{fmt_time(a)} to {fmt_time(b).split(' ', 1)[1]}"
        out.append(f"{span}: {w:.2f} in")
    return "; ".join(out)


def lee_aspects(directions):
    """Average wind direction (16-point) -> lee aspects in words, e.g. 'north to northeast'."""
    import math
    xs = [math.radians(COMPASS.index(d) * 22.5) for d in directions if d in COMPASS]
    if not xs:
        return None, None
    ang = math.degrees(math.atan2(sum(map(math.sin, xs)), sum(map(math.cos, xs)))) % 360
    wind = COMPASS[round(ang / 22.5) % 16]
    lee = (ang + 180) % 360
    # Lee aspects: the 8-point directions within 45 degrees of straight downwind
    names = [n for i, n in enumerate(["N", "NE", "E", "SE", "S", "SW", "W", "NW"])
             if min(abs(i * 45 - lee), 360 - abs(i * 45 - lee)) <= 45]
    return wind, " and ".join(ASPECT_WORDS[n] for n in names)


def sky_daily(lat, lon, elev_ft):
    """{date: (mean cloud cover %, hours of sunshine)} from Open-Meteo."""
    import requests
    r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=30, params={
        "latitude": lat, "longitude": lon, "elevation": round(elev_ft * 0.3048),
        "daily": "cloud_cover_mean,sunshine_duration", "timezone": "America/Denver",
        "forecast_days": 10})
    r.raise_for_status()
    d = r.json()["daily"]
    return {datetime.fromisoformat(t).date(): (c, (sun or 0) / 3600)
            for t, c, sun in zip(d["time"], d["cloud_cover_mean"], d["sunshine_duration"])
            if c is not None}


# Snow depth (inches, settled) needed to ski. Below NOT_SKIABLE_IN skis hit dirt and rocks
# everywhere; below THIN_IN only smooth, grassy, low-angle slopes work.
NOT_SKIABLE_IN = 8
THIN_IN = 20
SETTLE = 0.6  # new snow settles to roughly 60% of its fresh depth within a couple of days


def day_calls(now, days, temps, sky, depth=None, recent_snow=False):
    """Pick the kind of terrain for each day with simple rules, so the model only writes it up.

    depth: snow on the ground now (inches), mid-mountain if known.
    recent_snow: True if snow fell in the last few days (old surface starts as powder, not crust).
    Returns lines like 'Sat Oct 24: POWDER DAY - ... SURFACE: ...'.
    """
    p50 = days.get("P50", {})
    order = sorted(p50)
    surf = snow_surface.evolve(
        [(d, p50[d], *temps.get(d, (None, None)), sky.get(d, (None, None))[0]) for d in order],
        start="settled" if recent_snow else "crust")
    out, on_ground, since_storm = [], float(depth or 0), 99
    for d in order:
        new = p50[d]
        hi, lo = temps.get(d, (None, None))
        cloud = sky.get(d, (None, None))[0]
        on_ground += SETTLE * new
        if hi is not None and hi >= 40 and on_ground < THIN_IN:
            on_ground = max(on_ground - 1.5, 0)  # thin snow melts fast on warm days
        since_storm = 0 if new >= 2 else since_storm + 1
        stormy = new >= 2 or (cloud is not None and cloud >= 75)
        s = surf[d]
        shady, sunny = s["shady"], s["sunny"]

        sky_txt = f"{cloud:.0f}% cloud cover" if cloud is not None else "cloud cover n/a"
        t_txt = f"high {hi:.0f} F / low {lo:.0f} F" if hi is not None else "temps n/a"
        info = (f"({new:.1f} in new snow, about {on_ground:.0f} in on the ground, {t_txt}, "
                f"{sky_txt}, {s['sun'].lower()} sun for the time of year)")

        if on_ground < NOT_SKIABLE_IN:
            call = (f"NOT SKIABLE - only about {on_ground:.0f} in on the ground, so skis would hit "
                    "dirt and rocks everywhere. Do not suggest any terrain. "
                    + ("this snow is building the base. " if new >= 2 else "")
                    + "Hike, wax skis, or wait for more snow.")
        elif stormy and new >= 2:
            call = ("STORM DAY - snowing with flat light; ski low-angle trees where you can see, "
                    "stay out of big open terrain.")
        elif stormy:
            call = ("GRAY DAY - cloudy and flat light; trees and low-angle glades give the best "
                    "visibility.")
        elif sunny == "corn" and on_ground >= THIN_IN:
            call = ("CORN DAY - sunny freeze-thaw; ski open sunny slopes as they soften: east-facing "
                    "about 9-11 am, south-facing about 10 am-noon, west-facing about noon-2 pm. "
                    "Move off them once you sink past your boot tops.")
        elif shady in snow_surface.POWDER and since_storm == 1:
            call = ("POWDER DAY - fresh snow and sunshine; shady north-facing low-angle slopes and "
                    "trees hold the best snow.")
        elif shady in snow_surface.POWDER and snow_surface.SCORE[shady] > snow_surface.SCORE[sunny]:
            call = ("SHADY SLOPES - the soft snow is hiding on shady north-facing slopes; "
                    "skip the sunny slopes.")
        elif max(snow_surface.SCORE[shady], snow_surface.SCORE[sunny]) <= 1:
            call = ("CRUSTY - no soft snow on any aspect; a fitness tour or rest day. Wait for the "
                    "next storm to reset the surface.")
        else:
            best = "shady north-facing" if snow_surface.SCORE[shady] >= snow_surface.SCORE[sunny] \
                else "sunny south-facing"
            call = f"MIXED - the best snow is on {best} slopes."
        if NOT_SKIABLE_IN <= on_ground < THIN_IN:
            call += (" THIN COVER: only smooth, grassy, low-angle slopes; sharks in the water "
                     "everywhere else.")
        if on_ground >= NOT_SKIABLE_IN:
            call += (f" SURFACE: shady north-facing slopes: {snow_surface.describe(s, 'shady')}; "
                     f"sunny south-facing slopes: {snow_surface.describe(s, 'sunny')}.")
        out.append(f"{d:%a %b} {d.day}: {call} {info}")
    return out


def key_facts(now, days, temps, rows, depth=None, base=None):
    """Pre-computed facts so the model doesn't have to derive them."""
    f = []
    early = now.month in (10, 11) and (now.month == 10 or now.day <= 20)
    sun = snow_surface.sun_class(now.date())
    f.append(f"Time of year: {sun.lower()} sun. " + {
        "LOW": "Midwinter sun barely touches shady slopes, and sunny slopes only soften on warm days.",
        "MODERATE": "The sun is low but still bakes sunny slopes on clear days, so they crust up "
                    "while shady slopes stay soft.",
        "STRONG": "The sun works sunny slopes hard on any clear day: crusts, then corn once the "
                  "nights refreeze.",
        "VERY STRONG": "Spring sun softens almost every aspect; timing matters more than aspect.",
    }[sun])
    if depth is not None:
        b = f" ({base:.0f} in near the base)" if base is not None and base != depth else ""
        f.append(f"Season: {'early season' if early else 'winter'}. Snow on the ground right now: "
                 f"about {depth:.0f} in mid-mountain{b}. Snowpack structure "
                 "(weak layers) is not in the data (unknown).")
    elif early:
        f.append("Season: early season. Before this storm the ground is likely bare or nearly bare, "
                 "so days before the storm have little or nothing to ski. Existing snowpack depth "
                 "is not in the data (unknown).")
    else:
        f.append("Season: winter. Existing snowpack depth and structure are not in the data (unknown).")
    f.append(f"Elevations: Collins station {STATION_FT:,} ft; wind measured at Top of Collins 10,443 ft.")

    if rows:
        wet = [r for r in rows if r["water"] > 0]
        rain = [r for r in wet if not r["slr"]]
        snow = [r for r in rows if r["slr"] and r["water"] > 0]
        if wet:
            f.append(f"Precipitation in the next {len(rows)} hours starts {fmt_time(wet[0]['time'])} "
                     f"and totals {sum(r['water'] for r in rows):.2f} in of water.")
        else:
            f.append(f"No precipitation in the next {len(rows)} hours.")
        if rain:
            f.append(f"Rain: {sum(r['water'] for r in rain):.2f} in of water falls as rain "
                     f"({periods(rain)}).")
        if rain and snow and snow[0]["time"] > rain[0]["time"]:
            r = snow[0]
            f.append(f"Rain turns to snow around {fmt_time(r['time'])}. The rain-wetted surface "
                     "will freeze into a crust under the new snow.")
        if snow:
            f.append(f"Snow in the hourly forecast: {sum(r['snow'] for r in snow):.1f} in between "
                     f"{fmt_time(snow[0]['time'])} and {fmt_time(snow[-1]['time'])}, "
                     f"snow-to-liquid ratio {min(r['slr'] for r in snow)}:1 to "
                     f"{max(r['slr'] for r in snow)}:1 (dense, wet-ish snow below about 10:1).")
            # Winds while snow is falling and the 6 hours after
            end = snow[-1]["time"] + timedelta(hours=6)
            during = [r for r in rows if snow[0]["time"] <= r["time"] <= end]
            wind, lee = lee_aspects([r["wind_dir"] for r in during])
            if wind:
                f.append(f"Wind during and right after the snow: from the {wind}, "
                         f"{min(r['wind'] for r in during)} to {max(r['wind'] for r in during)} mph, "
                         f"gusting to {max(r['gust'] or 0 for r in during)} mph. Lee aspects where "
                         f"snow will drift: {lee}.")
        temps_h = [r["temp"] for r in rows]
        below = next((r for r in rows if r["temp"] <= 32), None)
        f.append(f"Hourly temperature at Collins ranges {min(temps_h)} to {max(temps_h)} F"
                 + (f"; first at or below freezing {fmt_time(below['time'])}." if below else "."))
        f.append(f"The hourly forecast ends {fmt_time(rows[-1]['time'])}; after that only daily data exists.")

    # Daily outlook: biggest snow day and first quieter day after it
    p50 = days.get("P50", {})
    if p50:
        storm = max(p50, key=p50.get)
        if p50[storm] >= 1:
            f.append(f"Biggest snow day: {storm:%A} with {p50[storm]:.1f} in (P50), range "
                     f"{days['P10'][storm]:.1f} to {days['P90'][storm]:.1f} in. Daily totals run "
                     f"6 pm the day before to 6 pm {storm:%A}.")
            # Compare with the HRRR over the same 6 pm-to-6 pm window, if it covers it
            start = datetime.combine(storm, datetime.min.time(), se.LOCAL_TZ) - timedelta(hours=6)
            win = [r for r in rows if start < r["time"] <= start + timedelta(hours=24)]
            if win and p50[storm] >= 2:
                h = sum(r["snow"] for r in win)
                covered = win[-1]["time"]
                if h < 0.6 * p50[storm]:
                    f.append(f"The two forecasts disagree: the ensemble has {p50[storm]:.1f} in for "
                             f"{storm:%A}, but the hourly HRRR shows only {h:.1f} in through "
                             f"{fmt_time(covered)}"
                             + (" (where the hourly forecast ends)" if covered == rows[-1]["time"] else "")
                             + f". Snow may continue or be heavier during {storm:%A} than the hourly "
                             "forecast shows, so do not assume the storm is over early.")
            f.append("Snow from the storm stays on the ground in the days after, so a quiet day "
                     "with little new snow still has the storm's snow to ski.")
            after = [d for d in sorted(p50) if d > storm and p50[d] < 1.0]
            if after:
                d = after[0]
                hi, lo = temps.get(d, (None, None))
                t = f", high {hi:.0f} F / low {lo:.0f} F" if hi is not None else ""
                f.append(f"First day after the storm with under 1 in of new snow (P50): {d:%A}{t}.")
        warm = [d for d in sorted(p50) if d > storm and temps.get(d, (0, 0))[0] >= 40]
        if p50[storm] >= 1 and warm:
            f.append(f"Warm-up after the storm: highs of 40 F or more starting {warm[0]:%A}; "
                     "sunny aspects will turn heavy or wet in the afternoon.")
    return f


MST = __import__("datetime").timezone(timedelta(hours=-7))
SNOTEL_URL = ("https://wcc.sc.egov.usda.gov/reportGenerator/view_csv/customSingleStationReport/"
              "hourly/{}%7Cid=%22%22%7Cname/-180,0/WTEQ::value,SNWD::value,PREC::value,TOBS::value")


def snotel_obs(station):
    """Last 7 days of hourly SNOTEL obs: [{time, swe, depth, prec, temp}] (blank values -> None)."""
    import csv
    import io
    text = se.fetch(SNOTEL_URL.format(station))
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    num = lambda v: float(v) if v.strip() else None
    out = []
    for row in list(csv.reader(io.StringIO("\n".join(lines))))[1:]:
        if len(row) >= 5:
            # SNOTEL uses local standard time (MST, UTC-7) all year
            t = datetime.strptime(row[0], "%Y-%m-%d %H:%M").replace(tzinfo=MST).astimezone(se.LOCAL_TZ)
            out.append({"time": t, "swe": num(row[1]),
                        "depth": num(row[2]), "prec": num(row[3]), "temp": num(row[4])})
    return out


def snotel_facts(obs):
    """Recent snow facts from SNOTEL obs. Returns (facts, current depth in inches or None)."""
    have = [o for o in obs if o["depth"] is not None]
    if not have:
        return ["Measured snow at Alta (SNOTEL): no snow depth reports in the last 7 days."], None
    last = have[-1]

    def ago(h, key):
        t = last["time"] - timedelta(hours=h)
        prev = [o for o in obs if o[key] is not None and o["time"] <= t]
        return prev[-1][key] if prev else None

    f = [f"Measured snow at Alta (SNOTEL, latest {fmt_time(last['time'])}): {last['depth']:.0f} in "
         "of snow on the ground"
         + (f", {last['swe']:.1f} in of water in the snowpack" if last["swe"] is not None else "")
         + (f", air temp {last['temp']:.0f} F" if last["temp"] is not None else "") + "."]
    changes = []
    for h, name in ((24, "24 hours"), (72, "3 days"), (168, "7 days")):
        d, p = ago(h, "depth"), ago(h, "prec")
        if d is None:
            continue
        txt = f"last {name}: depth {last['depth'] - d:+.0f} in"
        if p is not None and last["prec"] is not None:
            txt += f", {max(last['prec'] - p, 0):.1f} in of water fell"
        changes.append(txt)
    if changes:
        f.append("Measured snow at Alta, recent change (" + "; ".join(changes) + "). A depth drop means "
                 "settling or melting; depth readings wobble by about an inch.")
    return f, last["depth"]


def part_of_day(t):
    h = t.hour
    return ("early morning" if h < 6 else "morning" if h < 12 else "afternoon" if h < 17
            else "evening" if h < 21 else "night")


def gsl_strength(p):
    """GSLE probability -> (label, vibe) for the model. Above ~20% is rare; 10-20% is worth watching."""
    if p >= 20:
        return "VERY STRONG (rare)", "the lake goes into overdrive; could be the main event"
    if p >= 15:
        return "STRONG", "the lake opens the tap; expect a dump on top of the forecast"
    if p >= 10:
        return "MODERATE", "worth watching; the lake could chip in some bonus snow"
    return "WEAK", "long shot; maybe a few flakes of lake bonus"


def gsl_facts(now):
    """Great Salt Lake-effect windows as plain-language facts for the model."""
    import lake_effect
    run, init, clim, rows = lake_effect.parse(se.fetch(lake_effect.URL))
    wins = [w for w in lake_effect.windows(rows) if init and init + timedelta(hours=w["f1"]) >= now]
    if not wins:
        return [f"Great Salt Lake effect ({run}): no lake-effect signal in the next 5 days."]
    out = []
    for w in wins:
        local = lambda h: (init + timedelta(hours=h)).astimezone(se.LOCAL_TZ)  # add in UTC: DST-safe
        t0, t1 = local(w["f0"]), local(w["f1"])
        peak = next(local(r["fhr"])
                    for r in rows if w["f0"] <= r["fhr"] <= w["f1"] and r["prob"] == w["peak"])
        span = (f"{t0:%A} {part_of_day(t0)}" if (t0.date(), part_of_day(t0)) == (t1.date(), part_of_day(t1))
                else f"{t0:%A} {part_of_day(t0)} into "
                     + (f"{part_of_day(t1)}" if t0.date() == t1.date() else f"{t1:%A} {part_of_day(t1)}"))
        label, vibe = gsl_strength(w["peak"])
        hits = any("COTTONWOOD" in a for a in w["areas"])
        out.append(
            f"Great Salt Lake effect, {label}: {w['peak']}% peak chance of lake-effect bands, "
            f"{span} ({fmt_time(t0) if t0 == t1 else f'{fmt_time(t0)} to {fmt_time(t1)}'}), peaking {peak:%A} {part_of_day(peak)} "
            f"({fmt_time(peak)}), {w['wind']} flow. "
            + (("NW flow aimed straight at the Cottonwoods: the bands line up with upper Little "
                "Cottonwood, so Alta will be dumped on. " if w["wind"] in ("NW", "WNW", "NNW")
                else "Aimed at the Cottonwoods. ") if hits else
               f"Aimed at {', '.join(a.title() for a in w['areas']) or 'unknown areas'}, NOT the "
               "Cottonwoods, so it matters little here. ")
            + f"Vibe: {vibe}.")
    return out


def forecast_data(loc):
    init, point, tables = se.parse(se.fetch(loc["url"]))
    snow = se.find_table(tables, "Total Snowfall")
    now = datetime.now(se.LOCAL_TZ)
    lines = [
        f"LOCATION: {loc['name']}, Little Cottonwood Canyon, Utah.",
        f"TODAY: {now:%A, %B} {now.day}, {now.year} (generated {now:%I:%M %p %Z}).",
        "",
    ]

    # Daily snow (P10/P50/P90) with high/low temps
    days = {p: dict(se.daily_snow(snow[p], init)) for p in ("P10", "P50", "P90")} if init else {}
    if point and loc.get("temp_elevation_ft"):
        point = (point[0], point[1], loc["temp_elevation_ft"])
    temps, temp_src = se.get_temps(point)
    run = init.strftime("%HZ %d %b %Y") if init else "unknown"
    lines += [
        f"DAILY FORECAST (Utah Snow Ensemble, model run {run}). New snow in inches per day, "
        "measured 6 pm to 6 pm. Daily percentiles are taken from running totals, so on a single "
        "day P10 can exceed P50; use the multi-day totals below for the overall range.",
        f"High/low air temperature in F at {point[2] if point else '?'} ft from {temp_src or 'n/a'}.",
        "Day      | Snow P10 | Snow P50 | Snow P90 | High F | Low F",
    ]
    for d in sorted(days.get("P50", {})):
        hi, lo = temps.get(d, (None, None))
        t = f"{hi:.0f} | {lo:.0f}" if hi is not None else "n/a | n/a"
        lines.append(f"{d:%a %b} {d.day:<2} | {days['P10'][d]:.1f} | {days['P50'][d]:.1f} | "
                     f"{days['P90'][d]:.1f} | {t}")
    for w in (24, 120, 240):
        lines.append(f"Total next {w // 24 if w > 24 else 1} day(s): P10 {se.value_at(snow['P10'], w):.1f}, "
                     f"P50 {se.value_at(snow['P50'], w):.1f}, P90 {se.value_at(snow['P90'], w):.1f} in")
    lines.append("")

    # Hourly HRRR (+ NWS precip chance)
    rows = []
    if loc.get("hourly_dir"):
        try:
            label, rows = se.hrrr_hourly(loc["hourly_dir"])
            chances = se.nws_precip_chance(point[0], point[1]) if point else {}
            lines += [
                f"HOURLY FORECAST (HRRR run {label}). Temperature, snow and water at Collins (9,662 ft). "
                "Wind at Top of Collins (10,443 ft), direction is where it blows FROM. "
                "SLR = snow-to-liquid ratio (0 = no snow, i.e. rain if there is water). "
                "Precip chance from NWS.",
                "Time | Temp F | Wind dir | Wind mph | Gust mph | Water in | Snow in | SLR | "
                "Precip chance %",
            ]
            for r in rows:
                c = chances.get(r["time"] - timedelta(hours=1))
                lines.append(
                    f"{r['time']:%a %I %p} | {r['temp']} | {r['wind_dir']} | {r['wind']} | "
                    f"{r['gust'] or ''} | {r['water']:.2f} | {r['snow']:.1f} | "
                    f"{r['slr'] or ''} | {'' if c is None else c[0]}")
            lines.append(f"Hourly totals: {sum(r['snow'] for r in rows):.1f} in snow, "
                         f"{sum(r['water'] for r in rows):.2f} in water.")
        except Exception as e:
            lines.append(f"HOURLY FORECAST: not available ({e}).")
    facts = ["KEY FACTS (pre-computed from the data below; trust these):"]
    obs_facts, depth = [], None
    if loc.get("snotel"):
        try:
            obs_facts, depth = snotel_facts(snotel_obs(loc["snotel"]))
        except Exception as e:
            obs_facts = [f"Measured snow at Alta (SNOTEL): not available ({e})."]
    # Alta's report is mid-mountain, where most touring happens; the measured depth is near the base
    alta = se.get_alta_report() if loc.get("alta_report") else None
    base = depth
    if alta:
        (_, last12), (_, last24), (_, storm) = alta[0]
        depth = alta[1]
        obs_facts.insert(0, f"Resort snow report (as of {alta[2] or 'today'}): {alta[1]:.0f} in on "
                         f"the ground mid-mountain; new snow last 12 hours {last12:.0f} in, last 24 "
                         f"hours {last24:.0f} in, storm total {storm:.0f} in. The measured snow "
                         "depth below is near the base, lower down, so it usually reads less.")
    recent_snow = bool(alta and (alta[0][1][1] >= 2 or alta[0][2][1] >= 2))
    facts += [f"- {x}" for x in key_facts(now, days, temps, rows, depth, base)]
    facts += [f"- {x}" for x in obs_facts]
    if loc.get("lake_effect"):
        try:
            facts += [f"- {x}" for x in gsl_facts(now)]
        except Exception as e:
            facts.append(f"- Great Salt Lake effect: not available ({e}).")
    sky = {}
    if point:
        try:
            sky = sky_daily(point[0], point[1], point[2])
        except Exception as e:
            facts.append(f"- Cloud cover not available ({e}).")
    calls = ["DAY-BY-DAY CALLS (pre-computed; use these for where to ski each day):"]
    calls += [f"- {x}" for x in day_calls(now, days, temps, sky, depth, recent_snow)]
    return "\n".join(lines[:3] + calls + [""] + facts + [""] + lines[3:])


def ask_ollama(prompt, model, think):
    """Stream the prompt's reasoning and answer from the local Ollama server as they arrive."""
    import json
    import sys
    import requests
    body = {"model": model, "prompt": prompt, "stream": True}
    if think:
        body["think"] = think if think in ("low", "medium", "high") else True
    print(f"Sending prompt to {model} (first words can take a few seconds)...", file=sys.stderr, flush=True)
    section = None
    with requests.post("http://localhost:11434/api/generate", json=body, stream=True, timeout=900) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            for key, title in (("thinking", "=== THINKING ==="), ("response", "=== ANSWER ===")):
                text = chunk.get(key)
                if text:
                    if section != key:
                        print(("\n\n" if section else "") + title, flush=True)
                        section = key
                    print(text, end="", flush=True)
    print()


def build_prompt(cfg):
    """The full touring prompt: template filled with today's data for every location."""
    template = (HERE / "prompts" / "touring_prompt.txt").read_text()
    data = "\n\n".join(forecast_data(loc) for loc in cfg["locations"])
    return template.replace("{{FORECAST_DATA}}", data)


def ask_claude(prompt, model="sonnet", timeout=600):
    """Run the prompt through Claude Code (`claude -p`) and return the answer text."""
    import subprocess
    import tempfile
    # Run from an empty folder so Claude only sees the prompt, not this repo
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["claude", "-p", "--model", model], input=prompt, text=True,
                           capture_output=True, timeout=timeout, cwd=tmp)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude -p failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ollama", metavar="MODEL", help="run the prompt on a local Ollama model")
    ap.add_argument("--think", default="true",
                    help="reasoning: true, false, or low/medium/high (gpt-oss)")
    args = ap.parse_args()

    cfg = yaml.safe_load((HERE / "config.yaml").read_text())
    prompt = build_prompt(cfg)
    if not args.ollama:
        print(prompt)
        return
    think = None if args.think == "false" else args.think
    ask_ollama(prompt, args.ollama, think)


if __name__ == "__main__":
    main()
