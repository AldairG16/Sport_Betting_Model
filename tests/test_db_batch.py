"""
Inserción por lotes (src/utils/db_batch.py) con una conexión falsa que se
comporta como Postgres en lo que importa: ON CONFLICT DO NOTHING, RETURNING
solo de las filas nuevas, y una fila mala hace fallar su sentencia entera.
La validación contra Postgres real vive en scripts/db_smoke_test.py (tabla
temporal).
"""

from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

from src.utils.db_batch import insert_ignore_conflicts, describe, _native

COLS = ["date", "home_team", "away_team", "home_goals"]
KEY = ["date", "home_team", "away_team"]


class FakeConn:
    """Tabla en memoria con clave única; 'BAD' en una celda = error de tipo."""

    def __init__(self, existing=()):
        self.keys = set(existing)
        self.statements = 0
        self.savepoints = 0

    @contextmanager
    def begin_nested(self):
        self.savepoints += 1
        snapshot = set(self.keys)
        try:
            yield
        except Exception:
            self.keys = snapshot          # el savepoint revierte lo parcial
            raise

    def execute(self, stmt, params):
        self.statements += 1
        sql = str(stmt)
        if "VALUES (:date," in sql:       # sentencia de una fila
            rows = [params]
        else:
            n = len(params) // len(COLS)
            rows = [{c: params[f"{c}_{i}"] for c in COLS} for i in range(n)]
        if any(v == "BAD" for r in rows for v in r.values()):
            raise ValueError('invalid input syntax for type integer: "BAD"')
        returned = []
        for r in rows:
            k = tuple(r[c] for c in KEY)
            if k not in self.keys:
                self.keys.add(k)
                returned.append((1,))
        return type("R", (), {"fetchall": lambda self_: returned})()


def _rows(n, start=0):
    return [{"date": f"2026-01-{1 + (i % 28):02d}", "home_team": f"h{start + i}",
             "away_team": "a", "home_goals": i % 5} for i in range(n)]


def test_one_statement_per_chunk_and_exact_counts():
    conn = FakeConn(existing={("2026-01-01", "h0", "a")})
    res = insert_ignore_conflicts(conn, "matches", COLS, _rows(1200), KEY, chunk=500)
    assert res == {"inserted": 1199, "existing": 1, "errors": 0, "first_error": None}
    assert conn.statements == 3                      # 500 + 500 + 200, no 1200


def test_second_load_inserts_nothing():
    conn = FakeConn()
    insert_ignore_conflicts(conn, "matches", COLS, _rows(300), KEY)
    again = insert_ignore_conflicts(conn, "matches", COLS, _rows(300), KEY)
    assert again["inserted"] == 0 and again["existing"] == 300


def test_a_bad_row_is_isolated_instead_of_sinking_the_batch():
    rows = _rows(10)
    rows[4]["home_goals"] = "BAD"
    conn = FakeConn()
    res = insert_ignore_conflicts(conn, "matches", COLS, rows, KEY)
    assert res["inserted"] == 9 and res["errors"] == 1
    assert "invalid input syntax" in res["first_error"]
    assert ("2026-01-05", "h4", "a") not in conn.keys
    assert "1 con error" in describe(res)


def test_duplicates_inside_the_same_batch_count_once():
    rows = _rows(3) + _rows(3)
    res = insert_ignore_conflicts(FakeConn(), "matches", COLS, rows, KEY)
    assert res["inserted"] == 3 and res["existing"] == 3


def test_chunk_respects_the_bind_parameter_limit():
    conn = FakeConn()
    insert_ignore_conflicts(conn, "matches", COLS, _rows(20000), KEY, chunk=100000)
    assert conn.statements == 2                      # 60000 // 4 = 15000 filas por sentencia


@pytest.mark.parametrize("value,expected", [
    (np.int64(3), 3), (np.float64(2.5), 2.5), (float("nan"), None), (np.nan, None),
    (pd.NaT, None), (None, None), (np.bool_(True), True), ("x", "x"),
])
def test_native_values(value, expected):
    got = _native(value)
    assert got == expected and type(got) is type(expected)


def test_native_timestamp():
    assert _native(pd.Timestamp("2026-01-02 15:00")).isoformat() == "2026-01-02T15:00:00"


def test_empty():
    assert insert_ignore_conflicts(FakeConn(), "matches", COLS, [], KEY)["inserted"] == 0
