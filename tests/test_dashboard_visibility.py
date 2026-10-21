"""
Dashboard v1.7.0 (21-oct-26): lo que el programa analiza aunque no apueste.
En modo recolección casi no hay apuestas reales y el dashboard se veía vacío;
ahora muestra qué dice de cada partido, si acertó y qué propuso. Sin DB.
"""

import pandas as pd
import pytest

from dashboard import visibility as V

KICK = pd.Timestamp("2026-10-21 19:00", tz="UTC")


def _mvs(match="bayern munchen vs arsenal", when=KICK, model=(0.504, 0.223, 0.273),
         pin=(0.435, 0.257, 0.308), lam=(1.74, 1.51), league="soccer_uefa_champs_league"):
    return {"match": match, "match_date": when, "league": league,
            "p_home": model[0], "p_draw": model[1], "p_away": model[2],
            "pin_home": pin[0], "pin_draw": pin[1], "pin_away": pin[2],
            "lam_home": lam[0] if lam else None, "lam_away": lam[1] if lam else None,
            "p_over25": 0.61, "p_btts": 0.625}


UPCOMING = pd.DataFrame([{"home_team_norm": "bayern munchen", "away_team_norm": "arsenal",
                          "home_team": "Bayern Munich", "away_team": "Arsenal",
                          "home_odds": 2.30, "draw_odds": 3.90, "away_odds": 3.25}])


def test_prediction_shows_what_the_program_says_with_playdoit_minimums():
    (m,) = V.predictions(pd.DataFrame([_mvs()]), UPCOMING, set())
    assert m["match"] == "Bayern Munich vs Arsenal" and m["league"]
    assert m["lam"] == [1.74, 1.51] and m["likely"]["score"] == "1-1"
    home, draw, away = m["sides"]
    # mínimo = la prob más baja entre programa y Pinnacle, menos 2 pt:
    # local 43.5% (Pinnacle) → +141; empate 22.3% y visita 27.3% (programa)
    assert (home["min_us"], draw["min_us"], away["min_us"]) == ("+141", "+393", "+296")
    assert (home["best_us"], away["best_us"]) == ("+130", "+225")
    assert not any(s["value_at_best"] for s in m["sides"])
    assert m["reading"] == "Ve más al local (+7 pts que Pinnacle)"
    assert m["real_bet"] is False and m["match_date"].startswith("2026-10-21T19:00")


def test_prediction_marks_real_bets_and_value_at_the_best_price():
    up = UPCOMING.assign(home_odds=2.60)                                  # +160 ≥ +141
    (m,) = V.predictions(pd.DataFrame([_mvs()]), up, {"bayern munchen vs arsenal"})
    assert m["real_bet"] is True and m["sides"][0]["value_at_best"] is True


def test_prediction_without_expected_goals_or_odds_still_shows():
    (m,) = V.predictions(pd.DataFrame([_mvs(lam=None, model=(0.44, 0.26, 0.30))]),
                         UPCOMING.iloc[0:0], set())
    assert m["lam"] is None and m["likely"] is None
    assert m["match"] == "Bayern Munchen vs Arsenal" or "vs" in m["match"]
    assert m["reading"] == "Coincide con el mercado" and m["sides"][0]["best_us"] == ""


def test_history_compares_the_program_with_pinnacle_on_real_results():
    mvs = pd.DataFrame([
        _mvs(when=pd.Timestamp("2026-10-20 19:00", tz="UTC")),                         # antes de la corrección
        _mvs(match="atlas vs america", when=pd.Timestamp("2026-10-23 01:00", tz="UTC"),
             model=(0.27, 0.24, 0.49), pin=(0.27, 0.24, 0.49)),                         # después
    ])
    results = pd.DataFrame([
        {"h": "bayern munchen", "a": "arsenal", "d": "2026-10-20", "home_goals": 1, "away_goals": 2},
        {"h": "atlas", "a": "america", "d": "2026-10-22", "home_goals": 0, "away_goals": 1},  # fecha local
    ])
    h = V.history(mvs, results)
    assert len(h["rows"]) == 2
    bay = next(r for r in h["rows"] if "Arsenal" in r["match"])
    assert bay["score"] == "1-2" and bay["outcome"] == "away"
    assert bay["model_hit"] is False and bay["pin_hit"] is False and bay["fixed"] is False
    assert h["all"]["n"] == 2 and h["fixed"]["n"] == 1 and h["fixed"]["model_hit"] == 1.0
    assert h["all"]["model_p_actual"] == pytest.approx((0.273 + 0.49) / 2, abs=1e-3)


def test_matches_without_result_are_left_out_of_the_history():
    h = V.history(pd.DataFrame([_mvs()]), pd.DataFrame(columns=["h", "a", "d", "home_goals", "away_goals"]))
    assert h["rows"] == [] and h["all"] is None


def test_proposals_summary_counts_real_prices_only():
    df = pd.DataFrame([
        {"match": "a vs b", "match_date": KICK, "league": "soccer_epl", "market": "home_win", "odds": 2.0,
         "probability": 0.55, "pin_prob": 0.50, "decision": "no le gana a Pinnacle por 2 pt", "result": "win"},
        {"match": "c vs d", "match_date": KICK, "league": "soccer_epl", "market": "away_win", "odds": 3.0,
         "probability": 0.40, "pin_prob": 0.33, "decision": "no le gana a Pinnacle por 2 pt", "result": "loss"},
        {"match": "e vs f", "match_date": KICK, "league": "soccer_epl", "market": "btts", "odds": 1.65,
         "probability": 0.60, "pin_prob": None, "decision": "mercado desactivado", "result": "win"},
        {"match": "g vs h", "match_date": KICK, "league": "soccer_epl", "market": "draw", "odds": 3.4,
         "probability": 0.30, "pin_prob": 0.27, "decision": "liga bloqueada", "result": "pending"},
    ])
    out = V.proposals(df)
    assert len(out["rows"]) == 4
    (pin,) = [g for g in out["summary"] if g["decision"] == "no le gana a Pinnacle por 2 pt"]
    assert (pin["n"], pin["won"], pin["profit_1u"]) == (2, 1, 0.0)
    assert not any(g["decision"] == "mercado desactivado" for g in out["summary"])   # momio inventado
    fake = next(r for r in out["rows"] if r["market"] and r["fake_odds"])
    assert fake["profit_1u"] == pytest.approx(0.65)
    pend = next(r for r in out["rows"] if r["result_key"] == "pending")
    assert pend["profit_1u"] is None and pend["odds_us"] == "+240"


def test_profit_at_one_unit_matches_the_settlement():
    assert V.profit_1u(2.0, "win") == 1.0 and V.profit_1u(2.0, "loss") == -1.0
    assert V.profit_1u(2.0, "half_win") == 0.5 and V.profit_1u(2.0, "half_loss") == -0.5
    assert V.profit_1u(2.0, "push") == 0.0 and V.profit_1u(2.0, "pending") is None


def test_update_banner_only_for_a_newer_release():
    from dashboard.app import _newer
    assert _newer("1.7.1", "1.7.0") and _newer("1.10.0", "1.9.9")
    assert not _newer("1.6.1", "1.7.0") and not _newer("1.7.0", "1.7.0")
    assert not _newer(None, "1.7.0") and not _newer("1.7.0", "dev")


def test_model_vs_sharp_rows_carry_expected_goals_for_the_dashboard():
    from src.models.side_bias import record
    r = record("a vs b", KICK, "soccer_epl", {"home_win": 0.5, "draw": 0.25, "away_win": 0.25,
                                              "over25": 0.6, "btts": 0.55},
               {"home": 0.45, "draw": 0.27, "away": 0.28}, lambdas=(1.7, 1.1))
    assert (r["lam_home"], r["lam_away"], r["p_over25"], r["p_btts"]) == (1.7, 1.1, 0.6, 0.55)
    r = record("a vs b", KICK, "soccer_epl", {"home_win": 0.5, "draw": 0.25, "away_win": 0.25},
               {"home": 0.45, "draw": 0.27, "away": 0.28})
    assert r["lam_home"] is None and r["p_btts"] is None


def test_new_endpoints_answer_with_synthetic_data(monkeypatch):
    import dashboard.app as dash
    mvs = pd.DataFrame([_mvs()])

    def fake_q(sql, params=None):
        s = " ".join(str(sql).split())
        if "FROM model_vs_sharp" in s:
            return mvs
        if "FROM upcoming_matches" in s:
            return UPCOMING
        if "FROM bets_history" in s:
            return pd.DataFrame(columns=["match"])
        if "FROM matches" in s:
            return pd.DataFrame([{"h": "bayern munchen", "a": "arsenal", "d": "2026-10-21",
                                  "home_goals": 2, "away_goals": 2}])
        if "FROM playdoit_prices" in s:
            return pd.DataFrame()
        if "FROM model_proposals" in s:
            return pd.DataFrame([{"match": "a vs b", "match_date": KICK, "league": "soccer_epl",
                                  "market": "home_win", "odds": 2.0, "probability": 0.55, "pin_prob": 0.5,
                                  "decision": "no le gana a Pinnacle por 2 pt", "result": "loss"}])
        raise AssertionError(f"SQL inesperado: {s[:100]}")
    monkeypatch.setattr(dash, "_q", fake_q)
    c = dash.app.test_client()
    p = c.get("/api/predictions").get_json()
    assert p["ok"] and p["matches"][0]["match"] == "Bayern Munich vs Arsenal"
    h = c.get("/api/predictions/history?days=7").get_json()
    assert h["ok"] and h["rows"][0]["score"] == "2-2" and h["days"] == 7
    pr = c.get("/api/proposals").get_json()
    assert pr["ok"] and pr["summary"][0]["profit_1u"] == -1.0
    html = c.get("/").get_data(as_text=True)
    for el in ("todaylist", "nextlist", "histbody", "histkpis", "propsum", "propbody"):   # v1.9.0: pestañas
        assert f'id="{el}"' in html


def test_same_match_under_an_old_alias_shows_once_with_fresh_pinnacle():
    from src.utils.team_normalizer import normalize_team
    old = {**_mvs(match="santos laguna vs pumas unam"), "created_at": pd.Timestamp("2026-10-21 06:00", tz="UTC")}
    new = {**_mvs(match="santos laguna vs unam pumas"), "created_at": pd.Timestamp("2026-10-21 12:00", tz="UTC")}
    up = pd.DataFrame([{"home_team_norm": "santos laguna", "away_team_norm": "unam pumas",
                        "home_team": "Santos Laguna", "away_team": "Pumas", "home_odds": 2.5,
                        "draw_odds": 3.4, "away_odds": 3.0, "pin_home_odds": 2.40,
                        "pin_draw_odds": 3.60, "pin_away_odds": 3.00}])
    out = V.predictions(pd.DataFrame([old, new]), up, set(), normalize_team)
    assert len(out) == 1 and out[0]["match"] == "Santos Laguna vs Pumas"
    pin = [s["pin"] for s in out[0]["sides"]]
    assert pin[0] == pytest.approx((1 / 2.4) / (1 / 2.4 + 1 / 3.6 + 1 / 3.0), abs=1e-6)   # el de la última recarga


def test_rows_without_expected_goals_are_flagged_as_the_old_model():
    (m,) = V.predictions(pd.DataFrame([_mvs(lam=None)]), UPCOMING, set())
    assert m["old_model"] is True
    (m,) = V.predictions(pd.DataFrame([_mvs()]), UPCOMING, set())
    assert m["old_model"] is False
