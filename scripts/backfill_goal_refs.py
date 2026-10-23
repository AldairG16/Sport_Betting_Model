"""
scripts/backfill_goal_refs.py
=============================
Rellena una vez (23-oct-26) el mercado de goles y ambos anotan
(model_vs_sharp.mkt_over25 / mkt_btts) de los partidos que el pipeline
analizó antes de que existieran esas columnas, para que el sesgo de goles
(src/models/side_bias.py) se aprenda desde el modelo del 22-oct y no espere
a juntar partidos nuevos.

La referencia es la de siempre (side_bias.market_goal_refs) sacada de la foto
de momios de odds_history más reciente ANTES de que el programa analizara el
partido (la que vio); si no hay, la última antes del inicio. Solo llena
columnas vacías: no pisa nada.

    python scripts/backfill_goal_refs.py            # en seco: cuántos llenaría
    python scripts/backfill_goal_refs.py --apply
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from src.utils.log import get_logger

log = get_logger(__name__)


def plan(mvs: pd.DataFrame, snaps: pd.DataFrame, normalize=None) -> list:
    """Función pura: [{match, match_date, mkt_over25, mkt_btts}] de las filas
    sin referencia que tienen foto de momios."""
    from src.models.program_picks import _snap_before, _snap_index, match_key
    from src.models.side_bias import market_goal_refs
    idx = _snap_index(snaps, normalize)
    out = []
    for r in (mvs.to_dict("records") if not mvs.empty else []):
        h, _, a = str(r["match"]).partition(" vs ")
        key, kick = match_key(h, a, normalize), r["match_date"]
        snap = (_snap_before(idx, key, kick, r["created_at"]) if r.get("created_at") is not None else None) \
            or _snap_before(idx, key, kick, kick)
        refs = market_goal_refs(snap) if snap else {}
        if refs.get("over25") is None and refs.get("btts") is None:
            continue
        out.append({"match": r["match"], "match_date": r["match_date"],
                    "mkt_over25": refs.get("over25"), "mkt_btts": refs.get("btts")})
    return out


def main(apply: bool = False) -> int:
    from config.database import engine
    from config.settings import MODEL_SINCE
    from src.models.program_picks import _SNAPS_SQL
    from src.models.side_bias import ALTER_SQL, TABLE_SQL
    from src.utils.team_normalizer import normalize_team
    with engine.begin() as conn:
        conn.execute(text(TABLE_SQL))
        conn.execute(text(ALTER_SQL))
    mvs = pd.read_sql(text("""
        SELECT match, match_date, created_at FROM model_vs_sharp
        WHERE mkt_over25 IS NULL AND mkt_btts IS NULL AND match_date >= CAST(:since AS timestamptz)
    """), engine, params={"since": MODEL_SINCE})
    if mvs.empty:
        print("Nada que llenar.")
        return 0
    a = pd.Timestamp(mvs["match_date"].min()) - pd.Timedelta(days=10)
    b = pd.Timestamp(mvs["match_date"].max()) + pd.Timedelta(hours=3)
    snaps = pd.read_sql(text(_SNAPS_SQL), engine, params={"a": a.to_pydatetime(), "b": b.to_pydatetime()})
    rows = plan(mvs, snaps, normalize_team)
    print(f"{len(mvs)} partidos sin referencia de goles; con foto de momios: {len(rows)}")
    if not apply or not rows:
        print("En seco: no se escribió nada (usa --apply)." if not apply else "")
        return len(rows)
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE model_vs_sharp SET mkt_over25 = :mkt_over25, mkt_btts = :mkt_btts
            WHERE match = :match AND match_date = :match_date AND mkt_over25 IS NULL AND mkt_btts IS NULL
        """), rows)
    print(f"✅ {len(rows)} partidos con su referencia de goles y ambos anotan")
    return len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)
