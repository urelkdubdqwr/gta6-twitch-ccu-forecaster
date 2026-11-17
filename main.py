#!/usr/bin/env python3
"""Log-space launch-comparables quantile forecaster for hourly Twitch viewer counts.

For each case it builds release-day curves from comparable games, fits a
conditional linear shape against the hour-1 print, and blends an empirical
with a normal spread to produce q05/q25/q50/q75/q95 per hour 2..24.
Output is deterministic per case: no randomness, clock, or dict order.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = {
    0.05: -1.6448536269514722,
    0.25: -0.6744897501960817,
    0.5: 0.0,
    0.75: 0.6744897501960817,
    0.95: 1.6448536269514722,
}
WSIZE = 7
LQ = 0.5
K_TAIL = 0.55
K_MID = 0.45
CLAMP = 0.5
S_FLOOR = 0.05


def parse_ts(s):
    if s is None:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_curve(day_start, hourly):
    if day_start is None or hourly is None:
        return None
    values = {}
    for row in hourly:
        if not isinstance(row, dict):
            continue
        ts = parse_ts(row.get("from"))
        if ts is None:
            continue
        delta = ts - day_start
        hours = delta.total_seconds() / 3600.0
        if hours < 0 or hours >= 24:
            continue
        hnr = 1 + int(math.floor(hours))
        if hnr < 1 or hnr > 24:
            continue
        v = row.get("viewers")
        if v is None:
            return None
        values[hnr] = float(v)
    # fill missing hours by log-linear interpolation between nearest known neighbors
    curve = [None] * 25
    keys = sorted(values.keys())
    for k in keys:
        curve[k] = values[k]
    for h in range(1, 25):
        if curve[h] is not None:
            continue
        lower = None
        for k in range(h - 1, 0, -1):
            if curve[k] is not None:
                lower = k
                break
        upper = None
        for k in range(h + 1, 25):
            if curve[k] is not None:
                upper = k
                break
        if lower is not None and upper is not None:
            lv = math.log(curve[lower])
            uv = math.log(curve[upper])
            frac = (h - lower) / (upper - lower)
            curve[h] = math.exp(lv + (uv - lv) * frac)
        elif lower is not None:
            curve[h] = curve[lower]
        else:
            return None
    return curve


def ols_fit(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = 0.0
    sxy = 0.0
    for x, y in zip(xs, ys):
        sxx += (x - mx) * (x - mx)
        sxy += (x - mx) * (y - my)
    b = sxy / sxx if sxx != 0 else 0.0
    a = my - b * mx
    return a, b, mx, my


def interp_quantile(sorted_vals, tau):
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = tau * (n - 1)
    lo = int(math.floor(pos))
    hi = lo + 1
    if hi >= n:
        return sorted_vals[n - 1]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2 == 1:
        return s[n // 2]
    return 0.5 * (s[n // 2 - 1] + s[n // 2])


def validate_result(case_id, result):
    hours = result.get("hours")
    if not isinstance(hours, list) or len(hours) != 23:
        raise ValueError("case %r: hours must be a list of exactly 23 rows" % (case_id,))
    expected = list(range(2, 25))
    got = [row.get("hour") for row in hours]
    if got != expected:
        raise ValueError("case %r: hour values must be exactly 2..24 each once" % (case_id,))
    for row in hours:
        q = row.get("q")
        if not isinstance(q, list) or len(q) != 5:
            raise ValueError("case %r: hour %r q must be a list of five numbers" % (case_id, row.get("hour")))
        for v in q:
            if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 or v > 1e9:
                raise ValueError("case %r: hour %r has a q value that is not finite and in (0, 1e9]" % (case_id, row.get("hour")))
        if any(q[i] < q[i - 1] for i in range(1, len(q))):
            raise ValueError("case %r: hour %r q values are not non-decreasing" % (case_id, row.get("hour")))


def forecast_case(case):
    known = case.get("known") or []
    p1 = 1.0
    for row in known:
        if row.get("hour") == 1:
            v = row.get("viewers")
            p1 = max(float(v) if v is not None else 1.0, 1.0)
    lp1 = math.log(p1)

    comps = case.get("comparables") or []
    pool = []
    for c in comps:
        name = c.get("name") or ""
        if "Grand Theft Auto VI" in name:
            continue
        unlock = c.get("unlock") or ""
        if "Tier: comparable" not in unlock:
            continue
        day_start = parse_ts(c.get("day_start"))
        curve = build_curve(day_start, c.get("hourly"))
        if curve is None:
            continue
        if curve[1] is None or curve[1] <= 0:
            continue
        ok = True
        for h in range(1, 25):
            if curve[h] is None:
                ok = False
                break
        if not ok:
            continue
        pool.append((name, curve))

    pool.sort(key=lambda t: t[0])

    if len(pool) < 8:
        return fallback(p1)

    l1s = [math.log(curve[1]) for _, curve in pool]
    lmin = min(l1s)
    lmax = max(l1s)
    over = max(0.0, lp1 - lmax)
    under = max(0.0, lmin - lp1)
    lpe = lp1 - (over + under) * (1.0 - CLAMP)
    lpc = min(max(lpe, lmin), lmax)

    # per-hour residuals r(h) and OLS fit of r vs l1
    residuals = {}  # h -> list of r
    for h in range(2, 25):
        rs = []
        for (name, curve), l1 in zip(pool, l1s):
            r = math.log(curve[h]) - l1
            rs.append(r)
        residuals[h] = rs

    fits = {}
    shapes = {}
    for h in range(2, 25):
        ys = residuals[h]
        a, b, mx, my = ols_fit(l1s, ys)
        fits[h] = (a, b, mx, my)
        shapes[h] = my + b * (lpc - mx)

    # nearest WSIZE members by |l1 - lpc| (stable, pool already sorted by name)
    order = list(range(len(pool)))
    order.sort(key=lambda i: abs(l1s[i] - lpc))

    hours_out = []
    for h in range(2, 25):
        a, b, mx, my = fits[h]
        ys = residuals[h]
        # scale from whole pool
        abs_devs = [abs(ys[i] - my - b * (l1s[i] - mx)) for i in range(len(pool))]
        mad = median(abs_devs)
        s = 1.4826 * mad
        if s == 0:
            mean_r = sum(ys) / len(ys)
            var = sum((y - mean_r) ** 2 for y in ys) / len(ys)
            s = math.sqrt(var)
        if len(pool) == 1:
            s = S_FLOOR
        s = max(s, S_FLOOR)

        # residuals over nearest members
        sel = order[:WSIZE]
        near_res = []
        for i in sel:
            e = math.log(pool[i][1][h]) - l1s[i] - (a + b * l1s[i])
            near_res.append(e)
        near_res.sort()

        lmed = lp1 + shapes[h]
        qs = []
        for tau in LEVELS:
            k = K_MID if (tau == 0.25 or tau == 0.75) else K_TAIL
            emp = interp_quantile(near_res, tau)
            dev = LQ * emp + (1.0 - LQ) * Z[tau] * s * k
            qs.append(math.exp(lmed + dev))
        # floor
        floor_val = max(p1 / 1000.0, 1.0)
        qs = [max(q, floor_val) for q in qs]
        # cap
        qs = [min(q, 1e9) for q in qs]
        # monotonic non-decreasing
        for i in range(1, len(qs)):
            if qs[i] < qs[i - 1]:
                qs[i] = qs[i - 1]
        hours_out.append({"hour": h, "q": qs})

    return {"hours": hours_out}


def fallback(p1):
    hours_out = []
    for h in range(2, 25):
        qs = []
        for tau in LEVELS:
            q = p1 * math.exp(Z[tau] * 0.15 * math.sqrt(h - 1))
            qs.append(q)
        floor_val = max(p1 / 1000.0, 1.0)
        qs = [max(q, floor_val) for q in qs]
        qs = [min(q, 1e9) for q in qs]
        for i in range(1, len(qs)):
            if qs[i] < qs[i - 1]:
                qs[i] = qs[i - 1]
        hours_out.append({"hour": h, "q": qs})
    return {"hours": hours_out}


def main():
    in_dir = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
    out_dir = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(in_dir.glob("*.in"), key=lambda p: p.name)
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            case = json.load(fh)
        result = forecast_case(case)
        validate_result(case.get("id"), result)
        out_name = path.name[:-3] + ".out"
        out_path = out_dir / out_name
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(result, separators=(",", ":")))
            fh.write("\n")


if __name__ == "__main__":
    main()
