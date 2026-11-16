"""ORB Desk - Streamlit version. Deploy on Streamlit Community Cloud with app.py as the main file."""
import datetime as dt

import streamlit as st

import desk

st.set_page_config(page_title="ORB Desk", layout="wide")
base = desk.load_config()

# Alpaca paper keys (optional, only for the option panel) come from Streamlit secrets
import os
try:
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        if k in st.secrets:
            os.environ[k] = str(st.secrets[k])
except Exception:
    pass

with st.sidebar:
    st.header("Settings")
    ticker = st.text_input("Ticker (AUTO = first watchlist breakout)", base["ticker"]).strip().upper() or "AUTO"
    budget = st.number_input("Budget ($)", min_value=50.0, value=float(base["budget"]), step=50.0)
    max_loss_pct = st.number_input("Max loss per trade (% of budget)", min_value=0.5, max_value=100.0,
                                   value=float(base["max_loss_pct"]), step=0.5)
    r_mult = st.selectbox("Target", [1, 2, 3], index=[1, 2, 3].index(base["target_r_multiple"]),
                          format_func=lambda r: f"{r}R")
    replay = st.checkbox("Replay a past session")
    replay_date = st.date_input("Session date", dt.datetime.now(desk.NY).date()) if replay else None
    replay_time = st.time_input("As of (New York time)", dt.time(16, 0)) if replay else None
    st.caption("Yahoo data is delayed. This page shows a plan and never places an order.")

cfg = dict(base, ticker=ticker, budget=budget, max_loss_pct=max_loss_pct, target_r_multiple=r_mult)


@st.cache_data(ttl=60, show_spinner="Scanning the watchlist...")
def page(cfg_items, session_iso, clock_iso):
    c = dict(cfg_items)
    c["watchlist"] = list(c["watchlist"])
    now = session = None
    if session_iso:
        session = dt.date.fromisoformat(session_iso)
        now = dt.datetime.combine(session, dt.time.fromisoformat(clock_iso), desk.NY)
    return desk.render(desk.build_state(c, now=now, session_date=session))


@st.fragment(run_every=None if replay else 60)
def show():
    frozen = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in cfg.items()))
    try:
        html = page(frozen, replay_date.isoformat() if replay else None,
                    replay_time.isoformat() if replay else None)
    except Exception as err:
        st.error(f"Could not build the page: {err}")
        return
    st.iframe(html, height=1650)


show()
