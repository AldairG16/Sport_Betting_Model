"""
tests/test_goal_lines_and_leagues.py
====================================
Ligas fuera y líneas 1.5 / 3.5 de goles con precio real (30-oct-26).

El dueño, después de ver qué ligas pierden: "quiero tener ahorro de créditos
para poder valorar más varios mercados de picks". MLS y Championship dejan de
descargarse; el mercado por evento alternate_totals trae las líneas 1.5 y 3.5
de goles, que antes solo se estimaban del 2.5 y por eso nunca eran pick.
"""

import inspect
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import program_picks as PP

MODEL_EVEN = [0.44, 0.27, 0.29]
BASE = {"home_odds": 2.30, "draw_odds": 3.60, "away_odds": 3.30,
        "pin_home_odds": 2.25, "pin_draw_odds": 3.70, "pin_away_odds": 3.40,
        "consensus_home_odds": 2.20, "consensus_draw_odds": 3.50, "consensus_away_odds": 3.20,
        "over25_odds": 1.95, "under25_odds": 1.95, "consensus_over25_odds": 1.86, "consensus_under25_odds": 1.86}


def _u(**kw):
    return SimpleNamespace(**{**BASE, **kw})


def _alt(o15=None, u15=None, o35=None, u35=None):
    pts = [(1.5, o15, u15), (2.5, 1.9, 1.9), (3.5, o35, u35), (4.5, 6.0, 1.10)]
    return {"key": "alternate_totals",
            "outcomes": [o for p, ov, un in pts for o in ({"name": "Over", "point": p, "price": ov},
                                                          {"name": "Under", "point": p, "price": un}) if o["price"]]}


def _event(*books):
    return {"id": "e1", "home_team": "Alpha", "away_team": "Beta", "commence_time": "2030-01-05T15:00:00Z",
            "bookmakers": [{"key": k, "title": k, "markets": [m]} for k, m in books]}


# ─── la descarga ──────────────────────────────────────────────────────────────

def test_the_1_5_and_3_5_lines_come_from_alternate_totals_best_and_average():
    from scripts.update_upcoming_matches import parse_match
    row = parse_match(_event(("a", _alt(1.30, 3.40, 3.10, 1.36)),
                             ("b", _alt(1.26, 3.60, 3.30, 1.32)),
                             ("c", _alt(1.28, 3.50, 3.50, None))), "soccer_epl")   # c sin menos de 3.5
    assert (row["over15_odds"], row["under15_odds"]) == (1.30, 3.60)                  # mejor
    assert (row["consensus_over15_odds"], row["consensus_under15_odds"]) == (1.28, 3.5)  # promedio de 3 casas
    assert (row["over35_odds"], row["under35_odds"]) == (3.30, 1.36)                  # c no cuenta: un solo lado
    assert (row["consensus_over35_odds"], row["consensus_under35_odds"]) == (3.2, 1.34)
    none = parse_match(_event(("a", {"key": "h2h", "outcomes": [{"name": "Alpha", "price": 2.0},
                                                                 {"name": "Beta", "price": 3.5},
                                                                 {"name": "Draw", "price": 3.4}]})), "soccer_epl")
    assert none["over35_odds"] is None and none["consensus_under15_odds"] is None    # sin el mercado: NULL


def test_alternate_totals_is_a_per_event_market_that_only_costs_when_it_comes():
    import scripts.update_upcoming_matches as uum
    assert "alternate_totals" in uum.SPECIALTY_MARKETS.split(",")
    assert "alternate_totals" in uum._SPECIALTY_KEYS                  # /odds de la liga no lo trae
    assert set(uum.GOAL_LINE_COLS) <= set(uum.SPECIALTY_ODDS_COLS)    # su hora es specialty_fetched_at
    assert uum._costs()[1] == 8                                        # el presupuesto lo cuenta (a lo más)
    ev = _event(("a", _alt(1.30, 3.40, 3.10, 1.36)))
    uum.strip_specialty(ev)
    assert ev["bookmakers"][0]["markets"] == []


def test_the_new_columns_are_created_and_kept_like_corners():
    import scripts.update_upcoming_matches as uum
    assert "GOAL_LINE_COLS" in inspect.getsource(uum.ensure_schema)
    up = re.sub(r"\s+", " ", inspect.getsource(uum.upsert_matches))
    for col in uum.GOAL_LINE_COLS:                     # se guardan y una recarga sin ellas no los borra
        assert f":{col}," in up and f"{col} = COALESCE(EXCLUDED.{col}, upcoming_matches.{col})" in up


# ─── el pick ─────────────────────────────────────────────────────────────────

def test_a_real_3_5_line_is_a_candidate_with_the_average_price():
    u = _u(over35_odds=3.40, under35_odds=1.40, consensus_over35_odds=3.20, consensus_under35_odds=1.36)
    cs = {c["market"]: c for c in PP.candidates(MODEL_EVEN, (1.5, 1.2), 0.49, None, u)}
    mk = (1 / 1.36) / (1 / 1.36 + 1 / 3.20)
    assert cs["under_3.5"]["market_prob"] == pytest.approx(mk)
    assert cs["under_3.5"]["playdoit_odds"] == pytest.approx(1.36)              # promedio (ratio de PlayDoit: 1)
    assert cs["under_3.5"]["best_odds"] == 1.40
    assert cs["over_3.5"]["model"] + cs["under_3.5"]["model"] == pytest.approx(1.0)
    assert "over_1.5" not in cs                                                  # sin precio de 1.5: no entra


def test_the_3_5_line_can_be_the_pick_now():
    lam, po = (1.7, 1.4), 0.55
    goals = {"consensus_over25_odds": 1.76, "consensus_under25_odds": 2.15}      # el 2.5 sin ventaja (55%)
    u0 = _u(over35_odds=3.0, under35_odds=1.5, consensus_over35_odds=3.0, consensus_under35_odds=1.5, **goals)
    model_under = {c["market"]: c for c in PP.candidates(MODEL_EVEN, lam, po, None, u0)}["under_3.5"]["model"]
    # el mercado 5 pts abajo del programa, con el momio dentro del rango de la regla
    mk = model_under - 0.05
    under, over = 1 / mk * 0.97, 1 / (1 - mk) * 0.97
    assert PP.MIN_ODDS <= under <= PP.MAX_ODDS
    u = _u(over35_odds=over, under35_odds=under, consensus_over35_odds=over, consensus_under35_odds=under, **goals)
    pk, why = PP.choose(PP.candidates(MODEL_EVEN, lam, po, None, u))
    assert (pk["market"], pk["label"], pk["family"], why) == ("under_3.5", "Menos de 3.5 goles", "Goles", None)
    row = PP._row({"match": "alpha vs beta", "league": "soccer_epl"}, pd.Timestamp("2030-01-05", tz="UTC"),
                  "Alpha vs Beta", pk, why)
    assert row["market"] == "under_3.5" and row["min_us"] is not None


def test_pinnacles_main_line_still_wins_over_the_alternate():
    u = _u(pin_total_line=3.5, pin_total_over_odds=2.40, pin_total_under_odds=1.62,
           over35_odds=3.40, under35_odds=1.40, consensus_over35_odds=3.20, consensus_under35_odds=1.36)
    cs = {c["market"]: c for c in PP.candidates(MODEL_EVEN, (1.5, 1.2), 0.49, None, u)}
    assert cs["over_3.5"]["market_prob"] == pytest.approx(PP.devig2(2.40, 1.62))
    assert cs["over_3.5"]["best_odds"] is None


def test_the_view_stops_estimating_a_line_that_has_a_real_price():
    r = {"match": "alpha vs beta", "match_date": pd.Timestamp("2030-01-05 19:00", tz="UTC"),
         "league": "soccer_epl", "p_home": 0.44, "p_draw": 0.27, "p_away": 0.29,
         "pin_home": 0.43, "pin_draw": 0.26, "pin_away": 0.28, "lam_home": 1.5, "lam_away": 1.2,
         "p_over25": 0.49, "p_btts": None}
    u = _u(over35_odds=3.40, under35_odds=1.40, consensus_over35_odds=3.20, consensus_under35_odds=1.36)
    view = {v["market"]: v for v in PP.market_view(r, u)}
    assert not view["under_3.5"].get("estimated") and view["over_1.5"]["estimated"] is True
    assert sum(1 for v in PP.market_view(r, u) if v["market"] == "under_3.5") == 1     # sin duplicar


# ─── las ligas ───────────────────────────────────────────────────────────────

def test_mls_and_championship_are_no_longer_downloaded():
    from config.settings import SPORT_KEYS
    assert "soccer_usa_mls" not in SPORT_KEYS and "soccer_efl_champ" not in SPORT_KEYS
    # el dueño las propuso, pero no pierden: se quedan
    for kept in ("soccer_brazil_campeonato", "soccer_belgium_first_div", "soccer_turkey_super_league"):
        assert kept in SPORT_KEYS


def test_a_removed_league_still_gets_the_results_its_picks_need(monkeypatch, capsys):
    import scripts.fetch_results as FR
    calls = []

    class _R:
        headers = {"x-requests-last": "2"}

        def raise_for_status(self):
            pass

        def json(self):
            return []
    monkeypatch.setattr(FR, "SPORT_KEYS", ["soccer_epl", "soccer_brazil_campeonato"])
    monkeypatch.setattr(FR, "leagues_needing_results", lambda d: {"soccer_efl_champ", "soccer_epl"})
    monkeypatch.setattr(FR.requests, "get", lambda url, params=None, timeout=None: calls.append(url) or _R())
    FR.fetch_all_results(days_from=3)
    assert [c.split("/sports/")[1].split("/")[0] for c in calls] == ["soccer_epl", "soccer_efl_champ"]


def test_the_pipeline_skips_rows_of_leagues_no_longer_downloaded(capsys):
    from src.pipeline import prediction_pipeline as P
    df = pd.DataFrame({"sport_key": ["soccer_epl", "soccer_usa_mls", "soccer_usa_mls"], "home_team": list("abc")})
    out = P.only_fetched_leagues(df, ["soccer_epl"])
    assert list(out["home_team"]) == ["a"]
    assert "no se analizan: soccer_usa_mls (2 partidos)" in capsys.readouterr().out
    assert P.only_fetched_leagues(df.iloc[:1], ["soccer_epl"]).equals(df.iloc[:1])
    src = inspect.getsource(P)
    assert "df = only_fetched_leagues(df)" in src
