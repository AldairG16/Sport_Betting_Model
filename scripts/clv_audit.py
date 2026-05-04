"""
scripts/clv_audit.py
====================
Auditoría semanal de CLV (Closing Line Value).

Si CLV+ tiene WR <40% y P/L negativo durante 30+ días → ALERTA Telegram.
Eso indica que el modelo es ANTI-PREDICTIVO vs línea de cierre — señal grave
de overfitting o de que la línea se mueve en respuesta a las MISMAS señales
que el modelo usa (sin ventaja real).

Ejecutar semanalmente desde el workflow weekly.yml.
"""

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

from config.database import engine


# Umbrales de alarma
WR_FLOOR = 0.40        # CLV+ con <40% WR = anti-predictivo
MIN_SAMPLE = 30        # mínimo 30 bets resueltas para opinar
LOOKBACK_DAYS = 30


def audit_clv() -> dict:
    df = pd.read_sql(text(f"""
        SELECT id, match, market, odds, closing_odds, clv, result,
               CASE WHEN result = 'win'  THEN (odds - 1) * stake
                    WHEN result = 'loss' THEN -stake
                    ELSE 0 END AS pnl
        FROM bets_history
        WHERE clv IS NOT NULL
          AND result IN ('win', 'loss')
          AND match_date >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
    """), engine)

    if df.empty or len(df) < MIN_SAMPLE:
        return {"status": "insufficient_sample", "n": len(df)}

    pos = df[df["clv"] > 0]
    neg = df[df["clv"] < 0]

    pos_wr = (pos["result"] == "win").mean() if len(pos) else 0.0
    neg_wr = (neg["result"] == "win").mean() if len(neg) else 0.0
    pos_pnl = pos["pnl"].sum()
    neg_pnl = neg["pnl"].sum()

    inverted = (
        len(pos) >= MIN_SAMPLE
        and pos_wr < WR_FLOOR
        and pos_pnl < 0
    )

    return {
        "status": "inverted" if inverted else "ok",
        "n_total": len(df),
        "n_pos": len(pos),
        "n_neg": len(neg),
        "pos_wr": float(pos_wr),
        "neg_wr": float(neg_wr),
        "pos_pnl": float(pos_pnl),
        "neg_pnl": float(neg_pnl),
        "lookback_days": LOOKBACK_DAYS,
    }


def main():
    r = audit_clv()
    print("\n📊 CLV CALIBRATION AUDIT")
    print("=" * 50)
    print(f"Lookback: {r.get('lookback_days', '?')}d  |  Status: {r['status']}")

    if r["status"] == "insufficient_sample":
        print(f"⚠️  Muestra insuficiente ({r['n']} < {MIN_SAMPLE}). Skip.")
        return

    print(f"Total resueltas: {r['n_total']}")
    print(f"  CLV+:  n={r['n_pos']}  WR={r['pos_wr']*100:.1f}%  P/L={r['pos_pnl']:+.2f}u")
    print(f"  CLV-:  n={r['n_neg']}  WR={r['neg_wr']*100:.1f}%  P/L={r['neg_pnl']:+.2f}u")

    if r["status"] == "inverted":
        print(f"\n❌ CLV INVERTIDO — modelo anti-predictivo vs línea de cierre")
        try:
            from scripts.notify_telegram import send_message
            send_message(
                f"🚨 <b>CLV INVERTIDO ({r['lookback_days']}d)</b>\n\n"
                f"CLV+: {r['n_pos']} bets, "
                f"WR <b>{r['pos_wr']*100:.1f}%</b>, "
                f"P/L <b>{r['pos_pnl']:+.2f}u</b>\n"
                f"CLV-: {r['n_neg']} bets, "
                f"WR {r['neg_wr']*100:.1f}%, "
                f"P/L {r['neg_pnl']:+.2f}u\n\n"
                f"⚠️  El modelo concuerda con el mercado pero PIERDE.\n"
                f"Posibles causas: overfitting, closing odds capturadas "
                f"demasiado pronto, o señales públicas ya descontadas."
            )
        except Exception as e:
            print(f"⚠️  No se pudo enviar alerta Telegram: {e}")
    else:
        print(f"\n✅ Calibración CLV ok (CLV+ WR={r['pos_wr']*100:.1f}% ≥ {WR_FLOOR*100:.0f}%)")


if __name__ == "__main__":
    main()
