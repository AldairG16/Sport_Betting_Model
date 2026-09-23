"""
tests/fake_db.py
================
Motor SQLAlchemy falso mínimo para probar código que ESCRIBE en la base
sin una base real: registra cada sentencia (SQL normalizado + parámetros)
y responde lo que el test programe. Las lecturas con pd.read_sql se
sustituyen aparte (monkeypatch de pandas.read_sql).

    eng = FakeEngine()                       # rowcount 1, sin filas
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[("x",)]))
    ...
    eng.statements("UPDATE bets_history")    # [(sql, params), ...]
"""

from contextlib import contextmanager


class FakeResult:
    def __init__(self, rows=(), rowcount=1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchone(self):
        return self.first()

    def scalar(self):
        row = self.first()
        return row[0] if row else None


class FakeConn:
    def __init__(self, db):
        self.db = db

    def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.db.executed.append((sql, params))
        return self.db.respond(sql, params)

    @contextmanager
    def begin_nested(self):
        yield


class FakeEngine:
    """`respond(sql, params) -> FakeResult`; por defecto rowcount 1, sin filas."""

    def __init__(self, respond=None):
        self.executed = []
        self._respond = respond

    def respond(self, sql, params):
        return self._respond(sql, params) if self._respond else FakeResult()

    @contextmanager
    def begin(self):
        yield FakeConn(self)

    @contextmanager
    def connect(self):
        yield FakeConn(self)

    def statements(self, prefix: str) -> list:
        return [(s, p) for s, p in self.executed if s.startswith(prefix)]
