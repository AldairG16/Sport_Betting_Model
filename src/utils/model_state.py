"""
src/utils/model_state.py
========================
Estado aprendido compartido entre corridas (tabla `model_state` en Neon).

Por qué existe: en GitHub Actions cada corrida arranca en un runner limpio.
Un archivo que escribe el weekly (calibración, CLV gate, caché de CLV,
pesos del ancla) muere con su runner, y el morning del día siguiente — en
OTRO runner — jamás lo ve: seguía leyendo la copia commiteada en git
(calibración y caché de CLV congelados desde el 7-may-26; el CLV gate
nunca llegó a bloquear nada en producción). El mismo bug ya se había
resuelto para los parámetros DC-MLE (dc_mle_fitter, H2 rondas 1-4); este
módulo generaliza esa solución.

Contrato:
  - La DB es la fuente de verdad. El archivo local es espejo (inspección y
    desarrollo sin DB) y fallback de lectura.
  - save_state historiza cada escritura en `model_state_history`, así la
    evolución de lo aprendido semana a semana queda auditable.
  - Ninguna función lanza: un fallo de DB se loguea y el caller decide.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from config.database import engine

_DDL_STATE = """
    CREATE TABLE IF NOT EXISTS model_state (
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    )
"""

_DDL_HISTORY = """
    CREATE TABLE IF NOT EXISTS model_state_history (
        id SERIAL PRIMARY KEY,
        key TEXT NOT NULL,
        value JSONB NOT NULL,
        fitted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW()
    )
"""


def _write_file(file_path: Path, value: dict) -> None:
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        print(f"⚠️  model_state: no se pudo escribir el espejo {file_path.name}: {e}")


def save_state(key: str, value: dict, file_path: Path | None = None,
               history: bool = True) -> bool:
    """
    Persiste `value` bajo `key` en la DB (y espejo opcional en archivo).
    Devuelve True si la escritura en la DB tuvo éxito — que es la que ven
    las demás corridas.
    """
    if file_path is not None:
        _write_file(file_path, value)
    payload = json.dumps(value, default=str)
    try:
        with engine.begin() as conn:
            conn.execute(text(_DDL_STATE))
            conn.execute(text("""
                INSERT INTO model_state (key, value, updated_at)
                VALUES (:k, CAST(:v AS jsonb), NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value, updated_at = NOW()
            """), {"k": key, "v": payload})
            if history:
                conn.execute(text(_DDL_HISTORY))
                conn.execute(text("""
                    INSERT INTO model_state_history (key, value, fitted_at)
                    VALUES (:k, CAST(:v AS jsonb), :t)
                """), {"k": key, "v": payload, "t": datetime.now(timezone.utc)})
        return True
    except Exception as e:
        print(f"⚠️  model_state/{key}: no se pudo persistir en la DB — las demás "
              f"corridas NO verán este estado. {type(e).__name__}: {str(e)[:150]}")
        return False


def load_state(key: str, file_path: Path | None = None) -> dict | None:
    """
    Lee el estado de `key`: DB primero, archivo como fallback. None si no
    existe en ninguno (el caller aplica su default neutro).
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM model_state WHERE key = :k"), {"k": key}
            ).first()
        if row and row[0] is not None:
            v = row[0]
            return json.loads(v) if isinstance(v, str) else dict(v)
    except Exception as e:
        print(f"⚠️  model_state/{key}: DB no disponible, uso fallback local. "
              f"{type(e).__name__}: {str(e)[:120]}")
    if file_path is not None and file_path.exists():
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️  model_state: {file_path.name} ilegible: {e}")
    return None
