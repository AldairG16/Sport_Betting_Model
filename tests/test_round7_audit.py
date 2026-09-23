"""
tests/test_round7_audit.py
==========================
Tests de la Ronda 7: gate de CLV estadístico y alcanzable (B1/R10),
bloqueo explícito de favoritos AH, shadow logging (B2) y bandas.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.pipeline.prediction_pipeline as pp
import src.models.save_bets as sb
from scripts.clv_gate import (
    MIN_BETS_STAT,
    _deviation_band,
    market_clv_blocked,
)


# ============================================================
# B1(a) — criterio estadístico alcanzable
# ============================================================

def test_clv_gate_fires_with_realistic_sample():
    """Con efecto real (CLV −2pt) el gate debe disparar con n=35, no con 100."""
    assert market_clv_blocked(n=35, mean=-0.02, sd=0.04) is True


def test_clv_gate_spares_noise():
    """CLV levemente negativo sin significancia NO bloquea (falso positivo)."""
    assert market_clv_blocked(n=35, mean=-0.005, sd=0.04) is False


def test_clv_gate_requires_minimum_sample():
    assert market_clv_blocked(n=20, mean=-0.05, sd=0.04) is False
    assert MIN_BETS_STAT == 30


def test_clv_gate_large_sample_small_effect():
    """Con n grande, un efecto chico pero real sí bloquea."""
    assert market_clv_blocked(n=200, mean=-0.008, sd=0.04) is True
    assert market_clv_blocked(n=200, mean=-0.001, sd=0.04) is False


# ============================================================
# B2 — shadow logging
# ============================================================

def test_shadow_captures_before_edge_filter(monkeypatch):
    """
    (actualizado en ronda 8/C1) La captura ya no vive en el loop de bets
    (que pierde todo con edge_market < 0.02 en find_value_bets): es un
    barrido sobre clean_probabilities × odds ANTES de find_value_bets, con
    piso propio SHADOW_MIN_DEV declarado (R11).
    """
    # Verificado ejecutando el pipeline (22-sep-26): el shadow contiene
    # candidatas que find_value_bets descartaría (edge_market < 0.02).
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    assert any(s["edge_market"] < 0.02 for s in out["shadow"])
    assert all(abs(s["deviation"]) >= pp.SHADOW_MIN_DEV for s in out["shadow"])


def test_pipeline_persists_shadow_records(monkeypatch):
    """Todas las candidatas del barrido llegan a persist_shadow_bets."""
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    assert out["shadow"] and pp.LAST_RUN_SUMMARY["shadow"] == len(out["shadow"])


def test_shadow_table_dedupes_reruns():
    """Las re-corridas del mismo slate no duplican filas."""
    assert "UNIQUE (match, market, match_date)" in sb.SHADOW_TABLE_SQL


def _closing_world(monkeypatch, bets_rows):
    """El closing de producción (scripts/update_closing_odds) con una base
    falsa: una bet (opcional) y una candidata shadow del mismo partido, con
    kickoff en 30 min y cuota descargada 20 min antes del kickoff."""
    import pandas as pd
    import scripts.update_closing_odds as uco
    from tests.fake_db import FakeEngine

    kickoff = pd.Timestamp.now(tz="UTC").floor("min") + pd.Timedelta(minutes=30)
    fetched = kickoff - pd.Timedelta(minutes=20)
    bets = pd.DataFrame(bets_rows(kickoff), columns=["id", "match", "market", "odds",
                                                     "match_date", "closing_odds",
                                                     "closing_fetched_at"])
    shadow = pd.DataFrame([{"id": 7, "match": "alpha vs beta", "market": "draw",
                            "match_date": kickoff, "closing_odds": None,
                            "closing_fetched_at": None}])
    sql_seen, lookups, quotes = [], [], []

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        sql_seen.append(s)
        if "FROM bets_history" in s:
            return bets.copy()
        if "FROM shadow_bets" in s:
            return shadow.copy()
        raise AssertionError(f"SQL inesperado: {s[:100]}")

    def nearest(home, away, date):
        lookups.append((home, away))
        return pd.DataFrame([{"home_odds": 1.9}])

    def quote(market, row):
        quotes.append(market)
        return 1.85, fetched

    eng = FakeEngine()
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(uco, "engine", eng)
    monkeypatch.setattr(sb, "engine", eng)
    monkeypatch.setattr(sb, "_nearest_market_row", nearest)
    monkeypatch.setattr(sb, "closing_quote_for", quote)
    uco.update_closing_odds()
    return eng, sql_seen, lookups, quotes, fetched


def test_bets_and_shadow_closing_share_lookup_and_mapping(monkeypatch):
    """El closing de bets_history y el del shadow usan LAS MISMAS funciones:
    _nearest_market_row (fila más cercana al kickoff) y closing_quote_for
    (mercado → cuota + hora de descarga). Se verifica ejecutándolos."""
    eng, _, lookups, quotes, fetched = _closing_world(
        monkeypatch, lambda k: [(1, "alpha vs beta", "home_win", 1.9,
                                 k.tz_convert(None), None, None)])
    assert quotes == ["home_win", "draw"]
    assert lookups == [("alpha", "beta"), ("alpha", "beta")]
    (_, bet_upd), = eng.statements("UPDATE bets_history SET closing_odds")
    (_, sh_upd), = eng.statements("UPDATE shadow_bets SET closing_odds")
    for upd in (bet_upd, sh_upd):
        assert upd["closing_odds"] == 1.85
        assert upd["fetched_at"] == fetched.to_pydatetime()


def test_shadow_closing_runs_even_without_bets_to_close(monkeypatch):
    """Un slate sin bets también cierra su shadow (el dato del aprendizaje).
    Hasta el 23-sep-26 el cierre del shadow quedaba después del `return`
    de "no hay bets pendientes"."""
    eng, _, _, quotes, _ = _closing_world(monkeypatch, lambda k: [])
    assert quotes == ["draw"]
    assert len(eng.statements("UPDATE shadow_bets SET closing_odds")) == 1


# ============================================================
# B2 — bandas de desvío para el análisis CLV
# ============================================================

def test_deviation_bands_cover_the_window():
    """Las bandas de A: [0-5), [5-10), [10-15), [15-19), [19-25), [25-30), 30+."""
    assert _deviation_band(0.04) == "0-5"
    assert _deviation_band(0.07) == "5-10"
    assert _deviation_band(0.12) == "10-15"
    assert _deviation_band(0.17) == "15-19"
    assert _deviation_band(0.22) == "19-25"
    assert _deviation_band(0.28) == "25-30"
    assert _deviation_band(0.35) == "30+"
    assert _deviation_band(None) == "sin_ref"


# ============================================================
# B1(b) — favoritos AH bloqueados explícitamente
# ============================================================

def test_ah_favorites_explicitly_blocked(monkeypatch):
    """Bloqueo declarado en el filtro (no piso inalcanzable): el AH del
    local favorito no se apuesta. Desde el 22-sep-26 se verifica ejecutando
    el pipeline (tests/pipeline_harness.py), no buscando el texto."""
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)
    assert not any(b["market"].startswith("ah_home_-") for b in out["bets"])


# ============================================================
# R8 — los parámetros de la ronda 6 siguen intactos
# ============================================================

def test_round6_thresholds_unchanged():
    assert pp.MIN_EDGE == 0.05
    assert pp.MAX_MODEL_DEVIATION == 0.30
    assert max(pp.MIN_EDGE_BY_MARKET.values()) <= 0.06
