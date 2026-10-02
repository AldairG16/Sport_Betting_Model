"""
Vigilancia del closing y del weekly (24-sep-26): cada corrida del
orquestador deja una fila en pipeline_runs, y el watchdog avisa si el
closing (cada 30 min) o el weekly (lunes) dejan de correr — los dispara un
servicio externo (cron-job.org) que puede fallar sin que nada falle.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.utils.pipeline_runs import closing_issue, daily_issue, weekly_issue

NOW = datetime(2030, 1, 5, 12, 0, tzinfo=timezone.utc)
DAYS_AGO = NOW - timedelta(days=3)


def _every_30_min(hours: float, start=NOW):
    return [start - timedelta(minutes=30 * i) for i in range(1, int(hours * 2) + 1)]


def test_closing_every_30_min_is_healthy():
    runs = _every_30_min(8)
    assert closing_issue(runs, DAYS_AGO, runs[0], NOW) is None


def test_closing_stopped():
    msg = closing_issue([], DAYS_AGO, NOW - timedelta(hours=3), NOW)
    assert "CLOSING SIN CORRER" in msg and "3.0 h" in msg and "cron-job.org" in msg


def test_closing_irregular_when_only_github_sporadic_runs_remain():
    """El cron propio de GitHub sigue disparando a ratos: un closing reciente
    no prueba que el disparo cada 30 min viva."""
    runs = [NOW - timedelta(hours=h) for h in (1, 3, 5)]
    msg = closing_issue(runs, DAYS_AGO, runs[0], NOW)
    assert "CLOSING IRREGULAR" in msg and "Solo 3 corridas" in msg


def test_six_runs_in_six_hours_is_enough():
    runs = [NOW - timedelta(hours=h) for h in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5)]
    assert closing_issue(runs, DAYS_AGO, runs[0], NOW) is None


def test_no_opinion_on_regularity_right_after_install():
    """Menos historial que la ventana: sin falsa alarma de irregularidad."""
    runs = _every_30_min(2)
    assert closing_issue(runs, NOW - timedelta(hours=2), runs[0], NOW) is None
    assert closing_issue([], None, None, NOW) is None


@pytest.mark.parametrize("age_days,alert", [(1, False), (7.9, False), (9, True)])
def test_weekly(age_days, alert):
    msg = weekly_issue(NOW - timedelta(days=age_days), NOW)
    assert (msg is not None) is alert
    if alert:
        assert "WEEKLY SIN CORRER" in msg and "9 días" in msg


def test_weekly_without_data_has_no_opinion():
    assert weekly_issue(None, NOW) is None and weekly_issue(pd.NaT, NOW) is None


# ============================================================
# Trabajos de dos veces al día (1-oct-26): el resolvedor pasó del 11-may al
# 1-oct-26 sin correr una sola vez y nada lo detectó
# ============================================================

@pytest.mark.parametrize("mode", ["results", "resolve_pending"])
def test_daily_job_that_ran_recently_is_healthy(mode):
    assert daily_issue(mode, NOW - timedelta(hours=12), NOW) is None
    assert daily_issue(mode, NOW - timedelta(hours=29), NOW) is None


def test_daily_job_stopped():
    msg = daily_issue("resolve_pending", NOW - timedelta(hours=40), NOW)
    assert "RESOLVEDOR DE PENDIENTES SIN CORRER" in msg and "40 h" in msg
    assert "resolve_pending.yml" in msg and "heartbeat_dispatch.py" in msg


def test_daily_job_that_never_ran_is_a_problem():
    """Sin ninguna corrida registrada NO es "sin opinión": así se escondió el
    resolvedor (nunca registraba nada)."""
    for last in (None, pd.NaT):
        msg = daily_issue("results", last, NOW)
        assert "REINTENTO DE RESULTADOS SIN CORRER" in msg
        assert "No hay ninguna corrida registrada" in msg


def test_each_run_is_recorded_and_nothing_is_deleted():
    from src.utils.pipeline_runs import record_run
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    record_run(eng, "closing", 0, 63.04)
    (_, params), = eng.statements("INSERT INTO pipeline_runs")
    assert params == {"mode": "closing", "failed": 0, "seconds": 63.0}
    assert not eng.statements("DELETE")


def test_orchestrator_records_runs_without_ever_breaking_them(monkeypatch):
    import scripts.orchestrator as orch
    import src.utils.pipeline_runs as pr
    calls = []
    monkeypatch.setattr(pr, "record_run", lambda eng, *a: calls.append(a))
    orch._record_run("weekly", 2, 12.5)
    assert calls == [("weekly", 2, 12.5)]

    def boom(*a):
        raise RuntimeError("DB caída")
    monkeypatch.setattr(pr, "record_run", boom)
    orch._record_run("closing", 0, 1.0)            # no revienta el pipeline


# ============================================================
# El watchdog completo
# ============================================================

def _fake_world(monkeypatch, closing_runs, first_ever, last_weekly, last_run=None,
                daily=None, stuck=0, delivered=True):
    import config.database as cdb
    import scripts.watchdog as wd
    from tests.fake_db import FakeEngine
    now = pd.Timestamp.now(tz="UTC")
    last_run = last_run if last_run is not None else min(closing_runs)
    daily = daily if daily is not None else {"results": timedelta(hours=6),
                                             "resolve_pending": timedelta(hours=8)}

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "FROM pipeline_runs WHERE mode = 'closing' AND ran_at >=" in s:
            return pd.DataFrame({"ran_at": [now - d for d in closing_runs]})
        if "AS first_ever" in s:
            return pd.DataFrame([{"first_ever": now - first_ever, "last_run": now - last_run}])
        if "GREATEST(" in s:
            return pd.DataFrame([{"last_weekly": now - last_weekly}])
        if "WHERE mode IN ('results', 'resolve_pending')" in s:
            return pd.DataFrame([{"mode": m, "last_run": now - d} for m, d in daily.items()])
        if "AS last_update" in s:
            return pd.DataFrame([{"last_update": now - timedelta(hours=1)}])
        if "analyst_heartbeat" in s:
            return pd.DataFrame([{"last_ran": now - timedelta(minutes=10)}])
        if "COUNT(*) AS n FROM bets_history WHERE result = 'pending'" in s:
            return pd.DataFrame([{"n": stuck}])
        if "COUNT(*) AS n FROM upcoming_matches" in s:
            return pd.DataFrame([{"n": 50}])
        if "AS last_shadow FROM shadow_bets" in s:
            return pd.DataFrame([{"last_shadow": now - timedelta(hours=10)}])
        raise AssertionError(f"SQL inesperado en el watchdog: {s[:120]}")

    alerts = []
    monkeypatch.setattr(cdb, "engine", FakeEngine())
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(wd, "_send_alert", lambda m: alerts.append(m) or delivered)
    return wd, alerts


HEALTHY_CLOSING = [timedelta(minutes=30 * i) for i in range(1, 15)]


def test_watchdog_stays_silent_when_everything_runs(monkeypatch):
    wd, alerts = _fake_world(monkeypatch, HEALTHY_CLOSING, timedelta(days=3), timedelta(days=2))
    assert wd.run_watchdog() == 0
    assert alerts == []


def test_watchdog_warns_when_the_resolver_stops(monkeypatch):
    wd, alerts = _fake_world(monkeypatch, HEALTHY_CLOSING, timedelta(days=3), timedelta(days=2),
                             daily={"results": timedelta(hours=6)})     # resolvedor: nunca
    assert wd.run_watchdog() == 0
    (msg,) = alerts
    assert "RESOLVEDOR DE PENDIENTES SIN CORRER" in msg and "REINTENTO" not in msg


def test_watchdog_warns_about_bets_left_pending(monkeypatch):
    wd, alerts = _fake_world(monkeypatch, HEALTHY_CLOSING, timedelta(days=3), timedelta(days=2),
                             stuck=1)
    wd.run_watchdog()
    (msg,) = alerts
    assert "1 APUESTAS SIN LIQUIDAR" in msg and "4+ días" in msg


def test_watchdog_fails_the_run_when_its_alert_is_not_delivered(monkeypatch, capsys):
    """El watchdog es el que avisa: si Telegram no acepta su alerta, la corrida
    queda en rojo y GitHub avisa por correo (segundo canal)."""
    wd, alerts = _fake_world(monkeypatch, [], timedelta(days=3), timedelta(days=2),
                             last_run=timedelta(hours=5), delivered=False)
    assert wd.run_watchdog() == 1
    assert "::error::Watchdog" in capsys.readouterr().out


def test_watchdog_fails_the_run_when_the_db_is_down(monkeypatch):
    import config.database as cdb
    import scripts.watchdog as wd

    class Down:
        def connect(self):
            raise RuntimeError("password authentication failed")
    alerts = []
    monkeypatch.setattr(cdb, "engine", Down())
    monkeypatch.setattr(wd, "_send_alert", lambda m: alerts.append(m) or True)
    assert wd.run_watchdog() == 1
    assert "DB CAÍDA" in alerts[0]


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_send_alert_checks_telegram_answer(monkeypatch):
    import requests
    import scripts.watchdog as wd
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        return _Resp(400, "can't parse entities") if "parse_mode" in json else _Resp(200)
    monkeypatch.setattr(requests, "post", post)
    assert wd._send_alert("<b>roto") is True                     # 400 → reintento en texto plano
    assert "parse_mode" in sent[0] and "parse_mode" not in sent[1]

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(401, "Unauthorized"))
    assert wd._send_alert("x") is False


def test_send_alert_without_credentials_is_not_delivered(monkeypatch):
    import scripts.watchdog as wd
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert wd._send_alert("x") is False


def test_watchdog_warns_when_the_closing_dispatcher_stops(monkeypatch):
    wd, alerts = _fake_world(monkeypatch, [], timedelta(days=3), timedelta(days=2),
                             last_run=timedelta(hours=5))
    wd.run_watchdog()
    (msg,) = alerts
    assert "CLOSING SIN CORRER" in msg and "WEEKLY" not in msg


def test_watchdog_warns_when_the_weekly_stops(monkeypatch):
    wd, alerts = _fake_world(monkeypatch, [timedelta(minutes=30 * i) for i in range(1, 15)],
                             timedelta(days=3), timedelta(days=10))
    wd.run_watchdog()
    (msg,) = alerts
    assert "WEEKLY SIN CORRER" in msg and "CLOSING" not in msg
