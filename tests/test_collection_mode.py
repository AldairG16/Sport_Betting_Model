"""
Modo recolección (25-sep-26): dinero real solo con valor frente a Pinnacle,
mercados sin referencia en sombra hasta tener evidencia, corrección del
sesgo por lado del modelo y cuota mínima de PlayDoit con el piso de Pinnacle.

Medido ese día: 7 de 11 apuestas pendientes tenían valor negativo contra
Pinnacle aun a la mejor cuota europea, y el modelo daba +5.1 pt al local.
"""

import json

import pandas as pd
import pytest

from src.utils.min_odds import min_american, playdoit_line


# ============================================================
# Cuota mínima: la probabilidad más conservadora (modelo o Pinnacle)
# ============================================================

def test_floor_uses_the_more_conservative_probability():
    # modelo 60 %: bastaba -138; Pinnacle dice 55 %: hace falta -112 o mejor
    assert min_american(0.60) == -138
    assert min_american(0.60, pin_prob=0.55) == -112
    assert min_american(0.60, pin_prob=0.65) == -138                      # el modelo más bajo: manda
    for bad in (None, "x", 0, 1.2, float("nan")):
        assert min_american(0.60, pin_prob=bad) == -138
    assert "paga -112 o mejor" in playdoit_line(0.60, pin_prob=0.55)


PICK = {"match": "alpha vs beta", "league": "soccer_spain_la_liga", "market": "home_win",
        "probability": 0.60, "pin_prob": 0.55, "odds": 1.92, "edge": 0.08, "stake": 1.0,
        "match_date": "2026-09-26T19:00:00+00:00"}


def test_every_message_uses_the_pinnacle_floor():
    from scripts.notify_telegram import _build_bets_by_league, _tomorrow_line
    from scripts.revalidate_pending_bets import _format_confirmations
    msg, n = _build_bets_by_league(pd.DataFrame([PICK]), "HOY")
    assert n == 1 and "paga -112 o mejor" in msg
    assert "paga -112 o mejor" in _format_confirmations([PICK])
    assert "PlayDoit -112 o mejor" in _tomorrow_line(pd.Series(PICK))


def test_no_picks_message_says_not_to_bet():
    import scripts.notify_telegram as nt
    assert "No apuestes nada" in nt.NO_PICKS_TEXT and "Pinnacle" in nt.NO_PICKS_TEXT


# ============================================================
# Sesgo por lado (src/models/side_bias.py)
# ============================================================

MODEL = {"home_win": 0.60, "draw": 0.25, "away_win": 0.15,
         "dc_1x": 0.85, "dc_x2": 0.40, "dc_12": 0.75,
         "dnb_home": 0.80, "dnb_away": 0.20,
         "ah_home_-0.50": 0.60, "ah_away_-0.50": 0.40,
         "ah_home_+0.50": 0.85, "ah_away_+0.50": 0.15,
         "over25": 0.55}
BIAS = {"home": 0.05, "draw": -0.02, "away": -0.03}


def test_debias_moves_1x2_and_its_derivatives_consistently():
    from src.models.side_bias import debias
    out, shift = debias(MODEL, BIAS)
    assert (out["home_win"], out["draw"], out["away_win"]) == pytest.approx((0.55, 0.27, 0.18))
    assert shift == pytest.approx({"home": -0.05, "draw": 0.02, "away": 0.03})
    assert out["dc_1x"] == pytest.approx(0.82) and out["dc_x2"] == pytest.approx(0.45)
    assert out["dc_12"] == pytest.approx(0.73)
    assert out["ah_home_-0.50"] == pytest.approx(0.55)                     # = gana el local
    assert out["ah_away_-0.50"] == pytest.approx(0.45)
    assert out["ah_home_+0.50"] == pytest.approx(0.82)                     # = no pierde el local
    assert out["ah_away_+0.50"] == pytest.approx(0.18)
    dnb = 0.55 / (0.55 + 0.18) - 0.60 / 0.75                              # P(local | no empate)
    assert out["dnb_home"] == pytest.approx(0.80 + dnb)
    assert out["dnb_away"] == pytest.approx(0.20 - dnb)
    assert out["over25"] == 0.55                                          # goles: sin tocar
    assert MODEL["home_win"] == 0.60                                      # no muta la entrada


def test_without_learned_bias_nothing_changes():
    from src.models.side_bias import debias
    out, shift = debias(MODEL, {})
    assert out == MODEL and shift == {"home": 0.0, "draw": 0.0, "away": 0.0}


def _pairs(n, home_bias=0.05):
    """n partidos donde el modelo da `home_bias` de más al local y se lo
    quita por igual al empate y al visitante."""
    return pd.DataFrame([{"match": f"m{i}", "match_date": "2026-09-26", "league": "x",
                          "p_home": 0.50 + home_bias, "p_draw": 0.25 - home_bias / 2,
                          "p_away": 0.25 - home_bias / 2,
                          "pin_home": 0.50, "pin_draw": 0.25, "pin_away": 0.25}
                         for i in range(n)])


def test_learning_shrinks_and_limits_the_weekly_step():
    from src.models.side_bias import MAX_STEP, SHRINK_N, learn
    st = learn(_pairs(100))                  # local +5 pt, empate y visitante −2.5 pt
    assert (st["source"], st["n"]) == ("datos", 100)
    assert st["stats"]["home"]["mean"] == pytest.approx(0.05)
    shrink = 100 / (100 + SHRINK_N)          # con n = 100 el dato pesa 62.5 %
    assert 0.05 * shrink > MAX_STEP          # 3.1 pt: la primera semana corta en 3
    assert st["home"] == pytest.approx(MAX_STEP)
    assert st["draw"] == st["away"] == pytest.approx(-0.025 * shrink, abs=1e-5)
    # la semana siguiente llega al valor encogido
    st2 = learn(_pairs(100), previous=st)
    assert st2["home"] == pytest.approx(0.05 * shrink, abs=1e-5)


def test_learning_never_corrects_more_than_the_cap():
    from src.models.side_bias import MAX_ABS, learn
    st = learn(_pairs(1000, home_bias=0.20), previous={"home": 0.07, "draw": -0.035, "away": -0.035})
    assert st["home"] == pytest.approx(MAX_ABS)
    assert all(abs(st[s]) <= MAX_ABS + 1e-12 for s in ("home", "draw", "away"))


def test_learning_keeps_the_previous_value_with_little_data():
    from src.models.side_bias import learn
    st = learn(_pairs(10), previous={"home": 0.02, "draw": -0.01, "away": -0.01})
    assert (st["home"], st["source"]) == (0.02, "anterior")
    assert learn(_pairs(10))["home"] == 0.0                              # sin anterior: sin corrección


def test_weekly_step_reports_only_when_the_correction_moves(monkeypatch):
    import scripts.notify_telegram as nt
    import scripts.orchestrator as orch
    import src.models.side_bias as sb
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    zero = {"home": 0.0, "draw": 0.0, "away": 0.0}
    monkeypatch.setattr(sb, "load_side_bias", lambda: dict(zero))
    monkeypatch.setattr(sb, "run_side_bias", lambda verbose=True: {**zero, "n": 12, "source": "prior"})
    orch.step_side_bias()
    assert sent == []                                                     # sin novedad: silencio
    monkeypatch.setattr(sb, "run_side_bias", lambda verbose=True: {
        "home": 0.03, "draw": -0.01, "away": -0.02, "n": 80, "source": "datos",
        "stats": {"home": {"mean": 0.05}, "draw": {"mean": -0.02}, "away": {"mean": -0.03}}})
    orch.step_side_bias()
    assert len(sent) == 1 and "SESGO POR LADO" in sent[0] and "+3.0pt" in sent[0]


# ============================================================
# El pipeline: filtro Pinnacle y corrección del sesgo
# ============================================================

def _with_pinnacle(home, draw, away):
    """Partidos del arnés con precio 1X2 de Pinnacle solo en alpha vs beta
    (donde el sistema apuesta al local a 1.80)."""
    from tests.pipeline_harness import default_matches
    m = default_matches()
    for c, v in (("pin_home_odds", home), ("pin_draw_odds", draw), ("pin_away_odds", away)):
        m[c] = [v] + [None] * (len(m) - 1)
    return m


def test_bet_with_value_against_pinnacle_goes_through(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    # Pinnacle 1.60 / 4.00 / 6.00 → local ≈ 60 %: a 1.80 (55.6 %) sí hay valor
    cap = run_pipeline(monkeypatch, _with_pinnacle(1.60, 4.00, 6.00), sharp_gate=True)
    assert [(b["match"], b["market"]) for b in cap["bets"]] == [("alpha vs beta", "home_win")]
    log = json.loads(cap["bets"][0]["decision_log"])
    assert log["market_ctx"]["pin_prob"] > 1 / 1.80 + 0.02


def test_bet_without_value_against_pinnacle_is_not_placed(monkeypatch):
    from tests.pipeline_harness import default_matches, run_pipeline
    key = ("alpha vs beta", "home_win")
    # sin precio de Pinnacle (y su grupo reactivado) el sistema apuesta al local a 1.80
    plain = run_pipeline(monkeypatch, default_matches())
    assert key in {(b["match"], b["market"]) for b in plain["bets"]}
    # Pinnacle 2.00 / 3.40 / 3.60 → local ≈ 47 %: a 1.80 (55.6 %) no hay valor real.
    # Con precio de Pinnacle el filtro corre siempre, haya o no reactivaciones.
    gated = run_pipeline(monkeypatch, _with_pinnacle(2.00, 3.40, 3.60))
    assert key not in {(b["match"], b["market"]) for b in gated["bets"]}
    assert run_pipeline(monkeypatch, _with_pinnacle(2.00, 3.40, 3.60), sharp_gate=True)["bets"] == []
    # la medición no cambia: las mismas candidatas quedan en sombra, con su referencia
    assert len(gated["shadow"]) == len(plain["shadow"]) > 0
    pins = {(s["match"], s["market"]): s["pin_prob"] for s in gated["shadow"]}
    assert pins.get(key) == pytest.approx(0.47, abs=0.02)


def test_markets_without_reference_wait_for_evidence(monkeypatch):
    """Sin precio de Pinnacle: sin dinero real, salvo que su grupo esté reactivado."""
    from tests.pipeline_harness import default_matches, run_pipeline
    assert run_pipeline(monkeypatch, default_matches(), sharp_gate=True)["bets"] == []
    reactivated = run_pipeline(monkeypatch, default_matches(), sharp_gate=True,
                               learned={"reactivated": {"sinref:1x2", "sinref:ah_dnb"}})
    assert sorted((b["match"], b["market"]) for b in reactivated["bets"]) == [
        ("alpha vs beta", "home_win"), ("epsilon vs zeta", "dnb_away"), ("iota vs kappa", "home_win")]


def test_every_match_with_pinnacle_is_recorded_raw_for_the_side_bias(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    cap = run_pipeline(monkeypatch, _with_pinnacle(1.60, 4.00, 6.00),
                       learned={"side_bias": BIAS})
    (rec,) = cap["model_sharp"]                          # solo alpha vs beta tiene Pinnacle
    assert rec["match"] == "alpha vs beta"
    assert rec["p_home"] + rec["p_draw"] + rec["p_away"] == pytest.approx(1.0, abs=0.02)
    assert rec["pin_home"] > rec["pin_draw"] > rec["pin_away"]
    # CRUDO: antes de descontar el sesgo (si no, aprendería de su propia corrección)
    plain = run_pipeline(monkeypatch, _with_pinnacle(1.60, 4.00, 6.00))["model_sharp"][0]
    assert rec["p_home"] == pytest.approx(plain["p_home"])


def test_learned_side_bias_is_applied_before_the_anchor(monkeypatch):
    from tests.pipeline_harness import default_matches, run_pipeline
    base = run_pipeline(monkeypatch, default_matches())
    fixed = run_pipeline(monkeypatch, default_matches(), learned={"side_bias": BIAS})
    p = {(b["match"], b["market"]): b["probability"] for b in base["bets"]}
    q = {(b["match"], b["market"]): b for b in fixed["bets"]}
    key = ("alpha vs beta", "home_win")
    assert key in q and q[key]["probability"] < p[key]                    # el local baja
    log = json.loads(q[key]["decision_log"])
    assert log["model"]["side_bias"]["home"] < -0.03
    assert log["model"]["side_bias"]["away"] > 0


# ============================================================
# Reactivación de los mercados sin referencia (clv_gate)
# ============================================================

def test_no_reference_candidates_feed_their_own_reactivation_group():
    from scripts.clv_gate import aggregate_reactivation_stats
    new, old = "2026-09-26T12:00:00+00:00", "2026-09-24T12:00:00+00:00"
    df = pd.DataFrame([
        {"market": "btts", "odds": 2.0, "closing_odds": 1.9, "pin_prob": None, "created_at": new},
        {"market": "btts_no", "odds": 2.0, "closing_odds": 2.1, "pin_prob": None, "created_at": new},
        # con referencia: no cuenta como "sin referencia"
        {"market": "home_win", "odds": 2.0, "closing_odds": 1.8, "pin_prob": 0.52, "created_at": new},
        # grupo fijo
        {"market": "away_win", "odds": 3.0, "closing_odds": 2.8, "pin_prob": 0.35, "created_at": new},
        # antes del 25-sep no se guardaba pin_prob: vacío no es "sin referencia"
        {"market": "under25", "odds": 2.0, "closing_odds": 1.9, "pin_prob": None, "created_at": old},
    ])
    st = aggregate_reactivation_stats(df)
    assert st["sinref:btts"][0] == 2 and "sinref:1x2" not in st
    assert "sinref:totals" not in st
    assert st["away_win"][0] == 1
    assert aggregate_reactivation_stats(df.iloc[:0]) == {}


def test_reactivation_state_includes_the_no_reference_groups():
    from scripts.clv_gate import NO_REF_REACTIVABLE, merge_reactivation_state
    st = merge_reactivation_state({}, {"sinref:btts": (40, 0.004, 0.02),
                                       "sinref:corners_cards": (10, 0.05, 0.02)})
    assert set(NO_REF_REACTIVABLE) <= set(st)
    assert st["sinref:btts"] is True                    # n ≥ 30 y CLV ≥ 0
    assert st["sinref:corners_cards"] is False          # muestra corta: sigue en sombra


# ============================================================
# Dashboard: canceladas visibles y piso de Pinnacle
# ============================================================

def test_dashboard_shows_cancelled_bets_and_the_pinnacle_floor(monkeypatch):
    import dashboard.app as dash
    rows = pd.DataFrame([
        {"id": 1, "match_date": pd.Timestamp("2026-09-24 15:00", tz="UTC"), "match": "a vs b",
         "league": "soccer_epl", "market": "home_win", "probability": 0.60, "odds": 1.8,
         "stake": 1.0, "result": "stale", "profit": 0.0, "closing_odds": None, "clv": None,
         "pin_prob": 0.55, "cancelled": True},
        {"id": 2, "match_date": pd.Timestamp("2026-09-25 15:00", tz="UTC"), "match": "c vs d",
         "league": "soccer_epl", "market": "home_win", "probability": 0.60, "odds": 1.8,
         "stake": 1.0, "result": "pending", "profit": None, "closing_odds": None, "clv": None,
         "pin_prob": None, "cancelled": False},
    ])
    seen = []
    monkeypatch.setattr(dash, "_q", lambda sql, params=None: seen.append(sql) or rows.copy())
    got = dash.app.test_client().get("/api/bets").get_json()["bets"]
    assert (got[0]["result"], got[0]["result_key"]) == ("Cancelada", "cancelled")
    assert got[0]["min_us"] == "-112"                                     # piso de Pinnacle
    assert got[1]["min_us"] == "-138"                                     # sin Pinnacle: el modelo
    # la vista por defecto incluye las canceladas; la de histórico muerto, no
    assert "OR " + dash.CANCELLED_SQL in seen[0]
    dash.app.test_client().get("/api/bets?status=stale")
    assert "AND NOT " + dash.CANCELLED_SQL in seen[1]
