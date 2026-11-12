"""
tests/test_db_budget.py
=======================
El presupuesto del plan gratis de Neon (11-nov-26). El 9-nov la base se
bloqueó por pasarse de la transferencia del plan gratis y todo el programa
quedó detenido. El dueño: "esto tiene que funcionar 100% con planes gratis…
no debemos sufrir estos ataques y pausar todo por eso".
"""

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.utils import db_budget as B
from src.utils import db_meter as M


# ── El medidor: bytes y base despierta ───────────────────────────────────────

class _PgCursor:
    """Como el cursor de psycopg2 del lado del cliente: ya trae todas las filas."""

    def __init__(self, rows, broken_scroll=False):
        self._rows, self.rownumber, self.name = rows, 0, None
        self.rowcount = len(rows)
        self.description = [("a",), ("b",)]
        self._broken = broken_scroll

    def fetchall(self):
        out = self._rows[self.rownumber:]
        self.rownumber = len(self._rows)
        return out

    def scroll(self, value, mode="relative"):
        new = value if mode == "absolute" else self.rownumber + value
        if self._broken or new < 0 or new >= len(self._rows):      # como psycopg2: fuera de rango
            raise ValueError("scroll destination out of bounds")
        self.rownumber = new


_PgCursor.__module__ = "psycopg2.extensions"


def test_the_meter_counts_the_bytes_and_leaves_the_rows_for_the_caller():
    M.reset()
    cur = _PgCursor([("hola", 12), ("adiós", None)])
    M._after_execute(None, cur, "SELECT a, b FROM t", None, None, False)
    assert cur.fetchall() == [("hola", 12), ("adiós", None)]          # quien consultó lee todo
    t = M.totals()
    assert (t["rows"], t["cells"]) == (2, 4)
    assert t["bytes"] == 2 * M.ROW_OVERHEAD + 4 * M.FIELD_OVERHEAD + len("hola") + 2 + len("adiós")
    M.reset()


def test_an_empty_result_is_measured_without_rewinding():
    """11-nov-26, contra la base real: psycopg2 no deja rebobinar un resultado
    vacío ("scroll destination out of bounds") y la consulta reventaba."""
    M.reset()
    cur = _PgCursor([])
    M._after_execute(None, cur, "SELECT a, b FROM t WHERE false", None, None, False)
    assert M.totals()["bytes"] == 0 and cur.fetchall() == []
    M.reset()


def test_a_cursor_that_cannot_rewind_is_an_error_not_missing_rows():
    with pytest.raises(RuntimeError, match="rebobinar"):
        M._after_execute(None, _PgCursor([("x", 1)], broken_scroll=True), "SELECT a, b FROM t", None, None, False)
    M.reset()


def test_other_cursors_are_estimated_without_reading_them():
    class Other:
        rowcount, description, name = 10, [("a",), ("b",), ("c",)], None
    M.reset()
    M._after_execute(None, Other(), "SELECT a, b, c FROM t", None, None, False)
    assert M.totals()["bytes"] == 10 * M.ROW_OVERHEAD + 30 * (M.FIELD_OVERHEAD + M.AVG_CELL_BYTES)
    M.reset()


def test_the_database_counts_as_awake_five_minutes_after_each_query():
    M.reset()
    M._AWAKE["until"] = 0.0
    for t in (1000.0, 1100.0, 3000.0):                 # dos seguidas y otra media hora después
        M._awake(t)
    assert M.totals()["awake_s"] == 300 + 100 + 300
    M.reset()


def test_other_processes_save_what_they_spent_when_they_finish(monkeypatch):
    import src.utils.pipeline_runs as PR
    calls = []
    monkeypatch.setattr(PR, "record_run", lambda eng, *a: calls.append(a))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(M, "_STATE", {"recorded": False, "started": 0.0})
    M.reset()
    M.record("SELECT 1", 1, 1, 50)
    M._record_at_exit()                                # con pytest cargado no guarda nada
    assert calls == []
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(sys, "argv", ["scripts/watchdog.py"])
    M._record_at_exit()
    assert calls and calls[0][0] == "proc:watchdog" and calls[0][5] == 50
    M.mark_recorded()                                  # el orquestador ya lo guardó: no se repite
    M._record_at_exit()
    assert len(calls) == 1
    M.reset()


# ── El pronóstico del mes ────────────────────────────────────────────────────

NOW = datetime(2026, 12, 11, 12, tzinfo=timezone.utc)


def _usage(gb_day, awake_h_day, since=datetime(2026, 12, 1, tzinfo=timezone.utc)):
    days = (NOW - since).total_seconds() / 86400
    per_day = gb_day * 1e9 / B.WIRE_FACTOR
    return {"measured_since": since, "bytes": per_day * days, "awake_s": awake_h_day * 3600 * days,
            "rate_since": NOW - timedelta(days=3), "rate_bytes": per_day * 3, "rate_awake_s": awake_h_day * 3600 * 3}


def test_a_month_well_under_the_free_plan_runs_normal():
    st = B.project(_usage(gb_day=0.08, awake_h_day=6), NOW)       # 2.5 GB y 45 h al mes
    assert st["level"] == "normal" and st["proj_gb"] == pytest.approx(2.48, abs=0.01)
    assert st["pct_compute"] == pytest.approx(46.5, abs=0.5)


@pytest.mark.parametrize("gb_day,level", [(0.13, "ahorro"), (0.16, "esencial")])
def test_the_brake_turns_on_before_the_limit(gb_day, level):
    st = B.project(_usage(gb_day=gb_day, awake_h_day=4), NOW)
    assert st["level"] == level and st["worst"] == "transfer"


def test_compute_hours_also_brake():
    st = B.project(_usage(gb_day=0.02, awake_h_day=11), NOW)      # 0.25 × 11 h × 31 días = 85 h
    assert st["level"] == "ahorro" and st["worst"] == "compute"


def test_a_measurement_that_starts_mid_month_estimates_the_days_before():
    since = datetime(2026, 12, 8, tzinfo=timezone.utc)
    st = B.project(_usage(gb_day=0.1, awake_h_day=4, since=since), NOW)
    assert st["estimated_days"] == 7.0
    assert st["used_gb"] == pytest.approx(0.1 * 10.5, abs=0.01)   # 3.5 medidos + 7 estimados


def test_too_little_measurement_has_no_opinion():
    u = _usage(gb_day=1.0, awake_h_day=20)
    u["rate_since"] = NOW - timedelta(hours=2)
    assert B.project(u, NOW) == {"level": "normal", "measured": False, "month": "2026-12"}
    assert B.status(engine=object(), now=NOW)["level"] == "normal"        # sin base: nunca frena a ciegas


def test_each_warning_goes_once_a_month():
    st = B.project(_usage(gb_day=0.12, awake_h_day=4), NOW)       # ~74%
    assert B.threshold_crossed(st, {}) == 50
    assert B.threshold_crossed(st, {"2026-12": 50}) is None
    assert B.threshold_crossed(st, {"2026-11": 75}) == 50          # otro mes
    st = B.project(_usage(gb_day=0.13, awake_h_day=4), NOW)
    assert B.threshold_crossed(st, {"2026-12": 50}) == 75


def test_the_owner_hears_when_the_mode_changes_and_when_it_crosses_a_warning():
    from scripts.orchestrator import budget_messages
    st = B.project(_usage(gb_day=0.13, awake_h_day=4), NOW)
    msgs, state = budget_messages(st, {})
    assert len(msgs) == 1 and "Modo ahorro" in msgs[0] and "cada hora" in msgs[0]
    assert state == {"level": "ahorro", "alerted": {"2026-12": 75}}
    assert budget_messages(st, state) == ([], None)                   # nada nuevo: silencio
    calm = B.project(_usage(gb_day=0.09, awake_h_day=4), NOW)
    msgs, state = budget_messages(calm, state)
    assert "volvió a su ritmo normal" in msgs[0] and state["level"] == "normal"
    msgs, state = budget_messages(calm, {"level": "normal"})
    assert "50% del plan gratis" in msgs[0] and state["alerted"] == {"2026-12": 50}


# ── Lo que hace el freno ─────────────────────────────────────────────────────

def test_saving_mode_keeps_one_closing_an_hour_and_every_closing_before_a_match():
    from scripts.orchestrator import closing_skip_reason as skip
    assert skip("normal", 40, 0) is None
    assert skip("ahorro", 5, 0) is None                    # el de la primera media hora corre
    assert "modo ahorro" in skip("ahorro", 35, 0)
    assert skip("ahorro", 35, 2) is None                   # hay partidos en las próximas 2 h
    assert "modo esencial" in skip("esencial", 5, 0)
    assert skip("esencial", 35, 1) is None


def test_optional_steps_are_skipped_only_in_their_mode(monkeypatch):
    import scripts.orchestrator as O
    ran = []

    class L:
        steps_total = steps_ok = 0
        failed_steps = []

        def log(self, m):
            ran.append(m)
    for level, expect in (("normal", 2), ("ahorro", 1), ("esencial", 0)):
        monkeypatch.setitem(O._BUDGET, "level", level)
        calls = []
        O.run_step(L(), "Backtest", lambda: calls.append(1), skip_at="ahorro")
        O.run_step(L(), "ClubElo", lambda: calls.append(1), skip_at="esencial")
        assert len(calls) == {"normal": 2, "ahorro": 1, "esencial": 0}[level] == expect
    assert any("se salta (modo esencial de la base" in m for m in ran)


def test_reports_about_the_owners_own_bets_are_archived_while_there_are_no_new_bets(monkeypatch):
    """12-nov-26, el dueño (3c): Backtest, Holdout, Drift y Walk-forward solo analizan las apuestas
    propias ya cobradas (bets_history; la última, del 22-sep). No corren mientras no se cobre una nueva.
    CLV update no se archiva: una apuesta nueva lo necesita (hubo 2 en octubre)."""
    import pandas as pd
    import scripts.orchestrator as O
    now = pd.Timestamp("2026-11-12 03:00", tz="UTC")
    monkeypatch.setitem(O._BUDGET, "level", "normal")
    for last, expect in ((pd.Timestamp("2026-09-24 19:00"), "24-sep-2026"),          # congelada: archivado
                         (pd.Timestamp("2026-11-01 19:00", tz="UTC"), None),         # apuestas recientes: corre
                         (None, "nunca"),                                           # tabla vacía: archivado
                         (False, None)):                                            # no se pudo leer: corre
        monkeypatch.setattr(O, "_OWN_BETS", {"last": last})
        assert O.own_bets_idle(now) == expect
    ran, logged = [], []

    class L:
        steps_total = steps_ok = 0
        failed_steps = []

        def log(self, m):
            logged.append(m)
    monkeypatch.setattr(O, "_OWN_BETS", {"last": pd.Timestamp("2026-09-24 19:00")})
    assert O.run_step(L(), "Backtest", lambda: ran.append("backtest"), own_bets=True) is True
    assert O.run_step(L(), "Results", lambda: ran.append("results")) is True       # los demás pasos, como siempre
    assert ran == ["results"]
    assert any("Backtest — archivado: solo analiza las apuestas propias ya cobradas y la última es de 24-sep-2026"
               in m for m in logged)
    monkeypatch.setattr(O, "_OWN_BETS", {"last": pd.Timestamp.now(tz="UTC")})       # el dueño vuelve a apostar
    O.run_step(L(), "Backtest", lambda: ran.append("backtest"), own_bets=True)
    assert ran == ["results", "backtest"]
    # los cinco que se archivan, y solo esos
    steps = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, fn, *a, **k: steps.append((name, k.get("own_bets", False))) or True)
    monkeypatch.setattr(O, "closing_skip_reason", lambda *a, **k: None, raising=False)
    O.run_weekly(None)
    O.run_results_only(None)
    archived = {n for n, own in steps if own}
    assert archived == {"Holdout evaluation", "Drift detection", "Walk-forward backtest", "Backtest"}
    assert ("CLV update", False) in steps                                           # sirve a una apuesta nueva

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.append((str(sql), params))
            return type("R", (), {"scalar": lambda self: pd.Timestamp("2026-09-22 00:15")})()
    seen = []
    import config.database as DB
    monkeypatch.setattr(DB, "engine", type("E", (), {"connect": lambda self: Conn()})())
    monkeypatch.setattr(O, "_OWN_BETS", {})
    assert O.own_bets_idle(now) == "22-sep-2026" and O.own_bets_idle(now) == "22-sep-2026"
    assert len(seen) == 1                                                           # una lectura por corrida
    assert "MAX(match_date) FROM bets_history WHERE result IN" in seen[0][0]         # solo las cobradas
    assert seen[0][1] == {"r": list(O.OWN_BETS_SETTLED)} and "stale" not in O.OWN_BETS_SETTLED


def test_without_a_database_a_run_ends_quietly_and_says_why(monkeypatch):
    import scripts.orchestrator as O
    assert O.db_down_reason("OperationalError: ERROR: Your account or project has exceeded the quota") == \
        "Neon la bloqueó por pasarse del límite del plan gratis"
    assert "contraseña" in O.db_down_reason("password authentication failed for user x")
    assert O.db_down_reason("could not connect to server") == "no contesta (conexión)"


def test_the_watchdog_explains_a_blocked_database():
    from scripts.watchdog import db_down_message
    msg = db_down_message("ERROR: Your account or project has exceeded the quota", "…")
    assert "BLOQUEADA" in msg and "plan gratis" in msg and "día 1" in msg
    assert "NO CONTESTA" in db_down_message("timeout expired", "…")


def test_the_daily_check_sees_a_silent_meter_and_essential_mode():
    from src.utils.output_checks import budget_problems
    assert budget_problems({"level": "normal", "measured": True, "pct": 30}, 40) == []
    assert "ninguna corrida guardó" in budget_problems({"level": "normal"}, 0)[0]
    st = B.project(_usage(gb_day=0.16, awake_h_day=4), NOW)
    assert "modo esencial" in budget_problems(st, 40)[0]


# ── El dashboard ─────────────────────────────────────────────────────────────

def test_the_dashboard_saves_what_it_spent_every_half_hour(monkeypatch):
    from dashboard import app as A
    import src.utils.pipeline_runs as PR
    calls = []
    monkeypatch.setattr(PR, "record_run", lambda eng, *a: calls.append(a))
    clock = [10_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    monkeypatch.setitem(A._METER_FLUSH, "last", None)
    M.reset()
    with A.app.test_request_context("/api/x"):
        A._flush_db_meter(A.app.response_class("{}"))      # empieza a contar
        M.record("SELECT x", 10, 20, 5000)
        clock[0] += 600
        A._flush_db_meter(A.app.response_class("{}"))
        assert calls == []                                   # aún no son 30 min
        clock[0] += A.METER_FLUSH_S
        A._flush_db_meter(A.app.response_class("{}"))
    assert calls and calls[0][0] == "dashboard" and calls[0][5] == 5000
    assert M.totals()["queries"] == 0
    M.reset()


def test_in_saving_mode_the_page_refreshes_every_15_minutes(monkeypatch):
    from pathlib import Path
    js = (Path(__file__).parent.parent / "dashboard" / "ui_es5.js").read_text(encoding="utf-8")
    assert "_saving = d.budget_level === 'ahorro' || d.budget_level === 'esencial';" in js
    assert "AUTO_MS = (_saving ? 15 : 5) * 60000;" in js
    from dashboard import app as A
    monkeypatch.setattr(A, "_q", lambda *a, **k: __import__("pandas").DataFrame())
    monkeypatch.setattr(A, "_budget_status", lambda: {"level": "ahorro"})
    with A.app.test_client() as c:
        d = c.get("/api/board/header").get_json()
    assert d["budget_level"] == "ahorro"


def test_weekly_jobs_are_spread_over_the_week():
    """11-nov-26: recién medido, el weekly y la primera copia contaban como si
    pasaran cada día y el pronóstico salía del 400%."""
    u = _usage(gb_day=0.05, awake_h_day=3)
    u["week_bytes"] = 0.35e9 / B.WIRE_FACTOR                  # 350 MB de weekly + respaldo en la semana
    st = B.project(u, NOW)
    assert st["gb_day"] == pytest.approx(0.05 + 0.05, abs=0.001)


def test_with_the_paid_plan_it_measures_but_does_not_brake():
    from scripts.orchestrator import budget_messages
    st = B.project(_usage(gb_day=0.16, awake_h_day=4), NOW)
    assert st["level"] == "esencial"
    paid = B.paid_plan(st, '{"paid_until": "2026-12-20"}', NOW)
    assert paid["level"] == "normal" and paid["free_level"] == "esencial"
    assert "plan de pago hasta el 2026-12-20" in B.describe(paid) and "sería modo esencial" in B.describe(paid)
    assert budget_messages(paid, {}) == ([], None)
    assert B.paid_plan(st, {"paid_until": "2026-12-01"}, NOW)["level"] == "esencial"     # ya venció
    assert B.paid_plan(st, None, NOW)["level"] == "esencial"


# ── La cuenta del plan de pago y la vuelta al gratis (11-nov-26) ─────────────
# La alerta de $15 que el dueño puso en Neon es un correo que no se puede
# probar; el programa lleva su propia cuenta y avisa por Telegram.

DEC1 = datetime(2026, 12, 1, tzinfo=timezone.utc)


def _paid(awake_h_day, until="2027-01-01", paid_days=None, gb=0.2, **plan):
    """El estado con el plan de pago: awake_h_day horas encendida al día desde el día 1."""
    st = B.project(_usage(gb_day=0.02, awake_h_day=awake_h_day), NOW)
    days = (NOW - DEC1).total_seconds() / 86400 if paid_days is None else paid_days
    usage = {"paid_measured_since": DEC1, "paid_awake_s": awake_h_day * 3600 * days, "storage_bytes": gb * 1e9}
    return B.paid_plan(st, {"paid_until": until, "plan": "Launch", **plan}, NOW, usage)


def test_the_paid_plan_bill_comes_from_the_hours_the_database_is_awake():
    st = _paid(awake_h_day=6, until="2026-12-21")
    cuh = 6 * 10.5 * B.CU_SIZE                                  # 10.5 días a 6 h, base de 0.25
    assert st["bill_usd"] == pytest.approx(cuh * B.PAID_CU_HOUR_USD + 0.2 * B.PAID_GB_MONTH_USD * 10.5 / 31, abs=0.01)
    more = 6 * 9.5 * B.CU_SIZE                                  # lo que falta hasta el 21
    assert st["bill_proj_usd"] == pytest.approx((cuh + more) * B.PAID_CU_HOUR_USD
                                                + 0.2 * B.PAID_GB_MONTH_USD * 20 / 31, abs=0.01)
    assert st["bill_until"] == "2026-12-21" and st["bill_cap_usd"] == B.PAID_CAP_USD
    assert "cuenta estimada: $1.71 hasta hoy, $3.25 al 2026-12-21; tope $15" in B.describe(st)
    assert _paid(6, until="2027-02-01")["bill_until"] == "2027-01-01"        # Neon cobra por mes de calendario
    assert _paid(6, cap_usd=8)["bill_cap_usd"] == 8


def test_what_ran_before_the_meter_started_is_estimated_on_the_high_side():
    """El 11-nov el plan empezó a las 04:00 UTC y el medidor a las 05:14."""
    st = B.project(_usage(gb_day=0.02, awake_h_day=6), NOW)
    plan = {"paid_until": "2027-01-01", "since": "2026-12-08T00:00:00+00:00"}
    assert B.paid_from(plan, NOW) == datetime(2026, 12, 8, tzinfo=timezone.utc)
    assert B.paid_from({"paid_until": "2027-01-01", "since": "2026-11-11"}, NOW) == DEC1     # mes nuevo: desde el 1
    usage = {"paid_measured_since": datetime(2026, 12, 9, tzinfo=timezone.utc), "paid_awake_s": 6 * 3600 * 2.5}
    bill = B.paid_bill(st, plan, NOW, usage)
    assert bill["bill_usd"] == pytest.approx(6 * 3.5 * B.CU_SIZE * B.PAID_CU_HOUR_USD, abs=0.01)   # 2.5 medidos + 1 estimado
    assert B.paid_bill({"measured": False}, plan, NOW, usage) == {}          # sin medición no opina


def test_the_owner_hears_before_the_bill_reaches_the_cap_and_when_it_passes_it():
    from scripts.orchestrator import budget_messages
    cheap = _paid(awake_h_day=6)                                             # ~$5 al mes
    assert B.bill_stage(cheap) == 0 and budget_messages(cheap, {}) == ([], None)
    st = _paid(awake_h_day=24)                                               # encendida todo el día: ~$20
    assert st["bill_usd"] < 15 <= st["bill_proj_usd"] and B.bill_stage(st) == 1
    msgs, state = budget_messages(st, {"level": "normal", "alerted": {"2026-11": 75}})
    assert len(msgs) == 1 and "va para $20" in msgs[0] and "tope de tu alerta de Neon es $15" in msgs[0]
    assert "unas 24 al día" in msgs[0]
    assert state == {"level": "normal", "alerted": {"2026-11": 75}, "bill_alerted": {"2026-12": 1}}
    assert budget_messages(st, state) == ([], None)                          # una vez por mes
    over = _paid(awake_h_day=24, paid_days=25)                               # ya gastado más que el tope
    assert B.bill_stage(over) == 2
    msgs, state = budget_messages(over, state)
    assert "ya pasó de $15" in msgs[0] and state["bill_alerted"] == {"2026-12": 2}
    assert budget_messages(over, state) == ([], None)
    assert budget_messages(st, {"bill_alerted": {"2026-11": 2}})[0]          # otro mes: se avisa de nuevo


def test_a_bill_near_the_cap_warns_with_margin():
    """El medidor no ve las consultas hechas a mano: avisa desde el 80% del tope."""
    st = _paid(awake_h_day=6, cap_usd=6)                                     # ~$5 de 6
    assert st["bill_proj_usd"] < 6 and B.bill_stage(st) == 1


def test_when_the_paid_period_ends_the_owner_is_reminded_to_go_back_to_free_once():
    from scripts.orchestrator import budget_messages
    st = B.project(_usage(gb_day=0.02, awake_h_day=2), NOW)                  # NOW = 11-dic 12:00 UTC
    plan = {"paid_until": "2026-12-11T00:00:00+00:00", "plan": "Launch"}
    early = B.paid_plan(st, plan, NOW - timedelta(hours=1))                  # el mes de Neon cierra en UTC
    assert "downgrade_due" not in early and "paid_until" not in early
    due = B.paid_plan(st, plan, NOW)
    assert due["downgrade_due"] == "2026-12-11" and due["level"] == "normal"
    msgs, state = budget_messages(due, {"bill_alerted": {"2026-11": 1}})
    assert len(msgs) == 1 and "bajar Neon al plan gratis" in msgs[0] and "plan Launch terminó el 2026-12-11" in msgs[0]
    assert "Change plan" in msgs[0] and "0.25 CU" in msgs[0]
    assert state == {"bill_alerted": {"2026-11": 1}, "downgrade_reminded": "2026-12-11", "level": "normal",
                     "alerted": {}}
    assert budget_messages(due, state) == ([], None)                         # una sola vez
    assert "downgrade_due" not in B.paid_plan(st, plan, NOW + timedelta(days=15))
    hot = B.paid_plan(B.project(_usage(gb_day=0.13, awake_h_day=4), NOW), plan, NOW)
    msgs, state = budget_messages(hot, {})                                   # el freno del gratis ya actúa
    assert len(msgs) == 2 and "bajar Neon" in msgs[0] and "Modo ahorro" in msgs[1]
    assert state["level"] == "ahorro" and state["downgrade_reminded"] == "2026-12-11"


class _Conn:
    def __init__(self, plan, log):
        self.plan, self.log = plan, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        sql = str(sql)
        self.log.append((sql, params))
        out = SimpleNamespace()
        if "db_budget_plan" in sql:
            out.scalar = lambda: self.plan
        elif "pg_database_size" in sql:
            out.scalar = lambda: 250_000_000
        else:
            row = {**_usage(gb_day=0.02, awake_h_day=6), "paid_measured_since": DEC1, "paid_awake_s": 6 * 3600 * 10.5}
            out.mappings = lambda: SimpleNamespace(first=lambda: row)
        return out


def test_the_status_reads_the_plan_first_and_the_size_only_while_it_is_paid():
    log = []
    engine = SimpleNamespace(connect=lambda: _Conn('{"paid_until": "2026-12-21", "since": "2026-12-05"}', log))
    st = B.status(engine, NOW)
    assert st["paid_until"] == "2026-12-21" and st["bill_usd"] > 0 and st["level"] == "normal"
    assert [("db_budget_plan" in s, "pg_database_size" in s) for s, _ in log] == [(True, False), (False, False),
                                                                                (False, True)]
    assert log[1][1]["paid_from"] == datetime(2026, 12, 5, tzinfo=timezone.utc)
    log.clear()
    st = B.status(SimpleNamespace(connect=lambda: _Conn(None, log)), NOW)       # plan gratis: sin cuenta
    assert "bill_usd" not in st and len(log) == 2 and log[1][1]["paid_from"] == DEC1
