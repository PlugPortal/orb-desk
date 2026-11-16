# ORB Desk

A one-page opening-range-breakout view. Two ways to see the same page:

- **Streamlit** (`app.py`): live page with settings in a sidebar, refreshes every 60 seconds.
- **GitHub Pages** (`build_page.py` + `.github/workflows/desk.yml`): a static page rebuilt on a schedule.

Both use `desk.py` for the rule and `config.json` for your settings. Data is Yahoo Finance (free, delayed, no key).
The page shows a plan. It never places an order.

## The rule
1. Opening range = high and low of the first 15 minutes (9:30-9:45 New York).
2. Entry = the first completed 5-minute bar that closes above the range high, before 11:30.
3. Stop = range midpoint. Target = entry + 2 x (entry - stop). Flat by 15:55.
4. Shares = the smaller of (max loss / risk per share) and (budget / entry price).

## Second setup: level retests
Levels: PDH/PDL (prior day high/low) and PMH/PML (premarket high/low, 4:00-9:30 New York).
Long above PDH/PMH, short below PDL/PML. A 5-minute close must break the level, then a later bar must
touch it again and close back on the breakout side; that close is the entry (before 11:30).
Stop = 1% of entry, target = 2R. Shorts are shown as WATCH ONLY unless `trade_shorts` is true.

## Option ideas (optional, needs Alpaca paper keys)
For the ticker in focus: calls and puts 2-5 strikes out of the money, expiring within 5 sessions (0DTE included).
Stop = 15% of the premium; targets at 1:1, 1:2, 1:3. A row is blocked if one contract costs more than the budget,
if the 15% stop is under one cent, or if the bid is already below the stop.
Keys are read from secrets, never from this repo:
- GitHub: Settings -> Secrets and variables -> Actions -> New repository secret: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`.
- Streamlit: app Settings -> Secrets: `ALPACA_API_KEY = "..."` and `ALPACA_SECRET_KEY = "..."`.
Use paper-account keys only. Do not put keys in any file here; this repo is public.

## Settings (`config.json`)
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
