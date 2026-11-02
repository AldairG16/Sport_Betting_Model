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
     no se inventa resultado); si el partido se jugó días después (pospuesto),
     se dice así: la apuesta queda anulada

Las CANCELADAS antes del kickoff también son 'stale' y no se tocan: no se
apostaron (src/utils/bet_status.py).

Costo: 0 créditos de API — todo es lectura local de la DB.
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine
from src.models.save_bets import _market_required_match_fields
from src.utils.bet_status import CANCELLED_SQL
from src.utils.team_normalizer import normalize_team


POSTPONED_MAX_DAYS = 180        # hasta dónde se busca la fecha nueva de un partido pospuesto


def same_pair(matches: pd.DataFrame, home_raw: str, away_raw: str) -> pd.DataFrame:
    """Función pura. Las filas de matches del mismo local y visitante. El
    nombre en bets viene normalizado por el pipeline; matches puede tener el
    nombre crudo → se comparan los dos lados en Python."""
    if matches.empty:
        return matches
    h, a = home_raw.lower().strip(), away_raw.lower().strip()
    return matches[matches.apply(lambda r: (
        (r["home_l"] == h or r["home_c"] == normalize_team(home_raw)) and
        (r["away_l"] == a or r["away_c"] == normalize_team(away_raw))
    ), axis=1)]


def _played_later(home_raw: str, away_raw: str, md: pd.Timestamp):
    """La fecha en que se jugó un partido pospuesto (más de 2 días después),
    o None. 2-nov-26: Mjällby vs Djurgården (31-may) se jugó el 3-sep y el
    weekly lo reportaba cada lunes como "partido no está en matches"; la
    apuesta queda anulada (stale, profit 0) como en la casa."""
    later = pd.read_sql(text("""
        SELECT LOWER(home_team) AS home_l, LOWER(away_team) AS away_l,
               LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(home_team,'[.,\\-_]',' ','g'),'\\s+',' ','g'))) AS home_c,
               LOWER(TRIM(REGEXP_REPLACE(REGEXP_REPLACE(away_team,'[.,\\-_]',' ','g'),'\\s+',' ','g'))) AS away_c,
               date
        FROM matches
        WHERE date > :d_from AND date <= :d_to AND home_goals IS NOT NULL
        ORDER BY date
    """), engine, params={"d_from": (md + pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
                          "d_to": (md + pd.Timedelta(days=POSTPONED_MAX_DAYS)).strftime("%Y-%m-%d")})
    rows = same_pair(later, home_raw, away_raw)
    return None if rows.empty else pd.Timestamp(rows.iloc[0]["date"])


def resolve_stale_bets(apply: bool = True, verbose: bool = True) -> dict:
    bets = pd.read_sql(text(f"""
        SELECT id, match, market, match_date, league
        FROM bets_history
        WHERE result = 'stale'
          AND NOT {CANCELLED_SQL}
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
               home_yellow, away_yellow, home_red, away_red,
               home_goals_ht, away_goals_ht,
               home_goals_h2, away_goals_h2,
               home_shots_target, away_shots_target
        FROM matches
        WHERE date BETWEEN :d_from AND :d_to
    """), engine, params={"d_from": _min, "d_to": _max})

    recovered, still_stale, postponed = [], [], []
    for _, b in bets.iterrows():
        try:
            home_raw, away_raw = str(b["match"]).split(" vs ")
        except ValueError:
            still_stale.append((b["match"], b["market"], "nombre inválido"))
            continue

        md = pd.to_datetime(b["match_date"])
        cands = same_pair(matches, home_raw, away_raw)
        if cands.empty:
            later = _played_later(home_raw, away_raw, md)
            if later is not None:
                postponed.append((b["match"], b["market"], later))
            else:
                still_stale.append((b["match"], b["market"], "partido no está en matches"))
            continue
        cands = cands.assign(_d=(pd.to_datetime(cands["date"]) - md).abs())
        row = cands.sort_values("_d").iloc[0]
        if row["_d"] > pd.Timedelta(days=2):
            later = _played_later(home_raw, away_raw, md)
            if later is not None:
                postponed.append((b["match"], b["market"], later))
            else:
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
        if postponed:
            print(f"   🗓️  Pospuestos (la apuesta queda anulada): {len(postponed)} — "
                  + "; ".join(f"{m} se jugó el {d:%d-%b}" for m, _, d in postponed[:3]))

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

    return {"total": len(bets), "recovered": len(recovered), "still_stale": len(still_stale),
            "postponed": len(postponed)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Solo analizar, no cambiar nada")
    args = parser.parse_args()
    resolve_stale_bets(apply=not args.dry_run)
