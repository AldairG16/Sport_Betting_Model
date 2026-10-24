"""
Reintento de The Odds API (24-oct-26): un 502 pasajero dejó la MLS sin recargar
en un closing. Se reintenta ante 5xx y errores de red; un 4xx no.
"""

from types import SimpleNamespace

import pytest
import requests

from scripts.update_upcoming_matches import get_with_retry


def _seq(*responses):
    calls = []

    def get(url, params=None, timeout=None):
        r = responses[len(calls)]
        calls.append(url)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(status_code=r)
    return get, calls


def test_a_passing_5xx_is_retried():
    get, calls = _seq(502, 200)
    waits = []
    assert get_with_retry("u", {}, get=get, sleep=waits.append).status_code == 200
    assert len(calls) == 2 and waits == [3]


def test_a_4xx_is_not_retried_and_the_last_5xx_is_returned():
    get, calls = _seq(401)
    assert get_with_retry("u", {}, get=get, sleep=lambda s: None).status_code == 401 and len(calls) == 1
    get, calls = _seq(502, 503, 500)
    assert get_with_retry("u", {}, get=get, sleep=lambda s: None).status_code == 500 and len(calls) == 3


def test_network_errors_are_retried_then_raised():
    get, calls = _seq(requests.ConnectionError("x"), 200)
    assert get_with_retry("u", {}, get=get, sleep=lambda s: None).status_code == 200
    get, calls = _seq(requests.Timeout("a"), requests.Timeout("b"), requests.Timeout("c"))
    with pytest.raises(requests.Timeout):
        get_with_retry("u", {}, get=get, sleep=lambda s: None)
    assert len(calls) == 3
