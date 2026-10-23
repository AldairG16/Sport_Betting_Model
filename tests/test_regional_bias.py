"""
Sesgo del modelo por región y en goles (23-oct-26, src/models/side_bias.py).

Medido el 23-oct con el modelo del 22-oct: el local +3.9 pt sobre Pinnacle en
las 5 grandes, +1.3 en el resto de Europa, −0.6 en América; más de 2.5 −1.3
(−3.8 en América) y ambos anotan −2.7 contra el mercado. Una sola corrección
global no servía para todas, y goles no se corregía. Además, cada lector del
modelo guardado (picks, dashboard, revisor de PlayDoit) tiene que ver el mismo
modelo corregido que usó el pipeline.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

import src.models.side_bias as SB

STATE = {"home": 0.01, "draw": -0.005, "away": -0.005, "over25": -0.01, "btts": -0.02,
         "regions": {"big5": {"home": 0.03, "draw": -0.01, "away": -0.02, "over25": 0.0, "btts": -0.01, "n": 50},
                     "america": {"home": -0.01, "draw": 0.0, "away": 0.01, "over25": -0.04, "btts": -0.03, "n": 50}}}


def test_each_league_belongs_to_a_region():
    assert SB.region_of("soccer_epl") == SB.region_of("soccer_italy_serie_a") == "big5"
    assert SB.region_of("soccer_usa_mls") == SB.region_of("soccer_conmebol_copa_libertadores") == "america"
    assert SB.region_of("soccer_argentina_primera_division") == SB.region_of("soccer_mexico_ligamx") == "america"
    assert SB.region_of("soccer_japan_j_league") == SB.region_of("soccer_korea_kleague1") == "asia"
    assert SB.region_of("soccer_efl_champ") == SB.region_of("soccer_uefa_champs_league") == "europa"
    assert SB.region_of("soccer_afcon") == "europa" and SB.region_of(None) == "europa"


def test_the_big5_list_is_the_pipelines():
    from src.pipeline.prediction_pipeline import BIG5_LEAGUES
    assert set(SB.BIG5) == set(BIG5_LEAGUES)


def test_a_match_gets_its_regions_bias_or_the_global_one():
    assert SB.bias_for(STATE, "soccer_epl")["home"] == 0.03
    assert SB.bias_for(STATE, "soccer_usa_mls")["over25"] == -0.04
    assert SB.bias_for(STATE, "soccer_efl_champ") == {"home": 0.01, "draw": -0.005, "away": -0.005,
                                                      "over25": -0.01, "btts": -0.02}
    old = {"home": 0.0111, "draw": -0.0055, "away": -0.0056, "n": 141}        # estado de antes del 23-oct
    assert SB.bias_for(old, "soccer_epl") == {"home": 0.0111, "draw": -0.0055, "away": -0.0056}
    assert SB.bias_for(None, "soccer_epl") == {}


def test_goals_and_btts_are_debiased_with_their_complements():
    model = {"home_win": 0.5, "draw": 0.25, "away_win": 0.25, "over25": 0.50, "under25": 0.50,
             "btts": 0.45, "btts_no": 0.55}
    out, shift = SB.debias(model, {"over25": -0.03, "btts": -0.02})
    assert out["over25"] == pytest.approx(0.53) and out["under25"] == pytest.approx(0.47)
    assert out["btts"] == pytest.approx(0.47) and out["btts_no"] == pytest.approx(0.53)
    assert shift["over25"] == pytest.approx(0.03) and shift["btts"] == pytest.approx(0.02)
    assert out["home_win"] == 0.5 and model["over25"] == 0.50                    # 1X2 intacto, entrada intacta


def _df(rows):
    return pd.DataFrame(rows)


def _row(league, home_bias=0.0, over_bias=0.0, btts_bias=0.0, i=0):
    return {"match": f"m{league}{i}", "match_date": "2026-10-23", "league": league,
            "p_home": 0.50 + home_bias, "p_draw": 0.25 - home_bias / 2, "p_away": 0.25 - home_bias / 2,
            "pin_home": 0.50, "pin_draw": 0.25, "pin_away": 0.25,
            "p_over25": 0.50 + over_bias, "mkt_over25": 0.50, "p_btts": 0.50 + btts_bias, "mkt_btts": 0.50}


def test_regions_separate_from_the_global_by_their_sample():
    rows = [_row("soccer_epl", home_bias=0.06, i=i) for i in range(60)] + \
           [_row("soccer_usa_mls", home_bias=-0.02, over_bias=-0.04, i=i) for i in range(60)] + \
           [_row("soccer_japan_j_league", home_bias=0.10, i=i) for i in range(5)]       # muy pocos
    st = SB.learn(_df(rows))
    big5, am, asia = st["regions"]["big5"], st["regions"]["america"], st["regions"]["asia"]
    assert big5["home"] > st["home"] > am["home"]                     # cada una hacia su lado
    assert big5["n"] == 60 and asia["n"] == 5
    # Asia (5 < MIN_N_REGION) se queda con lo global
    assert asia["home"] == pytest.approx(st["home"], abs=1e-5)
    # el paso semanal parte de lo que se le venía aplicando (sin anterior: 0)
    assert all(abs(r[s]) <= SB.MAX_STEP + 1e-9 for r in st["regions"].values() for s in SB.SIDES)
    # sin topes de por medio, la región = global + (su media − la global)·n/(n + REGION_SHRINK_N)
    st2 = SB.learn(_df(rows), previous={**st, "regions": {}, "home": 0.0})
    n, n_r = len(rows), 60
    glob = sum(r["p_home"] - r["pin_home"] for r in rows) / n
    want = glob * n / (n + SB.SHRINK_N) + (0.06 - glob) * n_r / (n_r + SB.REGION_SHRINK_N)
    center = sum(  # el trío de la región se centra en 0, como el global
        [want] + [(-glob / 2) * n / (n + SB.SHRINK_N) + (-0.03 + glob / 2) * n_r / (n_r + SB.REGION_SHRINK_N)] * 2) / 3
    assert st2["regions"]["big5"]["home"] == pytest.approx(min(want - center, SB.MAX_STEP), abs=1e-4)


def test_goals_bias_is_learned_like_the_sides():
    rows = [_row("soccer_usa_mls", over_bias=-0.04, btts_bias=-0.03, i=i) for i in range(100)]
    st = SB.learn(_df(rows))
    shrink = 100 / (100 + SB.SHRINK_N)
    assert st["stats"]["over25"]["mean"] == pytest.approx(-0.04) and st["stats"]["over25"]["n"] == 100
    assert st["over25"] == pytest.approx(max(-0.04 * shrink, -SB.MAX_STEP), abs=1e-5)
    assert st["btts"] == pytest.approx(-0.03 * shrink, abs=1e-5)
    assert st["regions"]["america"]["over25"] == pytest.approx(st["over25"], abs=1e-5)  # una sola región
    big = [_row("soccer_usa_mls", over_bias=-0.30, i=i) for i in range(1000)]
    st = SB.learn(_df(big), previous={"over25": -0.059})
    assert st["over25"] == pytest.approx(-SB.GOAL_MAX_ABS)                       # nunca más que el tope


def test_without_goal_references_the_goals_correction_stays():
    rows = [{k: v for k, v in _row("soccer_epl", i=i).items() if k not in ("mkt_over25", "mkt_btts")}
            for i in range(60)]
    st = SB.learn(_df(rows), previous={"home": 0.0, "draw": 0.0, "away": 0.0, "over25": -0.02})
    assert st["over25"] == -0.02 and st["btts"] == 0.0


def test_with_little_data_everything_learned_is_kept():
    prev = {"home": 0.02, "draw": -0.01, "away": -0.01, "over25": -0.01, "btts": -0.02,
            "regions": {"big5": {"home": 0.03}}}
    st = SB.learn(_df([_row("soccer_epl", i=i) for i in range(5)]), previous=prev)
    assert (st["source"], st["over25"], st["btts"], st["regions"]) == ("anterior", -0.01, -0.02, {"big5": {"home": 0.03}})


def test_the_weekly_report_and_telegram_see_region_changes():
    prev = {"home": 0.01, "draw": -0.005, "away": -0.005}
    assert not SB.moved(dict(prev), prev)
    assert SB.moved({**prev, "over25": -0.01}, prev)
    assert SB.moved({**prev, "regions": {"big5": {"home": 0.03, "draw": -0.01, "away": -0.02}}}, prev)
    text = SB.format_report({**STATE, "n": 142, "source": "datos",
                             "stats": {"over25": {"mean": -0.013, "n": 142}}}, prev)
    assert "más de 2.5" in text and "5 grandes (n=50): +3.0 / -1.0 / -2.0 · +0.0 · -1.0 pt" in text
    assert "América (n=50)" in text


def test_program_probs_is_the_corrected_model_of_a_saved_row():
    r = {"league": "soccer_usa_mls", "p_home": 0.50, "p_draw": 0.25, "p_away": 0.25, "p_over25": 0.50, "p_btts": None}
    p = SB.program_probs(r, STATE)
    assert p["over25"] == pytest.approx(0.54) and p["btts"] is None
    assert p["home_win"] == pytest.approx(0.51 / 1.0, abs=1e-9)                   # America: local −1 pt → +1
    assert SB.program_probs(r, None) == {"home_win": 0.5, "draw": 0.25, "away_win": 0.25, "over25": 0.5, "btts": None}
    assert SB.program_probs({"p_home": None}, STATE)["home_win"] is None


# ─── cada lector del modelo guardado usa el corregido ────────────────────────

MVS_ROW = {"match": "la galaxy vs seattle", "match_date": "2026-10-25 02:00+00:00", "league": "soccer_usa_mls",
           "p_home": 0.50, "p_draw": 0.25, "p_away": 0.25, "pin_home": 0.50, "pin_draw": 0.25, "pin_away": 0.25,
           "lam_home": 1.6, "lam_away": 1.2, "p_over25": 0.50, "p_btts": 0.50, "league_factor": None,
           "skipped_at": None, "created_at": "2026-10-24 03:00+00:00"}
UPCOMING = SimpleNamespace(home_team="LA Galaxy", away_team="Seattle", home_team_norm="la galaxy",
                           away_team_norm="seattle", pin_home_odds=1.95, pin_draw_odds=3.9, pin_away_odds=3.9,
                           home_odds=2.0, draw_odds=4.0, away_odds=4.0, consensus_home_odds=1.95,
                           consensus_draw_odds=3.8, consensus_away_odds=3.8,
                           consensus_over25_odds=1.92, consensus_under25_odds=1.92,
                           over25_odds=1.98, under25_odds=1.98, btts_yes_odds=1.95, btts_no_odds=1.95,
                           pin_total_line=None, pin_total_over_odds=None, pin_total_under_odds=None,
                           corners_line=None, cards_line=None)


def test_the_pick_sees_the_corrected_goals():
    from src.models import program_picks as PK
    seen = {}
    real = PK.candidates

    def spy(model, lam, p_over25, p_btts, *a, **k):
        seen.update(model=model, over=p_over25, btts=p_btts)
        return real(model, lam, p_over25, p_btts, *a, **k)
    import unittest.mock as um
    with um.patch.object(PK, "candidates", spy):
        PK.decide(dict(MVS_ROW), UPCOMING, None, None, None, STATE)
    assert seen["over"] == pytest.approx(0.54) and seen["btts"] == pytest.approx(0.53)
    assert seen["model"][0] == pytest.approx(SB.program_probs(MVS_ROW, STATE)["home_win"])


def test_the_dashboard_shows_the_corrected_goals():
    from dashboard import visibility as V
    up = pd.DataFrame([vars(UPCOMING)])
    out = V.predictions(pd.DataFrame([MVS_ROW]), up, set(), None, {}, STATE)
    assert out[0]["p_over25"] == pytest.approx(0.54) and out[0]["p_btts"] == pytest.approx(0.53)
    assert out[0]["sides"][0]["model"] == pytest.approx(SB.program_probs(MVS_ROW, STATE)["home_win"])


def test_the_playdoit_checker_judges_with_the_corrected_model():
    from src.models import playdoit_prices as PP
    f = {**vars(UPCOMING), **{k: MVS_ROW[k] for k in ("p_home", "p_draw", "p_away", "p_over25", "p_btts")},
         "league": "soccer_usa_mls"}
    _, model = PP.reference_probs(f, STATE)
    assert model["over25"] == pytest.approx(0.54) and model["under25"] == pytest.approx(0.46)
    assert model["home_win"] == pytest.approx(SB.program_probs(MVS_ROW, STATE)["home_win"])
    _, raw = PP.reference_probs(f)
    assert raw["over25"] == 0.50


# ─── el pipeline: registra la referencia y descuenta el sesgo de su región ───

def test_the_pipeline_records_the_goal_market_and_debiases_by_region():
    from src.pipeline.prediction_pipeline import _stage_side_bias
    row = pd.Series({**vars(UPCOMING), "sport_key": "soccer_usa_mls", "league": "soccer_usa_mls"})
    probs = {"home_win": 0.50, "draw": 0.25, "away_win": 0.25, "over25": 0.50, "under25": 0.50,
             "btts": 0.50, "btts_no": 0.50}
    M = SimpleNamespace(model_probs=dict(probs), home_win=0.5, draw=0.25, away_win=0.25)
    T = SimpleNamespace(home="la galaxy", away="seattle", date=pd.Timestamp("2026-10-25 02:00", tz="UTC"))
    records = []
    _stage_side_bias(row, T, M, SimpleNamespace(side_bias=STATE), records, None)
    rec = records[0]
    assert rec["p_over25"] == 0.50                                    # se guarda CRUDO
    assert rec["mkt_over25"] == pytest.approx(0.5) and rec["mkt_btts"] == pytest.approx(0.5)
    assert M.model_probs["over25"] == pytest.approx(0.54)             # América: −4 pt
    assert M.model_probs["under25"] == pytest.approx(0.46)
    assert M.home_win == pytest.approx(SB.program_probs({**rec, "league": "soccer_usa_mls"}, STATE)["home_win"])


def test_market_goal_refs_uses_the_same_reference_as_the_picks():
    from src.models.program_picks import devig2
    r = {"consensus_over25_odds": 1.80, "consensus_under25_odds": 2.05, "over25_odds": 1.9, "under25_odds": 2.1,
         "btts_yes_odds": 1.7, "btts_no_odds": 2.2}
    refs = SB.market_goal_refs(r)
    assert refs["over25"] == pytest.approx(devig2(1.80, 2.05)) and refs["btts"] == pytest.approx(devig2(1.7, 2.2))
    assert SB.market_goal_refs({"over25_odds": 1.9, "under25_odds": 2.1})["over25"] == pytest.approx(devig2(1.9, 2.1))
    assert SB.market_goal_refs({}) == {"over25": None, "btts": None}


# ─── la auditoría del sistema vigila el sesgo que queda ──────────────────────

def test_residuals_measure_what_is_left_after_the_correction():
    rows = [_row("soccer_epl", home_bias=0.06, over_bias=-0.04, i=i) for i in range(40)]
    df = pd.DataFrame(rows)
    raw = {(r["scope"], r["market"]): r for r in SB.residuals(df, None)}
    assert raw[("global", "home_win")]["mean"] == pytest.approx(0.06)
    assert raw[("big5", "over25")]["mean"] == pytest.approx(-0.04) and raw[("big5", "over25")]["n"] == 40
    fixed = {(r["scope"], r["market"]): r for r in SB.residuals(df, {"regions": {"big5": {
        "home": 0.06, "draw": -0.03, "away": -0.03, "over25": -0.04}}})}
    assert fixed[("big5", "home_win")]["mean"] == pytest.approx(0.0, abs=1e-9)
    assert fixed[("big5", "over25")]["mean"] == pytest.approx(0.0, abs=1e-9)
    assert SB.residuals(pd.DataFrame(), None) == []


def test_the_audit_flags_a_bias_left_uncorrected_or_a_stale_learner():
    from scripts.system_audit import bias_problems
    now = pd.Timestamp("2026-10-25 12:00", tz="UTC")
    fresh = {"updated_at": "2026-10-25T02:00:00+00:00"}
    big = {"scope": "big5", "market": "home_win", "n": 60, "mean": 0.05, "se": 0.01}
    noisy = {"scope": "america", "market": "over25", "n": 60, "mean": 0.04, "se": 0.02}     # t = 2: ruido
    small = {"scope": "asia", "market": "btts", "n": 10, "mean": 0.10, "se": 0.01}         # muy pocos
    out = bias_problems([big, noisy, small], fresh, now)
    assert out == ["Sesgo local (big5): queda +5.0 ±1.0 pt después de corregir (n=60)"]
    assert bias_problems([], {"updated_at": "2026-10-10T02:00:00+00:00"}, now)[0].startswith("Sesgo: lo aprendido es de hace 15")
    assert bias_problems([], {}, now) == ["Sesgo: el weekly todavía no lo aprende"]


def test_the_backfill_takes_the_reference_from_the_odds_the_program_saw():
    from scripts.backfill_goal_refs import plan
    mvs = pd.DataFrame([{"match": "la galaxy vs seattle", "match_date": pd.Timestamp("2026-10-25 02:00", tz="UTC"),
                         "created_at": pd.Timestamp("2026-10-24 03:00", tz="UTC")},
                        {"match": "sin fotos vs nadie", "match_date": pd.Timestamp("2026-10-25 02:00", tz="UTC"),
                         "created_at": pd.Timestamp("2026-10-24 03:00", tz="UTC")}])
    snap = {"match": "LA Galaxy vs Seattle", "kickoff": pd.Timestamp("2026-10-25 02:00", tz="UTC"),
            "consensus_over25_odds": 1.80, "consensus_under25_odds": 2.05, "btts_yes_odds": 1.7, "btts_no_odds": 2.2}
    snaps = pd.DataFrame([{**snap, "captured_at": pd.Timestamp("2026-10-24 02:00", tz="UTC")},          # la que vio
                          {**snap, "captured_at": pd.Timestamp("2026-10-24 20:00", tz="UTC"),          # después
                           "consensus_over25_odds": 2.50, "consensus_under25_odds": 1.50}])
    rows = plan(mvs, snaps, None)
    assert len(rows) == 1 and rows[0]["match"] == "la galaxy vs seattle"
    assert rows[0]["mkt_over25"] == pytest.approx((1 / 1.80) / (1 / 1.80 + 1 / 2.05))
    assert plan(pd.DataFrame(), snaps, None) == []
