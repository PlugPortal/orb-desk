"""Quant formulas for ORB Desk. Standard library only (no numpy / scipy), so the page still builds
with nothing to install. Section numbers refer to "The entire math behind quant trading bots"
(openvibes.si/quant-math). Only the formulas that fit a long-side intraday breakout desk are here.

Every function takes plain lists of floats and returns plain floats (or None when there is not
enough data). None of them raises on thin data.
"""
import math

SQRT2 = math.sqrt(2.0)
BARS_PER_DAY = 78            # 5-minute bars in a 9:30-16:00 session
DAYS_PER_YEAR = 252


# ------------------------------------------------------------ 1. returns
def log_returns(prices):
    """1.2  r_t = ln(P_t / P_{t-1}). Additive over time."""
    return [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]


# ------------------------------------------------------------ 2. volatility
def mean(xs):
    return sum(xs) / len(xs) if xs else None


def stdev(xs):
    """2.1  Sample standard deviation (n - 1)."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def annualize_vol(sigma, periods_per_year):
    """2.2  Square-root-of-time: sigma_year = sigma_bar * sqrt(bars per year)."""
    return None if sigma is None else sigma * math.sqrt(periods_per_year)


def ewma_vol(returns, lam=0.94):
    """2.3  RiskMetrics EWMA: s2_t = lam * s2_{t-1} + (1 - lam) * r_{t-1}^2. Seeded with the first squared return."""
    if len(returns) < 2:
        return None
    s2 = returns[0] ** 2
    for r in returns[1:]:
        s2 = lam * s2 + (1 - lam) * r * r
    return math.sqrt(s2)


# ------------------------------------------------------------ 3. z-scores
def zscore(x, sample):
    """3.1  z = (x - mean) / sd of a reference sample."""
    sd = stdev(sample)
    if sd is None or sd == 0:
        return None
    return (x - mean(sample)) / sd


# ------------------------------------------------------------ 4/5. correlation and beta
def corr_beta(asset, market):
    """4.2 Pearson correlation and 5.2 beta = cov(asset, market) / var(market), on aligned returns."""
    n = min(len(asset), len(market))
    if n < 10:
        return None, None
    a, m = asset[-n:], market[-n:]
    ma, mm = sum(a) / n, sum(m) / n
    cov = sum((x - ma) * (y - mm) for x, y in zip(a, m)) / (n - 1)
    va = sum((x - ma) ** 2 for x in a) / (n - 1)
    vm = sum((y - mm) ** 2 for y in m) / (n - 1)
    if va == 0 or vm == 0:
        return None, None
    return cov / math.sqrt(va * vm), cov / vm


# ------------------------------------------------------------ 10. performance
def sharpe(xs):
    """10.1  mean / sd, per period (here: per trade, in R). Not annualized."""
    sd = stdev(xs)
    return None if not sd else mean(xs) / sd


def sortino(xs, target=0.0):
    """10.2  (mean - target) / downside deviation, downside dev = sqrt(mean(min(x - target, 0)^2))."""
    if len(xs) < 2:
        return None
    dd = math.sqrt(sum(min(x - target, 0.0) ** 2 for x in xs) / len(xs))
    return None if dd == 0 else (mean(xs) - target) / dd


def max_drawdown(pnl_steps):
    """10.3  Largest peak-to-trough fall of the running total. Input: per-trade P&L (e.g. in R). Returns a number <= 0."""
    eq = peak = worst = 0.0
    for x in pnl_steps:
        eq += x
        peak = max(peak, eq)
        worst = min(worst, eq - peak)
    return worst


def tstat(xs):
    """3.3  t = mean / (sd / sqrt(n)). Roughly: |t| < 2 means the average could easily be luck."""
    sd = stdev(xs)
    return None if not sd else mean(xs) / (sd / math.sqrt(len(xs)))


def _moments(xs):
    n = len(xs)
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2          # skew, raw kurtosis (normal = 3)


def sharpe_se(xs):
    """16.3  Standard error of the Sharpe ratio, with skew g3 and kurtosis g4:
    SE = sqrt((1 - g3*SR + (g4 - 1)/4 * SR^2) / (n - 1))."""
    n = len(xs)
    sr = sharpe(xs)
    if sr is None or n < 3:
        return None
    g3, g4 = _moments(xs)
    v = (1 - g3 * sr + (g4 - 1) / 4 * sr * sr) / (n - 1)
    return math.sqrt(v) if v > 0 else None


def psr(xs, sr_benchmark=0.0):
    """16.4  Probabilistic Sharpe ratio: P(true Sharpe > benchmark) = N((SR - SR*) / SE)."""
    sr, se = sharpe(xs), sharpe_se(xs)
    if sr is None or not se:
        return None
    return norm_cdf((sr - sr_benchmark) / se)


# ------------------------------------------------------------ 11. sizing
def kelly_binary(p, b):
    """11.1  f* = p - (1 - p) / b, where p = win rate and b = average win / average loss."""
    if b is None or b <= 0 or p is None:
        return None
    return p - (1 - p) / b


# ------------------------------------------------------------ 12. tail risk
def hist_var_es(xs, level=0.95):
    """12.2 historical VaR and 12.3 expected shortfall, returned as losses (positive numbers)."""
    if len(xs) < 5:
        return None, None
    s = sorted(xs)
    k = max(1, int(math.floor((1 - level) * len(s))))
    tail = s[:k]
    return -s[k - 1], -sum(tail) / len(tail)


# ------------------------------------------------------------ 13. execution
def vwap(bars):
    """13.5  VWAP = sum(typical price * volume) / sum(volume), typical price = (h + l + c) / 3."""
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * (b.get("v") or 0) for b in bars)
    v = sum(b.get("v") or 0 for b in bars)
    return pv / v if v else None


def vwap_series(bars):
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        v = b.get("v") or 0
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        out.append(pv / vv if vv else None)
    return out


def spread_pct(bid, ask):
    """13.1  (ask - bid) / mid."""
    mid = (bid + ask) / 2
    return None if mid <= 0 or bid <= 0 else (ask - bid) / mid


# ------------------------------------------------------------ 17. costs
def cost_in_r(risk_per_share, slippage_per_share, commission, shares):
    """17.1  Round-trip cost per share (slippage in and out + commission spread over the shares), in units of R."""
    if not risk_per_share or risk_per_share <= 0:
        return None
    per_share = 2 * slippage_per_share + (commission / shares if shares else 0.0)
    return per_share / risk_per_share


def breakeven_hit_rate(r_multiple, cost_r=0.0):
    """17.3  Win rate needed to break even: p (R - c) = (1 - p)(1 + c)  ->  p* = (1 + c) / (R + 1)."""
    return (1 + cost_r) / (r_multiple + 1)


def random_walk_hit_first(price, stop, target):
    """Gambler's-ruin baseline for a driftless random walk: P(target before stop) = (price - stop) / (target - stop).
    With no edge, a 2R trade reaches the target first one time in three."""
    if target == stop:
        return None
    p = (price - stop) / (target - stop)
    return min(1.0, max(0.0, p))


# ------------------------------------------------------------ 15. options
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / SQRT2))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(S, K, T, r, sigma):
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sq
    return d1, d1 - sq


def bs_price(S, K, T, r, sigma, kind="C"):
    """15.2  Black-Scholes (European, no dividends). Single-stock US options are American; for calls on
    non-dividend stocks the price is the same, for puts it is a close approximation."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "C" else max(0.0, K - S)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    if kind == "C":
        return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)


def bs_greeks(S, K, T, r, sigma, kind="C"):
    """15.4  delta, gamma, vega (per 1.00 of vol), theta (per year)."""
    d1, d2 = _d1d2(S, K, T, r, sigma)
    pdf, sqT = norm_pdf(d1), math.sqrt(T)
    gamma = pdf / (S * sigma * sqT)
    vega = S * pdf * sqT
    if kind == "C":
        delta = norm_cdf(d1)
        theta = -S * pdf * sigma / (2 * sqT) - r * K * math.exp(-r * T) * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1
        theta = -S * pdf * sigma / (2 * sqT) + r * K * math.exp(-r * T) * norm_cdf(-d2)
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def implied_vol(price, S, K, T, r, kind="C", lo=1e-4, hi=5.0):
    """15.5  Newton's method on vega, with a bisection fallback so it always stays inside [lo, hi].
    Returns None if the price is outside what any volatility can produce."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    if not (bs_price(S, K, T, r, lo, kind) - 1e-9 <= price <= bs_price(S, K, T, r, hi, kind)):
        return None
    sigma = 0.5
    for _ in range(100):
        diff = bs_price(S, K, T, r, sigma, kind) - price
        if abs(diff) < 1e-6:
            return sigma
        if diff > 0:
            hi = sigma
        else:
            lo = sigma
        d1, _ = _d1d2(S, K, T, r, sigma)
        vega = S * norm_pdf(d1) * math.sqrt(T)
        nxt = sigma - diff / vega if vega > 1e-8 else None
        sigma = nxt if nxt is not None and lo < nxt < hi else (lo + hi) / 2
    return sigma


# ------------------------------------------------------------ trade-list summary
def trade_stats(rs):
    """Summary of a list of per-trade results in R (1R = the planned loss at the stop)."""
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    p = len(wins) / n
    aw = mean(wins) if wins else 0.0
    al = -mean(losses) if losses else 0.0
    gross_loss = -sum(losses)
    var95, es95 = hist_var_es(rs)
    return {
        "n": n, "hit": p, "avg_r": mean(rs), "avg_win": aw, "avg_loss": al,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "sharpe": sharpe(rs), "sortino": sortino(rs), "t": tstat(rs), "psr": psr(rs),
        "max_dd": max_drawdown(rs), "kelly": kelly_binary(p, aw / al if al > 0 else None),
        "var95": var95, "es95": es95,
    }
