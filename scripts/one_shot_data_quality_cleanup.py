"""
scripts/one_shot_data_quality_cleanup.py
========================================
Limpieza de datos one-shot para restablecer calidad del dataset:

  1. Zombie bets: result='pending' con match_date pasado >6h  →  unresolved
  2. Bets viejas unresolved (>7 días)                         →  stale
  3. Bets muy viejas pending (>3 días, fallback)              →  unresolved
  4. upcoming_matches con match_date >2 días pasado           →  DELETE
  5. Backfill de closing_odds NULL para bets resueltas        →  update_closing_odds()

Diseñado para ejecutarse UNA VEZ. Idempotente (correr dos veces no hace daño).
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text
from config.database import engine


def cleanup_bets_states() -> dict:
    """Aplica transiciones de estado: pending→unresolved, unresolved→stale."""
    stats = {"zombie_to_unresolved": 0, "unresolved_to_stale": 0, "pending_old_to_unresolved": 0}
    with engine.begin() as conn:
        # 1. Zombies: pending con match_date pasado >6h (partido terminado, sin resolver)
        r = conn.execute(text("""
            UPDATE bets_history
            SET result = 'unresolved'
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '6 hours'
              AND match_date >= NOW() - INTERVAL '3 days'
        """))
        stats["zombie_to_unresolved"] = r.rowcount

        # 2. Pending muy viejas (>3 días) → unresolved (alinea con el nuevo timeout)
        r = conn.execute(text("""
            UPDATE bets_history
            SET result = 'unresolved'
            WHERE result = 'pending'
              AND match_date < NOW() - INTERVAL '3 days'
        """))
        stats["pending_old_to_unresolved"] = r.rowcount

        # 3. Unresolved viejas (>7 días) → stale (terminal, no más data esperada)
        r = conn.execute(text("""
            UPDATE bets_history
            SET result = 'stale'
            WHERE result = 'unresolved'
              AND match_date < NOW() - INTERVAL '7 days'
        """))
        stats["unresolved_to_stale"] = r.rowcount
    return stats


def cleanup_upcoming_matches() -> int:
    """Elimina upcoming_matches con match_date >2 días en el pasado."""
    with engine.begin() as conn:
        r = conn.execute(text("""
            DELETE FROM upcoming_matches
            WHERE match_date::timestamptz < NOW() - INTERVAL '2 days'
        """))
        return r.rowcount


def backfill_closing_odds() -> int:
    """Rellena closing_odds NULL usando odds actuales de upcoming_matches."""
    from src.models.save_bets import update_closing_odds
    # update_closing_odds ya itera sobre bets con closing_odds IS NULL
    # y las actualiza con odds de upcoming_matches (match_date ±4 hours).
    # No requiere ningún parámetro; imprime su propio resumen.
    print("\n📡 Backfill de closing_odds NULL...")
    update_closing_odds()
    return 0  # el conteo lo imprime update_closing_odds()


def main():
    print("=" * 60)
    print(f"ONE-SHOT DATA QUALITY CLEANUP — {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 60)

    # Resumen previo
    with engine.begin() as conn:
        pre = conn.execute(text("""
            SELECT
              COUNT(*) FILTER (WHERE result='pending')     AS pending,
              COUNT(*) FILTER (WHERE result='unresolved')  AS unresolved,
              COUNT(*) FILTER (WHERE result='stale')       AS stale,
              COUNT(*) FILTER (WHERE result IN ('win','loss','push') AND closing_odds IS NULL) AS null_close,
              (SELECT COUNT(*) FROM upcoming_matches WHERE match_date::timestamptz < NOW() - INTERVAL '2 days') AS stale_upcoming
            FROM bets_history
        """)).fetchone()
        print(f"\nEstado ANTES:")
        print(f"  bets pending:         {pre[0]}")
        print(f"  bets unresolved:      {pre[1]}")
        print(f"  bets stale:           {pre[2]}")
        print(f"  bets closing_odds=NULL: {pre[3]}")
        print(f"  upcoming stale:       {pre[4]}")

    # Ejecutar cleanup
    print("\n🧹 Ejecutando transiciones de estado...")
    s = cleanup_bets_states()
    print(f"  ✔ zombie pending → unresolved:    {s['zombie_to_unresolved']}")
    print(f"  ✔ pending viejas → unresolved:    {s['pending_old_to_unresolved']}")
    print(f"  ✔ unresolved viejas → stale:      {s['unresolved_to_stale']}")

    print("\n🗑️  Purgando upcoming_matches stale...")
    n = cleanup_upcoming_matches()
    print(f"  ✔ upcoming_matches eliminados: {n}")

    print("\n💰 Backfill de closing_odds...")
    try:
        backfill_closing_odds()
    except Exception as e:
        print(f"  ⚠️ Backfill falló (continuando): {e}")

    # Resumen final
    with engine.begin() as conn:
        post = conn.execute(text("""
            SELECT
              COUNT(*) FILTER (WHERE result='pending')     AS pending,
              COUNT(*) FILTER (WHERE result='unresolved')  AS unresolved,
              COUNT(*) FILTER (WHERE result='stale')       AS stale,
              COUNT(*) FILTER (WHERE result IN ('win','loss','push') AND closing_odds IS NULL) AS null_close,
              (SELECT COUNT(*) FROM upcoming_matches WHERE match_date::timestamptz < NOW() - INTERVAL '2 days') AS stale_upcoming
            FROM bets_history
        """)).fetchone()
        print(f"\nEstado DESPUÉS:")
        print(f"  bets pending:         {post[0]}   (Δ {post[0]-pre[0]:+d})")
        print(f"  bets unresolved:      {post[1]}   (Δ {post[1]-pre[1]:+d})")
        print(f"  bets stale:           {post[2]}   (Δ {post[2]-pre[2]:+d})")
        print(f"  bets closing_odds=NULL: {post[3]}   (Δ {post[3]-pre[3]:+d})")
        print(f"  upcoming stale:       {post[4]}   (Δ {post[4]-pre[4]:+d})")

    print("\n✅ Cleanup completo")


if __name__ == "__main__":
    main()
