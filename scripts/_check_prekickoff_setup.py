"""
Script de validacion one-shot:
1. Existe la tabla pre_kickoff_analyses?
2. Cuantas bets pending hay con kickoff futuro?
3. Lista de proximos partidos con hora local Mexico
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from sqlalchemy import text
from config.database import engine

print("=" * 70)
print("VALIDACION PRE-KICKOFF ANALYST SETUP")
print("=" * 70)

# 1) Existe la tabla?
with engine.begin() as conn:
    exists = conn.execute(text("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'pre_kickoff_analyses'
        )
    """)).scalar()

status = "[OK] EXISTE" if exists else "[X] NO EXISTE"
print(f"\n1) Tabla pre_kickoff_analyses: {status}")

if exists:
    with engine.begin() as conn:
        cols = conn.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'pre_kickoff_analyses'
            ORDER BY ordinal_position
        """)).fetchall()
    print("   Columnas:")
    for c in cols:
        print(f"     - {c[0]:<15} {c[1]}")

    with engine.begin() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM pre_kickoff_analyses")).scalar()
    print(f"   Registros actuales: {n}")
else:
    print("   -> Aun no se creo. Cora el orchestrator o evening pipeline una vez.")

# 2) Bets pending con kickoff futuro
print("\n" + "=" * 70)
print("2) BETS PENDING CON KICKOFF FUTURO")
print("=" * 70)

with engine.begin() as conn:
    rows = conn.execute(text("""
        SELECT match,
               match_date,
               match_date AT TIME ZONE 'UTC' AT TIME ZONE 'America/Mexico_City' AS kickoff_mx,
               market,
               odds,
               edge,
               probability
        FROM bets_history
        WHERE result = 'pending'
          AND match_date > NOW()
        ORDER BY match_date
        LIMIT 30
    """)).fetchall()

if not rows:
    print("\n   [!] No hay bets pending con kickoff futuro.")
    print("   -> El pipeline matutino aun no corrio hoy o no se generaron picks.")
else:
    print(f"\n   Total: {len(rows)} bets pending\n")
    print(f"   {'Match':<45} {'Kickoff (MX)':<20} {'Market':<10} {'Odds':<6} {'Edge':<7}")
    print(f"   {'-'*45} {'-'*20} {'-'*10} {'-'*6} {'-'*7}")
    for r in rows:
        match = (r[0] or "")[:43]
        kickoff_mx = r[2].strftime("%Y-%m-%d %H:%M") if r[2] else "?"
        market = (r[3] or "")[:10]
        odds = f"{float(r[4]):.2f}" if r[4] else "?"
        edge = f"+{float(r[5])*100:.1f}%" if r[5] is not None else "?"
        print(f"   {match:<45} {kickoff_mx:<20} {market:<10} {odds:<6} {edge:<7}")

print("\n" + "=" * 70)
print("LISTO. Si hay bets en la lista, ~60 min antes de cada kickoff (MX)")
print("recibiras el dictamen STRONG/MEDIUM/SKIP en Telegram.")
print("=" * 70)
