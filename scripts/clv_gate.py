"""
scripts/clv_gate.py
===================
CLV como métrica de DECISIÓN (no solo auditoría).

Con pocas apuestas el ROI es puro ruido; el CLV promedio predice la
rentabilidad de largo plazo mucho antes. Regla:

  Si un mercado tiene >= MIN_BETS (default 100) bets con closing_odds en los
  últimos LOOKBACK días y CLV promedio < CLV_FLOOR (default -0.005, escala de
  probabilidad), el mercado se considera sin ventaja real contra el cierre
  y se BLOQUEA: se guarda en la DB (model_state/clv_gate_markets), que el
  prediction_pipeline suma a sus kill-switches en cada corrida.

Se desbloquea solo: si el CLV trailing recupera (o la muestra cae por
rotación de la ventana), el mercado sale del archivo y vuelve a apostarse.

Persistencia (22-sep-26): el estado vive en la DB (model_state), con los
JSON de config/ como espejo. Antes solo existían los archivos, que morían
con el runner del weekly: el morning nunca los vio, el gate nunca bloqueó
nada en producción y la histéresis (2 semanas seguidas) jamás pudo contar
más de 1 semana.

Reactivación por shadow (run_shadow_reactivation): mercados bloqueados de
forma fija en el pipeline vuelven SOLO con evidencia de sus candidatas
shadow medidas contra el cierre, sin arriesgar dinero.

Solo mira datos de la cohorte actual (settings.LEARNING_SINCE).

Ejecutar semanalmente (lo llama el orchestrator --mode weekly).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from config.settings import LEARNING_SINCE
from src.utils.model_state import load_state, save_state

ROOT = Path(__file__).parent.parent
BLOCKED_FILE = ROOT / "config" / "clv_blocked_markets.json"
STATE_KEY_MARKETS = "clv_gate_markets"
STATE_KEY_LEAGUES = "clv_gate_leagues"
STATE_KEY_REACTIVATION = "shadow_reactivation"

LOOKBACK_DAYS = 120
_LEAGUE_MIN_BETS = 20      # muestra mínima para auto-bloquear liga
_LEAGUE_CLV_FLOOR = -0.05  # CLV medio peor que -5% → auto-bloqueo      # ventana trailing
# Ronda 7 (B1/R10): el criterio fijo n>=100 era INALCANZABLE — con las tasas
# reales (Q-Q: máximo 94 bets/120d en el mercado más grande, cobertura de
# closing 69-85%) el gate de mercados nunca había podido dispararse. Nuevo
# criterio ESTADÍSTICO: bloquear cuando el límite superior del IC95
# unilateral del CLV medio queda bajo cero (CLV significativamente negativo).
# Con un efecto real (CLV ≈ −1 a −2pt) alcanza con n=30-40, que sí es
# acumulable en semanas para los mercados grandes.
MIN_BETS_STAT = 30         # muestra mínima para el test estadístico
CLV_Z_95 = 1.645           # z unilateral 95% (normal aprox. del CLV medio)
MIN_BETS      = 100      # legado: solo se usa en el payload de política
CLV_FLOOR     = -0.005   # legado: documentación del criterio viejo


def market_clv_blocked(n: int, mean: float, sd: float) -> bool:
    """
    Criterio B1 (ronda 7): un mercado se bloquea cuando su CLV medio es
    significativamente negativo — límite superior del IC95 unilateral por
    debajo de cero. Con sd típico de CLV (~0.04) y efecto real (−1 a −2pt),
    dispara con n=30-40 en vez de n=100.
    """
    if n < MIN_BETS_STAT or sd is None or sd <= 0:
        return False
    upper = mean + CLV_Z_95 * sd / (n ** 0.5)
    return upper < 0


def _deviation_band(dev: float) -> str:
    """Banda de desvío para el análisis CLV del shadow (B2, ronda 7)."""
    if dev is None:
        return "sin_ref"
    b = abs(dev) * 100
    if b < 5:
        return "0-5"
    if b < 10:
        return "5-10"
    if b < 15:
        return "10-15"
    if b < 19:
        return "15-19"
    if b < 25:
        return "19-25"
    if b <= 30:
        return "25-30"
    return "30+"


def shadow_clv_bands(verbose: bool = True) -> dict:
    """
    CLV de las candidatas SHADOW por (mercado × banda) — D1/R12, ronda 9.

    La decisión B2 (bajar o no el piso) se aplica POR MERCADO (cada uno
    tiene su ventana), así que la conclusión debe agregarse en esa misma
    dimensión. Antes se agrupaba solo por banda: el CLV de una banda era el
    del mercado que dominara la cobertura de closing, no el del nivel de
    desvío. Columnas separadas n_banda / n_con_closing: "hay filas" y "hay
    medición" no son el mismo dato (Q-S ronda 7: cobertura 0-97%).
    """
    try:
        df = pd.read_sql(text("""
            SELECT market, deviation, odds, closing_odds
            FROM shadow_bets
            WHERE deviation IS NOT NULL AND odds > 1
        """), engine)
    except Exception as e:
        # tabla aún no creada → no es error en el primer ciclo
        if verbose:
            print(f"   ℹ️  shadow_clv_bands: {type(e).__name__} (¿tabla sin crear?)")
        return {"status": "no_table"}

    if df.empty:
        if verbose:
            print("   ℹ️  shadow_clv_bands: sin filas todavía")
        return {"status": "no_data"}

    df["clv"] = 1.0 / df["closing_odds"] - 1.0 / df["odds"]
    df.loc[df["closing_odds"].isna() | (df["closing_odds"] <= 1), "clv"] = None
    df["band"] = df["deviation"].map(_deviation_band)

    out = {}
    if verbose:
        print("\n🌑 SHADOW CLV por (mercado × banda): n_banda · n_closing · CLV")
    for (mkt, band), sub in df.groupby(["market", "band"]):
        n_band = len(sub)
        measured = sub["clv"].dropna()
        n_close = len(measured)
        avg = float(measured.mean()) if n_close else None
        out.setdefault(mkt, {})[band] = {
            "n_banda": n_band, "n_con_closing": n_close,
            "avg_clv": round(avg, 5) if avg is not None else None,
        }
        if verbose and n_band >= 3:   # ruido mínimo fuera del log
            clv_s = f"{avg:+.4f}" if avg is not None else "  -  "
            print(f"   {mkt:<16} [{band:>5}] n={n_band:>3} - n_closing={n_close:>3} - CLV={clv_s}")
    return {"status": "ok", "by_market": out}




# ── C2 (ronda 8): histéresis ─────────────────────────────────────────
# El gate viejo recalculaba la lista de cero cada semana: al bloquear, el
# mercado deja de apostar, la ventana se vacía, n<30 y desbloquea sin que
# el CLV haya mejorado (ciclo de 24-41 semanas). Nuevo diseño:
#   * BLOQUEO exige 2 ventanas semanales consecutivas con CLV
#     significativamente negativo (corrección de multiplicidad: P(espurio)
#     por mercado-año cae de ~14% a ~2%).
#   * DESBLOQUEO solo con evidencia positiva: n>=30 y CLV medio >= 0.
#     La ausencia de datos ya no es prueba de inocencia.
BLOCK_STREAK_WEEKS = 2


def significant_negative(n, mean, sd) -> bool:
    """CLV significativamente negativo (IC95 unilateral por debajo de 0)."""
    return n >= MIN_BETS_STAT and sd is not None and sd > 0 and (
        mean + CLV_Z_95 * sd / (n ** 0.5) < 0)


def positive_evidence(n, mean, sd) -> bool:
    """Evidencia suficiente para desbloquear: muestra real y CLV no negativo."""
    return n >= MIN_BETS_STAT and mean >= 0


def merge_gate_state(prev_blocked, prev_streaks, stats):
    """
    Fusión de estado (función pura, testeable).

    prev_blocked: set de mercados bloqueados la semana pasada
    prev_streaks: {market: semanas consecutivas significativo-negativas}
    stats:        {market: (n, mean, sd)} de la ventana actual

    Retorna (blocked_set, streaks, unblocked_list, blocked_since_updates)
    """
    blocked = set(prev_blocked)
    streaks = {}
    unblocked = []

    # mercados sin datos esta semana: conservan streak congelada
    for mkt in prev_blocked:
        if mkt not in stats:
            streaks[mkt] = prev_streaks.get(mkt, 0)

    for mkt, (n, mean, sd) in stats.items():
        if significant_negative(n, mean, sd):
            streaks[mkt] = prev_streaks.get(mkt, 0) + 1
        else:
            streaks[mkt] = 0

        if mkt in prev_blocked:
            if positive_evidence(n, mean, sd):
                unblocked.append(mkt)
                blocked.discard(mkt)
            # sin evidencia positiva → sigue bloqueado (histéresis)
        elif streaks[mkt] >= BLOCK_STREAK_WEEKS:
            blocked.add(mkt)

    return blocked, streaks, unblocked

def run_clv_gate(verbose: bool = True) -> dict:
    try:
        df = pd.read_sql(text(f"""
            SELECT market, odds, closing_odds
            FROM bets_history
            WHERE closing_odds IS NOT NULL
              AND closing_odds > 1
              AND odds > 1
              AND result IN ('win', 'loss', 'half_win', 'half_loss')
              AND match_date >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
              AND match_date >= CAST(:since AS timestamptz)
        """), engine, params={"since": LEARNING_SINCE})
    except Exception as e:
        if verbose:
            print(f"❌ clv_gate: no se pudo leer bets_history: {e}")
        return {"status": "error", "error": str(e)}

    if df.empty:
        if verbose:
            print("clv_gate: sin datos de CLV — no se bloquea nada.")
        _write_blocked([], verbose=False)
        return {"status": "no_data", "blocked": []}

    # CLV en escala de probabilidad (misma definición que clv_tracker)
    df["clv"] = 1.0 / df["closing_odds"] - 1.0 / df["odds"]

    # Estado previo (C2): blocked + streaks viven en el payload del JSON
    prev_state = _read_blocked_state()
    prev_blocked = set(prev_state.get("blocked_markets", []))
    prev_streaks = prev_state.get("negative_streak", {})

    stats = {}
    report = []
    for mkt, sub in df.groupby("market"):
        n = len(sub)
        avg = float(sub["clv"].mean())
        sd = float(sub["clv"].std(ddof=1)) if n > 1 else None
        stats[str(mkt)] = (n, avg, sd)
        entry = {"market": str(mkt), "n": n, "avg_clv": round(avg, 5)}
        if significant_negative(n, avg, sd):
            entry["significativo_neg"] = True
        report.append(entry)

    blocked_set, streaks, unblocked = merge_gate_state(
        prev_blocked, prev_streaks, stats)
    blocked = sorted(blocked_set)
    for u in unblocked:
        print(f"   🔓 {u}: desbloqueado por evidencia positiva (CLV>=0, n>=30)")
    for mkt in blocked:
        if mkt not in prev_blocked:
            print(f"   ⛔ {mkt}: bloqueado (2 ventanas consecutivas con CLV neg. significativo)")

    # ── GATE POR LIGA (n>=20, CLV <= -5%) ─────────────────────────────
    # Protege las ligas re-habilitadas: si vuelven a fallar con muestra,
    # vuelven a bloquearse solas. Mismo criterio que los mercados.
    blocked_leagues = []
    try:
        lg = pd.read_sql(text(f"""
            SELECT league, odds, closing_odds
            FROM bets_history
            WHERE closing_odds IS NOT NULL AND closing_odds > 1 AND odds > 1
              AND league IS NOT NULL
              AND result IN ('win', 'loss', 'push', 'half_win', 'half_loss')
              AND match_date >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
              AND match_date >= CAST(:since AS timestamptz)
        """), engine, params={"since": LEARNING_SINCE})
        if not lg.empty:
            lg["clv"] = 1.0 / lg["closing_odds"] - 1.0 / lg["odds"]
            for l, sub in lg.groupby("league"):
                if len(sub) < _LEAGUE_MIN_BETS:
                    continue
                avg = float(sub["clv"].mean())
                if avg <= _LEAGUE_CLV_FLOOR:
                    blocked_leagues.append(str(l))
    except Exception as e:
        print(f"   ⚠️  League gate omitido: {e}")
    _write_league_blocked(blocked_leagues, verbose=verbose)

    # blocked_since: conservar el existente; fecha nueva para los recién bloqueados
    prev_since = prev_state.get("blocked_since", {})
    now_iso = datetime.now(timezone.utc).isoformat()
    blocked_since = {m: prev_since.get(m, now_iso) for m in blocked}
    _write_blocked(blocked, verbose=verbose, streaks=streaks,
                   blocked_since=blocked_since)

    if verbose:
        print(f"\n🚦 CLV GATE (últimos {LOOKBACK_DAYS}d, criterio: IC95 unilateral "
              f"< 0 con n>={MIN_BETS_STAT})")
        for e in sorted(report, key=lambda x: x["avg_clv"]):
            flag = " ⛔ BLOQUEADO" if e.get("blocked") else ""
            print(f"   {e['market']:<16} n={e['n']:>4}  CLV medio={e['avg_clv']:+.4f}{flag}")
        if not blocked:
            print("   ✅ Ningún mercado cumple criterio de bloqueo")
        if blocked_leagues:
            print(f"   ⛔ LIGAS bloqueadas: {', '.join(blocked_leagues)}")
        else:
            print("   ✅ Ninguna liga cumple criterio de bloqueo")

    return {
        "status": "ok",
        "blocked": blocked,
        "blocked_leagues": blocked_leagues,
        "by_market": report,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_blocked_state() -> dict:
    """Estado completo del gate (blocked + streaks + blocked_since): DB
    primero — sin esto la racha de semanas negativas se reiniciaba en
    cada runner y el bloqueo (exige 2 seguidas) era inalcanzable."""
    return load_state(STATE_KEY_MARKETS, BLOCKED_FILE) or {}


def _write_blocked(blocked: list, verbose: bool = True,
                   streaks: dict | None = None, blocked_since: dict | None = None):
    payload = {
        "blocked_markets": blocked,
        "negative_streak": streaks or {},
        "blocked_since": blocked_since or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "lookback_days": LOOKBACK_DAYS,
            "criterion": ("2 ventanas consecutivas con IC95 unilateral < 0 "
                          f"(n>={MIN_BETS_STAT}); desbloqueo solo con "
                          "evidencia positiva (CLV>=0, n>=30)"),
            "min_bets_stat": MIN_BETS_STAT,
            "block_streak_weeks": BLOCK_STREAK_WEEKS,
            "legacy_min_bets": MIN_BETS,
            "legacy_clv_floor": CLV_FLOOR,
            "learning_since": LEARNING_SINCE,
        },
    }
    save_state(STATE_KEY_MARKETS, payload, file_path=BLOCKED_FILE)


LEAGUE_BLOCKED_FILE = ROOT / "config" / "clv_blocked_leagues.json"


def _write_league_blocked(leagues: list, verbose: bool = True):
    payload = {
        "blocked_leagues": leagues,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {"min_bets": _LEAGUE_MIN_BETS, "clv_floor": _LEAGUE_CLV_FLOOR,
                   "learning_since": LEARNING_SINCE},
    }
    save_state(STATE_KEY_LEAGUES, payload, file_path=LEAGUE_BLOCKED_FILE)


def load_clv_blocked_leagues() -> set:
    """Ligas bloqueadas por el gate (DB; archivo como fallback local)."""
    data = load_state(STATE_KEY_LEAGUES, LEAGUE_BLOCKED_FILE) or {}
    return set(data.get("blocked_leagues", []))


def load_clv_blocked_markets() -> set:
    """Mercados bloqueados por el gate (DB; archivo como fallback local)."""
    data = load_state(STATE_KEY_MARKETS, BLOCKED_FILE) or {}
    return set(data.get("blocked_markets", []))


# ── Reactivación por shadow ───────────────────────────────────────────
# Mercados que el pipeline bloquea de forma FIJA (no por el gate) y que
# pueden volver solo con evidencia de sus candidatas shadow: las que se
# habrían apostado (modelo por encima del precio y edge >= piso operativo)
# medidas contra el cierre, sin arriesgar dinero. Es el criterio que la
# ronda 7 (B1b) dejó documentado para los favoritos AH, ahora ejecutado.
#   - away_win: bloqueo fijo desde abril-26 (el peor mercado del histórico)
#   - AH con el local favorito: grupos ah_home_fav y ah_away_dog (ver
#     _ah_group — son las dos patas de la misma línea)
SHADOW_REACTIVABLE = ("away_win", "ah_home_fav", "ah_away_dog")
REACTIVATION_MIN_N = MIN_BETS_STAT      # 30, como el gate
REACTIVATION_MIN_EDGE = 0.05            # piso operativo del pipeline (MIN_EDGE)


def _reactivation_group(market: str) -> str:
    from src.models.calibration_monitor import _ah_group
    return _ah_group(market) or market


def merge_reactivation_state(prev: dict, stats: dict) -> dict:
    """
    Función pura. prev: {grupo: bool reactivado}; stats: {grupo: (n, mean, sd)}.
    Reactiva con evidencia positiva (n >= 30 y CLV medio >= 0); vuelve a
    bloquear solo con CLV significativamente negativo; entre ambos manda el
    estado anterior (histéresis, igual que merge_gate_state).
    """
    out = {g: bool(prev.get(g, False)) for g in SHADOW_REACTIVABLE}
    for g, (n, mean, sd) in stats.items():
        if g not in out:
            continue
        if not out[g] and positive_evidence(n, mean, sd):
            out[g] = True
        elif out[g] and significant_negative(n, mean, sd):
            out[g] = False
    return out


def shadow_reactivation_stats() -> dict:
    """
    Solo lectura: {grupo: (n, CLV medio, sd)} de las candidatas shadow que
    se habrían apostado, por grupo reactivable. Lanza si la DB falla.
    """
    df = pd.read_sql(text("""
        SELECT market, odds, closing_odds, deviation, edge_market
        FROM shadow_bets
        WHERE closing_odds > 1 AND odds > 1
          AND deviation > 0
          AND edge_market >= :min_edge
          AND match_date >= CAST(:since AS timestamptz)
    """), engine, params={"min_edge": REACTIVATION_MIN_EDGE, "since": LEARNING_SINCE})
    stats = {}
    if not df.empty:
        df["grp"] = df["market"].map(_reactivation_group)
        df = df[df["grp"].isin(SHADOW_REACTIVABLE)].copy()
        df["clv"] = 1.0 / df["closing_odds"].astype(float) - 1.0 / df["odds"].astype(float)
        for g, sub in df.groupby("grp"):
            n = len(sub)
            sd = float(sub["clv"].std(ddof=1)) if n > 1 else None
            stats[str(g)] = (n, float(sub["clv"].mean()), sd)
    return stats


def run_shadow_reactivation(verbose: bool = True) -> dict:
    try:
        stats = shadow_reactivation_stats()
    except Exception as e:
        if verbose:
            print(f"   ℹ️  reactivación shadow omitida: {type(e).__name__}: {e}")
        return {"status": "error"}

    prev_state = load_state(STATE_KEY_REACTIVATION) or {}
    prev = prev_state.get("reactivated", {})
    new = merge_reactivation_state(prev, stats)
    changed = {g: v for g, v in new.items() if bool(prev.get(g, False)) != v}
    save_state(STATE_KEY_REACTIVATION, {
        "reactivated": new,
        "stats": {g: {"n": s[0], "mean_clv": round(s[1], 5)} for g, s in stats.items()},
        "policy": {"min_n": REACTIVATION_MIN_N, "min_edge": REACTIVATION_MIN_EDGE,
                   "learning_since": LEARNING_SINCE},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if verbose:
        print("\n🔁 REACTIVACIÓN POR SHADOW (candidatas que se habrían apostado vs cierre)")
        for g in SHADOW_REACTIVABLE:
            n, mean, _ = stats.get(g, (0, None, None))
            clv_s = f"{mean:+.4f}" if mean is not None else "  -  "
            estado = "REACTIVADO" if new[g] else "bloqueado"
            print(f"   {g:<12} n={n:>3}  CLV={clv_s}  → {estado}")
    return {"status": "ok", "reactivated": new, "changed": changed, "stats": stats}


def load_shadow_reactivated() -> set:
    """Grupos bloqueados de forma fija que el shadow ya reactivó."""
    data = load_state(STATE_KEY_REACTIVATION) or {}
    return {g for g, on in (data.get("reactivated") or {}).items() if on}


if __name__ == "__main__":
    run_clv_gate()
