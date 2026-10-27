"""
Partidos reprogramados (27-oct-26): la fecha vieja se llevaba el resultado
del partido real y el mismo partido contaba dos o tres veces en los picks,
la sombra y el sesgo aprendido.
"""

import contextlib

import pandas as pd

from src.models import rescheduled as R

PSG = "paris saint germain vs lyon"
AME = "america vs pachuca"


def _r(match, date, written, table="program_picks"):
    return {"match": match, "match_date": pd.Timestamp(date, tz="UTC"),
            "written": pd.Timestamp(written, tz="UTC"), "table": table}


def test_the_old_date_of_a_rescheduled_match_is_a_phantom():
    rows = [_r(PSG, "2026-10-25 19:45", "2026-10-25 19:32"),
            _r(PSG, "2026-10-26 19:30", "2026-10-26 19:02"),
            _r(PSG, "2026-10-25 19:45", "2026-10-23 03:10", "shadow_bets"),
            _r(PSG, "2026-10-26 19:30", "2026-10-25 03:17", "shadow_bets")]
    assert R.phantom_dates(rows) == {(PSG, pd.Timestamp("2026-10-25 19:45", tz="UTC"))}


def test_espn_decides_when_the_feed_listed_the_match_twice_at_once():
    # la misma foto (00:33) traía la 01:00 y la 01:05; se jugó a la 01:05
    rows = [_r(AME, "2026-10-25 23:00", "2026-10-25 22:31"),
            _r(AME, "2026-10-26 01:00", "2026-10-26 00:33"),
            _r(AME, "2026-10-26 01:05", "2026-10-26 00:33")]
    espn = {AME: [pd.Timestamp("2026-10-26 01:05", tz="UTC")]}
    assert R.phantom_dates(rows, espn) == {(AME, pd.Timestamp("2026-10-25 23:00", tz="UTC")),
                                           (AME, pd.Timestamp("2026-10-26 01:00", tz="UTC"))}
    # sin ESPN, en empate gana la fecha más tardía
    assert (AME, pd.Timestamp("2026-10-26 01:05", tz="UTC")) not in R.phantom_dates(rows)


def test_another_fixture_of_the_same_teams_and_a_single_date_are_left_alone():
    rows = [_r(PSG, "2026-08-20 19:00", "2026-08-19 12:00"),       # la primera vuelta, meses antes
            _r(PSG, "2026-10-26 19:30", "2026-10-26 19:02"),
            _r(AME, "2026-10-26 01:05", "2026-10-26 00:33")]
    assert R.phantom_dates(rows) == set()


class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((str(sql), params))
        return type("Res", (), {"rowcount": 1})()


class _Engine:
    def __init__(self):
        self.log = []

    @contextlib.contextmanager
    def begin(self):
        yield _Conn(self.log)


def test_void_marks_the_phantom_in_every_table_and_leaves_bets_alone(monkeypatch, capsys):
    tables = {
        "program_picks": [_r(PSG, "2026-10-25 19:45", "2026-10-25 19:32"), _r(PSG, "2026-10-26 19:30", "2026-10-26 19:02")],
        "shadow_bets": [_r(PSG, "2026-10-25 19:45", "2026-10-23 03:10"), _r(PSG, "2026-10-26 19:30", "2026-10-25 03:17")],
        "model_vs_sharp": [_r(PSG, "2026-10-25 19:45", "2026-10-23 12:06"), _r(PSG, "2026-10-26 19:30", "2026-10-26 12:07")],
    }

    def fake_read_sql(sql, engine, params=None):
        s = str(sql)
        if "FROM live_scores" in s:
            return pd.DataFrame([{"home": "paris saint germain", "away": "lyon",
                                  "kickoff": pd.Timestamp("2026-10-26 19:30", tz="UTC")}])
        for t, rows in tables.items():
            if f"FROM {t} " in s:
                return pd.DataFrame(rows)[["match", "match_date", "written"]]
        raise RuntimeError(f'relation "{s.split("FROM ")[1].split()[0]}" does not exist')
    monkeypatch.setattr(R.pd, "read_sql", fake_read_sql)
    eng = _Engine()
    out = R.void_rescheduled(engine=eng)
    assert out["matches"] == [PSG]
    assert out["program_picks"] == out["shadow_bets"] == out["model_vs_sharp"] == 1
    assert "model_proposals" not in out                       # la tabla no existe todavía: no cuenta
    sqls = [s for s, _ in eng.log]
    assert not any("bets_history" in s for s in sqls)
    assert any("SET result = 'stale'" in s and "program_picks" in s for s in sqls)
    assert any("SET skipped_at = NOW()" in s for s in sqls)
    assert all(p["match_date"] == pd.Timestamp("2026-10-25 19:45", tz="UTC") for _, p in eng.log)
    assert "Partidos reprogramados: 1" in capsys.readouterr().out


def test_the_evening_voids_phantoms_before_settling_and_a_failure_is_an_error(monkeypatch):
    from scripts import orchestrator as O
    from src.models import save_bets, proposals, program_picks
    from src.utils.log import RUN_ISSUES
    order = []
    monkeypatch.setattr(R, "void_rescheduled", lambda: order.append("void"))
    monkeypatch.setattr(save_bets, "resolve_shadow_outcomes", lambda: order.append("shadow"))
    monkeypatch.setattr(proposals, "resolve_proposals", lambda: order.append("proposals"))
    monkeypatch.setattr(program_picks, "resolve_picks", lambda: order.append("picks"))
    O.step_shadow_results()
    assert order == ["void", "shadow", "proposals", "picks"]

    def boom():
        raise RuntimeError("sin conexión")
    monkeypatch.setattr(R, "void_rescheduled", boom)
    order.clear()
    RUN_ISSUES.reset()
    O.step_shadow_results()
    assert order == ["shadow", "proposals", "picks"]                   # la liquidación sigue
    assert any("Partidos reprogramados" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


def test_the_daily_check_flags_a_match_counted_on_two_dates():
    from src.utils.output_checks import counted_twice
    rows = [{"table": "program_picks", "match": PSG, "match_date": "2026-10-25 19:45+00:00"},
            {"table": "program_picks", "match": PSG, "match_date": "2026-10-26 19:30+00:00"},
            {"table": "bets_history", "match": AME, "match_date": "2026-10-25 23:00"},    # dos apuestas son dos
            {"table": "bets_history", "match": AME, "match_date": "2026-10-26 01:05"},
            {"table": "shadow_bets", "match": AME, "match_date": "2026-10-26 01:05+00:00"},
            {"table": "model_vs_sharp", "match": PSG, "match_date": "2026-10-26 19:30+00:00"}]
    out = counted_twice(rows)
    assert out == ["program_picks: paris saint germain vs lyon (25-Oct 19:45, 26-Oct 19:30)"]


def test_rebuilt_past_picks_skip_the_analyses_that_no_longer_count(monkeypatch):
    from src.models import program_picks as PK
    seen = []

    def fake_read_sql(sql, engine, params=None):
        seen.append(" ".join(str(sql).split()))
        return pd.DataFrame()
    monkeypatch.setattr(PK.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(PK, "build_past_picks", lambda *a, **k: [])
    assert PK.backfill_picks(engine=object()) == 0
    assert any("FROM model_vs_sharp" in s and "skipped_at IS NULL" in s for s in seen)
