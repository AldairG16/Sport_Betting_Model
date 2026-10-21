"""
Auditoría de salidas del 21-oct-26: lo que el sistema produce tiene sentido.

  A. hándicap y empate anula salen del MISMO reparto que el 1X2 final;
  B. sin Dixon-Coles confiable el partido no se evalúa (y el tope va antes
     de mezclar);
  E. el historial del dashboard encuentra partidos guardados con un nombre
     viejo;
  F. sin momios inventados (más/menos 1.5 y 3.5, tiros);
  G. chequeo diario de salidas (src/utils/output_checks.py).
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.utils import output_checks as oc
from src.utils.log import RUN_ISSUES
from tests.pipeline_harness import extended_kwargs, run_pipeline


# ─── A: hándicap / empate anula coherentes con el 1X2 ────────────────────────

def test_consistent_matrix_reproduces_the_final_1x2():
    from src.models.asian_handicap_model import consistent_score_matrix, prob_ah_from_matrix
    target = (0.50, 0.22, 0.28)
    m = consistent_score_matrix(1.9, 0.9, target, rho=-0.09)
    i, j = np.indices(m.shape)
    assert m.sum() == pytest.approx(1.0)
    assert (m[i > j].sum(), m[i == j].sum(), m[i < j].sum()) == pytest.approx(target)
    # -0.5 local = gana local; +0.5 local = gana o empata
    assert prob_ah_from_matrix(m, -0.5)[0] == pytest.approx(0.50)
    assert prob_ah_from_matrix(m, +0.5)[0] == pytest.approx(0.72)
    # línea entera: el empate devuelve → gana / (gana + pierde) en 0
    assert prob_ah_from_matrix(m, 0.0)[0] == pytest.approx(0.50 / 0.78 * 0.78 + 0.22 / 2)


def test_prob_ah_still_matches_the_poisson_when_called_directly():
    from src.models.asian_handicap_model import _score_matrix, prob_ah, prob_ah_from_matrix
    for line in (-1.5, -1.0, -0.75, -0.25, 0.0, 0.5, 1.25):
        assert prob_ah(1.6, 1.1, line, rho=-0.05) == pytest.approx(
            prob_ah_from_matrix(_score_matrix(1.6, 1.1, rho=-0.05), line))


# ─── B: sin Dixon-Coles confiable no se evalúa ───────────────────────────────

def _skip_on(kw, **extra):
    """El escenario extendido apaga el salto (sus equipos no tienen
    Dixon-Coles a propósito); aquí se vuelve a encender."""
    kw["overrides"] = {**kw.get("overrides", {}), "MIN_DC_WEIGHT": 0.5, **extra}
    return kw


def _evaluated(out):
    return {r["match"] for r in out["bets"] + out["shadow"] + out["paper"]}


def test_match_with_thin_dixon_coles_is_not_evaluated(monkeypatch, capsys):
    kw = extended_kwargs()
    counts = {t: 40 for t in ("lam", "mu", "nu", "xi", "wca", "wcb", "rho", "sig", "tau", "ups",
                              "phi", "khi", "psi", "ome", "iota", "beta", "gamma", "kappa",
                              "eqa", "eqb", "alpha", "zeta", "theta", "nof", "delta", "epsilon")}
    counts.update({"omi": 8, "pi": 8})                       # 25% de Dixon-Coles
    out = run_pipeline(monkeypatch, **_skip_on(kw, _safe_history_counts=lambda: dict(counts)))
    assert "omi vs pi" not in _evaluated(out)
    assert "lam vs mu" in _evaluated(out)
    assert "Sin Dixon-Coles confiable: omi vs pi (peso 25%)" in capsys.readouterr().out


def test_without_the_real_history_count_nothing_is_skipped(monkeypatch, capsys):
    # el conteo falla → el peso sale de la ventana de la forma (25% siempre):
    # saltar con eso dejaría el día entero sin evaluar
    out = run_pipeline(monkeypatch, **_skip_on(extended_kwargs()))
    assert "omi vs pi" in _evaluated(out)
    assert "Sin Dixon-Coles confiable: omi vs pi" not in capsys.readouterr().out


def test_team_missing_from_the_fit_is_not_evaluated(monkeypatch):
    kw = extended_kwargs()
    mle = kw["mle_lambdas"]
    kw["mle_lambdas"] = lambda h, a, n: (1.7, 1.1) if (h, a) == ("rho", "sig") else mle(h, a, n)
    out = run_pipeline(monkeypatch, **_skip_on(kw))
    assert "rho vs sig" in _evaluated(out)           # con Dixon-Coles (forma de 20 → 55%)
    assert "tau vs ups" not in _evaluated(out)       # sin él


def test_most_matches_without_dixon_coles_is_an_error(monkeypatch):
    kw = extended_kwargs()
    teams = {t for pair in kw["matches"][["home_team", "away_team"]].itertuples(index=False) for t in pair}
    _skip_on(kw, _safe_history_counts=lambda: {t: 5 for t in teams})
    RUN_ISSUES.reset()
    run_pipeline(monkeypatch, **kw)
    assert any("sin Dixon-Coles confiable" in e for e in RUN_ISSUES.errors())   # llega a Telegram
    RUN_ISSUES.reset()


def test_form_is_capped_before_blending_with_dixon_coles(monkeypatch):
    import tests.pipeline_harness as H
    from tests.pipeline_harness import _row
    # un local que golea a rivales débiles: su forma pasa de 3.5 goles
    monkeypatch.setitem(H.EXT_TEAMS, "big", {"attack": 6.0, "defense": 0.4, "matches": 35})
    monkeypatch.setitem(H.EXT_TEAMS, "weak", {"attack": 0.5, "defense": 3.5, "matches": 35})
    m = pd.DataFrame([_row("big", "weak", "soccer_spain_la_liga", home_odds=1.10, draw_odds=9.0,
                           away_odds=25.0, over25_odds=1.30, under25_odds=3.40, btts_yes_odds=2.6,
                           btts_no_odds=1.45, pin_home_odds=1.12, pin_draw_odds=9.5, pin_away_odds=26.0)])
    form = run_pipeline(monkeypatch, matches=m, mle_lambdas=lambda h, a, n: None,
                        overrides={"MIN_DC_WEIGHT": 0.0})
    assert form["model_sharp"][0]["lam_home"] == pytest.approx(3.5)        # en el tope
    out = run_pipeline(monkeypatch, matches=m, mle_lambdas=lambda h, a, n: (1.5, 1.0))
    # 35 partidos → 75% Dixon-Coles sobre la forma YA topada: 0.25·3.5 + 0.75·1.5
    assert out["model_sharp"][0]["lam_home"] == pytest.approx(2.0)


# ─── F: sin momios inventados ────────────────────────────────────────────────

def test_no_market_is_quoted_with_an_invented_price(monkeypatch):
    from src.models.proposals import FAKE_ODDS_MARKETS
    out = run_pipeline(monkeypatch, **extended_kwargs())
    rows = out["bets"] + out["shadow"] + out["paper"]
    assert rows and not [r for r in rows if r["market"] in FAKE_ODDS_MARKETS]


def test_old_fake_price_rows_do_not_count_in_the_dashboard_summary():
    from dashboard import visibility as V
    kick = pd.Timestamp("2026-10-18 19:00", tz="UTC")
    df = pd.DataFrame([
        {"match": "a vs b", "match_date": kick, "league": "soccer_korea_kleague1", "market": "under_3.5",
         "odds": 1.65, "probability": 0.80, "pin_prob": None, "decision": "liga bloqueada", "result": "win"},
        {"match": "c vs d", "match_date": kick, "league": "soccer_korea_kleague1", "market": "home_win",
         "odds": 2.10, "probability": 0.50, "pin_prob": 0.47, "decision": "liga bloqueada", "result": "loss"},
    ])
    out = V.proposals(df)
    fake = next(r for r in out["rows"] if r["match"].lower().startswith("a"))
    assert fake["fake_odds"] is True
    (g,) = out["summary"]
    assert (g["decision"], g["n"], g["profit_1u"]) == ("liga bloqueada", 1, -1.0)


def test_weekly_summary_excludes_fake_price_markets(monkeypatch):
    import src.models.proposals as P
    seen = {}

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k):
            return type("R", (), {"scalar": lambda self: "model_proposals"})()

    monkeypatch.setattr(P, "engine", type("E", (), {"connect": lambda self: _Conn()})())

    def fake_read_sql(sql, con=None, params=None, **kw):
        seen["sql"], seen["params"] = " ".join(str(sql).split()), params
        return pd.DataFrame(columns=["decision", "result", "profit"])
    monkeypatch.setattr(P.pd, "read_sql", fake_read_sql)
    P.weekly_lines()
    assert "NOT (market = ANY(:fake))" in seen["sql"]
    assert set(seen["params"]["fake"]) == set(P.FAKE_ODDS_MARKETS)


# ─── E: historial con nombres viejos ─────────────────────────────────────────

def test_history_finds_a_match_saved_under_an_old_team_name():
    from dashboard import visibility as V
    when = pd.Timestamp("2026-10-19 18:00", tz="UTC")
    mvs = pd.DataFrame([{"match": "Club Atletico Old vs Rival", "match_date": when, "league": "soccer_epl",
                         "p_home": 0.5, "p_draw": 0.25, "p_away": 0.25,
                         "pin_home": 0.48, "pin_draw": 0.27, "pin_away": 0.25}])
    res = pd.DataFrame([{"h": "atletico new", "a": "rival", "d": "2026-10-19", "home_goals": 2, "away_goals": 0}])
    assert V.history(mvs, res)["rows"] == []
    alias = {"club atletico old": "atletico new"}
    h = V.history(mvs, res, lambda t: alias.get(t.lower(), t.lower()))
    assert len(h["rows"]) == 1 and h["rows"][0]["score"] == "2-0"


# ─── G: chequeo diario de salidas ────────────────────────────────────────────

def _m(h="aa", a="bb", d="2026-10-19", hg=2, ag=1, **kw):
    base = {"h": h, "a": a, "d": d, "home_goals": hg, "away_goals": ag, "home_goals_ht": 1,
            "away_goals_ht": 1, "home_corners": 6, "away_corners": 4, "home_yellow": 2, "away_yellow": 3}
    return {**base, **kw}


@pytest.mark.parametrize("market,score,expected", [
    ("home_win", (2, 1), "win"), ("draw", (2, 1), "loss"), ("dc_x2", (1, 1), "win"),
    ("dnb_away", (1, 1), "push"), ("btts_no", (2, 0), "win"),
    ("over25", (2, 1), "win"), ("under_2.5", (2, 1), "loss"),
    ("over_2.25", (1, 1), "half_loss"), ("over_2.75", (2, 1), "half_win"), ("under_3.0", (2, 1), "push"),
    ("ah_home_-0.25", (1, 1), "half_loss"), ("ah_home_-1.00", (2, 1), "push"),
    # visitante con la línea del local (convención de resolve_market)
    ("ah_away_-0.50", (1, 1), "win"), ("ah_away_+0.50", (1, 1), "loss"), ("ah_away_+0.50", (1, 2), "win"),
    ("h1_draw", (2, 1), "win"), ("corners_over_9.5", (2, 1), "win"), ("cards_under_4.5", (2, 1), "loss"),
])
def test_independent_settlement(market, score, expected):
    assert oc.settle_independent(market, _m(hg=score[0], ag=score[1])) == expected


def test_independent_settlement_without_data_says_nothing():
    assert oc.settle_independent("corners_over_9.5", _m(home_corners=None)) is None
    assert oc.settle_independent("h1_home", _m(home_goals_ht=float("nan"))) is None
    assert oc.settle_independent("anytime_goalscorer_x", _m()) is None


def test_settlement_mismatch_is_reported_with_the_score():
    rows = [{"table": "shadow_bets", "id": 7, "match": "AA vs BB", "match_date": "2026-10-19T19:00:00+00:00",
             "market": "home_win", "result": "loss"},
            {"table": "shadow_bets", "id": 8, "match": "AA vs BB", "match_date": "2026-10-19T19:00:00+00:00",
             "market": "over25", "result": "win"}]
    (bad,) = oc.settlement_mismatches(rows, [_m()])
    assert (bad["id"], bad["expected"], bad["score"]) == (7, "win", "2-1")


def test_ambiguous_or_missing_matches_are_not_judged():
    rows = [{"table": "bets_history", "id": 1, "match": "aa vs bb", "match_date": "2026-10-19",
             "market": "home_win", "result": "loss"}]
    assert oc.settlement_mismatches(rows, [_m(), _m(d="2026-10-20", hg=0, ag=1)]) == []   # dos marcadores
    assert oc.settlement_mismatches(rows, [_m(d="2026-10-25")]) == []                    # otra fecha


def test_missing_results_use_canonical_names():
    analyzed = [{"match": "Old AA vs BB", "match_date": "2026-10-19"}, {"match": "cc vs dd", "match_date": "2026-10-19"}]
    norm = lambda t: {"old aa": "aa"}.get(t.lower(), t.lower())
    miss = oc.missing_results(analyzed, [_m()], norm)
    assert [m["match"] for m in miss] == ["cc vs dd"]


def test_learning_not_updated_by_the_weekly_is_flagged():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    fresh = {k: now - timedelta(days=2) for k in oc.WEEKLY_KEYS}
    assert oc.stale_learning(fresh, now) == []
    old = {**fresh, "side_bias": now - timedelta(days=12)}
    del old["anchor_weights"]
    out = oc.stale_learning(old, now)
    assert len(out) == 2 and any("side_bias" in p for p in out) and any("no existe" in p for p in out)


def _rec(lam=(1.5, 1.1), model=(0.45, 0.27, 0.28), pin=(0.44, 0.27, 0.29)):
    return {"lam_home": lam[0], "lam_away": lam[1], "p_home": model[0], "p_draw": model[1],
            "p_away": model[2], "pin_home": pin[0], "pin_draw": pin[1], "pin_away": pin[2]}


def test_pipeline_invariants_quiet_on_a_normal_day():
    assert oc.pipeline_invariants([_rec()] * 40) == []
    assert oc.pipeline_invariants([_rec(lam=(3.5, 0.4))] * 10) == []     # muestra chica: no se juzga


def test_pipeline_invariants_catch_the_xg_scale_bug():
    # así se veía con el xG de la temporada como si fuera por partido
    recs = [_rec(lam=(3.5, 3.1), model=(0.70, 0.12, 0.18))] * 10 + [_rec()] * 20
    out = oc.pipeline_invariants(recs)
    assert any("tope" in p for p in out) and any("20 pts" in p for p in out)


def test_pipeline_invariants_flag_a_drifting_model():
    out = oc.pipeline_invariants([_rec(model=(0.55, 0.22, 0.23))] * 30)
    assert out == ["Modelo: distancia típica a Pinnacle de 11.0 pts (normal: ~6)"]


def test_daily_check_reports_every_problem_as_an_error(monkeypatch):
    import src.utils.output_checks as mod
    now = pd.Timestamp.now(tz="UTC")

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "AS bets" in s:
            return pd.DataFrame([{"bets": 0, "shadow": 2, "props": 0}])
        if "FROM matches" in s:
            return pd.DataFrame([_m(d=str(now.date()))])
        if "FROM bets_history WHERE result IN" in s:
            return pd.DataFrame([{"id": 3, "match": "aa vs bb", "match_date": now, "market": "home_win",
                                  "result": "loss"}])
        if "FROM shadow_bets WHERE" in s or "FROM model_proposals WHERE" in s:
            return pd.DataFrame(columns=["id", "match", "match_date", "market", "result"])
        if "FROM model_vs_sharp" in s:
            return pd.DataFrame(columns=["match", "match_date"])
        if "team_xg_real" in s:
            return pd.DataFrame([{"m": 12.3}])
        if "model_state" in s:
            return pd.DataFrame([{"key": k, "updated_at": now} for k in oc.WEEKLY_KEYS])
        raise AssertionError(s[:80])
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    RUN_ISSUES.reset()
    out = mod.run_output_checks(engine=object(), normalize=lambda t: t.lower())
    assert len(out) == 3
    assert any("Liquidación: 1" in p and "loss≠win" in p for p in out)
    assert any("2 sombra" in p for p in out) and any("xG real: 12.3" in p for p in out)
    assert sum(1 for e in RUN_ISSUES.errors() if "Chequeo de salidas" in e) == 3   # "errores tolerados"
    RUN_ISSUES.reset()


def test_evening_runs_the_check_before_the_telegram_summary(monkeypatch):
    from scripts import orchestrator as O
    ran = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, fn, *a, **k: ran.append((name, fn)) or True)
    O.run_evening(type("L", (), {"log": lambda self, msg: None})())
    names = [n for n, _ in ran]
    assert names.index("Chequeo de salidas") < names.index("Telegram evening")
    assert dict(ran)["Chequeo de salidas"] is O.step_output_checks
