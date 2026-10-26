"""
Dashboard v1.10.0 y tarjetas con la roja (21-oct-26).

El dueño: "vale 1" (la roja cuenta 1 tarjeta en PlayDoit), "quiero tener
visibilidad sobre quién tuvo razón en cada apuesta… si es un ambos anotan
quiero ver quién ganó de los 2" y "quiero abrir el partido y ver sus últimos
5 encuentros, con resultados, goles, tiros de esquina y la alineación".
Sin DB.
"""

import json

import pandas as pd
import pytest

from dashboard import match_detail as MD
from dashboard import visibility as V

KICK = pd.Timestamp("2026-10-24 16:30", tz="UTC")


# ─── tarjetas: amarilla = 1, roja = 1 ────────────────────────────────────────

def _row(**kw):
    base = {"home_goals": 1, "away_goals": 0, "home_yellow": 2, "away_yellow": 2, "home_red": 1, "away_red": 0}
    return pd.Series({**base, **kw})


def test_a_red_card_counts_one_card_like_playdoit():
    from src.models.save_bets import card_total, resolve_market
    assert card_total(_row()) == 5
    assert card_total(_row(home_red=None)) == 4                      # sin dato de roja: 0
    # 2+2 amarillas y una roja: más de 4.5 gana (antes, solo amarillas, perdía)
    assert resolve_market("cards_over_4.5", _row(), 2.0, 1.0)[0] == "win"
    assert resolve_market("cards_under_4.5", _row(), 1.8, 1.0)[0] == "loss"
    assert resolve_market("cards_over_4.5", _row(home_red=0), 2.0, 1.0)[0] == "loss"


def test_the_daily_check_counts_the_red_card_too():
    from src.utils.output_checks import settle_independent
    r = {"home_goals": 1, "away_goals": 0, "home_yellow": 2, "away_yellow": 2, "home_red": 1, "away_red": None}
    assert settle_independent("cards_over_4.5", r) == "win"
    assert settle_independent("cards_under_5.0", r) == "push"


def test_card_models_read_yellow_plus_red(monkeypatch):
    import src.features.cards_stats as cs
    import src.models.props_fair as PF
    seen = []

    def fake_read_sql(sql, con=None, params=None, **kw):
        seen.append(" ".join(str(sql).split()))
        return pd.DataFrame(columns=["date", "cards_for", "cards_against"])
    monkeypatch.setattr(cs.pd, "read_sql", fake_read_sql)
    cs.get_team_cards("alpha")
    assert "home_yellow + COALESCE(home_red, 0)" in seen[-1]

    monkeypatch.setattr(PF.pd, "read_sql", lambda sql, con=None, **kw: (seen.append(" ".join(str(sql).split())) or
                        pd.DataFrame(columns=["date", "league", "home", "away", "home_cards", "away_cards",
                                              "home_corners", "away_corners"])))
    PF.load_history(engine=object())
    assert "home_yellow + COALESCE(home_red, 0) AS home_cards" in seen[-1]
    assert PF.PROPS["cards"]["cols"] == ("home_cards", "away_cards")


# ─── ¿quién tuvo razón? ──────────────────────────────────────────────────────

@pytest.mark.parametrize("model,market,result,who", [
    (0.75, 0.60, "win", "programa"),      # ambos anotan: pasó y el programa le daba más
    (0.75, 0.60, "loss", "mercado"),      # no pasó: el mercado le daba menos
    (0.40, 0.55, "win", "mercado"),
    (0.40, 0.55, "loss", "programa"),
    (0.75, 0.60, "push", None), (0.75, None, "win", None), (0.75, 0.60, "pending", None),
])
def test_who_was_right_is_who_gave_more_probability_to_what_happened(model, market, result, who):
    assert V.who_was_right(model, market, result) == who


def _pick(result, model=0.75, market=0.60, label="Ambos anotan"):
    return {"match": "chelsea vs tottenham", "match_date": KICK, "league": "soccer_epl", "display": "Chelsea vs Tottenham",
            "label": label, "model_prob": model, "market_prob": market, "lean": True, "min_us": -110,
            "playdoit_odds": 1.8, "value": False, "result": result, "profit": 0.8 if result == "win" else -1.0}


def test_picks_board_says_who_was_right_in_each_pick():
    b = V.picks_board(pd.DataFrame([_pick("win"), _pick("loss"), _pick("loss", 0.40, 0.55), _pick(None)]))
    assert [r["winner"] for r in b["rows"]] == ["programa", "mercado", "programa", None]
    assert b["right"] == {"programa": 2, "mercado": 1}
    assert b["rows"][0]["key"] == "chelsea vs tottenham"            # para abrir el partido


def test_proposals_say_who_was_right_against_pinnacle_and_pending_is_not_nan():
    df = pd.DataFrame([
        {"match": "a vs b", "match_date": KICK, "league": "soccer_epl", "market": "btts", "odds": 1.8,
         "probability": 0.62, "pin_prob": 0.55, "decision": "no le gana a Pinnacle por 2 pt", "result": "win"},
        {"match": "c vs d", "match_date": KICK, "league": "soccer_epl", "market": "home_win", "odds": 2.1,
         "probability": 0.50, "pin_prob": 0.45, "decision": "liga bloqueada", "result": float("nan")},
    ])
    out = V.proposals(df)
    assert [r["winner"] for r in out["rows"]] == ["programa", None]
    assert out["rows"][1]["result_key"] == "pending" and out["rows"][1]["result"] == "Pendiente"
    assert out["right"] == {"programa": 1, "mercado": 0}


# ─── el ángulo de goles (v2.4.7) ────────────────────────────────────────────

def _detail(ga_home, ga_away, n=5):
    return {"home": {"team": "LAFC", "summary": {"n": n, "goals_against": ga_home}},
            "away": {"team": "LA Galaxy", "summary": {"n": n, "goals_against": ga_away}}}


def _mk(market, model, market_prob):
    return {"market": market, "label": market, "family": "Goles", "model": model,
            "market_prob": market_prob, "gap": round(model - market_prob, 3)}


WIN = _mk("home_win", 0.62, 0.55)


def test_goals_angle_only_for_a_team_that_concedes_two_or_more_per_match():
    mk = [WIN, _mk("over25", 0.58, 0.61), _mk("btts", 0.55, 0.54)]
    assert MD.goals_angle(_detail(1.0, 1.8), mk) is None
    assert MD.goals_angle(_detail(1.0, 2.9, n=3), mk) is None         # 3 partidos no alcanzan
    a = MD.goals_angle(_detail(1.0, 2.9), mk)
    assert [t["team"] for t in a["teams"]] == ["LA Galaxy"] and a["teams"][0]["goals_against"] == 2.9
    assert [m["market"] for m in a["markets"]] == ["over25", "btts"]    # solo goles, no el ganador


def test_goals_alternative_only_where_the_program_also_sees_more_than_the_market():
    # LAFC–Galaxy: el mercado ya cobra esos goles y el programa no ve más → sin alternativa
    assert MD.goals_angle(_detail(1.0, 2.9), [WIN, _mk("over25", 0.58, 0.61), _mk("btts", 0.55, 0.54)])["alternative"] is None
    # el programa ve +5 pt en ambos anotan → esa es la alternativa
    alt = MD.goals_angle(_detail(1.0, 2.9), [WIN, _mk("over25", 0.58, 0.61), _mk("btts", 0.60, 0.55)])["alternative"]
    assert alt["market"] == "btts"
    # con diferencia pero el programa no lo cree (menos de 50%) → no
    assert MD.goals_angle(_detail(2.2, 2.9), [_mk("over25", 0.48, 0.42)])["alternative"] is None
    # sin momios de goles: el ángulo se ve, sin mercados
    assert MD.goals_angle(_detail(2.2, 1.0), [WIN])["markets"] == []


# ─── el partido abierto ──────────────────────────────────────────────────────

def _m(date, h, a, hg, ag, hc=5, ac=4, hy=2, ay=1, hr=0, ar=0, league="soccer_epl"):
    return {"date": pd.Timestamp(date), "league": league, "h": h, "a": a, "home_goals": hg, "away_goals": ag,
            "home_corners": hc, "away_corners": ac, "home_yellow": hy, "away_yellow": ay, "home_red": hr, "away_red": ar}


MATCHES = pd.DataFrame([
    _m("2026-10-17", "everton", "chelsea", 0, 2, hc=5, ac=7, hr=1),
    _m("2026-10-10", "chelsea", "bournemouth", 5, 1, hc=1, ac=7),
    _m("2026-09-18", "brentford", "chelsea", 3, 0),
    _m("2026-09-12", "chelsea", "hull city", 2, 2, hc=0, ac=0),          # 0-0 en córners: hueco de la fuente
    _m("2026-09-06", "arsenal", "chelsea", 2, 1),
    _m("2026-08-30", "chelsea", "fulham", 1, 0),                        # el sexto: fuera
    _m("2026-10-19", "tottenham", "coventry city", 3, 1),
    _m("2026-05-19", "chelsea", "tottenham", 2, 1, hc=3, ac=4),
    _m("2026-10-25", "chelsea", "leeds", 9, 0),                         # después del partido: no entra
])


def _lu(team, kickoff, formation, ids, names):
    return {"team": team, "kickoff": pd.Timestamp(kickoff, tz="UTC"), "formation": formation,
            "starters": json.dumps(ids), "names": json.dumps(names)}


NAMES = {str(i): f"Jugador {i}" for i in range(1, 15)}
LINEUPS = pd.DataFrame(
    [_lu("chelsea", f"2026-{d}", "4-2-3-1", list(range(1, 12)), NAMES)
     for d in ("10-17 14:00", "10-10 14:00", "09-18 19:00", "09-12 14:00")] +
    [_lu("tottenham", "2026-10-24 15:35", "3-4-3", list(range(4, 15)), NAMES)])     # confirmada (~1 h antes)


def test_last_five_with_results_goals_corners_cards_and_xi():
    d = MD.match_detail(MATCHES, LINEUPS, "chelsea", "tottenham", KICK)
    ch = d["home"]
    assert [m["opponent"] for m in ch["matches"]] == ["Everton", "Bournemouth", "Brentford", "Hull City", "Arsenal"]
    ev = ch["matches"][0]
    assert (ev["venue"], ev["score"], ev["result"], ev["corners"], ev["cards"]) == ("V", "2-0", "G", [7, 5], 4)
    assert ch["matches"][3]["corners"] is None                          # el 0-0 no se muestra como dato
    assert ch["matches"][0]["xi"]["players"][0] == "Jugador 1" and ch["matches"][4]["xi"] is None
    assert ch["summary"]["w"] == 2 and ch["summary"]["goals_for"] == 2.0
    # 2-0, 5-1, 0-3, 2-2, 1-2: más de 2.5 en 4 de 5 y ambos anotan en 3 (26-oct-26, "a simple vista")
    assert (ch["summary"]["over25"], ch["summary"]["btts"]) == (4, 3)
    assert ch["summary"]["corners_for"] == pytest.approx((7 + 1 + 4 + 4) / 4)       # de visitante cuenta la columna away


def test_match_lineup_confirmed_or_probable():
    d = MD.match_detail(MATCHES, LINEUPS, "chelsea", "tottenham", KICK)
    assert d["away"]["lineup"]["confirmed"] is True and d["away"]["lineup"]["formation"] == "3-4-3"
    pr = d["home"]["lineup"]
    assert pr["confirmed"] is False and pr["from"] == 4 and pr["players"][0] == "Jugador 1 (4 de 4)"
    assert MD.match_detail(MATCHES, LINEUPS.iloc[:0], "chelsea", "tottenham", KICK)["home"]["lineup"] is None


def test_head_to_head_only_before_the_match():
    h2h = MD.match_detail(MATCHES, LINEUPS, "chelsea", "tottenham", KICK)["h2h"]
    assert [(x["match"], x["score"], x["corners"]) for x in h2h] == [("Chelsea vs Tottenham", "2-1", [3, 4])]


def test_match_endpoint_and_predictions_carry_the_key(monkeypatch):
    import dashboard.app as dash
    seen = []

    def fake_q(sql, params=None, optional=False):
        s = " ".join(str(sql).split())
        seen.append(params)
        if "FROM matches" in s:
            return MATCHES
        if "FROM lineups" in s:
            return LINEUPS
        if "FROM model_vs_sharp" in s:               # sin análisis: la vista de mercados vacía (v2.4.6)
            return pd.DataFrame()
        if "FROM espn_matches" in s:                 # copas y amistosos (22-oct-26): ninguno aquí
            return pd.DataFrame()
        raise AssertionError(s[:80])
    monkeypatch.setattr(dash, "_q", fake_q)
    c = dash.app.test_client()
    d = c.get("/api/match?home=Chelsea&away=tottenham&date=2026-10-24T16:30:00%2B00:00").get_json()
    assert d["ok"] and d["home"]["team"] == "Chelsea" and len(d["home"]["matches"]) == 5
    assert seen[0]["h"] == "chelsea" and str(seen[0]["d"]).startswith("2026-10-24 16:30")
    assert c.get("/api/match?home=chelsea").get_json()["ok"] is False
    assert d["goals_angle"] is None                  # Chelsea recibe 1.6 por partido: sin ángulo de goles

    mvs = pd.DataFrame([{"match": "chelsea vs tottenham", "match_date": KICK, "league": "soccer_epl",
                         "p_home": 0.66, "p_draw": 0.19, "p_away": 0.15, "pin_home": 0.57, "pin_draw": 0.22,
                         "pin_away": 0.21, "lam_home": 2.4, "lam_away": 1.0, "p_over25": 0.63, "p_btts": 0.58}])
    (m,) = V.predictions(mvs, pd.DataFrame(columns=["home_team_norm", "away_team_norm"]), set())
    assert m["key"] == "chelsea vs tottenham"
