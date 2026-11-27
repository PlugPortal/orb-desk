# ORB Desk

A one-page opening-range-breakout view. Two ways to see the same page:

- **Streamlit** (`app.py`): live page with settings in a sidebar, refreshes every 60 seconds.
- **GitHub Pages** (`build_page.py` + `.github/workflows/desk.yml`): a static page rebuilt on a schedule.

Both use `desk.py` for the rule and `config.json` for your settings. Data is Yahoo Finance (free, delayed, no key).
The page shows a plan. It never places an order.

## The rule
0. Candles are 10 minutes (`bar_minutes`), the timeframe Ripster trades on. Yahoo has no 10-minute interval, so its
   5-minute bars are merged two at a time (9:30-9:40, 9:40-9:50, ...). Set `bar_minutes` to 5 for the old behaviour.
1. Opening range = high and low of the first 10 minutes (`range_minutes`, 9:30-9:40 New York).
2. Entry = the first completed 10-minute bar that closes above the range high, before 11:30.
3. Stop = range midpoint. Target = entry + 2 x (entry - stop). Flat by 15:55.
4. Shares = the smaller of (max loss / risk per share) and (budget / entry price).

## Second setup: level retests
Levels: PDH/PDL (prior day high/low) and PMH/PML (premarket high/low, 4:00-9:30 New York).
Long above PDH/PMH, short below PDL/PML. A 10-minute close must break the level, then a later bar must
touch it again and close back on the breakout side; that close is the entry (before 11:30).
Stop = 1% of entry, target = 2R. Shorts are shown as WATCH ONLY unless `trade_shorts` is true.

## EMA clouds (Ripster)
From ripstereducation.com/ripster-clouds/ema-clouds: the 5/12 cloud is the day-trade trendline, the 34/50 is the
trend cloud, drawn on the 10-minute chart with premarket and after hours on. Its rule: never long under the 10-minute
34/50, never short over it.
- **Chart**: both clouds as shaded bands under the candles (5/12 green when the 5 EMA is on top, red when not;
  34/50 blue / orange). VWAP stays as it was. Values come from the last closed 10-minute bar, so nothing looks ahead.
- **Risk check (off by default)**: `require_ema_cloud` adds "price over the 10m 34/50 cloud" (under it for a short
  level trade); `require_fast_cloud` adds "5/12 cloud points the trade's way and price is not on the wrong side of it".
- **BACKTEST**: every setup also has rows for the trades where the 34/50 agreed, where both clouds agreed, and where
  the 34/50 was against the trade. These are the same trades filtered, so you can see what each filter would have done
  before you switch it on. With about 5 days of Yahoo data the slow EMAs need a day or so to warm up, so the sample is
  small: read the t-stat and PSR.
- Settings: `ema_cloud_minutes` (10), `ema_fast_cloud` ([5, 12]), `ema_trend_cloud` ([34, 50]),
  `ema_cloud_extended_hours` (true), `require_ema_cloud` (false), `require_fast_cloud` (false).

## BUY / SELL arrows (confluence)
Every closed 10-minute bar is scored out of 5 for a long and for a short: close vs VWAP, close vs the 10m 34/50 cloud,
the 5/12 cloud's direction, close beyond the opening range, and relative volume at or over `confluence_rvol` (1.5x).
The strip over the chart shows the latest score (`CONFLUENCE L 4/5 | S 1/5`) and which checks are green.
An arrow needs a setup first (ORB or a level retest; SELL only from PDL / PML retests). From its trigger to the entry
cutoff, the arrow goes on the first bar whose score reaches `confluence_min` (5, every check agrees) while the setup's stop is untouched.
One BUY and one SELL per session at most. SELL arrows are marked watch only unless `trade_shorts` is true. The BACKTEST
has BUY ARROW and SELL ARROW rows: entry at the arrow's close, the setup's stop, target `target_r_multiple` from there.

## Option ideas (optional, needs Alpaca paper keys)
For the ticker in focus: calls and puts 2-5 strikes out of the money, expiring within 5 sessions (0DTE included).
Stop = 15% of the premium; targets at 1:1, 1:2, 1:3. A row is blocked if one contract costs more than the budget,
if the 15% stop is under one cent, or if the bid is already below the stop.
Keys are read from secrets, never from this repo:
- GitHub: Settings -> Secrets and variables -> Actions -> New repository secret: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.
- Streamlit: app Settings -> Secrets: `ALPACA_API_KEY = "..."` and `ALPACA_SECRET_KEY = "..."`.
Use paper-account keys only. Do not put keys in any file here; this repo is public.

## Quant panels (`quant.py`)
Formulas from "The entire math behind quant trading bots" (openvibes.si/quant-math) that fit this desk,
written in plain Python (still nothing to install). Section numbers match that page.
- **Chart**: running VWAP line (13.5).
- **QUANT** (ticker in focus): VWAP and distance from it, 5-minute volatility and EWMA volatility (2.1, 2.3),
  annualized volatility (2.2), beta and correlation against SPY (5.2, 4.2), trigger-bar relative volume and its
  z-score (3.1), round-trip cost in R (17.1), break-even win rate after costs (17.3), the no-edge odds of hitting the
  target before the stop, and how many standard deviations the target and stop are from the last price.
- **BACKTEST**: the ORB and level-retest rules replayed on the completed earlier sessions in the data, for the
  whole watchlist, net of costs: win rate, average R, profit factor, Sharpe and Sortino per trade (10.1, 10.2),
  t-stat (3.3), probabilistic Sharpe (16.4), max drawdown (10.3), expected shortfall (12.3) and Kelly (11.1).
  Yahoo keeps about 5 days of 5-minute bars, so this is a small sample; read the t-stat and PSR before trusting it.
- **Option ideas**: implied volatility from the mid quote (15.5), delta and theta (15.4), bid-ask spread (13.1),
  breakeven at expiry. Black-Scholes with no dividends.
- Run `python3 test_quant.py` to check the formulas against textbook values.

Not used, because they don't fit a long-side 5-minute breakout desk: GARCH, Ledoit-Wolf shrinkage, cointegration
and pairs trading, Ornstein-Uhlenbeck, Kalman filter, cross-sectional momentum, market making (Avellaneda-Stoikov),
order-book imbalance and order-flow imbalance (Yahoo gives no order book).

## Settings (`config.json`)
New: `slippage_per_share` (0.01, each way), `commission_per_trade` (0), `risk_free_rate` (0.04, used for option
IV; update it when rates change), `require_above_vwap` (false; true adds "entry above VWAP" to the risk check),
`min_rvol` (0 = off; e.g. 1.5 requires the trigger bar to trade 1.5x its usual volume), `beta_vs` ("SPY").

`budget` 500, `max_loss_pct` 15, `target_r_multiple` 2, `entry_cutoff`, `flat_by`, `watchlist`,
and `ticker` ("AUTO" picks the first watchlist breakout that passes the risk check, or name one ticker).

## Deploy on Streamlit Community Cloud
1. Go to share.streamlit.io, choose "Create app", pick this repo, branch `main`, main file `app.py`.
2. Deploy. No secrets are needed.

## Deploy on GitHub Pages
1. Repo Settings -> Pages -> Source: "GitHub Actions".
2. Actions tab -> "Build ORB Desk page" -> "Run workflow" for the first build.
3. After that it rebuilds every 5 minutes on weekdays during market hours.

## Replay a past session
`python3 build_page.py 2026-11-13` or `python3 build_page.py 2026-11-13 10:30`
(Yahoo keeps only recent 5-minute history; this build requests the last 5 days.)
