"""
Métricas de rendimiento solo con apuestas jugadas (28-sep-26).

El reporte semanal del lunes 28-sep contó las 14 canceladas del viernes
como apuestas: "0 bets" con sus mercados listados, y un acumulado de 1,180
bets / ROI −7.3% cuando lo real era 1,128 / −7.7%. El dashboard mostraba
"Bets 90d" con canceladas y las dejaba en la lista después del partido.
"""

from datetime import date

import pandas as pd

from src.utils.bet_status import CANCELLED_SQL, RESOLVED_SQL


# ============================================================
# Reporte semanal de Telegram
# ============================================================

def _run_weekly(monkeypatch, week, total, cancelled):
    import scripts.notify_telegram as nt
    import src.models.bankroll_manager as bm
    seen, sent = [], []

    def fake_read_sql(sql, con=None, **kw):
        s = str(sql)
        seen.append(" ".join(s.split()))
        if "COUNT(*)" in s:
            return pd.DataFrame([{"n": cancelled}])
        return (week if "7 days" in s else total).copy()

    monkeypatch.setattr(nt.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(bm, "get_bankroll_stats", lambda: {
        "current": 58.48, "initial": 100.0, "roi_pct": -41.5, "total_profit": -41.52,
        "drawdown_pct": 45.0})
    nt.send_weekly_report()
    return sent[0], seen


def _bets(results):
    return pd.DataFrame([{"market": "home_win", "odds": 2.0, "stake": 1.0, "result": r,
                          "profit": {"win": 1.0, "loss": -1.0, "push": 0.0,
                                     "half_win": 0.5, "half_loss": -0.5}[r],
                          "league": "soccer_usa_mls"} for r in results])


def test_weekly_report_counts_only_played_bets_and_lists_cancelled_apart(monkeypatch):
    week = _bets(["win", "win", "loss", "push"])
    total = _bets(["win"] * 3 + ["loss"] * 5)
    msg, seen = _run_weekly(monkeypatch, week, total, cancelled=14)
    assert "Bets:     4  (2W / 1L / 1 nulas  67%)" in msg
    assert "ROI:      📈 +25.0%" in msg                                # +1u / 4u
    assert "Canceladas antes del partido (no se apostaron): 14" in msg
    assert "Bets totales: 8  (WR: 38%)" in msg
    # la base filtra: solo jugadas y liquidadas; las canceladas, en su conteo
    assert all(RESOLVED_SQL in s for s in seen if "COUNT(*)" not in s)
    assert any("result = 'stale'" in s and CANCELLED_SQL in s for s in seen)


def test_week_with_only_cancelled_bets_says_so(monkeypatch):
    msg, _ = _run_weekly(monkeypatch, _bets([]).iloc[:0], _bets(["loss"]), cancelled=14)
    assert "Sin apuestas jugadas esta semana." in msg
    assert "Canceladas antes del partido (no se apostaron): 14" in msg
    assert "POR MERCADO" not in msg and "Bets:     0" not in msg


# ============================================================
# Dashboard
# ============================================================

def _kpi_rows():
    rows = [("win", 1.0, 0.02), ("loss", -1.0, -0.01), ("push", 0.0, None),
            ("pending", 0.0, None), ("stale", 0.0, 0.30), ("stale", 0.0, None)]
    return pd.DataFrame([{"result": r, "stake": 1.0, "odds": 2.0, "probability": 0.55,
                          "clv": c, "match_date": pd.Timestamp("2026-09-20", tz="UTC"),
                          "profit": p} for r, p, c in rows])


def test_dashboard_kpis_ignore_cancelled_and_sourceless(monkeypatch):
    import dashboard.app as dash
    monkeypatch.setattr(dash, "_q", lambda sql, params=None: (
        pd.DataFrame([{"bankroll": 58.48}]) if "bankroll" in sql else _kpi_rows()))
    d = dash.app.test_client().get("/api/kpis").get_json()
    assert (d["bets_90d"], d["resolved"], d["pending"]) == (4, 3, 1)   # sin las 2 'stale'
    assert d["win_rate"] == 0.5                                           # 1 / (1 + 1), la nula fuera
    assert (d["clv_n"], d["clv_avg"]) == (2, 0.005)                       # el +30% de la stale, fuera


def test_cancelled_bets_leave_the_main_list_after_kickoff(monkeypatch):
    import dashboard.app as dash
    seen = []
    monkeypatch.setattr(dash, "_q", lambda sql, params=None: seen.append(sql) or pd.DataFrame())
    client = dash.app.test_client()
    client.get("/api/bets")
    assert f"({CANCELLED_SQL} AND match_date > NOW())" in seen[-1]
    client.get("/api/bets?status=cancelled")
    assert f"AND {CANCELLED_SQL}" in " ".join(seen[-1].split())
    assert 'value="cancelled"' in client.get("/").get_data(as_text=True)


# ============================================================
# Análisis semanales
# ============================================================

def test_walkforward_reads_only_played_bets(monkeypatch):
    import src.models.walkforward_backtest as wf
    seen = []
    monkeypatch.setattr(wf.pd, "read_sql", lambda sql, con=None, **kw: seen.append(sql) or pd.DataFrame())
    assert wf.run_walkforward(verbose=False) == {}
    assert RESOLVED_SQL in seen[0] and "NOT IN" not in seen[0]


# ============================================================
# Reparación: canceladas que la recuperación de stale liquidó
# ============================================================

BETIS = {"id": 4194, "match": "real betis vs getafe", "market": "over25",
         "match_date": "2026-09-17", "result": "loss", "profit": -0.99}


def test_resurrected_cancelled_bet_goes_back_and_the_bankroll_recovers(monkeypatch):
    import scripts.fix_resurrected_cancelled as fx
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(fx, "engine", eng)
    monkeypatch.setattr(fx, "ensure_bankroll_schema", lambda: None)
    assert len(fx.apply_fixes([BETIS])) == 1
    (sql, p), = eng.statements("UPDATE bets_history")
    assert "result = 'stale', profit = 0.0" in sql and "AND result = :old_result" in sql
    assert (p["id"], p["old_result"]) == (4194, "loss") and '"profit": -0.99' in p["note"]
    (_, bank), = eng.statements("UPDATE bankroll")
    assert bank["profit"] == 0.99                                         # vuelve el −0.99u
    (_, hist), = eng.statements("INSERT INTO bankroll_history")
    assert "real betis vs getafe | over25 | loss → cancelada" in hist["notes"]


def test_running_the_fix_twice_does_nothing(monkeypatch):
    import scripts.fix_resurrected_cancelled as fx
    from tests.fake_db import FakeEngine, FakeResult
    eng = FakeEngine(lambda sql, p: FakeResult(rowcount=0) if sql.startswith("UPDATE bets_history")
                     else FakeResult())
    monkeypatch.setattr(fx, "engine", eng)
    monkeypatch.setattr(fx, "ensure_bankroll_schema", lambda: None)
    assert fx.apply_fixes([BETIS]) == []
    assert eng.statements("UPDATE bankroll") == []


# ============================================================
# Cargas de datos
# ============================================================

def test_season_start_reads_both_formats():
    from scripts.load_extra_leagues import season_start
    assert season_start("2026/2027") == 2026                             # J-League desde 2026-27
    assert season_start("2025") == 2025 and season_start(2024) == 2024
    assert season_start(None) is None and season_start("") is None and season_start("x") is None


def test_weekly_loader_includes_the_current_season():
    from scripts.load_historical_data import season_codes
    assert season_codes(date(2026, 9, 28))[-1] == "2627"                 # antes la lista acababa en 2526
    assert season_codes(date(2026, 6, 30))[-1] == "2526"                 # antes de julio: la anterior
    assert season_codes(date(2026, 9, 28))[0] == "1516"
