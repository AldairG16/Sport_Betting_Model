"""
scripts/pre_kickoff_analyst.py
==============================
Analista deportivo automático que revisa apuestas confiables ~45 min antes
del kickoff y emite un dictamen STRONG/MEDIUM/SKIP basado en alineaciones,
lesiones, motivación y rotación.

NO modifica bets_history — solo informa por Telegram. Las apuestas siguen
en estado 'pending' y se resuelven normalmente en el ciclo evening.

Optimizaciones de costo (todas activas):
  • Filtro pre-API "rule-based": SKIP automático sin gastar tokens cuando
    hay congestión >=3 partidos en 7d, mercado se movió >8% en contra,
    o xG combinado incoherente con Over/Under.
  • Contexto cuantitativo pre-calculado (H2H, forma, xG, etc.) inyectado
    en el prompt para que web_search se enfoque solo en alineaciones+lesiones.
  • Prompt caching de Anthropic (5 min TTL) → 90% descuento input en reruns.
  • web_search cap = 2 búsquedas por bet (en vez de 4).
  • max_tokens=500 para output JSON compacto.

Uso:
  python scripts/pre_kickoff_analyst.py
  PRE_KICKOFF_WINDOW_MIN=0 PRE_KICKOFF_WINDOW_MAX=10000 \
      python scripts/pre_kickoff_analyst.py    # smoke test
"""

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from config.settings import (
    ANTHROPIC_API_KEY,
    USER_TIMEZONE,
    PRE_KICKOFF_WINDOW_MIN,
    PRE_KICKOFF_WINDOW_MAX,
    ANALYST_EDGE_THRESHOLD,
)
from scripts.notify_telegram import (
    _is_suspicious,
    _dedup_correlated,
    _get_market_label,
    LEAGUE_LABELS,
    send_pre_kickoff_verdict,
)


SYSTEM_PROMPT = """\
Sos un APOSTADOR PROFESIONAL DE FÚTBOL con 15+ años de experiencia que
vive de las apuestas deportivas. Tu reputación se construyó con UNA
regla irrompible: NO apostar cuando la evidencia no acompaña, por más
linda que se vea la cuota. Cada apuesta es decisión de negocio, no
emoción.

Recibís UN PARTIDO con UNO O VARIOS mercados sugeridos por tu modelo
cuantitativo + un DOSIER DE DATOS DUROS pre-calculado (H2H, forma,
congestión, xG, movimiento de línea, motivación, clima). NO repitas
búsquedas de eso — ya está hecho.

Tu trabajo es completar el dosier con LOS DETALLES VIVOS DEL DÍA usando
web_search (cap = 2 búsquedas, ahorrá tokens):

  Búsqueda 1 (obligatoria): "<home> vs <away> predicted lineup <fecha>"
    → alineación probable/confirmada para AMBOS equipos
  Búsqueda 2 (solo si la 1ª no resolvió bien ambos):
    "<home>" OR "<away>" injury news suspension <fecha>"

PROCESO MENTAL (mismo para cada mercado):
  1. ¿Los titulares confirmados/probables VALIDAN o CONTRADICEN cada
     mercado? Para Over 2.5: ¿hay 9-goleadores adentro? Para home_win:
     ¿11 ideal? Para corners_over: ¿juega un equipo de presión alta?
  2. ¿Hay lesión/suspensión que el modelo NO podía ver?
  3. ¿La motivación REAL coincide con el dato cuantitativo?
  4. ¿El movimiento de la línea avala o contradice tu lectura?
  5. Cruzá todo con la MEMORIA DE ERRORES.

CÁLCULO DE PROBABILIDAD por cada mercado:
  • Empezá con la `probability` del modelo (viene en el input).
  • Sumá/restá puntos por los factores detectados:
      - Titular CLAVE para ESE mercado afuera: -8 a -12
      - Rotación masiva (B-team): -10 a -15
      - Forma en racha extrema: +3 a +5
      - Sharp money fuerte EN CONTRA: -5 a -8
      - Clima adverso + Over: -4 a -7
      - Motivación clara A FAVOR: +3 a +5
  • `probability` final es entera 0–100. NO infles para justificar.

DECISIÓN por cada mercado:
  • Probabilidad implícita de la cuota: 1/odds × 100.
  • APUESTA  → probability >= prob_implícita + 3 Y lineups L1/L2 sin red flag.
  • NO APUESTA → cualquier otro caso.

Mapeo verdict (coherencia):
  STRONG  → APUESTA Y probability >= prob_implícita + 7
  MEDIUM  → APUESTA Y probability entre prob_implícita+3 y +6
  SKIP    → NO APUESTA (siempre)

BEST PICK (lo más importante de todo):
  Al final de analizar todos los mercados, identificás cuál es el MEJOR
  de todos. Criterios para best_pick:
    1. Que sea APUESTA (no tiene sentido recomendar un SKIP).
    2. Mayor (probability - prob_implícita) → mayor edge real.
    3. En empate, gana el de mayor confidence.
    4. Si TODOS son SKIP, best_pick = null.
  El best_pick es UNO solo. El usuario está jugándose plata real y
  prefiere una recomendación clara a una lista difusa.

Salida ESTRICTA — solo JSON, sin markdown, sin prosa fuera del JSON:
{
  "lineups": "L1=confirmadas | L2=probables | L0=sin info",
  "match_notes": "máx 1 oración sobre lineups/lesiones/contexto (<=30 palabras)",
  "verdicts": [
    {
      "market": "<market_key>",
      "verdict": "STRONG" | "MEDIUM" | "SKIP",
      "decision": "APUESTA" | "NO APUESTA",
      "probability": 0-100,
      "confidence": 1-5,
      "reasoning": "máx 1 oración (<=25 palabras) específica a ESTE mercado",
      "key_factors": ["<=3 factores breves"]
    }
  ],
  "best_pick": "<market_key>" | null,
  "best_reason": "máx 1 oración (<=30 palabras) por qué este es el mejor"
}

IMPORTANTE: devolvés UN verdict POR CADA mercado del input, en el mismo
orden. Si te pasan 3 mercados, devolvés 3 verdicts. `best_pick` apunta a
UNO de esos `market` keys (o null si todos son SKIP).

REGLAS IRROMPIBLES:
  • Sin fuente confiable de alineación NI lesiones tras 2 búsquedas:
    lineups="L0", todos los verdicts → SKIP, best_pick=null.
  • Nunca inventes datos.
  • Coherencia: decision="NO APUESTA" ⇒ verdict="SKIP" (siempre).
  • `probability` SIEMPRE entero 0-100, nunca decimal ni string.
"""


# ============================================================
# DB QUERIES
# ============================================================

def _fetch_pending_bets() -> pd.DataFrame:
    """Bets pending con kickoff dentro de la ventana pre-kickoff.

    El JOIN con upcoming_matches usa DISTINCT ON sobre (home_norm, away_norm,
    sport_key) y se queda con la row más reciente por updated_at, para evitar
    asociar la bet con un row stale (mismo partido, fecha vieja) cuando la API
    corrigió la fecha del fixture.
    """
    now_utc = datetime.now(timezone.utc)
    lo = now_utc + timedelta(minutes=PRE_KICKOFF_WINDOW_MIN)
    hi = now_utc + timedelta(minutes=PRE_KICKOFF_WINDOW_MAX)

    df = pd.read_sql(text("""
        WITH u_dedup AS (
            SELECT DISTINCT ON (home_team_norm, away_team_norm, sport_key)
                   home_team, away_team, sport_key, updated_at
            FROM upcoming_matches
            ORDER BY home_team_norm, away_team_norm, sport_key,
                     updated_at DESC NULLS LAST
        )
        SELECT b.match, b.match_date, b.market,
               b.probability, b.odds, b.edge, b.stake,
               COALESCE(u.sport_key, b.league, '') AS league
        FROM bets_history b
        LEFT JOIN u_dedup u
          ON LOWER(u.home_team) = LOWER(SPLIT_PART(b.match, ' vs ', 1))
         AND LOWER(u.away_team) = LOWER(SPLIT_PART(b.match, ' vs ', 2))
        WHERE b.result = 'pending'
          AND b.match_date >= :lo
          AND b.match_date <= :hi
        ORDER BY b.match_date, b.edge DESC
    """), engine, params={"lo": lo, "hi": hi})
    return df


def _filter_confiables(df: pd.DataFrame) -> pd.DataFrame:
    """Aplica EL MISMO filtro que la notificación matutina."""
    if df.empty:
        return df
    confiables = df[df.apply(_is_suspicious, axis=1) == False].copy()
    return _dedup_correlated(confiables).reset_index(drop=True)


def _already_analyzed(match: str, market: str, match_date) -> bool:
    """Evita reanalizar el mismo match+market (cron corre cada 30 min)."""
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT 1 FROM pre_kickoff_analyses
            WHERE match = :m AND market = :mkt AND match_date = :d
            LIMIT 1
        """), {"m": match, "mkt": market, "d": match_date}).first()
    return row is not None


# ============================================================
# MEMORIA DE APRENDIZAJE (loop de feedback)
# ============================================================
# Idea: el agente lee, en cada corrida, un resumen de cómo le fue en los
# últimos 30 días — calibración por bucket de probabilidad y los peores
# fallos (apuestas que dijo "APUESTA" con prob alta y perdieron). Eso se
# inyecta como segundo bloque cacheado del system prompt → el modelo
# aprende del feedback real sin re-entrenar nada.
#
# Combina con `data/analyst_lessons.md` (editable por el usuario) para
# imponer reglas duras vía texto.

LESSONS_FILE = Path(__file__).parent.parent / "data" / "analyst_lessons.md"


def _load_manual_lessons() -> str:
    """Lee data/analyst_lessons.md si existe. Devuelve string vacío si no."""
    try:
        if LESSONS_FILE.exists():
            txt = LESSONS_FILE.read_text(encoding="utf-8").strip()
            # Cap defensivo: 3000 chars ≈ 750 tokens, suficiente para 600 palabras
            if len(txt) > 3000:
                txt = txt[:3000] + "\n\n[truncado a 3000 chars]"
            return txt
    except Exception:
        pass
    return ""


def _build_learning_memo(days: int = 30, max_examples: int = 3) -> str:
    """
    Construye memoria de aprendizaje desde la DB:
      • Calibración: por bucket de probabilidad estimada vs hit rate real
      • Peores fallos: APUESTA con prob >= 65 que perdieron (con reasoning)
      • Mejores aciertos: APUESTA con prob >= 65 que ganaron

    El bloque resultante se cachea como segundo system block → costo bajo
    aún en cron de 15 min (5 min TTL del prompt cache amortiza varios calls).

    Devuelve string vacío si no hay datos suficientes (primera semana).
    """
    try:
        with engine.begin() as conn:
            # Solo bets ya resueltas, con análisis del agente, en ventana
            rows = conn.execute(text("""
                SELECT p.match, p.market, p.verdict, p.probability,
                       p.decision, p.reasoning, b.result, b.odds, b.edge
                FROM pre_kickoff_analyses p
                JOIN bets_history b
                  ON b.match      = p.match
                 AND b.market     = p.market
                 AND b.match_date = p.match_date
                WHERE p.analyzed_at >= NOW() - (:days || ' days')::interval
                  AND b.result IN ('win', 'loss')
                  AND p.probability IS NOT NULL
                ORDER BY p.analyzed_at DESC
                LIMIT 500
            """), {"days": days}).fetchall()
    except Exception:
        return ""  # tabla aún no migrada o algún error → memo vacío

    if not rows or len(rows) < 5:
        return ""  # muestra muy chica → no inyectar memo (evita ruido)

    # ── Calibración por bucket ───────────────────────────────────────
    buckets = {
        "[40-50)": {"n": 0, "won": 0},
        "[50-60)": {"n": 0, "won": 0},
        "[60-70)": {"n": 0, "won": 0},
        "[70-80)": {"n": 0, "won": 0},
        "[80-100]": {"n": 0, "won": 0},
    }
    apuestas_total = 0
    apuestas_ganadas = 0
    for r in rows:
        p = int(r.probability)
        won = (r.result == "win")
        if p < 50:
            key = "[40-50)"
        elif p < 60:
            key = "[50-60)"
        elif p < 70:
            key = "[60-70)"
        elif p < 80:
            key = "[70-80)"
        else:
            key = "[80-100]"
        buckets[key]["n"] += 1
        if won:
            buckets[key]["won"] += 1
        if (r.decision or "").upper() == "APUESTA":
            apuestas_total += 1
            if won:
                apuestas_ganadas += 1

    cal_lines = []
    for k, v in buckets.items():
        if v["n"] == 0:
            continue
        wr = v["won"] / v["n"] * 100
        # Comentario si calibración está desviada
        target = {
            "[40-50)": 45, "[50-60)": 55, "[60-70)": 65,
            "[70-80)": 75, "[80-100]": 85,
        }[k]
        if v["n"] >= 5:
            diff = wr - target
            tag = (" OK"          if abs(diff) < 8 else
                   " OPTIMISTA"   if diff < -8     else
                   " CONSERVADOR")
        else:
            tag = " (muestra chica)"
        cal_lines.append(f"  {k}: {v['won']}/{v['n']} = {wr:.0f}%{tag}")

    # ── Peores fallos: APUESTA con prob >=65 que perdieron ───────────
    bad = [r for r in rows
           if (r.decision or "").upper() == "APUESTA"
           and int(r.probability) >= 65
           and r.result == "loss"][:max_examples]
    bad_lines = []
    for r in bad:
        reason = (r.reasoning or "").strip().replace("\n", " ")
        if len(reason) > 110:
            reason = reason[:107] + "..."
        bad_lines.append(f"  • {r.match} ({r.market}) prob={r.probability}% "
                         f"@{float(r.odds):.2f} → PERDIÓ. Tu razón: \"{reason}\"")

    # ── Mejores aciertos (refuerzo positivo) ─────────────────────────
    good = [r for r in rows
            if (r.decision or "").upper() == "APUESTA"
            and int(r.probability) >= 65
            and r.result == "win"][:max_examples]
    good_lines = []
    for r in good:
        reason = (r.reasoning or "").strip().replace("\n", " ")
        if len(reason) > 90:
            reason = reason[:87] + "..."
        good_lines.append(f"  • {r.match} ({r.market}) prob={r.probability}% "
                          f"@{float(r.odds):.2f} → GANÓ. Razón: \"{reason}\"")

    # ── Armar memo ───────────────────────────────────────────────────
    parts = [
        "=== MEMORIA DE ERRORES Y ACIERTOS (últimos "
        f"{days} días, n={len(rows)}) ===",
        "",
    ]
    if apuestas_total > 0:
        wr_global = apuestas_ganadas / apuestas_total * 100
        parts.append(f"Hit rate cuando dijiste APUESTA: "
                     f"{apuestas_ganadas}/{apuestas_total} = {wr_global:.0f}%")
    if cal_lines:
        parts.append("\nCalibración por bucket de probabilidad:")
        parts.extend(cal_lines)
    if bad_lines:
        parts.append("\nPEORES FALLOS (apostaste con confianza y perdiste — "
                     "estudiá el patrón):")
        parts.extend(bad_lines)
    if good_lines:
        parts.append("\nMEJORES ACIERTOS (lo que funcionó):")
        parts.extend(good_lines)
    parts.append(
        "\nINSTRUCCIÓN: si ves un bucket marcado OPTIMISTA, sé más "
        "conservador en esa franja. Si ves un fallo cuyo patrón se "
        "repite hoy, evita la apuesta o bajá probability. Aprendé."
    )
    return "\n".join(parts)


# ============================================================
# CONTEXTO CUANTITATIVO (sin tokens)
# ============================================================

def _build_context(bet: dict) -> dict:
    """Reúne contexto cuantitativo desde la DB (sin gastar API)."""
    from src.features.h2h_stats          import get_h2h_stats
    from src.features.team_form          import get_team_form
    from src.features.fixture_congestion import get_fixture_congestion
    from src.features.line_movement      import get_line_movement
    from src.features.motivation_factor  import get_motivation_factor
    from src.features.xg_proxy           import get_team_xg
    from src.features.weather_impact     import get_weather_multiplier
    from src.utils.team_normalizer       import normalize_team

    home, _, away = bet["match"].partition(" vs ")
    md = pd.to_datetime(bet["match_date"])
    league = bet.get("league", "")

    # match_key se construye igual que en update_upcoming_matches.py:456
    home_norm = normalize_team(home).lower().strip()
    away_norm = normalize_team(away).lower().strip()
    md_utc = md if md.tzinfo is not None else md.tz_localize("UTC")
    match_day = md_utc.tz_convert("UTC").strftime("%Y-%m-%d")
    match_key = f"{home_norm}_{away_norm}_{match_day}"

    ctx: dict = {}

    try: ctx["h2h"] = get_h2h_stats(home, away, cutoff_date=md)
    except Exception: ctx["h2h"] = None

    try: ctx["form_home"] = get_team_form(home, venue="home", cutoff_date=md)
    except Exception: ctx["form_home"] = None

    try: ctx["form_away"] = get_team_form(away, venue="away", cutoff_date=md)
    except Exception: ctx["form_away"] = None

    try:
        cong_h = get_fixture_congestion(home, md)
        ctx["cong_home"] = cong_h
        ctx["cong_home_n"] = int(cong_h.get("matches_in_10d", 0))
    except Exception:
        ctx["cong_home"] = None
        ctx["cong_home_n"] = 0

    try:
        cong_a = get_fixture_congestion(away, md)
        ctx["cong_away"] = cong_a
        ctx["cong_away_n"] = int(cong_a.get("matches_in_10d", 0))
    except Exception:
        ctx["cong_away"] = None
        ctx["cong_away_n"] = 0

    try: ctx["line"] = get_line_movement(match_key)
    except Exception: ctx["line"] = None

    try: ctx["motiv_home"] = float(get_motivation_factor(home, league))
    except Exception: ctx["motiv_home"] = 0.0
    try: ctx["motiv_away"] = float(get_motivation_factor(away, league))
    except Exception: ctx["motiv_away"] = 0.0

    try:
        xg_h = get_team_xg(home) or {}
        ctx["xg_home_for"] = xg_h.get("xg_for")
        ctx["xg_home_against"] = xg_h.get("xg_against")
    except Exception:
        ctx["xg_home_for"] = ctx["xg_home_against"] = None

    try:
        xg_a = get_team_xg(away) or {}
        ctx["xg_away_for"] = xg_a.get("xg_for")
        ctx["xg_away_against"] = xg_a.get("xg_against")
    except Exception:
        ctx["xg_away_for"] = ctx["xg_away_against"] = None

    try: ctx["weather"] = float(get_weather_multiplier(home))
    except Exception: ctx["weather"] = 1.0

    return ctx


# ============================================================
# FILTRO PRE-API (gratis)
# ============================================================

def _quick_skip_signals(bet: dict, ctx: dict) -> str | None:
    """
    Reglas baratas que descartan la apuesta SIN llamar a la API.
    Devuelve razón humana si dispara, None si la apuesta merece análisis LLM.
    """
    # ── 1. Mercado se movió fuerte en contra (sharp money contrario) ─────
    line = ctx.get("line") or {}
    market = bet.get("market", "")
    market_to_movement = {
        "home_win": line.get("home_movement", 0.0),
        "draw":     line.get("draw_movement", 0.0),
        "away_win": line.get("away_movement", 0.0),
        "over25":   line.get("over25_movement", 0.0),
        "under25":  -line.get("over25_movement", 0.0),
    }
    mv = market_to_movement.get(market, 0.0) or 0.0
    if mv < -0.40:
        return f"Sharp contrario: línea {market} cayó {mv*100:+.0f}%"

    # ── 2. Congestión >=3 partidos/10d → rotación esperada ───────────────
    if ctx.get("cong_home_n", 0) >= 3 or ctx.get("cong_away_n", 0) >= 3:
        return (f"Congestión {max(ctx['cong_home_n'], ctx['cong_away_n'])} "
                f"partidos/10d -> rotación esperada")

    # ── 3. xG combinado incoherente con Over/Under/BTTS ──────────────────
    xg_for_home = ctx.get("xg_home_for") or 0
    xg_ag_away  = ctx.get("xg_away_against") or 0
    xg_for_away = ctx.get("xg_away_for") or 0
    xg_ag_home  = ctx.get("xg_home_against") or 0
    # xG total estimado del partido
    if xg_for_home and xg_for_away:
        xg_total = (xg_for_home + xg_ag_away) / 2 + (xg_for_away + xg_ag_home) / 2
        if market in ("over25", "btts") and 0 < xg_total < 2.0:
            return f"xG combinado {xg_total:.1f} < 2.0 — incoherente con {market}"
        if market in ("under_1.5",) and xg_total > 3.0:
            return f"xG combinado {xg_total:.1f} > 3.0 — incoherente con {market}"

    return None


# ============================================================
# PROMPT BUILDING + LLM CALL
# ============================================================

def _format_context(ctx: dict) -> str:
    """Compacta el contexto en texto legible para el modelo."""
    parts = []

    h = ctx.get("h2h") or {}
    if h:
        parts.append(
            f"H2H ({h.get('h2h_matches', 0)} partidos): "
            f"avg {h.get('h2h_avg_goals', 0):.1f} goles, "
            f"win local {h.get('h2h_home_win_rate', 0)*100:.0f}%, "
            f"draw {h.get('h2h_draw_rate', 0)*100:.0f}%"
        )
    else:
        parts.append("H2H: sin datos suficientes")

    fh = ctx.get("form_home") or {}
    fa = ctx.get("form_away") or {}
    if fh and not fh.get("is_fallback", True):
        parts.append(
            f"Forma local ({fh.get('matches', 0)}p): "
            f"atk={fh.get('attack_rating', 0):.2f} "
            f"def={fh.get('defense_rating', 0):.2f} "
            f"pts={fh.get('points', 0)}"
        )
    if fa and not fa.get("is_fallback", True):
        parts.append(
            f"Forma visit ({fa.get('matches', 0)}p): "
            f"atk={fa.get('attack_rating', 0):.2f} "
            f"def={fa.get('defense_rating', 0):.2f} "
            f"pts={fa.get('points', 0)}"
        )

    parts.append(
        f"Congestión 10d: local={ctx.get('cong_home_n', 0)} "
        f"visit={ctx.get('cong_away_n', 0)}"
    )

    line = ctx.get("line") or {}
    if line.get("has_movement"):
        parts.append(
            f"Línea: home_mv={line.get('home_movement', 0)*100:+.1f}% "
            f"away_mv={line.get('away_movement', 0)*100:+.1f}% "
            f"o2.5_mv={line.get('over25_movement', 0)*100:+.1f}% "
            f"sharp={line.get('sharp_signal', 'none')} "
            f"({line.get('movement_strength', 'none')})"
        )
    else:
        parts.append("Línea: sin movimiento significativo")

    mh = ctx.get("motiv_home", 0.0)
    ma = ctx.get("motiv_away", 0.0)
    parts.append(f"Motivación: local={mh:+.2f}  visit={ma:+.2f} (0=neutral, +=alta)")

    xg_for_h = ctx.get("xg_home_for") or 0
    xg_for_a = ctx.get("xg_away_for") or 0
    if xg_for_h and xg_for_a:
        parts.append(
            f"xG: local for={xg_for_h:.2f} ag={ctx.get('xg_home_against', 0):.2f} | "
            f"visit for={xg_for_a:.2f} ag={ctx.get('xg_away_against', 0):.2f}"
        )

    parts.append(f"Clima multiplier: {ctx.get('weather', 1.0):.2f} (1.0=normal, <1=baja goles)")
    return "\n".join(parts)


def _normalize_result(parsed: dict, bet: dict) -> dict:
    """
    Garantiza coherencia + valores válidos en el dict del modelo.
    El modelo puede devolver decision/probability vacíos, inconsistentes,
    o con tipos malos — el backend decide y normaliza.
    """
    # ── verdict ──────────────────────────────────────────────────────
    verdict = (parsed.get("verdict") or "MEDIUM").upper()
    if verdict not in ("STRONG", "MEDIUM", "SKIP"):
        verdict = "MEDIUM"

    # ── probability (entero 0-100) ───────────────────────────────────
    try:
        prob = int(round(float(parsed.get("probability", 0))))
        prob = max(0, min(100, prob))
    except (TypeError, ValueError):
        prob = 0

    # ── decision ─────────────────────────────────────────────────────
    decision = (parsed.get("decision") or "").upper().strip()
    if decision not in ("APUESTA", "NO APUESTA"):
        # Inferir desde verdict si el modelo no la devolvió
        decision = "APUESTA" if verdict in ("STRONG", "MEDIUM") else "NO APUESTA"

    # ── Coherencia obligatoria: SKIP ⇄ NO APUESTA ────────────────────
    if verdict == "SKIP":
        decision = "NO APUESTA"
    elif decision == "NO APUESTA" and verdict == "STRONG":
        # contradicción → degradamos a SKIP
        verdict = "SKIP"

    # ── Validación cuantitativa: prob >= implícita + threshold para apostar ──
    # Threshold configurable vía env var ANALYST_EDGE_THRESHOLD (default 3).
    try:
        odds = float(bet.get("odds", 0))
        if odds > 1.0:
            implied = (1.0 / odds) * 100
            if decision == "APUESTA" and prob < implied + ANALYST_EDGE_THRESHOLD:
                # No tiene edge real → no apuestes
                decision = "NO APUESTA"
                verdict = "SKIP"
    except Exception:
        pass

    parsed["verdict"]     = verdict
    parsed["decision"]    = decision
    parsed["probability"] = prob
    return parsed


def _analyze_match_group(client, match_bets: list[dict], ctx: dict,
                          learning_memo: str = "", manual_lessons: str = "") -> list[dict]:
    """
    Analiza UN partido con N mercados en UNA sola llamada a Claude.

    Devuelve una lista de results, uno por bet de `match_bets`, en el MISMO
    orden. Cada result contiene además `is_best` (True para el mercado
    elegido por el agente como mejor) y `best_reason` (poblado solo en el
    is_best=True).

    Beneficios vs el llamado por-bet:
      • 1 sola llamada API por partido (3 markets → 1 call en vez de 3)
      • El agente ve TODOS los mercados juntos → puede comparar y rankear
      • Decisión "best_pick" más informada que decisiones aisladas
    """
    if not match_bets:
        return []

    match = match_bets[0]["match"]
    league_label = LEAGUE_LABELS.get(match_bets[0].get("league", ""),
                                      match_bets[0].get("league", ""))

    md = pd.to_datetime(match_bets[0]["match_date"])
    md_utc = md if md.tzinfo is not None else md.tz_localize("UTC")
    dt_local = md_utc.tz_convert(ZoneInfo(USER_TIMEZONE))

    # Listado de mercados con sus stats
    market_lines = []
    for i, bet in enumerate(match_bets, 1):
        market_label = _get_market_label(bet["market"], match)
        try:    odds = float(bet["odds"])
        except: odds = 0.0
        try:    prob = float(bet["probability"]) * 100
        except: prob = 0.0
        try:    edge = float(bet["edge"]) * 100
        except: edge = 0.0
        implied = (1.0 / odds) * 100 if odds > 1 else 0
        market_lines.append(
            f"  {i}. market=\"{bet['market']}\"  → {market_label}\n"
            f"     odds={odds:.2f}  prob_modelo={prob:.1f}%  "
            f"edge=+{edge:.1f}%  prob_implícita={implied:.1f}%\n"
            f"     (threshold mínimo para APUESTA: prob >= "
            f"{implied+ANALYST_EDGE_THRESHOLD:.0f}%)"
        )

    user_msg = (
        f"Partido: {match}\n"
        f"Liga: {league_label}\n"
        f"Kickoff: {dt_local.strftime('%Y-%m-%d %H:%M %Z')}\n\n"
        f"MERCADOS A EVALUAR ({len(match_bets)}):\n"
        + "\n".join(market_lines) + "\n\n"
        f"DOSIER CUANTITATIVO (NO buscar esto en la web):\n"
        f"{_format_context(ctx)}\n\n"
        f"Devolvé UN JSON con {len(match_bets)} verdicts (uno por mercado, "
        f"mismo orden y mismas `market` keys que el input) + best_pick."
    )

    # ── Construir system prompt: 1-3 bloques cacheados ────────────────
    # Bloque 1: instrucciones (estables)  — se cachea siempre
    # Bloque 2: lecciones manuales         — se cachea si existe
    # Bloque 3: memoria de aprendizaje    — se cachea si existe
    system_blocks = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    if manual_lessons:
        system_blocks.append({
            "type": "text",
            "text": f"=== LECCIONES MANUALES (escritas por el operador) ===\n\n{manual_lessons}",
            "cache_control": {"type": "ephemeral"},
        })
    if learning_memo:
        system_blocks.append({
            "type": "text",
            "text": learning_memo,
            "cache_control": {"type": "ephemeral"},
        })

    # ── Build system prompt blocks (cacheados) ─────────────────────────
    system_blocks = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]
    if manual_lessons:
        system_blocks.append({
            "type": "text",
            "text": f"=== LECCIONES MANUALES ===\n\n{manual_lessons}",
            "cache_control": {"type": "ephemeral"},
        })
    if learning_memo:
        system_blocks.append({
            "type": "text",
            "text": learning_memo,
            "cache_control": {"type": "ephemeral"},
        })

    # ── BUDGET GUARD ──────────────────────────────────────────────────
    # Antes per-bet: 3 markets del Atlético = 3 calls × $0.016 = $0.048.
    # Ahora per-match: 1 call con N markets = ~$0.018-0.025 → 50-60% ahorro
    # adicional sobre los partidos multi-mercado.
    from src.utils.anthropic_budget import (
        can_call, estimate_call_cost, record_call, extract_usage_from_response,
    )
    MODEL = "claude-haiku-4-5"
    n_markets = len(match_bets)
    est_input  = 4000 + 250 * n_markets      # más tokens para N mercados
    est_output = 250  + 150 * n_markets
    est_cost = estimate_call_cost(MODEL, est_input_tokens=est_input,
                                  est_output_tokens=est_output, web_searches=1)
    ok, reason = can_call(engine, est_cost)
    if not ok:
        raise RuntimeError(f"Budget guard: {reason}")

    resp = client.messages.create(
        model=MODEL,
        max_tokens=300 + 200 * n_markets,    # escalable según N mercados
        system=system_blocks,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 2,                   # subido de 1→2: el agente ahora
                                              # debe cubrir N mercados, justifica
                                              # 1 búsqueda extra de respaldo
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    u = extract_usage_from_response(resp)
    record_call(engine,
                input_tokens=u["input_tokens"],
                output_tokens=u["output_tokens"],
                model=MODEL,
                web_searches=u["web_searches"],
                cache_read_tokens=u["cache_read_tokens"])

    # Extraer último bloque de texto
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = text_blocks[-1].strip() if text_blocks else "{}"
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Si falla parse, devolvemos SKIP para todos los markets — failsafe
        parsed = {
            "lineups": "L0",
            "match_notes": "parse error en respuesta del modelo",
            "verdicts": [{
                "market": b["market"],
                "verdict": "SKIP", "decision": "NO APUESTA",
                "probability": 0, "confidence": 1,
                "reasoning": "parse error",
                "key_factors": [],
            } for b in match_bets],
            "best_pick": None,
            "best_reason": "",
        }

    # ── Sources globales del partido (compartidas) ────────────────────
    sources: list[str] = []
    for block in resp.content:
        if getattr(block, "type", "") == "web_search_tool_result":
            for r in getattr(block, "content", []) or []:
                url = getattr(r, "url", None)
                if url:
                    sources.append(url)

    # ── Match verdicts → input bets (por market key) ──────────────────
    verdicts_by_market = {v.get("market"): v
                          for v in (parsed.get("verdicts") or [])
                          if isinstance(v, dict) and v.get("market")}
    best_pick = parsed.get("best_pick")
    best_reason = (parsed.get("best_reason") or "")[:200]
    lineups_global = parsed.get("lineups", "L0")
    match_notes = (parsed.get("match_notes") or "")[:300]

    results = []
    for bet in match_bets:
        mk = bet["market"]
        v = verdicts_by_market.get(mk)
        if v is None:
            # El agente no devolvió este mercado → SKIP defensivo
            v = {
                "verdict": "SKIP", "decision": "NO APUESTA",
                "probability": 0, "confidence": 1,
                "reasoning": "El agente no incluyó este mercado en la respuesta",
                "key_factors": [],
            }
        # Compartir lineups y sources entre todos los mercados del partido
        v["lineups"] = v.get("lineups") or lineups_global
        v["sources"] = sources
        v["match_notes"] = match_notes
        # Marcar el best pick
        is_best = (best_pick == mk)
        v["is_best"] = is_best
        v["best_reason"] = best_reason if is_best else ""
        # Normalizar coherencia
        v = _normalize_result(v, bet)
        # Si después de normalize quedó SKIP pero era best_pick, des-marcar
        # (el best_pick debe ser apostable; backend safety)
        if is_best and v.get("decision") != "APUESTA":
            v["is_best"] = False
            v["best_reason"] = ""
        results.append(v)

    return results


def _save_analysis(bet: dict, result: dict):
    """Persiste el dictamen en pre_kickoff_analyses (idempotente).

    Incluye `probability` (0-100) y `decision` (APUESTA/NO APUESTA) para
    poder armar la calibración real → memoria de aprendizaje.
    """
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pre_kickoff_analyses
                (match, match_date, market, verdict, confidence,
                 reasoning, lineups, sources, probability, decision)
            VALUES
                (:match, :match_date, :market, :verdict, :confidence,
                 :reasoning, :lineups, :sources, :probability, :decision)
            ON CONFLICT (match, market, match_date) DO NOTHING
        """), {
            "match":       bet["match"],
            "match_date":  bet["match_date"],
            "market":      bet["market"],
            "verdict":     result.get("verdict", "MEDIUM"),
            "confidence":  int(result.get("confidence", 1) or 1),
            "reasoning":   (result.get("reasoning") or "")[:1000],
            "lineups":     (result.get("lineups") or "")[:500],
            "sources":     json.dumps(result.get("sources", [])),
            "probability": int(result.get("probability", 0) or 0),
            "decision":    (result.get("decision") or "NO APUESTA")[:15],
        })


# ============================================================
# MAIN
# ============================================================

def _write_heartbeat(bets_found: int = 0, bets_analyzed: int = 0,
                     bets_skipped_rule: int = 0, error_msg: str | None = None):
    """
    Escribe una row en analyst_heartbeat. Cada corrida del cron deja
    huella, así detectamos gaps (cron throttled / disabled / crash).

    NUNCA debe lanzar excepción — si la tabla no existe aún (DDL aún no
    corrió) o la DB está caída, no queremos arruinar la corrida.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO analyst_heartbeat
                    (bets_found, bets_analyzed, bets_skipped_rule, error_msg)
                VALUES
                    (:found, :analyzed, :skipped, :err)
            """), {
                "found": int(bets_found),
                "analyzed": int(bets_analyzed),
                "skipped": int(bets_skipped_rule),
                "err": (error_msg or "")[:500] or None,
            })
    except Exception as e:
        print(f"⚠️  No se pudo escribir heartbeat: {e}")


def _alert_telegram_crash(exc: BaseException):
    """En caso de crash del analyst, manda alerta a Telegram para que el
    usuario sepa INMEDIATAMENTE que el cron está fallando. Antes el script
    moría en silencio y el usuario solo se enteraba al final del día."""
    try:
        from scripts.notify_telegram import send_message_prekickoff
        tb = "".join(traceback.format_exception(exc))[:1500]
        msg = (
            "🚨 <b>PRE-KICKOFF ANALYST CRASH</b>\n\n"
            f"<code>{type(exc).__name__}: {exc}</code>\n\n"
            f"<pre>{tb}</pre>\n"
            "<i>El cron volverá a intentar en 15 min. "
            "Si persiste, revisa los logs.</i>"
        )
        send_message_prekickoff(msg)
    except Exception as e:
        print(f"⚠️  No se pudo enviar alerta de crash: {e}")


def main():
    print(f"\n🔍 PRE-KICKOFF ANALYST — ventana [{PRE_KICKOFF_WINDOW_MIN}, "
          f"{PRE_KICKOFF_WINDOW_MAX}] min desde NOW UTC\n")

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY no configurada — abortando.")
        _write_heartbeat(error_msg="ANTHROPIC_API_KEY missing")
        return

    bets = _fetch_pending_bets()
    if bets.empty:
        print("✓ Sin bets pending en la ventana pre-kickoff")
        _write_heartbeat(bets_found=0)
        return

    bets = _filter_confiables(bets)
    if bets.empty:
        print("✓ Sin bets CONFIABLES en la ventana (todas filtradas)")
        _write_heartbeat(bets_found=0)
        return

    bets_found_total = len(bets)
    print(f"📋 {bets_found_total} bet(s) confiable(s) en ventana — analizando...\n")

    # ── Carga memo + lecciones UNA SOLA VEZ por corrida ──────────────
    # Ambas se inyectan como bloques cacheados en cada call; computarlas
    # una vez ahorra queries repetidas.
    learning_memo  = _build_learning_memo()
    manual_lessons = _load_manual_lessons()
    if learning_memo:
        print("🧠 Memoria de aprendizaje activa (últimos 30 días)")
    if manual_lessons:
        print(f"📝 Lecciones manuales activas ({len(manual_lessons)} chars)")
    print()

    # Import perezoso de anthropic — solo cuando hay bets para analizar.
    # Evita romper el cron si la lib no se instaló y no hay nada que hacer.
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    verdicts = []
    n_skipped_rule = 0
    per_bet_errors: list[str] = []

    # ── Agrupar por partido para hacer 1 sola call por match ─────────
    # Antes: por cada market = 1 call. Atlético (3 markets) = 3 calls.
    # Ahora: por cada match = 1 call con N markets → ahorro 50-60% en
    # partidos multi-mercado + ranking de "best pick" más informado.
    grouped = bets.groupby(["match", "match_date"], sort=False)

    for (match_key, match_date_key), group in grouped:
        all_bets = group.to_dict("records")

        # Filtrar bets ya analizadas previamente
        fresh_bets = [b for b in all_bets
                      if not _already_analyzed(b["match"], b["market"],
                                                b["match_date"])]
        if not fresh_bets:
            print(f"  · {match_key} — todos los mercados ya analizados, skip")
            continue

        # Contexto cuantitativo (es el mismo para todos los mercados del partido)
        try:
            ctx = _build_context(fresh_bets[0])
        except Exception as e:
            err = f"_build_context: {type(e).__name__}: {e}"
            print(f"  ❌ {match_key}: {err}")
            per_bet_errors.append(f"{match_key}: {err}")
            continue

        # ── Separar rule-based SKIPs (gratis) de los que van al LLM ──
        rule_results: list[tuple[dict, dict]] = []   # (bet, result)
        llm_bets: list[dict] = []
        for b in fresh_bets:
            reason = _quick_skip_signals(b, ctx)
            if reason:
                try:
                    implied = int(round((1.0 / float(b["odds"])) * 100))
                except Exception:
                    implied = 0
                rule_results.append((b, {
                    "verdict": "SKIP",
                    "decision": "NO APUESTA",
                    "probability": max(0, implied - 5),
                    "confidence": 4,
                    "reasoning": reason,
                    "lineups": "L0",
                    "key_factors": [reason],
                    "sources": [],
                    "is_best": False,
                    "best_reason": "",
                }))
                n_skipped_rule += 1
                print(f"  🔴 {b['match']} ({b['market']}) — "
                      f"SKIP rule-based: {reason}")
            else:
                llm_bets.append(b)

        # ── 1 sola llamada LLM con TODOS los markets del partido ─────
        llm_results: list[tuple[dict, dict]] = []
        if llm_bets:
            try:
                results_list = _analyze_match_group(
                    client, llm_bets, ctx,
                    learning_memo=learning_memo,
                    manual_lessons=manual_lessons,
                )
                # results_list está en el MISMO orden que llm_bets
                for b, result in zip(llm_bets, results_list):
                    v = result.get("verdict", "?")
                    d = result.get("decision", "?")
                    p = result.get("probability", 0)
                    icon = {"STRONG": "🟢", "MEDIUM": "🟡", "SKIP": "🔴"}.get(v, "⚪")
                    best_tag = " ⭐BEST" if result.get("is_best") else ""
                    print(f"  {icon} {b['match']} ({b['market']}) — "
                          f"{v}/{d} prob={p}%{best_tag}: "
                          f"{result.get('reasoning', '')[:60]}")
                    llm_results.append((b, result))
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc()
                print(f"  ❌ Error analizando match {match_key}: {err}")
                print(tb)
                per_bet_errors.append(f"{match_key} (group): {err}")
                continue

        # ── Guardar y appender al output ─────────────────────────────
        for b, result in rule_results + llm_results:
            try:
                _save_analysis(b, result)
            except Exception as e:
                print(f"  ⚠️  No se pudo guardar análisis de {b['match']}: {e}")
            verdicts.append({**b, **result})

    if verdicts:
        try:
            send_pre_kickoff_verdict(verdicts)
            print(f"\n✅ {len(verdicts)} dictámen(es) enviado(s) a Telegram")
        except Exception as e:
            print(f"\n❌ Error mandando a Telegram: {e}")
    else:
        print("\n✓ Sin nuevos análisis (todos ya analizados o sin contenido)")

    # ── Heartbeat final (siempre, incluso si no se envió nada) ───────
    # Si hubo errores por bet, los guardamos en error_msg para que el
    # próximo audit los pueda leer desde la DB.
    err_summary = None
    if per_bet_errors:
        err_summary = " | ".join(per_bet_errors)[:500]
        print(f"\n⚠️  {len(per_bet_errors)} error(es) per-bet:")
        for e in per_bet_errors:
            print(f"    - {e}")

    _write_heartbeat(
        bets_found=bets_found_total,
        bets_analyzed=len(verdicts),
        bets_skipped_rule=n_skipped_rule,
        error_msg=err_summary,
    )

    # ── Alerta Telegram si hay falla sistémica ───────────────────────
    # Si TODAS las bets analizables (excluyendo rule-based) fallaron,
    # el bug es sistémico (API caída, code regression, etc.) y el
    # usuario debe enterarse INMEDIATAMENTE, no al final del día.
    n_analyzable = bets_found_total - n_skipped_rule
    if per_bet_errors and len(per_bet_errors) >= max(3, n_analyzable):
        try:
            from scripts.notify_telegram import send_message_prekickoff
            uniq_errors = sorted({e.split(": ", 1)[1].split(":")[0]
                                  if ": " in e else e
                                  for e in per_bet_errors})[:3]
            send_message_prekickoff(
                "🚨 <b>ANALISTA FALLANDO EN SERIE</b>\n\n"
                f"<code>{len(per_bet_errors)}/{n_analyzable}</code> bets "
                f"crashearon en el último cron.\n\n"
                f"Errores únicos:\n• "
                + "\n• ".join(f"<code>{e[:100]}</code>" for e in uniq_errors)
                + "\n\n<i>Probablemente bug sistémico (API key, model, "
                  "tools). Revisar logs GH Actions.</i>"
            )
        except Exception as e:
            print(f"⚠️  No se pudo enviar alerta sistémica: {e}")


def debug_mode_run():
    """Modo SANITY CHECK: agarra UNA bet pending futura ignorando la ventana
    30-90 min, la corre por el pipeline completo, y reporta éxito o el
    error real. Útil cuando el cron no muestra bets en ventana pero
    queremos verificar que la integración con Claude API funciona.

    Sirve para distinguir entre "el cron no encontró bets" (estado válido)
    y "el cron encontró bets pero Claude API falla" (bug a arreglar).
    """
    print("\n🔧 DEBUG MODE — sanity check de una bet futura cualquiera\n")
    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY no configurada — abortando.")
        return

    df = pd.read_sql(text("""
        SELECT b.match, b.match_date, b.market,
               b.probability, b.odds, b.edge, b.stake,
               COALESCE(b.league, '') AS league
        FROM bets_history b
        WHERE b.result = 'pending'
          AND b.match_date > NOW()
        ORDER BY b.match_date
        LIMIT 1
    """), engine)
    if df.empty:
        print("⚠️  No hay bets pending FUTURAS para testear. "
              "Esperá a que la morning pipeline genere las de mañana.")
        return

    bet = df.iloc[0].to_dict()
    print(f"📋 Bet test: {bet['match']} | {bet['market']} | "
          f"kickoff={bet['match_date']} | odds={bet['odds']}\n")

    print("→ Construyendo contexto...")
    ctx = _build_context(bet)
    print(f"  OK ({len(ctx)} campos)\n")

    print("→ Cargando memo + lecciones...")
    memo = _build_learning_memo()
    lessons = _load_manual_lessons()
    print(f"  memo={len(memo)} chars · lessons={len(lessons)} chars\n")

    print("→ Llamando a Claude API + web_search... (~10-15 seg)\n")
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        result = _analyze_bet(client, bet, ctx,
                              learning_memo=memo, manual_lessons=lessons)
    except BaseException as exc:
        print("\n❌❌❌ CLAUDE API CALL FAILED — este es el bug:\n")
        traceback.print_exc()
        print()
        try:
            from scripts.notify_telegram import send_message_prekickoff
            tb = traceback.format_exc()[:1500]
            send_message_prekickoff(
                "🚨 <b>DEBUG SANITY CHECK FAILED</b>\n\n"
                f"<code>{type(exc).__name__}: {exc}</code>\n\n"
                f"<pre>{tb}</pre>"
            )
        except Exception:
            pass
        _write_heartbeat(error_msg=f"DEBUG: {type(exc).__name__}: {exc}"[:500])
        sys.exit(1)

    print("\n✅ ÉXITO — Claude respondió:")
    print(json.dumps({k: v for k, v in result.items() if k != "sources"},
                     indent=2, ensure_ascii=False, default=str))
    print(f"\nFuentes ({len(result.get('sources', []))}):")
    for s in result.get("sources", [])[:5]:
        print(f"  - {s}")
    print("\n🟢 El analista FUNCIONA. El problema anterior debe ser otro "
          "(ventana, filtros, secretos).")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--debug", action="store_true",
                    help="Sanity check: agarra 1 bet futura ignorando ventana, "
                         "corre el pipeline completo, muestra el error si falla.")
    args = ap.parse_args()

    # Top-level try/except: si el script crashea por cualquier razón
    # (DB caída, lib rota, JSON inválido), mandamos alerta a Telegram en
    # vez de morir en silencio. Histórico: el agente pasó 28h sin correr
    # y nadie se enteró hasta que el usuario notó la ausencia de mensajes.
    try:
        if args.debug:
            debug_mode_run()
        else:
            main()
    except SystemExit:
        raise
    except BaseException as _exc:
        traceback.print_exc()
        try:
            _write_heartbeat(error_msg=f"{type(_exc).__name__}: {_exc}"[:500])
        except Exception:
            pass
        _alert_telegram_crash(_exc)
        sys.exit(1)
