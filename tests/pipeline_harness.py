"""
tests/pipeline_harness.py
=========================
Arnés para correr run_prediction_pipeline() de punta a punta SIN base de
datos ni APIs: cada dependencia externa (DB, features que leen la DB,
persistencia) se sustituye por datos fijos. Lo que queda real es toda la
matemática y los filtros del pipeline — justo lo que hay que proteger.

Uso:
    from tests.pipeline_harness import run_pipeline
    out = run_pipeline(monkeypatch)            # fixture por defecto
    out["bets"], out["shadow"], out["paper"]

Los partidos de ejemplo cubren: 1X2/O-U/BTTS, AH, DNB, medio tiempo,
córners, una liga bloqueada, un favorito visitante y movimiento de línea.
"""

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────
# EQUIPOS (ratings en escala de goles, baseline 1.35)
# ─────────────────────────────────────────────────────────────
TEAMS = {
    "alpha":   {"attack": 2.00, "defense": 0.90},   # fuerte
    "beta":    {"attack": 1.00, "defense": 1.70},   # débil
    "gamma":   {"attack": 1.40, "defense": 1.30},   # promedio
    "delta":   {"attack": 1.35, "defense": 1.35},   # promedio
    "epsilon": {"attack": 1.10, "defense": 1.50},   # flojo local
    "zeta":    {"attack": 1.90, "defense": 1.00},   # visitante fuerte
    "eta":     {"attack": 1.60, "defense": 1.20},
    "theta":   {"attack": 1.20, "defense": 1.40},
    "iota":    {"attack": 1.70, "defense": 1.10},
    "kappa":   {"attack": 1.05, "defense": 1.60},
}

ELO = {"alpha": 1650, "beta": 1400, "gamma": 1510, "delta": 1500,
       "epsilon": 1440, "zeta": 1620, "eta": 1560, "theta": 1470,
       "iota": 1590, "kappa": 1420}

# Sábado fijo: el filtro midweek depende del día de la semana, y la
# fecha fija hace el arnés independiente de cuándo se corra.
SAT = "2030-01-05T15:00:00+00:00"
SUN = "2030-01-06T18:00:00+00:00"


def _row(home, away, league, date=SAT, **odds):
    base = {
        "home_team": home, "away_team": away,
        "home_team_norm": home, "away_team_norm": away,
        "match_date": date, "league": league, "sport_key": league,
        "match_key": f"{home}_{away}",
        "home_odds": None, "draw_odds": None, "away_odds": None,
        "over25_odds": None, "under25_odds": None,
        "btts_yes_odds": None, "btts_no_odds": None,
        "ah_line": None, "ah_home_odds": None, "ah_away_odds": None,
        "dnb_home_odds": None, "dnb_away_odds": None,
        "dc_1x_odds": None, "dc_x2_odds": None, "dc_12_odds": None,
        "h1_home_odds": None, "h1_draw_odds": None, "h1_away_odds": None,
        "h2_home_odds": None, "h2_draw_odds": None, "h2_away_odds": None,
        "corners_over_odds": None, "corners_under_odds": None, "corners_line": None,
        "cards_over_odds": None, "cards_under_odds": None, "cards_line": None,
        "h2h_spread_pct": 6.0, "bookmaker_count": 12, "consensus_home_odds": None,
    }
    base.update(odds)
    return base


def default_matches() -> pd.DataFrame:
    rows = [
        # M1 — favorito local claro, AH y DNB con cuotas, movimiento de línea
        _row("alpha", "beta", "soccer_spain_la_liga",
             home_odds=1.80, draw_odds=3.70, away_odds=4.60,
             over25_odds=1.95, under25_odds=1.90,
             btts_yes_odds=2.05, btts_no_odds=1.80,
             ah_line=-0.5, ah_home_odds=1.82, ah_away_odds=2.05,
             dnb_home_odds=1.32, dnb_away_odds=3.40),
        # M2 — liga "dura" (Serie A), partido parejo
        _row("gamma", "delta", "soccer_italy_serie_a",
             home_odds=2.45, draw_odds=3.20, away_odds=3.05,
             over25_odds=2.05, under25_odds=1.80,
             btts_yes_odds=1.85, btts_no_odds=1.95),
        # M3 — visitante fuerte: candidatos del lado visitante
        _row("epsilon", "zeta", "soccer_brazil_campeonato",
             home_odds=2.90, draw_odds=3.30, away_odds=2.45,
             over25_odds=2.00, under25_odds=1.85,
             btts_yes_odds=1.90, btts_no_odds=1.90,
             ah_line=0.5, ah_home_odds=1.95, ah_away_odds=1.90),
        # M4 — liga bloqueada (EFL Championship): solo shadow
        _row("eta", "theta", "soccer_efl_champ",
             home_odds=2.10, draw_odds=3.40, away_odds=3.60,
             over25_odds=1.90, under25_odds=1.95,
             btts_yes_odds=1.80, btts_no_odds=2.00),
        # M5 — medio tiempo + córners en liga cubierta
        _row("iota", "kappa", "soccer_germany_bundesliga", date=SUN,
             home_odds=1.75, draw_odds=3.90, away_odds=4.80,
             over25_odds=1.70, under25_odds=2.15,
             btts_yes_odds=1.80, btts_no_odds=2.00,
             h1_home_odds=2.40, h1_draw_odds=2.20, h1_away_odds=5.50,
             corners_over_odds=1.90, corners_under_odds=1.90, corners_line=9.5,
             dc_1x_odds=1.22, dc_x2_odds=2.50),
    ]
    return pd.DataFrame(rows)


# Movimiento de línea por partido: en M1 el visitante acortó 6% (señal
# "sharp" contra el local) y el over 2.5 no se movió.
LINE_MOVES = {
    "alpha_beta": {"home_movement": 0.02, "draw_movement": 0.0,
                   "away_movement": -0.06, "over25_movement": 0.0,
                   "sharp_signal": "away_win", "movement_strength": "moderate",
                   "has_movement": True},
}


def _neutral_line():
    return {"home_movement": 0.0, "draw_movement": 0.0, "away_movement": 0.0,
            "over25_movement": 0.0, "sharp_signal": "none",
            "movement_strength": "none", "has_movement": False}


def _form(team, venue=None, **_):
    t = TEAMS[team]
    return {
        "matches": 20, "gpg": t["attack"], "gcpg": t["defense"], "ppg": 1.5,
        "spg": 12, "stpg": 4,
        "attack_rating": t["attack"], "defense_rating": t["defense"],
        "uncertainty": 0.20, "is_fallback": False, "venue": venue or "combined",
    }


def run_pipeline(monkeypatch, matches: pd.DataFrame | None = None,
                 learned: dict | None = None,
                 line_moves: dict | None = None,
                 form_fn=None) -> dict:
    """
    Corre el pipeline con dependencias sustituidas y devuelve lo capturado.
    `learned` sustituye el estado aprendido por el weekly (DB): claves
    blocked_markets, blocked_leagues, reactivated, anchor.
    """
    import src.pipeline.prediction_pipeline as pp
    import src.models.betting_engine as be
    import src.models.calibration_monitor as cm
    import src.models.dc_mle_fitter as dcf
    import src.models.monte_carlo_simulator as mcs

    df = default_matches() if matches is None else matches
    moves = LINE_MOVES if line_moves is None else line_moves
    captured = {"bets": [], "shadow": [], "paper": []}

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = str(sql)
        if "upcoming_matches" in s:
            return df.copy()
        if "bets_history" in s:
            return pd.DataFrame({"d": [], "s": []})
        raise AssertionError(f"SQL inesperado en el arnés: {s[:120]}")

    # DB / estado persistido
    monkeypatch.setattr(pp.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(pp, "ensure_bankroll_schema", lambda: None)
    monkeypatch.setattr(pp, "get_current_bankroll", lambda: 100.0)
    monkeypatch.setattr(pp, "load_calibration_factors", lambda: {})
    monkeypatch.setattr(cm, "load_calibration_factors", lambda: {})
    monkeypatch.setattr(pp, "is_params_fresh", lambda max_age_days=8: False)
    monkeypatch.setattr(dcf, "_load_params",
                        lambda: {"rho": -0.09, "converged": True,
                                 "fitted_at": "2030-01-01", "final_grad_max": 0.1})
    monkeypatch.setattr(pp, "_previous_fit_rho", lambda: -0.09)
    monkeypatch.setattr(pp, "_has_coverage", lambda league, kind: True)
    monkeypatch.setattr(be, "_load_clv_cache", lambda: {})
    state = {"blocked_markets": set(), "blocked_leagues": set(),
             "reactivated": set(), "anchor": {}}
    state.update(learned or {})
    monkeypatch.setattr(pp, "load_learned_state", lambda: dict(state))

    # Features (leen la DB en producción)
    monkeypatch.setattr(pp, "compute_elo", lambda: dict(ELO))
    monkeypatch.setattr(pp, "get_team_form", form_fn or _form)
    monkeypatch.setattr(pp, "get_real_xg", lambda team: None)
    monkeypatch.setattr(pp, "get_team_xg",
                        lambda team: {"xg_for": 1.8, "xg_against": 1.0,
                                      "source": "proxy"} if team == "alpha" else None)
    monkeypatch.setattr(pp, "get_lambda_multipliers", lambda league: (1.25, 1.0))
    monkeypatch.setattr(pp, "get_fixture_congestion",
                        lambda team, date: {"attack_multiplier": 1.0,
                                            "defense_multiplier": 1.0,
                                            "is_fatigued": False, "days_rest": 7})
    monkeypatch.setattr(pp, "get_motivation_factor", lambda team, league: 0.0)
    monkeypatch.setattr(pp, "is_unreliable_match", lambda h, a, lg: (False, ""))
    monkeypatch.setattr(pp, "get_h2h_stats", lambda h, a: None)
    monkeypatch.setattr(pp, "get_team_corners",
                        lambda team: {"attack_rating": 5.6, "defense_rating": 4.9}
                        if team in ("iota", "kappa") else None)
    monkeypatch.setattr(pp, "get_team_shots", lambda team: None)
    monkeypatch.setattr(pp, "get_team_cards", lambda team: None)
    monkeypatch.setattr(pp, "get_weather_multiplier", lambda team: 1.0)
    monkeypatch.setattr(pp, "get_over25_rate", lambda league: 0.50)
    monkeypatch.setattr(pp, "get_btts_rate", lambda league: 0.50)
    monkeypatch.setattr(pp, "get_luck", lambda team, cutoff=None: None)
    monkeypatch.setattr(pp, "get_line_movement",
                        lambda key: dict(moves.get(key, _neutral_line())))

    # Monte Carlo reproducible sin importar qué otros tests corrieron antes
    monkeypatch.setattr(mcs, "RNG", np.random.default_rng(seed=42))

    # Persistencia capturada
    monkeypatch.setattr(pp, "save_bets", lambda bets: captured["bets"].extend(bets))
    monkeypatch.setattr(pp, "persist_shadow_bets",
                        lambda recs: captured["shadow"].extend(recs))
    monkeypatch.setattr(pp, "_persist_paper_bets",
                        lambda bets: captured["paper"].extend(bets))

    captured["returned"] = pp.run_prediction_pipeline()
    return captured


def normalize(captured: dict) -> dict:
    """Forma estable y comparable del resultado (sin timestamps)."""
    bets = sorted(
        ({"match": b["match"], "market": b["market"], "league": b["league"],
          "odds": round(float(b["odds"]), 4),
          "probability": round(float(b["probability"]), 6),
          "edge": round(float(b["edge"]), 6),
          "stake": round(float(b["stake"]), 2)}
         for b in captured["bets"]),
        key=lambda b: (b["match"], b["market"]))
    shadow = sorted(
        ({"match": s["match"], "market": s["market"],
          "p_final": round(float(s["p_final"]), 6),
          "p_ref": round(float(s["p_ref"]), 6),
          "deviation": round(float(s["deviation"]), 6),
          "odds": round(float(s["odds"]), 4),
          "edge_market": round(float(s["edge_market"]), 6)}
         for s in captured["shadow"]),
        key=lambda s: (s["match"], s["market"]))
    return {"bets": bets, "shadow": shadow}
