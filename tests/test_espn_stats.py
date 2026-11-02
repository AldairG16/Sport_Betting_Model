"""
Tiros, tiros a puerta y córners de ESPN para las ligas que football-data no
publica (src/features/espn_stats.py, 2-nov-26). El dueño: "arranca con los
tiros y córners de ESPN". La MLS no tenía un tiro en 512 partidos del último
año y su 🎯 iba 8 de 23 (−10.03u). Sin red ni DB.
"""

import pandas as pd
import pytest

from src.features import espn_stats as ES
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine


def _ev(eid, home, away, hg, ag, shots=(21, 9), sot=(5, 3), corners=(12, 1), completed=True, date="2026-10-26T01:00Z"):
    def side(where, name, goals, sh, st, co):
        stats = [{"name": "totalShots", "displayValue": str(sh)}, {"name": "shotsOnTarget", "displayValue": str(st)}]
        if co is not None:
            stats.append({"name": "wonCorners", "displayValue": str(co)})
        return {"homeAway": where, "score": str(goals), "team": {"displayName": name}, "statistics": stats}
    comp = {"status": {"type": {"completed": completed}},
            "competitors": [side("home", home, hg, shots[0], sot[0], corners[0] if corners else None),
                            side("away", away, ag, shots[1], sot[1], corners[1] if corners else None)]}
    return {"id": eid, "date": date, "competitions": [comp]}


def _row(mid, h, a, hg, ag, d="2026-10-25", **stats):
    base = {f"{s}_{k}": None for s in ("home", "away") for k in ES.STATS}
    return {"id": mid, "date": pd.Timestamp(d), "h": h, "a": a, "home_goals": hg, "away_goals": ag, **base, **stats}


# ─── lectura ─────────────────────────────────────────────────────────────────

def test_only_finished_matches_with_shots_of_both_teams():
    data = {"events": [_ev("1", "LAFC", "LA Galaxy", 2, 2),
                       _ev("2", "Austin FC", "Dallas", 0, 0, completed=False),         # en juego
                       _ev("3", "Inter Miami", "Orlando City", 1, 0, shots=(0, 0), sot=(0, 0)),   # sin estadística
                       _ev("4", "Seattle", "Portland", 1, 1, shots=(4, 9), sot=(6, 3)),  # roto: más a puerta que tiros
                       _ev("5", "Columbus", "Toronto", 3, 1, corners=None)]}             # sin córners: va igual
    rows = ES.parse_stats(data)
    assert [r["espn_id"] for r in rows] == ["1", "5"]
    r = rows[0]
    assert (r["home_shots"], r["away_shots"], r["home_shots_target"], r["away_shots_target"]) == (21, 9, 5, 3)
    assert (r["home_corners"], r["away_corners"], r["home_goals"]) == (12, 1, 2)
    assert rows[1]["home_corners"] is None


def test_each_espn_match_finds_its_row_by_date_score_and_names():
    events = ES.parse_stats({"events": [_ev("1", "LAFC", "LA Galaxy", 2, 2),
                                        _ev("2", "New York Red Bulls", "Inter Miami CF", 1, 3)]})
    matches = [_row(10, "lafc", "la galaxy", 2, 2),                      # ESPN da la hora UTC: un día después
               _row(11, "ny red bulls", "inter miami", 1, 3),
               _row(12, "lafc", "la galaxy", 1, 0, d="2026-05-03")]      # otro marcador, otra fecha
    assert {mid: ev["espn_id"] for mid, ev in ES.pair_rows(events, matches)} == {10: "1", 11: "2"}


def test_two_rows_that_look_alike_are_not_guessed():
    events = ES.parse_stats({"events": [_ev("1", "Atlético Mineiro", "Santos", 1, 0)]})
    twins = [_row(1, "atletico mineiro", "santos", 1, 0), _row(2, "atletico mineiro", "santos", 1, 0)]
    assert ES.pair_rows(events, twins) == []
    assert ES.pair_rows(events, [_row(3, "flamengo", "palmeiras", 1, 0)]) == []      # nombres distintos


# ─── qué se escribe ──────────────────────────────────────────────────────────

def test_only_rows_without_shots_are_filled_and_existing_data_is_kept():
    ev = ES.parse_stats({"events": [_ev("1", "LAFC", "LA Galaxy", 2, 2)]})[0]
    rows = {1: _row(1, "lafc", "la galaxy", 2, 2),                                     # NULL: se llena
            2: _row(2, "x", "y", 2, 2, home_shots=0, away_shots=0, home_shots_target=0, away_shots_target=0,
                    home_corners=0, away_corners=0),                                   # puros ceros (Brasil, México)
            3: _row(3, "x", "y", 2, 2, home_shots=14, away_shots=8, home_shots_target=6, away_shots_target=2),
            4: _row(4, "x", "y", 2, 2, home_corners=7.0, away_corners=float("nan"))}   # córners ya: no se pisan
    params = {p["id"]: p for p in ES.fill_params([(1, ev), (2, ev), (3, ev), (4, ev)], rows)}
    assert set(params) == {1, 2, 4}                                                   # el 3 ya tenía tiros
    assert (params[1]["home_shots"], params[1]["home_corners"], params[2]["away_corners"]) == (21, 12, 1)
    assert params[4]["home_corners"] == 7 and params[4]["away_corners"] is None       # NaN → NULL, nunca NaN


def test_the_proxy_ratio_says_if_espn_shots_measure_like_football_data():
    evs = ES.parse_stats({"events": [_ev("1", "A", "B", 2, 1, shots=(14, 10), sot=(5, 4))]})
    from src.features.xg_proxy import SHOT_ON_TARGET_RATE, SHOT_RATE
    xg = (5 + 4) * SHOT_ON_TARGET_RATE + (9 + 6) * SHOT_RATE
    assert ES.proxy_ratio(evs) == pytest.approx(xg / 3)
    assert ES.proxy_ratio([]) is None


class _Get:
    def __init__(self, data):
        self.data, self.urls = data, []

    def __call__(self, url, headers=None, timeout=None):
        self.urls.append(url)
        data = self.data(url)
        return type("R", (), {"status_code": 200 if data is not None else 503, "json": lambda s: data})()


def test_the_load_backs_up_and_fills_only_the_matched_rows(monkeypatch):
    monkeypatch.setattr(ES, "_slugs", lambda: {"soccer_usa_mls": "usa.1"})
    get = _Get(lambda url: {"events": [_ev("1", "LAFC", "LA Galaxy", 2, 2)]})
    monkeypatch.setattr(ES.pd, "read_sql", lambda *a, **k: pd.DataFrame([_row(10, "lafc", "la galaxy", 2, 2)]))
    eng = FakeEngine()
    out = ES.load_espn_stats(eng, months=2, get=get, pause=0, today=pd.Timestamp("2026-11-02"), verbose=False)
    assert out["filled"] == 1 and out["reads"] == 2
    assert [u.split("dates=")[1][:6] for u in get.urls] == ["202611", "202610"]
    (sql, p), = eng.statements("INSERT INTO matches_identity_backup")
    assert p["ids"] == [10] and p["action"] == "espn_stats"                           # copia antes de cambiar
    (sql, params), = eng.statements("UPDATE matches SET")
    assert params[0]["id"] == 10 and params[0]["home_shots_target"] == 5 and params[0]["away_corners"] == 1
    dry = FakeEngine()
    assert ES.load_espn_stats(dry, months=1, apply=False, get=get, pause=0, verbose=False)["filled"] == 1
    assert dry.executed == []                                                         # en seco no escribe


def test_without_espn_it_is_an_error_and_unpaired_leagues_warn(monkeypatch):
    RUN_ISSUES.reset()
    monkeypatch.setattr(ES, "_slugs", lambda: {"soccer_usa_mls": "usa.1"})
    out = ES.load_espn_stats(FakeEngine(), months=1, get=_Get(lambda url: None), pause=0, verbose=False)
    assert out["filled"] == 0 and any("Tiros y córners (ESPN)" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()
    many = {"events": [_ev(str(i), f"Team {i}", f"Other {i}", 1, 0) for i in range(ES.MIN_EVENTS_TO_JUDGE)]}
    monkeypatch.setattr(ES.pd, "read_sql", lambda *a, **k: pd.DataFrame([_row(1, "lafc", "la galaxy", 1, 0)]))
    ES.load_espn_stats(FakeEngine(), months=1, get=_Get(lambda url: many), pause=0, verbose=False)
    assert any("ninguno emparejado" in w for w in RUN_ISSUES.warnings())             # ¿cambiaron los nombres?
    RUN_ISSUES.reset()


# ─── el xG de esas ligas, calibrado con sus goles ────────────────────────────

def _xg_world(monkeypatch, rows, factors):
    import src.features.xg_proxy as xp
    monkeypatch.setattr(xp.pd, "read_sql", lambda *a, **k: pd.DataFrame(rows))
    monkeypatch.setattr(xp, "espn_league_factors", lambda: factors)
    return xp


def _m(league, hsot=6, hsh=16, asot=3, ash=9, home="boca juniors", away="river plate"):
    return {"home_team": home, "away_team": away, "league": league, "home_shots": hsh, "away_shots": ash,
            "home_shots_target": hsot, "away_shots_target": asot, "date": pd.Timestamp("2026-10-01")}


def test_the_xg_of_an_espn_league_is_divided_by_its_league_ratio(monkeypatch):
    # Argentina: el xG estimado daba 1.31 veces los goles reales
    lg = "soccer_argentina_primera_division"
    raw = _xg_world(monkeypatch, [_m("soccer_epl")] * 4, {}).get_team_xg("boca juniors")
    xp = _xg_world(monkeypatch, [_m(lg)] * 4, {lg: 1.31})
    xg = xp.get_team_xg("boca juniors")
    assert xg["xg_for"] == pytest.approx(raw["xg_for"] / 1.31, abs=1e-3)
    assert xg["xg_against"] == pytest.approx(raw["xg_against"] / 1.31, abs=1e-3)
    # sin su relación (poca historia o la consulta falló), sus tiros no entran
    assert _xg_world(monkeypatch, [_m(lg)] * 4, {}).get_team_xg("boca juniors") is None
    # las ligas de football-data no cambian
    assert _xg_world(monkeypatch, [_m("soccer_epl")] * 4, {lg: 1.31}).get_team_xg("boca juniors") == raw


def test_the_league_ratio_needs_enough_matches(monkeypatch):
    import src.features.xg_proxy as xp
    monkeypatch.setattr(xp.pd, "read_sql", lambda *a, **k: pd.DataFrame([
        {"league": "soccer_usa_mls", "n": 400, "xg": 1020.0, "goals": 1000},
        {"league": "soccer_china_superleague", "n": xp.MIN_FACTOR_MATCHES - 1, "xg": 50.0, "goals": 40}]))
    xp.espn_league_factors.cache_clear()
    assert xp.espn_league_factors() == {"soccer_usa_mls": pytest.approx(1.02)}
    xp.espn_league_factors.cache_clear()


# ─── el chequeo diario y el lugar del paso ───────────────────────────────────

def test_the_daily_check_says_when_a_league_is_left_without_shots():
    from src.utils.output_checks import ESPN_SHOTS_MIN_ROWS, espn_shots_gaps
    rows = [{"league": "soccer_usa_mls", "n": 40, "with_shots": 39},
            {"league": "soccer_argentina_primera_division", "n": 30, "with_shots": 4},
            {"league": "soccer_china_superleague", "n": ESPN_SHOTS_MIN_ROWS - 1, "with_shots": 0}]   # pocos: sin juicio
    (msg,) = espn_shots_gaps(rows)
    assert "soccer_argentina_primera_division con tiros en 4 de 30" in msg


def test_the_evening_fills_shots_before_settling(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_evening")
    assert names.index("Tiros y córners (ESPN)") == names.index("Córners y tarjetas (ESPN)") + 1
    assert names.index("Tiros y córners (ESPN)") < names.index("Results")
    assert steps["Tiros y córners (ESPN)"] is O.step_espn_stats


def test_the_weekly_xg_check_leaves_out_the_espn_leagues(monkeypatch):
    import scripts.weekly_sanity_audit as sa
    seen = []

    def read_sql(sql, con=None, params=None, **kw):
        seen.append((str(sql), params))
        return pd.DataFrame([{"hsot": 4.5, "asot": 3.8, "hsh": 13, "ash": 11, "hg": 1.5, "ag": 1.2, "n": 900}])
    monkeypatch.setattr(sa.pd, "read_sql", read_sql)
    sa.audit_xg_sanity()
    sql, params = seen[0]
    assert "NOT (league = ANY(:espn))" in sql and set(params["espn"]) == set(ES.STATS_LEAGUES)
