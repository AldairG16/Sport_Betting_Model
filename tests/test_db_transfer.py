"""
tests/test_db_transfer.py
=========================
Transferencia de la base (10-nov-26). El 9-nov Neon cortó la base: el plan
gratis trae 5 GB de transferencia al mes y el programa bajó 5.58 GB en 9 días
de una base de 236 MB. Lo que se releía sin necesidad, y cómo se mide ahora.
"""

import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.utils import db_meter


# ── El medidor ───────────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, rows, cols):
        self.rowcount = rows
        self.description = [(f"c{i}",) for i in range(cols)] if cols else None


def test_the_meter_counts_rows_and_cells_of_each_read():
    db_meter.reset()
    db_meter._after_execute(None, _Cur(100, 4), "SELECT a, b, c, d\n  FROM lineups", None, None, False)
    db_meter._after_execute(None, _Cur(100, 4), "SELECT a, b, c, d FROM lineups", None, None, False)
    db_meter._after_execute(None, _Cur(3, 1), "SELECT 1", None, None, False)
    db_meter._after_execute(None, _Cur(1, 0), "UPDATE x SET y = 1", None, None, False)   # no es lectura
    db_meter._after_execute(None, _Cur(-1, 3), "SELECT * FROM t", None, None, False)      # SQLite: no sabe
    assert db_meter.totals() == {"queries": 3, "rows": 203, "cells": 803}
    (k, times, rows, cells), _ = db_meter.top(2)
    assert k == "SELECT a, b, c, d FROM lineups" and (times, rows, cells) == (2, 200, 800)
    lines = db_meter.summary_lines()
    assert lines[0].startswith("🗄️  Lecturas de la base: 203 filas · 803 celdas en 3 consultas")
    db_meter.reset()
    assert db_meter.summary_lines() == []


def test_measuring_never_breaks_a_query():
    class Broken:
        @property
        def description(self):
            raise RuntimeError("cursor cerrado")
    db_meter._after_execute(None, Broken(), "SELECT 1", None, None, False)


def test_the_pipeline_engine_is_measured():
    from config.database import engine
    assert id(engine) in db_meter._INSTALLED


# ── Titulares habituales: solo los 15 partidos que se usan ───────────────────

def test_profiles_read_only_the_last_15_matches_of_each_team():
    from src.models import lineup_alerts as LA
    assert "ROW_NUMBER() OVER (PARTITION BY team ORDER BY kickoff DESC)" in LA.PROFILES_SQL
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE lineups (team TEXT, kickoff TEXT, starters TEXT, goals TEXT, names TEXT, completed INT)")
    for i in range(40):
        con.execute("INSERT INTO lineups VALUES ('alpha', ?, ?, '{}', '{}', 1)", (f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", f"[{i}]"))
    for i in range(5):
        con.execute("INSERT INTO lineups VALUES ('beta', ?, '[]', '{}', '{}', 1)", (f"2026-05-{i + 1:02d}",))
    con.execute("INSERT INTO lineups VALUES ('alpha', '2026-12-01', '[99]', '{}', '{}', 0)")   # sin terminar
    sql = LA.PROFILES_SQL.replace("team = ANY(:t)", "team IN ('alpha', 'beta')").replace(":n", str(LA.HISTORY_MATCHES))
    rows = con.execute(sql).fetchall()
    alpha = [r[1] for r in rows if r[0] == "alpha"]
    assert len(alpha) == 15 and alpha[0] == "[39]" and alpha[-1] == "[25]"      # los más recientes, en orden
    assert sum(r[0] == "beta" for r in rows) == 5


def test_profiles_pass_the_window_to_the_query():
    from src.models import lineup_alerts as LA
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    LA.load_profiles(eng, ["alpha"])
    (sql, params), = [x for x in eng.executed if "FROM lineups" in x[0]]
    assert params == {"t": ["alpha"], "n": LA.HISTORY_MATCHES}


def test_the_dashboard_keeps_the_profiles_half_an_hour(monkeypatch):
    from dashboard import app as A
    calls = []

    def load(engine, teams):
        calls.append(tuple(teams))
        return {t: {"n": 15} for t in teams}
    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    assert A._cached_profiles(load, ["a", "b"]) == {"a": {"n": 15}, "b": {"n": 15}}
    clock[0] += 300                                    # la siguiente recarga, 5 min después
    A._cached_profiles(load, ["a", "b"])
    assert calls == [("a", "b")]
    A._cached_profiles(load, ["a", "c"])               # cambian los partidos de hoy
    clock[0] += A.PROFILES_TTL_S + 1
    A._cached_profiles(load, ["a", "c"])
    assert calls == [("a", "b"), ("a", "c"), ("a", "c")]


# ── Córners y tarjetas: solo la historia de los partidos por venir ───────────

def _hist(today):
    rng = np.random.default_rng(7)
    rows = []
    teams = {"liga_a": ["a1", "a2", "a3", "a4"], "liga_b": ["b1", "b2", "b3", "b4"], "liga_c": ["c1", "c2"]}
    for d in range(1, 880, 3):
        for lg, ts in teams.items():
            h, a = rng.choice(ts, 2, replace=False)
            rows.append({"date": today - pd.Timedelta(days=d), "league": lg, "home": h, "away": a,
                         "home_corners": float(rng.integers(2, 9)), "away_corners": float(rng.integers(1, 7)),
                         "home_cards": float(rng.integers(0, 4)), "away_cards": float(rng.integers(0, 4))})
    # un equipo que también jugó en otra liga (copa) hace mucho
    rows.append({"date": today - pd.Timedelta(days=500), "league": "liga_c", "home": "a1", "away": "c1",
                 "home_corners": 12.0, "away_corners": 1.0, "home_cards": 5.0, "away_cards": 0.0})
    return pd.DataFrame(rows).sort_values("date", ascending=False).reset_index(drop=True)


def test_the_scoped_history_gives_the_same_corners_and_cards():
    from src.models import props_fair as PF
    today = pd.Timestamp(datetime.now(timezone.utc).date())
    full = _hist(today)
    leagues, teams = ["liga_a"], ["a1", "a2"]
    # lo mismo que filtra la consulta: el año de esas ligas y todo lo de esos equipos
    keep = (((full["league"].isin(leagues)) & (full["date"] >= today - pd.Timedelta(days=PF.LEAGUE_DAYS + 1)))
            | full["home"].isin(teams) | full["away"].isin(teams))
    scoped = full[keep].reset_index(drop=True)
    assert len(scoped) < len(full) / 2
    for kick in (today + pd.Timedelta(hours=20), today + pd.Timedelta(days=3)):
        assert PF.match_props(scoped, "a1", "a2", "liga_a", kick) == PF.match_props(full, "a1", "a2", "liga_a", kick)


def test_the_history_query_takes_the_scope(monkeypatch):
    from src.models import props_fair as PF
    seen = []

    def read_sql(sql, con=None, params=None):
        seen.append((" ".join(str(sql).split()), params))
        return pd.DataFrame(columns=["date", "league", "home", "away", "home_corners", "away_corners",
                                     "home_cards", "away_cards"])
    monkeypatch.setattr(PF.pd, "read_sql", read_sql)
    PF.load_history(engine=object(), leagues=["soccer_epl"], teams=["Arsenal", "chelsea"])
    sql, params = seen[-1]
    assert f"(league = ANY(:leagues) AND date >= CURRENT_DATE - {PF.LEAGUE_DAYS + 1})" in sql
    assert "(LOWER(home_team) = ANY(:teams) OR LOWER(away_team) = ANY(:teams))" in sql
    assert params == {"leagues": ["soccer_epl"], "teams": ["arsenal", "chelsea"]}
    PF.load_history(engine=object())                                   # sin alcance: todo, como antes
    assert "ANY(" not in seen[-1][0] and seen[-1][1] is None
    PF.load_history(engine=object(), leagues=[], teams=[])             # nada que buscar
    assert seen[-1][0].endswith("AND FALSE")


def test_picks_ask_only_for_the_history_of_their_matches():
    from src.models.program_picks import props_scope
    mvs = pd.DataFrame([{"match": "Arsenal vs Chelsea", "league": "soccer_epl"},
                        {"match": "Boca vs River", "league": None},
                        {"match": "Lyon vs PSG", "league": np.nan}])
    norm = lambda n: {"psg": "paris saint germain"}.get(n.lower(), n.lower())
    leagues, teams = props_scope(mvs, norm)
    assert leagues == ["soccer_epl"]
    assert teams == ["arsenal", "boca", "chelsea", "lyon", "paris saint germain", "river"]
    assert props_scope(pd.DataFrame(), norm) == ([], [])


# ── El chequeo diario ────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def _runs(today_cells, before_cells, n=10):
    out = [{"ran_at": NOW - pd.Timedelta(hours=1 + i * 2), "cells_read": today_cells} for i in range(n)]
    out += [{"ran_at": NOW - pd.Timedelta(days=2 + i * 0.5), "cells_read": before_cells} for i in range(n)]
    return out


def test_a_closing_that_suddenly_reads_much_more_is_flagged():
    from src.utils.output_checks import reads_jump
    msg, = reads_jump(_runs(900_000, 300_000), NOW)
    assert "3.0 veces más" in msg and "900,000 celdas contra 300,000" in msg
    assert reads_jump(_runs(400_000, 300_000), NOW) == []            # sube poco
    assert reads_jump(_runs(40_000, 1_000), NOW) == []                # mucho, pero no gasta nada
    assert reads_jump(_runs(900_000, 300_000, n=3), NOW) == []         # pocas corridas para comparar
    assert reads_jump([{"ran_at": NOW, "cells_read": None}], NOW) == []

