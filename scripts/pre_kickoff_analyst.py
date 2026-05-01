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
)
from scripts.notify_telegram import (
    _is_suspicious,
    _dedup_correlated,
    _get_market_label,
    LEAGUE_LABELS,
    send_pre_kickoff_verdict,
)


SYSTEM_PROMPT = """\
Eres un analista deportivo. Validás UNA apuesta generada por un modelo
cuantitativo. Recibirás CONTEXTO DEL MODELO (H2H, forma, congestión, xG,
movimiento de línea, motivación, clima) ya pre-calculado — NO lo busques
de nuevo en la web.

Tu trabajo: usar web_search SOLO para 2 cosas que NO están en el contexto:
1. Alineación probable/confirmada (predicted XI o starting lineup)
2. Noticias de lesiones/suspensiones de últimas 48h

Cap: 2 búsquedas máximo. Si la primera resolvió alineaciones+lesiones,
no hagas la segunda.

Salida ESTRICTA — solo JSON, sin markdown, sin prosa:
{
  "verdict": "STRONG" | "MEDIUM" | "SKIP",
  "confidence": 1-5,
  "reasoning": "máx 1 oración (<=25 palabras)",
  "lineups": "L1=confirmadas | L2=probables | L0=sin info",
  "key_factors": ["<=3 factores breves"]
}

Criterios:
- STRONG: titulares clave dentro + contexto cuantitativo alinea con la
  apuesta + sin red flag de rotación.
- MEDIUM: 1 baja secundaria O info parcial O contexto borderline.
- SKIP: titular clave fuera, rotación masiva confirmada (UCL en 3 días,
  equipo ya clasificado descansando), o lineup contradice la apuesta
  (Over 2.5 pero ambos equipos rotan B-team).

Si no hay info confirmada de lineup: MEDIUM, confidence=2, lineups="L0".
Nunca inventes datos.
"""


# ============================================================
# DB QUERIES
# ============================================================

def _fetch_pending_bets() -> pd.DataFrame:
    """Bets pending con kickoff dentro de la ventana pre-kickoff."""
    now_utc = datetime.now(timezone.utc)
    lo = now_utc + timedelta(minutes=PRE_KICKOFF_WINDOW_MIN)
    hi = now_utc + timedelta(minutes=PRE_KICKOFF_WINDOW_MAX)

    df = pd.read_sql(text("""
        SELECT b.match, b.match_date, b.market,
               b.probability, b.odds, b.edge, b.stake,
               COALESCE(u.sport_key, b.league, '') AS league
        FROM bets_history b
        LEFT JOIN upcoming_matches u
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


def _analyze_bet(client, bet: dict, ctx: dict) -> dict:
    """Llama a Claude API con web_search para emitir dictamen."""
    market_label = _get_market_label(bet["market"], bet["match"])
    league_label = LEAGUE_LABELS.get(bet["league"], bet["league"])

    md = pd.to_datetime(bet["match_date"])
    md_utc = md if md.tzinfo is not None else md.tz_localize("UTC")
    dt_local = md_utc.tz_convert(ZoneInfo(USER_TIMEZONE))

    user_msg = (
        f"Partido: {bet['match']}\n"
        f"Liga: {league_label}\n"
        f"Kickoff: {dt_local.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"Apuesta: {market_label} @ {float(bet['odds']):.2f}\n"
        f"Edge del modelo: +{float(bet['edge'])*100:.1f}% "
        f"(prob {float(bet['probability'])*100:.1f}%)\n\n"
        f"CONTEXTO DEL MODELO (no buscar):\n{_format_context(ctx)}\n\n"
        f"Devuelve SOLO el JSON."
    )

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 2,
        }],
        messages=[{"role": "user", "content": user_msg}],
    )

    # Extraer último bloque de texto (después de tool_use loops)
    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = text_blocks[-1].strip() if text_blocks else "{}"
    if raw.startswith("```"):
        # Cleanup defensivo si Claude devolvió ```json ... ```
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {
            "verdict": "MEDIUM", "confidence": 1,
            "reasoning": "parse error en respuesta del modelo",
            "lineups": "L0", "key_factors": [],
        }

    # Sources: extraer URLs visitadas por web_search
    sources: list[str] = []
    for block in resp.content:
        if getattr(block, "type", "") == "web_search_tool_result":
            for r in getattr(block, "content", []) or []:
                url = getattr(r, "url", None)
                if url:
                    sources.append(url)
    parsed["sources"] = sources
    return parsed


def _save_analysis(bet: dict, result: dict):
    """Persiste el dictamen en pre_kickoff_analyses (idempotente)."""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO pre_kickoff_analyses
                (match, match_date, market, verdict, confidence,
                 reasoning, lineups, sources)
            VALUES
                (:match, :match_date, :market, :verdict, :confidence,
                 :reasoning, :lineups, :sources)
            ON CONFLICT (match, market, match_date) DO NOTHING
        """), {
            "match":      bet["match"],
            "match_date": bet["match_date"],
            "market":     bet["market"],
            "verdict":    result.get("verdict", "MEDIUM"),
            "confidence": int(result.get("confidence", 1) or 1),
            "reasoning":  (result.get("reasoning") or "")[:1000],
            "lineups":    (result.get("lineups") or "")[:500],
            "sources":    json.dumps(result.get("sources", [])),
        })


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"\n🔍 PRE-KICKOFF ANALYST — ventana [{PRE_KICKOFF_WINDOW_MIN}, "
          f"{PRE_KICKOFF_WINDOW_MAX}] min desde NOW UTC\n")

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY no configurada — abortando.")
        return

    bets = _fetch_pending_bets()
    if bets.empty:
        print("✓ Sin bets pending en la ventana pre-kickoff")
        return

    bets = _filter_confiables(bets)
    if bets.empty:
        print("✓ Sin bets CONFIABLES en la ventana (todas filtradas)")
        return

    print(f"📋 {len(bets)} bet(s) confiable(s) en ventana — analizando...\n")

    # Import perezoso de anthropic — solo cuando hay bets para analizar.
    # Evita romper el cron si la lib no se instaló y no hay nada que hacer.
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    verdicts = []
    for _, bet in bets.iterrows():
        b = bet.to_dict()
        if _already_analyzed(b["match"], b["market"], b["match_date"]):
            print(f"  · {b['match']} ({b['market']}) — ya analizada, skip")
            continue

        ctx = _build_context(b)

        # Filtro pre-API rule-based (sin tokens)
        skip_reason = _quick_skip_signals(b, ctx)
        if skip_reason:
            result = {
                "verdict": "SKIP", "confidence": 4,
                "reasoning": skip_reason,
                "lineups": "L0",
                "key_factors": [skip_reason],
                "sources": [],
            }
            print(f"  🔴 {b['match']} ({b['market']}) — SKIP rule-based: {skip_reason}")
        else:
            try:
                result = _analyze_bet(client, b, ctx)
                v = result.get("verdict", "?")
                icon = {"STRONG": "🟢", "MEDIUM": "🟡", "SKIP": "🔴"}.get(v, "⚪")
                print(f"  {icon} {b['match']} ({b['market']}) — {v}: "
                      f"{result.get('reasoning', '')[:80]}")
            except Exception as e:
                print(f"  ❌ Error analizando {b['match']}: {e}")
                continue

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


if __name__ == "__main__":
    main()
