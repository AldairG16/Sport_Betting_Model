"""
tests/test_model_proposals.py
=============================
Lista exacta de lo que el modelo propuso (src/models/proposals.py, 12-oct-26):
el dueño veía "55 sin valor frente a Pinnacle" en el log y no podía saber
cuáles eran ni cómo habrían terminado.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import proposals as P
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine


def _p(match, market, decision, odds=2.0):
    return P.proposal({"market": market, "odds": odds, "probability": 0.55, "edge_market": 0.05},
                      match, pd.Timestamp("2026-10-10 18:00", tz="UTC"), "soccer_epl", decision,
                      pin_prob=0.5)


def test_proposal_keeps_the_real_edge_and_the_pinnacle_price():
    r = _p("a vs b", "home_win", P.NO_GANA_PINNACLE)
    assert (r["edge"], r["pin_prob"], r["decision"]) == (0.05, 0.5, P.NO_GANA_PINNACLE)


def test_final_decision_marks_what_the_portfolio_or_paper_took_out():
    props = [_p("a vs b", "home_win", P.APOSTADA), _p("c vs d", "over25", P.APOSTADA),
             _p("e vs f", "btts", P.APOSTADA), _p("g vs h", "draw", P.VENTAJA_CORTA)]
    saved = [{"match": "a vs b", "market": "home_win"}]
    paper = [{"match": "e vs f", "market": "btts"}]
    out = {(r["match"], r["market"]): r["decision"] for r in P.final_decisions(props, saved, paper)}
    assert out == {("a vs b", "home_win"): P.APOSTADA, ("c vs d", "over25"): P.LIMITE_CARTERA,
                   ("e vs f", "btts"): P.PAPEL, ("g vs h", "draw"): P.VENTAJA_CORTA}


def test_persist_upserts_in_one_batch_and_never_touches_settled_rows(monkeypatch):
    eng = FakeEngine()
    monkeypatch.setattr(P, "engine", eng)
    recs = [_p("a vs b", "home_win", P.NO_GANA_PINNACLE), _p("a vs b", "home_win", P.APOSTADA),
            _p("c vs d", "over25", P.SIN_PINNACLE), _p("x vs y", "btts", P.APOSTADA, odds=1.0)]
    assert P.persist_proposals(recs) == 2            # repetida → la última; sin cuota real → fuera
    sql, params = eng.statements("INSERT INTO model_proposals")[0]
    assert "ON CONFLICT (match, market, match_date) DO UPDATE" in sql
    assert "WHERE model_proposals.result IS NULL" in sql
    assert params["decision0"] == P.APOSTADA
    from sqlalchemy import text
    assert set(text(sql).compile().params) == set(params)


def test_a_failed_save_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()

    class Boom(FakeEngine):
        def respond(self, sql, params):
            raise RuntimeError("neon caído")
    monkeypatch.setattr(P, "engine", Boom())
    assert P.persist_proposals([_p("a vs b", "home_win", P.APOSTADA)]) == 0
    assert any("Propuestas del modelo no guardadas" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


def test_summary_counts_what_would_have_happened_and_what_was_bet():
    df = pd.DataFrame([
        {"decision": P.APOSTADA, "result": "win", "profit": 1.0},
        {"decision": P.NO_GANA_PINNACLE, "result": "loss", "profit": -1.0},
        {"decision": P.NO_GANA_PINNACLE, "result": "loss", "profit": -1.0},
        {"decision": P.NO_GANA_PINNACLE, "result": "push", "profit": 0.0},
        {"decision": P.VENTAJA_CORTA, "result": None, "profit": None},
    ])
    s = P.summarize(df)
    assert (s["total"], s["played"], s["wins"], s["losses"], s["profit"]) == (5, 4, 1, 2, -1.0)
    assert (s["bet"], s["bet_wins"], s["bet_profit"]) == (1, 1, 1.0)
    assert s["by_reason"][0] == (P.NO_GANA_PINNACLE, 3, -2.0)


def test_weekly_block_is_plain_and_empty_without_data(monkeypatch):
    monkeypatch.setattr(P.pd, "read_sql", lambda *a, **k: pd.DataFrame(columns=["decision", "result", "profit"]))
    assert P.weekly_lines() == []
    monkeypatch.setattr(P.pd, "read_sql", lambda *a, **k: pd.DataFrame([
        {"decision": P.NO_GANA_PINNACLE, "result": "loss", "profit": -1.0},
        {"decision": P.APOSTADA, "result": "win", "profit": 0.9}]))
    text = "\n".join(P.weekly_lines())
    assert "Propuestas: 2  ·  apostadas: 1" in text
    assert "1 ganadas, 1 perdidas, -0.10u" in text
