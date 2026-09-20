"""Dashboard 100% server-side - cero JS."""
import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from flask import Flask, redirect, request
from sqlalchemy import text

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from config.database import engine
from dashboard.display import league_name, market_name, match_name, result_label

app = Flask(__name__)
RESOLVED = ("win","loss","push","half_win","half_loss")
WIN_LIKE = ("win","half_win")
GH_REPO = "AldairG16/Sport_Betting_Model"
MX_TZ = "America/Mexico_City"

def _q(sql, params=None):
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception as e:
        app.logger.warning(str(e)[:100])
        return pd.DataFrame()

def _profit(df):
    if df.empty: return pd.Series(dtype=float)
    f = df["result"].map({"win":1.0,"half_win":0.5,"loss":-1.0,"half_loss":-0.5,"push":0.0}).fillna(0.0)
    return (df["odds"]-1) * df["stake"] * f

def _app_version():
    cands = [ROOT / "VERSION"]
    if getattr(sys, "frozen", False): cands.insert(0, Path(sys.executable).parent / "VERSION")
    for c in cands:
        try:
            if c.exists():
                v = c.read_bytes().decode("utf-8","ignore").replace(chr(0),"").strip()
                return v or "dev"
        except OSError: pass
    return "dev"

def _mx(dt):
    try: return pd.Timestamp(dt).tz_convert("America/Mexico_City").strftime("%d %b, %H:%M")
    except Exception: return str(dt)[:16]

def _pill(res):
    r = str(res).strip().lower()
    m = {"win":"Ganada","loss":"Perdida","push":"Nula","half_win":"Media G",
         "half_loss":"Media P","pending":"Pendiente","stale":"Sin fuente","unresolved":"Sin datos"}
    cls = {"win":"win","loss":"loss","pending":"pending","push":"push",
           "stale":"stale","unresolved":"stale","half_win":"win","half_loss":"loss"}.get(r,"push")
    lbl = m.get(r, r)
    return f'<span class="pill {cls}">{lbl}</span>'

def _bar(roi):
    p = max(0, min(int(abs(roi)), 60)); n = max(0, min(-int(roi), 60))
    return '<span style="color:var(--blue);font-family:monospace">' + "▓"*int(p/5) + "░"*int(n/5) + "</span>"

def _esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def compute_kpis():
    df = _q("SELECT result, stake, odds, probability, clv FROM bets_history WHERE match_date >= NOW() - INTERVAL '90 days'")
    window = "90d"
    if df.empty or not df["result"].isin(RESOLVED).any():
        df = _q("SELECT result, stake, odds, probability, clv FROM bets_history"); window = "histórico"
    o = {"window":window,"bets":len(df),"pending":0,"resolved":0,"wr":None,"profit":None,
         "roi":None,"brier":None,"clv":None,"clv_n":0,"bank":None}
    bank = _q("SELECT bankroll FROM bankroll ORDER BY updated_at DESC LIMIT 1")
    o["bank"] = float(bank.iloc[0]["bankroll"]) if not bank.empty else None
    if df.empty: return o
    o["pending"] = int((df["result"]=="pending").sum())
    res = df[df["result"].isin(RESOLVED)]
    o["resolved"] = len(res)
    if res.empty: return o
    o["wr"] = round(float(res["result"].isin(["win","half_win"]).mean()), 3)
    pf = float(_profit(res).sum()); sk = float(res["stake"].sum())
    o["profit"] = round(pf, 2); o["roi"] = round(pf/sk, 3) if sk > 0 else None
    bi = res[res["result"] != "push"]
    if len(bi):
        real = bi["result"].isin(["win","half_win"]).astype(float)
        o["brier"] = round(float(((bi["probability"] - real)**2).mean()), 4)
    clv = df["clv"].dropna()
    o["clv"] = round(float(clv.mean()), 4) if len(clv) else None
    o["clv_n"] = len(clv)
    return o

def compute_by(dim):
    df = _q(f"SELECT {dim}, result, stake, odds FROM bets_history WHERE result IN ('win','loss','push','half_win','half_loss') AND match_date >= NOW() - INTERVAL '180 days'")
    if df.empty: return []
    df["profit"] = _profit(df)
    g = df.groupby(dim).agg(n=("profit","size"), profit=("profit","sum"), staked=("stake","sum"))
    g["roi"] = (g["profit"]/g["staked"]*100).round(1)
    g = g[g["n"] >= 3].sort_values("roi", ascending=False)
    return [(str(i), int(r["n"]), float(r["stake"]), float(r["profit"]), float(r["roi"])) for i, r in g.iterrows()]

def get_bets(status="all", limit=300):
    w = {"pending":"result='pending'","resolved":"result IN ('win','loss','push','half_win','half_loss')",
         "stale":"result='stale'"}.get(status, "result IS DISTINCT FROM 'stale'")
    return _q(f"SELECT match_date, match, league, market, probability, odds, stake, result, profit, closing_odds, clv FROM bets_history WHERE {w} ORDER BY match_date DESC LIMIT {int(limit)}", params=None)

def get_scorers():
    return _q("SELECT match_date, match, player, team, probability, fair_odds, result FROM goalscorer_picks WHERE match_date >= NOW() - INTERVAL '7 days' ORDER BY match_date DESC LIMIT 30")

def get_narrative():
    return _q("SELECT narrative, engine, created_at FROM weekly_narrative ORDER BY created_at DESC LIMIT 1")
