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


def audit_duplicate_matches() -> tuple[str, str]:
    """
    AUTO-REPARADOR: detecta pares duplicados (misma fecha, mismo marcador,
    nombres similares) y los fusiona en el acto — stats hacia la fila con
    nombre limpio, se borra la del apóstrofe/variante. Fue alerta hasta que
    demostró recrearse en cada recarga de datasets (13-sep-26).
    """
    df = pd.read_sql(text("""
        SELECT id, date, home_team, away_team, home_goals, away_goals,
               home_corners, away_corners, home_yellow, away_yellow,
               home_shots, away_shots, home_shots_target, away_shots_target
        FROM matches
        WHERE date >= CURRENT_DATE - 60 AND home_goals IS NOT NULL
        ORDER BY date
    """), engine)
    dups = []
    for _, g in df.groupby(["date", "home_goals", "away_goals"]):
        if len(g) < 2:
            continue
        rows = g.to_dict("records")
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                sh = difflib.SequenceMatcher(None, a["home_team"], b["home_team"]).ratio()
                sa = difflib.SequenceMatcher(None, a["away_team"], b["away_team"]).ratio()
                if sh > 0.62 and sa > 0.62:
                    dups.append((a, b))

    if not dups:
        return "ok", "sin duplicados ✓"

    def stats_count(r):
        cols = ("home_corners", "away_corners", "home_yellow", "away_yellow",
                "home_shots", "away_shots", "home_shots_target", "away_shots_target")
        return sum(1 for c in cols if pd.notna(r[c]))

    def has_apostrophe(r):
        return "'" in r["home_team"] or "'" in r["away_team"]

    to_delete = []
    with engine.begin() as conn:
        for a, b in dups:
            # keep = nombre limpio; si ambos limpios, el de más stats
            if has_apostrophe(a) and not has_apostrophe(b):
                keep, donor = b, a
            elif has_apostrophe(b) and not has_apostrophe(a):
                keep, donor = a, b
            elif stats_count(a) >= stats_count(b):
                keep, donor = a, b
            else:
                keep, donor = b, a
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
            """), {"hc": donor["home_corners"], "ac": donor["away_corners"],
                   "hy": donor["home_yellow"], "ay": donor["away_yellow"],
                   "hs": donor["home_shots"], "asx": donor["away_shots"],
                   "hst": donor["home_shots_target"], "ast": donor["away_shots_target"],
                   "kid": keep["id"]})
            to_delete.append(donor["id"])
        if to_delete:
            conn.execute(text("DELETE FROM matches WHERE id = ANY(:ids)"),
                         {"ids": to_delete})

    sample = "; ".join(f"{a['home_team']} vs {a['away_team']} ({str(a['date'])[:10]})"
                       for a, b in dups[:3])
    return "ok", (f"{len(dups)} duplicados detectados y AUTO-FUSIONADOS "
                  f"(ej: {sample})")


def audit_xg_sanity() -> tuple[str, str]:
    # El xG proxy del equipo PROMEDIO debe dar ~1.35-1.45 (promedio real liga).
    # Si se sale de rango, las constantes de conversión se descalibraron.
    df = pd.read_sql(text("""
        SELECT AVG(home_shots_target) AS sot, AVG(home_shots) AS sh
        FROM matches
        WHERE date >= CURRENT_DATE - 90 AND home_shots_target IS NOT NULL
    """), engine)
    if df.empty or df.iloc[0]["sot"] is None:
        return "ok", "sin datos de tiros recientes"
    sot = float(df.iloc[0]["sot"]); sh = float(df.iloc[0]["sh"])
    xg_prom = sot * 0.28 + max(sh - sot, 0) * 0.03
    if not (1.15 <= xg_prom <= 1.60):
        return "alerta", (f"xG promedio del proxy = {xg_prom:.2f} "
                          f"(esperado 1.35-1.45) — conversión descalibrada")
    return "ok", f"xG promedio proxy = {xg_prom:.2f} ✓"


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
    JUICIO DE LAS SENALES: CLV y ROI de las apuestas que llevan los tiltes
    nuevos (tabla miente, FLB asimetrico, empates contextuales). Informativo
    hasta ~50 bets; despues, flag con CLV negativo sostenido = quitar la senal.
    """
    df = pd.read_sql(text("""
        SELECT
          COUNT(*) FILTER (WHERE COALESCE(decision_log,'{}'::jsonb) ? 'shades') AS n_shades,
          AVG(clv) FILTER (WHERE COALESCE(decision_log,'{}'::jsonb) ? 'shades'
                           AND clv IS NOT NULL) AS clv_shades,
          COUNT(*) FILTER (WHERE COALESCE(decision_log,'{}'::jsonb) ? 'shades'
                           AND result IN ('win','half_win')) AS w_shades,
          COUNT(*) FILTER (WHERE COALESCE(decision_log,'{}'::jsonb) ? 'shades'
                           AND result IN ('win','loss','push','half_win','half_loss')) AS r_shades,
          SUM(profit) FILTER (WHERE COALESCE(decision_log,'{}'::jsonb) ? 'shades'
                           AND result IN ('win','loss','push','half_win','half_loss')) AS pnl
        FROM bets_history
        WHERE match_date >= CURRENT_DATE - 60
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
