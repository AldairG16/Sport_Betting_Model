"""
Revisor de PlayDoit por captura (21-oct-26): el dueño manda una captura al
bot, Claude la lee y el sistema responde momio por momio si vale la pena,
con la misma regla de siempre (la probabilidad más baja entre el programa y
el mercado, menos 2 pts). Telegram, Claude y la base son falsos: no gastan.
"""

from types import SimpleNamespace

import pytest

from src.models import playdoit_prices as PP

RM = {"home_norm": "real madrid", "away_norm": "leipzig", "home": "Real Madrid", "away": "RB Leipzig",
      "label": "Real Madrid vs RB Leipzig", "home_team_norm": "real madrid", "away_team_norm": "leipzig",
      "sport_key": "soccer_uefa_champs_league", "match_date": "2026-10-21 19:00+00:00",
      "pin_home_odds": 1.24, "pin_draw_odds": 7.5, "pin_away_odds": 11.0,
      "consensus_home_odds": 1.23, "consensus_draw_odds": 7.36, "consensus_away_odds": 10.16,
      "consensus_over25_odds": 1.177, "consensus_under25_odds": 4.957,
      "btts_yes_odds": 1.55, "btts_no_odds": 2.70,
      "p_home": 0.807, "p_draw": 0.12, "p_away": 0.073, "p_over25": 0.83, "p_btts": 0.62}


def test_prices_as_playdoit_shows_them():
    assert PP.parse_price("+150") == 2.5 and PP.parse_price("-120") == pytest.approx(1.8333, abs=1e-4)
    assert PP.parse_price("2.50") == 2.5 and PP.parse_price("−300") == pytest.approx(1.3333, abs=1e-4)
    assert PP.parse_price("+50") is None and PP.parse_price("abc") is None and PP.parse_price("") is None


def test_markets_read_from_the_screenshot():
    assert PP.market_key({"mercado": "1x2", "seleccion": "local"}) == "home_win"
    assert PP.market_key({"mercado": "doble_oportunidad", "seleccion": "X2"}) == "dc_x2"
    assert PP.market_key({"mercado": "total_goles", "seleccion": "mas", "linea": 2.5}) == "over25"
    assert PP.market_key({"mercado": "total_goles", "seleccion": "mas", "linea": 3.5}) is None
    assert PP.market_key({"mercado": "ambos_anotan", "seleccion": "sí"}) == "btts"
    assert PP.market_key({"mercado": "otro", "seleccion": "x"}) is None


def test_the_match_is_found_even_with_other_spellings():
    assert PP.find_fixture("Real Madrid", "Leipzig", [RM], None) is RM
    assert PP.find_fixture("Real Madrid CF", "RB Leipzig", [RM], None) is RM
    assert PP.find_fixture("Barcelona", "Sevilla", [RM], None) is None


def test_same_rule_as_the_system():
    # conservadora: min(programa 80.7%, Pinnacle 78.2%) = 78.2%; −300 = 75% → +3.2 pts ✅
    ok = PP.judge(PP.parse_price("-300"), 0.782, 0.807)
    assert ok["verdict"] == "apuesta" and ok["min_american"] == -320 and ok["ev"] > 0
    no = PP.judge(PP.parse_price("-370"), 0.782, 0.807)
    assert no["verdict"] == "no"
    assert PP.judge(2.0, None, None)["verdict"] == "sin_referencia"


ITEMS = [
    {"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "1x2", "seleccion": "local", "momio": "-300"},
    {"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "1x2", "seleccion": "empate", "momio": "+650"},
    {"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "ambos_anotan", "seleccion": "no",
     "momio": "+210", "mejorado": True},
    {"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "otro", "seleccion": "Vinícius anota",
     "momio": "+120", "descripcion": "Vinícius anota"},
    {"local": "Barcelona", "visitante": "Sevilla", "mercado": "1x2", "seleccion": "local", "momio": "-150"},
]


def test_reply_says_which_prices_are_worth_it():
    rows = PP.evaluate_items(ITEMS, [RM], None)
    verdicts = [r["verdict"] for r in rows]
    assert verdicts == ["apuesta", "no", "apuesta", "sin_mercado", "sin_partido"]
    text = PP.format_reply(rows)
    assert "✅ Gana Real Madrid -300: vale la pena (mínimo -320" in text
    assert "❌ Empate +650: no (necesita +" in text
    assert "✅ Ambos anotan: no +210 🚀" in text
    assert "➖ Vinícius anota +120: mercado sin referencia" in text
    assert "Barcelona vs Sevilla" in text and "Apuesta solo lo que tenga ✅" in text
    assert PP.format_reply([]).startswith("🔎 No encontré momios")


def test_playdoit_price_ratio_is_learned_from_screenshots():
    rows = [{"decimal": 1.90, "consensus_decimal": 2.00, "boosted": False}] * 6 + \
           [{"decimal": 1.95, "consensus_decimal": 2.00, "boosted": False}] * 5 + \
           [{"decimal": 2.40, "consensus_decimal": 2.00, "boosted": True}] * 5        # mejorados no cuentan
    assert PP.playdoit_ratio(rows) == pytest.approx(0.95)
    assert PP.playdoit_ratio(rows[:5]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Telegram + Claude falsos
# ─────────────────────────────────────────────────────────────────────────────

class FakeHttp:
    def __init__(self, updates=None):
        self.sent, self.posts, self.updates = [], [], list(updates or [])

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append((url, json))
        method = url.rsplit("/", 1)[-1]
        if "api.github.com" in url:
            return SimpleNamespace(status_code=204, json=lambda: {})
        result = {"getFile": {"file_path": "photos/f.jpg"}, "getWebhookInfo": {"url": ""},
                  "sendMessage": {"message_id": 99}}.get(method)
        if method == "sendMessage":
            self.sent.append(json)
        if method == "getUpdates":
            result = [self.updates.pop(0)] if self.updates else []
        return SimpleNamespace(status_code=200, json=lambda r=result: {"ok": True, "result": r})

    def get(self, url, timeout=None):
        return SimpleNamespace(content=b"\xff\xd8\xff fake jpeg", status_code=200)


class FakeClaude:
    def __init__(self, text):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._text = text

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=2100, output_tokens=400, cache_read_input_tokens=0))


@pytest.fixture
def checker(monkeypatch):
    import scripts.playdoit_checker as C
    import src.utils.anthropic_budget as AB
    monkeypatch.setattr(AB, "can_call", lambda engine, est: (True, ""))
    monkeypatch.setattr(AB, "record_call", lambda engine, **kw: 0.0004)
    monkeypatch.setattr(C, "_today_spent", lambda engine: 0.0)
    monkeypatch.setattr(C, "_add_spent", lambda cost: None)
    saved = []
    monkeypatch.setattr(PP, "save_rows", lambda engine, rows, mid=None: saved.append((rows, mid)) or len(rows))
    return C, saved


PHOTO = {"message_id": 7, "chat": {"id": 555}, "photo": [{"file_id": "a", "file_size": 10},
                                                          {"file_id": "b", "file_size": 900}]}


def test_screenshot_from_the_owner_gets_a_verdict(checker, monkeypatch):
    C, saved = checker
    import json
    client = FakeClaude("Listo:\n" + json.dumps({"items": ITEMS[:2]}))
    http = FakeHttp()
    import src.utils.team_normalizer as tn
    monkeypatch.setattr(tn, "normalize_team", lambda x: str(x).lower().replace("rb ", ""))
    out = C.handle_message(PHOTO, "TOKEN", "555", engine=object(), client=client, http=http, fixtures=[RM])
    assert "✅ Gana Real Madrid -300" in out and "❌ Empate +650" in out
    (kw,) = client.calls
    assert kw["model"] == "claude-haiku-5-5"
    img = kw["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"
    assert http.sent[0]["chat_id"] == "555" and http.sent[0]["reply_to_message_id"] == 7
    assert any("getFile" in u and j["file_id"] == "b" for u, j in http.posts)   # la foto más grande
    assert saved and saved[0][1] == 7


def test_other_chats_are_ignored_and_text_gets_the_help(checker):
    C, _ = checker
    http = FakeHttp()
    assert C.handle_message({**PHOTO, "chat": {"id": 999}}, "TOKEN", "555", engine=object(), http=http) is None
    assert http.sent == []
    out = C.handle_message({"message_id": 8, "chat": {"id": 555}, "text": "hola"}, "TOKEN", "555",
                           engine=object(), http=http)
    assert out.startswith("🔎 <b>Revisor de PlayDoit</b>") and http.sent


def test_unreadable_answer_or_daily_cap_replies_why(checker, monkeypatch):
    C, _ = checker
    http = FakeHttp()
    out = C.handle_message(PHOTO, "TOKEN", "555", engine=object(), client=FakeClaude("no sé"), http=http,
                           fixtures=[RM])
    assert out.startswith("⚠️ no entendí la captura")
    monkeypatch.setattr(C, "_today_spent", lambda engine: 1.0)
    out = C.handle_message(PHOTO, "TOKEN", "555", engine=object(), client=FakeClaude("{}"), http=http,
                           fixtures=[RM])
    assert "tope diario" in out


def test_listener_keeps_the_offset_and_hands_over_before_the_time_limit(checker, monkeypatch):
    C, _ = checker
    import src.utils.model_state as ms
    store = {}
    monkeypatch.setattr(ms, "load_state", lambda k: store.get(k))
    monkeypatch.setattr(ms, "save_state", lambda k, v, history=True: store.__setitem__(k, v))
    handled = []
    monkeypatch.setattr(C, "handle_message", lambda msg, *a, **k: handled.append(msg) or "ok")
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    t = {"now": 0.0}

    def clock():
        t["now"] += 60          # cada vuelta avanza un minuto
        return t["now"]
    http = FakeHttp(updates=[{"update_id": 41, "message": {"message_id": 1, "chat": {"id": 555}, "text": "hola"}}])
    n = C.listen(5, "TOKEN", "555", http=http, clock=clock)
    assert n == 1 and store[C.OFFSET_KEY] == {"offset": 42}
    assert any("api.github.com" in u and "playdoit_checker.yml" in u for u, _ in http.posts)   # relevo
