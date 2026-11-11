"""
src/utils/db_meter.py
=====================
Cuánto lee de la base cada corrida (10-nov-26).

El 9-nov Neon bloqueó la base: el plan gratis trae 5 GB de transferencia al
mes y el programa bajó 5.58 GB en 9 días de una base de 236 MB. Nada lo medía:
una consulta que relee el historial entero en cada closing (48 al día) o en
cada recarga del dashboard no falla, solo gasta, hasta que la base se cierra.

Cuenta, por consulta, las filas y las celdas (filas × columnas) que devuelve.
No son bytes exactos (psycopg2 no los da), pero sirven para ver qué consulta
pesa más y si un cambio multiplica las lecturas: el orquestador imprime el
resumen al final y lo guarda en pipeline_runs (rows_read, cells_read), y el
chequeo de salidas compara el closing de hoy con el de la semana.
"""

import threading

_LOCK = threading.Lock()
_STATS: dict = {}            # consulta (primeros caracteres) → [veces, filas, celdas]
_INSTALLED: set = set()
KEY_CHARS = 110


def _key(statement) -> str:
    return " ".join(str(statement).split())[:KEY_CHARS]


def _after_execute(conn, cursor, statement, parameters, context, executemany):
    """after_cursor_execute: con psycopg2 el cursor ya trae todas las filas y
    rowcount dice cuántas. Lo que no es una lectura (sin description) o no
    sabe cuántas filas trae (rowcount -1, como SQLite) no cuenta."""
    try:
        desc = cursor.description
        rows = cursor.rowcount
        if not desc or rows is None or rows < 0:
            return
        record(statement, int(rows), int(rows) * len(desc))
    except Exception:
        return                                   # medir nunca rompe una consulta


def record(statement, rows: int, cells: int) -> None:
    k = _key(statement)
    with _LOCK:
        s = _STATS.setdefault(k, [0, 0, 0])
        s[0] += 1
        s[1] += rows
        s[2] += cells


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


def totals() -> dict:
    with _LOCK:
        vals = list(_STATS.values())
    return {"queries": sum(v[0] for v in vals), "rows": sum(v[1] for v in vals),
            "cells": sum(v[2] for v in vals)}


def top(n: int = 5) -> list:
    """[(consulta, veces, filas, celdas)] de las que más celdas leyeron."""
    with _LOCK:
        items = [(k, *v) for k, v in _STATS.items()]
    return sorted(items, key=lambda x: -x[3])[:n]


def summary_lines(n: int = 5) -> list:
    t = totals()
    if not t["queries"]:
        return []
    out = [f"🗄️  Lecturas de la base: {t['rows']:,} filas · {t['cells']:,} celdas en {t['queries']} consultas"]
    for k, times, rows, cells in top(n):
        out.append(f"   {cells:>10,} celdas · {rows:>8,} filas · {times:>3}× — {k}")
    return out
