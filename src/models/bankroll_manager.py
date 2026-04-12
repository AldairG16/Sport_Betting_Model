"""
Bankroll Manager
================
Rastreo del bankroll real para que el Kelly Criterion use
el capital actual en vez de un valor fijo.

¿Por qué es crítico?
  - Kelly con bankroll fijo ($100 siempre):
      Racha ganadora → bankroll es $150 pero stakes siguen igual → sub-apostado
      Racha perdedora → bankroll es $60 pero stakes siguen igual → riesgo de ruina

  - Kelly con bankroll real:
      Racha ganadora → stakes crecen proporcionalmente → maximiza ganancia
      Racha perdedora → stakes se reducen automáticamente → protege el capital

Estructura DB:
  - bankroll:         tabla de una sola fila con el estado actual
  - bankroll_history: log de cada movimiento (bet_result, deposit, withdrawal)

Configuración inicial:
  - Agrega INITIAL_BANKROLL=1000 en .env (o el monto que prefieras)
  - La primera vez que corra, se inicializa con ese valor
  - Si no se configura, usa 100 unidades por default
"""

import pandas as pd
from datetime import datetime
from sqlalchemy import text

from config.database import engine
from config.settings import INITIAL_BANKROLL


# ─────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────

def ensure_bankroll_schema():
    """
    Crea las tablas bankroll y bankroll_history si no existen.
    Si bankroll está vacía, la inicializa con INITIAL_BANKROLL.
    """
    with engine.begin() as conn:

        # Tabla principal (una sola fila)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bankroll (
                id                SERIAL PRIMARY KEY,
                initial_bankroll  FLOAT   NOT NULL,
                current_bankroll  FLOAT   NOT NULL,
                peak_bankroll     FLOAT   NOT NULL,
                total_deposited   FLOAT   NOT NULL,
                last_updated      TIMESTAMP DEFAULT NOW()
            )
        """))

        # Log de movimientos
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bankroll_history (
                id        SERIAL PRIMARY KEY,
                date      TIMESTAMP DEFAULT NOW(),
                event     TEXT,
                amount    FLOAT,
                balance   FLOAT,
                notes     TEXT
            )
        """))

    # Inicializar si está vacía
    count = pd.read_sql("SELECT COUNT(*) as n FROM bankroll", engine).iloc[0]["n"]
    if count == 0:
        _initialize_bankroll(INITIAL_BANKROLL)


def _initialize_bankroll(amount: float):
    """Inserta el bankroll inicial. Solo se llama una vez."""
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO bankroll
                (initial_bankroll, current_bankroll, peak_bankroll, total_deposited)
            VALUES (:amount, :amount, :amount, :amount)
        """), {"amount": float(amount)})

        conn.execute(text("""
            INSERT INTO bankroll_history (event, amount, balance, notes)
            VALUES ('deposit', :amount, :amount, 'Bankroll inicial')
        """), {"amount": float(amount)})

    print(f"💰 Bankroll inicializado: {amount:.2f} unidades")


# ─────────────────────────────────────────────────────────────
# OPERACIONES
# ─────────────────────────────────────────────────────────────

def get_current_bankroll() -> float:
    """
    Retorna el bankroll actual desde la DB.
    Fallback: INITIAL_BANKROLL si la tabla aún no existe o está vacía.
    """
    try:
        ensure_bankroll_schema()
        df = pd.read_sql(
            "SELECT current_bankroll FROM bankroll ORDER BY id LIMIT 1",
            engine
        )
        if df.empty:
            return float(INITIAL_BANKROLL)
        return float(df.iloc[0]["current_bankroll"])
    except Exception:
        return float(INITIAL_BANKROLL)


def update_bankroll(profit: float, notes: str = "") -> float:
    """
    Actualiza el bankroll sumando el profit/loss de una apuesta resuelta.

    Args:
        profit: float positivo (ganancia) o negativo (pérdida)
        notes:  descripción del movimiento (ej. "Liverpool vs Arsenal home_win")

    Returns:
        Nuevo balance del bankroll
    """
    try:
        ensure_bankroll_schema()
        current = get_current_bankroll()
        new_balance = current + profit

        with engine.begin() as conn:
            # Actualizar balance actual
            conn.execute(text("""
                UPDATE bankroll
                SET current_bankroll = :balance,
                    peak_bankroll    = GREATEST(peak_bankroll, :balance),
                    last_updated     = NOW()
            """), {"balance": float(new_balance)})

            # Registrar en historial
            conn.execute(text("""
                INSERT INTO bankroll_history (event, amount, balance, notes)
                VALUES ('bet_result', :profit, :balance, :notes)
            """), {
                "profit":  float(profit),
                "balance": float(new_balance),
                "notes":   notes or "",
            })

        return new_balance

    except Exception as e:
        print(f"⚠️ bankroll_manager.update_bankroll error: {e}")
        return float(INITIAL_BANKROLL)


def deposit(amount: float, notes: str = "Depósito manual") -> float:
    """Agrega fondos al bankroll (recarga)."""
    try:
        ensure_bankroll_schema()
        current = get_current_bankroll()
        new_balance = current + amount

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE bankroll
                SET current_bankroll = :balance,
                    peak_bankroll    = GREATEST(peak_bankroll, :balance),
                    total_deposited  = total_deposited + :amount,
                    last_updated     = NOW()
            """), {"balance": float(new_balance), "amount": float(amount)})

            conn.execute(text("""
                INSERT INTO bankroll_history (event, amount, balance, notes)
                VALUES ('deposit', :amount, :balance, :notes)
            """), {
                "amount":  float(amount),
                "balance": float(new_balance),
                "notes":   notes,
            })

        print(f"💰 Depósito: +{amount:.2f}  →  Nuevo balance: {new_balance:.2f}")
        return new_balance

    except Exception as e:
        print(f"⚠️ bankroll_manager.deposit error: {e}")
        return float(INITIAL_BANKROLL)


# ─────────────────────────────────────────────────────────────
# ESTADÍSTICAS
# ─────────────────────────────────────────────────────────────

def get_bankroll_stats() -> dict:
    """
    Retorna un resumen completo del estado del bankroll.

    Returns:
        {
            "current":     float  — balance actual
            "initial":     float  — capital inicial
            "peak":        float  — máximo histórico
            "total_profit":float  — ganancia/pérdida total
            "roi_pct":     float  — ROI sobre el capital inicial
            "drawdown_pct":float  — caída desde el pico (%)
        }
    """
    try:
        ensure_bankroll_schema()
        df = pd.read_sql(
            "SELECT * FROM bankroll ORDER BY id LIMIT 1", engine
        )
        if df.empty:
            return _empty_stats()

        row = df.iloc[0]
        initial  = float(row["initial_bankroll"])
        current  = float(row["current_bankroll"])
        peak     = float(row["peak_bankroll"])
        deposited = float(row["total_deposited"])

        total_profit = current - deposited
        roi_pct      = (total_profit / deposited * 100) if deposited > 0 else 0.0
        drawdown_pct = ((peak - current) / peak * 100) if peak > 0 else 0.0

        return {
            "current":      round(current,      2),
            "initial":      round(initial,       2),
            "peak":         round(peak,          2),
            "total_profit": round(total_profit,  2),
            "roi_pct":      round(roi_pct,       2),
            "drawdown_pct": round(drawdown_pct,  2),
        }

    except Exception:
        return _empty_stats()


def _empty_stats() -> dict:
    init = float(INITIAL_BANKROLL)
    return {
        "current":      init,
        "initial":      init,
        "peak":         init,
        "total_profit": 0.0,
        "roi_pct":      0.0,
        "drawdown_pct": 0.0,
    }
