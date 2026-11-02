"""
Marcadores de respaldo de TheSportsDB (scripts/fetch_results_tsdb.py, 2-nov-26).

The Odds API /scores no devolvió la jornada 34 de la K-League (31-oct y
1-nov) y sus 6 picks se quedaban sin liquidar; ni ESPN ni football-data
tienen esa liga. TheSportsDB tenía la jornada completa.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.fetch_results_tsdb as T

KL = "soccer_korea_kleague1"


def _ev(home, away, hg, ag, ts="2026-11-01T05:00:00", status="FT", rnd="34", season="2026"):
    return {"strHomeTeam": home, "strAwayTeam": away, "intHomeScore": hg, "intAwayScore": ag,
            "strTimestamp": ts, "dateEvent": ts[:10], "strStatus": status, "intRound": rnd, "strSeason": season}


ROUND_34 = [_ev("Jeonbuk Hyundai Motors", "Daejeon Hana Citizen", "0", "0", "2026-10-31T05:00:00"),
            _ev("Gwangju FC", "Gimcheon Sangmu", "1", "1", "2026-10-31T07:30:00"),
            _ev("Pohang Steelers", "Jeju SK", "1", "1"),
            _ev("Ulsan HD", "Gangwon FC", "2", "1"),
            _ev("FC Anyang", "Bucheon FC 1995", "2", "1", "2026-11-01T07:30:00"),
            _ev("FC Seoul", "Incheon United", "0", "1", "2026-11-01T07:30:00")]

MISSING = [{"match": "jeonbuk hyundai motors vs daejeon citizen", "match_date": "2026-10-31 05:00+00:00", "league": KL},
           {"match": "gwangju fc vs sangju sangmu fc", "match_date": "2026-10-31 07:30+00:00", "league": KL},
           {"match": "pohang steelers vs jeju united fc", "match_date": "2026-11-01 05:00+00:00", "league": KL},
           {"match": "ulsan hyundai fc vs gangwon fc", "match_date": "2026-11-01 05:00+00:00", "league": KL},
           {"match": "fc anyang vs bucheon fc 1995", "match_date": "2026-11-01 07:30+00:00", "league": KL},
           {"match": "fc seoul vs incheon united", "match_date": "2026-11-01 07:30+00:00", "league": KL}]


def test_thesportsdb_names_reach_the_names_of_the_odds_feed():
    rows = T.finished_rows(ROUND_34, KL)
    assert [f"{r['home_team']} vs {r['away_team']}" for r in rows] == [m["match"] for m in MISSING]
    assert rows[0] == {"date": "2026-10-31", "home_team": "jeonbuk hyundai motors", "away_team": "daejeon citizen",
                       "home_goals": 0, "away_goals": 0, "league": KL}


def test_only_finished_matches_with_a_score_count():
    evs = [_ev("FC Seoul", "Ulsan HD", None, None, status="NS"),
           _ev("FC Seoul", "Ulsan HD", "1", "0", status="1H"),
           _ev("FC Seoul", "Ulsan HD", "", "", status="FT"),
           _ev("FC Seoul", "Ulsan HD", "2", "0", status="AET")]
    assert [(r["home_goals"], r["away_goals"]) for r in T.finished_rows(evs, KL)] == [(2, 0)]


def test_round_34_pairs_with_the_six_picks_left_without_a_score():
    found, left = T.pair_with_missing(T.finished_rows(ROUND_34, KL), MISSING)
    assert len(found) == 6 and left == []
    found, left = T.pair_with_missing(T.finished_rows(ROUND_34[:5], KL), MISSING)
    assert len(found) == 5 and [m["match"] for m in left] == ["fc seoul vs incheon united"]
    late = [{**MISSING[0], "match_date": "2026-11-03 05:00+00:00"}]                 # a 3 días: otro partido
    assert T.pair_with_missing(T.finished_rows(ROUND_34, KL), late) == ([], late)


def test_history_skips_what_matches_already_has_a_day_apart():
    rows = T.finished_rows(ROUND_34, KL)
    have = [{"h": "jeonbuk hyundai motors", "a": "daejeon citizen", "d": "2026-11-01"},   # a 1 día: el mismo
            {"h": "fc seoul", "a": "incheon united", "d": "2026-05-01"}]                 # otro partido
    add = T.new_rows(rows + rows[:1], have)
    assert len(add) == 5 and add[0]["home_team"] == "gwangju fc"


def test_the_daily_backup_asks_the_round_of_each_missing_date(monkeypatch, capsys):
    import scripts.fetch_results as FR
    calls, saved = [], []

    def get(endpoint, **p):
        calls.append((endpoint, p))
        if endpoint == "eventsday":                                      # la clave libre: hasta 3 por día
            return [e for e in ROUND_34 if e["dateEvent"] == p["d"]][:3]
        return ROUND_34 if (p["r"], p["s"]) == ("34", "2026") else []
    monkeypatch.setattr(T, "_get", get)
    monkeypatch.setattr(T, "_missing", lambda days: MISSING)
    monkeypatch.setattr(FR, "insert_results", lambda rows: saved.extend(rows) or (len(rows), 0))
    assert T.fetch_tsdb_backup() == 6
    assert [c[0] for c in calls] == ["eventsday", "eventsday", "eventsround"]
    assert {f"{r['home_team']} {r['home_goals']}-{r['away_goals']} {r['away_team']}" for r in saved} >= {
        "fc seoul 0-1 incheon united", "ulsan hyundai fc 2-1 gangwon fc"}
    assert "6 marcadores nuevos" in capsys.readouterr().out

    calls.clear()
    monkeypatch.setattr(T, "_missing", lambda days: [])
    assert T.fetch_tsdb_backup() == 0 and calls == []                    # nada pendiente: ninguna llamada


def test_a_missing_match_without_a_pair_is_named(monkeypatch):
    import scripts.fetch_results as FR
    from src.utils.log import RUN_ISSUES
    monkeypatch.setattr(T, "_get", lambda endpoint, **p: ROUND_34[:1] if endpoint == "eventsday" else ROUND_34[:5])
    monkeypatch.setattr(T, "_missing", lambda days: MISSING)
    monkeypatch.setattr(FR, "insert_results", lambda rows: (len(rows), 0))
    RUN_ISSUES.reset()
    assert T.fetch_tsdb_backup() == 5
    assert any("1 partido(s) sin pareja (fc seoul vs incheon united)" in w for w in RUN_ISSUES.warnings())
    RUN_ISSUES.reset()

    # aplazado en TheSportsDB: no es un alias que falte, no avisa
    postponed = ROUND_34[:5] + [_ev("FC Seoul", "Incheon United", None, None, status="Postponed")]
    monkeypatch.setattr(T, "_get", lambda endpoint, **p: ROUND_34[:1] if endpoint == "eventsday" else postponed)
    assert T.fetch_tsdb_backup() == 5
    assert not any("sin pareja" in w for w in RUN_ISSUES.warnings())


class _R:
    def __init__(self, status, headers=None, events=None):
        self.status_code, self.headers, self._events = status, headers or {}, events

    def raise_for_status(self):
        if self.status_code >= 400:
            raise T.requests.HTTPError(str(self.status_code))

    def json(self):
        return {"events": self._events}


def test_a_429_waits_what_retry_after_says():
    """Medido el 2-nov: ~10 llamadas seguidas y luego 429 con Retry-After de 120 s."""
    waits, answers = [], [_R(429, {"retry-after": "120"}), _R(200, events=[{"x": 1}])]
    got = T._get("eventsround", sleep=waits.append, get=lambda url, params=None, timeout=None: answers.pop(0), id="4689")
    assert got == [{"x": 1}] and waits == [T.TSDB_PAUSE_S, 121, T.TSDB_PAUSE_S]
    waits, answers = [], [_R(503)] * 4
    try:
        T._get("eventsround", sleep=waits.append, get=lambda url, params=None, timeout=None: answers.pop(0))
        raise AssertionError("debía fallar")
    except T.requests.HTTPError:
        pass
    assert waits == [T.TSDB_PAUSE_S, 20, T.TSDB_PAUSE_S, 40, T.TSDB_PAUSE_S, 60, T.TSDB_PAUSE_S]


def test_the_weekly_completes_the_rounds_of_the_last_week(monkeypatch):
    calls, saved = [], {}

    def get(endpoint, **p):
        calls.append(endpoint)
        if endpoint == "eventsday":                                      # cualquier día de la semana: jornada 34
            return ROUND_34[:1]
        return ROUND_34
    monkeypatch.setattr(T, "_get", get)
    monkeypatch.setattr(T, "_save", lambda sk, rows, apply, what, verbose: saved.update(rows=rows, what=what))
    T.load_recent(days=T.RECENT_DAYS)
    assert calls.count("eventsday") == T.RECENT_DAYS and calls.count("eventsround") == 1   # una vez por jornada
    assert len(saved["rows"]) == 6 and saved["what"] == "jornadas 34"


def test_the_evening_and_the_results_run_ask_thesportsdb_before_settling(monkeypatch):
    from tests.test_program_picks import _steps
    for mode in ("run_evening", "run_results_only"):
        names, steps, O = _steps(monkeypatch, mode)
        assert names.index("Fetch results (fbdata backup)") < names.index("Fetch results (TheSportsDB)") \
            < names.index("Results")
    names, steps, O = _steps(monkeypatch, "run_weekly")
    assert steps["Historial K-League (TheSportsDB)"] is O.step_tsdb_history
    assert names.index("Historial K-League (TheSportsDB)") < names.index("Fit DC-MLE parameters")


def test_a_thesportsdb_outage_is_a_warning_not_a_failed_step(monkeypatch):
    import scripts.orchestrator as O
    from src.utils.log import RUN_ISSUES

    def down(days=10):
        raise ConnectionError("sin red")
    monkeypatch.setattr(T, "fetch_tsdb_backup", down)
    RUN_ISSUES.reset()
    O.step_fetch_results_tsdb()
    assert any("TheSportsDB sin respuesta" in w for w in RUN_ISSUES.warnings())
    RUN_ISSUES.reset()
