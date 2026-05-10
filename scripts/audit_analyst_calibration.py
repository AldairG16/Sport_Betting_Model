"""
scripts/audit_analyst_calibration.py
====================================
Auditoría manual del Pre-Kickoff Analyst.

Ejecutá esto cada 2-4 semanas (o cuando dudes del agente) para ver si
sus probabilidades estimadas coinciden con la realidad. Si un bucket
está sistemáticamente OPTIMISTA (-8 puntos o más), agregá una regla en
`data/analyst_lessons.md` y el agente la usará en la próxima corrida.

Uso:
  python scripts/audit_analyst_calibration.py            # últimos 60 días
  python scripts/audit_analyst_calibration.py --days 30  # ventana custom
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine


# Targets esperados por bucket (centro del rango)
BUCKET_TARGETS = {
    "[40-50)": 45,
    "[50-60)": 55,
    "[60-70)": 65,
    "[70-80)": 75,
    "[80-100]": 85,
}


def _bucket(prob: int) -> str:
    if prob < 50:   return "[40-50)"
    if prob < 60:   return "[50-60)"
    if prob < 70:   return "[60-70)"
    if prob < 80:   return "[70-80)"
    return "[80-100]"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60,
                    help="Ventana de días a auditar (default 60)")
    args = ap.parse_args()

    print(f"\n📊 AUDITORÍA DEL PRE-KICKOFF ANALYST — últimos {args.days} días\n")

    df = pd.read_sql(text("""
        SELECT p.match, p.market, p.verdict, p.decision, p.probability,
               p.confidence, p.reasoning, p.analyzed_at,
               b.result, b.odds
        FROM pre_kickoff_analyses p
        JOIN bets_history b
          ON b.match      = p.match
         AND b.market     = p.market
         AND b.match_date = p.match_date
        WHERE p.analyzed_at >= NOW() - (:days || ' days')::interval
          AND b.result IN ('win', 'loss')
          AND p.probability IS NOT NULL
        ORDER BY p.analyzed_at DESC
    """), engine, params={"days": args.days})

    if df.empty:
        print("⚠️  Sin datos. Posibles causas:")
        print("   • Tabla pre_kickoff_analyses vacía (agente recién instalado)")
        print("   • No hay bets resueltas con análisis del agente todavía")
        print("   • Probability quedó NULL (chequeá la migración ALTER TABLE)\n")
        return

    print(f"Total de análisis con resultado conocido: {len(df)}\n")

    # ── 1) Hit rate global por decision ──────────────────────────────
    print("=" * 60)
    print("HIT RATE POR DECISIÓN")
    print("=" * 60)
    for dec, sub in df.groupby("decision"):
        won = (sub["result"] == "win").sum()
        n = len(sub)
        wr = won / n * 100 if n else 0
        print(f"  {dec:15s}: {won:3d}/{n:3d} = {wr:5.1f}%")
    print()

    # ── 2) Calibración por bucket de probabilidad (solo APUESTA) ─────
    apuestas = df[df["decision"].str.upper() == "APUESTA"].copy()
    if not apuestas.empty:
        print("=" * 60)
        print("CALIBRACIÓN POR BUCKET (solo APUESTA — n debe ser >=5)")
        print("=" * 60)
        print(f"  {'Bucket':10s} {'n':>4s} {'won':>4s} {'hit%':>6s} "
              f"{'target%':>8s} {'diff':>6s}  {'estado':12s}")
        print("  " + "-" * 56)
        apuestas["bucket"] = apuestas["probability"].apply(_bucket)
        for bucket in ["[40-50)", "[50-60)", "[60-70)", "[70-80)", "[80-100]"]:
            sub = apuestas[apuestas["bucket"] == bucket]
            n = len(sub)
            if n == 0:
                continue
            won = (sub["result"] == "win").sum()
            wr = won / n * 100
            tgt = BUCKET_TARGETS[bucket]
            diff = wr - tgt
            if n < 5:
                estado = "muestra chica"
            elif abs(diff) < 8:
                estado = "OK"
            elif diff < -8:
                estado = "🔴 OPTIMISTA"
            else:
                estado = "🟢 conservador"
            print(f"  {bucket:10s} {n:4d} {won:4d} {wr:5.1f}% "
                  f"{tgt:7d}% {diff:+5.1f}  {estado}")
        print()

    # ── 3) Hit rate por verdict ──────────────────────────────────────
    print("=" * 60)
    print("HIT RATE POR VERDICT")
    print("=" * 60)
    for v, sub in df.groupby("verdict"):
        won = (sub["result"] == "win").sum()
        n = len(sub)
        wr = won / n * 100 if n else 0
        print(f"  {v:10s}: {won:3d}/{n:3d} = {wr:5.1f}%")
    print("  Esperado: STRONG > MEDIUM > SKIP en hit rate.\n")

    # ── 4) Hit rate por mercado (top 6) ─────────────────────────────
    print("=" * 60)
    print("HIT RATE POR MERCADO (mín 5 bets)")
    print("=" * 60)
    by_mkt = (apuestas.groupby("market")
              .agg(n=("result", "size"),
                   won=("result", lambda s: (s == "win").sum()))
              .reset_index())
    by_mkt = by_mkt[by_mkt["n"] >= 5]
    if not by_mkt.empty:
        by_mkt["hit_rate"] = by_mkt["won"] / by_mkt["n"] * 100
        by_mkt = by_mkt.sort_values("hit_rate", ascending=False)
        for _, r in by_mkt.iterrows():
            tag = "" if r["hit_rate"] >= 50 else "  ← REVISAR"
            print(f"  {r['market']:20s}: {int(r['won']):3d}/{int(r['n']):3d} "
                  f"= {r['hit_rate']:5.1f}%{tag}")
    else:
        print("  (sin mercados con >=5 bets aún)")
    print()

    # ── 5) Peores fallos (apuesta con prob >= 65 que perdieron) ──────
    bad = apuestas[(apuestas["probability"] >= 65) &
                   (apuestas["result"] == "loss")].head(5)
    if not bad.empty:
        print("=" * 60)
        print(f"PEORES FALLOS — apostaste con prob >=65 y perdiste ({len(bad)})")
        print("=" * 60)
        for _, r in bad.iterrows():
            reason = (r["reasoning"] or "").strip().replace("\n", " ")
            if len(reason) > 80:
                reason = reason[:77] + "..."
            print(f"  • {r['match']} ({r['market']}) prob={r['probability']}% "
                  f"@{r['odds']:.2f}")
            print(f"    Tu razón: \"{reason}\"")
        print()

    print("✓ Auditoría completa.")
    print("  Si ves un bucket 🔴 OPTIMISTA, editá data/analyst_lessons.md")
    print("  con una regla del tipo: 'En bucket [60-70), bajá probability 5 puntos'.\n")


if __name__ == "__main__":
    main()
