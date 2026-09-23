"""
Reglas manuales contra resultados reales (22-sep-26):
  - liquidación de las candidatas shadow (save_bets.plan_shadow_resolutions)
  - escala aprendida de los ajustes manuales (shade_learner)
  - reporte de evidencia por regla (rule_evidence)
  - integración con el pipeline: p_pre_shade / shade_delta en el shadow y la
    escala aplicada después del tope D12.
Datos sintéticos; sin DB.
"""

import json

import numpy as np
import pandas as pd
import pytest

from src.models import rule_evidence as re_
from src.models import shade_learner as sl
from src.models.save_bets import plan_shadow_resolutions


# ─────────────────────────────────────────────────────────────────────────────
# Liquidación de las candidatas shadow
# ─────────────────────────────────────────────────────────────────────────────

def _matches(**over):
    row = {"home_team_l": "alpha", "away_team_l": "beta",
           "date": "2026-09-20",                      # DATE sin zona, como en la DB
           "home_goals": 2, "away_goals": 1,
           "home_corners": None, "away_corners": None,
           "home_yellow": None, "away_yellow": None,
           "home_goals_ht": None, "away_goals_ht": None,
           "home_goals_h2": None, "away_goals_h2": None,
           "home_shots_target": None, "away_shots_target": None}
    row.update(over)
    return pd.DataFrame([row])


def _pending(*markets_odds, match="alpha vs beta"):
    return pd.DataFrame([
        {"id": i + 1, "match": match, "market": m, "odds": o,
         # TIMESTAMPTZ: con zona, a diferencia de matches.date
         "match_date": pd.Timestamp("2026-09-20 19:00", tz="UTC")}
        for i, (m, o) in enumerate(markets_odds)])


class TestShadowResolution:

    def test_settles_with_stake_one_and_mixed_timezones(self):
        plan = plan_shadow_resolutions(_pending(("home_win", 2.0), ("draw", 3.4)), _matches())
        assert plan == [{"id": 1, "result": "win", "profit": 1.0},
                        {"id": 2, "result": "loss", "profit": -1.0}]

    def test_draw_no_bet_push(self):
        plan = plan_shadow_resolutions(_pending(("dnb_home", 1.5)),
                                       _matches(home_goals=1, away_goals=1))
        assert plan == [{"id": 1, "result": "push", "profit": 0.0}]

    def test_waits_for_market_specific_data(self):
        """Córners sin datos: se espera (no se liquida como pérdida)."""
        assert plan_shadow_resolutions(_pending(("corners_over_9.5", 1.9)), _matches()) == []
        plan = plan_shadow_resolutions(_pending(("corners_over_9.5", 1.9)),
                                       _matches(home_corners=6, away_corners=5))
        assert plan == [{"id": 1, "result": "win", "profit": pytest.approx(0.9)}]

    def test_skips_unknown_match_missing_goals_and_unknown_market(self):
        assert plan_shadow_resolutions(_pending(("home_win", 2.0), match="x vs y"), _matches()) == []
        assert plan_shadow_resolutions(_pending(("home_win", 2.0)),
                                       _matches(home_goals=None)) == []
        assert plan_shadow_resolutions(_pending(("mercado_raro", 2.0)), _matches()) == []

    def test_empty(self):
        assert plan_shadow_resolutions(pd.DataFrame(), _matches()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Escala aprendida de los ajustes manuales
# ─────────────────────────────────────────────────────────────────────────────

def _scale_data(n, s_true, market="home_win", seed=7, delta_sd=0.05, start=0):
    """Candidatas donde la probabilidad real es p_ancla + s_true·δ."""
    rng = np.random.default_rng(seed)
    p_pre = rng.uniform(0.25, 0.65, n)
    delta = rng.normal(0.0, delta_sd, n)
    p_true = np.clip(p_pre + s_true * delta, 0.02, 0.98)
    y = rng.random(n) < p_true
    return pd.DataFrame({
        "match": [f"m{start + i} vs x{start + i}" for i in range(n)],
        "match_date": "2026-09-20 15:00:00+00:00",
        "market": market, "p_pre_shade": p_pre, "shade_delta": delta,
        "result": np.where(y, "win", "loss")})


class TestShadeLearner:

    @pytest.mark.parametrize("s_true", [0.0, 1.0])
    def test_fit_recovers_the_true_scale(self, s_true):
        data = sl.prepare(_scale_data(20000, s_true))
        fit = sl.fit_scale(data["delta"].to_numpy(), data["resid"].to_numpy(),
                           data["cluster"].to_numpy())
        assert fit["n"] == 20000 and fit["se"] < 0.1
        assert abs(fit["s"] - s_true) < 3 * fit["se"]

    def test_without_enough_data_keeps_the_rules_as_designed(self):
        state = sl.learn_shade_scales(_scale_data(50, 0.0))
        node = state["families"]["1x2"]
        assert node["scale"] == 1.0 and node["source"] == "prior"

    def test_useless_rules_are_turned_down_one_step_at_a_time(self):
        df = _scale_data(20000, 0.0)
        first = sl.learn_shade_scales(df)
        node = first["families"]["1x2"]
        assert node["source"] == "family" and node["target"] < 0.3
        assert node["scale"] == pytest.approx(1.0 - sl.MAX_WEEKLY_STEP)
        second = sl.learn_shade_scales(df, previous=first)
        assert second["families"]["1x2"]["scale"] == pytest.approx(1.0 - 2 * sl.MAX_WEEKLY_STEP)

    def test_never_amplifies_beyond_the_designed_rules(self):
        state = sl.learn_shade_scales(_scale_data(20000, 2.0))
        assert state["families"]["1x2"]["s_hat"] > 1.3
        assert state["families"]["1x2"]["scale"] == 1.0

    def test_family_without_data_uses_the_pooled_estimate(self):
        df = pd.concat([_scale_data(20000, 0.0, market="over25"),
                        _scale_data(40, 0.0, market="home_win", seed=3, start=50000)])
        fams = sl.learn_shade_scales(df)["families"]
        assert fams["totals"]["source"] == "family"
        assert fams["1x2"]["source"] == "pooled" and fams["1x2"]["scale"] < 1.0

    def test_prepare_drops_pushes_zero_deltas_and_rows_without_pre_shade(self):
        df = _scale_data(4, 1.0)
        df.loc[0, "result"] = "push"
        df.loc[1, "shade_delta"] = 0.0
        df.loc[2, "p_pre_shade"] = None
        assert len(sl.prepare(df)) == 1

    def test_scale_for_defaults_and_validation(self):
        state = {"families": {"1x2": {"scale": 0.4}, "totals": {"scale": 1.7},
                              "btts": {"scale": "x"}}}
        assert sl.shade_scale_for("home_win", state) == 0.4
        assert sl.shade_scale_for("over25", state) == 1.0      # fuera de rango
        assert sl.shade_scale_for("btts", state) == 1.0         # basura
        assert sl.shade_scale_for("ah_home_-0.5", state) == 1.0  # familia sin estado
        assert sl.shade_scale_for("dc_1x", state) == 1.0        # no anclable
        assert sl.shade_scale_for("home_win", None) == 1.0
        for corrupt in ({"families": ["x"]}, {"families": {"1x2": "x"}}, "x", {"families": None}):
            assert sl.family_scale("1x2", corrupt) == 1.0

    def test_report_renders(self):
        text = sl.format_report(sl.learn_shade_scales(_scale_data(20000, 0.0)))
        assert "1x2" in text and "100% → 75%" in text


# ─────────────────────────────────────────────────────────────────────────────
# Evidencia por regla
# ─────────────────────────────────────────────────────────────────────────────

SAT = "2026-09-19 15:00:00+00:00"
TUE = "2026-09-22 19:00:00+00:00"


def _evidence_data(n=6000, seed=11, midweek_overconfidence=0.10):
    """
    Modelo bien calibrado (p_final ≈ real) frente a un mercado ruidoso; los
    ajustes manuales solo agregan ruido; entre semana el modelo está
    sobreconfiado. Una candidata por partido.
    """
    rng = np.random.default_rng(seed)
    p_true = rng.uniform(0.25, 0.65, n)
    y = rng.random(n) < p_true
    delta = rng.normal(0.0, 0.05, n)
    midweek = np.arange(n) % 2 == 0
    p_pre = p_true + np.where(midweek, midweek_overconfidence, 0.0)
    p_final = np.clip(p_pre + delta, 0.02, 0.98)
    p_ref = np.clip(p_true + rng.normal(0.0, 0.15, n), 0.05, 0.95)
    odds = 1.0 / np.clip(p_true - 0.03, 0.05, 0.95)
    return pd.DataFrame({
        "match": [f"h{i} vs a{i}" for i in range(n)],
        "match_date": np.where(midweek, TUE, SAT),
        "league": np.where(np.arange(n) % 3 == 0, "soccer_italy_serie_a", "soccer_spain_la_liga"),
        "market": "home_win", "p_final": p_final, "p_ref": p_ref, "odds": odds,
        "edge_market": 0.06, "p_pre_shade": p_pre, "shade_delta": delta,
        "result": np.where(y, "win", "loss"), "profit": np.where(y, odds - 1.0, -1.0)})


class TestRuleEvidence:

    def test_cluster_mean_uses_matches_as_clusters(self):
        s = re_.cluster_mean([1, 1, 0, 0], ["a", "a", "b", "b"])
        assert s["mean"] == 0.5 and s["clusters"] == 2 and s["se"] == pytest.approx(0.5)

    @pytest.mark.parametrize("mean,se,lower,expected", [
        (-0.02, 0.005, True, "a favor"),
        (0.02, 0.005, True, "en contra"),
        (0.02, 0.005, False, "a favor"),
        (0.002, 0.005, True, "sin evidencia"),
    ])
    def test_verdict(self, mean, se, lower, expected):
        stat = {"n": 500, "clusters": 400, "mean": mean, "se": se}
        assert re_.verdict(stat, lower_supports=lower) == expected

    def test_verdict_needs_enough_rows_and_matches(self):
        assert re_.verdict({"n": 50, "clusters": 50, "mean": -1.0, "se": 0.01}, True) == "insuficiente"
        assert re_.verdict({"n": 500, "clusters": 10, "mean": -1.0, "se": 0.01}, True) == "insuficiente"

    def test_detects_what_the_synthetic_world_contains(self):
        ev = re_.build_rule_evidence(_evidence_data())
        assert ev["n_settled"] == 6000 and ev["n_matches"] == 6000
        # el modelo acierta más que el mercado ruidoso
        assert ev["model_vs_market"]["all"]["verdict"] == "a favor"
        assert ev["model_vs_market"]["by_family"]["1x2"]["n"] == 6000
        # los ajustes solo agregan ruido
        assert ev["shades"]["all"]["verdict"] == "en contra"
        # entre semana el modelo está sobreconfiado: el filtro acierta
        mid = ev["filters"]["entre_semana"]
        assert mid["gap_diff"]["verdict"] == "a favor"
        assert mid["gap_diff"]["mean"] == pytest.approx(0.10, abs=0.03)
        # la liga no importa en este mundo: diferencia dentro de 3σ (al 95%
        # un grupo sin efecto real cruza el IC 1 de cada 20 veces por azar)
        tough = ev["filters"]["liga_dura"]["gap_diff"]
        assert abs(tough["mean"]) < 3 * tough["se"]

    def test_flb_of_the_market(self):
        """Mercado que infravalora favoritos y sobrevalora longshots."""
        rng = np.random.default_rng(5)
        n = 8000
        p_true = rng.uniform(0.15, 0.75, n)
        y = rng.random(n) < p_true
        fav = p_true > 1 / 2.8
        p_ref = np.where(fav, p_true - 0.05, p_true + 0.05)
        df = pd.DataFrame({
            "match": [f"h{i} vs a{i}" for i in range(n)], "match_date": SAT,
            "league": "soccer_spain_la_liga", "market": "home_win",
            "p_final": p_true, "p_ref": p_ref, "odds": 1.0 / p_ref, "edge_market": 0.0,
            "p_pre_shade": None, "shade_delta": None,
            "result": np.where(y, "win", "loss"), "profit": 0.0})
        flb = re_.build_rule_evidence(df)["flb_market"]
        assert flb["favorites"]["verdict"] == "a favor"
        assert flb["longshots"]["verdict"] == "a favor"
        assert {r["side"] for r in flb["table"]} == {"home"}

    def test_sweet_spot_split_uses_the_pipeline_bands(self):
        df = _evidence_data(n=400)
        df.loc[:199, "odds"] = 4.5          # home_win fuera de (1.30, 3.80)
        df.loc[200:, "odds"] = 2.0
        node = re_.build_rule_evidence(df)["filters"]["fuera_de_sweet_spot"]
        assert node["penalized"]["gap"]["n"] == 200 and node["rest"]["gap"]["n"] == 200

    def test_pushes_count_in_roi_but_not_in_calibration(self):
        df = _evidence_data(n=10)
        df.loc[0, "result"], df.loc[0, "profit"] = "push", 0.0
        ev = re_.build_rule_evidence(df)
        assert ev["bettable"]["roi"]["n"] == 10 and ev["bettable"]["gap"]["n"] == 9

    def test_empty_and_json_safe(self):
        ev = re_.build_rule_evidence(pd.DataFrame())
        assert ev["n_settled"] == 0 and not re_.conclusive(ev)
        assert "Aún no hay" in re_.format_report(ev)
        full = re_.build_rule_evidence(_evidence_data(n=300))
        json.dumps(full, allow_nan=False)          # JSONB no acepta NaN
        assert re_.conclusive(full)
        text = re_.format_report(full, html=True)
        assert "<b>" in text and "entre semana" in text


# ─────────────────────────────────────────────────────────────────────────────
# Integración con el pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _by_key(records):
    return {(r["match"], r["market"]): r for r in records}


def test_shadow_records_carry_the_pre_shade_probability(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    shadow = run_pipeline(monkeypatch)["shadow"]
    anchored = [r for r in shadow if r["p_pre_shade"] is not None]
    assert anchored and any(abs(r["shade_delta"]) > 1e-6 for r in anchored)
    for r in anchored:
        # con escala 1 (sin estado aprendido) p_final = ancla + δ exacto
        assert r["p_final"] == pytest.approx(r["p_pre_shade"] + r["shade_delta"], abs=1e-12)
        assert abs(r["shade_delta"]) <= 0.05 + 1e-12        # tope D12
    assert all(r["shade_delta"] is None for r in shadow if r["market"].startswith("dc_"))


@pytest.mark.parametrize("corrupt", [{"families": ["x"]}, {"families": {"1x2": "x"}},
                                     {"families": {"1x2": {"scale": 7}}}])
def test_corrupt_learned_scale_behaves_like_no_state(monkeypatch, corrupt):
    from tests.pipeline_harness import run_pipeline, normalize
    base = normalize(run_pipeline(monkeypatch))
    assert normalize(run_pipeline(monkeypatch, learned={"shades": corrupt})) == base


def test_learned_scale_zero_turns_off_the_rules_of_that_family_only(monkeypatch):
    from tests.pipeline_harness import run_pipeline
    base = _by_key(run_pipeline(monkeypatch)["shadow"])
    off = run_pipeline(monkeypatch, learned={"shades": {"families": {"1x2": {"scale": 0.0}}}})
    scaled = _by_key(off["shadow"])
    one_x_two = [k for k in scaled if k[1] in ("home_win", "draw", "away_win")
                 and scaled[k]["p_pre_shade"] is not None]
    assert one_x_two
    for k in one_x_two:
        r = scaled[k]
        assert r["p_final"] == pytest.approx(r["p_pre_shade"], abs=1e-12)
        if k in base:   # δ se guarda SIN escalar: igual que con escala 1
            assert r["shade_delta"] == pytest.approx(base[k]["shade_delta"], abs=1e-12)
    others = [k for k in scaled if k in base and k[1] in ("over25", "under25")]
    assert others
    for k in others:
        assert scaled[k]["p_final"] == pytest.approx(base[k]["p_final"], abs=1e-12)
    logs = [json.loads(b["decision_log"]) for b in off["bets"] if b.get("decision_log")]
    assert any(d["model"]["shade_scale"].get("1x2") == 0.0 for d in logs)
