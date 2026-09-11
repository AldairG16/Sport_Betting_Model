"""
Scorer Model — Anytime Goalscorer
==================================
Modelo de "jugador anota en cualquier momento" (anytime goalscorer)
entrenado con match_events (goleadores reales cargados por el weekly).

Matemática (v1, conservadora):
  lambda_base   = goles del jugador / partidos del equipo (ventana 18 meses,
                  decay hacia lo reciente). NO se sabe en qué partidos jugó,
                  así que dividimos entre TODOS los partidos del equipo →
                  base SUBESTIMADA (conservador: preferimos eso a inflar).
  lambda_partido = lambda_base × (goles esperados del equipo en ESTE partido
                  / promedio histórico de goles del equipo)
                  → si el equipo hoy rinde 1.8 goles y su media es 1.2, el
                  jugador tiene 1.5× más probabilidad de anotar.
  P(anota ≥1)   = 1 − Poisson.cdf(0, lambda_partido) = 1 − exp(−λ)

Costo de API: CERO — solo lee match_events y upcoming_matches.

Fair odds = 1 / P. Como el plan actual de The Odds API no incluye player
props, los picks se guardan como PAPEL con fair odds; si el usuario ve una
cuota real mejor que la fair, tiene valor (y se puede registrar odds_placed
para medir slippage igual que en bets_history).
"""

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from config.database import engine
from src.utils.team_normalizer import normalize_team

WINDOW_DAYS    = 550        # ~18 meses de historia
MIN_GOALS      = 4          # muestra mínima de goles para opinar
MIN_BASE_RATE  = 0.22       # ≥0.22 goles/partido-equipo (≈ elite)
DECAY          = 0.995      # decay por partido de antigüedad

_rates_cache: dict | None = None
_cache_ts: float = 0.0
_CACHE_TTL_S = 6 * 3600


def _parse_scorers(raw) -> list[dict]:
    """match_events.home_scorers/away_scorers vienen como JSON string."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def load_scorer_rates(force: bool = False) -> dict:
    """
    Lee match_events y calcula por jugador:
      goals, team_matches (partidos de su equipo en la ventana),
      goals_per_team_match (decay-weighted), team, last_seen

    Returns: {player_lower: {...}}
    """
    global _rates_cache, _cache_ts
    now = datetime.now(timezone.utc).timestamp()
    if not force and _rates_cache and (now - _cache_ts) < _CACHE_TTL_S:
        return _rates_cache

    ev = pd.read_sql(text(f"""
        SELECT date, home_team, away_team, home_scorers, away_scorers
        FROM match_events
        WHERE date >= CURRENT_DATE - {WINDOW_DAYS}
        ORDER BY date DESC
    """), engine)
    if ev.empty:
        _rates_cache = {}
        _cache_ts = now
        return _rates_cache

    # Partidos por equipo (para el denominador conservador)
    team_matches: dict = {}
    for _, r in ev.iterrows():
        for t in (r["home_team"], r["away_team"]):
            tn = normalize_team(t)
            team_matches[tn] = team_matches.get(tn, 0) + 1

    # Goles por jugador con decay por antigüedad
    # (los own goals ya están atribuidos al rival por collect_match_events)
    rows = []
    for i, r in enumerate(ev.iterrows()):
        _, row = r
        age = i  # 0 = más reciente
        w = DECAY ** age
        for team_raw, scorers_raw in ((row["home_team"], row["home_scorers"]),
                                      (row["away_team"], row["away_scorers"])):
            team = normalize_team(team_raw)
            for s in _parse_scorers(scorers_raw):
                p = str(s.get("player", "")).strip().lower()
                if p:
                    rows.append({"player": p, "team": team, "w": w})

    if not rows:
        _rates_cache = {}
        _cache_ts = now
        return _rates_cache

    gl = pd.DataFrame(rows).groupby("player").agg(
        goals_w=("w", "sum"),        # goles ponderados
        goals=("w", "size"),         # goles totales
        team=("team", "first"),
    ).reset_index()

    out: dict = {}
    for _, r in gl.iterrows():
        tm = max(team_matches.get(r["team"], 1), 1)
        rate_w = float(r["goals_w"]) / tm
        rate_raw = int(r["goals"]) / tm
        out[r["player"]] = {
            "team":          r["team"],
            "goals":         int(r["goals"]),
            "team_matches":  tm,
            "rate_weighted": round(rate_w, 4),
            "rate_raw":      round(rate_raw, 4),
        }
    _rates_cache = out
    _cache_ts = now
    return out


def anytime_scorer_prob(player: str, team_expected_goals: float,
                        rates: dict | None = None) -> dict | None:
    """
    P(jugador anota ≥1) dado los goles esperados de su equipo HOY.

    Returns: {"player","team","lambda","prob","fair_odds","goals","rate"}
             None si el jugador no califica (muestra o tasa insuficiente).
    """
    rates = rates if rates is not None else load_scorer_rates()
    key = player.strip().lower()
    info = rates.get(key)
    if info is None:
        # Tolerancia a acentos: "Mbappé" ↔ "Mbappe" (el dataset guarda
        # nombres acentuados, los callers no siempre)
        import unicodedata
        clean = unicodedata.normalize("NFKD", key).encode("ascii", "ignore").decode()
        for cand, ci in rates.items():
            if unicodedata.normalize("NFKD", cand).encode("ascii", "ignore").decode() == clean:
                info = ci
                key = cand
                break
    if info is None:
        return None
    if info["goals"] < MIN_GOALS:
        return None
    if info["rate_raw"] < MIN_BASE_RATE:
        return None

    # Escalar por el contexto ofensivo del partido: la tasa base está
    # calculada sobre TODOS los partidos del equipo; hoy el equipo puede
    # rendir más o menos que su media histórica.
    team_avg_goals = max(info["rate_raw"], 0.05)  # proxy: tasa del jugador ~ media del equipo
    lam = info["rate_weighted"] * (team_expected_goals / team_avg_goals)
    lam = float(np.clip(lam, 0.01, 1.2))
    prob = float(1.0 - np.exp(-lam))
    return {
        "player":     player.strip(),
        "team":       info["team"],
        "lambda":     round(lam, 3),
        "prob":       round(prob, 4),
        "fair_odds":  round(1.0 / prob, 2) if prob > 0 else None,
        "goals":      info["goals"],
        "rate":       info["rate_raw"],
    }
