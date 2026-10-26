"""
El dashboard con la base caída (26-oct-26).

El 25-oct una consulta falló (la base tardó, por la VPN de la PC) y
Anteriores salió con todos los partidos "Sin análisis" sin avisar. Esta
prueba recorre TODOS los endpoints de datos con cada consulta fallando: ninguno
puede contestar ok con datos vacíos (parecería que no hay nada) ni tronar con
un 500. Deben decir que no hubo conexión. Así la clase entera queda vigilada,
no solo la pantalla donde apareció.
"""

import pandas as pd
import pytest

import dashboard.app as A

# los que no leen la base, o la leen solo con tablas opcionales
NO_DB = {"/api/health", "/api/version", "/api/gh/status", "/api/narrative"}
# los que hacen algo (disparan workflows, gastan): no se llaman aquí
ACTIONS = {"/api/gh/dispatch/<wf>", "/api/narrative/generate"}
ARGS = {"/api/by/<dim>": "/api/by/league", "/api/match": "/api/match?home=a&away=b&date=2026-10-25T00:00:00Z",
        "/api/board/team": "/api/board/team?name=a"}


def _routes():
    out = []
    for rule in A.app.url_map.iter_rules():
        r = str(rule)
        if r.startswith("/api/") and r not in ACTIONS:
            out.append(r)
    return sorted(out)


@pytest.mark.parametrize("route", _routes())
def test_no_endpoint_shows_empty_data_as_if_it_were_fine(monkeypatch, route):
    def down(*a, **k):
        raise pd.errors.DatabaseError("(psycopg2.OperationalError) server closed the connection unexpectedly")
    monkeypatch.setattr(A.pd, "read_sql", down)
    monkeypatch.setattr(A, "QUERY_RETRY_S", 0)
    monkeypatch.setitem(A._PAST_CACHE, "rows", None)
    resp = A.app.test_client().get(ARGS.get(route, route))
    assert resp.status_code == 200, f"{route} tronó con la base caída ({resp.status_code})"
    if route in NO_DB:
        return
    d = resp.get_json()
    assert d is not None and d.get("ok") is False, f"{route} contestó ok sin base: {str(d)[:160]}"
    assert "conexión" in d.get("msg", "") or "datos" in d.get("msg", "")
