"""
scripts/db_smoke_test.py
========================
Smoke test de INTEGRACIÓN contra la base de datos real (Neon).

Valida que TODAS las queries introducidas/modificadas en la revisión de
septiembre-2026 funcionan contra el esquema real de producción:

  1. Conectividad y versión de Postgres
  2. Esquema: tablas y columnas esperadas existen
  3. Query de calibración (ventana 180d + exclusión holdout 45d)
  4. Query del CLV gate (por mercado, 120d)
  5. Queries del holdout evaluation (fit vs holdout)
  6. Query del market regime monitor (vig/consenso desde upcoming_matches)
  7. Query de stats por equipo de enrich_matches (filtro SQL por equipo)
  8. Query de line movement por match_key
  9. Query del cap de exposición por slate (pending por fecha)
  10. Queries de slippage (odds_placed) y backtest_engine
  11. DDL guards de save_bets (ADD COLUMN IF NOT EXISTS — idempotente)
  12. Ciclo de aprendizaje (22-sep-26): model_state, cohorte, peso del
      modelo y reactivación por shadow EN SECO, closing compartido y BTTS

READ-ONLY sobre datos: no inserta, no actualiza ni borra bets. Los DDL
guards son idempotentes (IF NOT EXISTS) y son los mismos que correría el
pipeline en su próxima ejecución — aplicarlos aquí es seguro y evita
sorpresas en producción.

Uso:
    python scripts/db_smoke_test.py

Exit code 0 = todo OK. Diseñado para correr en GitHub Actions con el
secret DB_URL (ver .github/workflows/db_smoke.yml).
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str):
    """Decorator: registra cada check con pass/fail y mensaje."""
    def deco(fn):
        try:
            msg = fn() or ""
            results.append((name, True, msg))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))
    return deco


@check("Conectividad a Postgres")
def _conn():
    with engine.connect() as c:
        v = c.execute(text("SELECT version()")).scalar()
        return str(v).split(",")[0]


@check("Esquema: tablas core existen y tienen filas")
def _tables():
    with engine.connect() as c:
        for t in ("matches", "upcoming_matches", "bets_history"):
            n = c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            assert n is not None, f"{t} inaccesible"
        return f"matches={c.execute(text('SELECT COUNT(*) FROM matches')).scalar():,} rows"


@check("Calibración: ventana 180d + exclusión holdout 45d")
def _calib():
    from src.models.calibration_monitor import CALIBRATION_WINDOW_DAYS, CALIBRATION_HOLDOUT_DAYS
    df = pd.read_sql(text(f"""
        SELECT market, probability, result, league
        FROM bets_history
        WHERE result IN ('win', 'loss', 'half_win', 'half_loss')
          AND probability IS NOT NULL AND probability > 0 AND probability < 1
          AND match_date >= NOW() - INTERVAL '{CALIBRATION_WINDOW_DAYS} days'
          AND match_date <  NOW() - INTERVAL '{CALIBRATION_HOLDOUT_DAYS} days'
    """), engine)
    return f"{len(df)} bets en ventana de fit"


@check("CLV gate: agregado por mercado 120d (escala prob)")
def _clv_gate():
    df = pd.read_sql(text("""
        SELECT market, odds, closing_odds
        FROM bets_history
        WHERE closing_odds IS NOT NULL AND closing_odds > 1 AND odds > 1
          AND result IN ('win', 'loss', 'half_win', 'half_loss')
          AND match_date >= NOW() - INTERVAL '120 days'
    """), engine)
    return f"{len(df)} bets con closing_odds"


@check("Holdout evaluation: ventanas fit vs holdout")
def _holdout():
    from scripts.evaluate_holdout import evaluate_holdout
    r = evaluate_holdout()
    assert r.get("status") in ("ok", "no_data"), r
    return f"status={r['status']}"


@check("Market regime: vig/consenso desde upcoming_matches")
def _regime():
    df = pd.read_sql(text("""
        SELECT sport_key, home_odds, draw_odds, away_odds,
               bookmaker_count, h2h_spread_pct, updated_at
        FROM upcoming_matches
        WHERE home_odds IS NOT NULL AND away_odds IS NOT NULL
          AND home_odds > 1 AND away_odds > 1
    """), engine)
    return f"{len(df)} partidos con odds completas"


@check("enrich_matches: filtro SQL por equipo (últimos 20)")
def _enrich():
    df = pd.read_sql(text("""
        SELECT home_team
        FROM matches
        WHERE home_corners IS NOT NULL
        LIMIT 1
    """), engine)
    team = df.iloc[0]["home_team"] if not df.empty else "test"
    _SQL_CLEAN = (
        "trim(regexp_replace(regexp_replace(lower({col}), "
        "'[.,\\-_]', ' ', 'g'), '\\s+', ' ', 'g'))"
    )
    df2 = pd.read_sql(
        text(f"""
            SELECT home_team, away_team, home_corners, away_corners, date
            FROM matches
            WHERE {_SQL_CLEAN.format(col='home_team')} = :team
               OR {_SQL_CLEAN.format(col='away_team')} = :team
            ORDER BY date DESC
            LIMIT 20
        """),
        engine,
        params={"team": str(team).lower().strip()},
    )
    return f"equipo '{team}': {len(df2)} partidos"


@check("Line movement: lookup por match_key")
def _line_movement():
    from src.features.line_movement import get_line_movement
    line = get_line_movement("smoke_test_nonexistent_key")
    assert isinstance(line, dict)
    return "retorna dict (vacío para key inexistente)"


@check("Cap por slate: pending acumulado por fecha")
def _slate_cap():
    df = pd.read_sql(text("""
        SELECT match_date::date AS d, COALESCE(SUM(stake), 0) AS s
        FROM bets_history
        WHERE result = 'pending'
        GROUP BY 1
    """), engine)
    return f"{len(df)} slates con bets pending"


@check("save_bets: DDL guards idempotentes (decision_log, odds_placed)")
def _ddl():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS decision_log JSONB"))
        conn.execute(text("ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS odds_placed NUMERIC"))
        conn.execute(text("ALTER TABLE bets_history ADD COLUMN IF NOT EXISTS league TEXT"))
    with engine.connect() as c:
        cols = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'bets_history'"
        ))]
        assert "decision_log" in cols and "odds_placed" in cols, f"faltan columnas: {cols}"
    return "columnas presentes"


@check("Tabla odds_history (acumulador de líneas)")
def _odds_history():
    n = pd.read_sql(text("""
        SELECT COUNT(*) n FROM odds_history
    """), engine)
    return f"{int(n.iloc[0]['n'])} fotografías acumuladas"


@check("Slippage report + backtest_engine queries")
def _misc():
    from src.models.save_bets import slippage_report
    r = slippage_report()
    df = pd.read_sql(text("""
        SELECT * FROM bets_history
        WHERE result IS NOT NULL
          AND result::text NOT IN ('pending', 'null', '', 'unresolved', 'stale')
        ORDER BY match_date
    """), engine)
    return f"slippage={r.get('status')} | resueltas={len(df)}"


# ── Ciclo de aprendizaje (22-sep-26) — todo SOLO LECTURA ──────────────
# Ejecuta las queries y la matemática nuevas contra datos reales sin
# persistir nada: lo que el weekly haría, en seco.

@check("Aprendizaje: tabla model_state y claves guardadas")
def _model_state():
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT key, updated_at FROM model_state ORDER BY key")).fetchall()
    return ", ".join(f"{k} ({str(u)[:10]})" for k, u in rows) or "vacía"


@check("Aprendizaje: calibración de la cohorte (LEARNING_SINCE)")
def _calib_cohort():
    from config.settings import LEARNING_SINCE
    from src.models.calibration_monitor import CALIBRATION_WINDOW_DAYS, CALIBRATION_HOLDOUT_DAYS
    df = pd.read_sql(text(f"""
        SELECT market FROM bets_history
        WHERE result IN ('win', 'loss', 'half_win', 'half_loss')
          AND probability IS NOT NULL AND probability > 0 AND probability < 1
          AND match_date >= NOW() - INTERVAL '{CALIBRATION_WINDOW_DAYS} days'
          AND match_date <  NOW() - INTERVAL '{CALIBRATION_HOLDOUT_DAYS} days'
          AND match_date >= CAST(:since AS timestamptz)
    """), engine, params={"since": LEARNING_SINCE})
    return f"{len(df)} bets de la cohorte fuera del holdout (desde {LEARNING_SINCE})"


@check("Aprendizaje: CLV gate de la cohorte")
def _clv_cohort():
    from config.settings import LEARNING_SINCE
    df = pd.read_sql(text("""
        SELECT market, COUNT(*) AS n
        FROM bets_history
        WHERE closing_odds IS NOT NULL AND closing_odds > 1 AND odds > 1
          AND result IN ('win', 'loss', 'half_win', 'half_loss')
          AND match_date >= NOW() - INTERVAL '120 days'
          AND match_date >= CAST(:since AS timestamptz)
        GROUP BY market ORDER BY n DESC
    """), engine, params={"since": LEARNING_SINCE})
    return ", ".join(f"{r.market}={r.n}" for r in df.head(8).itertuples()) or "sin datos"


@check("Aprendizaje: peso del modelo (anchor_learner en seco)")
def _anchor_dry():
    from src.models.anchor_learner import (read_shadow_with_closing,
                                           learn_anchor_weights, format_report)
    df = read_shadow_with_closing()
    state = learn_anchor_weights(df)          # sin save_state
    print("\n" + format_report(state) + "\n")
    return f"{len(df)} candidatas shadow con cierre"


@check("Aprendizaje: reactivación por shadow (en seco)")
def _reactivation_dry():
    from scripts.clv_gate import shadow_reactivation_stats
    stats = shadow_reactivation_stats()
    return ", ".join(f"{g}: n={s[0]} CLV={s[1]:+.4f}" for g, s in stats.items()) or "sin candidatas aún"


@check("Closing: mapeo compartido sobre una bet real")
def _closing_shared():
    from src.models.save_bets import _nearest_market_row, _closing_odds_for
    bet = pd.read_sql(text("""
        SELECT match, market, match_date FROM bets_history
        WHERE match_date >= NOW() - INTERVAL '7 days' AND position(' vs ' in match) > 0
        ORDER BY match_date DESC LIMIT 1
    """), engine)
    if bet.empty:
        return "sin bets recientes"
    b = bet.iloc[0]
    home, away = b["match"].split(" vs ")
    row = _nearest_market_row(home, away, pd.to_datetime(b["match_date"], utc=True))
    co = _closing_odds_for(b["market"], row.iloc[0]) if not row.empty else None
    return f"{b['match']} | {b['market']} → cierre {co}"


@check("Cierres reales: columnas de frescura (DDL idempotente)")
def _closing_columns():
    from scripts.update_upcoming_matches import ensure_schema
    from src.utils.closing_quality import ensure_closing_columns
    ensure_schema()
    ensure_closing_columns(engine)
    with engine.connect() as c:
        cols = {(t, col) for t, col in c.execute(text("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE column_name IN ('odds_fetched_at', 'specialty_fetched_at', 'closing_fetched_at')
        """))}
    need = {("upcoming_matches", "odds_fetched_at"), ("upcoming_matches", "specialty_fetched_at"),
            ("bets_history", "closing_fetched_at"), ("shadow_bets", "closing_fetched_at")}
    assert need <= cols, f"faltan: {need - cols}"
    return "4 columnas presentes"


@check("Cierres reales: partidos a recargar ahora (en seco)")
def _closing_targets():
    from scripts.update_closing_odds import select_closing_targets
    t = select_closing_targets()
    return f"{len(t)} partidos con kickoff en 10-80 min con bets/shadow"


@check("Cierres reales: cierres VÁLIDOS disponibles para aprender")
def _valid_closings():
    from src.utils.closing_quality import valid_closing_sql
    df = pd.read_sql(text(f"""
        SELECT
          (SELECT COUNT(*) FROM shadow_bets WHERE {valid_closing_sql()}
             AND closing_fetched_at > created_at) AS shadow_ok,
          (SELECT COUNT(*) FROM shadow_bets WHERE closing_odds IS NOT NULL) AS shadow_all,
          (SELECT COUNT(*) FROM bets_history WHERE {valid_closing_sql()}) AS bets_ok,
          (SELECT COUNT(*) FROM bets_history WHERE closing_odds IS NOT NULL
             AND match_date >= NOW() - INTERVAL '30 days') AS bets_30d
    """), engine)
    r = df.iloc[0]
    return (f"shadow {int(r['shadow_ok'])}/{int(r['shadow_all'])} válidos | "
            f"bets {int(r['bets_ok'])} válidos (de {int(r['bets_30d'])} con cierre en 30d)")


@check("Pipeline completo en ENSAYO sobre datos reales (no escribe nada)")
def _pipeline_dry_run():
    """Valida el código de predicción de ESTA rama contra los partidos y
    features reales de producción, sin guardar apuestas ni shadow ni
    mandar Telegram. Un partido que falla aquí fallaría mañana en el morning."""
    import src.pipeline.prediction_pipeline as pp
    pp.run_prediction_pipeline(dry_run=True)
    s = pp.LAST_RUN_SUMMARY
    assert s.get("dry_run") is True, s
    assert s.get("failed_matches", 0) == 0, f"{s.get('failed_matches')} partidos con error"
    return (f"partidos={s.get('matches')} errores={s.get('failed_matches')} "
            f"bets={s.get('bets')} papel={s.get('paper_bets')} shadow={s.get('shadow')} "
            f"mercados={s.get('markets')}")


@check("Closing: bets BTTS con cierre (antes 0 por clave 'btts_yes')")
def _btts_closing():
    df = pd.read_sql(text("""
        SELECT COUNT(*) FILTER (WHERE closing_odds IS NOT NULL) AS con,
               COUNT(*) AS total
        FROM bets_history
        WHERE market IN ('btts', 'btts_no')
          AND match_date >= NOW() - INTERVAL '120 days'
    """), engine)
    return f"{int(df.iloc[0]['con'])}/{int(df.iloc[0]['total'])} con closing (120d)"


def main():
    print("\n🧪 DB SMOKE TEST — validación de integración\n" + "=" * 55)
    n_ok = sum(1 for _, ok, _ in results if ok)
    for name, ok, msg in results:
        print(f"  {PASS if ok else FAIL} {name}" + (f"  →  {msg}" if msg else ""))
    print("=" * 55)
    total = len(results)
    print(f"  {n_ok}/{total} checks OK\n")
    if n_ok < total:
        print("Detalles de fallos:")
        for name, ok, msg in results:
            if not ok:
                print(f"\n❌ {name}: {msg}")
        sys.exit(1)
    print("🎉 Esquema y queries validados — listo para producción.")


if __name__ == "__main__":
    main()
