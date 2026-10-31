"""Rule-of-thumb model of the snow surface on shady and sunny slopes, day by day.

A lookup table driven by time of year (how strong the sun is), daily high/low temps, cloud
cover and new snow. It's a rough guide for the touring outlook, not a snowpack model. The
prompt's "HOW THE SNOW SURFACE CHANGES" cheat sheet describes the same rules in words, so keep
the two in sync.
"""
import math

LAT = 40.58  # Alta

# Sun strength by noon sun angle (degrees), which tracks the time of year at Alta:
# LOW ~ late Nov to mid Jan, MODERATE ~ late Oct to late Nov and mid Jan to late Feb,
# STRONG ~ late Feb to early Apr and Sep to late Oct, VERY STRONG ~ April on.
SUN_CLASSES = [(28, "LOW"), (38, "MODERATE"), (55, "STRONG"), (91, "VERY STRONG")]

# Daily high (F) at which the surface starts to soften (LIGHT: damp, then crusts overnight)
# and at which it turns wet (STRONG), on a mostly clear day (under 50% cloud).
SUNNY_HI = {"LOW": (26, 36), "MODERATE": (20, 32), "STRONG": (12, 26), "VERY STRONG": (0, 22)}
SHADY_HI = {"LOW": (36, 42), "MODERATE": (36, 42), "STRONG": (34, 40), "VERY STRONG": (30, 36)}
PARTLY_CLOUDY_SHIFT = 6  # 50-75% cloud: the sun needs a warmer day to do the same work
# Overcast (75%+): the sun barely reaches the snow, so sunny slopes act like shady ones.

POWDER = {"fresh", "dense_new", "settled", "recycled"}
CRUSTY = {"crust", "firm", "corn", "mush"}

DESCRIBE = {
    "fresh": "fresh powder",
    "dense_new": "heavy, dense new snow",
    "settled": "settled powder, still soft",
    "recycled": "loose, sugary recycled powder (old snow kept soft by the cold)",
    "sunbaked": "soft early, then damp and sticky in the afternoon; freezes into a punchy crust overnight",
    "heavy": "powder early, heavy and wet by afternoon; freezes into a crust overnight",
    "crust": "breakable crust, punchy and grabby",
    "softening": "crusty in the morning, softening to heavy, wet snow in the afternoon",
    "firm": "firm refrozen crust, hard and scratchy all day",
    "corn": "frozen in the morning, softening to corn by late morning, then sloppy",
    "mush": "wet and mushy with no overnight refreeze (wet slide concern)",
}
# How fun each surface is to ski, for picking the better aspect
SCORE = {"fresh": 5, "settled": 4, "corn": 4, "recycled": 3, "dense_new": 3, "sunbaked": 3,
         "heavy": 2, "softening": 1, "firm": 1, "crust": 0, "mush": 0}


def sun_class(d):
    """Sun strength for a date, from the noon sun angle at Alta."""
    decl = 23.44 * math.sin(math.radians(360 / 365 * (d.timetuple().tm_yday - 81)))
    noon = 90 - LAT + decl
    return next(name for top, name in SUN_CLASSES if noon < top)


def impact(aspect, sun, hi, cloud):
    """How much the day's sun and warmth work on the surface: 0 none, 1 light, 2 strong."""
    if hi is None:
        return 0
    if aspect == "sunny" and (cloud is None or cloud < 75):
        light, strong = SUNNY_HI[sun]
        if cloud is not None and cloud >= 50:
            light, strong = light + PARTLY_CLOUDY_SHIFT, strong + PARTLY_CLOUDY_SHIFT
    else:
        light, strong = SHADY_HI[sun]
    return 2 if hi >= strong else 1 if hi >= light else 0


def step(state, age, new, hi, lo, hit):
    """One day on one aspect. Returns (what you ski today, state carried overnight, powder age)."""
    refreeze = lo is None or lo <= 30
    if new >= 2:
        today = "dense_new" if hi is not None and hi >= 34 else "fresh"
        # A few inches on top of a crust don't hide it for long
        return today, today, 0
    if state in POWDER:
        age += 1
        if hit == 0:
            aged = "recycled" if age >= 4 else "settled" if age >= 2 else state
            return aged, aged, age
        if hit == 1:
            return "sunbaked", "crust", age
        # A thorough melt refreezes into a thick, firm crust
        return "heavy", "firm" if refreeze else "mush", age
    # Old surface: crust, firm, corn or mush (or unknown, treated as crust)
    if hit == 2:
        if not refreeze:
            return "mush", "mush", age
        # Corn needs a surface that has already been through a melt-freeze cycle
        if state in ("firm", "corn"):
            return "corn", "corn", age
        return "softening", "firm", age
    if hit == 1:
        today = "crust" if state == "crust" else "firm"
        return today, today, age
    today = "firm" if state in ("corn", "mush", "firm") else "crust"
    return today, today, age


def evolve(days, start="crust"):
    """Surface on shady and sunny slopes for each day.

    days: [(date, new snow in, high F, low F, cloud %)] in order.
    start: surface before the first day ("crust" is a safe guess for unknown old snow).
    Returns {date: {"shady": key, "sunny": key, "over_crust": {aspect: bool}}}.
    """
    out = {}
    st = {"shady": [start, 9], "sunny": [start, 9]}
    for d, new, hi, lo, cloud in days:
        sun = sun_class(d)
        res = {"sun": sun, "over_crust": {}}
        for aspect in ("shady", "sunny"):
            prev = st[aspect][0]
            hit = impact(aspect, sun, hi, cloud)
            today, nxt, age = step(prev, st[aspect][1], new, hi, lo, hit)
            res[aspect] = today
            # Under about 6 inches of new snow, skis still find the old crust underneath
            res["over_crust"][aspect] = 2 <= new < 6 and prev in CRUSTY
            st[aspect] = [nxt, age]
        out[d] = res
    return out


def describe(res, aspect):
    txt = DESCRIBE[res[aspect]]
    if res["over_crust"].get(aspect):
        txt += " over an old crust (skis may punch through)"
    return txt
