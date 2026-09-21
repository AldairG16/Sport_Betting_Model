"""
tests/test_round16_audit.py
===========================
Tests de la Ronda 16 (K1/R17): la cobertura de LEAGUE_FACTORS se verifica
contra el universo operativo (SPORT_KEYS activas), no contra sí misma.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import SPORT_KEYS
from src.features.league_calibration import LEAGUE_FACTORS, LEAGUE_FACTORS_VERSION
from src.pipeline.prediction_pipeline import BLOCKED_LEAGUES


def test_every_active_league_has_own_calibration():
    """
    R17 convertida en test (K1): toda liga de SPORT_KEYS que no esté
    bloqueada debe tener entrada PROPIA en LEAGUE_FACTORS. Antes: 6 de 17
    activas corrían con DEFAULT_FACTORS — 35% del universo apostable.
    """
    sin_entrada = [k for k in SPORT_KEYS
                   if k not in BLOCKED_LEAGUES and k not in LEAGUE_FACTORS]
    assert sin_entrada == [], (
        f"ligas activas sin calibración propia: {sin_entrada}")


def test_unmeasurable_league_is_blocked_not_defaulted():
    """Noruega (n=21 en 3 años — no medible) está BLOQUEADA, no corriendo
    con genéricos en silencio."""
    assert "soccer_norway_eliteserien" in BLOCKED_LEAGUES


def test_calibration_version_is_declared():
    """R13: los cambios de la tabla de ligas son trazables por cohorte."""
    assert LEAGUE_FACTORS_VERSION == "r16"


def test_new_leagues_have_all_five_parameters():
    """Las cinco ligas nuevas de Q-CA traen los cinco parámetros."""
    for lg in ("soccer_korea_kleague1", "soccer_sweden_allsvenskan",
               "soccer_belgium_first_div", "soccer_greece_super_league",
               "soccer_turkey_super_league"):
        f = LEAGUE_FACTORS[lg]
        for key in ("home_advantage", "tempo", "draw_rate",
                    "over25_rate", "btts_rate"):
            assert key in f, f"{lg} sin {key}"
            assert 0.2 <= f[key] <= 2.5, f"{lg}.{key} fuera de rango"
