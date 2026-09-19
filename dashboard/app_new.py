"""
Dashboard Web Local — Sport Betting Model
==========================================
App Flask 100% SERVER-SIDE: todo el contenido (KPIs, tablas, panel de
control) se genera como HTML puro en el servidor. Cero JavaScript
obligatorio — funciona en cualquier navegador, incluidos los embebidos
más limitados. Auto-refresco vía meta refresh (nativo del navegador).

Seguridad:
    - Solo escucha en 127.0.0.1.
    - Solo SELECT sobre la DB; el panel de control solo dispara workflows
      de GitHub (tu token, tu máquina).
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, redirect, render_template_string, request, send_file

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.database import engine  # noqa: E402
from src.utils.team_normalizer import normalize_team  # noqa: E402
from dashboard.display import league_name, team_name, market_name, match_name, result_label  # noqa: E402

app = Flask(__name__)

RESOLVED = ("win", "loss", "push", "half_win", "half_loss")
WIN_LIKE = ("win", "half_win")
PROFIT_FACTOR = {"win": 1.0, "half_win": 0.5, "loss": -1.0, "half_loss": -0.5, "push": 0.0}
GH_REPO = "AldairG16/Sport_Betting_Model"
GH_API = f"https://api.github.com/repos/{GH_REPO}"
DISPATCHABLE = {
    "morning": ("morning.yml", "Morning Pipeline"),
    "evening": ("evening.yml", "Evening Pipeline"),
    "closing": ("closing.yml", "Closing Odds Pipeline"),
    "weekly": ("weekly.yml", "Weekly Pipeline"),
}


def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception as e:
        app.logger.warning(f"query falló: {e}")
        return pd.DataFrame()


from sqlalchemy import text  # noqa: E402


def _profit(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    factor = df["result"].map(PROFIT_FACTOR).fillna(0.0)
    return (df["odds"] - 1.0) * df["stake"] * factor


def _app_version() -> str:
    candidates = [Path(__file__).parent.parent / "VERSION"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).parent / "VERSION")
    for c in candidates:
        try:
            if c.exists():
                v = c.read_bytes().decode("utf-8", errors="ignore")
                v = v.replace(chr(0), "").strip()
                return v or "dev"
        except OSError:
            pass
    return "dev"


# ─────────────────────────────────────────────────────────────
# KPIs (server-side, compartido entre / y /api/kpis)
# ─────────────────────────────────────────────────────────────

def compute_kpis() -> dict:
    df = _q("""
        SELECT result, stake, odds, probability, clv, match_date
        FROM bets_history
        WHERE match_date >= NOW() - INTERVAL '90 days'
    """)
    window = "90d"
    if df.empty or not df["result"].isin(RESOLVED).any():
        df = _q("SELECT result, stake, odds, probability, clv, match_date FROM bets_history")
        window = "histórico"

    out = {"window": window, "bets_90d": int(len(df)), "pending": 0,
           "resolved": 0, "win_rate": None, "profit": None, "roi": None,
           "brier": None, "clv_avg": None, "clv_n": 0, "bankroll": None,
           "ok": True}

    bank = _q("SELECT bankroll FROM bankroll ORDER BY updated_at DESC LIMIT 1")
    out["bankroll"] = float(bank.iloc[0]["bankroll"]) if not bank.empty else None

    if df.empty:
        return out

    out["pending"] = int((df["result"] == "pending").sum())
    resolved = df[df["result"].isin(RESOLVED)]
    out["resolved"] = int(len(resolved))
    if resolved.empty:
        return out

    wins = resolved["result"].isin(WIN_LIKE)
    out["win_rate"] = round(float(wins.mean()), 3)
    profit = float(_profit(resolved).sum())
    staked = float(resolved["stake"].sum())
    out["profit"] = round(profit, 2)
    out["roi"] = round(profit / staked, 3) if staked > 0 else None

    binary = resolved[resolved["result"] != "push"]
    if len(binary):
        real = binary["result"].isin(WIN_LIKE).astype(float)
        out["brier"] = round(float(((binary["probability"] - real) ** 2).mean()), 4)

    clv = df["clv"].dropna()
    out["clv_avg"] = round(float(clv.mean()), 4) if len(clv) else None
    out["clv_n"] = int(len(clv))
    return out


def compute_by(dim: str) -> pd.DataFrame:
    df = _q(f"""
        SELECT {dim}, result, stake, odds
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND match_date >= NOW() - INTERVAL '180 days'
    """)
    if df.empty:
        return pd.DataFrame()
    df["profit"] = _profit(df)
    g = df.groupby(dim).agg(n=("profit", "size"), profit=("profit", "sum"),
                            staked=("stake", "sum"))
    g["roi"] = (g["profit"] / g["staked"] * 100).round(1)
    g = g[g["n"] >= 3].sort_values("roi", ascending=False)
    return g


def get_bets(status: str = "all", market: str = "", league: str = "",
             limit: int = 150) -> pd.DataFrame:
    where, params = ["1=1"], {}
    if status == "pending":
        where.append("result = 'pending'")
    elif status == "resolved":
        where.append("result IN ('win','loss','push','half_win','half_loss')")
    elif status == "stale":
        where.append("result = 'stale'")
    else:
        where.append("result IS DISTINCT FROM 'stale'")
    if market:
        where.append("market = :market"); params["market"] = market
    if league:
        where.append("league = :league"); params["league"] = league
    df = _q(f"""
        SELECT match_date, match, league, market, probability, odds,
               stake, result, profit, closing_odds, clv
        FROM bets_history
        WHERE {' AND '.join(where)}
        ORDER BY match_date DESC
        LIMIT {int(limit)}
    """, params)
    return df


def get_clv_series() -> pd.DataFrame:
    return _q("""
        SELECT clv, result FROM bets_history
        WHERE clv IS NOT NULL
          AND result IN ('win','loss','half_win','half_loss')
        ORDER BY match_date
    """)


# ─────────────────────────────────────────────────────────────
# GitHub (panel de control) — server-side
# ─────────────────────────────────────────────────────────────

def _gh_headers():
    token = os.environ.get("GH_TOKEN", "")
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "betting-dashboard"}


def _gh_runs() -> list[dict]:
    try:
        req = urllib.request.Request(
            f"{GH_API}/actions/runs?per_page=8", headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        runs = data.get("workflow_runs", [])
        out = []
        for r in runs:
            created = r["created_at"]
            try:
                t = datetime.fromisoformat(created.replace("Z", "+00:00"))
                created = (t - timedelta(hours=6)).strftime("%d/%m %H:%M")
            except Exception:
                pass
            out.append({"name": r["name"], "status": r["status"],
                        "conclusion": r["conclusion"], "created": created,
                        "url": r["html_url"]})
        return out
    except Exception:
        return []


def _gh_dispatch(wf_file: str) -> tuple[bool, str]:
    if not os.environ.get("GH_TOKEN"):
        return False, "Falta GH_TOKEN en .env"
    try:
        req = urllib.request.Request(
            f"{GH_API}/actions/workflows/{wf_file}/dispatches",
            headers=_gh_headers(), method="POST",
            data=json.dumps({"ref": "master"}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, "disparado"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:120]


def _gh_today_failures() -> list[str]:
    """Nombres de pipelines que fallaron hoy (para el banner)."""
    runs = _gh_runs()
    today = datetime.now(timezone.utc).strftime("%d/%m")
    return [r["name"] for r in runs
            if r["status"] == "completed" and r["conclusion"] == "failure"
            and r["created"].startswith(today)]


def _newer_release() -> str | None:
    try:
        req = urllib.request.Request(
            f"{GH_API}/releases/latest", headers={"User-Agent": "betting-dashboard"})
        with urllib.request.urlopen(req, timeout=4) as r:
            tag = json.loads(r.read()).get("tag_name", "")
            return tag[1:] if tag.startswith("v") else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# DATOS PARA LA PÁGINA
# ─────────────────────────────────────────────────────────────

def get_pill_class(result: str) -> str:
    r = (result or "").lower()
    return r if r in ("win", "loss", "pending", "push", "stale", "unresolved") else "push"


def get_page_data() -> dict:
    d = {}
    d["version"] = _app_version()
    d["newer"] = _newer_release()

    # KPIs
    d["kpi"] = compute_kpis()

    # Bets (todas activas, hasta 300)
    bets = get_bets("all", limit=300)
    d["bets"] = bets

    # Por mercado / liga (barras unicode server-side)
    by_mkt = compute_by("market")
    by_lg = compute_by("league")
    def bars(g: pd.DataFrame) -> list[tuple]:
        out = []
        for idx, r in g.iterrows():
            pos = max(0.0, min(float(r["roi"]), 100.0))
            neg = max(0.0, min(-float(r["roi"]), 100.0))
            bar = "▓" * int(round(pos / 10)) + "░" * int(round(neg / 10))
            out.append((str(idx), int(r["n"]), float(r["roi"]), bar))
        return out
    d["by_mkt"] = bars(by_mkt)
    d["by_lg"] = bars(by_lg)

    # Goleadores
    d["scorers"] = _q("""
        SELECT match_date, match, player, team, probability, fair_odds,
               odds_placed, result
        FROM goalscorer_picks
        WHERE match_date >= NOW() - INTERVAL '7 days'
        ORDER BY match_date DESC LIMIT 30
    """)

    # Narrativa IA
    d["narrative"] = _q("""
        SELECT week_start, narrative, engine, created_at
        FROM weekly_narrative ORDER BY created_at DESC LIMIT 1
    """)

    # GitHub: corridas + fallos de hoy
    d["gh_runs"] = []
    d["gh_configured"] = bool(os.environ.get("GH_TOKEN"))
    if d["gh_configured"]:
        try:
            req = urllib.request.Request(
                f"{GH_API}/actions/runs?per_page=8", headers=_gh_headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            for rr in data.get("workflow_runs", []):
                t = rr["created_at"]
                try:
                    t = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    t = (t - timedelta(hours=6)).strftime("%d/%m %H:%M")
                except Exception:
                    pass
                d["gh_runs"].append({"name": rr["name"], "status": rr["status"],
                                     "conclusion": rr["conclusion"], "created": t,
                                     "url": rr["html_url"]})
        except Exception:
            pass
    d["gh_failures_today"] = [
        r["name"] for r in d["gh_runs"]
        if r["status"] == "completed" and r["conclusion"] == "failure"
        and r["created"].startswith(datetime.now(timezone.utc).strftime("%d/%m"))
    ]

    # Mercados/ligas para los filtros (de las bets visibles)
    d["markets"] = sorted(bets["market"].dropna().unique().tolist()) if not bets.empty else []
    d["leagues"] = sorted(bets["league"].dropna().unique().tolist()) if not bets.empty else []
    return d


PAGE_HEAD = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<title>Betting Dashboard</title>
<meta http-equiv="refresh" content="120">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1420; --card:#1a2233; --text:#e2e8f0; --muted:#8b98ad; --green:#22c55e; --red:#ef4444; --blue:#3b82f6; }
  * { box-sizing:border-box; margin:0; padding:0; font-family:'Segoe UI',system-ui,sans-serif; }
  body { background:var(--bg); color:var(--text); padding:20px; }
  h1 { font-size:1.3rem; margin-bottom:4px; }
  .sub { color:var(--muted); font-size:.85rem; margin-bottom:14px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border-radius:10px; padding:14px; }
  .card .lbl { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.5px; }
  .card .val { font-size:1.5rem; font-weight:700; margin-top:4px; }
  .pos { color:var(--green); } .neg { color:var(--red); }
  table { width:100%%; border-collapse:collapse; font-size:.82rem; background:var(--card); border-radius:10px; }
  .tablewrap { max-height:480px; overflow-y:auto; border-radius:10px; }
  th { text-align:left; padding:8px 10px; color:var(--muted); font-weight:600; border-bottom:1px solid #2a3550; position:sticky; top:0; background:#1a2233; z-index:1; }
  td { padding:7px 10px; border-bottom:1px solid #232d45; }
  tr:hover td { background:#202a40; }
  .pill { padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600; }
  .pill.win,.pill.ganada{background:#14351f;color:var(--green)}
  .pill.loss,.pill.perdida{background:#3a1a1a;color:var(--red)}
  .pill.pending,.pill.pendiente{background:#2a2a14;color:#eab308}
  .pill.push,.pill.stale,.pill.nula,.pill.sin{background:#1e2a3a;color:#93c5fd}
  .controls { display:flex; gap:10px; margin:14px 0; flex-wrap:wrap; align-items:center; }
  select, input[type=submit], button { background:var(--card); color:var(--text); border:1px solid #2a3550; border-radius:8px; padding:6px 10px; font-size:.85rem; }
  input[type=submit] { cursor:pointer; }
  .btn { display:inline-block; background:#14351f; color:var(--green); border:1px solid #22c55e44; border-radius:8px; padding:7px 13px; text-decoration:none; font-size:.85rem; }
  .btn:hover { background:#1a4527; }
  .section { margin-top:26px; } .section h2 { font-size:1.05rem; margin-bottom:10px; }
  .upd { background:#14351f; border:1px solid #22c55e44; color:var(--green); border-radius:8px; padding:8px 12px; margin-bottom:14px; font-size:.85rem; }
  .alert { background:#3a1a1a; border:1px solid #ef444444; color:var(--red); border-radius:8px; padding:8px 12px; margin-bottom:14px; font-size:.85rem; }
  .bar { color:var(--blue); letter-spacing:1px; }
  a { color:var(--blue); }
  .muted { color:var(--muted); }
</style></head><body>
<h1>⚽ Sport Betting Model — Dashboard <span style="font-size:.7rem;color:var(--muted);font-weight:400">v%(version)s</span></h1>
<div class="sub">Datos en vivo desde Neon · solo lectura · se actualiza solo cada 2 min · %(hora)s</div>
"""


PAGE_TAIL = """</body></html>"""


@app.route("/")
def index():
    d = get_page_data()
    k = d["kpi"]
    hora = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # banner de nueva versión
    banner = ""
    if d.get("newer") and d["newer"] != d["version"]:
        banner = (f'<div class="upd">🔄 Nueva versión disponible: <b>v{d["newer"]}</b> '
                  f'(tienes v{d["version"]}) — <a href="https://github.com/{GH_REPO}/releases/latest" '
                  f'target="_blank">descargar</a></div>')

    # banner de fallos de hoy
    alerts = ""
    if d["gh_failures_today"]:
        alerts = ('<div class="alert">🚨 Fallaron hoy: '
                  + ", ".join(d["gh_failures_today"])
                  + ' — <a href="https://github.com/AldairG16/Sport_Betting_Model/actions" target="_blank">ver logs</a></div>')

    html = PAGE_HEAD % {"version": d["version"], "hora": hora} + banner + alerts
    html += kpi_cards_html(d)
    html += tables_html(d)
    html += control_panel_html(d)
    html += PAGE_TAIL
    return html


def kpi_cards_html(d: dict) -> str:
    k = d["kpi"]
    bankroll = k["bankroll"]
    cards = [
        ("Bankroll", "—" if bankroll is None else f"{bankroll:.1f}u", ""),
        ("Profit", "—" if k["profit"] is None else f"{k['profit']:+.2f}u",
         "pos" if (k["profit"] or 0) >= 0 else "neg"),
        ("ROI", "—" if k["roi"] is None else f"{k['roi']:+.1%}", ""),
        ("Win rate", "—" if k["win_rate"] is None else f"{k['win_rate']:.0%}", ""),
        ("Resueltas", k["resolved"], ""),
        ("Pendientes", k["pending"], ""),
        ("Brier", "—" if k["brier"] is None else f"{k['brier']:.3f}", ""),
        ("CLV medio", "—" if k["clv_avg"] is None else f"{k['clv_avg']:+.2%}", ""),
    ]
    html = '<div class="kpis">'
    for lbl, val, cls in cards:
        html += f'<div class="card"><div class="lbl">{lbl}</div><div class="val {cls}">{val}</div></div>'
    html += "</div>"
    html += f'<div class="sub">Ventana: {k["window"]} · solo lectura</div>'
    return html


def tables_html(d: dict) -> str:
    html = '<div class="section"><h2>📋 Apuestas</h2>'
    html += ('<form method="get" action="/" class="controls">'
             '<select name="status"><option value="all">Activas</option>'
             '<option value="pending"' + (" selected" if request.args.get("status") == "pending" else "") + '>Pendientes</option>'
             '<option value="resolved"' + (" selected" if request.args.get("status") == "resolved" else "") + '>Resueltas</option>'
             '<option value="stale"' + (" selected" if request.args.get("status") == "stale" else "") + '>Histórico muerto</option>'
             '</select>'
             '<input type="submit" value="Filtrar"></form>')
    html += '<div class="tablewrap"><table><thead><tr><th>Fecha (MX)</th><th>Partido</th><th>Liga</th><th>Mercado</th><th>Prob</th><th>Odd</th><th>Stake</th><th>Estado</th><th>Profit</th><th>CLV</th></tr></thead><tbody>'
    for _, b in d["bets"].iterrows():
        cls = get_pill_class(str(b["result"]))
        prof = "" if str(b["result"]) == "pending" else f"{float(b['profit'] or 0):+.2f}u"
        clv = "—" if pd.isna(b["clv"]) else f"{float(b['clv'])*100:.1f}%"
        html += (f"<tr><td>{pd.Timestamp(b['match_date']).tz_localize('UTC').tz_convert('America/Mexico_City').strftime('%d %b, %H:%M') if pd.notna(b['match_date']) else ''}</td>"
                 f"<td>{match_name(str(b['match']))}</td><td>{league_name(str(b['league'] or ''))}</td>"
                 f"<td>{market_name(str(b['market']))}</td><td>{float(b['probability'])*100:.0f}%</td>"
                 f"<td>{float(b['odds']):.2f}</td><td>{float(b['stake']):.2f}u</td>"
                 f'<td><span class="pill {cls}">{result_label(str(b["result"]))}</span></td>'
                 f"<td>{prof}</td><td>{clv}</td></tr>")
    if d["bets"].empty:
        html += '<tr><td colspan="10" class="muted">Sin apuestas</td></tr>'
    html += "</tbody></table></div>"

    # Goleadores
    if not d["scorers"].empty:
        html += '<div class="section"><h2>⚽ Goleadores (anytime scorer · papel)</h2>'
        html += '<div class="tablewrap"><table><thead><tr><th>Fecha (MX)</th><th>Partido</th><th>Jugador</th><th>Equipo</th><th>P(anota)</th><th>Fair odd</th><th>Estado</th></tr></thead><tbody>'
        for _, s in d["scorers"].iterrows():
            cls = get_pill_class(str(s["result"]))
            html += (f"<tr><td>{pd.Timestamp(s['match_date']).tz_localize('UTC').tz_convert('America/Mexico_City').strftime('%d %b, %H:%M') if pd.notna(s['match_date']) else ''}</td>"
                     f"<td>{match_name(str(s['match']))}</td><td>{s['player']}</td><td>{s['team']}</td>"
                     f"<td>{float(s['probability'])*100:.0f}%</td><td>@{float(s['fair_odds']):.2f}</td>"
                     f'<td><span class="pill {cls}">{result_label(str(s["result"]))}</span></td></tr>')
        html += "</tbody></table></div>"
    return html


def control_panel_html(d: dict) -> str:
    html = '<div class="section"><h2>🎮 Panel de control (GitHub Actions)</h2>'
    if not d["gh_configured"]:
        html += ('<div class="alert">⚠️ Falta GH_TOKEN en .env — créalo en '
                 'github.com/settings/tokens (fine-grained, permiso Actions: Read and write) '
                 'y agrégalo a tu .env como GH_TOKEN=xxx</div>')
    else:
        html += ('<div class="controls">'
                 + "".join(f'<form method="post" action="/dispatch/{key}" style="display:inline">'
                           f'<button type="submit">{label}</button></form>'
                           for key, label in DISPATCHABLE.items())
                 + "</div>")
        if d["gh_runs"]:
            html += '<div class="tablewrap" style="max-height:260px"><table><thead><tr><th>Corrida</th><th>Estado</th><th>Cuándo (MX)</th><th>Link</th></tr></thead><tbody>'
            for r in d["gh_runs"]:
                icon = "🔄" if r["status"] != "completed" else ("✅" if r["conclusion"] == "success" else "❌")
                html += (f"<tr><td>{r['name']}</td><td>{icon} {r['status']}</td>"
                         f"<td>{r['created']}</td>"
                         f'<td><a href="{r["url"]}" target="_blank">ver</a></td></tr>')
            html += "</tbody></table>"
    html += "</div>"
    return html


@app.route("/dispatch/<key>", methods=["POST"])
def dispatch(key: str):
    if key not in DISPATCHABLE:
        return "workflow desconocido", 404
    ok, msg = _gh_dispatch(key)
    if ok:
        return redirect("/", code=303)
    return f"⚠️ {msg}", 400
