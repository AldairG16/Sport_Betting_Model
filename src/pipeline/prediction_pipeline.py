import sys
import pandas as pd
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text as _sql_text

from config.database import engine


# ─────────────────────────────────────────────────────────────
# COVERAGE GATE — evita generar bets en mercados sin data
# ─────────────────────────────────────────────────────────────
# Umbral mínimo de cobertura para permitir bets de un mercado stats-based.
# Si la liga no tiene al menos N% de partidos con córners (o tarjetas) en los
# últimos 30 días, NO generar bets de ese mercado: quedarían siempre en
# 'unresolved' por falta de fuente de datos y contaminan la calibración.
_COVERAGE_MIN_PCT      = 0.50
_COVERAGE_WINDOW_DAYS  = 30
_COVERAGE_CACHE: dict  = {}   # key=(league, market_kind) → bool


def _has_coverage(league: str, market_kind: str) -> bool:
    """
    Devuelve True si la liga tiene >= _COVERAGE_MIN_PCT de partidos con datos
    fuente en los últimos _COVERAGE_WINDOW_DAYS días. Cachea por (liga, kind).

    market_kind: "corners" | "cards" | "shots" | "ht"
    """
    if not league:
        return False
    key = (league, market_kind)
    if key in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[key]

    cols_map = {
        "corners": ("home_corners", "away_corners"),
        "cards":   ("home_yellow",  "away_yellow"),
        "shots":   ("home_shots_target", "away_shots_target"),
        "ht":      ("home_goals_ht", "away_goals_ht"),
    }
    if market_kind not in cols_map:
        _COVERAGE_CACHE[key] = False
        return False

    c1, c2 = cols_map[market_kind]
    try:
        df = pd.read_sql(
            _sql_text(f"""
                SELECT
                    COUNT(*) FILTER (WHERE {c1} IS NOT NULL AND {c2} IS NOT NULL) AS with_data,
                    COUNT(*) AS total
                FROM matches
                WHERE date >= (NOW() - INTERVAL '{_COVERAGE_WINDOW_DAYS} days')::date
                  AND league = :league
            """),
            engine,
            params={"league": league},
        )
        total = int(df.iloc[0]["total"]) if not df.empty else 0
        with_data = int(df.iloc[0]["with_data"]) if not df.empty else 0
        pct = (with_data / total) if total > 0 else 0.0
        _COVERAGE_CACHE[key] = (pct >= _COVERAGE_MIN_PCT)
    except Exception as e:
        # Si la query falla, asumimos que la liga NO tiene cobertura
        # (conservador: no usar señales no validables). Logueamos para que
        # el orchestrator vea el warning en el log de GH Actions.
        print(f"⚠️  league_coverage_ok({league}): error en query — asumiendo sin cobertura. {e}")
        _COVERAGE_CACHE[key] = False
    return _COVERAGE_CACHE[key]

from src.features.elo_rating import compute_elo
from src.features.team_form import get_team_form
from src.features.xg_proxy import get_team_xg
from src.features.h2h_stats import get_h2h_stats
from src.features.league_calibration import get_lambda_multipliers
from src.utils.team_normalizer import normalize_team

from src.models.dixon_coles_model import match_outcomes
from src.models.poisson_markets import totals_and_btts, totals_extended
from src.models.ensemble_model import ensemble_predict

from src.models.betting_engine import find_value_bets, kelly_stake

from src.features.market_odds import market_probabilities
from src.models.bet_ranker import rank_bets
from src.models.save_bets import save_bets
from src.models.bet_filters import bet_quality_filter

from src.features.market_calibration import calibrate_probability, is_strong_edge
from src.features.market_intelligence import market_intelligence_filter, add_market_score
from src.features.line_movement import get_line_movement, apply_line_movement_signal, line_moved_against, should_skip_low_liquidity
from src.dashboard.betting_dashboard import mostrar_dashboard

from src.features.corners_stats import get_team_corners
from src.features.shots_stats import get_team_shots
from src.features.cards_stats import get_team_cards
from src.models.corners_model import predict_corners, corners_to_confidence_signal
from src.models.shots_model import predict_shots, shots_to_confidence_signal
from src.models.cards_model import predict_cards

from src.features.fixture_congestion import get_fixture_congestion
from src.features.motivation_factor import get_motivation_factor
from src.models.asian_handicap_model import prob_ah, get_dnb_probs
from src.features.weather_impact import get_weather_multiplier
from src.models.monte_carlo_simulatior import simulate_match, mc_confidence_vs_analytical
from src.models.bankroll_manager import get_current_bankroll, ensure_bankroll_schema
from src.models.calibration_monitor import (
    load_calibration_factors,
    get_calibration_factor,
    apply_calibration,
    MIN_BETS_FOR_CALIBRATION,
)
from src.models.dc_mle_fitter import get_dc_lambdas, is_params_fresh, DC_MLE_WEIGHT


# Torneos donde la mayoría de partidos se juegan en sede neutral.
# Se usa para anular el `home_adv` del MLE en estos partidos — sin esto,
# el modelo aplica una ventaja artificial al "local" del bracket que no
# existe en la realidad (Brasil vs Argentina en USA = 50/50 de venue).
NEUTRAL_VENUE_LEAGUES = {
    "soccer_fifa_world_cup",
    "soccer_uefa_euro",
    "soccer_conmebol_copa_america",
    "soccer_concacaf_gold_cup",
    "soccer_afcon",
    "soccer_afc_asian_cup",
}
from src.features.league_calibration import get_over25_rate, OVER25_SHRINK

# =========================
# SAFE HELPERS (CRÍTICO)
# =========================

def get_safe_form(team, venue: str = None):
    """
    Wrapper seguro de get_team_form que garantiza un dict válido.
    venue="home" → forma solo como local
    venue="away" → forma solo como visitante
    venue=None   → combinada (fallback)
    """
    form = get_team_form(team, venue=venue)

    if not form:
        return {
            "matches":        0,
            "gpg":            1.2,
            "gcpg":           1.2,
            "ppg":            1.2,
            "spg":            10,
            "stpg":           3,
            "attack_rating":  1.0,
            "defense_rating": 1.0,
            "is_fallback":    True,
            "venue":          venue or "combined",
        }

    return form


def safe_odds(value):
    if value is None or pd.isna(value) or value <= 1:
        return None
    if value > 15:
        return None
    return float(value)


def clamp_prob(p):
    return max(0.01, min(p, 0.95))


# =========================
# MAIN PIPELINE
# =========================

def run_prediction_pipeline():

    print("\nRUNNING PREDICTION PIPELINE\n")

    # ── Bankroll real para Kelly Criterion ────────────────────────────────
    # Asegura que la tabla existe y usa el balance actual.
    # Si nunca se inicializó, usa INITIAL_BANKROLL de settings.py.
    ensure_bankroll_schema()
    current_bankroll = get_current_bankroll()
    print(f"💰 Bankroll actual: {current_bankroll:.2f} unidades")

    # ── Circuit Breaker: si el bankroll es < 10u, detener apuestas ────────
    CIRCUIT_BREAKER_THRESHOLD = 10.0
    if current_bankroll < CIRCUIT_BREAKER_THRESHOLD:
        print(f"🚨 CIRCUIT BREAKER ACTIVADO: bankroll ({current_bankroll:.2f}u) "
              f"< umbral ({CIRCUIT_BREAKER_THRESHOLD}u)")
        print("   ⛔ No se generarán apuestas hasta que se recargue el bankroll.")
        try:
            from scripts.notify_telegram import send_message
            from src.models.bankroll_manager import get_bankroll_stats
            stats = get_bankroll_stats()
            send_message(
                f"🚨 <b>CIRCUIT BREAKER ACTIVADO</b>\n\n"
                f"Bankroll actual: <b>{current_bankroll:.2f}u</b>\n"
                f"Umbral mínimo: {CIRCUIT_BREAKER_THRESHOLD}u\n"
                f"Drawdown desde pico: {stats['drawdown_pct']:.1f}%\n\n"
                f"⛔ Apuestas pausadas automáticamente.\n"
                f"Recarga el bankroll o espera resultados pendientes."
            )
        except Exception as e:
            # Si Telegram cae no rompemos el pipeline, pero AVISAMOS al log:
            # antes este bloque silenciaba el alert de circuit breaker —
            # podías quedarte sin enterar de que el bankroll cruzó el umbral.
            print(f"⚠️  Circuit breaker activo pero NO se pudo notificar a Telegram: {e}")
        return

    # ── Factores de calibración (Brier) ───────────────────────────────────
    # Corrigen el sesgo sistemático del modelo (sobreconfianza / subconfianza)
    # Si el archivo no existe aún, usa factores neutros (1.0 = sin ajuste)
    cal_factors = load_calibration_factors()
    cal_active  = any(
        isinstance(v, dict) and v.get("n_bets", 0) >= MIN_BETS_FOR_CALIBRATION
        for k, v in cal_factors.items()
        if k not in ("updated_at", "window_days", "by_league")
    )
    if cal_active:
        n_calibrated = sum(
            1 for k, v in cal_factors.items()
            if isinstance(v, dict) and v.get("n_bets", 0) >= MIN_BETS_FOR_CALIBRATION
            and k not in ("updated_at", "window_days", "by_league")
        )
        print(f"📐 Factores de calibración activos ({n_calibrated} mercados, "
              f"min_bets={MIN_BETS_FOR_CALIBRATION})")
    else:
        print(f"📐 Calibración: datos insuficientes (< {MIN_BETS_FOR_CALIBRATION} bets), "
              f"usando factores neutros")

    # ── Parámetros MLE Dixon-Coles ─────────────────────────────────────────
    # Si existen parámetros ajustados recientes (<= 8 días), los usa.
    # El ajuste se hace en el modo weekly (lunes 7AM).
    mle_fresh = is_params_fresh(max_age_days=8)
    if mle_fresh:
        print("🔧 Parámetros DC-MLE cargados (ajuste reciente)")
    else:
        print("🔧 DC-MLE: sin parámetros frescos, usando solo forma actual")

    # MEJORA #3 (06-may-26): cargar rho de DC para corregir matriz score.
    # rho es el parámetro tau de Dixon-Coles. SOLO se aplica si el fit
    # detectó señal (|rho| > 0.02). Si el fit dejó rho=0 (corner solution
    # del optimizador), aplicar un -0.13 empírico podría EMPEORAR el modelo
    # en este dataset — mejor mantener Poisson cruda y dejar que la
    # calibración por mercado (Mejora #1+#2) corrija el bias residual.
    try:
        from src.models.dc_mle_fitter import _load_params as _load_dc_params
        _dc_params  = _load_dc_params() or {}
        _rho_fit    = float(_dc_params.get("rho", 0.0) or 0.0)
        if abs(_rho_fit) < 0.02:
            DC_RHO_GLOBAL = None
            print(f"🔧 DC rho fitteado={_rho_fit:+.3f} ≈0 → BTTS usa Poisson cruda")
            print(f"   (calibración por mercado corrige bias residual)")
        else:
            DC_RHO_GLOBAL = _rho_fit
            print(f"🔧 DC rho={DC_RHO_GLOBAL:+.3f} (BTTS con tau-correction activa)")
    except Exception as _e:
        DC_RHO_GLOBAL = None
        print(f"⚠️  DC rho no disponible ({_e}) → Poisson cruda")

    elo = compute_elo()

    # DISTINCT ON dedup: cuando un partido tiene 2 rows en upcoming_matches
    # (típicamente un row stale con match_date vieja + uno nuevo con la fecha
    # corregida), nos quedamos con el más reciente por updated_at. Esto evita
    # generar predicciones sobre datos desactualizados — bug detectado el
    # 8-may-2026 con Talleres/Belgrano y otros 10 partidos AR/PT/EPL.
    df = pd.read_sql("""
        SELECT * FROM (
            SELECT DISTINCT ON (home_team_norm, away_team_norm, sport_key) *
            FROM upcoming_matches
            WHERE match_date::timestamp BETWEEN NOW() + INTERVAL '1 hour'
                         AND NOW() + INTERVAL '3 days'
            ORDER BY home_team_norm, away_team_norm, sport_key,
                     updated_at DESC NULLS LAST
        ) t
        ORDER BY match_date
    """, engine)

    print(f"📊 Matches encontrados: {len(df)}")

    if df.empty:
        return

    all_bets = []

    skipped_no_odds  = 0
    fallback_used    = 0
    xg_used          = 0
    sharp_confirmed  = 0
    sharp_rejected   = 0
    h2h_used         = 0
    corners_used     = 0
    shots_used       = 0
    fatigued_teams   = 0
    weather_adjusted = 0
    mc_diverged      = 0

    # =========================
    # LOOP MATCHES
    # =========================

    for _, row in df.iterrows():

        home = normalize_team(row.home_team)
        away = normalize_team(row.away_team)
        date = row.match_date

        # =========================
        # ODDS CHECK
        # =========================

        odds_available = any([
            safe_odds(row.get("home_odds")),
            safe_odds(row.get("over25_odds")),
            safe_odds(row.get("btts_yes_odds"))
        ])

        if not odds_available:
            skipped_no_odds += 1
            continue

        # =========================
        # TEAM FORM (SAFE + VENUE SPLIT)
        # =========================
        # Usamos forma LOCAL del equipo de casa y forma VISITANTE del equipo
        # visitante. Si hay pocos partidos de venue, get_team_form() hace
        # fallback automático a forma combinada.

        home_form = get_safe_form(home, venue="home")
        away_form = get_safe_form(away, venue="away")

        # 🔥 FALLBACK DETECTION
        home_fallback = home_form.get("is_fallback", False)
        away_fallback = away_form.get("is_fallback", False)

        # CAMBIO B: Skip partido si cualquier equipo es fallback (sin historial real)
        if home_fallback or away_fallback:
            fallback_used += 1
            print(f"⚠️  Skip fallback: {home} vs {away} — datos insuficientes")
            skipped_no_odds += 1
            continue

        # 🔥 CONFIDENCE
        min_matches = min(home_form["matches"], away_form["matches"])
        confidence  = min(1.0, min_matches / 10)

        # =========================
        # TEAM STRENGTH
        # =========================

        # Factores calibrados por liga (calculados de 58k+ partidos reales)
        HOME_ADVANTAGE, TEMPO = get_lambda_multipliers(
            row.get("league", row.get("sport_key", ""))
        )
        XG_WEIGHT  = 0.40   # 40% xG proxy, 60% goals-based form
        H2H_WEIGHT = 0.15   # 15% H2H histórico sobre lambdas finales

        home_attack  = home_form.get("attack_rating", 1.0)
        home_defense = home_form.get("defense_rating", 1.0)
        away_attack  = away_form.get("attack_rating", 1.0)
        away_defense = away_form.get("defense_rating", 1.0)

        # =========================
        # xG PROXY BLEND
        # =========================
        # Reemplaza hasta 40% del ataque/defensa basado en goals
        # con estimaciones xG más estables (menos varianza)

        home_xg = get_team_xg(home)
        away_xg = get_team_xg(away)

        if home_xg:
            home_attack  = home_attack  * (1 - XG_WEIGHT) + home_xg["xg_for"]     * XG_WEIGHT
            home_defense = home_defense * (1 - XG_WEIGHT) + home_xg["xg_against"] * XG_WEIGHT
            xg_used += 1

        if away_xg:
            away_attack  = away_attack  * (1 - XG_WEIGHT) + away_xg["xg_for"]     * XG_WEIGHT
            away_defense = away_defense * (1 - XG_WEIGHT) + away_xg["xg_against"] * XG_WEIGHT

        # =========================
        # PENALIZACIÓN POR POCO DATA
        # =========================

        # =========================
        # FILTRO: AMBOS EQUIPOS FALLBACK
        # =========================
        # Si ambos equipos usan valores de fallback (sin historial en DB),
        # el modelo no tiene información real → los edges son falsos.
        # Caso típico: selecciones nacionales (solo tenemos datos de clubes).
        # Con ambos fallback → skip del partido para evitar apuestas incorrectas.

        # Nota: el skip por fallback ya ocurrió arriba — este bloque ya no aplica

        if min_matches < 3:
            home_attack *= 0.9
            away_attack *= 0.9

        # =========================
        # FIXTURE CONGESTION
        # =========================
        # Detecta fatiga por acumulación de partidos recientes.
        # Un equipo con 3 días de descanso rinde ~8% menos en ataque.
        # Usa fechas históricas de la DB → 0 créditos API.

        home_cong = get_fixture_congestion(home, date)
        away_cong = get_fixture_congestion(away, date)

        home_attack  *= home_cong["attack_multiplier"]
        home_defense *= home_cong["defense_multiplier"]
        away_attack  *= away_cong["attack_multiplier"]
        away_defense *= away_cong["defense_multiplier"]

        if home_cong["is_fatigued"] or away_cong["is_fatigued"]:
            fatigued_teams += 1
            fatigued_info = []
            if home_cong["is_fatigued"]:
                fatigued_info.append(f"{home}({home_cong['days_rest']}d)")
            if away_cong["is_fatigued"]:
                fatigued_info.append(f"{away}({away_cong['days_rest']}d)")
            print(f"  Fatiga: {' vs '.join(fatigued_info)}")

        # =========================
        # FACTOR DE MOTIVACIÓN
        # =========================
        # Ajusta ataque/defensa según situación en la tabla:
        # campeon asegurado / descendido → menos motivado
        # peleando descenso / título / Europa → más motivado

        league_key = row.get("league", row.get("sport_key", ""))
        home_motiv = get_motivation_factor(home, league_key)
        away_motiv = get_motivation_factor(away, league_key)

        if home_motiv != 0.0:
            home_attack  *= (1 + home_motiv)
            home_defense *= (1 + home_motiv)
            label = "MOTIVADO" if home_motiv > 0 else "sin motivacion"
            print(f"  Motivacion {home}: {home_motiv:+.0%} ({label})")

        if away_motiv != 0.0:
            away_attack  *= (1 + away_motiv)
            away_defense *= (1 + away_motiv)
            label = "MOTIVADO" if away_motiv > 0 else "sin motivacion"
            print(f"  Motivacion {away}: {away_motiv:+.0%} ({label})")

        # =========================
        # ELO BLEND
        # =========================

        home_attack = (home_attack + elo.get(home, 1500) / 1500) / 2
        away_attack = (away_attack + elo.get(away, 1500) / 1500) / 2

        lambda_home = home_attack * away_defense * HOME_ADVANTAGE * TEMPO
        lambda_away = away_attack * home_defense * TEMPO

        # =========================
        # H2H ADJUSTMENT
        # =========================
        # Blend 15% de los goles históricos directos sobre los lambdas finales
        # Captura dinámicas específicas del enfrentamiento (derbies, estilos)

        h2h = get_h2h_stats(home, away)

        if h2h:
            lambda_home = lambda_home * (1 - H2H_WEIGHT) + h2h["h2h_home_goals"] * H2H_WEIGHT
            lambda_away = lambda_away * (1 - H2H_WEIGHT) + h2h["h2h_away_goals"] * H2H_WEIGHT
            h2h_used += 1

        # =========================
        # CORNERS MODEL
        # =========================
        # Predice tiros de esquina usando Poisson simple.
        # Ajusta la confianza en 1x2 si hay dominancia clara de córners.
        # No consume créditos API — usa datos históricos de la DB.

        corners_prediction = None
        corners_confidence_delta = 0.0

        home_corners_stats = get_team_corners(home)
        away_corners_stats = get_team_corners(away)

        if home_corners_stats and away_corners_stats:
            corners_prediction = predict_corners(
                home_attack    = home_corners_stats["attack_rating"],
                home_defense   = home_corners_stats["defense_rating"],
                away_attack    = away_corners_stats["attack_rating"],
                away_defense   = away_corners_stats["defense_rating"],
            )
            corners_confidence_delta = corners_to_confidence_signal(corners_prediction)
            corners_used += 1

        # =========================
        # SHOTS MODEL
        # =========================
        # Predice tiros al arco. Alta presión de tiros → partido más abierto.
        # Se usa para calibrar la confianza en over/under goles.

        shots_prediction = None
        shots_confidence_delta = 0.0

        home_shots_stats = get_team_shots(home)
        away_shots_stats = get_team_shots(away)

        if home_shots_stats and away_shots_stats:
            shots_prediction = predict_shots(
                home_attack    = home_shots_stats["attack_rating"],
                home_defense   = home_shots_stats["defense_rating"],
                away_attack    = away_shots_stats["attack_rating"],
                away_defense   = away_shots_stats["defense_rating"],
            )
            shots_confidence_delta = shots_to_confidence_signal(shots_prediction)
            shots_used += 1

        # =========================
        # CARDS MODEL
        # =========================
        # Predice tarjetas totales usando Poisson.
        # Requiere home_yellow/away_yellow en la tabla matches.
        # Sin datos suficientes → silenciosamente se omite.

        cards_prediction = None

        home_cards_stats = get_team_cards(home)
        away_cards_stats = get_team_cards(away)

        if home_cards_stats and away_cards_stats:
            cards_prediction = predict_cards(
                home_attack    = home_cards_stats["attack_rating"],
                home_defense   = home_cards_stats["defense_rating"],
                away_attack    = away_cards_stats["attack_rating"],
                away_defense   = away_cards_stats["defense_rating"],
            )

        # ─── Info por partido (corners + shots) ──────────────────────────
        if corners_prediction or shots_prediction:
            parts = []
            if corners_prediction:
                c = corners_prediction
                parts.append(
                    f"Corners exp: {c['lambda_home']:.1f}H / {c['lambda_away']:.1f}A"
                    f"  (over9.5={c['over95']:.0%})"
                )
            if shots_prediction:
                s = shots_prediction
                parts.append(
                    f"SOT exp: {s['lambda_home']:.1f}H / {s['lambda_away']:.1f}A"
                    f"  (over5.5={s['over55']:.0%})"
                )
            print(f"  >> {home} vs {away}: {' | '.join(parts)}")

        # =========================
        # WEATHER IMPACT
        # =========================
        # Ajusta ambos lambdas según clima en el estadio local.
        # Lluvia/viento/nieve reducen la tasa de goles.
        # 0 créditos extra (OpenWeatherMap gratis 1,000 calls/día).
        # Requiere WEATHER_API_KEY en .env — sin clave, no hay ajuste.

        weather_mult = get_weather_multiplier(home)
        if weather_mult < 1.0:
            lambda_home *= weather_mult
            lambda_away *= weather_mult
            weather_adjusted += 1

        # =========================
        # BLEND DC-MLE (si disponible)
        # =========================
        # Los parámetros MLE ajustan lambdas por calidad del rival.
        # Blend: 40% MLE + 60% forma actual → transición suave.
        # Si algún equipo no está en los parámetros MLE → 100% forma actual.

        if mle_fresh:
            # Detectar venue neutral por liga — fundamental para Mundial,
            # Euro, Copa América y otros torneos en sede única.
            _league_for_neutral = row.get("league", row.get("sport_key", ""))
            _is_neutral_match   = _league_for_neutral in NEUTRAL_VENUE_LEAGUES
            mle_result = get_dc_lambdas(home, away, is_neutral=_is_neutral_match)
            if mle_result is not None:
                mle_lh, mle_la = mle_result
                lambda_home = lambda_home * (1 - DC_MLE_WEIGHT) + mle_lh * DC_MLE_WEIGHT
                lambda_away = lambda_away * (1 - DC_MLE_WEIGHT) + mle_la * DC_MLE_WEIGHT

        # =========================
        # CAP GOALS
        # =========================

        lambda_home = min(lambda_home, 2.5)
        lambda_away = min(lambda_away, 2.5)

        # =========================
        # ASIAN HANDICAP + DNB
        # =========================
        # Calcula probabilidades AH usando la distribución Poisson bivariada.
        # AH elimina el empate → mercado de 2 resultados → más fácil hallar edge.
        # DNB: derivado de h2h odds existentes (sin API extra).

        _ah_line = None
        _ah_home_odds = None
        _ah_away_odds = None
        _raw_ah_line = row.get("ah_line") if hasattr(row, "get") else getattr(row, "ah_line", None)
        if _raw_ah_line is not None and not pd.isna(_raw_ah_line):
            _ah_line = float(_raw_ah_line)
            _ah_home_odds = safe_odds(row.get("ah_home_odds") if hasattr(row, "get") else getattr(row, "ah_home_odds", None))
            _ah_away_odds = safe_odds(row.get("ah_away_odds") if hasattr(row, "get") else getattr(row, "ah_away_odds", None))

        # AH model probs (solo si tenemos línea y odds del mercado)
        _p_ah_home = _p_ah_away = None
        if _ah_line is not None and (_ah_home_odds or _ah_away_odds):
            _p_ah_home, _p_ah_away = prob_ah(lambda_home, lambda_away, _ah_line)

        # DNB probs (siempre disponible desde Poisson)
        _dnb = get_dnb_probs(lambda_home, lambda_away)

        # DNB odds: preferir las de API (draw_no_bet), fallback a derivadas de h2h
        _dnb_home_api = safe_odds(row.get("dnb_home_odds") if hasattr(row, "get") else getattr(row, "dnb_home_odds", None))
        _dnb_away_api = safe_odds(row.get("dnb_away_odds") if hasattr(row, "get") else getattr(row, "dnb_away_odds", None))

        _dnb_home_odds = _dnb_away_odds = None
        if _dnb_home_api and _dnb_away_api:
            _dnb_home_odds = _dnb_home_api
            _dnb_away_odds = _dnb_away_api
        else:
            # Fallback: derivar de h2h odds
            _h_odds = safe_odds(row.home_odds)
            _a_odds = safe_odds(row.away_odds)
            if _h_odds and _a_odds:
                _imp_h = 1.0 / _h_odds
                _imp_a = 1.0 / _a_odds
                _imp_sum = _imp_h + _imp_a
                if _imp_sum > 0:
                    _dnb_home_odds = round(_imp_sum / _imp_h, 3)
                    _dnb_away_odds = round(_imp_sum / _imp_a, 3)

        # ── Double Chance odds de API ────────────────────────────────────
        _dc_1x_odds = safe_odds(row.get("dc_1x_odds") if hasattr(row, "get") else getattr(row, "dc_1x_odds", None))
        _dc_x2_odds = safe_odds(row.get("dc_x2_odds") if hasattr(row, "get") else getattr(row, "dc_x2_odds", None))
        _dc_12_odds = safe_odds(row.get("dc_12_odds") if hasattr(row, "get") else getattr(row, "dc_12_odds", None))

        # ── Half-time odds de API ─────────────────────────────────────────
        _h1_home_odds = safe_odds(row.get("h1_home_odds") if hasattr(row, "get") else getattr(row, "h1_home_odds", None))
        _h1_draw_odds = safe_odds(row.get("h1_draw_odds") if hasattr(row, "get") else getattr(row, "h1_draw_odds", None))
        _h1_away_odds = safe_odds(row.get("h1_away_odds") if hasattr(row, "get") else getattr(row, "h1_away_odds", None))
        _h2_home_odds = safe_odds(row.get("h2_home_odds") if hasattr(row, "get") else getattr(row, "h2_home_odds", None))
        _h2_draw_odds = safe_odds(row.get("h2_draw_odds") if hasattr(row, "get") else getattr(row, "h2_draw_odds", None))
        _h2_away_odds = safe_odds(row.get("h2_away_odds") if hasattr(row, "get") else getattr(row, "h2_away_odds", None))

        # ── Corners / Cards odds de API ───────────────────────────────────
        _corners_over_api  = safe_odds(row.get("corners_over_odds")  if hasattr(row, "get") else getattr(row, "corners_over_odds", None))
        _corners_under_api = safe_odds(row.get("corners_under_odds") if hasattr(row, "get") else getattr(row, "corners_under_odds", None))
        _corners_line_api  = row.get("corners_line") if hasattr(row, "get") else getattr(row, "corners_line", None)
        _cards_over_api    = safe_odds(row.get("cards_over_odds")   if hasattr(row, "get") else getattr(row, "cards_over_odds", None))
        _cards_under_api   = safe_odds(row.get("cards_under_odds")  if hasattr(row, "get") else getattr(row, "cards_under_odds", None))
        _cards_line_api    = row.get("cards_line") if hasattr(row, "get") else getattr(row, "cards_line", None)

        # =========================
        # 1X2 — DIXON-COLES
        # =========================

        dc_home, dc_draw, dc_away = match_outcomes(lambda_home, lambda_away)

        # Normalizar Dixon-Coles
        dc_total = dc_home + dc_draw + dc_away
        dc_home /= dc_total
        dc_draw /= dc_total
        dc_away /= dc_total

        # =========================
        # ENSEMBLE (3 señales)
        # =========================
        # Combina Dixon-Coles + ELO puro + Form puro con pesos adaptativos.
        # agreement alto → más confianza en el modelo.

        ensemble = ensemble_predict(
            dc_probs     = (dc_home, dc_draw, dc_away),
            elo_home     = elo.get(home, 1500),
            elo_away     = elo.get(away, 1500),
            home_attack  = home_attack,
            home_defense = home_defense,
            away_attack  = away_attack,
            away_defense = away_defense,
        )

        home_win = clamp_prob(ensemble["home_win"])
        draw     = clamp_prob(ensemble["draw"])
        away_win = clamp_prob(ensemble["away_win"])

        # =========================
        # MONTE CARLO (4ª señal)
        # =========================
        # Simula 50,000 partidos con ruido en lambda (15% CV).
        # Si MC coincide con ensemble → confianza alta.
        # Si MC diverge → partido incierto → penalizar confianza.
        # Blend MC: 15% MC + 85% ensemble (señal de corrección suave).

        mc = simulate_match(lambda_home, lambda_away)

        mc_agreement = mc_confidence_vs_analytical(
            mc,
            {"home_win": home_win, "draw": draw, "away_win": away_win}
        )

        # Blend suave MC → reduce extremos del ensemble
        MC_WEIGHT = 0.15
        home_win = clamp_prob(home_win * (1 - MC_WEIGHT) + mc["home_win"] * MC_WEIGHT)
        draw     = clamp_prob(draw     * (1 - MC_WEIGHT) + mc["draw"]     * MC_WEIGHT)
        away_win = clamp_prob(away_win * (1 - MC_WEIGHT) + mc["away_win"] * MC_WEIGHT)

        # Ajuste de confianza según acuerdo MC ↔ ensemble
        # mc_agreement < 0.85 → el partido es genuinamente incierto
        if mc_agreement < 0.80:
            confidence *= 0.88
            mc_diverged += 1
        elif mc_agreement > 0.95:
            confidence = min(1.0, confidence * 1.03)   # ligero boost

        # Ajustar confianza según acuerdo entre señales (ensemble)
        confidence = min(1.0, max(0.0,
            confidence + ensemble["confidence_boost"]
        ))

        # Ajuste adicional por dominancia de córners y tiros
        # Ambas señales apuntan en la misma dirección → más confianza
        corner_shot_delta = corners_confidence_delta + shots_confidence_delta
        confidence = min(1.0, max(0.0, confidence + corner_shot_delta))

        # =========================
        # POISSON
        # =========================

        # MEJORA #3: pasar rho para usar Dixon-Coles tau-correction.
        # Mejora calibración de BTTS (-30pp de bias en sample 90d).
        poisson_probs = totals_and_btts(lambda_home, lambda_away,
                                        rho=DC_RHO_GLOBAL)

        # =========================
        # HALF-TIME PREDICTIONS (h2h_h1 / h2h_h2)
        # =========================
        # Modelamos primer tiempo con λ/2 (misma forma, mitad de goles esperados)
        # y segundo tiempo con λ * 0.55 (ligeramente más goles en el 2T que en el 1T)
        # Solo generamos probs si la API nos dio odds para esos mercados.

        lh_h1 = min(lambda_home / 2.0, 2.0)
        la_h1 = min(lambda_away / 2.0, 2.0)
        lh_h2 = min(lambda_home * 0.55, 2.0)
        la_h2 = min(lambda_away * 0.55, 2.0)

        # IMPORTANTE: inicializar model_probs ANTES de que h1/h2 le asigne keys.
        # Si NO se inicializa aquí y un partido tiene _h1_*_odds o _h2_*_odds
        # (viene del enrichment per-event), el `if` de abajo dispara
        # UnboundLocalError porque `model_probs = {...}` está más adelante.
        # Más adelante usamos .update(...) en vez de `=` para no perder estas keys.
        model_probs: dict = {}

        if _h1_home_odds or _h1_draw_odds or _h1_away_odds:
            from src.models.dixon_coles_model import match_outcomes as _mo
            h1h, h1d, h1a = _mo(lh_h1, la_h1)
            h1_total = h1h + h1d + h1a
            model_probs["h1_home"] = clamp_prob(h1h / h1_total)
            model_probs["h1_draw"] = clamp_prob(h1d / h1_total)
            model_probs["h1_away"] = clamp_prob(h1a / h1_total)

        if _h2_home_odds or _h2_draw_odds or _h2_away_odds:
            from src.models.dixon_coles_model import match_outcomes as _mo2
            h2h, h2d, h2a = _mo2(lh_h2, la_h2)
            h2_total = h2h + h2d + h2a
            model_probs["h2_home"] = clamp_prob(h2h / h2_total)
            model_probs["h2_draw"] = clamp_prob(h2d / h2_total)
            model_probs["h2_away"] = clamp_prob(h2a / h2_total)

        # =========================
        # OVER/UNDER POR LIGA
        # =========================
        # Calibra over25 con la tasa histórica real de la liga.
        # Eredivisie (62%) ≠ Ligue 1 (48%) ≠ Argentina (38%).
        # Blend: 80% Poisson + 20% tasa histórica de liga.

        league_key    = row.get("sport_key") if hasattr(row, "get") else getattr(row, "sport_key", "")
        league_over25 = get_over25_rate(league_key)

        poisson_probs["over25"] = (
            poisson_probs["over25"]  * (1 - OVER25_SHRINK)
            + league_over25          * OVER25_SHRINK
        )
        poisson_probs["under25"] = 1.0 - poisson_probs["over25"]

        # 🔥 sanity cap
        poisson_probs["over25"]   = min(poisson_probs["over25"],   0.75)
        poisson_probs["btts_yes"] = min(poisson_probs["btts_yes"], 0.75)

        totals_probs = totals_extended(lambda_home, lambda_away)

        # .update() (no `=`) — preserva las keys h1_*/h2_* asignadas arriba.
        # Antes del fix esto era `model_probs = {...}` que sobreescribía el dict
        # y borraba silenciosamente cualquier predicción de half-time.
        model_probs.update({
            "home_win": home_win,
            "draw": draw,
            "away_win": away_win,
            "over25": clamp_prob(poisson_probs["over25"]),
            "under25": clamp_prob(poisson_probs["under25"]),
            "btts": clamp_prob(poisson_probs["btts_yes"]),
            "btts_no": clamp_prob(poisson_probs["btts_no"])
        })

        model_probs.update(totals_probs)

        # =========================
        # CORNERS COMO MERCADO
        # =========================
        CORNERS_DEFAULT_ODDS = 1.80
        CARDS_DEFAULT_ODDS   = 1.80

        # Gate de cobertura: si la liga no tiene >= 50% de partidos con
        # córners/tarjetas en los últimos 30 días, NO generamos bets para
        # ese mercado — quedarían 'unresolved' indefinidamente y ensuciarían
        # la calibración + el bankroll en paper.
        _league_for_gate = row.get("sport_key") if hasattr(row, "get") else getattr(row, "sport_key", "")

        if corners_prediction and _has_coverage(_league_for_gate, "corners"):
            # Usar la línea de la API si está disponible, fallback 9.5
            cl = float(_corners_line_api) if _corners_line_api is not None else 9.5
            over_key  = f"corners_over_{cl}"
            under_key = f"corners_under_{cl}"
            model_probs[over_key]  = clamp_prob(corners_prediction["over95"])
            model_probs[under_key] = clamp_prob(corners_prediction["under95"])

        if cards_prediction and _has_coverage(_league_for_gate, "cards"):
            cl_c = float(_cards_line_api) if _cards_line_api is not None else 4.5
            model_probs[f"cards_over_{cl_c}"]  = clamp_prob(cards_prediction["over45"])
            model_probs[f"cards_under_{cl_c}"] = clamp_prob(cards_prediction["under45"])

        # =========================
        # OVER 1.5 / OVER 3.5 GOLES
        # =========================
        # totals_extended() ya los calcula — solo necesitamos cuotas de referencia.
        # Odds fijas: promedio real de mercado europeo.
        OVER15_ODDS  = 1.35   # ~74% implied — en la mayoría de partidos se marca
        UNDER15_ODDS = 3.20   # ~31% implied
        OVER35_ODDS  = 2.20   # ~45% implied — partido de muchos goles
        UNDER35_ODDS = 1.65   # ~61% implied
        # totals_probs ya está en model_probs via update(totals_probs)
        # Solo necesitamos agregar las odds de referencia al dict de odds más abajo

        # =========================
        # TOTAL TIROS AL ARCO
        # =========================
        SHOTS_DEFAULT_ODDS = 1.80
        if shots_prediction:
            model_probs["shots_over_5.5"]  = clamp_prob(shots_prediction["over55"])
            model_probs["shots_under_5.5"] = clamp_prob(shots_prediction["under55"])

        # =========================
        # DOBLE OPORTUNIDAD (1X / X2 / 12)
        # =========================
        # Probabilidades derivadas de 1x2. Odds derivadas de cuotas 1x2 existentes.
        model_probs["dc_1x"] = clamp_prob(home_win + draw)
        model_probs["dc_x2"] = clamp_prob(draw + away_win)
        model_probs["dc_12"] = clamp_prob(home_win + away_win)

        # Agregar AH y DNB (clave con línea embebida para resolución automática)
        if _p_ah_home is not None:
            _ah_key_home = f"ah_home_{_ah_line:+.1f}"   # ej: "ah_home_-1.5"
            _ah_key_away = f"ah_away_{_ah_line:+.1f}"   # ej: "ah_away_-1.5"
            model_probs[_ah_key_home] = clamp_prob(_p_ah_home)
            model_probs[_ah_key_away] = clamp_prob(_p_ah_away)

        model_probs["dnb_home"] = clamp_prob(_dnb["dnb_home"])
        model_probs["dnb_away"] = clamp_prob(_dnb["dnb_away"])

        # =========================
        # MARKET
        # =========================

        market_probs = {}

        if safe_odds(row.home_odds) and safe_odds(row.away_odds):
            mh, md, ma = market_probabilities(
                row.home_odds,
                row.draw_odds,
                row.away_odds
            )
            market_probs.update({
                "home_win": mh,
                "draw": md,
                "away_win": ma
            })

        if safe_odds(row.over25_odds):
            market_probs["over25"] = 1 / row.over25_odds

        if safe_odds(row.under25_odds):
            market_probs["under25"] = 1 / row.under25_odds

        if safe_odds(row.btts_yes_odds):
            market_probs["btts"] = 1 / row.btts_yes_odds

        if safe_odds(row.btts_no_odds):
            market_probs["btts_no"] = 1 / row.btts_no_odds

        # AH market probs (si tenemos odds del spreads market)
        if _p_ah_home is not None:
            if _ah_home_odds:
                market_probs[f"ah_home_{_ah_line:+.1f}"] = 1.0 / _ah_home_odds
            if _ah_away_odds:
                market_probs[f"ah_away_{_ah_line:+.1f}"] = 1.0 / _ah_away_odds

        # DNB market probs (API directo o derivadas)
        if _dnb_home_odds:
            market_probs["dnb_home"] = 1.0 / _dnb_home_odds
        if _dnb_away_odds:
            market_probs["dnb_away"] = 1.0 / _dnb_away_odds

        # Double Chance market probs (API)
        if _dc_1x_odds:
            market_probs["dc_1x"] = 1.0 / _dc_1x_odds
        if _dc_x2_odds:
            market_probs["dc_x2"] = 1.0 / _dc_x2_odds
        if _dc_12_odds:
            market_probs["dc_12"] = 1.0 / _dc_12_odds

        # Half-time market probs (API)
        if _h1_home_odds:
            market_probs["h1_home"] = 1.0 / _h1_home_odds
        if _h1_draw_odds:
            market_probs["h1_draw"] = 1.0 / _h1_draw_odds
        if _h1_away_odds:
            market_probs["h1_away"] = 1.0 / _h1_away_odds
        if _h2_home_odds:
            market_probs["h2_home"] = 1.0 / _h2_home_odds
        if _h2_draw_odds:
            market_probs["h2_draw"] = 1.0 / _h2_draw_odds
        if _h2_away_odds:
            market_probs["h2_away"] = 1.0 / _h2_away_odds

        # Corners / Cards market probs (API → usa odds reales en vez de fijas)
        if _corners_over_api:
            market_probs[f"corners_over_{_corners_line_api or 9.5}"] = 1.0 / _corners_over_api
        if _corners_under_api:
            market_probs[f"corners_under_{_corners_line_api or 9.5}"] = 1.0 / _corners_under_api
        if _cards_over_api:
            market_probs[f"cards_over_{_cards_line_api or 4.5}"] = 1.0 / _cards_over_api
        if _cards_under_api:
            market_probs[f"cards_under_{_cards_line_api or 4.5}"] = 1.0 / _cards_under_api

        # =========================
        # CALIBRATION
        # =========================

        probabilities = {}

        for market, model_prob in model_probs.items():

            market_prob = market_probs.get(market)

            if market_prob is None:
                probabilities[market] = model_prob
                continue

            edge = model_prob - market_prob
            edge = min(edge, 0.25)

            if is_strong_edge(edge, confidence):
                probabilities[market] = model_prob
            else:
                probabilities[market] = calibrate_probability(
                    model_prob,
                    market_prob,
                    edge
                )

        # =========================
        # ODDS
        # =========================

        odds = {
            "home_win": safe_odds(row.home_odds),
            "draw": safe_odds(row.draw_odds),
            "away_win": safe_odds(row.away_odds),
            "over25": safe_odds(row.over25_odds),
            "under25": safe_odds(row.under25_odds),
            "btts": safe_odds(row.btts_yes_odds),
            "btts_no": safe_odds(row.btts_no_odds),
            "dnb_home": _dnb_home_odds,
            "dnb_away": _dnb_away_odds,
        }

        if _p_ah_home is not None:
            odds[f"ah_home_{_ah_line:+.1f}"] = _ah_home_odds
            odds[f"ah_away_{_ah_line:+.1f}"] = _ah_away_odds

        # Corners odds: SOLO si la API trae odds reales.
        # El fallback a CORNERS_DEFAULT_ODDS generaba edge ficticio: el modelo
        # comparaba su prob Poisson contra una odd hardcoded (no la del mercado)
        # y "detectaba" ventaja aunque no la hubiera.
        if _corners_over_api and _corners_line_api is not None:
            _cl = float(_corners_line_api)
            odds[f"corners_over_{_cl}"]  = _corners_over_api
            odds[f"corners_under_{_cl}"] = _corners_under_api or CORNERS_DEFAULT_ODDS

        # Cards odds: SOLO si la API trae odds reales. Mismo motivo.
        # Además: cards requiere datos históricos en la liga — get_team_cards
        # ya retorna None si hay < 5 partidos con data, por lo que
        # cards_prediction ya no se genera en esos casos.
        if _cards_over_api and _cards_line_api is not None:
            _cdl = float(_cards_line_api)
            odds[f"cards_over_{_cdl}"]  = _cards_over_api
            odds[f"cards_under_{_cdl}"] = _cards_under_api or CARDS_DEFAULT_ODDS

        # Over 1.5 / 3.5 odds de referencia fijas
        odds["over_1.5"]  = OVER15_ODDS
        odds["under_1.5"] = UNDER15_ODDS
        odds["over_3.5"]  = OVER35_ODDS
        odds["under_3.5"] = UNDER35_ODDS

        # Total shots odds fijas
        if shots_prediction:
            odds["shots_over_5.5"]  = SHOTS_DEFAULT_ODDS
            odds["shots_under_5.5"] = SHOTS_DEFAULT_ODDS

        # Double Chance: preferir API, fallback a derivadas de 1x2
        if _dc_1x_odds:
            odds["dc_1x"] = _dc_1x_odds
        if _dc_x2_odds:
            odds["dc_x2"] = _dc_x2_odds
        if _dc_12_odds:
            odds["dc_12"] = _dc_12_odds
        if not (_dc_1x_odds and _dc_x2_odds and _dc_12_odds):
            _h = safe_odds(row.home_odds)
            _d = safe_odds(row.draw_odds)
            _a = safe_odds(row.away_odds)
            if _h and _d and _a:
                if not _dc_1x_odds:
                    odds["dc_1x"] = round(1.0 / (1.0/_h + 1.0/_d), 3)
                if not _dc_x2_odds:
                    odds["dc_x2"] = round(1.0 / (1.0/_d + 1.0/_a), 3)
                if not _dc_12_odds:
                    odds["dc_12"] = round(1.0 / (1.0/_h + 1.0/_a), 3)

        # Half-time odds (de API)
        if _h1_home_odds:
            odds["h1_home"] = _h1_home_odds
        if _h1_draw_odds:
            odds["h1_draw"] = _h1_draw_odds
        if _h1_away_odds:
            odds["h1_away"] = _h1_away_odds
        if _h2_home_odds:
            odds["h2_home"] = _h2_home_odds
        if _h2_draw_odds:
            odds["h2_draw"] = _h2_draw_odds
        if _h2_away_odds:
            odds["h2_away"] = _h2_away_odds

        # =========================
        # CALIBRACIÓN DE PROBABILIDADES
        # =========================
        # Mejora 3: Corrección global de sobrecalibración.
        # Walk-forward backtest (332 bets) demuestra que el modelo
        # sobreestima en TODOS los rangos:
        #   Pred 55% → Real 39% (-16pp)
        #   Pred 74% → Real 60% (-15pp)
        # Factor global: prob_corregida = prob * 0.85
        # Después aplica factores por mercado si hay suficientes datos.
        GLOBAL_CALIBRATION = 0.85

        # Liga del partido — usado para calibración específica (Mundial/Euro/etc.)
        _row_league = row.get("league", row.get("sport_key", "")) if hasattr(row, "get") \
                      else getattr(row, "league", "") or getattr(row, "sport_key", "")

        for market in list(probabilities.keys()):
            # Paso 1: corrección global de sobreconfianza
            probabilities[market] = probabilities[market] * GLOBAL_CALIBRATION

            # Paso 2: calibración por mercado.
            # MEJORA #5 — apply_calibration usa isotonic regression (curva
            # no-paramétrica) cuando hay sample suficiente (n>=30); si no,
            # cae al factor escalar suavizado bayesianamente. Para mercados
            # sin sample, deja la prob sin tocar.
            if cal_active:
                probabilities[market] = apply_calibration(
                    probabilities[market], market, league=_row_league
                )

            # Clamp final
            probabilities[market] = min(0.95, max(0.05, probabilities[market]))

        # =========================
        # SANITY CHECK (🔥 NUEVO)
        # =========================

        clean_probabilities = {}

        for market, p in probabilities.items():

            # ❌ probabilidades irreales
            if p is None:
                continue

            if p < 0.03 or p > 0.90:
                continue

            # ❌ evitar favoritos extremos en odds altas
            odd = odds.get(market)
            if odd and odd > 4 and p > 0.6:
                continue

            clean_probabilities[market] = p


        # =========================
        # LINE MOVEMENT
        # =========================
        # Detecta si hay dinero sharp entrando en algún lado.
        # No consume créditos API — usa las opening_odds ya guardadas en DB.

        match_key = row.get("match_key", "")
        line = get_line_movement(match_key)

        # =========================
        # CONSENSO DE BOOKMAKERS
        # =========================
        # spread_pct alto (>15%) = mercado dividido = más incertidumbre real
        # spread_pct bajo (<5%)  = mercado muy eficiente = bets más confiables
        # soft line: nuestra mejor odd >> consenso (+10%) = edge potencial extra
        # pocos bookmakers (<3)  = mercado poco líquido = más caution

        spread_pct      = row.get("h2h_spread_pct")   if hasattr(row, "get") else getattr(row, "h2h_spread_pct", None)
        bk_count        = row.get("bookmaker_count")   if hasattr(row, "get") else getattr(row, "bookmaker_count", None)
        cons_home       = row.get("consensus_home_odds") if hasattr(row, "get") else getattr(row, "consensus_home_odds", None)
        best_home_odd   = safe_odds(row.home_odds)

        consensus_conf_adj = 1.0
        soft_line_detected = False

        if spread_pct is not None and spread_pct > 0:
            if spread_pct > 20:
                consensus_conf_adj *= 0.82     # mercado muy dividido
            elif spread_pct > 15:
                consensus_conf_adj *= 0.90
            elif spread_pct > 10:
                consensus_conf_adj *= 0.95
            elif spread_pct < 5:
                consensus_conf_adj *= 1.02     # mercado eficiente → ligero boost

        if bk_count is not None and bk_count < 3:
            consensus_conf_adj *= 0.88         # pocos bookmakers = mercado poco líquido

        # ── FILTRO DURO: liquidez mínima ──────────────────────────────────
        # Menos de 4 casas = mercado no tiene consenso suficiente → skip partido
        if should_skip_low_liquidity(bk_count):
            print(f"  Skip liquidez: {home} vs {away} ({bk_count} books < 4)")
            skipped_no_odds += 1
            continue

        # Detección de línea blanda: mejor precio >> consenso (+10%)
        if best_home_odd and cons_home and cons_home > 0:
            if best_home_odd > cons_home * 1.10:
                soft_line_detected = True
                # Soft line: potencial edge extra, pero también posible error del book
                # Mantener el edge pero marcar para revisión (no cambia confianza)

        if consensus_conf_adj != 1.0:
            confidence = max(0.1, min(1.0, confidence * consensus_conf_adj))

        if spread_pct and spread_pct > 15:
            print(
                f"  Spread {spread_pct:.1f}%  "
                f"books={bk_count}  "
                f"{'soft line!' if soft_line_detected else ''}"
            )

        # =========================
        # VALUE BETS
        # =========================

        raw_bets = find_value_bets(clean_probabilities, odds)

        # Aplicar señal de línea + filtro duro por apuesta
        filtered_bets = []
        for bet in raw_bets:
            # ── FILTRO DURO: movimiento de línea en contra ─────────────────
            # Si la línea cayó >8% en nuestra dirección, el edge ya fue
            # absorbido por el mercado sharp antes de que apostemos
            if line_moved_against(bet["market"], line):
                mkt   = bet["market"]
                delta = line.get(f"{mkt.replace('_win','')}_movement",
                                  line.get("over25_movement", 0))
                print(f"  Skip linea: {home} vs {away} | {mkt} | caida {delta*100:.1f}%")
                sharp_rejected += 1
                continue

            adj_edge, adj_conf = apply_line_movement_signal(
                bet["market"], bet["edge"], confidence, line
            )
            if adj_edge > bet["edge"]:
                sharp_confirmed += 1
            elif adj_edge < bet["edge"]:
                sharp_rejected += 1
            # Calculamos factor de ajuste sobre edge_ev y lo aplicamos también
            # a edge_market (el verdadero edge usado por filtros downstream).
            _adj_factor = (adj_edge / bet["edge"]) if bet["edge"] > 0 else 1.0
            bet["edge"]        = adj_edge
            bet["edge_market"] = max(min(bet.get("edge_market", 0) * _adj_factor, 0.25), -0.25)
            bet["probability"] = min(0.95, bet["probability"] * (1 + (adj_conf - confidence) * 0.3))
            filtered_bets.append(bet)

        raw_bets = filtered_bets

        bets = bet_quality_filter(raw_bets) or raw_bets
        bets = market_intelligence_filter(bets) or bets
        bets = add_market_score(bets)

        if not bets:
            continue

        # =========================
        # FILTROS DE CALIDAD (optimizados con 332 bets walk-forward)
        # =========================
        # ⚠️  IMPORTANTE — bug detectado 04-may-26 por model-auditor:
        # `bet["edge"]` es `edge_ev` ((prob*odds)-1), NO el edge real.
        # `bet["edge_market"]` (prob - implied_prob) es el verdadero edge.
        # Antes filtrábamos `bet["edge"] < 0.10` que con odds 5.0 dejaba
        # pasar bets con solo 5% de edge real → bucket 15-20% edge tenía
        # -23.7% ROI (overfit). Ahora filtramos sobre edge_market real.
        MIN_EDGE = 0.05        # default — usado si el mercado no está en MIN_EDGE_BY_MARKET
        MAX_ODDS = 3.80        # elimina longshots que no pegan

        # MEJORA #3 (Sprint 2 — 06-may-26): MIN_EDGE adaptativo por mercado.
        # Valores derivados del backtest 120d sobre 585 bets — para cada mercado
        # se barrió el threshold 0.03..0.15 y se eligió un punto conservador
        # (no el óptimo absoluto, para evitar overfit con muestras chicas).
        # Mercados sin entrada caen al MIN_EDGE global (0.05).
        MIN_EDGE_BY_MARKET = {
            # Mercados grandes (n≥100) — mantenemos cerca del 5% global para
            # no recortar +EV ya validado en producción. El A/B sobre 386 bets
            # muestra que subir estos a 0.06 recorta ~6 bets ganadoras.
            "home_win":     0.05,
            "over25":       0.05,
            "under25":      0.04,   # backtest: +EV a edge bajo (n=25)

            # Mercados pequeños/ruidosos — exigir mayor edge
            "draw":         0.08,   # n=15 — alta varianza
            "btts":         0.06,   # bias estructural BTTS sobreestimado
            "btts_no":      0.10,   # backtest -42% ROI a 5% → solo alto edge
            "dnb_away":     0.08,   # backtest: positivo solo en edge alto

            # Doble oportunidad (cuotas bajas — necesitan más edge para +EV)
            "dc_1x":        0.06,
            "dc_x2":        0.06,

            # Asian Handicap — SUBIDOS 11-may-26 tras alerta de calibración:
            #   ah_home_fav real win rate 20% (predicho 58%, n=15, factor=0.78)
            #   ah_away_fav real win rate 30% (predicho 64%, n=23, factor=0.77)
            #   Investigación 90d (41 bets): avg pred 61%, real 27%, ROI -46%.
            # El modelo sobre-estima los favoritos AH (acierta el ganador pero
            # falla el cover del handicap). Hasta que se recalibre con más
            # data, requerir EDGE mucho más alto. fav/pk al 12-15%, dogs OK.
            "ah_home_fav":  0.15,
            "ah_home_pk":   0.10,   # factor=0.82, leve bias
            "ah_home_dog":  0.08,   # factor=1.01, calibrado
            "ah_away_fav":  0.15,
            "ah_away_pk":   0.06,   # factor=0.97, calibrado
            "ah_away_dog":  0.08,
            # `away_win` y `dnb_home` siguen BLOQUEADOS (líneas más abajo) —
            # no figuran aquí porque el filtro de mercado-bloqueado los corta.
        }

        # Ligas problemáticas: exigir más edge
        TOUGH_LEAGUES = {
            "soccer_italy_serie_a",
            "soccer_usa_mls",
            "soccer_france_ligue_one",
        }
        # Ligas bloqueadas: ROI negativo consistente en walk-forward backtest
        # + bloqueos NUEVOS por desempeño en producción (60d rolling, 04-may-26)
        BLOCKED_LEAGUES = {
            "soccer_fifa_world_cup_qualifiers_europe",  # -44.6% ROI, datos basura
            "soccer_uefa_europa_league",                # -64.5% ROI
            "soccer_netherlands_eredivisie",             # -50.3% ROI
            "soccer_conmebol_copa_libertadores",         # -100% ROI (2 bets, 0 wins)
            "soccer_uefa_champs_league",                 # -100% ROI (1 bet, 0 wins)
            # ── Bloqueos por producción (model-auditor RED, 04-may-26) ──
            "soccer_japan_j_league",                     # -12.46u, 17% WR (24 bets/60d) — peor liga del modelo
            "soccer_turkey_super_league",                # -1.05u, 25% WR (12 bets/60d) — mercado se hizo eficiente
            "soccer_norway_eliteserien",                 # solo 32 partidos historicos — sample insuficiente para apostar
        }
        _league = row.get("league", row.get("sport_key", ""))
        if _league in BLOCKED_LEAGUES:
            continue

        # ── MEJORA #12: filtro de partidos "raros" ─────────────────
        # Dead rubber (ambos en zona media, fin de temporada) o asimetría
        # extrema de motivación → resultado impredecible. Saltar.
        try:
            from src.features.motivation_factor import is_unreliable_match
            _unreliable, _why = is_unreliable_match(
                row.get("home_team",""), row.get("away_team",""), _league
            )
            if _unreliable:
                continue
        except Exception:
            pass   # si la feature falla, no bloquea

        # ── Penalización Lun-Mié (ROI -27% a -60%) ──────────────────
        # Partidos entre semana (copas, recuperaciones) tienen datos
        # menos fiables y líneas más eficientes.
        _match_dow = None
        try:
            _match_dow = pd.to_datetime(date).weekday()  # 0=Lun ... 6=Dom
        except Exception as e:
            # Si la fecha no parsea, no aplicamos penalización midweek.
            # Loguear permite detectar formatos raros en upcoming_matches.
            print(f"⚠️  No se pudo parsear match_date={date!r} para weekday: {e}")
        MIDWEEK_DAYS = {0, 1, 2}     # Lunes, Martes, Miércoles
        _midweek = _match_dow in MIDWEEK_DAYS if _match_dow is not None else False

        # =========================
        # GRUPOS MUTUAMENTE EXCLUYENTES
        # =========================
        # Si apostamos home_win, NO podemos apostar away_win ni draw.
        # Si apostamos over25, NO podemos apostar under25.
        # Esto previene apuestas contradictorias en el mismo partido.
        _EXCLUSIVE_GROUPS = {
            "home_win":  "1x2",
            "draw":      "1x2",
            "away_win":  "1x2",
            "dnb_home":  "1x2",    # misma dirección que home_win
            "dnb_away":  "1x2",    # misma dirección que away_win
            "dc_1x":     "dc",
            "dc_x2":     "dc",
            "dc_12":     "dc",
            "over25":    "goals_total",
            "under25":   "goals_total",
            "over_1.5":  "goals_15",
            "under_1.5": "goals_15",
            "over_3.5":  "goals_35",
            "under_3.5": "goals_35",
            "btts":      "btts",
            "btts_no":   "btts",
            "h1_home":   "h1_1x2",
            "h1_draw":   "h1_1x2",
            "h1_away":   "h1_1x2",
            "h2_home":   "h2_1x2",
            "h2_draw":   "h2_1x2",
            "h2_away":   "h2_1x2",
        }

        # Mercados con odds fijas → desactivados (edges inflados sin odds reales)
        _DISABLED_MARKETS = {
            "over_1.5", "under_1.5",     # odds fija 1.35/3.20 — edge irreal
            "over_3.5", "under_3.5",     # odds fija 2.20/1.65 — edge irreal
            "shots_over_5.5", "shots_under_5.5",  # odds fija 1.80
            "dc_12",                     # "cualquiera gana" a ~1.20 — nunca tiene edge
        }

        MAX_BETS_PER_MATCH = 2     # máximo 2 bets por partido
        match_bets_count = 0
        groups_used = set()

        for bet in bets:
            if match_bets_count >= MAX_BETS_PER_MATCH:
                break

            mkt = bet["market"]

            # ── Mercados desactivados ─────────────────────────────────
            if mkt in _DISABLED_MARKETS:
                continue

            # ── Mejora 1: Bloquear away_win (27% WR, -36.5% ROI) ────
            if mkt == "away_win":
                continue

            # ── Mercados desactivados individualmente ────────────────
            # Kill-switch basado en auditoría 17-may-2026 sobre 746 bets
            # resueltas. Markets con n>=10 y ROI negativo persistente:
            #   away_win:      -32% ROI (n=53)
            #   btts:          -21% ROI (n=38)
            #   under25:       -37% ROI (n=29)
            #   dnb_home:      -20% ROI (n=16) — bloqueado original
            #   ah_home_+0.0:  -65% ROI (n=13) — pk no calibrado
            # Resultado proyectado: ROI global pasa de -7% a aprox +2%
            # eliminando ~140 bets perdedoras sin tocar las ganadoras.
            # Si en 30 días aparece data nueva que recalibra alguno,
            # revivir con threshold alto y monitorear.
            _BLOCKED_MARKETS = {
                "dnb_home",
                "away_win",
                "btts",
                "under25",
            }
            if mkt in _BLOCKED_MARKETS:
                continue
            # AH pk (handicap +0.0 / 0.0): bloqueado por -65% ROI persistente
            if mkt.startswith("ah_") and ("+0.0" in mkt or mkt.endswith("_0.0")):
                continue

            # ── Mejora 4: Sweet spots de odds por mercado ────────────
            # Rangos donde el modelo ha demostrado edge real:
            #   home_win @1.5-2.0 → 81% WR, +42% ROI
            #   over25   @2.0-2.5 → 53% WR, +16% ROI
            #   draw     @2.5-4.0 → 100% WR (muestra chica, mantener amplio)
            _odds = bet["odds"]
            if mkt == "home_win" and not (1.3 <= _odds <= 3.80):
                continue
            if mkt == "over25" and not (1.50 <= _odds <= 3.00):
                continue
            if mkt == "draw" and not (2.5 <= _odds <= 5.0):
                continue

            # ── Edge mínimo dinámico (Mejora #3 — por mercado) ──────
            # Usar `edge_market` (prob - implied) — el verdadero edge.
            # `edge` (= edge_ev) infla con odds altas y rompe filtros.
            _edge_real = bet.get("edge_market", bet["edge"])

            # 1) Threshold base por mercado (Mejora #3)
            # FIX 11-may-26: para mercados AH parametrizados (`ah_home_-0.5`,
            # `ah_away_-1.0`, etc.), el lookup directo en MIN_EDGE_BY_MARKET
            # FALLABA (el dict tiene `ah_home_fav` no `ah_home_-0.5`) y caía
            # al default 0.05. Esto dejó pasar 41 ah_fav bets en 90d con
            # edge=5-12% que en realidad tenían win rate 27%. Ahora hacemos
            # fallback al grupo AH (ah_home_fav/pk/dog) antes del default.
            _min_edge = MIN_EDGE_BY_MARKET.get(mkt)
            if _min_edge is None:
                from src.models.calibration_monitor import _ah_group
                grp = _ah_group(mkt)
                if grp is not None:
                    _min_edge = MIN_EDGE_BY_MARKET.get(grp, MIN_EDGE)
                else:
                    _min_edge = MIN_EDGE

            # 2) Bump por liga problemática (Italia, MLS, Ligue 1)
            if _league in TOUGH_LEAGUES:
                _min_edge = max(_min_edge, 0.07)

            # 3) Bump por partido entre semana Lun-Mié (datos menos fiables)
            if _midweek:
                _min_edge *= 1.4

            if _edge_real < _min_edge:
                continue
            if bet["odds"] > MAX_ODDS:
                continue

            # ── Filtro de contradicción ───────────────────────────────
            # Si ya apostamos en un grupo, no apostar en el mismo grupo
            group = _EXCLUSIVE_GROUPS.get(mkt)
            if group and group in groups_used:
                continue

            # MEJORA #14: pasar market+league para que kelly_stake module la
            # fracción según el CLV histórico (ver _adjusted_kelly_fraction).
            stake = kelly_stake(
                bet["probability"], bet["odds"],
                bankroll=current_bankroll,
                market=mkt,
                league=_league,
            )

            all_bets.append({
                "match":       f"{home} vs {away}",
                "match_date":  date,
                "league":      row.get("league", row.get("sport_key", "")),
                "market":      bet["market"],
                "probability": bet["probability"],
                "odds":        bet["odds"],
                # Guardamos `edge_market` (verdadero edge vs línea) en lugar
                # de `edge_ev`. A partir de 04-may-26 bets_history.edge mide
                # `prob - implied`, no `(prob*odds)-1` (que era inflado).
                "edge":        bet.get("edge_market", bet["edge"]),
                "stake":       stake
            })

            match_bets_count += 1
            if group:
                groups_used.add(group)

    # =========================
    # DEBUG
    # =========================

    print("\n📊 DEBUG SUMMARY")
    print("Sin odds:           ", skipped_no_odds)
    print("Fallback usados:    ", fallback_used)
    print("xG proxy usado:     ", xg_used)
    print("H2H usado:          ", h2h_used)
    print("Corners model:      ", corners_used)
    print("Shots model:        ", shots_used)
    print("Fatiga detectada:   ", fatigued_teams)
    print("Weather ajustado:   ", weather_adjusted)
    print("MC divergio:        ", mc_diverged)
    print("Sharp confirmados:  ", sharp_confirmed)
    print("Sharp rechazados:   ", sharp_rejected)
    print("Bets generadas:     ", len(all_bets))

    if all_bets:
        print("\n  Signals: DC + ELO + Form(venue) + MC + Corners + Shots + Congestion + Weather")

    if all_bets:
        ranked = rank_bets(all_bets)

        print("\n🔥 BEST BETS\n")

        for _, bet in ranked.head(10).iterrows():
            print(
                bet["match"],
                "|", bet["market"],
                "| edge:", round(bet["edge"], 3),
                "| odds:", bet["odds"],
                "| stake:", round(bet["stake"], 2)
            )

    # =========================
    # PORTFOLIO EXPOSURE
    # =========================
    # Antes de guardar, verifica que el stake total no exceda límites seguros
    # y que no haya concentración excesiva en un solo tipo de mercado.
    #
    # Límites:
    #   - Stake total <= 15% del bankroll (protección de ruina)
    #   - Un mercado no puede representar > 50% del stake total (diversificación)
    #   - Si se viola algún límite → escalar stakes proporcionalmente

    # =========================
    # CORRELACIÓN POR PARTIDO
    # =========================
    # Cuando tenemos 2+ apuestas en el mismo partido, no son independientes:
    # si Botafogo gana 3-0 ambas ganan juntas; si pierde 0-1 ambas pierden.
    # Aplicamos factor 1/sqrt(n) por apuesta para reducir la sobreexposición
    # sin eliminar las apuestas correladas (que sí tienen valor individual).
    #
    # Factor:  1 apuesta → 100%  |  2 apuestas → 71% c/u  |  3 → 58% c/u

    from collections import Counter
    match_counts = Counter(b["match"] for b in all_bets)
    corr_adjusted = 0
    for b in all_bets:
        n = match_counts[b["match"]]
        if n > 1:
            factor = 1.0 / (n ** 0.5)
            b["stake"] = round(b["stake"] * factor, 2)
            corr_adjusted += 1

    if corr_adjusted:
        print(f"\n🔗 Correlacion ajustada: {corr_adjusted} bets en partidos con apuestas multiples")
        for match, n in match_counts.items():
            if n > 1:
                factor = round(1.0 / (n ** 0.5) * 100)
                print(f"   {match}  ({n} bets → {factor}% stake c/u)")

    # =========================
    # CAMBIO A: FILTRO SOSPECHOSAS
    # =========================
    # Elimina bets donde el edge llegó al tope artificial del modelo (0.499)
    # o donde la prob del modelo es > 2x la probabilidad implícita del mercado.
    # Estas bets casi siempre pierden porque no hay datos reales detrás.

    MAX_RELIABLE_EDGE = 0.499
    MAX_PROB_RATIO    = 2.0
    suspicious_removed = 0
    clean_bets = []
    for b in all_bets:
        odds           = float(b.get("odds", 0))
        prob           = float(b.get("probability", 0))
        edge           = float(b.get("edge", 0))
        market_implied = 1.0 / odds if odds > 0 else 0
        if edge >= MAX_RELIABLE_EDGE:
            suspicious_removed += 1
            continue
        if market_implied > 0 and prob > MAX_PROB_RATIO * market_implied:
            suspicious_removed += 1
            continue
        clean_bets.append(b)
    all_bets = clean_bets
    if suspicious_removed:
        print(f"🚫 Sospechosas eliminadas:  {suspicious_removed}")

    if all_bets and current_bankroll > 0:
        total_stake = sum(b["stake"] for b in all_bets)
        max_total   = current_bankroll * 0.15

        # ── Concentración por mercado ─────────────────────────────────────
        market_stakes: dict[str, float] = {}
        for b in all_bets:
            mkt = b["market"]
            market_stakes[mkt] = market_stakes.get(mkt, 0) + b["stake"]

        dominant_mkt   = max(market_stakes, key=market_stakes.get)
        dominant_stake = market_stakes[dominant_mkt]
        concentration  = dominant_stake / total_stake if total_stake > 0 else 0

        # ── Calcular factor de escala ─────────────────────────────────────
        scale = 1.0

        if total_stake > max_total:
            scale = min(scale, max_total / total_stake)

        if concentration > 0.60 and total_stake > 5:
            # Penalizar el mercado dominante para reducir concentración
            penalty = 0.75
            for b in all_bets:
                if b["market"] == dominant_mkt:
                    b["stake"] = round(b["stake"] * penalty, 2)
            total_stake = sum(b["stake"] for b in all_bets)
            if total_stake > max_total:
                scale = min(scale, max_total / total_stake)

        if scale < 1.0:
            for b in all_bets:
                b["stake"] = round(b["stake"] * scale, 2)
            print(
                f"\n⚖️  Portfolio ajustado: stake total {total_stake:.2f}u → "
                f"{sum(b['stake'] for b in all_bets):.2f}u  "
                f"(máx {max_total:.2f}u = 15% bankroll)"
            )

        if concentration > 0.50:
            print(
                f"  📊 Concentración {dominant_mkt}: {concentration*100:.0f}% del portfolio"
            )

        print(
            f"\n💼 Portfolio: {len(all_bets)} bets | "
            f"Stake total: {sum(b['stake'] for b in all_bets):.2f}u | "
            f"Bankroll: {current_bankroll:.2f}u | "
            f"Exposición: {sum(b['stake'] for b in all_bets)/current_bankroll*100:.1f}%"
        )

    # =========================
    # PAPER-TRADING SPLIT (Kill-Switch Mundial)
    # =========================
    # Las ligas en PAPER_ONLY_LEAGUES NO entran a bets_history. Su única
    # huella es data/paper_trades.jsonl. Esto garantiza que durante el
    # periodo de validación del Mundial 2026 el modelo pueda predecir sin
    # contaminar bankroll, ROI ni CLV.
    from config.settings import PAPER_ONLY_LEAGUES

    real_bets  = []
    paper_bets = []
    for b in all_bets:
        if b.get("league") in PAPER_ONLY_LEAGUES:
            b["is_paper"] = True
            paper_bets.append(b)
        else:
            b["is_paper"] = False
            real_bets.append(b)

    if paper_bets:
        _persist_paper_bets(paper_bets)
        print(f"\n📝 PAPER-TRADING: {len(paper_bets)} bets de "
              f"{', '.join(sorted(PAPER_ONLY_LEAGUES))} → data/paper_trades.jsonl "
              f"(NO insertadas en bets_history)")

    save_bets(real_bets)

    # Devolvemos TODAS (real + paper) para que notify_telegram las muestre
    return all_bets


def _persist_paper_bets(paper_bets: list[dict]) -> None:
    """
    Append-only log de bets paper-trading.

    Formato JSONL en data/paper_trades.jsonl. Cada línea es un dict serializable.
    No usa la DB (queremos cero contaminación con datos reales).
    """
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    paper_log = Path(__file__).resolve().parents[2] / "data" / "paper_trades.jsonl"
    paper_log.parent.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with open(paper_log, "a", encoding="utf-8") as f:
            for b in paper_bets:
                row = {
                    "logged_at":   timestamp,
                    "match":       b.get("match"),
                    "match_date":  str(b.get("match_date", "")),
                    "league":      b.get("league"),
                    "market":      b.get("market"),
                    "probability": float(b.get("probability", 0)),
                    "odds":        float(b.get("odds", 0)),
                    "edge":        float(b.get("edge", 0)),
                    "stake":       float(b.get("stake", 0)),
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        # No rompemos el pipeline si el log falla — solo avisamos
        print(f"⚠️  No se pudo escribir paper_trades.jsonl: {e}")