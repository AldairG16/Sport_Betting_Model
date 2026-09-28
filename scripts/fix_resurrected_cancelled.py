"""
scripts/fix_resurrected_cancelled.py
====================================
Repara las bets CANCELADAS antes del kickoff que la recuperación de 'stale'
(resolve_stale_bets / backfill_espn_results) volvió a 'pending' y liquidó.
No se apostaron: vuelven a 'stale' con profit 0 y el bankroll recupera ese
profit, en la misma transacción (bankroll_history con nota), igual que
scripts/fix_stat_settlements.py. Desde el 25-sep-26 esos scripts ya dejan
fuera las canceladas (src/utils/bet_status.py); esto repara lo anterior.

Encontrada el 28-sep-26: real betis vs getafe | over25, cancelada el 16-sep
(la línea se movió) y liquidada como pérdida el 18-sep (−0.99u al bankroll).

Qué hace, por cada bet con marca de cancelación y resultado final:
  - UPDATE a 'stale' / profit 0 con guarda `result = <el registrado>`
    (re-ejecutar no hace nada) y la corrección anotada en decision_log;
  - ajuste del bankroll por −profit en la misma transacción.

Uso:
    python scripts/fix_resurrected_cancelled.py            # en seco: solo muestra
    python scripts/fix_resurrected_cancelled.py --apply    # corrige
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text

from config.database import engine
from src.models.bankroll_manager import ensure_bankroll_schema, update_bankroll
from src.utils.bet_status import CANCELLED_SQL, RESOLVED_SQL

NOTE = "Corrección (28-sep-26): cancelada antes del partido y liquidada por error"


def find_resurrected() -> list[dict]:
    """Solo lectura: canceladas antes del kickoff que hoy figuran liquidadas."""
    df = pd.read_sql(text(f"""
        SELECT id, match, market, match_date, result, profit
        FROM bets_history
        WHERE {CANCELLED_SQL} AND {RESOLVED_SQL}
        ORDER BY match_date
    """), engine)
    return df.to_dict("records")


def apply_fixes(rows: list[dict]) -> list[dict]:
    """Aplica las correcciones; devuelve las que se escribieron de verdad."""
    ensure_bankroll_schema()
    done = []
    with engine.begin() as conn:
        for b in rows:
            note = json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                               "was": {"result": b["result"], "profit": float(b["profit"] or 0)},
                               "why": NOTE})
            with conn.begin_nested():
                r = conn.execute(text("""
                    UPDATE bets_history
                    SET result = 'stale', profit = 0.0,
                        decision_log = COALESCE(decision_log, '{}'::jsonb)
                                      || jsonb_build_object('correction', CAST(:note AS jsonb))
                    WHERE id = :id AND result = :old_result
                """), {"id": int(b["id"]), "old_result": b["result"], "note": note})
                if r.rowcount != 1:
                    print(f"   ↷ #{b['id']} ya no está como '{b['result']}' — se omite")
                    continue
                balance = update_bankroll(
                    profit=-float(b["profit"] or 0),
                    notes=f"{NOTE}: {b['match']} | {b['market']} | {b['result']} → cancelada",
                    conn=conn)
            done.append({**b, "balance": balance})
    return done


def main(apply: bool) -> int:
    rows = find_resurrected()
    if not rows:
        print("Nada que corregir: ninguna cancelada figura liquidada.")
        return 0
    delta = -sum(float(b["profit"] or 0) for b in rows)
    for b in rows:
        print(f"  #{b['id']} {b['match']} | {b['market']} ({str(b['match_date'])[:10]}): "
              f"{b['result']} {float(b['profit'] or 0):+.2f}u → cancelada 0.00u")
    print(f"\n{len(rows)} correcciones · ajuste total del bankroll {delta:+.2f}u")
    if not apply:
        print("EN SECO: no se escribió nada. Repetir con --apply para corregir.")
        return 0
    done = apply_fixes(rows)
    for b in done:
        print(f"   ✅ #{b['id']} corregida (bankroll {b['balance']:.2f}u)")
    print(f"Corregidas {len(done)}/{len(rows)}.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe las correcciones")
    sys.exit(main(ap.parse_args().apply))
