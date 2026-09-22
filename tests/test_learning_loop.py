"""
tests/test_learning_loop.py
===========================
Tests de COMPORTAMIENTO del ciclo de aprendizaje (22-sep-26):

- Lo aprendido por el weekly llega a la corrida diaria (load_learned_state).
- El peso del modelo frente al mercado se aprende del CLV shadow
  (anchor_learner) y mueve las probabilidades en la dirección correcta.
- Los bloqueos fijos vuelven solo con evidencia shadow.
- Los datos con los que se aprende (cuotas de cierre) son correctos.
- La selección de apuestas es coherente (sin fallback `or`, un solo bet
  por resultado, edge = prob − 1/odds).

A diferencia de los tests de rondas anteriores, aquí no se busca texto en
el código: se ejecuta.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.pipeline_harness import run_pipeline, _row


# ============================================================
# Pipeline de punta a punta
# ============================================================

def _bets(out):
    return {(b["match"], b["market"]): b for b in out["bets"]}


def test_filter_rejecting_everything_means_no_bet(monkeypatch):
    """Si el filtro rechaza TODAS las candidatas, no se apuesta. Antes el
    `or raw_bets` apostaba entonces la lista sin filtrar."""
    import src.pipeline.prediction_pipeline as pp
    monkeypatch.setattr(pp, "market_intelligence_filter", lambda bets: [])
    out = run_pipeline(monkeypatch)
    assert out["bets"] == []


def test_stored_edge_is_coherent_with_probability(monkeypatch):
    """edge guardado = probabilidad − 1/cuota, también tras la señal de
    línea (antes se escalaban por separado y quedaban incoherentes)."""
    out = run_pipeline(monkeypatch)
    assert out["bets"], "el arnés debe producir apuestas"
    for b in out["bets"]:
        assert b["edge"] == pytest.approx(b["probability"] - 1.0 / b["odds"], abs=1e-9)


def test_at_most_one_bet_per_result_group(monkeypatch):
    """AH, DNB y 1X2 apuestan al mismo resultado: máximo uno por partido."""
    import src.pipeline.prediction_pipeline as pp
    out = run_pipeline(monkeypatch, learned={"reactivated": {"ah_home_fav", "ah_away_dog"}})
    seen = {}
    for b in out["bets"]:
        g = pp._exclusive_group(b["market"])
        if g:
            assert (b["match"], g) not in seen, f"doble bet en {b['match']} grupo {g}"
            seen[(b["match"], g)] = b["market"]


def test_ah_home_favorite_blocked_but_still_observed(monkeypatch):
    """El AH del local favorito no se apuesta, pero el shadow lo mide
    (la reactivación necesita esos datos)."""
    out = run_pipeline(monkeypatch)
    assert not any(b["market"].startswith("ah_home_-") for b in out["bets"])
    assert any(s["match"] == "alpha vs beta" and s["market"] == "ah_home_-0.50"
               for s in out["shadow"])


def test_shadow_reactivation_unblocks_ah_group(monkeypatch):
    out = run_pipeline(monkeypatch, learned={"reactivated": {"ah_home_fav"}})
    bets = _bets(out)
    assert ("alpha vs beta", "ah_home_-0.50") in bets
    assert ("alpha vs beta", "home_win") not in bets   # mismo resultado


def test_away_win_needs_shadow_evidence(monkeypatch):
    # cuota 2.60: edge de away_win ≈ 7pt, sobre el piso operativo de 5pt
    m = pd.DataFrame([_row("epsilon", "zeta", "soccer_brazil_campeonato",
                           home_odds=3.40, draw_odds=3.40, away_odds=2.60,
                           over25_odds=2.00, under25_odds=1.85,
                           btts_yes_odds=1.90, btts_no_odds=1.90)])
    # dnb_away bloqueado para que no ocupe el grupo 1X2 antes que away_win
    base = {"blocked_markets": {"dnb_away"}}
    out = run_pipeline(monkeypatch, matches=m, learned=base)
    assert ("epsilon vs zeta", "away_win") not in _bets(out)

    out = run_pipeline(monkeypatch, matches=m,
                       learned={**base, "reactivated": {"away_win"}})
    assert ("epsilon vs zeta", "away_win") in _bets(out)


def test_learned_anchor_weight_moves_probabilities(monkeypatch):
    fams = ("1x2", "totals", "btts", "ah_dnb", "halftime", "corners_cards")
    base = _bets(run_pipeline(monkeypatch))
    high = _bets(run_pipeline(monkeypatch, learned={
        "anchor": {"families": {f: {"weight": 0.50} for f in fams}}}))
    low = _bets(run_pipeline(monkeypatch, learned={
        "anchor": {"families": {f: {"weight": 0.10} for f in fams}}}))
    key = ("alpha vs beta", "home_win")
    assert high[key]["probability"] > base[key]["probability"]
    # con el modelo casi sin peso, su opinión ya no genera value
    assert len(low) < len(base)


def test_learned_blocks_apply(monkeypatch):
    out = run_pipeline(monkeypatch, learned={"blocked_leagues": {"soccer_spain_la_liga"},
                                             "blocked_markets": {"dnb_away"}})
    matches = {b["match"] for b in out["bets"]}
    assert "alpha vs beta" not in matches
    assert not any(b["market"] == "dnb_away" for b in out["bets"])


def test_load_learned_state_reads_every_source(monkeypatch):
    """Reemplaza al test de carga import-time (ronda 4, H3): el estado se
    lee en cada corrida, de la DB, vía los loaders de cada módulo."""
    import scripts.clv_gate as cg
    import src.models.anchor_learner as al
    import src.pipeline.prediction_pipeline as pp
    monkeypatch.setattr(cg, "load_clv_blocked_markets", lambda: {"m1"})
    monkeypatch.setattr(cg, "load_clv_blocked_leagues", lambda: {"l1"})
    monkeypatch.setattr(cg, "load_shadow_reactivated", lambda: {"away_win"})
    monkeypatch.setattr(al, "load_anchor_weights", lambda: {"families": {"1x2": {"weight": 0.2}}})
    st = pp.load_learned_state()
    assert st["blocked_markets"] == {"m1"}
    assert st["blocked_leagues"] == {"l1"}
    assert st["reactivated"] == {"away_win"}
    assert st["anchor"]["families"]["1x2"]["weight"] == 0.2


def test_load_learned_state_survives_a_broken_loader(monkeypatch):
    import scripts.clv_gate as cg
    import src.models.anchor_learner as al
    import src.pipeline.prediction_pipeline as pp

    def boom():
        raise RuntimeError("DB caída")
    monkeypatch.setattr(cg, "load_clv_blocked_markets", boom)
    monkeypatch.setattr(cg, "load_clv_blocked_leagues", lambda: set())
    monkeypatch.setattr(cg, "load_shadow_reactivated", lambda: set())
    monkeypatch.setattr(al, "load_anchor_weights", lambda: {})
    st = pp.load_learned_state()
    assert st["blocked_markets"] == set()


# ============================================================
# anchor_learner — funciones puras
# ============================================================

def _synthetic(n_matches, beta, alpha=0.002, noise=0.01, family_market="home_win",
               seed=0, legs_per_match=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_matches):
        d = rng.uniform(0.02, 0.30)
        delta = alpha + beta * d + rng.normal(0, noise)
        odds = 2.0
        closing = 1.0 / (1.0 / odds + delta)
        for _ in range(legs_per_match):
            rows.append({"match": f"m{i}", "match_date": "2030-01-05",
                         "market": family_market, "deviation": d,
                         "odds": odds, "closing_odds": closing})
    return pd.DataFrame(rows)


def test_fit_recovers_known_slope():
    from src.models.anchor_learner import prepare, fit_slope
    data = prepare(_synthetic(2000, beta=0.20))
    fit = fit_slope(data["d"].to_numpy(), data["delta"].to_numpy(), data["cluster"].to_numpy())
    assert fit["beta"] == pytest.approx(0.20, abs=0.02)
    assert fit["alpha"] == pytest.approx(0.002, abs=0.003)


def test_duplicated_legs_do_not_fake_precision():
    """Filas repetidas del mismo partido no son información nueva: el SE
    robusto por cluster no debe encogerse como si lo fueran."""
    from src.models.anchor_learner import prepare, fit_slope
    one = prepare(_synthetic(300, beta=0.2, seed=1, legs_per_match=1))
    three = prepare(_synthetic(300, beta=0.2, seed=1, legs_per_match=3))
    se1 = fit_slope(one["d"].to_numpy(), one["delta"].to_numpy(), one["cluster"].to_numpy())["se"]
    se3 = fit_slope(three["d"].to_numpy(), three["delta"].to_numpy(), three["cluster"].to_numpy())["se"]
    assert se3 == pytest.approx(se1, rel=0.05)


def test_little_data_keeps_the_prior():
    from src.models.anchor_learner import learn_anchor_weights, PRIOR_MODEL_WEIGHT
    st = learn_anchor_weights(_synthetic(20, beta=0.0))
    node = st["families"]["1x2"]
    assert node["source"] == "prior"
    assert node["weight"] == PRIOR_MODEL_WEIGHT


def test_weight_learns_but_moves_at_most_one_step_per_week():
    from src.models.anchor_learner import learn_anchor_weights, MAX_WEEKLY_STEP, PRIOR_MODEL_WEIGHT
    df = _synthetic(1500, beta=0.05, seed=2)
    wk1 = learn_anchor_weights(df)
    n1 = wk1["families"]["1x2"]
    assert n1["source"] == "family"
    assert n1["target"] < 0.12                      # los datos dicen ~0.05
    assert n1["weight"] == pytest.approx(PRIOR_MODEL_WEIGHT - MAX_WEEKLY_STEP)
    wk2 = learn_anchor_weights(df, previous=wk1)
    assert wk2["families"]["1x2"]["weight"] == pytest.approx(n1["weight"] - MAX_WEEKLY_STEP)


def test_weight_is_capped():
    from src.models.anchor_learner import learn_anchor_weights, W_MAX
    st = learn_anchor_weights(_synthetic(1500, beta=0.95, seed=3),
                              previous={"families": {"1x2": {"weight": W_MAX}}})
    assert st["families"]["1x2"]["weight"] <= W_MAX


def test_family_without_data_uses_pooled_estimate():
    from src.models.anchor_learner import learn_anchor_weights
    st = learn_anchor_weights(_synthetic(1500, beta=0.10, seed=4))
    assert st["families"]["1x2"]["source"] == "family"
    assert st["families"]["totals"]["source"] == "pooled"


def test_prepare_drops_unusable_rows():
    from src.models.anchor_learner import prepare
    df = pd.DataFrame([
        {"match": "a", "match_date": "x", "market": "home_win", "deviation": 0.1, "odds": 2.0, "closing_odds": 1.9},
        {"match": "b", "match_date": "x", "market": "home_win", "deviation": -0.1, "odds": 2.0, "closing_odds": 1.9},
        {"match": "c", "match_date": "x", "market": "dc_1x", "deviation": 0.1, "odds": 1.3, "closing_odds": 1.3},
        {"match": "d", "match_date": "x", "market": "over25", "deviation": 0.1, "odds": 2.0, "closing_odds": float("nan")},
        {"match": "e", "match_date": "x", "market": "btts", "deviation": 0.9, "odds": 2.0, "closing_odds": 1.9},
    ])
    assert list(prepare(df)["match"]) == ["a"]


def test_model_weight_for_defaults_to_prior():
    from src.models.anchor_learner import model_weight_for, PRIOR_MODEL_WEIGHT
    assert model_weight_for("home_win", {}) == PRIOR_MODEL_WEIGHT
    assert model_weight_for("home_win", {"families": {"1x2": {"weight": 7}}}) == PRIOR_MODEL_WEIGHT
    assert model_weight_for("ah_away_+0.50", {"families": {"ah_dnb": {"weight": 0.2}}}) == 0.2
    assert model_weight_for("dc_1x", {"families": {"1x2": {"weight": 0.2}}}) == PRIOR_MODEL_WEIGHT


# ============================================================
# Reactivación por shadow — función pura
# ============================================================

def test_reactivation_needs_positive_evidence_and_has_hysteresis():
    from scripts.clv_gate import merge_reactivation_state
    st = merge_reactivation_state({}, {"away_win": (29, 0.01, 0.03)})
    assert st["away_win"] is False                       # n insuficiente
    st = merge_reactivation_state(st, {"away_win": (30, 0.001, 0.03)})
    assert st["away_win"] is True                        # evidencia positiva
    st = merge_reactivation_state(st, {"away_win": (40, -0.002, 0.03)})
    assert st["away_win"] is True                        # zona gris: se mantiene
    st = merge_reactivation_state(st, {"away_win": (60, -0.02, 0.03)})
    assert st["away_win"] is False                       # negativo significativo


# ============================================================
# Grupos AH — perspectiva del lado apostado
# ============================================================

@pytest.mark.parametrize("market,group", [
    ("ah_home_-0.50", "ah_home_fav"),
    ("ah_home_+0.50", "ah_home_dog"),
    ("ah_away_-0.50", "ah_away_dog"),   # visitante RECIBE +0.5
    ("ah_away_+0.50", "ah_away_fav"),   # visitante DA 0.5
    ("ah_home_+0.00", "ah_home_pk"),
    ("home_win", None),
])
def test_ah_group_uses_the_bet_side(market, group):
    from src.models.calibration_monitor import _ah_group
    assert _ah_group(market) == group


# ============================================================
# Cuotas de cierre — los datos con los que se aprende
# ============================================================

def _odds_row(**kw):
    base = {"home_odds": 2.0, "draw_odds": 3.4, "away_odds": 3.8,
            "over25_odds": 1.9, "under25_odds": 1.95,
            "btts_yes_odds": 1.8, "btts_no_odds": 2.0,
            "ah_line": -0.5, "ah_home_odds": 2.0, "ah_away_odds": 1.85,
            "dnb_home_odds": np.nan, "dnb_away_odds": np.nan,
            "corners_line": 9.5, "corners_over_odds": 1.9, "corners_under_odds": 1.9,
            "cards_line": 4.5, "cards_over_odds": 1.8, "cards_under_odds": 2.0}
    base.update(kw)
    return pd.Series(base)


def test_closing_btts_is_found():
    """Antes el closing de producción buscaba 'btts_yes': las bets 'btts'
    nunca recibían cierre ni CLV."""
    from src.models.save_bets import _closing_odds_for
    assert _closing_odds_for("btts", _odds_row()) == 1.8


def test_closing_requires_the_same_line():
    from src.models.save_bets import _closing_odds_for
    assert _closing_odds_for("ah_home_-0.50", _odds_row()) == 2.0
    assert _closing_odds_for("ah_home_-0.50", _odds_row(ah_line=-0.75)) is None
    assert _closing_odds_for("corners_over_9.5", _odds_row(corners_line=10.5)) is None
    assert _closing_odds_for("cards_under_4.5", _odds_row()) == 2.0


def test_closing_dnb_uses_same_source_as_opening():
    from src.models.save_bets import _closing_odds_for
    assert _closing_odds_for("dnb_home", _odds_row(dnb_home_odds=1.45, dnb_away_odds=2.6)) == 1.45
    derived = _closing_odds_for("dnb_home", _odds_row())
    assert derived == pytest.approx((1 / 2.0 + 1 / 3.8) / (1 / 2.0), abs=1e-3)


def test_closing_never_returns_nan():
    from src.models.save_bets import _closing_odds_for
    assert _closing_odds_for("home_win", _odds_row(home_odds=np.nan)) is None


# ============================================================
# Señales y filtros de selección
# ============================================================

def test_line_signal_only_touches_markets_on_that_side():
    from src.features.line_movement import apply_line_movement_signal, market_side
    line = {"has_movement": True, "sharp_signal": "away_win", "movement_strength": "strong"}
    assert apply_line_movement_signal("over25", 0.10, 0.8, line) == (0.10, 0.8)
    assert apply_line_movement_signal("corners_under_9.5", 0.10, 0.8, line) == (0.10, 0.8)
    _, conf = apply_line_movement_signal("home_win", 0.10, 0.8, line)
    assert conf < 0.8
    _, conf = apply_line_movement_signal("ah_home_-0.50", 0.10, 0.8, line)
    assert conf < 0.8
    assert market_side("dnb_away") == "away_win"
    assert market_side("btts") is None


def test_market_filter_lets_parametric_markets_through():
    from src.features.market_intelligence import market_intelligence_filter
    bets = [{"market": m, "edge": 0.2, "edge_market": 0.07, "odds": 2.0, "probability": 0.55}
            for m in ("ah_home_+0.50", "dnb_away", "h1_home", "dc_1x")]
    assert [b["market"] for b in market_intelligence_filter(bets)] == [
        "ah_home_+0.50", "dnb_away", "h1_home", "dc_1x"]


# ============================================================
# Persistencia y fallos visibles
# ============================================================

class _DeadEngine:
    def connect(self):
        raise ConnectionError("sin DB")

    def begin(self):
        raise ConnectionError("sin DB")


def test_model_state_falls_back_to_file_and_reports_db_failure(monkeypatch, tmp_path):
    import src.utils.model_state as ms
    monkeypatch.setattr(ms, "engine", _DeadEngine())
    f = tmp_path / "state.json"
    assert ms.save_state("k", {"a": 1}, file_path=f) is False     # la DB no la vio
    assert ms.load_state("k", file_path=f) == {"a": 1}             # espejo local
    assert ms.load_state("otra") is None


def test_anthropic_budget_fails_closed():
    from src.utils.anthropic_budget import can_call
    import src.utils.anthropic_budget as ab
    ab._TABLE_ENSURED = True
    ok, reason = can_call(_DeadEngine(), 0.01)
    assert ok is False and "desconocido" in reason


def test_orchestrator_exits_nonzero_when_a_step_fails(monkeypatch, tmp_path):
    import scripts.orchestrator as orch
    import scripts.notify_telegram as nt
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(orch, "LOG_DIR", tmp_path)
    monkeypatch.setattr(orch, "LOCK_FILE", tmp_path / "lock")
    for fn in ("_rotate_logs", "_check_world_cup_activation", "_ensure_db_indexes"):
        monkeypatch.setattr(orch, fn, lambda: None)

    def boom():
        raise RuntimeError("paso roto")
    monkeypatch.setattr(orch, "run_results_only",
                        lambda logger: orch.run_step(logger, "Paso de prueba", boom))
    monkeypatch.setattr(sys, "argv", ["orchestrator.py", "--mode", "results"])
    with pytest.raises(SystemExit) as exc:
        orch.main()
    assert exc.value.code == 1
    assert any("Paso de prueba" in m for m in sent)
