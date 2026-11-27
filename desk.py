"""ORB Desk - shared logic for the one-page opening-range-breakout view.

Standard library only. Used by both app.py (Streamlit) and build_page.py (GitHub Pages).
Data: Yahoo Finance chart JSON and Yahoo Finance RSS headlines (free, delayed, no key).
Long side only. This page displays a plan; it never places an order.
"""
import bisect
import datetime as dt
import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import quant as q

e_ = html.escape

NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (orb-desk)"}
BAR = dt.timedelta(minutes=5)                                     # Yahoo's bar; resampled to cfg["bar_minutes"]


def bar_minutes(cfg):
    return int(cfg.get("bar_minutes", 5))


def set_bar(cfg):
    """The desk runs on `bar_minutes` candles (10 by default, the timeframe Ripster trades on). Yahoo has no
    10-minute interval, so 5-minute bars are fetched and merged two at a time, aligned to the clock (9:30, 9:40, ...)."""
    global BAR
    BAR = dt.timedelta(minutes=bar_minutes(cfg))


def to_bar_size(meta, bars, cfg):
    """Resamples the regular, premarket and after-hours bars to `bar_minutes`. No-op at 5."""
    m = bar_minutes(cfg)
    if m == 5:
        return meta, bars
    meta = dict(meta, pre_bars=q.resample(meta.get("pre_bars", []), m), post_bars=q.resample(meta.get("post_bars", []), m))
    return meta, q.resample(bars, m)
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- settings
def load_config(path=None):
    with open(path or os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _hhmm(text):
    h, m = text.split(":")
    return dt.time(int(h), int(m))


# ---------------------------------------------------------------- data
def _get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
        except Exception as e:  # network errors, rate limits
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def parse_chart(raw):
    """Yahoo chart JSON -> (meta, bars). Regular-hours 5-minute bars, New York time."""
    res = json.loads(raw)["chart"]["result"][0]
    meta = res.get("meta", {})
    q = res["indicators"]["quote"][0]
    bars, pre, post = [], [], []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        t = dt.datetime.fromtimestamp(ts, NY)
        bar = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v or 0}
        if dt.time(4, 0) <= t.time() < dt.time(9, 30):
            pre.append(bar)                                      # premarket, used for PMH / PML
        elif dt.time(9, 30) <= t.time() < dt.time(16, 0):
            bars.append(bar)
        elif dt.time(16, 0) <= t.time() < dt.time(20, 0):
            post.append(bar)                                     # after hours, used only for the EMA clouds
    meta["pre_bars"] = pre
    meta["post_bars"] = post
    return meta, bars


def fetch_bars(ticker):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(ticker) + "?interval=5m&range=5d&includePrePost=true")
    return parse_chart(_get(url))


def fetch_news(ticker, limit=6):
    url = ("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
           + urllib.parse.quote(ticker) + "&region=US&lang=en-US")
    try:
        root = ET.fromstring(_get(url))
    except Exception:
        return []
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(NY)
        except Exception:
            when = None
        if title:
            if not link.startswith(("https://", "http://")):
                link = "https://finance.yahoo.com/quote/" + urllib.parse.quote(ticker)
            items.append({"title": title, "link": link, "when": when, "tags": tag_headline(title)})
    items.sort(key=lambda x: x["when"] or dt.datetime.min.replace(tzinfo=NY), reverse=True)
    return items[:limit]


TAGS = {
    "earnings": ("earnings", "results", "revenue", "guidance", "quarter"),
    "deal": ("merger", "acquire", "acquisition", "deal", "buyout", "takeover"),
    "analyst": ("upgrade", "downgrade", "price target", "analyst", "rating"),
    "regulatory": ("sec ", "probe", "lawsuit", "recall", "investigation", "regulator"),
}


def tag_headline(title):
    low = title.lower()
    return [tag for tag, words in TAGS.items() if any(w in low for w in words)]


# ---------------------------------------------------------------- logic
def split_sessions(bars):
    days = {}
    for b in bars:
        days.setdefault(b["t"].date(), []).append(b)
    return days


def rvol_at(bar, history):
    """Relative volume of a bar: its volume over the average volume of the same 5-minute slot on earlier sessions,
    plus the z-score (3.1) of that volume against those same slots."""
    same = [b["v"] for day in history for b in day if b["t"].time() == bar["t"].time() and b["v"]]
    if not same or not bar["v"]:
        return None, None
    return bar["v"] / (sum(same) / len(same)), q.zscore(bar["v"], same)


# ---------------------------------------------------------------- EMA clouds (Ripster)
def cloud_pairs(cfg):
    """(fast cloud, trend cloud) as (short, long) EMA lengths. Ripster's core set: 5/12 for momentum and the
    34/50 as the trend cloud ("never long under the 10-min 34/50")."""
    return tuple(cfg.get("ema_fast_cloud", [5, 12])), tuple(cfg.get("ema_trend_cloud", [34, 50]))


def build_clouds(bars, cfg):
    """EMA clouds on `ema_cloud_minutes` bars (10 by default, the timeframe Ripster uses for day trades),
    built from every bar given (all sessions, oldest first) so the slow EMAs carry over from day to day.
    Returns the resampled bars, each with "ema" = {(a, b): (ema_a, ema_b)} and "end" = its close time."""
    rb = q.resample(sorted(bars, key=lambda b: b["t"]), int(cfg.get("ema_cloud_minutes", 10)))
    closes = [b["c"] for b in rb]
    lens = {n for pair in cloud_pairs(cfg) for n in pair}
    ema = {n: q.ema_series(closes, n) for n in lens}
    for i, b in enumerate(rb):
        b["ema"] = {pair: (ema[pair[0]][i], ema[pair[1]][i]) for pair in cloud_pairs(cfg)}
    return rb


def cloud_at(clouds, when):
    """EMA values from the last cloud bar that had CLOSED by `when` (no look-ahead). None before the first one."""
    if not clouds:
        return None
    i = bisect.bisect_right([c["end"] for c in clouds], when) - 1
    return clouds[i]["ema"] if i >= 0 else None


def cloud_agrees(ema, pair, price, side, strict):
    """Does this cloud agree with a trade at `price`? strict (trend cloud): price must be over the whole cloud for a
    long, under it for a short. Not strict (fast cloud): the cloud must point the trade's way (fast EMA over slow for
    a long) and price must not be on the wrong side of it. None while the EMAs are still warming up."""
    pos, bull = q.cloud_state(price, *(ema or {}).get(pair, (None, None)))
    if pos is None:
        return None
    if side == "LONG":
        return pos == "above" if strict else (bull and pos != "below")
    return pos == "below" if strict else ((not bull) and pos != "above")


def cloud_checks(ema, price, side, cfg):
    """(fast_ok, trend_ok) plus the optional risk-check rows switched on in config.json."""
    fast, trend = cloud_pairs(cfg)
    f_ok, t_ok = cloud_agrees(ema, fast, price, side, False), cloud_agrees(ema, trend, price, side, True)
    m = cfg.get("ema_cloud_minutes", 10)
    word = "over" if side == "LONG" else "under"
    rows = []
    if cfg.get("require_ema_cloud"):
        rows.append((f"{m}m {trend[0]}/{trend[1]} cloud", bool(t_ok),
                     f"{word} the cloud" if t_ok else ("cloud warming up" if t_ok is None else f"not {word} the cloud")))
    if cfg.get("require_fast_cloud"):
        rows.append((f"{m}m {fast[0]}/{fast[1]} cloud", bool(f_ok),
                     ("points " + ("up" if side == "LONG" else "down")) if f_ok
                     else ("cloud warming up" if f_ok is None else "against the trade")))
    return f_ok, t_ok, rows


def analyze(day_bars, prev_close, cfg, now, history=None, clouds=None):
    """Opening-range breakout on one session. Returns a dict; never raises on thin data.
    `history` = earlier sessions' bars (oldest first), used only for relative volume.
    `clouds` = output of build_clouds(), used for the optional EMA cloud checks."""
    out = {"status": "NO DATA", "bars": [], "prev_close": prev_close}
    if not day_bars:
        return out
    day = day_bars[0]["t"].date()
    done = [b for b in day_bars if b["t"] + BAR <= now]          # completed bars only
    out["bars"] = done
    out["date"] = day
    if not done:
        out["status"] = "NOT OPEN"
        return out
    out["last"] = done[-1]["c"]
    out["last_time"] = done[-1]["t"] + BAR
    out["vwap"] = q.vwap(done)
    if prev_close:
        out["chg_pct"] = (out["last"] / prev_close - 1) * 100

    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)
    range_end = open_t + dt.timedelta(minutes=cfg["range_minutes"])
    cutoff = dt.datetime.combine(day, _hhmm(cfg["entry_cutoff"]), NY)
    flat_t = dt.datetime.combine(day, _hhmm(cfg["flat_by"]), NY)
    rng = [b for b in done if b["t"] < range_end]
    need = max(1, cfg["range_minutes"] // bar_minutes(cfg))
    if len(rng) < need:
        out["status"] = "BUILDING RANGE"
        return out
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    out.update(range_high=hi, range_low=lo, range_end=range_end, cutoff=cutoff, flat_t=flat_t)

    trig = next((b for b in done if range_end <= b["t"] and b["t"] + BAR <= cutoff and round(b["c"], 2) > round(hi, 2)), None)
    if trig is None:
        session_over = now >= cutoff
        out["status"] = "NO TRIGGER" if session_over else "WAITING"
        out["gap_to_high_pct"] = (hi / out["last"] - 1) * 100
        return out

    entry = round(trig["c"], 2)
    stop = math.floor(((hi + lo) / 2) * 100) / 100               # range midpoint, rounded down
    risk = round(entry - stop, 2)
    target = round(entry + cfg["target_r_multiple"] * risk, 2)
    max_loss = round(cfg["budget"] * cfg["max_loss_pct"] / 100, 2)
    by_loss = math.floor(max_loss / risk) if risk > 0 else 0
    by_budget = math.floor(cfg["budget"] / entry)
    shares = max(0, min(by_loss, by_budget))
    decided = "max loss per trade" if by_loss < by_budget else "budget"
    cost = round(shares * entry, 2)
    loss_at_stop = round(shares * risk, 2)
    rr = round((target - entry) / risk, 2) if risk > 0 else 0
    vwap_t = q.vwap([b for b in done if b["t"] <= trig["t"]])
    rvol, vol_z = rvol_at(trig, history or [])
    cost_r = q.cost_in_r(risk, cfg.get("slippage_per_share", 0.01), cfg.get("commission_per_trade", 0.0), shares or 1)
    be_hit = q.breakeven_hit_rate(rr, cost_r or 0) if rr > 0 else None

    checks = [
        ("Stop below entry", risk > 0, f"{stop:.2f} vs {entry:.2f}"),
        ("At least 1 share fits", shares >= 1, f"{shares} sh"),
        ("Max loss / trade", loss_at_stop <= max_loss, f"${loss_at_stop:,.2f} / ${max_loss:,.2f}"),
        ("Reward : risk", rr >= cfg["min_reward_risk"], f"{rr:.1f}R"),
        ("Cost within budget", cost <= cfg["budget"], f"${cost:,.2f} / ${cfg['budget']:,.2f}"),
        ("Entry window", trig["t"] + BAR <= cutoff, f"by {cfg['entry_cutoff']}"),
    ]
    # optional filters, off unless switched on in config.json
    if cfg.get("require_above_vwap"):
        checks.append(("Entry above VWAP", vwap_t is not None and entry > vwap_t,
                       f"{entry:.2f} vs {vwap_t:.2f}" if vwap_t else "no volume"))
    if cfg.get("min_rvol"):
        checks.append(("Relative volume", rvol is not None and rvol >= cfg["min_rvol"],
                       f"{rvol:.1f}x / {cfg['min_rvol']}x" if rvol else "no history"))
    ema_t = cloud_at(clouds, trig["t"] + BAR)                    # clouds as they stood when the trigger bar closed
    fast_ok, trend_ok, crow = cloud_checks(ema_t, entry, "LONG", cfg)
    checks += crow
    verdict = "PASS" if all(ok for _, ok, _ in checks) else "BLOCK"

    # what happened after the trigger (only meaningful once later bars exist)
    result, exit_px, exit_time = "OPEN", out["last"], out["last_time"]
    for b in done:
        if b["t"] <= trig["t"]:
            continue
        if b["l"] <= stop:                                       # if both hit in one bar, assume stop first
            result, exit_px, exit_time = "STOPPED", stop, b["t"] + BAR
            break
        if b["h"] >= target:
            result, exit_px, exit_time = "TARGET", target, b["t"] + BAR
            break
        if b["t"] + BAR >= flat_t:
            result, exit_px, exit_time = "FLAT AT CLOSE", round(b["c"], 2), b["t"] + BAR
            break
    out.update(
        status="TRIGGERED", trigger_time=trig["t"] + BAR, entry=entry, stop=stop, risk=risk,
        target=target, max_loss=max_loss, shares=shares, decided_by=decided, cost=cost,
        loss_at_stop=loss_at_stop, rr=rr, checks=checks, verdict=verdict,
        result=result, exit_px=exit_px, exit_time=exit_time,
        pnl=round(shares * (exit_px - entry), 2) + 0.0,
        over_high_pct=(entry / hi - 1) * 100,
        vwap_at_entry=vwap_t, rvol=rvol, vol_z=vol_z, cost_r=cost_r, breakeven_hit=be_hit,
        ema_at_entry=ema_t, fast_ok=fast_ok, trend_ok=trend_ok,
        r_result=(exit_px - entry) / risk if risk > 0 else None,
    )
    return out


def analyze_levels(day_bars, prior_bars, pre_bars, cfg, now, clouds=None):
    """Second setup: retest of the prior-day high/low and premarket high/low.

    Long above PDH / PMH, short below PDL / PML. A level must first be broken by a 5-minute
    close, then touched again and closed back on the breakout side: that close is the entry.
    Stop is a fixed percent of the entry price; target is a multiple of that risk.
    """
    if not day_bars:
        return []
    day = day_bars[0]["t"].date()
    done = [b for b in day_bars if b["t"] + BAR <= now]
    cutoff = dt.datetime.combine(day, _hhmm(cfg["entry_cutoff"]), NY)
    flat_t = dt.datetime.combine(day, _hhmm(cfg["flat_by"]), NY)
    sp = cfg.get("level_stop_pct", 1) / 100
    rm = cfg.get("level_target_r", 2)
    tol = cfg.get("retest_tolerance_pct", 0.1) / 100
    max_loss = round(cfg["budget"] * cfg["max_loss_pct"] / 100, 2)
    pre = [b for b in pre_bars if b["t"].date() == day and b["t"] + BAR <= now]
    defs = {}
    if prior_bars:
        defs["PDH"] = ("LONG", max(b["h"] for b in prior_bars))
        defs["PDL"] = ("SHORT", min(b["l"] for b in prior_bars))
    if len(pre) >= 3:
        defs["PMH"] = ("LONG", max(b["h"] for b in pre))
        defs["PML"] = ("SHORT", min(b["l"] for b in pre))
    out = []
    for name in ("PDH", "PMH", "PDL", "PML"):
        if name not in defs:
            out.append({"name": name, "status": "N/A"})
            continue
        side, level = defs[name]
        sg = 1 if side == "LONG" else -1
        r = {"name": name, "side": side, "level": level, "status": "NOT OPEN"}
        out.append(r)
        if not done:
            continue
        r["dist_pct"] = (level / done[-1]["c"] - 1) * 100
        prev, bi = done[0]["o"], None
        for i, b in enumerate(done):
            if b["t"] + BAR > cutoff:
                break
            if sg * (prev - level) <= 0 and sg * (b["c"] - level) > 0:
                bi = i
                break
            prev = b["c"]
        if bi is None:
            r["status"] = "NO BREAK" if now >= cutoff else "WAITING"
            continue
        r["break_time"] = done[bi]["t"] + BAR
        ei, failed = None, False
        for j in range(bi + 1, len(done)):
            b = done[j]
            if b["t"] + BAR > cutoff:
                break
            touch = b["l"] <= level * (1 + tol) if sg > 0 else b["h"] >= level * (1 - tol)
            if touch:
                if sg * (b["c"] - level) > 0:
                    ei = j
                else:
                    failed = True
                break
        if failed:
            r["status"] = "FAILED RETEST"
            continue
        if ei is None:
            r["status"] = "NO RETEST" if now >= cutoff else "BROKE, AWAITING RETEST"
            continue
        entry = round(done[ei]["c"], 2)
        risk = round(entry * sp, 2)
        stop = round(entry - sg * risk, 2)
        target = round(entry + sg * rm * risk, 2)
        by_loss = math.floor(max_loss / risk) if risk > 0 else 0
        by_budget = math.floor(cfg["budget"] / entry)
        shares = max(0, min(by_loss, by_budget))
        ok = shares >= 1 and round(shares * risk, 2) <= max_loss and shares * entry <= cfg["budget"]
        fast_ok, trend_ok, crow = cloud_checks(cloud_at(clouds, done[ei]["t"] + BAR), entry, side, cfg)
        ok = ok and all(c_ok for _, c_ok, _ in crow)
        if side == "SHORT" and not cfg.get("trade_shorts", False):
            verdict = "WATCH ONLY"
        else:
            verdict = "PASS" if ok else "BLOCK"
        result, exit_px, exit_time = "OPEN", done[-1]["c"], done[-1]["t"] + BAR
        for b in done[ei + 1:]:
            if (sg > 0 and b["l"] <= stop) or (sg < 0 and b["h"] >= stop):   # both in one bar: stop first
                result, exit_px, exit_time = "STOPPED", stop, b["t"] + BAR
                break
            if (sg > 0 and b["h"] >= target) or (sg < 0 and b["l"] <= target):
                result, exit_px, exit_time = "TARGET", target, b["t"] + BAR
                break
            if b["t"] + BAR >= flat_t:
                result, exit_px, exit_time = "FLAT AT CLOSE", round(b["c"], 2), b["t"] + BAR
                break
        r.update(status="TRIGGERED", trigger_time=done[ei]["t"] + BAR, entry=entry, stop=stop,
                 target=target, risk=risk, shares=shares, cost=round(shares * entry, 2),
                 loss_at_stop=round(shares * risk, 2), verdict=verdict, result=result,
                 exit_px=exit_px, exit_time=exit_time, fast_ok=fast_ok, trend_ok=trend_ok,
                 cloud_fail=[n for n, c_ok, _ in crow if not c_ok],
                 pnl=round(shares * sg * (exit_px - entry), 2) + 0.0,
                 r_result=sg * (exit_px - entry) / risk if risk > 0 else None)
    return out


# ---------------------------------------------------------------- confluence arrows
def confluence_score(bar, vwap_now, ema, rvol, a, cfg):
    """Five confirmations, scored for a long and for a short on one closed 5-minute bar:
    1 close vs VWAP, 2 close vs the 10m trend cloud (34/50), 3 fast cloud (5/12) direction and side,
    4 close beyond the opening range (over the high for a long, under the low for a short),
    5 relative volume at or over `confluence_rvol` (counts for both sides: volume confirms whichever way it breaks).
    Returns (long_points, short_points, {name: (long_ok, short_ok)})."""
    c = bar["c"]
    fast, trend = cloud_pairs(cfg)
    rv_ok = rvol is not None and rvol >= cfg.get("confluence_rvol", 1.5)
    hi, lo = a.get("range_high"), a.get("range_low")
    parts = {
        "VWAP": (vwap_now is not None and c > vwap_now, vwap_now is not None and c < vwap_now),
        f"{trend[0]}/{trend[1]}": (bool(cloud_agrees(ema, trend, c, "LONG", True)),
                                   bool(cloud_agrees(ema, trend, c, "SHORT", True))),
        f"{fast[0]}/{fast[1]}": (bool(cloud_agrees(ema, fast, c, "LONG", False)),
                                 bool(cloud_agrees(ema, fast, c, "SHORT", False))),
        "Range": (hi is not None and round(c, 2) > round(hi, 2), lo is not None and round(c, 2) < round(lo, 2)),
        "Volume": (rv_ok, rv_ok),
    }
    return sum(l for l, _ in parts.values()), sum(sh for _, sh in parts.values()), parts


def _play(done, i, entry, stop, target, side, flat_t):
    """Walks the bars after index i: first of stop (checked first when both are in one bar), target, or flat time."""
    sg = 1 if side == "LONG" else -1
    for b in done[i + 1:]:
        if (sg > 0 and b["l"] <= stop) or (sg < 0 and b["h"] >= stop):
            return "STOPPED", stop, b["t"] + BAR
        if (sg > 0 and b["h"] >= target) or (sg < 0 and b["l"] <= target):
            return "TARGET", target, b["t"] + BAR
        if b["t"] + BAR >= flat_t:
            return "FLAT AT CLOSE", round(b["c"], 2), b["t"] + BAR
    return "OPEN", round(done[-1]["c"], 2), done[-1]["t"] + BAR


def confluence_arrows(a, cfg, now, history=None, clouds=None):
    """BUY / SELL arrows at the confluence point. A setup must have triggered first (ORB or a level retest; shorts only
    from PDL / PML retests). From its trigger bar to the entry cutoff, the arrow goes on the first closed bar whose
    score for that side reaches `confluence_min` (4 of 5 by default) while the setup's stop has not been touched.
    At most one BUY and one SELL per session: the earliest. Fills a["conf"] (score per bar) and a["arrows"]."""
    done = a.get("bars") or []
    a["conf"], a["arrows"] = [], []
    if not done:
        return a
    vw = q.vwap_series(done)
    slots = {}
    for day_bars in history or []:
        for h in day_bars:
            if h["v"]:
                slots.setdefault(h["t"].time(), []).append(h["v"])
    for b, v in zip(done, vw):
        ema = cloud_at(clouds, b["t"] + BAR)
        same = slots.get(b["t"].time())
        rv = b["v"] / (sum(same) / len(same)) if same and b["v"] else None
        L_, S_, parts = confluence_score(b, v, ema, rv, a, cfg)
        a["conf"].append({"t": b["t"], "long": L_, "short": S_, "parts": parts})
    day = done[0]["t"].date()
    cutoff = dt.datetime.combine(day, _hhmm(cfg["entry_cutoff"]), NY)
    flat_t = dt.datetime.combine(day, _hhmm(cfg["flat_by"]), NY)
    need = int(cfg.get("confluence_min", 4))
    rm = cfg.get("target_r_multiple", 2)
    setups = []
    if a.get("status") == "TRIGGERED":
        setups.append(("ORB", "LONG", a["trigger_time"], a["stop"]))
    for l in a.get("levels", []):
        if l.get("status") == "TRIGGERED":
            setups.append((l["name"], l["side"], l["trigger_time"], l["stop"]))
    for side in ("LONG", "SHORT"):
        best = None
        for name, sd, trig_end, stop in setups:
            if sd != side:
                continue
            sg = 1 if side == "LONG" else -1
            for i, b in enumerate(done):
                if b["t"] + BAR < trig_end:
                    continue
                if b["t"] + BAR > cutoff:
                    break
                if b["t"] + BAR > trig_end and ((sg > 0 and b["l"] <= stop) or (sg < 0 and b["h"] >= stop)):
                    break                                        # setup stopped out before confluence arrived
                score = a["conf"][i]["long" if sg > 0 else "short"]
                if score >= need and sg * (b["c"] - stop) > 0:
                    if best is None or b["t"] < best[1]["t"]:
                        best = (i, b, name, stop, score)
                    break
        if best is None:
            continue
        i, b, name, stop, score = best
        entry = round(b["c"], 2)
        risk = round(abs(entry - stop), 2)
        target = round(entry + (1 if side == "LONG" else -1) * rm * risk, 2)
        res, px, t_out = _play(done, i, entry, stop, target, side, flat_t)
        a["arrows"].append({
            "side": side, "signal": "BUY" if side == "LONG" else "SELL", "setup": name, "t": b["t"],
            "time": b["t"] + BAR, "entry": entry, "stop": stop, "target": target, "risk": risk, "score": score,
            "need": need, "parts": a["conf"][i]["parts"], "result": res, "exit_px": px, "exit_time": t_out,
            "r_result": ((px - entry) if side == "LONG" else (entry - px)) / risk if risk > 0 else None,
            "watch_only": side == "SHORT" and not cfg.get("trade_shorts", False),
        })
    return a


# ---------------------------------------------------------------- options (needs Alpaca keys)
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _alpaca_chain(ticker, lo, hi, d0, d1):
    """Alpaca option-chain snapshots. Keys come from the environment, never from the repo."""
    key = (os.environ.get("ALPACA_API_KEY") or "").strip()
    sec = (os.environ.get("ALPACA_SECRET_KEY") or "").strip()
    if sec.startswith("PK") and not key.startswith("PK"):       # the two values were saved under each other's name
        key, sec = sec, key
    if not key or not sec:
        return None
    snaps, token = {}, None
    for _ in range(4):
        q = {"feed": "indicative", "limit": 1000, "strike_price_gte": f"{lo:.2f}", "strike_price_lte": f"{hi:.2f}",
             "expiration_date_gte": d0.isoformat(), "expiration_date_lte": d1.isoformat()}
        if token:
            q["page_token"] = token
        url = ("https://data.alpaca.markets/v1beta1/options/snapshots/"
               + urllib.parse.quote(ticker.replace("-", ".")) + "?" + urllib.parse.urlencode(q))
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec,
                                                   "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read())
        snaps.update(body.get("snapshots") or {})
        token = body.get("next_page_token")
        if not token:
            break
    return snaps


def option_ideas(ticker, spot, cfg, today, chain=None, now=None):
    """Out-of-the-money calls and puts for the next few sessions, sized to the budget.

    Stop is a percent of the premium paid (ask); targets are 1x, 2x, 3x that risk.
    """
    days, d1 = cfg.get("option_days", 5), today
    n = 1
    while n < days:                                              # count trading days, today included
        d1 += dt.timedelta(days=1)
        if d1.weekday() < 5:
            n += 1
    try:
        snaps = chain if chain is not None else _alpaca_chain(ticker, spot * 0.85, spot * 1.15, today, d1)
    except urllib.error.HTTPError as err:
        return {"status": f"ERROR {err.code}", "calls": [], "puts": []}
    except Exception as err:
        return {"status": f"ERROR {type(err).__name__}", "calls": [], "puts": []}
    if snaps is None:
        return {"status": "NO KEYS", "calls": [], "puts": []}
    sp = cfg.get("option_stop_pct", 15) / 100
    lo_n, hi_n = cfg.get("option_otm_min", 2), cfg.get("option_otm_max", 5)
    max_loss = cfg["budget"] * cfg["max_loss_pct"] / 100
    book = {}
    for sym, s in snaps.items():
        m = OCC.match(sym)
        if not m:
            continue
        exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
        if not (today <= exp <= d1):
            continue
        qt = s.get("latestQuote") or {}
        book.setdefault((m.group(3), exp), []).append((int(m.group(4)) / 1000, qt.get("bp") or 0, qt.get("ap") or 0, sym))
    out = {"status": "OK", "calls": [], "puts": []}
    for kind, name in (("C", "calls"), ("P", "puts")):
        exps = sorted(e for k, e in book if k == kind)[: cfg.get("option_expiries_shown", 2)]
        for exp in exps:
            ladder = sorted((x for x in book[(kind, exp)] if (x[0] > spot if kind == "C" else x[0] < spot)),
                            key=lambda x: x[0], reverse=(kind == "P"))
            for i, (strike, bid, ask, sym) in enumerate(ladder, start=1):
                if not (lo_n <= i <= hi_n):
                    continue
                row = {"symbol": sym, "expiry": exp, "dte": (exp - today).days, "strike": strike, "otm": i,
                       "bid": bid, "ask": ask}
                row.update(_option_math(kind, spot, strike, bid, ask, exp, now, cfg))
                if ask <= 0:
                    row.update(verdict="BLOCK", reason="no quote", qty=0)
                else:
                    qty = math.floor(cfg["budget"] / (ask * 100))
                    row.update(cost=ask * 100, stop=ask * (1 - sp), t1=ask * (1 + sp), t2=ask * (1 + 2 * sp),
                               t3=ask * (1 + 3 * sp), qty=qty, loss_at_stop=qty * ask * 100 * sp)
                    if qty < 1:
                        row.update(verdict="BLOCK", reason="over budget")
                    elif ask * sp < 0.01:
                        row.update(verdict="BLOCK", reason="stop under 1 cent")
                    elif bid <= ask * (1 - sp):
                        row.update(verdict="BLOCK", reason="spread wider than stop")
                    elif row["loss_at_stop"] > max_loss + 0.005:
                        row.update(verdict="BLOCK", reason="over max loss")
                    else:
                        row.update(verdict="PASS", reason="")
                out[name].append(row)
    return out


def _option_math(kind, spot, strike, bid, ask, exp, now, cfg):
    """13.1 spread, 15.5 implied vol from the mid, 15.4 delta and theta. Black-Scholes, no dividends."""
    if ask <= 0:
        return {}
    now = now or dt.datetime.now(NY)
    T = (dt.datetime.combine(exp, dt.time(16, 0), NY) - now).total_seconds() / (365 * 86400)
    T = max(T, 15 * 60 / (365 * 86400))                          # floor at 15 minutes so 0DTE near the bell stays finite
    r = cfg.get("risk_free_rate", 0.04)
    mid = (bid + ask) / 2 if bid > 0 else ask
    out = {"mid": mid, "spread_pct": q.spread_pct(bid, ask),
           "breakeven": strike + ask if kind == "C" else strike - ask}
    if mid < cfg.get("iv_min_premium", 0.05):                    # a quote of a few cents is mostly rounding: IV would be noise
        return out
    iv = q.implied_vol(mid, spot, strike, T, r, kind)
    if iv:
        g = q.bs_greeks(spot, strike, T, r, iv, kind)
        out.update(iv=iv, delta=g["delta"])
        if T * 365 < 1:                                          # under a day left: decay = all remaining time value
            intrinsic = max(0.0, spot - strike) if kind == "C" else max(0.0, strike - spot)
            out["theta_day"] = -max(0.0, mid - intrinsic) * 100
        else:
            out["theta_day"] = g["theta"] / 365 * 100            # $ per contract per calendar day
    return out


def analyze_ticker(ticker, cfg, now, session_date=None):
    try:
        meta, bars = fetch_bars(ticker)
    except Exception as e:
        return {"ticker": ticker, "status": "NO DATA", "error": str(e), "bars": []}
    return analyze_from_bars(ticker, meta, bars, cfg, now, session_date)


def analyze_from_bars(ticker, meta, bars, cfg, now, session_date=None):
    set_bar(cfg)
    meta, bars = to_bar_size(meta, bars, cfg)
    days = split_sessions(bars)
    dates = sorted(d for d in days if d <= now.date())
    if session_date:
        dates = [d for d in dates if d <= session_date]
    if not dates:
        return {"ticker": ticker, "status": "NO DATA", "bars": []}
    day = dates[-1]
    prior = dates[-2] if len(dates) > 1 else None
    prev_close = days[prior][-1]["c"] if prior else None
    # Yahoo's own previous close is the official one; use it only for the live/latest session
    if meta.get("previousClose") and day == max(days):
        prev_close = meta["previousClose"]
    ext = cfg.get("ema_cloud_extended_hours", True)              # Ripster keeps premarket / after hours on
    cbars = bars + (meta.get("pre_bars", []) + meta.get("post_bars", []) if ext else [])
    clouds = build_clouds([b for b in cbars if b["t"].date() <= day], cfg)
    a = analyze(days[day], prev_close, cfg, now, history=[days[d] for d in dates[:-1]], clouds=clouds)
    a["levels"] = analyze_levels(days[day], days[prior] if prior else [], meta.get("pre_bars", []), cfg, now, clouds)
    confluence_arrows(a, cfg, now, history=[days[d] for d in dates[:-1]], clouds=clouds)
    a["cloud_bars"] = [cloud_at(clouds, b["t"] + BAR) for b in a["bars"]]
    a["cloud_now"] = a["cloud_bars"][-1] if a["cloud_bars"] else None
    a["ticker"] = ticker
    a["name"] = meta.get("shortName") or ticker
    a["_days"] = {d: days[d] for d in dates}                      # kept for beta / volatility on the focus ticker
    a["history"] = backtest_sessions(ticker, days, dates[:-1], meta.get("pre_bars", []), cfg, clouds)
    return a


def backtest_sessions(ticker, days, past_dates, pre_bars, cfg, clouds=None):
    """16.1 No look-ahead: replay the same rules on each COMPLETED earlier session only (never the one in focus).
    Returns one row per trigger with its result in R (1R = the planned loss at the stop)."""
    rows = []
    for i, d in enumerate(past_dates):
        end = dt.datetime.combine(d, dt.time(16, 5), NY)
        prior = days[past_dates[i - 1]] if i else []
        prev_close = prior[-1]["c"] if prior else None
        a = analyze(days[d], prev_close, cfg, end, history=[days[x] for x in past_dates[:i]], clouds=clouds)
        if a.get("status") == "TRIGGERED" and a.get("r_result") is not None:
            rows.append({"setup": "ORB", "ticker": ticker, "t": a["trigger_time"], "r": a["r_result"],
                         "cost_r": a["cost_r"] or 0.0, "result": a["result"],
                         "fast_ok": a.get("fast_ok"), "trend_ok": a.get("trend_ok")})
        a["levels"] = analyze_levels(days[d], prior, pre_bars, cfg, end, clouds)
        confluence_arrows(a, cfg, end, history=[days[x] for x in past_dates[:i]], clouds=clouds)
        for ar in a["arrows"]:
            if ar["r_result"] is not None:
                c = q.cost_in_r(ar["risk"], cfg.get("slippage_per_share", 0.01), cfg.get("commission_per_trade", 0.0), 1) or 0.0
                rows.append({"setup": ar["signal"] + " ARROW", "ticker": ticker, "t": ar["time"], "r": ar["r_result"],
                             "cost_r": c, "result": ar["result"]})
        for l in a["levels"]:
            if l.get("status") == "TRIGGERED" and l.get("r_result") is not None:
                c = q.cost_in_r(l["risk"], cfg.get("slippage_per_share", 0.01),
                                cfg.get("commission_per_trade", 0.0), l["shares"] or 1) or 0.0
                rows.append({"setup": "LEVEL " + l["side"], "ticker": ticker, "t": l["trigger_time"],
                             "r": l["r_result"], "cost_r": c, "result": l["result"],
                             "fast_ok": l.get("fast_ok"), "trend_ok": l.get("trend_ok")})
    return rows


def backtest_summary(scans, cfg):
    """Pools every past trigger across the watchlist and summarises it per setup (sections 10, 11, 12, 16, 17)."""
    rows = sorted((r for s in scans for r in s.get("history", [])), key=lambda r: r["t"])
    fast, trend = cloud_pairs(cfg)
    tn, fn = f"{trend[0]}/{trend[1]}", f"{fast[0]}/{fast[1]}"
    # every trigger is replayed whatever the config says, then split by whether the clouds agreed with it,
    # so the table shows what each cloud filter would have done to the same trades
    groups = []
    for setup in ("ORB", "LEVEL LONG", "LEVEL SHORT"):
        base = [r for r in rows if r["setup"] == setup]
        groups.append((setup, base))
        groups.append((f"{setup} + {tn}", [r for r in base if r.get("trend_ok")]))
        groups.append((f"{setup} + {tn} + {fn}", [r for r in base if r.get("trend_ok") and r.get("fast_ok")]))
        groups.append((f"{setup} against {tn}", [r for r in base if r.get("trend_ok") is False]))
    for setup in ("BUY ARROW", "SELL ARROW"):
        groups.append((setup, [r for r in rows if r["setup"] == setup]))
    out = []
    for setup, rs in groups:
        net = [r["r"] - r["cost_r"] for r in rs]
        st = q.trade_stats(net)
        st["setup"] = setup
        st["gross_avg_r"] = q.mean([r["r"] for r in rs]) if rs else None
        st["sessions"] = len({r["t"].date() for r in rs})
        st["avg_cost_r"] = q.mean([r["cost_r"] for r in rs]) if rs else None
        out.append(st)
    dates = sorted({d for s in scans for d in list(s.get("_days", {}))[:-1]})
    return {"setups": out, "sessions": dates}


def focus_quant(focus, scans, cfg, now):
    """Numbers for the QUANT panel on the ticker in focus."""
    days = focus.get("_days") or {}
    if not days or not focus.get("bars"):
        return {}
    closes_by_day = [[b["c"] for b in bs] for _, bs in sorted(days.items())]
    rets = [r for cl in closes_by_day for r in q.log_returns(cl)]   # within-session 5m returns, no overnight gaps
    sig = q.stdev(rets)
    out = {"sigma5": sig, "ewma5": q.ewma_vol(rets), "ann_vol": q.annualize_vol(sig, 390 // bar_minutes(cfg) * q.DAYS_PER_YEAR),
           "n_rets": len(rets), "vwap": focus.get("vwap")}
    if focus.get("vwap") and focus.get("last"):
        out["vs_vwap_pct"] = (focus["last"] / focus["vwap"] - 1) * 100
    # 4.2 / 5.2 against SPY, on 5-minute returns matched by timestamp
    spy = next((s for s in scans if s["ticker"] == cfg.get("beta_vs", "SPY")), None)
    if spy and spy is not focus and spy.get("_days"):
        def rmap(dd):
            m = {}
            for bs in dd.values():
                for a, b in zip(bs, bs[1:]):
                    if a["c"] > 0 and b["c"] > 0:
                        m[b["t"]] = math.log(b["c"] / a["c"])
            return m
        fa, fm = rmap(days), rmap(spy["_days"])
        common = sorted(set(fa) & set(fm))
        out["corr"], out["beta"] = q.corr_beta([fa[t] for t in common], [fm[t] for t in common])
        out["beta_vs"] = spy["ticker"]
    if focus.get("status") == "TRIGGERED" and sig:
        last, flat_t = focus["last"], focus["flat_t"]
        bars_left = max(0, int((flat_t - max(now, focus["last_time"])) / BAR))
        out["bars_left"] = bars_left
        if focus.get("result") == "OPEN" and bars_left:
            move = last * sig * math.sqrt(bars_left)              # 2.2 square-root-of-time, in dollars
            out["target_sigmas"] = (focus["target"] - last) / move
            out["stop_sigmas"] = (last - focus["stop"]) / move
            out["rw_target_first"] = q.random_walk_hit_first(last, focus["stop"], focus["target"])
        out["rw_at_entry"] = q.random_walk_hit_first(focus["entry"], focus["stop"], focus["target"])
    return out


def build_state(cfg, now=None, session_date=None):
    """Everything the page needs. `now` and `session_date` let you replay a past session."""
    now = now or dt.datetime.now(NY)
    set_bar(cfg)
    watch = list(dict.fromkeys(cfg["watchlist"]))
    with ThreadPoolExecutor(max_workers=5) as ex:
        scans = list(ex.map(lambda t: analyze_ticker(t, cfg, now, session_date), watch))
    triggered = sorted((s for s in scans if s["status"] == "TRIGGERED"), key=lambda s: s["trigger_time"])
    want = cfg.get("ticker", "AUTO").upper()
    if want == "AUTO":
        passing = [s for s in triggered if s["verdict"] == "PASS"]
        focus = passing[0] if passing else triggered[0] if triggered else next(
            (s for s in scans if s["ticker"] == cfg.get("fallback_ticker")), scans[0])
    else:
        focus = next((s for s in scans if s["ticker"] == want), None) or analyze_ticker(want, cfg, now, session_date)
    live = session_date is None
    return {
        "cfg": cfg, "now": now, "scans": scans, "triggered": triggered, "focus": focus,
        "news": fetch_news(focus["ticker"]),
        "live": live,
        "options": _focus_options(focus, cfg, now, live),
        "quant": focus_quant(focus, scans, cfg, now),
        "backtest": backtest_summary(scans, cfg),
    }


def _focus_options(focus, cfg, now, live):
    if not live or "last" not in focus:
        return {"status": "REPLAY", "calls": [], "puts": []}
    return option_ideas(focus["ticker"], focus["last"], cfg, now.date(), now=now)


# ---------------------------------------------------------------- page
CSS = """
*{box-sizing:border-box}body{margin:0;background:#0b0906;color:#f2ead8;
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:14px}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.bar{border:1px solid #f5a623;padding:8px 12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.am{color:#f5a623}.gr{color:#2fd27a}.rd{color:#ff4d3d}.dim{color:#8a7a55}
.grid{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:12px;margin-top:12px}
.grid3{display:grid;grid-template-columns:1.45fr .5fr .5fr;gap:12px;margin-top:12px}
.p{border:1px solid #f5a623;min-width:0}.p>h2{margin:0;background:#f5a623;color:#0b0906;font-size:14px;
padding:5px 10px;display:flex;justify-content:space-between;gap:8px}
.b{padding:10px}table{width:100%;border-collapse:collapse}td,th{padding:4px 8px;border-bottom:1px solid #2a2212;
text-align:left;white-space:nowrap}th{color:#f5a623;font-weight:600}td.r,th.r{text-align:right}
.big{font-size:34px;font-weight:700;line-height:1.1}.tile{border:1px solid #3a2f16;padding:8px 10px;flex:1}
.tiles{display:flex;gap:10px;margin-bottom:10px}.lab{color:#8a7a55;font-size:11px;letter-spacing:.08em}
.verdict{text-align:center;font-size:40px;font-weight:700;margin:14px 0 4px}
.news td,.wrap td{white-space:normal}.foot{margin-top:12px;font-size:12px}
.note{border:1px dashed #f5a623;padding:8px 10px;margin-top:10px;text-align:center}
svg{display:block;width:100%;height:auto}
.nav{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:12px}
.nav a{border:1px solid #3a2f16;padding:3px 9px}.nav a:hover{border-color:#f5a623;text-decoration:none}
.nav a.on{background:#f5a623;color:#0b0906;border-color:#f5a623;font-weight:700}
@media(max-width:900px){.grid,.grid3{grid-template-columns:1fr}}
"""


def _money(x):
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def _pct(x, d=1):
    return "-" if x is None else f"{x * 100:.{d}f}%"


def _num(x, fmt="{:.2f}"):
    return "-" if x is None else fmt.format(x)


def _chart(a):
    bars = a.get("bars") or []
    if not bars:
        return '<div class="b dim">No bars yet for this session.</div>'
    W, H, L, R, T, B = 900, 380, 10, 78, 14, 26
    ys = [b["h"] for b in bars] + [b["l"] for b in bars]
    for k in ("stop", "target", "range_high", "range_low"):
        if k in a:
            ys.append(a[k])
    lo, hi = min(ys), max(ys)
    span = hi - lo
    lv_draw = [l for l in a.get("levels", []) if "level" in l and lo - span * 0.5 <= l["level"] <= hi + span * 0.5]
    for l in lv_draw:
        lo, hi = min(lo, l["level"]), max(hi, l["level"])
    cb = a.get("cloud_bars") or []
    near = [v for e_ in cb if e_ for pair in e_.values() for v in pair
            if v is not None and lo - span * 0.5 <= v <= hi + span * 0.5]
    if near:
        lo, hi = min(lo, min(near)), max(hi, max(near))
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    n = 390 // int(BAR.total_seconds() // 60)                    # bars in a full 9:30-16:00 session
    step = (W - L - R) / n
    y = lambda p: T + (hi - p) / (hi - lo) * (H - T - B)
    day = bars[0]["t"].date()
    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)
    xi = lambda t: L + ((t - open_t) / BAR) * step
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{int(BAR.total_seconds() // 60)}-minute chart">']
    for i in range(5):                                           # price grid
        p = lo + (hi - lo) * i / 4
        s.append(f'<line x1="{L}" x2="{W-R}" y1="{y(p):.1f}" y2="{y(p):.1f}" stroke="#221b0d"/>'
                 f'<text x="{W-R+6}" y="{y(p)+4:.1f}" fill="#8a7a55" font-size="11">{p:.2f}</text>')
    for hr in range(10, 16):                                     # time labels
        x = xi(dt.datetime.combine(day, dt.time(hr, 0), NY))
        s.append(f'<text x="{x:.1f}" y="{H-8}" fill="#8a7a55" font-size="11" text-anchor="middle">{hr}:00</text>')
    if "range_high" in a:
        x2 = xi(a["range_end"])
        s.append(f'<rect x="{L}" y="{y(a["range_high"]):.1f}" width="{x2-L:.1f}" '
                 f'height="{y(a["range_low"])-y(a["range_high"]):.1f}" fill="#f5a623" opacity=".10"/>')
        for k, lab in (("range_high", "RANGE HIGH"), ("range_low", "RANGE LOW")):
            s.append(f'<line x1="{L}" x2="{W-R}" y1="{y(a[k]):.1f}" y2="{y(a[k]):.1f}" stroke="#f5a623" '
                     f'stroke-dasharray="6 5" opacity=".8"/>'
                     f'<text x="{L+4}" y="{y(a[k])-5:.1f}" fill="#f5a623" font-size="11">{lab} {a[k]:.2f}</text>')
    for l in lv_draw:
        s.append(f'<line x1="{L}" x2="{W-R}" y1="{y(l["level"]):.1f}" y2="{y(l["level"]):.1f}" stroke="#6fb7ff" '
                 f'stroke-dasharray="2 4" opacity=".9"/>'
                 f'<text x="{W-R-4}" y="{y(l["level"])-4:.1f}" fill="#6fb7ff" font-size="11" text-anchor="end">'
                 f'{l["name"]} {l["level"]:.2f}</text>')
    s.append(_cloud_svg(a, bars, cb, xi, y, step, lo, hi, L, H))
    vw = q.vwap_series(bars)                                     # 13.5 running VWAP
    pts = [f'{xi(b["t"]) + step / 2:.1f},{y(v):.1f}' for b, v in zip(bars, vw) if v is not None and lo <= v <= hi]
    if len(pts) > 1:
        s.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#c38cff" stroke-width="1.3" opacity=".9"/>'
                 f'<text x="{L + 4}" y="{H - 30}" fill="#c38cff" font-size="11">VWAP {vw[-1]:.2f}</text>')
    if a.get("status") == "TRIGGERED":
        x1 = xi(a["trigger_time"])
        s.append(f'<rect x="{x1:.1f}" y="{y(a["target"]):.1f}" width="{W-R-x1:.1f}" '
                 f'height="{y(a["entry"])-y(a["target"]):.1f}" fill="#2fd27a" opacity=".10"/>')
        s.append(f'<rect x="{x1:.1f}" y="{y(a["entry"]):.1f}" width="{W-R-x1:.1f}" '
                 f'height="{y(a["stop"])-y(a["entry"]):.1f}" fill="#ff4d3d" opacity=".12"/>')
        for k, col, lab in (("target", "#2fd27a", "TARGET"), ("entry", "#f2ead8", "ENTRY"), ("stop", "#ff4d3d", "STOP")):
            s.append(f'<line x1="{x1:.1f}" x2="{W-R}" y1="{y(a[k]):.1f}" y2="{y(a[k]):.1f}" stroke="{col}" stroke-width="1.5"/>'
                     f'<rect x="{W-R+2}" y="{y(a[k])-9:.1f}" width="{R-4}" height="18" fill="{col}"/>'
                     f'<text x="{W-R+8}" y="{y(a[k])+4:.1f}" fill="#0b0906" font-size="12" font-weight="700">{a[k]:.2f}</text>'
                     f'<text x="{x1+6:.1f}" y="{y(a[k])-5:.1f}" fill="{col}" font-size="11">{lab}</text>')
    w = max(2.0, step * 0.62)
    for b in bars:
        x = xi(b["t"]) + step / 2
        col = "#2fd27a" if b["c"] >= b["o"] else "#ff4d3d"
        top, bot = y(max(b["o"], b["c"])), y(min(b["o"], b["c"]))
        s.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y(b["h"]):.1f}" y2="{y(b["l"]):.1f}" stroke="{col}"/>'
                 f'<rect x="{x-w/2:.1f}" y="{top:.1f}" width="{w:.1f}" height="{max(1.0, bot-top):.1f}" fill="{col}"/>')
    for ar in a.get("arrows", []):
        bi = next((i for i, b in enumerate(bars) if b["t"] == ar["t"]), None)
        if bi is None:
            continue
        b = bars[bi]
        x = xi(b["t"]) + step / 2
        long_ = ar["side"] == "LONG"
        col = "#2fd27a" if long_ else "#ff4d3d"
        if long_:
            y0 = y(b["l"]) + 6
            tri = f"{x:.1f},{y0:.1f} {x - 8:.1f},{y0 + 14:.1f} {x + 8:.1f},{y0 + 14:.1f}"
            ty = y0 + 28
        else:
            y0 = y(b["h"]) - 6
            tri = f"{x:.1f},{y0:.1f} {x - 8:.1f},{y0 - 14:.1f} {x + 8:.1f},{y0 - 14:.1f}"
            ty = y0 - 20
        lab = f'{ar["signal"]} {ar["entry"]:.2f} · {ar["score"]}/5'
        s.append(f'<polygon points="{tri}" fill="{col}" stroke="#0b0906" stroke-width="1"/>'
                 f'<rect x="{x - 52:.1f}" y="{ty - 11:.1f}" width="104" height="15" rx="3" fill="{col}"/>'
                 f'<text x="{x:.1f}" y="{ty:.1f}" fill="#0b0906" font-size="11" font-weight="700" '
                 f'text-anchor="middle">{lab}</text>')
    s.append("</svg>")
    return "".join(s)


CLOUD_COL = [("#2fd27a", "#ff4d3d", ".16"), ("#3fa7ff", "#ff9f1a", ".14")]   # (fast > slow, fast < slow, opacity)


def _cloud_svg(a, bars, cb, xi, y, step, lo, hi, L, H):
    """Ripster EMA clouds as shaded bands: the area between each pair's two EMAs, coloured by which one is on top.
    Fast cloud green/red, trend cloud blue/orange. Values come from the last CLOSED cloud bar at each 5-minute bar."""
    if not cb or not any(cb):
        return ""
    clip = lambda v: min(hi, max(lo, v))
    out, labels = [], []
    pairs = list(next(e for e in cb if e).keys())
    for k, pair in enumerate(pairs):
        up_c, dn_c, op = CLOUD_COL[min(k, len(CLOUD_COL) - 1)]
        seg, seg_up = [], None

        def flush():
            if len(seg) > 1:
                top = " ".join(f"{x:.1f},{y(clip(f)):.1f}" for x, f, _ in seg)
                bot = " ".join(f"{x:.1f},{y(clip(sl)):.1f}" for x, _, sl in reversed(seg))
                col = up_c if seg_up else dn_c
                out.append(f'<polygon points="{top} {bot}" fill="{col}" opacity="{op}"/>'
                           f'<polyline points="{top}" fill="none" stroke="{col}" stroke-width=".8" opacity=".55"/>')
        for b, e_ in zip(bars, cb):
            f, sl = (e_ or {}).get(pair, (None, None))
            if f is None or sl is None:
                flush()
                seg, seg_up = [], None
                continue
            up = f > sl
            x = xi(b["t"]) + step / 2
            if seg_up is not None and up != seg_up:
                seg.append((x, f, sl))                           # share the point so bands join where they flip
                flush()
                seg = []
            seg_up = up
            seg.append((x, f, sl))
        flush()
        last = cb[-1].get(pair) if cb[-1] else None
        if last and None not in last:
            labels.append((up_c if last[0] > last[1] else dn_c,
                           f'EMA {pair[0]}/{pair[1]} {last[0]:.2f}/{last[1]:.2f}'))
    for i, (col, txt) in enumerate(labels):
        out.append(f'<text x="{L + 110 + i * 190}" y="{H - 30}" fill="{col}" font-size="11">{txt}</text>')
    return "".join(out)


def _conf_strip(a, cfg):
    """One line over the chart: confluence now (long and short, out of 5), which checks are on, and the arrows."""
    conf = a.get("conf") or []
    if not conf:
        return ""
    now_ = conf[-1]
    need = int(cfg.get("confluence_min", 4))
    tag = lambda n: "gr" if n >= need else ("am" if n == need - 1 else "dim")
    chips = " ".join(f'<span class="{"gr" if l_ else ("rd" if s_ else "dim")}">{e_(k)}</span>'
                     for k, (l_, s_) in now_["parts"].items())
    arrows = " · ".join(
        f'<span class="{"gr" if ar["side"] == "LONG" else "rd"}">{"▲" if ar["side"] == "LONG" else "▼"} '
        f'{ar["signal"]} ({e_(ar["setup"])}) {ar["entry"]:.2f} at {ar["time"]:%H:%M}'
        f'{" · watch only" if ar["watch_only"] else ""}</span>' for ar in a.get("arrows", [])) \
        or '<span class="dim">no arrow yet</span>'
    return (f'<div class="b" style="border-bottom:1px solid #2a2212;padding:6px 10px">CONFLUENCE '
            f'<b class="{tag(now_["long"])}">L {now_["long"]}/5</b> | <b class="{tag(now_["short"])}">S {now_["short"]}/5</b>'
            f' <span class="dim">(arrow at {need}/5)</span> · {chips} · SIGNAL: {arrows}</div>')


def _cloud_txt(ema, price, cfg):
    """One line per cloud for the QUANT panel: which EMA is on top and where the price sits."""
    if not ema:
        return "warming up"
    parts = []
    for pair in cloud_pairs(cfg):
        pos, bull = q.cloud_state(price, *ema.get(pair, (None, None)))
        parts.append(f"{pair[0]}/{pair[1]} " + ("warming up" if pos is None else
                                                f'{"bull" if bull else "bear"}, price {pos}'))
    return " · ".join(parts)


STATUS_COL = {"TRIGGERED": "gr", "WAITING": "am", "BUILDING RANGE": "am", "NO TRIGGER": "dim",
              "NOT OPEN": "dim", "NO DATA": "rd"}


def with_focus(state, ticker):
    """Same scan, different ticker in focus (used to build one static page per watchlist name)."""
    focus = next(s for s in state["scans"] if s["ticker"] == ticker)
    return dict(state, focus=focus, news=fetch_news(ticker),
                options=_focus_options(focus, state["cfg"], state["now"], state["live"]),
                quant=focus_quant(focus, state["scans"], state["cfg"], state["now"]))


def render(state, nav=False, auto_page=False):
    cfg, a, now = state["cfg"], state["focus"], state["now"]
    e = html.escape
    navbar = ""
    if nav:
        links = [f'<a class="{"on" if auto_page else ""}" href="index.html">AUTO</a>']
        for s in sorted(state["scans"], key=lambda s: s["ticker"]):
            cls = ("on " if (not auto_page and s["ticker"] == a.get("ticker")) else "") + \
                  ("gr" if s["status"] == "TRIGGERED" else "")
            links.append(f'<a class="{cls.strip()}" href="{e(s["ticker"])}.html">{e(s["ticker"])}</a>')
        navbar = '<div class="nav"><span class="lab">TICKER</span>' + "".join(links) + "</div>"
    trig = a.get("status") == "TRIGGERED"
    scans = state["scans"]
    n_trig = len(state["triggered"])
    session = a.get("date")
    session_txt = session.strftime("%a %b %d %Y").upper() if session else "-"
    mode = "LIVE (DELAYED)" if state["live"] and session == now.date() else "REPLAY OF LAST SESSION"
    chg = a.get("chg_pct")
    chg_txt = f'<span class="{"gr" if chg >= 0 else "rd"}">{chg:+.2f}%</span>' if chg is not None else ""
    last_txt = f'{a["last"]:.2f}' if "last" in a else "-"

    # scanner rows: triggered first, then closest to the range high
    def key(s):
        return (0, s["trigger_time"]) if s["status"] == "TRIGGERED" else (1, s.get("gap_to_high_pct", 99))
    rows = []
    for s in sorted(scans, key=lambda s: (key(s)[0], str(key(s)[1])))[:12]:
        if s["status"] == "TRIGGERED":
            det = f'closed {s["entry"]:.2f} over {s["range_high"]:.2f} at {s["trigger_time"]:%H:%M}'
        elif "gap_to_high_pct" in s:
            det = f'{s["gap_to_high_pct"]:+.2f}% to range high {s["range_high"]:.2f}'
        else:
            det = s.get("error", "")[:40]
        for ar in s.get("arrows", []):
            det += f' {"▲" if ar["side"] == "LONG" else "▼"}{ar["time"]:%H:%M}'
        lt = [l["name"] for l in s.get("levels", []) if l["status"] == "TRIGGERED"]
        if lt:
            det += " · retest: " + ", ".join(lt)
        c = s.get("chg_pct")
        rows.append(f'<tr><td>{e(s["ticker"])}</td><td class="{STATUS_COL.get(s["status"], "")}">{s["status"]}</td>'
                    f'<td>{e(det)}</td><td class="r">{"" if c is None else f"{c:+.2f}%"}</td></tr>')

    news = "".join(
        f'<tr><td class="dim">{(n["when"].strftime("%b %d %H:%M") if n["when"] else "")}</td>'
        f'<td><a href="{e(n["link"], quote=True)}" target="_blank" rel="noopener">{e(n["title"])}</a>'
        f'{" <span class=am>[" + ", ".join(n["tags"]) + "]</span>" if n["tags"] else ""}</td></tr>'
        for n in state["news"]) or '<tr><td class="dim">No headlines returned.</td></tr>'

    # pipeline
    if trig:
        steps = [
            ("Scan", "PASS", "gr", f'closed over its {cfg["range_minutes"]}m range'),
            ("News", "LIVE", "gr", f'{len(state["news"])} headlines'),
            ("Entry", "TRIGGERED", "gr", f'first {bar_minutes(cfg)}m close over range, {a["entry"]:.2f} at {a["trigger_time"]:%H:%M}'),
            ("Exit", "PLAN", "gr", f'stop {a["stop"]:.2f} · target {a["target"]:.2f} · flat {cfg["flat_by"]}'),
            ("Size", "SIZED" if a["shares"] else "TOO BIG", "gr" if a["shares"] else "rd",
             f'{_money(a["max_loss"])} max loss / {_money(a["risk"])} risk · limit: {a["decided_by"]} = {a["shares"]} sh'),
            ("Risk", a["verdict"], "gr" if a["verdict"] == "PASS" else "rd", f'{len(a["checks"])} limits checked'),
            ("Result", a["result"] if a["shares"] else "NOT TAKEN",
             "gr" if a["pnl"] > 0 else ("rd" if a["pnl"] < 0 else "am"),
             f'{a["exit_px"]:.2f} at {a["exit_time"]:%H:%M} · P&L {_money(a["pnl"])}' if a["shares"]
             else f'the setup itself ended {a["result"].lower()} at {a["exit_px"]:.2f}'),
        ]
    else:
        why = {"WAITING": f'no {bar_minutes(cfg)}m close over {a.get("range_high", 0):.2f} yet',
               "NO TRIGGER": f'no {bar_minutes(cfg)}m close over {a.get("range_high", 0):.2f} before {cfg["entry_cutoff"]}',
               "BUILDING RANGE": "opening range still forming", "NOT OPEN": "market not open",
               "NO DATA": "no data returned"}.get(a.get("status"), "")
        steps = [("Scan", a.get("status", "-"), STATUS_COL.get(a.get("status"), ""), why),
                 ("News", "LIVE", "gr", f'{len(state["news"])} headlines')]
        steps += [(n, "-", "dim", "waits for an entry") for n in ("Entry", "Exit", "Size", "Risk")]
    pipe = "".join(f'<tr><td>{n}</td><td class="{c}">{e(s)}</td><td>{e(d)}</td></tr>' for n, s, c, d in steps)

    if trig:
        ticket = "".join(f"<tr><td class=am>{k}</td><td class='r {c}'>{v}</td></tr>" for k, v, c in [
            ("SIDE", "BUY", "gr"), ("SYMBOL", e(a["ticker"]), ""), ("SHARES", a["shares"], ""),
            ("ENTRY", f'{a["entry"]:.2f}', ""), ("STOP", f'{a["stop"]:.2f}', "rd"),
            ("TARGET", f'{a["target"]:.2f} ({a["rr"]:.0f}R)', "gr"), ("COST", _money(a["cost"]), ""),
            ("MAX LOSS", _money(a["loss_at_stop"]), "rd"), ("MODE", "PAPER · ALPACA", "gr")])
        risk = "".join(f'<tr><td class=am>{e(n)}</td><td class="r">{e(d)} '
                       f'<span class="{"gr" if ok else "rd"}">{"✓" if ok else "✗"}</span></td></tr>'
                       for n, ok, d in a["checks"])
        failed = [n for n, ok, _ in a["checks"] if not ok]
        reason = (f'one share costs {_money(a["entry"])}, over the {_money(cfg["budget"])} budget'
                  if not a["shares"] and a["entry"] > cfg["budget"] else "failed: " + ", ".join(failed))
        verdict = (f'<div class="verdict {"gr" if a["verdict"] == "PASS" else "rd"}">{a["verdict"]}</div>'
                   + (f'<div class="b rd" style="text-align:center">{e(reason)}</div>' if failed else ""))
        send = ('<div class="note am">This page cannot send orders. To place it, ask Claude to run '
                '/trader-broker and answer yes there.</div>')
    else:
        ticket = '<tr><td class="dim">No ticket: no entry has triggered.</td></tr>'
        risk = (f'<tr><td class=am>Budget</td><td class="r">{_money(cfg["budget"])}</td></tr>'
                f'<tr><td class=am>Max loss / trade</td><td class="r">{cfg["max_loss_pct"]}% = '
                f'{_money(cfg["budget"] * cfg["max_loss_pct"] / 100)}</td></tr>')
        verdict = '<div class="verdict dim">NO TRADE</div>'
        send = ""

    LCOL = {"TRIGGERED": "gr", "WAITING": "am", "BROKE, AWAITING RETEST": "am", "FAILED RETEST": "rd"}
    lrows = []
    for l in a.get("levels", []):
        if "level" not in l:
            lrows.append(f'<tr><td>{l["name"]}</td><td class="dim" colspan="9">no data for this level</td></tr>')
            continue
        if l["status"] == "TRIGGERED":
            vc = {"PASS": "gr", "BLOCK": "rd"}.get(l["verdict"], "am")
            rc = "gr" if l["pnl"] > 0 else ("rd" if l["pnl"] < 0 else "am")
            tail = (f'<td class="r">{l["entry"]:.2f} at {l["trigger_time"]:%H:%M}</td><td class="r rd">{l["stop"]:.2f}</td>'
                    f'<td class="r gr">{l["target"]:.2f}</td><td class="r">{l["shares"]}</td>'
                    f'<td class="r">{_money(l["loss_at_stop"])}</td><td class="{vc}">{l["verdict"]}'
                    f'{" · " + e(", ".join(l["cloud_fail"])) if l.get("cloud_fail") and l["verdict"] == "BLOCK" else ""}</td>'
                    f'<td class="{rc}">{l["result"]} {l["exit_px"]:.2f} · {_money(l["pnl"])}</td>')
        else:
            tail = f'<td class="dim" colspan="7">{l["dist_pct"]:+.2f}% from last price</td>' if "dist_pct" in l \
                else '<td class="dim" colspan="7"></td>'
        lrows.append(f'<tr><td>{l["name"]}</td><td class="{"gr" if l["side"] == "LONG" else "rd"}">{l["side"]}</td>'
                     f'<td class="r">{l["level"]:.2f}</td><td class="{LCOL.get(l["status"], "dim")}">{l["status"]}</td>{tail}</tr>')
    levels_panel = (
        '<div class="p" style="margin-top:12px"><h2>LEVELS · SECOND SETUP<span>retest of prior-day and premarket '
        f'high/low · stop {cfg.get("level_stop_pct", 1)}% · target {cfg.get("level_target_r", 2)}R</span></h2>'
        '<div style="overflow-x:auto"><table><tr><th>LEVEL</th><th>SIDE</th><th class="r">PRICE</th><th>STATUS</th>'
        '<th class="r">ENTRY</th><th class="r">STOP</th><th class="r">TARGET</th><th class="r">SHARES</th>'
        '<th class="r">LOSS AT STOP</th><th>RISK</th><th>RESULT</th></tr>' + "".join(lrows) + '</table></div>'
        '<div class="b dim">PDH/PDL = prior day high/low · PMH/PML = premarket high/low (4:00-9:30 ET). '
        + ('Shorts are marked WATCH ONLY: shown for reference and hedging, not as trades to take.' if not cfg.get("trade_shorts", False) else '')
        + '</div></div>')
    op = state.get("options") or {"status": "REPLAY", "calls": [], "puts": []}
    long_live = [n for n, ok in [("ORB", trig)] if ok] + [l["name"] for l in a.get("levels", [])
                                                           if l["status"] == "TRIGGERED" and l["side"] == "LONG"]
    short_live = [l["name"] for l in a.get("levels", []) if l["status"] == "TRIGGERED" and l["side"] == "SHORT"]
    osp = cfg.get("option_stop_pct", 15)

    def opt_rows(rows, live_names, label):
        hdr = (f'<tr><td colspan="17" class="am">{label} · '
               + (f'<span class="gr">live trigger: {", ".join(live_names)}</span>' if live_names
                  else '<span class="dim">no live trigger</span>') + '</td></tr>')
        body = []
        for r in rows:
            base = (f'<td>{r["expiry"]:%b %d} ({r["dte"]}d)</td><td class="r">{r["strike"]:g}</td>'
                    f'<td class="r dim">{r["otm"]} OTM</td><td class="r">{r["bid"]:.2f}</td><td class="r">{r["ask"]:.2f}</td>')
            if "cost" in r:
                base += (f'<td class="r">{_money(r["cost"])}</td><td class="r rd">{r["stop"]:.2f}</td>'
                         f'<td class="r gr">{r["t1"]:.2f}</td><td class="r gr">{r["t2"]:.2f}</td><td class="r gr">{r["t3"]:.2f}</td>'
                         f'<td class="r">{r["qty"]}</td>')
            else:
                base += '<td colspan="6"></td>'
            sp_ = r.get("spread_pct")
            dl = "" if r.get("delta") is None else f'{r["delta"]:+.2f}'
            th = "" if r.get("theta_day") is None else _money(r["theta_day"])
            be = "" if "breakeven" not in r else f'{r["breakeven"]:.2f}'
            wide = "rd" if sp_ and sp_ > 0.10 else ""
            base += (f'<td class="r">{_pct(r.get("iv"), 0)}</td><td class="r">{dl}</td>'
                     f'<td class="r rd">{th}</td><td class="r {wide}">{_pct(sp_, 0)}</td><td class="r">{be}</td>')
            vc = "gr" if r["verdict"] == "PASS" else "rd"
            body.append(f'<tr>{base}<td class="{vc}">{r["verdict"]}{" · " + r["reason"] if r["reason"] else ""}</td></tr>')
        return hdr + ("".join(body) or '<tr><td colspan="17" class="dim">no contracts in range</td></tr>')

    if op["status"] == "OK":
        obody = ('<div style="overflow-x:auto"><table><tr><th>EXPIRY</th><th class="r">STRIKE</th><th></th><th class="r">BID</th>'
                 f'<th class="r">ASK</th><th class="r">COST x1</th><th class="r">STOP -{osp}%</th><th class="r">1:1</th>'
                 '<th class="r">1:2</th><th class="r">1:3</th><th class="r">QTY</th>'
                 '<th class="r">IV</th><th class="r">DELTA</th><th class="r">THETA/DAY</th><th class="r">SPREAD</th>'
                 '<th class="r">BREAKEVEN</th><th>RISK</th></tr>'
                 + opt_rows(op["calls"], long_live, "CALLS for long setups")
                 + opt_rows(op["puts"], short_live, "PUTS for short setups") + '</table></div>'
                 '<div class="b dim">Indicative quotes from Alpaca: delayed and adjusted, not tradable prices. '
                 'Cost assumes buying at the ask. Untested idea: no historical option data was available to test this rule. '
                 f'IV, delta and theta are Black-Scholes from the mid quote (no dividends, rate {cfg.get("risk_free_rate", 0.04):.1%}); '
                 'theta is dollars per contract per calendar day (for 0DTE: all the time value left, gone by the close); '
                 'IV is left blank under 5 cents of premium; breakeven is at expiry. A spread over 10% is shown in red.</div>')
    else:
        why = {"NO KEYS": "Add your Alpaca paper keys as secrets to switch this panel on (see README).",
               "REPLAY": "Option quotes are shown for the live session only."}.get(
                   op["status"], f'Alpaca returned {e(op["status"])}. Check the keys and try again.')
        obody = f'<div class="b dim">{why}</div>'
    options_panel = (
        '<div class="p" style="margin-top:12px"><h2>OPTION IDEAS<span>'
        f'{cfg.get("option_otm_min", 2)}-{cfg.get("option_otm_max", 5)} strikes OTM · expiring within '
        f'{cfg.get("option_days", 5)} sessions incl. 0DTE · stop {osp}% of premium</span></h2>{obody}</div>')
    n_lv = sum(1 for s in scans for l in s.get("levels", []) if l["status"] == "TRIGGERED")

    # ---- QUANT: the formulas applied to the ticker in focus
    qv = state.get("quant") or {}
    qrows = [
        ("VWAP (13.5)", f'{_num(qv.get("vwap"))} · last {_num(qv.get("vs_vwap_pct"), "{:+.2f}%")} vs VWAP'),
        (f"{bar_minutes(cfg)}m volatility (2.1 / 2.3)", f'{_pct(qv.get("sigma5"), 2)} per bar · EWMA {_pct(qv.get("ewma5"), 2)} · '
                                      f'{qv.get("n_rets", 0)} returns'),
        ("Annualized vol (2.2)", _pct(qv.get("ann_vol"), 0)),
        (f'Beta / corr vs {qv.get("beta_vs", "SPY")} (5.2 / 4.2)',
         f'{_num(qv.get("beta"))} / {_num(qv.get("corr"))}' if "beta_vs" in qv else "SPY not on the watchlist"),
        (f'EMA clouds ({cfg.get("ema_cloud_minutes", 10)}m) now', _cloud_txt(a.get("cloud_now"), a.get("last"), cfg)),
    ]
    if trig:
        qrows += [
            ("VWAP at entry", f'{_num(a.get("vwap_at_entry"))} · entry {"above" if a.get("vwap_at_entry") and a["entry"] > a["vwap_at_entry"] else "below"}'),
            ("EMA clouds at entry", _cloud_txt(a.get("ema_at_entry"), a["entry"], cfg)),
            ("Trigger-bar volume (3.1)", f'{_num(a.get("rvol"), "{:.1f}x")} same-slot average · z {_num(a.get("vol_z"), "{:+.1f}")}'),
            ("Costs in R (17.1)", f'{_num(a.get("cost_r"), "{:.3f}R")} round trip at ${cfg.get("slippage_per_share", 0.01):.2f}/share slippage each way'),
            ("Break-even win rate (17.3)", f'{_pct(a.get("breakeven_hit"), 1)} at {a["rr"]:.0f}R after costs'),
            ("No-edge odds of target first", f'{_pct(qv.get("rw_at_entry"), 0)} at entry'
                                             + (f' · {_pct(qv.get("rw_target_first"), 0)} from last price' if "rw_target_first" in qv else "")),
        ]
        if "target_sigmas" in qv:
            qrows.append(("Distance to close (2.2)", f'target {qv["target_sigmas"]:.1f} sd away, stop {qv["stop_sigmas"]:.1f} sd, '
                                                      f'{qv["bars_left"]} bars left'))
    quant_panel = ('<div class="p"><h2>QUANT<span>formulas on the ticker in focus</span></h2><table class="wrap">'
                   + "".join(f'<tr><td class="am">{e(k)}</td><td class="r">{e(v)}</td></tr>' for k, v in qrows)
                   + '</table><div class="b dim">No-edge odds: on a random walk with no drift, price reaches the target '
                     'before the stop (entry - stop) / (target - stop) of the time; a real edge has to beat that, and beat '
                     'the break-even win rate after costs.</div></div>')

    # ---- BACKTEST: the same rules on the completed earlier sessions in the data
    bt = state.get("backtest") or {"setups": [], "sessions": []}
    brow = []
    for st_ in bt["setups"]:
        if not st_.get("n"):
            brow.append(f'<tr><td>{"&nbsp;&nbsp;" if ("+" in st_["setup"] or "against" in st_["setup"]) else ""}{e(st_["setup"])}</td>'
                        f'<td class="dim" colspan="11">no triggers</td></tr>')
            continue
        good = st_["avg_r"] > 0
        kel = st_.get("kelly")
        brow.append(
            f'<tr><td>{"&nbsp;&nbsp;" if ("+" in st_["setup"] or "against" in st_["setup"]) else ""}{e(st_["setup"])}</td><td class="r">{st_["n"]}</td><td class="r">{_pct(st_["hit"], 0)}</td>'
            f'<td class="r {"gr" if good else "rd"}">{st_["avg_r"]:+.2f}R</td><td class="r">{_num(st_.get("profit_factor"))}</td>'
            f'<td class="r">{_num(st_.get("sharpe"))}</td><td class="r">{_num(st_.get("sortino"))}</td>'
            f'<td class="r">{_num(st_.get("t"), "{:+.1f}")}</td><td class="r">{_pct(st_.get("psr"), 0)}</td>'
            f'<td class="r rd">{st_["max_dd"]:.1f}R</td><td class="r">{_num(st_.get("es95"), "{:.2f}R")}</td>'
            f'<td class="r">{"-" if kel is None else ("none" if kel <= 0 else _pct(kel, 1))}</td></tr>')
    sess = bt["sessions"]
    span = f'{sess[0]:%b %d}-{sess[-1]:%b %d}, {len(sess)} sessions' if sess else "no completed sessions"
    backtest_panel = (
        f'<div class="p"><h2>BACKTEST<span>same rules on earlier sessions · {span} · whole watchlist · net of costs</span></h2>'
        '<div style="overflow-x:auto"><table><tr><th>SETUP</th><th class="r">TRADES</th><th class="r">WIN</th>'
        '<th class="r">AVG</th><th class="r">PF</th><th class="r">SHARPE</th><th class="r">SORTINO</th>'
        '<th class="r">T-STAT</th><th class="r">PSR</th><th class="r">MAX DD</th><th class="r">ES 95%</th>'
        '<th class="r">KELLY</th></tr>' + "".join(brow) + '</table></div>'
        f'<div class="b dim">Per trade, in R (1R = the planned loss at the stop; {_money(cfg["budget"] * cfg["max_loss_pct"] / 100)} '
        f'here). Sharpe and Sortino are per trade, not annualized (10.1, 10.2). T-STAT under about 2 and PSR under about 95% '
        f'mean the average could be luck (3.3, 16.4). Kelly (11.1) is the fraction of the account to risk per trade that this '
        f'sample implies; your setting is {cfg["max_loss_pct"]}%. Yahoo keeps only about 5 days of intraday bars, so this is a '
        f'small sample from one market regime, and testing many setups on it overstates the best one (16.2). '
        f'Only completed sessions are used; the session on screen is never in it (16.1). '
        f'Rows with "+ {"/".join(map(str, cloud_pairs(cfg)[1]))}" keep only the trades where the '
        f'{cfg.get("ema_cloud_minutes", 10)}-minute EMA cloud agreed when the trigger bar closed (price over it for a long, '
        f'under it for a short); "+ {"/".join(map(str, cloud_pairs(cfg)[0]))}" also needs the fast cloud pointing the '
        f'trade\'s way; "against" is the rest. Trades from before the slow EMAs had enough bars are in none of them. '
        f'They are the same trades as the row above, filtered, not new ones. BUY / SELL ARROW: entry at the arrow bar\'s '
        f'close, the setup\'s own stop, target {cfg["target_r_multiple"]}R from there; SELL arrows count even when shorts are '
        f'watch only.</div></div>')

    head = (f'{e(a.get("ticker", "-"))} {bar_minutes(cfg)}-MIN · OPENING RANGE BREAKOUT'
            f'<span>{last_txt} {chg_txt}</span>')
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ORB Desk · {e(a.get("ticker", ""))}</title>
<style>{CSS}</style></head><body>
<div class="bar"><div><b>{e(a.get("ticker", "-"))}</b> <span class="am">&lt;EQUITY&gt; DESK</span></div>
<div><span class="gr">PAPER</span> · <span class="am">{mode}</span> · SESSION {session_txt} · AS OF {now:%b %d %H:%M} ET</div></div>
{navbar}
<div class="grid">
 <div class="p"><h2>{head}</h2>{_conf_strip(a, cfg)}{_chart(a)}</div>
 <div>
  <div class="p"><h2>SCANNER<span>watchlist of {len(scans)}</span></h2><div class="b">
   <div class="tiles"><div class="tile"><div class="lab">CHARTS SCANNED</div><div class="big">{len(scans)}</div></div>
   <div class="tile"><div class="lab">CLOSED ABOVE {cfg["range_minutes"]}M RANGE</div><div class="big gr">{n_trig}</div></div>
   <div class="tile"><div class="lab">LEVEL RETEST ENTRIES</div><div class="big" style="color:#6fb7ff">{n_lv}</div></div></div>
   <div style="overflow-x:auto"><table>{"".join(rows)}</table></div></div></div>
 </div>
</div>
{levels_panel}
{options_panel}
<div class="grid">
 {backtest_panel}
 {quant_panel}
</div>
<div class="grid">
 <div class="p"><h2>PIPELINE<span>each step feeds the next</span></h2>
  <table><tr><th>STEP</th><th>STATUS</th><th>DETAIL</th></tr>{pipe}</table></div>
 <div class="p"><h2>NEWS · {e(a.get("ticker", ""))}<span>Yahoo Finance · NY time</span></h2>
  <table class="news">{news}</table></div>
</div>
<div class="grid">
 <div class="p"><h2>ORDER TICKET</h2><table>{ticket}</table><div class="b">{send}</div></div>
 <div class="p"><h2>RISK</h2><table>{risk}</table>{verdict}
  <div class="b dim" style="text-align:center">nothing sends from this page</div></div>
</div>
<div class="bar foot"><span><span class="am">DATA</span> Yahoo Finance 5-minute bars as {bar_minutes(cfg)}-minute candles, and headlines, delayed ·
 budget {_money(cfg["budget"])} · max loss {cfg["max_loss_pct"]}% · target {cfg["target_r_multiple"]}R ·
 entries until {cfg["entry_cutoff"]} · flat by {cfg["flat_by"]} ET</span>
 <span class="dim">Long side only. A display of a rule, not advice.</span></div>
</body></html>"""
