"""ORB Desk - shared logic for the one-page opening-range-breakout view.

Standard library only. Used by both app.py (Streamlit) and build_page.py (GitHub Pages).
Data: Yahoo Finance chart JSON and Yahoo Finance RSS headlines (free, delayed, no key).
Long side only. This page displays a plan; it never places an order.
"""
import datetime as dt
import html
import json
import math
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (orb-desk)"}
BAR = dt.timedelta(minutes=5)
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
    bars, pre = [], []
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
    meta["pre_bars"] = pre
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


def analyze(day_bars, prev_close, cfg, now):
    """Opening-range breakout on one session. Returns a dict; never raises on thin data."""
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
    if prev_close:
        out["chg_pct"] = (out["last"] / prev_close - 1) * 100

    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)
    range_end = open_t + dt.timedelta(minutes=cfg["range_minutes"])
    cutoff = dt.datetime.combine(day, _hhmm(cfg["entry_cutoff"]), NY)
    flat_t = dt.datetime.combine(day, _hhmm(cfg["flat_by"]), NY)
    rng = [b for b in done if b["t"] < range_end]
    need = cfg["range_minutes"] // 5
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

    checks = [
        ("Stop below entry", risk > 0, f"{stop:.2f} vs {entry:.2f}"),
        ("At least 1 share fits", shares >= 1, f"{shares} sh"),
        ("Max loss / trade", loss_at_stop <= max_loss, f"${loss_at_stop:,.2f} / ${max_loss:,.2f}"),
        ("Reward : risk", rr >= cfg["min_reward_risk"], f"{rr:.1f}R"),
        ("Cost within budget", cost <= cfg["budget"], f"${cost:,.2f} / ${cfg['budget']:,.2f}"),
        ("Entry window", trig["t"] + BAR <= cutoff, f"by {cfg['entry_cutoff']}"),
    ]
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
    )
    return out


def analyze_levels(day_bars, prior_bars, pre_bars, cfg, now):
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
                 exit_px=exit_px, exit_time=exit_time,
                 pnl=round(shares * sg * (exit_px - entry), 2) + 0.0)
    return out


def analyze_ticker(ticker, cfg, now, session_date=None):
    try:
        meta, bars = fetch_bars(ticker)
    except Exception as e:
        return {"ticker": ticker, "status": "NO DATA", "error": str(e), "bars": []}
    return analyze_from_bars(ticker, meta, bars, cfg, now, session_date)


def analyze_from_bars(ticker, meta, bars, cfg, now, session_date=None):
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
    a = analyze(days[day], prev_close, cfg, now)
    a["levels"] = analyze_levels(days[day], days[prior] if prior else [], meta.get("pre_bars", []), cfg, now)
    a["ticker"] = ticker
    a["name"] = meta.get("shortName") or ticker
    return a


def build_state(cfg, now=None, session_date=None):
    """Everything the page needs. `now` and `session_date` let you replay a past session."""
    now = now or dt.datetime.now(NY)
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
    return {
        "cfg": cfg, "now": now, "scans": scans, "triggered": triggered, "focus": focus,
        "news": fetch_news(focus["ticker"]),
        "live": session_date is None,
    }


# ---------------------------------------------------------------- page
CSS = """
*{box-sizing:border-box}body{margin:0;background:#0b0906;color:#f2ead8;
font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:14px}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
.bar{border:1px solid #f5a623;padding:8px 12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.am{color:#f5a623}.gr{color:#2fd27a}.rd{color:#ff4d3d}.dim{color:#8a7a55}
.grid{display:grid;grid-template-columns:1.45fr 1fr;gap:12px;margin-top:12px}
.grid3{display:grid;grid-template-columns:1.45fr .5fr .5fr;gap:12px;margin-top:12px}
.p{border:1px solid #f5a623;min-width:0}.p>h2{margin:0;background:#f5a623;color:#0b0906;font-size:14px;
padding:5px 10px;display:flex;justify-content:space-between;gap:8px}
.b{padding:10px}table{width:100%;border-collapse:collapse}td,th{padding:4px 8px;border-bottom:1px solid #2a2212;
text-align:left;white-space:nowrap}th{color:#f5a623;font-weight:600}td.r,th.r{text-align:right}
.big{font-size:34px;font-weight:700;line-height:1.1}.tile{border:1px solid #3a2f16;padding:8px 10px;flex:1}
.tiles{display:flex;gap:10px;margin-bottom:10px}.lab{color:#8a7a55;font-size:11px;letter-spacing:.08em}
.verdict{text-align:center;font-size:40px;font-weight:700;margin:14px 0 4px}
.news td{white-space:normal}.foot{margin-top:12px;font-size:12px}
.note{border:1px dashed #f5a623;padding:8px 10px;margin-top:10px;text-align:center}
svg{display:block;width:100%;height:auto}
.nav{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:12px}
.nav a{border:1px solid #3a2f16;padding:3px 9px}.nav a:hover{border-color:#f5a623;text-decoration:none}
.nav a.on{background:#f5a623;color:#0b0906;border-color:#f5a623;font-weight:700}
@media(max-width:900px){.grid,.grid3{grid-template-columns:1fr}}
"""


def _money(x):
    return f"${x:,.2f}"


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
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad
    n = 78                                                       # 5-minute bars in a full session
    step = (W - L - R) / n
    y = lambda p: T + (hi - p) / (hi - lo) * (H - T - B)
    day = bars[0]["t"].date()
    open_t = dt.datetime.combine(day, dt.time(9, 30), NY)
    xi = lambda t: L + ((t - open_t) / BAR) * step
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="5-minute chart">']
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
    s.append("</svg>")
    return "".join(s)


STATUS_COL = {"TRIGGERED": "gr", "WAITING": "am", "BUILDING RANGE": "am", "NO TRIGGER": "dim",
              "NOT OPEN": "dim", "NO DATA": "rd"}


def with_focus(state, ticker):
    """Same scan, different ticker in focus (used to build one static page per watchlist name)."""
    focus = next(s for s in state["scans"] if s["ticker"] == ticker)
    return dict(state, focus=focus, news=fetch_news(ticker))


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
            ("Entry", "TRIGGERED", "gr", f'first 5m close over range, {a["entry"]:.2f} at {a["trigger_time"]:%H:%M}'),
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
        why = {"WAITING": f'no 5m close over {a.get("range_high", 0):.2f} yet',
               "NO TRIGGER": f'no 5m close over {a.get("range_high", 0):.2f} before {cfg["entry_cutoff"]}',
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
                    f'<td class="r">{_money(l["loss_at_stop"])}</td><td class="{vc}">{l["verdict"]}</td>'
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
    n_lv = sum(1 for s in scans for l in s.get("levels", []) if l["status"] == "TRIGGERED")

    head = (f'{e(a.get("ticker", "-"))} 5-MIN · OPENING RANGE BREAKOUT'
            f'<span>{last_txt} {chg_txt}</span>')
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ORB Desk · {e(a.get("ticker", ""))}</title>
<style>{CSS}</style></head><body>
<div class="bar"><div><b>{e(a.get("ticker", "-"))}</b> <span class="am">&lt;EQUITY&gt; DESK</span></div>
<div><span class="gr">PAPER</span> · <span class="am">{mode}</span> · SESSION {session_txt} · AS OF {now:%b %d %H:%M} ET</div></div>
{navbar}
<div class="grid">
 <div class="p"><h2>{head}</h2>{_chart(a)}</div>
 <div>
  <div class="p"><h2>SCANNER<span>watchlist of {len(scans)}</span></h2><div class="b">
   <div class="tiles"><div class="tile"><div class="lab">CHARTS SCANNED</div><div class="big">{len(scans)}</div></div>
   <div class="tile"><div class="lab">CLOSED ABOVE {cfg["range_minutes"]}M RANGE</div><div class="big gr">{n_trig}</div></div>
   <div class="tile"><div class="lab">LEVEL RETEST ENTRIES</div><div class="big" style="color:#6fb7ff">{n_lv}</div></div></div>
   <table>{"".join(rows)}</table></div></div>
 </div>
</div>
{levels_panel}
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
<div class="bar foot"><span><span class="am">DATA</span> Yahoo Finance 5-minute bars and headlines, delayed ·
 budget {_money(cfg["budget"])} · max loss {cfg["max_loss_pct"]}% · target {cfg["target_r_multiple"]}R ·
 entries until {cfg["entry_cutoff"]} · flat by {cfg["flat_by"]} ET</span>
 <span class="dim">Long side only. A display of a rule, not advice.</span></div>
</body></html>"""
