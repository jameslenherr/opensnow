"""Great Salt Lake-effect section: group GSLE probability rows into windows."""
import re
from datetime import datetime, timedelta, timezone
from html import escape

URL = "https://weather.utah.edu/text/LAKE_EFFECT.txt"

ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\w{3} \d{2} [AP]M)\s+\d+ FT\s+(-?[\d.]+) C\s+(-?[\d.]+) C\s+"
    r"(-?[\d.]+) C\s+(\d+)%\s+(\w+)\s+(\d+) KT\s+[\d.]+ C/KM\s+(\d+)%\s*(.*)$"
)
RUN_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4}) (\d{2})Z GFS")
AREA_NAMES = {"WEST-SLV": "West SL Valley", "SLV": "SL Valley"}


def parse(text):
    """Return (run label, init datetime, climatology flag, list of row dicts)."""
    m = RUN_RE.search(text)
    run = f"{m[4]}Z GFS, {m[1]}/{m[2]}" if m else "GFS run unknown"
    init = datetime(int(m[3]), int(m[1]), int(m[2]), int(m[4]), tzinfo=timezone.utc) if m else None
    clim = "BASED ON CLIMATOLOGY" in text
    rows = []
    for line in text.splitlines():
        r = ROW_RE.match(line)
        if r:
            rows.append({
                "fhr": int(r[1]), "time": r[2], "t700": float(r[3]), "excess": float(r[5]),
                "rh": int(r[6]), "wdir": r[7], "wspd": int(r[8]),
                "prob": int(r[9]), "areas": r[10].split(),
            })
    return run, init, clim, rows


def windows(rows, min_prob=5):
    """Group consecutive rows with prob >= min_prob."""
    out, cur = [], []
    for row in rows + [None]:
        if row and row["prob"] >= min_prob:
            cur.append(row)
        elif cur:
            areas = []
            for r in cur:
                areas += [a for a in r["areas"] if a not in areas]
            peak = max(cur, key=lambda r: r["prob"])
            out.append({
                "start": cur[0]["time"], "end": cur[-1]["time"],
                "f0": cur[0]["fhr"], "f1": cur[-1]["fhr"],
                "peak": peak["prob"], "peak_time": peak["time"],
                "excess": max(r["excess"] for r in cur),
                "wind": peak["wdir"], "areas": areas,
            })
            cur = []
    return out


def select(wins, init, now=None, n=3, soon_h=24):
    """Drop past windows; slot 1 = biggest within soon_h, rest = biggest overall."""
    now = now or datetime.now(timezone.utc)
    h = (now - init).total_seconds() / 3600 if init else 0
    live = [w for w in wins if w["f1"] >= h]
    size = lambda w: (w["peak"], w["excess"])
    soon = [w for w in live if w["f0"] <= h + soon_h]
    first = [max(soon, key=size)] if soon else []
    rest = sorted((w for w in live if w not in first), key=size, reverse=True)
    picked = first + sorted(rest[: n - len(first)], key=lambda w: w["f0"])
    for w in picked:
        w["soon"] = w in first
    return picked, len(live) - len(picked)


def _area(a):
    return AREA_NAMES.get(a, a.title())


LOW_W, HIGH_W = 128, 32  # 0-20% gets 80% of the bar
GRAD = "linear-gradient(90deg,#22c55e,#eab308 55%,#f97316 85%,#ef4444)"

FOOTNOTE = (
    "<b>Peak chance:</b> the highest hourly probability, during the window, of snow bands "
    "forming off the Great Salt Lake. Values above ~20% (dashed line) are rare, so "
    "10&ndash;20% is worth watching.<br>"
    "<b>Max excess:</b> how far the lake-to-700&nbsp;mb temperature difference (lake surface "
    "vs. ~10,000&nbsp;ft) goes above the ~16.5&nbsp;&deg;C needed for lake effect. 0 means no "
    "lake-effect potential; higher means stronger instability and heavier bands if moisture is there.<br>"
    "<b>Flow:</b> wind direction at ~10,000&nbsp;ft. Bands form over the lake and drift downwind:<br>"
    "&nbsp;&nbsp;W&ndash;WNW &rarr; Ogden, Bountiful<br>"
    "&nbsp;&nbsp;NW &rarr; Salt Lake Valley, Cottonwoods<br>"
    "&nbsp;&nbsp;NNW&ndash;N &rarr; West SL Valley, Oquirrhs, Tooele"
)


def _bar(p):
    """Email-safe bar: 0-20% cell and 20-100% cell split by a dashed line."""
    lo = min(p, 20) / 20 * LOW_W
    hi = max(p - 20, 0) / 80 * HIGH_W
    track = "background:#f1f5f9;height:8px;margin:3px 0"
    return (
        '<table role="presentation" style="border-collapse:collapse;display:inline-table;vertical-align:middle">'
        f'<tr><td style="width:{LOW_W}px;padding:0">'
        f'<div style="{track};border-radius:4px 0 0 4px">'
        f'<div style="height:8px;width:{lo:.0f}px;border-radius:4px 0 0 4px;background:#22c55e;'
        f'background-image:{GRAD};background-size:{LOW_W}px 8px"></div></div></td>'
        f'<td style="width:{HIGH_W}px;padding:0;border-left:2px dashed #64748b">'
        f'<div style="{track};border-radius:0 4px 4px 0">'
        f'<div style="height:8px;width:{hi:.0f}px;border-radius:0 4px 4px 0;background:#dc2626"></div>'
        "</div></td></tr></table>"
    )


def render_html(run, clim, wins, hidden=0, title=True):
    """Section HTML; title=False drops the heading (for when the email adds its own)."""
    head = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:560px;color:#0f172a">'
        + ('<div style="font-size:17px;font-weight:600;margin:0 0 2px">Great Salt Lake effect</div>'
           if title else "")
        + f'<div style="font-size:12px;color:#64748b;margin:0 0 12px">{escape(run)}'
        + (" &middot; lake temp from climatology" if clim else "") + "</div>"
    )
    if not wins:
        body = ('<div style="padding:12px 14px;background:#f1f5f9;border-radius:8px;'
                'font-size:14px;color:#475569">No lake-effect signal in the forecast.</div>')
        return head + body + "</div>"

    cards = []
    for w in wins:
        span = w["start"] if w["start"] == w["end"] else f'{w["start"]} &ndash; {w["end"]}'
        chips = "".join(
            f'<span style="display:inline-block;background:#e0e7ff;color:#3730a3;'
            f'font-size:12px;padding:2px 8px;border-radius:10px;margin:0 4px 4px 0">'
            f"{escape(_area(a))}</span>" for a in w["areas"]
        )
        cards.append(
            '<div style="border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px;margin:0 0 10px">'
            f'<div style="font-size:15px;font-weight:600">{span}'
            + (' <span style="font-size:11px;font-weight:600;color:#b45309;background:#fef3c7;'
               'padding:1px 6px;border-radius:8px;vertical-align:middle">Next 24 h</span>'
               if w.get("soon") else "") + "</div>"
            '<table role="presentation" style="width:100%;border-collapse:collapse;margin:8px 0 6px;font-size:13px">'
            '<tr><td style="color:#64748b;width:90px">Peak chance</td>'
            f'<td>{_bar(w["peak"])}'
            f' <b>{w["peak"]}%</b> <span style="color:#64748b">({w["peak_time"]})</span></td></tr>'
            f'<tr><td style="color:#64748b">Max excess</td><td><b>{w["excess"]:.1f} &deg;C</b></td></tr>'
            f'<tr><td style="color:#64748b">Flow</td><td>{w["wind"]}</td></tr>'
            "</table>"
            f"<div>{chips}</div></div>"
        )
    more = (f'<div style="font-size:12px;color:#64748b;margin:0 0 8px">+{hidden} smaller '
            f'window{"s" if hidden > 1 else ""} not shown</div>' if hidden else "")
    note = (f'<div style="font-size:11px;line-height:1.5;color:#64748b;margin:4px 0 0">'
            f"{FOOTNOTE}</div>")
    return head + "".join(cards) + more + note + "</div>"


if __name__ == "__main__":
    import sys
    text = open(sys.argv[1]).read() if len(sys.argv) > 1 else __import__("requests").get(URL, timeout=60).text
    run, init, clim, rows = parse(text)
    wins, hidden = select(windows(rows), init)
    print(render_html(run, clim, wins, hidden))
