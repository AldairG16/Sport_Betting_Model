"""
src/utils/db_meter.py
=====================
Cuánto gasta de la base cada proceso (10/11-nov-26).

El 9-nov Neon bloqueó la base: el plan gratis trae 5 GB de transferencia al
mes y el programa bajó 5.58 GB en 9 días de una base de 236 MB. Nada lo medía:
una consulta que relee el historial entero en cada closing (48 al día) o en
cada recarga del dashboard no falla, solo gasta, hasta que la base se cierra.

Por consulta cuenta las filas, las celdas (filas × columnas) y los bytes que
viajan. Por proceso, el tiempo que tuvo la base despierta (Neon la apaga tras
5 min sin consultas: cada consulta la deja encendida hasta 5 min después).

- Bytes: psycopg2 manda el resultado en texto. Con el cursor del lado del
  cliente, todas las filas ya están en memoria al terminar execute(): se leen,
  se suman los largos de cada valor más lo que pone el protocolo por campo y
  por fila, y se rebobina el cursor para quien hizo la consulta. Es una
  estimación del lado bajo (sin TLS ni TCP); src/utils/db_budget.py le suma
  el margen.
- Despierta: unión de [consulta, consulta + 5 min] del proceso.

El orquestador guarda su resumen en pipeline_runs (rows_read, cells_read,
bytes_read, awake_s). Los demás procesos de GitHub Actions (watchdog,
resolvedor, PlayDoit, análisis previo…) lo guardan al salir como
"proc:<script>", y el dashboard cada 30 min como "dashboard".
"""

import atexit
import os
import sys
import threading
import time

_LOCK = threading.Lock()
_STATS: dict = {}            # consulta (primeros caracteres) → [veces, filas, celdas, bytes]
_AWAKE = {"until": 0.0, "s": 0.0}
_INSTALLED: set = set()
_STATE = {"recorded": False, "started": time.time()}
KEY_CHARS = 110
TAIL_S = 300                 # Neon apaga la base tras 5 min sin consultas
ROW_OVERHEAD = 7             # DataRow: tipo (1) + largo (4) + número de campos (2)
FIELD_OVERHEAD = 4           # largo de cada campo
MAX_SIZED_ROWS = 1_500_000   # más filas que esto: bytes por celda promedio, sin releer
AVG_CELL_BYTES = 12


class RewindError(RuntimeError):
    """El cursor no volvió al principio: quien consultó perdería filas."""


def _key(statement) -> str:
    return " ".join(str(statement).split())[:KEY_CHARS]


def _value_len(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (str, bytes, bytearray)):
        return len(v)
    if isinstance(v, memoryview):
        return v.nbytes
    return len(str(v))


def _wire_bytes(cursor, rows: int, cells: int) -> int:
    """Bytes del resultado en el protocolo de texto de Postgres. Solo con un
    cursor de psycopg2 del lado del cliente (ya trae todo); si no, o si es
    enorme, celdas × promedio. Nunca deja el cursor movido."""
    base = rows * ROW_OVERHEAD + cells * FIELD_OVERHEAD
    client_side = (type(cursor).__module__.startswith("psycopg2") and getattr(cursor, "name", None) is None
                   and hasattr(cursor, "scroll") and getattr(cursor, "rownumber", None) == 0)
    if rows == 0:                                # psycopg2 no deja rebobinar un resultado vacío
        return 0
    if not client_side or rows > MAX_SIZED_ROWS:
        return base + cells * AVG_CELL_BYTES
    try:
        data = cursor.fetchall()
    finally:                                     # pase lo que pase, quien consultó lee desde el principio
        try:
            cursor.scroll(0, mode="absolute")
        except Exception as e:
            raise RewindError(f"db_meter: el cursor no se pudo rebobinar ({type(e).__name__})") from e
        if cursor.rownumber != 0:                # no debería pasar: que se vea, no que falten filas
            raise RewindError("db_meter: el cursor no se pudo rebobinar")
    return base + sum(_value_len(v) for row in data for v in row)


def _after_execute(conn, cursor, statement, parameters, context, executemany):
    """after_cursor_execute: toda consulta despierta la base; las lecturas
    (con description y rowcount conocido) suman filas, celdas y bytes."""
    try:
        _awake(time.time())
        desc = cursor.description
        rows = cursor.rowcount
        if not desc or rows is None or rows < 0:
            return
        cells = int(rows) * len(desc)
        nbytes = _wire_bytes(cursor, int(rows), cells)
    except RewindError:
        raise
    except Exception:
        return                                   # medir nunca rompe una consulta
    record(statement, int(rows), cells, nbytes)


def _awake(t: float) -> None:
    with _LOCK:
        if t >= _AWAKE["until"]:
            _AWAKE["s"] += TAIL_S
        else:
            _AWAKE["s"] += t + TAIL_S - _AWAKE["until"]
        _AWAKE["until"] = t + TAIL_S


def record(statement, rows: int, cells: int, nbytes: int = 0) -> None:
    k = _key(statement)
    with _LOCK:
        s = _STATS.setdefault(k, [0, 0, 0, 0])
        s[0] += 1
        s[1] += rows
        s[2] += cells
        s[3] += nbytes


def install(engine) -> None:
    """Una vez por engine."""
    if id(engine) in _INSTALLED:
        return
    from sqlalchemy import event
    event.listen(engine, "after_cursor_execute", _after_execute)
    _INSTALLED.add(id(engine))


def reset() -> None:
    with _LOCK:
        _STATS.clear()
        _AWAKE["s"] = 0.0


def totals() -> dict:
    with _LOCK:
        vals = list(_STATS.values())
        awake = _AWAKE["s"]
    return {"queries": sum(v[0] for v in vals), "rows": sum(v[1] for v in vals),
            "cells": sum(v[2] for v in vals), "bytes": sum(v[3] for v in vals), "awake_s": round(awake)}


def top(n: int = 5) -> list:
    """[(consulta, veces, filas, celdas, bytes)] de las que más bytes leyeron."""
    with _LOCK:
        items = [(k, *v) for k, v in _STATS.items()]
    return sorted(items, key=lambda x: -x[4])[:n]


def mb(nbytes) -> str:
    return f"{(nbytes or 0) / 1e6:,.2f} MB"


def summary_lines(n: int = 5) -> list:
    t = totals()
    if not t["queries"]:
        return []
    out = [f"🗄️  Lecturas de la base: {mb(t['bytes'])} · {t['rows']:,} filas · {t['cells']:,} celdas en "
           f"{t['queries']} consultas · base despierta ~{t['awake_s'] / 60:.0f} min"]
    for k, times, rows, cells, nbytes in top(n):
        out.append(f"   {mb(nbytes):>10} · {rows:>8,} filas · {times:>3}× — {k}")
    return out


# ── Lo que gastan los procesos que no son el orquestador ─────────────────────

def mark_recorded() -> None:
    """El proceso ya guardó su consumo (el orquestador): no se repite al salir."""
    _STATE["recorded"] = True


def _record_at_exit() -> None:
    if _STATE["recorded"] or "pytest" in sys.modules or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    t = totals()
    if not t["queries"]:
        return
    try:
        from config.database import engine
        from src.utils.pipeline_runs import record_run
        mode = "proc:" + os.path.splitext(os.path.basename(sys.argv[0] or "python"))[0]
        record_run(engine, mode, 0, time.time() - _STATE["started"], t["rows"], t["cells"],
                   t["bytes"], t["awake_s"])
    except Exception as e:                       # al salir no hay a quién avisar: que quede en el log
        print(f"⚠️  db_meter: no se pudo guardar el consumo del proceso ({type(e).__name__})")


atexit.register(_record_at_exit)
