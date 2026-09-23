"""
scripts/weekly_sanity_audit.py
==============================
AUDITOR DE SANIDAD DEL SISTEMA — las lecciones de los bugs encontrados en
septiembre-2026 convertidas en chequeos automáticos permanentes.

Cada chequeo existe porque un bug real pasó a producción:

  1. DUPLICADOS EN MATCHES   ← 427 filas duplicadas por variantes de nombre
                                ("nott'm forest" vs "nottm forest") corruptas
                                form/xG de todos los equipos
  2. SANIDAD DEL XG          ← el proxy daba 1.88 xG al equipo promedio
                                (real: ~1.40) → +35% inflación → overs
                                sobrevalorados en TODO el sistema
  3. CALIBRACIÓN RODANTE     ← predicción media 57.3% vs acierto real 44.4%
                                = sobreconfianza sistematica (ROI -7.7%)
  4. BETS DOBLES             ← mismo partido+mercado en el mismo día con
                                2+ filas (por cambios de horario del kickoff)
  5. RESOLUCIÓN ESTANCADA    ← bets pending con partido jugado hace >36h
                                (el hueco que dejó 127 stale)
  6. INTEGRIDAD NUMÉRICA     ← bets con probability NULL/0/1, odds <= 1

Corre en el weekly y envía alerta a Telegram SOLO si algo falla.
Exit code 1 si hay alertas (visible en Actions).
"""

import sys
from pathlib import Path

import pandas as pd
import difflib
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine


_STAT_COLS = ("home_corners", "away_corners", "home_yellow", "away_yellow",
              "home_shots", "away_shots", "home_shots_target", "away_shots_target")

# Por encima de esto no se fusiona solo: tantos "duplicados" de golpe
# indican que la heurística se descontroló (o una recarga masiva), y borrar
# partidos reales es peor que dejar duplicados una semana más.
MAX_AUTO_MERGE_PAIRS = 300


def _sql_value(v):
    """
    Valor apto para una columna INTEGER. pandas convierte los NULL de una
    columna entera en NaN (float); pasado tal cual, Postgres evaluaba
    COALESCE(int, NaN) como double y al guardarlo en la columna entera
    reventaba con "integer out of range" — la transacción entera se
    revertía y los duplicados NO se fusionaban (al menos desde el 21-sep-26).
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def find_duplicate_pairs(df: pd.DataFrame) -> list[tuple[dict, dict]]:
    """
    Pares candidatos a duplicado: misma fecha y marcador, nombres parecidos
    (>0.62 en local y visitante). Nunca empareja dos ligas distintas cuando
    ambas filas tienen liga: el mismo día y marcador en dos competiciones
    con nombres parecidos son dos partidos, no uno. Función pura.
    """
    pairs = []
    for _, g in df.groupby(["date", "home_goals", "away_goals"]):
        if len(g) < 2:
            continue
        rows = g.to_dict("records")
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                la, lb = a.get("league"), b.get("league")
                if (isinstance(la, str) and la and isinstance(lb, str) and lb
                        and la != lb):
                    continue
                sh = difflib.SequenceMatcher(None, a["home_team"], b["home_team"]).ratio()
                sa = difflib.SequenceMatcher(None, a["away_team"], b["away_team"]).ratio()
                if sh > 0.62 and sa > 0.62:
                    pairs.append((a, b))
    return pairs


def choose_keep(a: dict, b: dict) -> tuple[dict, dict]:
    """(keep, donor): nombre limpio sobre variante con apóstrofe; si ambos
    limpios, la fila con más stats."""
    def stats_count(r):
        return sum(1 for c in _STAT_COLS if _sql_value(r.get(c)) is not None)

    def has_apostrophe(r):
        return "'" in r["home_team"] or "'" in r["away_team"]

    if has_apostrophe(a) and not has_apostrophe(b):
        return b, a
    if has_apostrophe(b) and not has_apostrophe(a):
        return a, b
    return (a, b) if stats_count(a) >= stats_count(b) else (b, a)


def merge_params(keep: dict, donor: dict) -> dict:
    """Parámetros del UPDATE de fusión, sin NaN (ver _sql_value)."""
    keys = ("hc", "ac", "hy", "ay", "hs", "asx", "hst", "ast")
    params = {k: _sql_value(donor.get(c)) for k, c in zip(keys, _STAT_COLS)}
    params["kid"] = int(keep["id"])
    return params


def audit_duplicate_matches() -> tuple[str, str]:
    """
    AUTO-REPARADOR: detecta pares duplicados (misma fecha, mismo marcador,
    nombres similares) y los fusiona en el acto — stats hacia la fila con
    nombre limpio, se borra la del apóstrofe/variante. Fue alerta hasta que
    demostró recrearse en cada recarga de datasets (13-sep-26).
    """
    df = pd.read_sql(text("""
        SELECT id, date, league, home_team, away_team, home_goals, away_goals,
               home_corners, away_corners, home_yellow, away_yellow,
               home_shots, away_shots, home_shots_target, away_shots_target
        FROM matches
        WHERE date >= CURRENT_DATE - 60 AND home_goals IS NOT NULL
        ORDER BY date
    """), engine)
    dups = find_duplicate_pairs(df)

    if not dups:
        return "ok", "sin duplicados ✓"

    sample = "; ".join(f"{a['home_team']} vs {a['away_team']} ({str(a['date'])[:10]})"
                       for a, b in dups[:3])
    if len(dups) > MAX_AUTO_MERGE_PAIRS:
        return "alerta", (f"{len(dups)} pares duplicados — demasiados para fusionar "
                          f"solo (tope {MAX_AUTO_MERGE_PAIRS}); revisar a mano (ej: {sample})")

    to_delete: list[int] = []
    with engine.begin() as conn:
        for a, b in dups:
            # Triplicados: una fila ya marcada para borrar no participa en
            # otro par (ni como destino de stats ni como donante).
            if int(a["id"]) in to_delete or int(b["id"]) in to_delete:
                continue
            keep, donor = choose_keep(a, b)
            # fusionar stats faltantes del donor hacia keep
            conn.execute(text("""
                UPDATE matches k SET
                    home_corners      = COALESCE(k.home_corners,      :hc),
                    away_corners      = COALESCE(k.away_corners,      :ac),
                    home_yellow       = COALESCE(k.home_yellow,       :hy),
                    away_yellow       = COALESCE(k.away_yellow,       :ay),
                    home_shots        = COALESCE(k.home_shots,        :hs),
                    away_shots        = COALESCE(k.away_shots,        :asx),
                    home_shots_target = COALESCE(k.home_shots_target, :hst),
                    away_shots_target = COALESCE(k.away_shots_target, :ast)
                WHERE k.id = :kid
            """), merge_params(keep, donor))
            to_delete.append(int(donor["id"]))
        if to_delete:
            conn.execute(text("DELETE FROM matches WHERE id = ANY(:ids)"),
                         {"ids": to_delete})

    return "ok", (f"{len(to_delete)} duplicados detectados y AUTO-FUSIONADOS "
                  f"(ej: {sample})")


XG_RATIO_BAND = (0.90, 1.10)   # proxy / goles reales aceptable (±10%)


def xg_proxy_vs_goals(hsot: float, asot: float, hsh: float, ash: float,
                      hg: float, ag: float) -> tuple[float, float, float]:
    """
    (xG proxy por equipo, goles reales por equipo, ratio). Promedia LOCAL y
    VISITANTE, igual que get_team_xg (que mira tiros a favor y en contra en
    ambas condiciones). Función pura.
    """
    from src.features.xg_proxy import SHOT_ON_TARGET_RATE, SHOT_RATE

    def proxy(sot, sh):
        return sot * SHOT_ON_TARGET_RATE + max(sh - sot, 0.0) * SHOT_RATE

    xg_team = (proxy(hsot, hsh) + proxy(asot, ash)) / 2
    goals_team = (hg + ag) / 2
    ratio = xg_team / goals_team if goals_team > 0 else float("nan")
    return xg_team, goals_team, ratio


def audit_xg_sanity() -> tuple[str, str]:
    """
    El xG proxy promedio debe parecerse a los goles REALES del mismo periodo.
    Hasta el 22-sep-26 este chequeo usaba solo los tiros del LOCAL (que tira
    más que el visitante) y un rango fijo 1.35-1.45: alertaba "1.63" semana
    tras semana por la ventaja de local, no por el proxy. Ahora compara
    ambos lados contra los goles de los mismos partidos.
    """
    df = pd.read_sql(text("""
        SELECT AVG(home_shots_target) AS hsot, AVG(away_shots_target) AS asot,
               AVG(home_shots) AS hsh, AVG(away_shots) AS ash,
               AVG(home_goals) AS hg, AVG(away_goals) AS ag, COUNT(*) AS n
        FROM matches
        WHERE date >= CURRENT_DATE - 90
          AND home_shots_target IS NOT NULL AND away_shots_target IS NOT NULL
          AND home_shots IS NOT NULL AND away_shots IS NOT NULL
          AND home_goals IS NOT NULL AND away_goals IS NOT NULL
    """), engine)
    if df.empty or not int(df.iloc[0]["n"] or 0):
        return "ok", "sin datos de tiros recientes"
    r = df.iloc[0]
    xg_team, goals_team, ratio = xg_proxy_vs_goals(
        float(r["hsot"]), float(r["asot"]), float(r["hsh"]), float(r["ash"]),
        float(r["hg"]), float(r["ag"]))
    msg = (f"xG proxy {xg_team:.2f} vs goles reales {goals_team:.2f} por equipo "
           f"(ratio {ratio:.2f}, n={int(r['n'])}, 90d)")
    lo, hi = XG_RATIO_BAND
    if not (lo <= ratio <= hi):
        return "alerta", msg + " — conversión descalibrada"
    return "ok", msg + " ✓"


def audit_calibration() -> tuple[str, str]:
    df = pd.read_sql(text("""
        SELECT probability, result
        FROM bets_history
        WHERE result IN ('win','loss','half_win','half_loss')
          AND probability IS NOT NULL
          AND match_date >= CURRENT_DATE - 45
        ORDER BY match_date DESC LIMIT 100
    """), engine)
    if len(df) < 30:
        return "ok", f"muestra chica ({len(df)} < 30) — sin opinión"
    pred = df["probability"].mean()
    real = df["result"].isin(["win", "half_win"]).mean()
    gap = pred - real
    if abs(gap) > 0.08:
        return "alerta", (f"sobreconfianza: predice {pred:.1%}, acierta {real:.1%} "
                          f"(brecha {gap:+.1%} en últimas {len(df)})")
    return "ok", f"predicción {pred:.1%} vs real {real:.1%} (brecha {gap:+.1%}) ✓"


def audit_double_bets() -> tuple[str, str]:
    df = pd.read_sql(text("""
        SELECT match, market, COUNT(*) n, SUM(stake) AS st
        FROM bets_history
        WHERE match_date::date >= CURRENT_DATE - 14
        GROUP BY match, market, match_date::date
        HAVING COUNT(*) > 1
    """), engine)
    if len(df) > 0:
        sample = "; ".join(f"{r['match']} [{r['market']}] x{r['n']}" for _, r in df.head(3).iterrows())
        return "alerta", f"{len(df)} pares de bets duplicadas en 14d (ej: {sample})"
    return "ok", "sin bets duplicadas ✓"


def audit_stuck_resolution() -> tuple[str, str]:
    df = pd.read_sql(text("""
        SELECT COUNT(*) n FROM bets_history
        WHERE result IN ('pending','unresolved')
          AND match_date < NOW() - INTERVAL '36 hours'
    """), engine)
    n = int(df.iloc[0]["n"])
    if n > 15:
        return "alerta", f"{n} bets con partido jugado hace >36h siguen sin resolver"
    return "ok", f"{n} bets en rezago (umbral 15)"


def audit_numeric_integrity() -> tuple[str, str]:
    df = pd.read_sql(text("""
        SELECT COUNT(*) n FROM bets_history
        WHERE match_date >= CURRENT_DATE - 30
          AND (probability IS NULL OR probability <= 0 OR probability >= 1
               OR odds IS NULL OR odds <= 1 OR stake IS NULL OR stake < 0)
    """), engine)
    n = int(df.iloc[0]["n"])
    if n > 0:
        return "alerta", f"{n} bets con valores imposibles (prob/odds/stake) en 30d"
    return "ok", "integridad numérica ✓"



def audit_via_negativa() -> tuple[str, str]:
    """
    MÉTRICA VÍA NEGATIVA: cuánto margen añaden los kill-switches.
    Suma el profit histórico (180d) de los mercados y ligas que hoy están
    bloqueados. Negativo = cada semana que pasa bloqueado, el sistema
    "gana" ese dinero en apuestas evitadas. Es la estadística que ningún
    amateur mide y la que más margen acumula sin predecir nada.
    """
    blocked_leagues = [
        "soccer_fifa_world_cup_qualifiers_europe", "soccer_uefa_europa_league",
        "soccer_netherlands_eredivisie", "soccer_conmebol_copa_libertadores",
        "soccer_japan_j_league", "soccer_turkey_super_league",
        "soccer_norway_eliteserien", "soccer_efl_champ",
        "soccer_greece_super_league",
    ]
    blocked_markets = [
        "dnb_home", "away_win", "btts", "under25",
        "over_1.5", "over_3.5", "under_3.5", "shots_over_5.5",
        "shots_under_5.5", "dc_12",
    ]
    marks = ",".join(f"'{m}'" for m in blocked_markets)
    leagues = ",".join(f"'{l}'" for l in blocked_leagues)
    df = pd.read_sql(text(f"""
        SELECT
          COALESCE(SUM(profit) FILTER (
            WHERE market IN ({marks}) OR league IN ({leagues})), 0) AS evitado,
          COUNT(*) FILTER (
            WHERE market IN ({marks}) OR league IN ({leagues})) AS n_evitado
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND match_date >= CURRENT_DATE - 180
    """), engine)
    evitado = float(df.iloc[0]["evitado"] or 0)
    n = int(df.iloc[0]["n_evitado"] or 0)
    sign = "margen evitado" if evitado < 0 else "habria GANADO"
    return "ok", (f"via negativa: {n} bets bloqueadas de 180d sumaban "
                  f"{evitado:+.2f}u ({sign})")



def audit_shades() -> tuple:
    """
    JUICIO DE LAS SENALES: CLV y ROI de las apuestas donde una señal manual
    MOVIÓ la probabilidad: "la tabla miente" aplicada (shades.aplicado) o
    empate contextual (shades.draw_context). Informativo hasta ~50 bets;
    despues, flag con CLV negativo sostenido = quitar la senal.

    Hasta el 22-sep-26 buscaba 'shades' en la raíz del decision_log, pero
    vive en decision_log.model.shades: el chequeo nunca encontró una apuesta.
    """
    df = pd.read_sql(text("""
        WITH b AS (
            SELECT clv, result, profit,
                   COALESCE(decision_log->'model'->'shades', '{}'::jsonb) AS s
            FROM bets_history
            WHERE match_date >= CURRENT_DATE - 60
              AND jsonb_typeof(decision_log) = 'object'
        )
        SELECT
          COUNT(*) FILTER (WHERE s ? 'aplicado' OR s ? 'draw_context') AS n_shades,
          AVG(clv) FILTER (WHERE (s ? 'aplicado' OR s ? 'draw_context')
                           AND clv IS NOT NULL) AS clv_shades,
          COUNT(*) FILTER (WHERE (s ? 'aplicado' OR s ? 'draw_context')
                           AND result IN ('win','half_win')) AS w_shades,
          COUNT(*) FILTER (WHERE (s ? 'aplicado' OR s ? 'draw_context')
                           AND result IN ('win','loss','push','half_win','half_loss')) AS r_shades,
          SUM(profit) FILTER (WHERE (s ? 'aplicado' OR s ? 'draw_context')
                           AND result IN ('win','loss','push','half_win','half_loss')) AS pnl
        FROM b
    """), engine)
    n = int(df.iloc[0]["n_shades"] or 0)
    clv = df.iloc[0]["clv_shades"]
    pnl = float(df.iloc[0]["pnl"] or 0)
    if n == 0:
        return "ok", "aun sin apuestas con senales nuevas"
    clv_s = ", CLV {:+.2%}".format(float(clv)) if clv is not None else ""
    return "ok", "{} bets con senales, {} resueltas, pnl {:+.2f}u{}".format(
        n, int(df.iloc[0]["r_shades"]), pnl, clv_s)


CHECKS = [
    ("Partidos duplicados",    audit_duplicate_matches),
    ("Sanidad del xG proxy",   audit_xg_sanity),
    ("Calibracion rodante",    audit_calibration),
    ("Bets dobles",            audit_double_bets),
    ("Resolucion estancada",   audit_stuck_resolution),
    ("Integridad numerica",    audit_numeric_integrity),
    ("Via negativa",           audit_via_negativa),
    ("Senales (shades)",       audit_shades),
]


def run_sanity_audit(verbose: bool = True) -> dict:
    results = []
    for name, fn in CHECKS:
        try:
            status, msg = fn()
        except Exception as e:
            status, msg = "error", f"{type(e).__name__}: {e}"
        results.append((name, status, msg))
        if verbose:
            icon = {"ok": "✅", "alerta": "🚨", "error": "⚠️"}.get(status, "·")
            print(f"  {icon} {name:<24} {msg}")

    alerts = [r for r in results if r[1] in ("alerta", "error")]
    if verbose:
        print(f"\n  {'🎉 Sin alertas de sanidad' if not alerts else f'🚨 {len(alerts)} alerta(s)'}")

    if alerts:
        try:
            from scripts.notify_telegram import send_message
            send_message(
                "🩺 <b>AUDITORÍA DE SANIDAD</b>\n\n"
                + "\n".join(f"• <b>{n}</b>: {m}" for n, s, m in alerts)
                + "\n\n<i>Revisar antes de seguir apostando.</i>"
            )
        except Exception:
            pass

    return {"alerts": alerts, "results": results}


if __name__ == "__main__":
    print("\n🩺 AUDITORÍA DE SANIDAD SEMANAL\n" + "=" * 60)
    run_sanity_audit()
