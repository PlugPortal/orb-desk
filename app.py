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

# ---- watchlist editor: add / remove tickers for this browser session; the page link remembers the changes
import re

BASE_WL = list(base["watchlist"])


def _clean(text):
    out = []
    for t in re.split(r"[\s,;]+", (text or "").upper()):
        t = t.strip().replace("/", "-").replace(".", "-")
        if re.fullmatch(r"[A-Z]{1,5}(-[A-Z])?", t) and t not in out:
            out.append(t)
    return out


if "watch_all" not in st.session_state:
    added = [t for t in _clean(st.query_params.get("add", "")) if t not in BASE_WL]
    removed = set(_clean(st.query_params.get("remove", "")))
    st.session_state.watch_all = BASE_WL + added
    st.session_state.watch_sel = [t for t in BASE_WL + added if t not in removed]


def _add():
    for t in _clean(st.session_state.get("add_box", "")):
        if t not in st.session_state.watch_all:
            st.session_state.watch_all.append(t)
        if t not in st.session_state.watch_sel:
            st.session_state.watch_sel = st.session_state.watch_sel + [t]
    st.session_state.add_box = ""


def _reset():
    st.session_state.watch_all = list(BASE_WL)
    st.session_state.watch_sel = list(BASE_WL)


with st.sidebar:
    st.header("Watchlist")
    st.text_input("Add tickers (separate with commas or spaces)", key="add_box", placeholder="e.g. AMD, COIN")
    st.button("Add", on_click=_add)
    st.multiselect("Scanning these (click the x on a ticker to remove it)",
                   options=st.session_state.watch_all, key="watch_sel")
    st.button("Reset to saved list", on_click=_reset)
    st.caption("Changes apply here only and are kept in this page's link, so bookmark it. "
               "They do not change the GitHub page or the saved list.")

watch = list(st.session_state.watch_sel)
adds = [t for t in watch if t not in BASE_WL]
rems = [t for t in BASE_WL if t not in watch]
for name, vals in (("add", adds), ("remove", rems)):
    if vals:
        st.query_params[name] = ",".join(vals)
    elif name in st.query_params:
        del st.query_params[name]
if not watch:
    st.warning("The watchlist is empty. Add a ticker or reset to the saved list.")
    st.stop()

cfg = dict(base, ticker=ticker, budget=budget, max_loss_pct=max_loss_pct, target_r_multiple=r_mult, watchlist=watch)


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
