import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from sqlalchemy import text
from config.database import engine


# =========================
# CLV CALC (PRO)
# =========================

def calculate_clv(open_odds, closing_odds, overround_est: float = 0.05):
    """
    Calcula Closing Line Value.

    CLV > 0: apostamos a mejor precio que el cierre → edge real
    CLV < 0: el mercado movió en nuestra contra → señal negativa

    NOTA: El ajuste de overround previo multiplicaba AMBAS probabilidades
    por el mismo factor (1 - overround/2), lo cual se cancela al restarlas:
        p_o*f - p_c*f = (p_o - p_c) * f   (solo escala, no quita overround)
    Quitar overround de verdad requiere las odds de TODOS los outcomes del
    mercado (home+draw+away en 1X2, over+under en O/U, etc.), las cuales
    no almacenamos por bet. Así que usamos CLV como diferencia directa de
    probabilidades implícitas — interpretación simple y matemáticamente
    honesta.

    Args:
        open_odds:      odds al momento de apostar
        closing_odds:   odds al cierre del mercado
        overround_est:  parámetro legacy (ignorado — se mantiene por compat)

    Returns:
        CLV como diferencia de probabilidades implícitas [float] o None
    """
    if open_odds is None or closing_odds is None:
        return None

    if open_odds <= 1 or closing_odds <= 1:
        return None

    try:
        open_prob  = 1 / open_odds
        close_prob = 1 / closing_odds

        # Positivo = apostamos ANTES de que el mercado ajustara en nuestra contra
        # (nuestro precio implicaba menor prob que el cierre → +EV histórico)
        clv = close_prob - open_prob

        return round(clv, 4)

    except Exception:
        return None


# =========================
# EXTRA METRICS (PRO)
# =========================

def clv_label(clv):

    if clv is None:
        return "unknown"

    if clv > 0.02:
        return "🔥 strong"
    elif clv > 0:
        return "✅ good"
    elif clv > -0.02:
        return "⚖️ neutral"
    else:
        return "❌ bad"


# =========================
# UPDATE CLV
# =========================

def update_clv():

    print("\n📊 UPDATING CLV (PRO)...\n")

    df = pd.read_sql("""
        SELECT id, odds, closing_odds
        FROM bets_history
        WHERE closing_odds IS NOT NULL
    """, engine)

    if df.empty:
        print("⚠️ No bets with closing odds")
        return

    df["clv"] = df.apply(
        lambda row: calculate_clv(row["odds"], row["closing_odds"]),
        axis=1
    )

    df["clv_label"] = df["clv"].apply(clv_label)

    df = df[df["clv"].notna()]

    print(f"✅ Calculated CLV for {len(df)} bets")

    # =========================
    # UPDATE DB
    # =========================

    with engine.begin() as conn:
        for _, r in df.iterrows():
            conn.execute(text("""
                UPDATE bets_history
                SET clv = :clv
                WHERE id = :id
            """), {
                "id": int(r["id"]),
                "clv": float(r["clv"])
            })

    # =========================
    # SUMMARY (🔥 CLAVE)
    # =========================

    print("\n📊 CLV SUMMARY\n")

    print("Avg CLV:", round(df["clv"].mean(), 4))
    print("Positive CLV %:", round((df["clv"] > 0).mean() * 100, 2), "%")

    print("\nDistribución:")
    print(df["clv_label"].value_counts())

    print("\n💾 CLV UPDATED")


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    update_clv()