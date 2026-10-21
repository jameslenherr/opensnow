#!/usr/bin/env python3
"""Email Utah Snow Ensemble snowfall percentiles for configured locations."""
import html
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) snow-email/1.0"}
INIT_RE = re.compile(r"Initialized (\d{2})(\d{2}) UTC (\d{1,2} \w{3} \d{4})")
POINT_RE = re.compile(r"Guidance for ([\d.]+) N ([\d.]+) W (\d+) ft")
ROW_LABELS = {"Min", "P10", "P25", "P50", "P75", "P90", "Max"}
STEP_H = 6  # table columns are 6-h steps starting at init + 6 h
LOCAL_TZ = ZoneInfo("America/Denver")
CHART_DAYS = 10
BAR_PX = 100  # height of the tallest bar


def fetch(url):
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.text


def to_float(s):
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse(html):
    """Return (init datetime or None, (lat, lon, elev ft) or None, {table title: {row: [values]}})."""
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    m = INIT_RE.search(text)
    init = None
    if m:
        init = datetime.strptime(f"{m[1]}{m[2]} {m[3]}", "%H%M %d %b %Y")
        init = init.replace(tzinfo=timezone.utc)
    p = POINT_RE.search(text)
    point = (float(p[1]), -float(p[2]), int(p[3])) if p else None

    tables = {}
    for t in soup.find_all("table"):
        title = t.find_previous(string=re.compile("Guidance"))
        if not title:
            continue
        rows = {}
        for tr in t.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if cells and cells[0] in ROW_LABELS:
                rows[cells[0]] = [to_float(x) for x in cells[1:]]
        if rows:
            tables[" ".join(title.split())] = rows
    return init, point, tables


def temps_open_meteo(lat, lon, elev_ft):
    """{local date: (high, low)} in °F for 10 days."""
    r = requests.get("https://api.open-meteo.com/v1/forecast", headers=UA, timeout=30, params={
        "latitude": lat, "longitude": lon, "elevation": round(elev_ft * 0.3048),
        "daily": "temperature_2m_max,temperature_2m_min", "temperature_unit": "fahrenheit",
        "timezone": "America/Denver", "forecast_days": CHART_DAYS + 1,
    })
    r.raise_for_status()
    d = r.json()["daily"]
    return {datetime.fromisoformat(t).date(): (hi, lo)
            for t, hi, lo in zip(d["time"], d["temperature_2m_max"], d["temperature_2m_min"])
            if hi is not None and lo is not None}


def temps_nws(lat, lon):
    """{local date: (high, low)} in °F from NWS hourly forecast (about 7 days)."""
    r = requests.get("https://forecast.weather.gov/MapClick.php", headers=UA, timeout=30,
                     params={"lat": lat, "lon": lon, "FcstType": "digitalDWML"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    times = [datetime.fromisoformat(t.text).astimezone(LOCAL_TZ).date()
             for t in root.find(".//time-layout").iter("start-valid-time")]
    vals = [v.text for v in root.find(".//temperature[@type='hourly']").iter("value")]
    by_day = {}
    for day, v in zip(times, vals):
        if v and v.strip():
            by_day.setdefault(day, []).append(float(v))
    # Skip days with too few hours for a fair high/low (e.g. the tail of the forecast).
    return {d: (max(v), min(v)) for d, v in by_day.items() if len(v) >= 18}


HRRR_FILE_RE = re.compile(r'href="(hrrrlccforecast_(\d{10})\.txt)"')
HRRR_ROW_RE = re.compile(
    r"^\s*\d+\s+(\d{2} [AP]M \w{3} \w{3} \d{1,2} \d{4})\s+(\S+)\s+(\S+)\s+(\S+)"
    r"\s+(-?\d+)F\s+(-?\d+)F\s+(-?\d+)F\s+-?\d+F\s+-?\d+F\s+\S+\s+(\d+):1/\S*"
    r"\s+([\d.]+)/\s*[\d.]+\s+([\d.]+)/\s*[\d.]+\s*$")
WIND_RE = re.compile(r"([NSEW]+)(\d+)(?:G(\d+))?")


def hrrr_hourly(base_url):
    """Hourly CLN forecast from the newest HRRR text file, as (init label, [rows]).

    Each row: {time, temp, wind_dir, wind, gust, slr, water, snow}. Winds are from
    Top of Collins (ALT); CLN has no wind column. Past hours are dropped.
    """
    files = sorted(HRRR_FILE_RE.findall(fetch(base_url)), key=lambda f: f[1])
    if not files:
        raise ValueError("no HRRR files listed")
    now = datetime.now(LOCAL_TZ)
    for name, stamp in reversed(files[-2:]):  # newest, then the one before
        rows = []
        for line in fetch(base_url + name).splitlines():
            m = HRRR_ROW_RE.match(line)
            if not m:
                continue
            t = datetime.strptime(m[1], "%I %p %a %b %d %Y").replace(tzinfo=LOCAL_TZ)
            w = WIND_RE.fullmatch(m[3])  # ALT wind
            rows.append({
                "time": t, "temp": int(m[7]),
                "wind_dir": w[1] if w else "", "wind": int(w[2]) if w else 0,
                "gust": int(w[3]) if w and w[3] else None,
                "slr": int(m[8]), "water": float(m[9]), "snow": float(m[10]),
            })
        rows = [r for r in rows if r["time"] > now]
        if rows:
            return f"{stamp[8:]}Z {stamp[6:8]}/{stamp[4:6]}", rows
    raise ValueError("HRRR files had no future hours")


def nws_precip_chance(lat, lon):
    """{hour start (local): (chance %, 'snow'|'rain'|'')} from the NWS hourly forecast."""
    r = requests.get("https://forecast.weather.gov/MapClick.php", headers=UA, timeout=30,
                     params={"lat": lat, "lon": lon, "FcstType": "digitalDWML"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    times = [datetime.fromisoformat(t.text).astimezone(LOCAL_TZ)
             for t in root.find(".//time-layout").iter("start-valid-time")]
    pops = [v.text for v in root.find(".//probability-of-precipitation").iter("value")]
    kinds = []
    for c in root.find(".//weather").iter("weather-conditions"):
        types = " ".join(v.get("weather-type", "") for v in c.iter("value"))
        kinds.append("snow" if "snow" in types else "rain" if "rain" in types else "")
    out = {}
    for i, t in enumerate(times):
        if i < len(pops) and pops[i]:
            out[t] = (int(pops[i]), kinds[i] if i < len(kinds) else "")
    return out


def hourly_html(init_label, rows, chances):
    """48-h hourly table, one column per hour, in a sideways-scrolling box."""
    cell = ("text-align:center;padding:2px 3px;font-family:Arial,sans-serif;"
            "font-size:11px;min-width:30px;white-space:nowrap;")
    label = ("text-align:left;padding:2px 6px 2px 0;font-family:Arial,sans-serif;"
             "font-size:11px;color:#666;white-space:nowrap;")
    top = max((r["snow"] for r in rows), default=0)

    def day_edge(i):  # vertical line at midnight
        return "border-left:1px solid #ccc;" if i and rows[i]["time"].hour == 0 else ""

    # Day header: one cell spanning each day's hours
    days, start = [], 0
    for i in range(1, len(rows) + 1):
        if i == len(rows) or rows[i]["time"].date() != rows[start]["time"].date():
            d = rows[start]["time"]
            days.append(f'<td colspan="{i - start}" style="{cell}text-align:left;font-weight:bold;'
                        f'color:#222;{day_edge(start)}">{d:%a} {d.day}</td>')
            start = i

    def row(name, cells):
        return f'<tr><td style="{label}">{name}</td>{"".join(cells)}</tr>'

    hours, bars, snow, water, slr, temp, wdir, wind, gusts, pop = ([] for _ in range(10))
    for i, r in enumerate(rows):
        e = day_edge(i)
        h = r["time"].hour % 12 or 12
        hours.append(f'<td style="{cell}color:#666;{e}">{h}{"a" if r["time"].hour < 12 else "p"}</td>')
        bh = 0 if top <= 0 or r["snow"] <= 0 else max(round(r["snow"] / top * 40), 2)
        bar = (f'<div style="width:14px;height:{bh}px;background:#3b82f6;margin:0 auto;'
               f'border-radius:2px 2px 0 0;"></div>') if bh else ""
        bars.append(f'<td height="40" style="{cell}height:40px;vertical-align:bottom;'
                    f'border-bottom:1px solid #ccc;{e}">{bar}</td>')
        on = "color:#1d4ed8;font-weight:bold;"
        off = "color:#bbb;"
        snow.append(f'<td style="{cell}{on if r["snow"] else off}{e}">{r["snow"]:.1f}</td>')
        water.append(f'<td style="{cell}{on if r["water"] else off}{e}">{r["water"]:.2f}</td>')
        # 0:1 means no precip that hour, so leave it blank
        ratio = f'{r["slr"]}:1' if r["slr"] else ""
        slr.append(f'<td style="{cell}color:#222;{e}">{ratio}</td>')
        temp.append(f'<td style="{cell}color:#222;{e}">{r["temp"]}&deg;</td>')
        wdir.append(f'<td style="{cell}color:#222;{e}">{r["wind_dir"]}</td>')
        wind.append(f'<td style="{cell}color:#222;{e}">{r["wind"]}</td>')
        gusts.append(f'<td style="{cell}color:#222;{e}">{r["gust"] or ""}</td>')
        # NWS hours are labelled by start; HRRR hours by end.
        c = chances.get(r["time"] - timedelta(hours=1))
        txt = "" if c is None else f"{c[0]}%"
        pop.append(f'<td style="{cell}color:#222;{e}">{txt}</td>')

    tot_s, tot_w = sum(r["snow"] for r in rows), sum(r["water"] for r in rows)
    first, last = rows[0]["time"], rows[-1]["time"]
    title = (f'<p style="font-family:Arial,sans-serif;font-size:13px;margin:12px 0 4px;">'
             f'<b>Next {len(rows)} hours at Collins</b>: {tot_s:.1f}" snow, {tot_w:.2f}" water '
             f'<span style="color:#888;">(HRRR {init_label})</span></p>')
    table = "".join([
        f'<tr><td></td>{"".join(days)}</tr>', row("", hours), row("", bars),
        row("Snow in", snow), row("Water in", water), row("Snow ratio", slr), row("Temp", temp),
        row("Wind dir", wdir), row("Wind mph", wind), row("Gust mph", gusts), row("Precip chance", pop),
    ])
    return (title + '<div style="overflow-x:auto;max-width:100%;">'
            f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{table}'
            '</table></div><p style="font-family:Arial,sans-serif;font-size:11px;color:#888;'
            'margin:4px 0 16px;">Wind from Top of Collins (ALT). Precip chance from NWS. '
            'Scroll sideways for more hours.</p>'), tot_s, tot_w


def get_hourly(loc, point):
    """Return (html, text line) for the 48-h table, or ("", "") if it can't be built."""
    base = loc.get("hourly_dir")
    if not base:
        return "", ""
    try:
        init_label, rows = hrrr_hourly(base)
    except Exception as e:
        print(f"Hourly HRRR forecast failed: {e}", flush=True)
        return "", ""
    chances = {}
    if point:
        try:
            chances = nws_precip_chance(point[0], point[1])
        except Exception as e:
            print(f"NWS precip chance failed: {e}", flush=True)
    html_part, s, w = hourly_html(init_label, rows, chances)
    return html_part, f"Next {len(rows)} h (HRRR {init_label}): {s:.1f} in snow, {w:.2f} in water"


def get_temps(point):
    """Return ({date: (high, low)}, source name); empty if every source fails."""
    if not point:
        return {}, None
    lat, lon, elev = point
    for name, fn in (("Open-Meteo.com", lambda: temps_open_meteo(lat, lon, elev)),
                     ("NWS", lambda: temps_nws(lat, lon))):
        try:
            temps = fn()
            if temps:
                return temps, name
        except Exception as e:
            print(f"Temperatures from {name} failed: {e}", flush=True)
    return {}, None


def find_table(tables, key):
    for title, rows in tables.items():
        if key in title:
            return rows
    raise KeyError(f"no table matching '{key}'")


def value_at(series, hours):
    i = min(hours // STEP_H - 1, len(series) - 1)
    return series[max(i, 0)]


def daily_snow(total, init):
    """Snow per day as [(local date, inches)], 00Z to 00Z (6 pm MDT / 5 pm MST).

    Taken as differences of the cumulative series; partial first days are dropped.
    """
    days, prev, prev_end = [], 0.0, init
    for i, v in enumerate(total):
        end = init + timedelta(hours=STEP_H * (i + 1))
        if end.hour != 0:
            continue
        if end - prev_end == timedelta(hours=24) and v == v and prev == prev:  # skip NaN
            days.append((end.astimezone(LOCAL_TZ).date(), max(v - prev, 0.0)))
        prev, prev_end = v, end
        if len(days) == CHART_DAYS:
            break
    return days


def fmt_in(v):
    if v == 0:
        return '0"'
    return f'{v:.1f}"' if v < 1 else f'{v:.0f}"'


def chart_html(days, temps=None):
    """OpenSnow-style daily bar chart as an email-safe HTML table (no SVG/JS)."""
    if not days:
        return "<p>Daily chart not available.</p>"
    temps = temps or {}
    top = max(v for _, v in days)
    cell = "text-align:center;padding:0 5px;font-family:Arial,sans-serif;"
    amt, bars, dow, dom, his, los = [], [], [], [], [], []
    for d, v in days:
        h = 0 if top <= 0 or v <= 0 else max(round(v / top * BAR_PX), 2)
        bar = (f'<div style="width:22px;height:{h}px;background:#3b82f6;'
               f'margin:0 auto;border-radius:3px 3px 0 0;"></div>') if h else ""
        amt.append(f'<td style="{cell}font-size:12px;color:#333;">{fmt_in(v)}</td>')
        bars.append(f'<td height="{BAR_PX}" style="{cell}height:{BAR_PX}px;'
                    f'vertical-align:bottom;border-bottom:1px solid #ccc;">{bar}</td>')
        dow.append(f'<td style="{cell}font-size:12px;color:#666;padding-top:4px;">'
                   f'{"MTWTFSS"[d.weekday()]}</td>')
        dom.append(f'<td style="{cell}font-size:14px;font-weight:bold;color:#222;">{d.day}</td>')
        hi, lo = (f"{t:.0f}&deg;" for t in temps[d]) if d in temps else ("", "")
        his.append(f'<td style="{cell}font-size:12px;color:#c2410c;padding-top:4px;">{hi}</td>')
        los.append(f'<td style="{cell}font-size:12px;color:#2563eb;">{lo}</td>')
    shown = (amt, bars, dow, dom, his, los) if temps else (amt, bars, dow, dom)
    rows = "".join(f"<tr>{''.join(r)}</tr>" for r in shown)
    return f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{rows}</table>'


def summarize(loc, cfg):
    init, point, tables = parse(fetch(loc["url"]))
    snow = find_table(tables, "Total Snowfall")
    pcts = cfg["percentiles"]

    lines = [loc["name"]]
    run = init.strftime("%HZ %d %b %Y") if init else "unknown"
    lines.append(f"Run: {run}")
    if init:
        age = (datetime.now(timezone.utc) - init).total_seconds() / 3600
        if age > cfg["max_age_hours"]:
            lines.append(f"WARNING: run is {age:.0f} h old (page not updated)")

    if len(pcts) > 1:
        lines.append(f"{'':8}" + "".join(f"{p:>7}" for p in pcts))
    medians = {}
    for w in cfg["windows"]:
        vals = [value_at(snow[p], w["hours"]) for p in pcts]
        lines.append(f"{w['label']:8}" + "".join(f"{v:>7.1f}" for v in vals))
        medians[w["label"]] = vals[0]
    lines.append("Snowfall in inches, accumulated from model init.")

    days = daily_snow(snow[pcts[0]], init) if init else []
    if point and loc.get("temp_elevation_ft"):
        point = (point[0], point[1], loc["temp_elevation_ft"])
    temps, temp_src = get_temps(point) if days else ({}, None)
    if days:
        def day_txt(d, v):
            t = f" {temps[d][0]:.0f}/{temps[d][1]:.0f}F" if d in temps else ""
            return f"{d:%a} {d.day} {v:.1f}{t}"
        lines.append(f"Daily {pcts[0]}: " + " | ".join(day_txt(d, v) for d, v in days))
    if temp_src:
        lines.append(f"High/low temperatures (F): {temp_src}")
    hourly, hourly_txt = get_hourly(loc, point)
    if hourly_txt:
        lines.append(hourly_txt)
    return "\n".join(lines), medians, hourly + chart_html(days, temps)


def send(subject, body, html_body):
    # One email per recipient so nobody sees the rest of the list.
    recipients = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        for to in recipients:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = os.environ["SMTP_USER"]
            msg["To"] = to
            msg.set_content(body)
            msg.add_alternative(html_body, subtype="html")
            s.send_message(msg)


def try_all(locs, cfg):
    """Return ({name: (text, medians, chart html)}, {name: error})."""
    ok, bad = {}, {}
    for loc in locs:
        try:
            ok[loc["name"]] = summarize(loc, cfg)
        except Exception as e:
            bad[loc["name"]] = str(e)
    return ok, bad


def main():
    cfg_path = Path(os.environ.get("SNOW_CONFIG", Path(__file__).with_name("config.yaml")))
    cfg = yaml.safe_load(cfg_path.read_text())
    locs = cfg["locations"]
    delay = float(os.environ.get("RETRY_DELAY_MIN", cfg.get("retry_delay_min", 60)))

    ok, bad = try_all(locs, cfg)
    if bad and delay > 0:
        print(f"Failed: {bad}; retrying in {delay:.0f} min", flush=True)
        time.sleep(delay * 60)
        ok2, bad = try_all([l for l in locs if l["name"] in bad], cfg)
        ok.update(ok2)

    today = datetime.now(ZoneInfo("America/Denver"))
    d = today.day
    suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    sections, html_parts = [], []
    subject = f"Snow forecast for {today:%a %b} {d}{suffix}"
    for loc in locs:
        name = loc["name"]
        if name in ok:
            text, _, chart = ok[name]
        else:
            text, chart = f"{name}\nData not available (tried twice): {bad[name]}", ""
        sections.append(text)
        pre = "\n".join(l for l in text.splitlines() if not l.startswith(("Daily ", "Next ")))
        html_parts.append(f"<pre>{html.escape(pre)}</pre>{chart}")

    if not ok:
        subject += ": data not available"
    elif len(locs) == 1:
        med = ok[locs[0]["name"]][1]
        subject += ": " + " | ".join(f'{k} {v:.0f}"' for k, v in med.items())

    source = ("Source: Utah Snow Ensemble (U of Utah, experimental). "
              "Hourly: U of Utah HRRR guidance (experimental) and NWS.")
    body = "\n\n".join(sections) + f"\n\n{source}\n"
    html_body = "<html><body>" + "<br>".join(html_parts) + f"<p>{source}</p></body></html>"
    if os.environ.get("DRY_RUN"):
        print(subject + "\n\n" + body)
        Path("preview.html").write_text(html_body)
        print("HTML written to preview.html")
    else:
        send(subject, body, html_body)
    if bad:
        print(f"Failed: {bad}", flush=True)
    if not ok:
        sys.exit(1)  # flag the Actions run red


if __name__ == "__main__":
    main()
