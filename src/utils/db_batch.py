"""
src/utils/db_batch.py
=====================
Inserción por lotes con ON CONFLICT DO NOTHING — un viaje a la base por
lote, conteo real de filas nuevas y aislamiento de filas malas.

Por qué existe (22-sep-26):
  - `conn.execute(text(...), lista_de_dicts)` NO agrupa: con SQL textual,
    psycopg2 ejecuta una sentencia por fila (un viaje de red a Neon cada
    una, ~80 ms desde Actions). El weekly tardaba 25 min en cargar 18k filas
    de ligas extra y 21 min en 15k eventos, rozando el límite del job.
  - Los cargadores contaban "insertadas" las filas PROCESADAS (con ON
    CONFLICT DO NOTHING casi todas ya existían): el log mentía.
  - Un `try/except: pass` por fila dentro de una transacción no aísla nada:
    en Postgres el primer fallo aborta la transacción, las filas siguientes
    fallan también y el COMMIT final se convierte en ROLLBACK de todo el
    lote — en silencio.

Aquí cada lote es UNA sentencia multi-VALUES con RETURNING (cuenta exacta
de insertadas) dentro de un savepoint. Si el lote falla se reintenta fila a
fila, cada una con su savepoint: las filas buenas entran, las malas se
cuentan y la primera causa se reporta.
"""

import math
from datetime import date, datetime

from sqlalchemy import text

MAX_BIND_PARAMS = 60000     # Postgres admite 65535 por sentencia


def _native(v):
    """Valor que psycopg2 sabe adaptar: numpy → nativo, NaN/NaT → None."""
    if v is None:
        return None
    if hasattr(v, "to_pydatetime"):              # pandas Timestamp
        try:
            return None if v != v else v.to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(v, (str, bytes, bool, date, datetime)):
        return v
    if hasattr(v, "item"):                       # escalares numpy
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def insert_ignore_conflicts(conn, table: str, columns: list[str], rows: list[dict],
                            conflict_cols: list[str], chunk: int = 500) -> dict:
    """
    Inserta `rows` (dicts con al menos `columns`) en `table`, ignorando las
    que chocan con `conflict_cols`. Debe llamarse dentro de una transacción
    (`with engine.begin() as conn`). `table`, `columns` y `conflict_cols`
    son identificadores del código, nunca datos de usuario.

    Devuelve {"inserted", "existing", "errors", "first_error"}.
    """
    out = {"inserted": 0, "existing": 0, "errors": 0, "first_error": None}
    if not rows:
        return out
    cols_sql = ", ".join(columns)
    tail = f"ON CONFLICT ({', '.join(conflict_cols)}) DO NOTHING RETURNING 1"
    single = text(f"INSERT INTO {table} ({cols_sql}) VALUES "
                  f"({', '.join(':' + c for c in columns)}) {tail}")
    chunk = max(1, min(chunk, MAX_BIND_PARAMS // max(1, len(columns))))
    clean = [{c: _native(r.get(c)) for c in columns} for r in rows]

    for start in range(0, len(clean), chunk):
        part = clean[start:start + chunk]
        values = ", ".join("(" + ", ".join(f":{c}_{i}" for c in columns) + ")"
                           for i in range(len(part)))
        params = {f"{c}_{i}": r[c] for i, r in enumerate(part) for c in columns}
        try:
            with conn.begin_nested():
                n = len(conn.execute(
                    text(f"INSERT INTO {table} ({cols_sql}) VALUES {values} {tail}"),
                    params).fetchall())
            out["inserted"] += n
            out["existing"] += len(part) - n
            continue
        except Exception:
            pass   # el lote falló entero (savepoint revertido): aislar fila a fila
        for r in part:
            try:
                with conn.begin_nested():
                    n = len(conn.execute(single, r).fetchall())
                out["inserted"] += n
                out["existing"] += 1 - n
            except Exception as e:
                out["errors"] += 1
                if out["first_error"] is None:
                    out["first_error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
    return out


def describe(result: dict) -> str:
    """Resumen de una línea para el log."""
    s = f"{result['inserted']:,} nuevas, {result['existing']:,} ya existían"
    if result["errors"]:
        s += f", {result['errors']:,} con error (primera: {result['first_error']})"
    return s
