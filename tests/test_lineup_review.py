"""
Revisión con Claude de las alertas de alineación (20-oct-26): tope de
$0.50/semana, Haiku 5.5 con 2 búsquedas web, veredicto en JSON. Las pruebas
usan un cliente falso: no gastan.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.models import lineup_review as LR

GROUP = [{"sport_key": "soccer_uefa_champs_league", "kickoff": "2026-10-20 19:00:00+00:00",
          "home": "liverpool", "away": "villarreal", "weakened": "liverpool",
          "missing": ["Alexander Isak", "Florian Wirtz", "Dominik Szoboszlai", "Milos Kerkez"],
          "top_scorer_out": "Alexander Isak", "market": "dc_x2", "pin_prob": 0.384, "min_american": 175}]


class FakeClient:
    def __init__(self, text, stop="end_turn"):
        self.calls = []
        self._text, self._stop = text, stop
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        content = [SimpleNamespace(type="server_tool_use"), SimpleNamespace(type="web_search_tool_result"),
                   SimpleNamespace(type="text", text=self._text)]
        usage = SimpleNamespace(input_tokens=3000, output_tokens=600, cache_read_input_tokens=0)
        return SimpleNamespace(content=content, stop_reason=self._stop, usage=usage)


@pytest.fixture
def budget(monkeypatch):
    import src.utils.anthropic_budget as AB
    spent = {"week": 0.10}
    monkeypatch.setattr(LR, "weekly_spent", lambda engine: spent["week"])
    monkeypatch.setattr(AB, "can_call", lambda engine, est: (True, ""))
    monkeypatch.setattr(AB, "record_call", lambda engine, **kw: 0.021)
    return spent


def test_review_asks_haiku_with_two_searches_and_reads_the_verdict(budget):
    client = FakeClient('Revisé noticias. {"veredicto": "CONFIRMA", "bajas": "lesion", '
                        '"motivo": "Isak y Wirtz lesionados según el club"}')
    r = LR.review_alert(GROUP, engine=object(), client=client,
                        now=datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc))
    assert r == {"verdict": "CONFIRMA", "absences": "lesion",
                 "reason": "Isak y Wirtz lesionados según el club", "cost": 0.021}
    (kw,) = client.calls
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}]
    assert kw["output_config"] == {"effort": "medium"}
    assert "2026-10-20" in kw["system"]
    assert "Alexander Isak" in kw["messages"][0]["content"] and "villarreal" in kw["messages"][0]["content"]


def test_weekly_cap_stops_the_call(budget):
    budget["week"] = 0.49
    client = FakeClient('{"veredicto": "CONFIRMA"}')
    assert LR.review_alert(GROUP, engine=object(), client=client) is None
    assert client.calls == []                                          # no se gastó nada


def test_unusable_answers_keep_the_cost_but_no_verdict(budget):
    for client in (FakeClient("no sé", stop="end_turn"), FakeClient("", stop="refusal"),
                   FakeClient('{"veredicto": "TAL VEZ"}')):
        r = LR.review_alert(GROUP, engine=object(), client=client)
        assert r == {"verdict": None, "absences": None, "reason": None, "cost": 0.021}


def test_message_line_and_json_extraction():
    assert LR.review_line(None) == "" and LR.review_line({"verdict": None}) == ""
    line = LR.review_line({"verdict": "DESCARTA", "reason": "rotación por la final del domingo"})
    assert "no apuestes al rival" in line and "final del domingo" in line
    assert LR.extract_json('texto {"a": "x}y", "b": {"c": 1}} más') == {"a": "x}y", "b": {"c": 1}}
    assert LR.extract_json("{roto") is None and LR.extract_json("") is None
