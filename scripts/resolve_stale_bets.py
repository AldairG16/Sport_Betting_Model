"""
scripts/resolve_stale_bets.py
=============================
Recupera bets marcadas 'stale' cuyo partido YA tiene resultados en la tabla
matches (cargados después por el weekly desde fuentes históricas gratuitas).

Flujo:
  1. Lee las bets 'stale'
  2. Para cada una, busca el partido en matches (equipos lower + fecha ±1 día)
     y valida que existan los campos que su mercado necesita
  3. Las recuperables vuelven a 'pending' → update_bet_results() las resuelve
     con la MISMA lógica probada de producción
  4. Las que siguen sin datos fuente quedan en 'stale' (honesto: sin datos
     no se inventa resultado)

Costo: 0 créditos de API — todo es lectura local de la DB.
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.models.save_bets import _market_required_match_fields
from src.utils.team_normalizer import normalize_team


def resolve_stale_bets(apply: bool = True, verbose: bool = True) -> dict:
    bets = pd.read_sql(text("""
        SELECT id, match, market, match_date, league
        FROM bets_history
        WHERE result = 'stale'
        ORDER BY match_date
    """), engine)

    if bets.empty:
        if verbose:
            print("✅ No hay bets stale")
        return {"total": 0}

    _min = (bets["match_date"].min() - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    _max = (bets["match_date"].max() + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    matches = pd.read_sql(text("""
        SELECT LOWER(home_team) AS home_l, LOWER(away_team) AS away_l,
               LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(home_team,'[.,\\-_]',' ','g'),'\\s+',' ','g'))) AS home_c,
               LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(away_team,'[.,\\-_]',' ','g'),'\\s+',' ','g'))) AS away_c,
               date, home_goals, away_goals,
               home_corners, away_corners,
               home_yellow, away_yellow,
               home_goals_ht, away_goals_ht,
               home_goals_h2, away_goals_h2,
               home_shots_target, away_shots_target
        FROM matches
        WHERE date BETWEEN :d_from AND :d_to
    """), engine, params={"d_from": _min, "d_to": _max})

    recovered, still_stale = [], []
    for _, b in bets.iterrows():
        try:
            home_raw, away_raw = str(b["match"]).split(" vs ")
        except ValueError:
            still_stale.append((b["match"], b["market"], "nombre inválido"))
            continue

        # El nombre en bets viene normalizado por el pipeline; matches puede
        # tener el nombre crudo → normalizamos ambos lados en Python.
        md = pd.to_datetime(b["match_date"])
        cands = matches[
            matches.apply(lambda r: (
                (r["home_l"] == home_raw.lower().strip() or r["home_c"] == normalize_team(home_raw)) and
                (r["away_l"] == away_raw.lower().strip() or r["away_c"] == normalize_team(away_raw))
            ), axis=1)
        ]
        if cands.empty:
            still_stale.append((b["match"], b["market"], "partido no está en matches"))
            continue
        cands = cands.assign(_d=(pd.to_datetime(cands["date"]) - md).abs())
        row = cands.sort_values("_d").iloc[0]
        if row["_d"] > pd.Timedelta(days=2):
            still_stale.append((b["match"], b["market"], "fecha no coincide"))
            continue

        required = _market_required_match_fields(str(b["market"]))
        missing = [f for f in required if pd.isna(row.get(f))]
        if missing:
            still_stale.append((b["match"], b["market"], f"faltan campos: {','.join(missing)}"))
            continue

        recovered.append(int(b["id"]))

    if verbose:
        print(f"🗑️  STALE RECOVERY — {len(bets)} bets analizadas")
        print(f"   ♻️  Recuperables (datos ya disponibles): {len(recovered)}")
        print(f"   ❌ Sin datos fuente aún:                {len(still_stale)}")
        if still_stale:
            reasons: dict = {}
            for m, mkt, why in still_stale:
                reasons[why] = reasons.get(why, 0) + 1
            for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
                print(f"      · {why}: {n}")

    if apply and recovered:
        with engine.begin() as conn:
            r = conn.execute(text("""
                UPDATE bets_history
                SET result = 'pending', profit = 0.0
                WHERE id = ANY(:ids)
            """), {"ids": recovered})
            print(f"   → {r.rowcount} bets reactivadas a 'pending'")
        # Resolver YA con la lógica estándar de producción
        from src.models.save_bets import update_bet_results
        update_bet_results()

    return {"total": len(bets), "recovered": len(recovered), "still_stale": len(still_stale)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Solo analizar, no cambiar nada")
    args = parser.parse_args()
    resolve_stale_bets(apply=not args.dry_run)
