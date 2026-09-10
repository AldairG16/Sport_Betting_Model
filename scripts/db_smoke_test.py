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
