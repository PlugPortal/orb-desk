"""Builds the static one-pager for GitHub Pages: writes site/index.html.

Usage:
  python3 build_page.py                      latest session, as of now
  python3 build_page.py 2026-11-13           replay a past session (full day)
  python3 build_page.py 2026-11-13 10:30     replay it as of a time of day (New York)
"""
import datetime as dt
import os
import sys

import desk


def main():
    cfg = desk.load_config()
    now, session = None, None
    if len(sys.argv) > 1:
        session = dt.date.fromisoformat(sys.argv[1])
        clock = desk._hhmm(sys.argv[2]) if len(sys.argv) > 2 else dt.time(16, 0)
        now = dt.datetime.combine(session, clock, desk.NY)
    state = desk.build_state(cfg, now=now, session_date=session)
    out = os.path.join(desk.HERE, "site")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(desk.render(state, nav=True, auto_page=True))
    # one page per watchlist ticker, so the ticker can be switched from the page itself
    from concurrent.futures import ThreadPoolExecutor
    tickers = [s["ticker"] for s in state["scans"]]
    with ThreadPoolExecutor(max_workers=5) as ex:
        pages = list(ex.map(lambda t: (t, desk.render(desk.with_focus(state, t), nav=True)), tickers))
    for t, page in pages:
        with open(os.path.join(out, f"{t}.html"), "w", encoding="utf-8") as f:
            f.write(page)
    a = state["focus"]
    print(f'{a["ticker"]}: {a["status"]} | triggered on watchlist: {len(state["triggered"])}/{len(state["scans"])}')


if __name__ == "__main__":
    main()
