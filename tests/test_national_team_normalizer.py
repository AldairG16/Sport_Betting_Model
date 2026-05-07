"""
tests/test_national_team_normalizer.py
======================================
Garantiza que las variantes de nombres de selecciones nacionales se
mapeen al canonical del CSV histórico (martj42/international_results)
para que el modelo del Mundial 2026 pueda cruzar datos de entrenamiento
con apuestas vivas de the-odds-api sin perder partidos por mismatch
de naming.
"""

import pytest

from src.utils.team_normalizer import normalize_team


# ── Variantes esperadas que the-odds-api podría mandar para el WC 2026 ──
@pytest.mark.parametrize("variant, expected", [
    # USA — variante más común que rompe matching
    ("USA",                        "united states"),
    ("United States of America",   "united states"),
    ("us",                         "united states"),

    # Coreas
    ("South Korea",                "south korea"),
    ("Korea Republic",             "south korea"),
    ("Korea Rep.",                 "south korea"),
    ("Republic of Korea",          "south korea"),
    ("Korea DPR",                  "north korea"),
    ("North Korea",                "north korea"),

    # Czechia / Czech Republic
    ("Czech Republic",             "czechia"),
    ("Czechia",                    "czechia"),

    # Bosnia
    ("Bosnia and Herzegovina",     "bosnia & herzegovina"),
    ("Bosnia & Herzegovina",       "bosnia & herzegovina"),
    ("Bosnia-Herzegovina",         "bosnia & herzegovina"),

    # Macedonia
    ("North Macedonia",            "north macedonia"),
    ("FYR Macedonia",              "north macedonia"),
    ("Macedonia",                  "north macedonia"),

    # Türkiye / Turkey (Türkiye se vuelve "turkiye" tras strip de acento)
    ("Türkiye",                    "turkey"),
    ("Turkiye",                    "turkey"),
    ("Turkey",                     "turkey"),

    # Côte d'Ivoire / Ivory Coast — clean_name sí mantiene el apóstrofe
    ("Ivory Coast",                "cote d'ivoire"),
    ("Cote d'Ivoire",              "cote d'ivoire"),
    ("Côte d'Ivoire",              "cote d'ivoire"),

    # Cabo Verde
    ("Cape Verde",                 "cape verde islands"),
    ("Cape Verde Islands",         "cape verde islands"),
    ("Cabo Verde",                 "cape verde islands"),

    # DR Congo (no confundir con Congo Brazzaville)
    ("DR Congo",                   "dr congo"),
    ("Democratic Republic of the Congo", "dr congo"),
    ("Congo DR",                   "dr congo"),

    # Trinidad & Tobago
    ("Trinidad and Tobago",        "trinidad and tobago"),
    ("Trinidad & Tobago",          "trinidad and tobago"),

    # Irlanda
    ("Republic of Ireland",        "republic of ireland"),
    ("Ireland",                    "republic of ireland"),

    # Curaçao (acento removido)
    ("Curaçao",                    "curacao"),
    ("Curacao",                    "curacao"),

    # Códigos FIFA
    ("ARG",                        "argentina"),
    ("BRA",                        "brazil"),
    ("ESP",                        "spain"),
    ("GER",                        "germany"),
])
def test_national_team_normalization(variant, expected):
    assert normalize_team(variant) == expected


# ── Casos de borde: equipos con nombre idéntico al canonical ────────────
@pytest.mark.parametrize("name", [
    "Argentina", "Brazil", "France", "Germany", "England",
    "Mexico", "Japan", "Australia", "Canada", "Poland",
])
def test_canonical_unchanged(name):
    """Equipos cuyo nombre directo ya es el canonical no deben mutar."""
    assert normalize_team(name) == name.lower()


# ── Sanity check: clubes existentes no se rompen ────────────────────────
def test_clubs_not_affected():
    """El nuevo map nacional NO debe afectar la normalización de clubes."""
    # No estos NO matchean con NATIONAL_TEAM_ALIASES (no son nombres de país)
    assert normalize_team("Real Madrid")     == "real madrid"
    assert normalize_team("Manchester City") == "manchester city"
    assert normalize_team("Boca Juniors")    == "boca juniors"


# ── Idempotencia: aplicar normalize 2 veces da el mismo resultado ──────
@pytest.mark.parametrize("name", [
    "USA", "Korea Republic", "Türkiye", "Ivory Coast", "Bosnia and Herzegovina",
])
def test_idempotent(name):
    once = normalize_team(name)
    twice = normalize_team(once)
    assert once == twice
