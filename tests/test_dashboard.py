"""
Dashboard local (dashboard/app.py): cada ruta responde con datos sintéticos
(sin DB ni red), cada endpoint que pide el JS existe, y la ganancia sale de
la liquidación real.

Hasta el 22-sep-26 dashboard/app.py era una reescritura a medias (29 nombres
sin definir, render_page duplicado y NINGUNA ruta registrada): el .exe en uso
servía una versión anterior que ya no se podía reconstruir desde el repo.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent


def _bet(day, match, result, profit, odds=1.80, clv=None):
    return {"match_date": pd.Timestamp(f"2026-09-{day} 15:00", tz="UTC"), "match": match,
            "league": "soccer_spain_la_liga", "market": "home_win", "probability": 0.60,
            "odds": odds, "stake": 1.0, "result": result, "profit": profit,
            "closing_odds": None, "clv": clv}


# Liquidación real (save_bets.resolve_market): ganada a 1.80 → +0.80,
# perdida → −1.00 (stake completo). La fórmula vieja del dashboard,
# (odds − 1)·stake·factor, contaba −0.80 por pérdida.
BETS = pd.DataFrame([
    _bet(19, "alpha vs beta", "win", 0.80, clv=0.016),
    _bet(20, "gamma vs delta", "loss", -1.00, clv=-0.015),
    _bet(21, "eps vs zeta", "loss", -1.00),
    _bet(22, "eta vs theta", "pending", 0.0),
])


@pytest.fixture
def dash(monkeypatch):
    import dashboard.app as dash

    def fake_q(sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM bankroll" in s:
            # la tabla tiene current_bankroll (no bankroll/updated_at)
            assert "current_bankroll" in s
            return pd.DataFrame({"bankroll": [57.9]})
        if "FROM goalscorer_picks" in s:
            return pd.DataFrame([{"match_date": "2026-09-21", "match": "alpha vs beta",
                                  "league": "soccer_spain_la_liga", "player": "X", "team": "alpha",
                                  "probability": 0.3, "fair_odds": 3.3, "odds_placed": None,
                                  "result": "win"}])
        if "FROM weekly_narrative" in s:
            return pd.DataFrame([{"week_start": "2026-09-15", "narrative": "texto",
                                  "engine": "ollama", "created_at": "2026-09-21 10:00"}])
        if "FROM bets_history" in s:
            df = BETS.copy()
            if "result IN" in s:
                df = df[df["result"] != "pending"]
            if "clv IS NOT NULL" in s:
                df = df[df["clv"].notna()]
            return df
        raise AssertionError(f"SQL inesperado en el dashboard: {s[:120]}")

    def no_network(*a, **k):
        raise OSError("sin red en tests")

    monkeypatch.setattr(dash, "_q", fake_q)
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return dash


@pytest.fixture
def client(dash):
    return dash.app.test_client()


def test_every_endpoint_the_js_calls_exists(dash):
    js = (ROOT / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")
    called = set(re.findall(r"""['"](/api/[a-z_/]+)""", js))
    assert called, "ui_es5.js no llama a ningún endpoint"
    rules = [r.rule for r in dash.app.url_map.iter_rules()]
    missing = [c for c in called
               if not any(r == c or (c.endswith("/") and r.startswith(c)) for r in rules)]
    assert not missing, f"el JS llama endpoints que no existen: {missing}"


def test_page_and_static_assets_are_served_locally(client):
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert '<script src="/ui_es5.js">' in html and '<script src="/chartjs.js">' in html
    for path in ("/ui_es5.js", "/chartjs.js"):
        r = client.get(path)
        assert r.status_code == 200, path          # 302 = Chart.js no está en el repo
        assert "javascript" in r.content_type and len(r.get_data()) > 1000
        r.close()


def test_kpis_use_the_settled_profit_not_a_recomputation(client):
    k = client.get("/api/kpis").get_json()
    assert k["ok"] and k["resolved"] == 3 and k["pending"] == 1
    assert k["profit"] == pytest.approx(-1.20)        # la fórmula vieja daba −0.80
    assert k["roi"] == pytest.approx(-0.40)
    assert k["bankroll"] == 57.9 and k["win_rate"] == pytest.approx(0.333)


def test_equity_and_breakdowns_add_up_to_the_same_profit(client):
    eq = client.get("/api/equity").get_json()
    assert eq["ok"] and eq["cumulative"][-1] == pytest.approx(-1.20)
    by = client.get("/api/by/market").get_json()
    assert by["ok"] and by["n"] == [3] and by["profit"] == [pytest.approx(-1.20)]


def test_by_dimension_only_accepts_known_columns(client):
    """La dimensión va a un f-string SQL: solo market y league."""
    assert client.get("/api/by/league").status_code == 200
    assert client.get("/api/by/stake;drop").status_code == 404


def test_tables_and_panels(client):
    bets = client.get("/api/bets?status=all").get_json()
    assert bets["ok"] and {"result", "result_key"} <= set(bets["bets"][0])
    assert client.get("/api/clv").get_json()["ok"]
    assert client.get("/api/scorers").get_json()["ok"]
    assert client.get("/api/narrative").get_json()["narrative"] == "texto"
    gh = client.get("/api/gh/status").get_json()
    assert gh == {"ok": False, "configured": False, "msg": gh["msg"]}


def test_version_comes_from_the_version_file(client):
    v = client.get("/api/version").get_json()
    expected = (ROOT / "VERSION").read_bytes().decode("utf-8", "ignore").replace("\x00", "").lstrip("﻿").strip()
    assert v["current"] == expected and v["latest"] is None and v["update_available"] is False


def _failing_db(monkeypatch, message):
    """El _q real, con pd.read_sql fallando como falla contra Neon."""
    import dashboard.app as dash

    def boom(sql, con=None, params=None, **kw):
        raise RuntimeError(message)

    monkeypatch.setattr(dash.pd, "read_sql", boom)
    return dash.app.test_client()


def test_connection_errors_say_so_instead_of_no_data(monkeypatch):
    """Una conexión que Neon cerró no debe verse como "no hay apuestas"
    (23-sep-26: la tabla mostraba "sin datos" con 200 bets en la base)."""
    import dashboard.app as dash
    client = _failing_db(monkeypatch, "(psycopg2.OperationalError) SSL connection has "
                                      "been closed unexpectedly")
    for path in ("/api/bets", "/api/kpis", "/api/equity", "/api/by/market"):
        d = client.get(path).get_json()
        assert d == {"ok": False, "msg": dash.DB_DOWN}, path


def test_missing_table_is_no_data_not_a_connection_problem(monkeypatch):
    client = _failing_db(monkeypatch, "Execution failed on sql: (psycopg2.errors."
                                      "UndefinedTable) relation \"goalscorer_picks\" does not exist")
    assert client.get("/api/scorers").get_json() == {"ok": False, "msg": "sin datos"}


def test_dashboard_engine_survives_idle_connections():
    """Proceso de larga vida: prueba la conexión antes de usarla y la
    renueva antes de que Neon la corte."""
    import dashboard.app as dash
    assert dash.engine.pool._pre_ping is True
    assert 0 < dash.engine.pool._recycle <= 300


def test_dispatch_needs_a_known_workflow_and_a_token(client):
    assert client.post("/api/gh/dispatch/weekly").status_code == 400      # sin GH_TOKEN
    assert client.post("/api/gh/dispatch/borrar_todo").status_code == 404
