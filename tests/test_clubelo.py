"""
La fuerza de cada liga con ClubElo (22-oct-26).

El dueño: "si, ve con clubelo". En Champions y Europa League el programa
comparaba goles de ligas distintas como si valieran lo mismo (Viking 20%
contra el Aston Villa, Pinnacle 10%). Ahora los goles esperados se reparten
por la diferencia de fuerza de las dos ligas según ClubElo, amortiguada, y
el pick de esos partidos solo sale si el análisis se hizo con ese ajuste.
Sin DB ni red.
"""

from collections import Counter
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from src.features import clubelo as CE
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine


def _club_row(country, slug, club, elo, rank=1):
    return (f"['<td class=\"l\"><a href=\"/{country}\"><img src=\"x.png\" alt=\"{country}\"  style=\"w\"></a> "
            f"<small> {rank} </small><a href=\"/{slug}\">{club}<span class=\"min481\"></span></a></td>', "
            f"'{elo}', '-0.49', '1.83']")


def _page(*rows):
    return "<html><script>const eloData = [" + ", ".join(rows) + "];</script></html>"


NOR = _page(_club_row("NOR", "BodoeGlimt", "Bodø/Glimt", 1785), _club_row("NOR", "Viking", "Viking", 1706, 2),
            _club_row("SWE", "Malmoe", "Malmö", 1650, 3))

CLUBS = [{"country": "ENG", "club": "Arsenal", "slug": "Arsenal", "elo": 2049},
         {"country": "ENG", "club": "Man City", "slug": "ManCity", "elo": 2000},
         {"country": "ENG", "club": "Man United", "slug": "ManUnited", "elo": 1890},
         {"country": "ENG", "club": "Aston Villa", "slug": "AstonVilla", "elo": 1880},
         {"country": "ITA", "club": "Inter", "slug": "Inter", "elo": 1950},
         {"country": "ITA", "club": "Milan", "slug": "Milan", "elo": 1850},
         {"country": "ITA", "club": "Como", "slug": "Como", "elo": 1780},
         {"country": "NOR", "club": "Bodø/Glimt", "slug": "BodoeGlimt", "elo": 1785},
         {"country": "NOR", "club": "Viking", "slug": "Viking", "elo": 1706},
         {"country": "NOR", "club": "Brann", "slug": "Brann", "elo": 1650},
         {"country": "UKR", "club": "Shakhtar", "slug": "Shakhtar", "elo": 1700}]


# ─── leer ClubElo ────────────────────────────────────────────────────────────

def test_a_country_page_gives_its_clubs_and_their_elo():
    assert CE.parse_country(NOR, "NOR") == [
        {"country": "NOR", "club": "Bodø/Glimt", "slug": "BodoeGlimt", "elo": 1785.0},
        {"country": "NOR", "club": "Viking", "slug": "Viking", "elo": 1706.0}]     # el sueco no es de esta página
    assert CE.parse_country("<html>mantenimiento</html>", "NOR") == []


@pytest.mark.parametrize("ours,slug", [
    ("aston villa", "AstonVilla"),          # mismas palabras
    ("viking fk", "Viking"),                # "fk" no cuenta
    ("manchester city", "ManCity"),         # "man" = manchester
    ("inter milan", "Inter"),               # tiene "milan": va por alias
    ("bod/glimt", "BodoeGlimt"),            # perdió la ø: alias
    ("shakhtar donetsk", "Shakhtar"),       # un único club contenido
])
def test_our_team_is_the_right_clubelo_club(ours, slug):
    assert CE.match_team(ours, CLUBS)["slug"] == slug


def test_without_a_single_candidate_there_is_no_club():
    assert CE.match_team("manchester", CLUBS) is None          # City o United: mejor no ajustar
    assert CE.match_team("racing club", CLUBS) is None


def test_league_strength_is_the_mean_of_its_best_five_clubs():
    S = CE.league_strengths(CLUBS)
    assert S["ENG"] == pytest.approx((2049 + 2000 + 1890 + 1880) / 4)
    assert S["NOR"] == pytest.approx((1785 + 1706 + 1650) / 3)
    assert "UKR" not in S                                      # un club no mide una liga
    assert CE.team_strengths(["aston villa", "viking fk", "shakhtar donetsk", "manchester"], CLUBS) == {
        "aston villa": S["ENG"], "viking fk": S["NOR"]}


def test_the_league_factor_moves_goals_to_the_stronger_league_damped():
    s_eng, s_nor = 1965, 1642
    f = CE.league_factor(s_eng, s_nor)
    full = CE.side_factor(CE.BASE_GOALS, CE.BASE_GOALS, s_eng - CE.ELO_HOME_ADV, s_nor)
    assert f == pytest.approx(full ** CE.GAMMA) and 1.0 < f < full
    # sin ventaja local de más: invertir las ligas invierte el reparto
    assert CE.league_factor(s_nor, s_eng) == pytest.approx(1 / f, rel=1e-4)
    assert CE.league_factor(1900, 1900) == 1.0
    assert CE.league_factor(s_eng, s_nor, gamma=0) == pytest.approx(1.0)


# ─── la foto semanal ─────────────────────────────────────────────────────────

def test_the_weekly_snapshot_saves_every_country_it_could_read():
    eng = FakeEngine()
    pages = {"NOR": NOR}

    def get(url):
        code = url.rsplit("/", 1)[1]
        if code not in pages:
            raise TimeoutError
        return pages[code]
    assert CE.refresh_clubelo(eng, get, pause=0, countries=("NOR", "SWE")) == 2
    (sql, rows), = eng.statements("INSERT INTO club_elo")
    assert "ON CONFLICT (fetched_on, slug) DO UPDATE" in sql
    assert [r["slug"] for r in rows] == ["BodoeGlimt", "Viking"] and rows[0]["d"] == date.today()


def test_without_any_country_it_is_an_error_that_reaches_telegram():
    RUN_ISSUES.reset()
    eng = FakeEngine()
    assert CE.refresh_clubelo(eng, lambda url: "<html>502</html>", pause=0, countries=("NOR", "SWE")) == 0
    assert eng.executed == []
    assert any("ClubElo" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


@pytest.mark.parametrize("last,refresh", [(1, False), (6, False), (7, True), (None, True)])
def test_the_snapshot_is_renewed_once_a_week(monkeypatch, last, refresh):
    def read_sql(sql, con=None, **kw):
        if last is None:
            raise RuntimeError('relation "club_elo" does not exist')
        return pd.DataFrame({"d": [date.today() - timedelta(days=last)]})
    called = []
    monkeypatch.setattr(CE.pd, "read_sql", read_sql)
    monkeypatch.setattr(CE, "refresh_clubelo", lambda engine=None, **kw: called.append(kw) or 5)
    CE.refresh_if_stale(engine=FakeEngine())
    assert bool(called) is refresh
    if called and last is not None:
        assert called[0]["last"] == date.today() - timedelta(days=last)   # para saber cuánto sirve la foto


class _Blocked(Exception):
    code = 403                                     # urllib.error.HTTPError lleva el estado en .code


@pytest.mark.parametrize("age,level", [(7, "warning"), (40, "error"), (50, "error"), (None, "error")])
def test_when_clubelo_blocks_the_old_snapshot_is_used_and_it_warns_before_it_expires(age, level, caplog):
    # 29-oct-26: las páginas de ClubElo piden un reto anti-bots de Cloudflare (403)
    RUN_ISSUES.reset()
    asked = []

    def get(url):
        asked.append(url)
        raise _Blocked()
    last = None if age is None else date.today() - timedelta(days=age)
    with caplog.at_level("WARNING"):
        assert CE.refresh_clubelo(FakeEngine(), get, pause=0, last=last) == 0
    assert len(asked) == CE.BLOCK_PROBE                       # tres 403 seguidos: no se piden los otros 50
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "bloquea las lecturas automáticas" in msg and "403" in msg
    errors = [e for e in RUN_ISSUES.errors() if "ClubElo" in e]
    assert bool(errors) is (level == "error")                 # el ERROR llega a Telegram una semana antes
    if age == 7:
        assert "sirve hasta el" in msg
    if age == 40:
        assert "vence el" in errors[0]
    RUN_ISSUES.reset()


# ─── el pipeline ─────────────────────────────────────────────────────────────

def _lambdas(monkeypatch, league, clubelo):
    import src.pipeline.prediction_pipeline as pp
    for name in ("get_h2h_stats", "get_team_corners", "get_team_shots", "get_team_cards"):
        monkeypatch.setattr(pp, name, lambda *a, **k: None)
    monkeypatch.setattr(pp, "get_weather_multiplier", lambda *a, **k: 1.0)
    monkeypatch.setattr(pp, "get_dc_lambdas", lambda h, a, is_neutral=False: (1.5, 1.4))
    form = {"matches": 8, "attack_rating": 1.35, "defense_rating": 1.35}
    T = SimpleNamespace(home="aston villa", away="viking fk", home_form=form, away_form=form,
                        home_attack=1.35, home_defense=1.35, away_attack=1.35, away_defense=1.35,
                        HOME_ADVANTAGE=1.2, TEMPO=1.0, H2H_WEIGHT=0.15)
    ctx = SimpleNamespace(mle_fresh=True, hist_counts={"aston villa": 120, "viking fk": 120}, clubelo=clubelo)
    row = pd.Series({"sport_key": league, "league": league})
    stats = Counter()
    return pp._stage_lambdas(row, T, ctx, stats), stats


def test_a_cup_match_splits_the_goals_by_the_strength_of_each_league(monkeypatch):
    S = {"aston villa": 1965.0, "viking fk": 1642.0}
    plain, _ = _lambdas(monkeypatch, "soccer_uefa_europa_league", {})
    L, stats = _lambdas(monkeypatch, "soccer_uefa_europa_league", S)
    f = CE.league_factor(1965.0, 1642.0)
    assert L.lambda_home == pytest.approx(plain.lambda_home * f)
    assert L.lambda_away == pytest.approx(plain.lambda_away / f)
    assert L.league_gap == (1965.0, 1642.0, pytest.approx(f)) and stats["clubelo"] == 1
    assert plain.league_gap is None


def test_a_league_match_is_not_touched_by_clubelo(monkeypatch):
    S = {"aston villa": 1965.0, "viking fk": 1642.0}
    plain, _ = _lambdas(monkeypatch, "soccer_epl", {})
    L, stats = _lambdas(monkeypatch, "soccer_epl", S)
    assert (L.lambda_home, L.lambda_away, L.league_gap) == (plain.lambda_home, plain.lambda_away, None)
    assert stats["clubelo"] == 0


def _cup_match():
    from tests.pipeline_harness import _row
    return pd.DataFrame([_row("beta", "alpha", "soccer_uefa_champs_league",
                              home_odds=3.10, draw_odds=3.40, away_odds=2.30,
                              pin_home_odds=3.00, pin_draw_odds=3.45, pin_away_odds=2.40)])


def test_the_analysis_records_the_league_factor_and_leans_to_the_stronger_league(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    plain = run_pipeline(monkeypatch, matches=_cup_match())["model_sharp"]
    adj = run_pipeline(monkeypatch, matches=_cup_match(),
                       overrides={"_safe_clubelo": lambda df: {"beta": 1965.0, "alpha": 1642.0}})["model_sharp"]
    (p,), (a,) = plain, adj
    assert p["league_factor"] is None
    assert a["league_factor"] == pytest.approx(CE.league_factor(1965.0, 1642.0))
    assert a["p_home"] > p["p_home"] and a["p_away"] < p["p_away"]      # el local es de la liga fuerte


# ─── los picks ───────────────────────────────────────────────────────────────

def test_a_cup_match_only_has_a_pick_when_it_was_analysed_by_league():
    from tests.test_program_picks import MODEL_EVEN, _mvs, _u
    from src.models import program_picks as PP
    pk, why = PP.choose(PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.60, None, _u()),
                        league="soccer_uefa_champs_league", adjusted=True)
    assert pk["market"] == "over25" and why is None
    row = {**_mvs(model=MODEL_EVEN, over=0.60), "league": "soccer_uefa_champs_league"}
    assert PP.decide(row, _u())[1] == PP.CROSS                                 # sin factor: fuera de la regla
    assert PP.decide({**row, "league_factor": float("nan")}, _u())[1] == PP.CROSS
    pk, why = PP.decide({**row, "league_factor": 1.12}, _u())
    assert pk["market"] == "over25" and why is None


def test_model_vs_sharp_keeps_the_league_factor():
    from src.models import side_bias as sb
    probs = {"home_win": 0.5, "draw": 0.25, "away_win": 0.25}
    pin = {"home": 0.45, "draw": 0.27, "away": 0.28}
    assert "league_factor" in sb.EXTRA_COLUMNS
    assert sb.record("a vs b", "2030-01-05", "soccer_uefa_champs_league", probs, pin,
                     league_factor=1.12)["league_factor"] == pytest.approx(1.12)
    assert sb.record("a vs b", "2030-01-05", "soccer_epl", probs, pin)["league_factor"] is None


def test_the_morning_reads_clubelo_before_the_predictions(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_morning")
    assert names.index("ClubElo (fuerza de liga)") == names.index("Predictions") - 1
    assert steps["ClubElo (fuerza de liga)"] is O.step_clubelo
