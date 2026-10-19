"""
tests/test_strategy_lab.py
==========================
Laboratorio de estrategias (src/models/strategy_lab.py, 13-oct-26): cada
forma de elegir apuestas se mide contra el cierre válido de Pinnacle, con el
mismo veredicto que sharp_reference. Solo mide.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import strategy_lab as L


def _rows(n, pin_prob, p_final, close, odds=2.0, result="win", valid=True, start=0):
    ko = pd.Timestamp("2026-10-10 18:00", tz="UTC")
    rows = []
    for i in range(start, start + n):
        rows.append({
            "match": f"local {i} vs visita {i}", "match_date": ko + pd.Timedelta(hours=i),
            "created_at": ko - pd.Timedelta(days=2), "market": "home_win",
            "p_final": p_final, "odds": odds, "edge_market": p_final - 1 / odds,
            "pin_prob": pin_prob, "pin_close_prob": close,
            "pin_close_at": (ko + pd.Timedelta(hours=i) - pd.Timedelta(minutes=30)) if valid
                            else ko - pd.Timedelta(days=1),
            "result": result, "profit": (odds - 1) if result == "win" else -1.0})
    return rows


def test_each_strategy_picks_its_own_candidates():
    df = pd.DataFrame(_rows(5, pin_prob=0.52, p_final=0.53, close=0.53)          # precio +2 pt, modelo +3
                      + _rows(5, pin_prob=0.505, p_final=0.45, close=0.5, start=5)  # precio +0.5, modelo −5
                      + _rows(5, pin_prob=0.45, p_final=0.56, close=0.45, start=10))  # modelo +6, precio −5
    lab = L.build_lab(df)["strategies"]
    assert lab["actual"]["candidates"] == 5
    assert lab["precio_0"]["candidates"] == 10
    assert lab["precio_1"]["candidates"] == 5
    assert lab["modelo"]["candidates"] == 10
    assert lab["precio_y_modelo"]["candidates"] == 5


def test_verdict_needs_100_valid_closes_in_30_matches():
    lab = L.build_lab(pd.DataFrame(_rows(50, 0.52, 0.53, 0.54)))["strategies"]["precio_0"]
    assert lab["value"]["verdict"] == "insuficiente"
    # cierres inválidos (descargados un día antes) no cuentan
    lab = L.build_lab(pd.DataFrame(_rows(150, 0.52, 0.53, 0.54, valid=False)))["strategies"]["precio_0"]
    assert lab["value"]["n"] == 0


def test_beating_the_close_is_a_win_and_losing_to_it_is_a_loss():
    rng = np.random.default_rng(7)
    good = pd.DataFrame(_rows(150, 0.52, 0.53, 0.55))
    good["pin_close_prob"] = 0.55 + rng.normal(0, 0.005, len(good))
    v = L.build_lab(good)["strategies"]["precio_0"]["value"]
    assert v["verdict"] == "a favor" and v["mean"] > 0.05
    bad = pd.DataFrame(_rows(150, 0.52, 0.53, 0.45))
    bad["pin_close_prob"] = 0.45 + rng.normal(0, 0.005, len(bad))
    assert L.build_lab(bad)["strategies"]["precio_0"]["value"]["verdict"] == "en contra"


def test_results_at_one_unit():
    r = L.results(pd.DataFrame(_rows(3, .5, .5, .5) + _rows(2, .5, .5, .5, result="loss", start=3)))
    assert r == {"played": 5, "wins": 3, "losses": 2, "profit": 1.0}


def test_message_speaks_the_owners_language():
    df = pd.DataFrame(_rows(150, 0.52, 0.53, 0.55))
    text = "\n".join(L.format_lines(L.build_lab(df)))
    assert "LABORATORIO DE ESTRATEGIAS" in text
    assert "✅ le gana al mercado" in text
    assert "⏳ faltan datos" not in text.split("Precio sobre Pinnacle, sin preguntar")[1].split("\n")[0]
    empty = "\n".join(L.format_lines(L.build_lab(pd.DataFrame(_rows(10, 0.52, 0.53, 0.55)))))
    assert "⏳ faltan datos (10 de 100)" in empty


def test_no_longshots_variant_only_counts_new_matches():
    """19-oct-26: la hipótesis "sin cuotas altas" salió de mirar resultados,
    así que solo cuentan partidos desde el 20-oct y con cuota hasta +250."""
    old = _rows(5, pin_prob=0.52, p_final=0.53, close=0.53)                    # 10-oct: no cuenta
    new = _rows(6, pin_prob=0.52, p_final=0.53, close=0.53, start=300)         # 22-oct
    longshot = _rows(4, pin_prob=0.30, p_final=0.30, close=0.30, odds=4.0, start=320)  # +300
    lab = L.build_lab(pd.DataFrame(old + new + longshot))["strategies"]
    assert lab["precio_0"]["candidates"] == 15
    assert lab["precio_sin_altas"]["candidates"] == 6
    text = "\n".join(L.format_lines({"strategies": lab, "kalshi": {}}))
    assert "sin cuotas altas (hasta +250)" in text
