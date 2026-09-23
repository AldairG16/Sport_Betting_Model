"""
scripts/fix_stat_settlements.py
===============================
Corrige las bets de córners, tarjetas y tiros que se liquidaron con un
resultado distinto al que dan los datos actuales de `matches`.

Origen (22-sep-26): un NULL leído con pandas llega como NaN y el resolver
solo miraba `is not None` → las apuestas cuyos datos aún no habían llegado
se liquidaban "loss" (over y under a la vez). El resolver ya está
corregido (save_bets._has); esto repara el historial.

Qué hace, por cada bet que save_bets.audit_stat_settlements() marca como
"resultado distinto hoy":
  - UPDATE de result/profit, con guarda `result = <el registrado>` (si
    otra corrida ya la tocó, se omite; re-ejecutar no hace nada);
  - ajuste del bankroll por la DIFERENCIA de profit, en la misma
    transacción (bankroll_history: event 'bet_result', con nota).
Las liquidadas SIN datos (hoy seguirían sin datos) solo se reportan.

Uso:
    python scripts/fix_stat_settlements.py            # en seco: solo muestra
    python scripts/fix_stat_settlements.py --apply    # corrige
"""

import argparse
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from config.database import engine
from src.models.bankroll_manager import update_bankroll, ensure_bankroll_schema
from src.models.save_bets import audit_stat_settlements

NOTE = "Corrección de liquidación (bug NaN, 22-sep-26)"


def apply_corrections(corrections: list[dict]) -> list[dict]:
    """Aplica las correcciones; devuelve las que se escribieron de verdad."""
    ensure_bankroll_schema()
    done = []
    with engine.begin() as conn:
        for c in corrections:
            with conn.begin_nested():
                r = conn.execute(text("""
                    UPDATE bets_history
                    SET result = :new_result, profit = :new_profit
                    WHERE id = :id AND result = :old_result
                """), {"id": c["id"], "new_result": c["new_result"],
                       "new_profit": c["new_profit"], "old_result": c["old_result"]})
                if r.rowcount != 1:
                    print(f"   ↷ #{c['id']} ya no está como '{c['old_result']}' — se omite")
                    continue
                balance = update_bankroll(
                    profit=c["new_profit"] - c["old_profit"],
                    notes=(f"{NOTE}: {c['match']} | {c['market']} | "
                           f"{c['old_result']} → {c['new_result']}"),
                    conn=conn)
            done.append({**c, "balance": balance})
    return done


def main(apply: bool) -> int:
    a = audit_stat_settlements()
    print(f"Revisadas {a['checked']} bets de córners/tarjetas/tiros · "
          f"sin datos: {a['no_data']} · resultado distinto hoy: {a['different']} · "
          f"sin partido para verificar: {a['unverifiable']}")
    for line in a["details"]:
        print("  " + line)
    fixes = a["corrections"]
    if not fixes:
        print("Nada que corregir.")
        return 0
    delta = sum(c["new_profit"] - c["old_profit"] for c in fixes)
    print(f"\n{len(fixes)} correcciones · ajuste total del bankroll {delta:+.2f}u")
    if not apply:
        print("EN SECO: no se escribió nada. Repetir con --apply para corregir.")
        return 0
    done = apply_corrections(fixes)
    for c in done:
        print(f"   ✅ #{c['id']} {c['match']} | {c['market']}: {c['old_result']} "
              f"{c['old_profit']:+.2f}u → {c['new_result']} {c['new_profit']:+.2f}u "
              f"(bankroll {c['balance']:.2f}u)")
    print(f"Corregidas {len(done)}/{len(fixes)}.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="escribe las correcciones")
    sys.exit(main(ap.parse_args().apply))
