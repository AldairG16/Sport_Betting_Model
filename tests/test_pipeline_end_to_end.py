"""
tests/test_pipeline_end_to_end.py
=================================
Primer test que EJECUTA run_prediction_pipeline() de punta a punta (con el
arnés de tests/pipeline_harness.py) en vez de revisar el texto del código.

- test_pipeline_matches_golden: congela la salida completa (apuestas +
  candidatas shadow). Si un cambio altera qué se apuesta, este test lo
  hace visible; si el cambio es intencional, se regenera el golden con
  UPDATE_GOLDEN=1 y el diff queda documentado en el commit.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.pipeline_harness import run_pipeline, normalize

GOLDEN = Path(__file__).parent / "golden" / "pipeline_baseline.json"


def _assert_same_rows(got: list, expected: list, what: str):
    """Mismas filas (qué se apuesta / qué se observa) exactas; números con
    tolerancia 1e-5 para no fallar por el último bit entre Windows y Linux."""
    key = lambda r: (r["match"], r["market"])
    assert [key(r) for r in got] == [key(r) for r in expected], f"{what}: cambió el conjunto"
    for g, e in zip(got, expected):
        for field, ev in e.items():
            if isinstance(ev, float):
                assert g[field] == pytest.approx(ev, abs=1e-5), f"{what} {key(e)} {field}"
            else:
                assert g[field] == ev, f"{what} {key(e)} {field}"


def test_pipeline_matches_golden(monkeypatch):
    got = normalize(run_pipeline(monkeypatch))
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False), encoding="utf-8")
        pytest.skip("golden regenerado")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    _assert_same_rows(got["bets"], expected["bets"], "bets")
    _assert_same_rows(got["shadow"], expected["shadow"], "shadow")


def test_one_broken_match_does_not_kill_the_slate(monkeypatch):
    """Una fila corrupta se omite; el resto del slate se evalúa igual."""
    import tests.pipeline_harness as h

    def form(team, venue=None, **kw):
        if team == "gamma":
            raise ValueError("fila corrupta simulada")
        return h._form(team, venue)

    sent = []
    import scripts.notify_telegram as nt
    monkeypatch.setattr(nt, "send_message", lambda msg, *a, **k: sent.append(msg))

    out = run_pipeline(monkeypatch, form_fn=form)
    matches = {b["match"] for b in out["bets"]}
    assert "gamma vs delta" not in matches
    assert "alpha vs beta" in matches          # el resto sigue apostando
    assert any("Predicciones parciales" in m for m in sent)


def test_systemic_failure_raises(monkeypatch):
    """Si TODOS los partidos fallan, el paso debe fallar (no 'sin value bets')."""
    def form(team, venue=None, **kw):
        raise ValueError("feature rota")

    with pytest.raises(RuntimeError, match="error sistémico"):
        run_pipeline(monkeypatch, form_fn=form)
