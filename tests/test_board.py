"""
Dashboard v2.0 (22-oct-26): partidos, equipos y gráficas.

El dueño: "necesito un mejor dashboard, que sea entendible, sin tanto texto
innecesario… quiero ver solo los partidos, equipos, gráficas y los partidos
anteriores desde septiembre y los próximos". Y no veía los cambios porque su
pestaña seguía con la página vieja: la página ahora se recarga sola al cambiar
la versión y no se guarda en caché. Sin DB.
"""

import pandas as pd
import pytest

from dashboard import board as B


def _m(date, h, a, hg, ag, league="soccer_epl", hc=6, ac=3, hy=2, ay=2, hr=0, ar=0):
    return {"date": pd.Timestamp(date), "league": league, "h": h, "a": a, "home_goals": hg, "away_goals": ag,
            "home_corners": hc, "away_corners": ac, "home_yellow": hy, "away_yellow": ay, "home_red": hr, "away_red": ar}


MATCHES = pd.DataFrame([
    _m("2026-10-22", "atlas", "club america", 2, 5, league="soccer_mexico_ligamx", hc=None, ac=None, hy=None, ay=None),
    _m("2026-10-18", "real madrid", "sevilla", 4, 1, hr=1),
    _m("2026-10-11", "sevilla", "real madrid", 1, 1, hc=0, ac=0),
    _m("2026-09-20", "real madrid", "getafe", 0, 1),
])
MVS = pd.DataFrame([
    {"match": "atlas vs club america", "match_date": pd.Timestamp("2026-10-22 01:00", tz="UTC"),
     "p_home": 0.30, "p_draw": 0.22, "p_away": 0.48, "pin_home": 0.27, "pin_draw": 0.24, "pin_away": 0.49},
    {"match": "real madrid vs sevilla", "match_date": pd.Timestamp("2026-10-18 19:00", tz="UTC"),
     "p_home": 0.50, "p_draw": 0.16, "p_away": 0.34, "pin_home": 0.78, "pin_draw": 0.14, "pin_away": 0.08},
])
PICKS = pd.DataFrame([
    {"match": "atlas vs club america", "match_date": pd.Timestamp("2026-10-22 01:00", tz="UTC"), "label": "Más de 2.5 goles",
     "market": "over25", "family": "Goles", "reason": None,
     "model_prob": 0.67, "market_prob": 0.58, "result": "win", "profit": 0.59, "source": None},
    {"match": "real madrid vs sevilla", "match_date": pd.Timestamp("2026-10-18 19:00", tz="UTC"), "label": "Gana el visitante",
     "market": "away_win", "family": "Ganador", "reason": "El programa se separa demasiado del mercado",
     "model_prob": 0.34, "market_prob": 0.08, "result": "loss", "profit": None, "source": "reconstruido"},
])
BETS = pd.DataFrame([
    {"match": "Real Madrid vs Sevilla", "match_date": pd.Timestamp("2026-10-18 19:00", tz="UTC"), "market": "home_win",
     "result": "win", "profit": 0.4},
    {"match": "Real Madrid vs Sevilla", "match_date": pd.Timestamp("2026-10-18 19:00", tz="UTC"), "market": "over25",
     "result": "stale", "profit": 0.0},                                    # cancelada: no se muestra
])


def _past():
    return B.past_matches(MATCHES, MVS, PICKS, BETS, normalize=lambda t: str(t).lower())


def test_past_matches_join_the_program_the_pick_and_the_real_bets():
    rows = _past()
    atlas = next(r for r in rows if r["key"] == "atlas vs club america")
    assert atlas["date"] == "2026-10-21"                 # 19:00 de México, aunque en UTC ya era el 22
    assert atlas["score"] == [2, 5] and atlas["corners"] is None and atlas["cards"] is None
    assert atlas["fav"]["model"] == "away" and atlas["fav"]["model_hit"] is True and atlas["fav"]["pin_hit"] is True
    assert atlas["pick"]["result"] == "win" and atlas["pick"]["winner"] == "programa"
    assert atlas["pick"]["rebuilt"] is False and atlas["pick"]["old_model"] is False and atlas["pick"]["rule"] is True
    rm = next(r for r in rows if r["key"] == "real madrid vs sevilla")
    assert rm["fav"]["model_hit"] is True and rm["cards"] == 5            # 2 + 2 amarillas + 1 roja
    assert [b["result"] for b in rm["bets"]] == ["win"]                  # la cancelada no
    assert rm["pick"]["rebuilt"] is True and rm["pick"]["old_model"] is True and rm["pick"]["winner"] == "mercado"
    assert rm["pick"]["rule"] is False and rm["pick"]["reason"].startswith("El programa se separa")
    sev = next(r for r in rows if r["key"] == "sevilla vs real madrid")
    assert sev["corners"] is None and sev["fav"] is None                 # 0-0 en córners = sin dato
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)


def test_summary_and_weekly_accuracy():
    rows = _past()
    s = B.summary(rows)
    assert (s["matches"], s["analyzed"], s["model_hit"], s["pin_hit"]) == (4, 2, 1.0, 1.0)
    # el pick del 22-oct es del modelo corregido; el reconstruido del 18-oct, del modelo con error
    assert s["picks_fixed"] == {"n": 1, "won": 1, "profit": 0.59, "priced": 1}
    assert s["picks_rule"] == {"n": 1, "won": 1, "profit": 0.59, "priced": 1}
    assert s["picks_old"] == {"n": 1, "won": 0, "profit": 0, "priced": 0}
    assert (s["bets"], s["bets_profit"]) == (1, 0.4)
    assert s["families"]["Goles"] == {"n": 1, "won": 1, "profit": 0.59, "priced": 1}     # tendencias por tipo
    weeks = B.weekly_accuracy(rows)
    assert [(w["week"], w["n"]) for w in weeks] == [("2026-10-12", 1), ("2026-10-19", 1)]


def test_team_list_and_team_view():
    teams = B.team_list(MATCHES)
    rm = next(t for t in teams if t["key"] == "real madrid")
    assert rm["n"] == 3 and rm["league"] == "Premier League"
    up = pd.DataFrame([{"match": "real madrid vs barcelona", "match_date": pd.Timestamp("2026-10-26 15:00", tz="UTC"),
                        "league": "soccer_spain_la_liga", "p_home": 0.5, "p_draw": 0.25, "p_away": 0.25},
                       {"match": "atlas vs leon", "match_date": pd.Timestamp("2026-10-26 15:00", tz="UTC"),
                        "league": "soccer_mexico_ligamx", "p_home": 0.4, "p_draw": 0.3, "p_away": 0.3}])
    v = B.team_view("real madrid", MATCHES, up)
    assert [(m["opponent"], m["venue"], m["gf"], m["ga"], m["result"]) for m in v["matches"]] == [
        ("Getafe", "L", 0, 1, "P"), ("Sevilla", "V", 1, 1, "E"), ("Sevilla", "L", 4, 1, "G")]
    assert v["record"] == {"w": 1, "d": 1, "l": 1} and v["avg"]["gf"] == pytest.approx(5 / 3, abs=0.01)
    assert v["avg"]["cf"] == 6.0                                       # el 0-0 de córners no cuenta
    assert [u["away"] for u in v["upcoming"]] == ["Barcelona"]


def test_chart_series():
    hist = pd.DataFrame([{"date": "2026-10-20 10:00", "balance": 64.0}, {"date": "2026-10-20 12:00", "balance": 63.9},
                         {"date": "2026-10-21 23:00", "balance": 63.39}])
    assert B.bankroll_series(hist) == {"dates": ["2026-10-20", "2026-10-21"], "values": [63.9, 63.39]}
    picks = pd.DataFrame([{"match_date": "2026-10-20T01:00:00Z", "result": "loss", "profit": -1.0, "reason": None},  # modelo con error
                          {"match_date": "2026-10-22T01:00:00Z", "result": "win", "profit": 0.59, "reason": None},
                          {"match_date": "2026-10-22T03:00:00Z", "result": "loss", "profit": -1.0, "reason": "Sin ventaja"},
                          {"match_date": "2026-10-23T01:00:00Z", "result": None, "profit": None, "reason": None}])
    assert B.picks_series(picks) == {"dates": ["2026-10-20", "2026-10-22"], "old": [-1.0, None], "fixed": [None, -0.41],
                                     "rule": [None, 0.59]}           # 🎯 solo los que pasan la regla


@pytest.fixture
def dash(monkeypatch):
    import dashboard.app as d
    monkeypatch.setitem(d._PAST_CACHE, "rows", None)

    def fake_q(sql, params=None, optional=False):
        s = " ".join(str(sql).split())
        if "FROM matches" in s and "LOWER(home_team) = :t" in s:
            return MATCHES
        if "FROM matches" in s and "home_goals, away_goals" not in s:
            return MATCHES[["league", "h", "a"]]
        if "FROM matches" in s:
            return MATCHES
        if "FROM model_vs_sharp WHERE match_date >= NOW()" in s:
            return pd.DataFrame(columns=["match", "match_date", "league", "p_home", "p_draw", "p_away"])
        if "FROM model_vs_sharp" in s:
            return MVS
        if "FROM program_picks" in s:
            return PICKS
        if "FROM bets_history" in s:
            return BETS
        if "FROM bankroll_history" in s:
            return pd.DataFrame([{"date": "2026-10-21", "balance": 63.39}])
        if "FROM model_proposals" in s:
            return pd.DataFrame(columns=["match_date", "market", "odds", "result"])
        if "FROM live_scores" in s:                  # marcadores del día (v2.4.4): ninguno aquí
            return pd.DataFrame()
        if "FROM espn_matches" in s:                 # copas y amistosos (22-oct-26): ninguno aquí
            return pd.DataFrame()
        raise AssertionError(s[:90])
    monkeypatch.setattr(d, "_q", fake_q)
    return d


def test_board_endpoints(dash):
    c = dash.app.test_client()
    p = c.get("/api/board/past").get_json()
    assert p["ok"] and p["since"] == "2026-09-01" and p["summary"]["matches"] == 4 and len(p["weekly"]) == 2
    t = c.get("/api/board/teams").get_json()
    assert t["ok"] and {x["key"] for x in t["teams"]} >= {"real madrid", "club america"}
    v = c.get("/api/board/team?name=Real Madrid").get_json()
    assert v["ok"] and v["team"] == "Real Madrid" and len(v["matches"]) == 3
    ch = c.get("/api/board/charts").get_json()
    assert ch["ok"] and ch["bankroll"]["values"] == [63.39] and ch["picks"]["fixed"] == [0.59]


def test_the_page_is_never_cached_and_carries_its_version(dash):
    r = dash.app.test_client().get("/")
    assert r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert f'window.APP_V = "{dash._app_version()}"' in html                 # la recarga sola al cambiar
    for tab in ("proximos", "anteriores", "equipos", "graficas"):
        assert f'data-tab="{tab}"' in html


def test_the_refresh_button_reads_past_results_again_without_the_5_min_cache(dash, monkeypatch):
    """v2.5.3: "necesito un botón de refresh para estar monitoreando los resultados
    cuando cambia algo". Sin saltarse la caché, el botón devolvía lo mismo hasta 5 min."""
    calls = []
    real = dash._load_past_rows
    monkeypatch.setattr(dash, "_load_past_rows", lambda: calls.append(1) or real())
    c = dash.app.test_client()
    c.get("/api/board/past"); c.get("/api/board/past")
    assert len(calls) == 1                                    # la recarga sola de 5 min usa la caché
    c.get("/api/board/past?fresh=1"); c.get("/api/board/charts?fresh=1")
    assert len(calls) == 3                                    # el botón lee otra vez

    html = c.get("/").get_data(as_text=True)
    assert 'id="refresh"' in html and 'id="updated"' in html
    from pathlib import Path
    js = (Path(dash.__file__).parent / "ui_es5.js").read_text(encoding="utf-8")
    assert "refreshAll(true)" in js and "'/api/board/past' + (fresh ? '?fresh=1' : '')" in js
    assert "hay cambios" in js and "sin cambios" in js


# ─── Ganadas y perdidas (v2.5.3) ─────────────────────────────────────────────

def _row(date, league, home, away, label=None, result=None, profit=None, model=None, rule=True, old=False,
         low=False, started=False, bets=()):
    pick = {"label": label, "family": "Ganador", "result": result, "profit": profit, "model": model,
            "rule": rule, "old_model": old, "low": low} if label else {"none": True, "result": "none"}
    return {"date": date, "league": league, "home": home, "away": away, "pick": pick,
            "started": started, "bets": list(bets)}


def test_wins_and_losses_by_day_type_and_league():
    rows = [_row("2026-10-24", "Premier League", "A", "B", "Gana el local", "win", 0.9, 0.55),
            _row("2026-10-24", "Premier League", "C", "D", "Gana el local", "loss", -1.0, 0.70),
            _row("2026-10-25", "La Liga", "E", "F", "Ambos anotan", "win", 1.5, 0.48, rule=False),
            _row("2026-10-25", "La Liga", "G", "H", "Ambos anotan", "push", 0.0, 0.50),
            _row("2026-10-25", "La Liga", "I", "J", "Gana el local", "loss", -1.0, 0.62, low=True),     # poca historia: no cuenta
            _row("2026-10-20", "La Liga", "K", "L", "Gana el local", "win", 0.8, 0.6, old=True),       # modelo con error
            _row("2026-10-26", "Serie A", "M", "N", "Gana el local", "win", 0.7, 0.6, started=True),   # provisional
            _row("2026-10-25", "La Liga", "O", "P", bets=[{"market": "Gana el local", "result": "loss", "profit": -2.0}])]
    out = B.results_board(rows)
    g = out["groups"]["picks"]
    assert g["total"] == {"n": 4, "won": 2, "lost": 1, "push": 1, "hit": round(2 / 3, 3), "units": 1.4,
                          "roi": 0.35, "few": True}
    assert g["daily"] == {"dates": ["2026-10-24", "2026-10-25"], "won": [1, 1], "lost": [1, 0], "units": [-0.1, 1.4]}
    assert [(t["name"], t["units"]) for t in g["by_type"]] == [("Ambos anotan", 1.5), ("Gana el local", -0.1)]
    assert [(lg["name"], lg["won"], lg["lost"]) for lg in g["by_league"]] == [("La Liga", 1, 0), ("Premier League", 1, 1)]
    assert g["best"][0]["match"] == "E vs F" and g["best"][0]["odds"] == "+150"     # americano, nunca decimal
    assert [w["match"] for w in g["worst"]] == ["C vs D"] and g["worst"][0]["model"] == 0.70
    assert out["groups"]["rule"]["total"]["n"] == 3                                  # sin el que no pasa la regla
    bets = out["groups"]["bets"]
    assert bets["total"]["lost"] == 1 and bets["by_league"][0]["name"] == "La Liga" and bets["worst"][0]["profit"] == -2.0


def test_results_endpoint_and_tab(dash):
    c = dash.app.test_client()
    r = c.get("/api/board/results").get_json()
    assert r["ok"] and set(r["groups"]) == {"picks", "rule", "bets"} and r["min_n"] == B.RESULTS_MIN_N
    html = c.get("/").get_data(as_text=True)
    for el_id in ('data-tab="resultados"', 'id="cdaily"', 'id="rtypes"', 'id="rleagues"', 'id="cleagues"',
                  'id="rbest"', 'id="rworst"'):
        assert el_id in html
