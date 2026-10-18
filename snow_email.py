#!/usr/bin/env python3
"""Email Utah Snow Ensemble snowfall percentiles for configured locations."""
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) snow-email/1.0"}
INIT_RE = re.compile(r"Initialized (\d{2})(\d{2}) UTC (\d{1,2} \w{3} \d{4})")
ROW_LABELS = {"Min", "P10", "P25", "P50", "P75", "P90", "Max"}
STEP_H = 6  # table columns are 6-h steps starting at init + 6 h


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
    """Return (init datetime or None, {table title: {row label: [values]}})."""
    soup = BeautifulSoup(html, "html.parser")
    m = INIT_RE.search(soup.get_text(" "))
    init = None
    if m:
        init = datetime.strptime(f"{m[1]}{m[2]} {m[3]}", "%H%M %d %b %Y")
        init = init.replace(tzinfo=timezone.utc)

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
    return init, tables


def find_table(tables, key):
    for title, rows in tables.items():
        if key in title:
            return rows
    raise KeyError(f"no table matching '{key}'")


def value_at(series, hours):
    i = min(hours // STEP_H - 1, len(series) - 1)
    return series[max(i, 0)]


def summarize(loc, cfg):
    init, tables = parse(fetch(loc["url"]))
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
    return "\n".join(lines), medians


def send(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def try_all(locs, cfg):
    """Return ({name: (text, medians)}, {name: error})."""
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
        print(f"Failed: {list(bad)}; retrying in {delay:.0f} min", flush=True)
        time.sleep(delay * 60)
        ok2, bad = try_all([l for l in locs if l["name"] in bad], cfg)
        ok.update(ok2)

    today = datetime.now(ZoneInfo("America/Denver"))
    d = today.day
    suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    sections, subject = [], f"Snow forecast for {today:%a %b} {d}{suffix}"
    for loc in locs:
        name = loc["name"]
        if name in ok:
            sections.append(ok[name][0])
        else:
            sections.append(f"{name}\nData not available (tried twice): {bad[name]}")

    if not ok:
        subject += ": data not available"
    elif len(locs) == 1:
        med = ok[locs[0]["name"]][1]
        subject += ": " + " | ".join(f'{k} {v:.0f}"' for k, v in med.items())

    body = "\n\n".join(sections) + "\n\nSource: Utah Snow Ensemble (U of Utah, experimental)\n"
    if os.environ.get("DRY_RUN"):
        print(subject + "\n\n" + body)
    else:
        send(subject, body)
    if not ok:
        sys.exit(1)  # flag the Actions run red


if __name__ == "__main__":
    main()
