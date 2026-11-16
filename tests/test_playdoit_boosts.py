"""
Boosts y combinadas en el revisor de PlayDoit (16-nov-26, el dueño: B1). Un
boost junta varias apuestas de un partido; se mide con lo que salió en los
partidos parecidos del historial (src/models/combo_odds.py) y se juzga con la
misma regla de 2 puntos. La base, Telegram y Claude son falsos: no gastan.
"""

import json

import pytest

from src.models import combo_odds as CO
from src.models import playdoit_prices as PP
from tests.test_playdoit_checker import PHOTO, RM, FakeClaude, FakeHttp

GOALS_CORNERS = [{"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "ambos_anotan", "seleccion": "si"},
                 {"mercado": "total_goles", "seleccion": "mas", "linea": 2.5},
                 {"mercado": "corners_total", "seleccion": "mas", "linea": 8.5}]


def _boost(legs, price="+243", before="+200"):
    return {"patas": legs, "momio": price, "momio_antes": before, "mejorado": True, "descripcion": "boost"}


def _history(p, n=2205):
    calls = []

    def fn(legs, fh, fa, fo, league):
        calls.append((legs, fh, fa, fo, league))
        return {"p": p, "n": n, "n_league": 100, "tol": 0.03, "p_all": p}
    fn.calls = calls
    return fn


# ─── las patas ─────────────────────────────────────────────────────────────

def test_each_leg_of_a_boost_is_read_into_something_the_history_can_count():
    assert CO.parse_leg({"mercado": "1x2", "seleccion": "visitante"}) == {"kind": "result", "sel": "away"}
    assert CO.parse_leg({"mercado": "total_goles", "seleccion": "más", "linea": "3.5"}) == \
        {"kind": "goals", "sel": "over", "line": 3.5}
    assert CO.parse_leg({"mercado": "corners_total", "seleccion": "menos", "linea": 10.5})["kind"] == "corners"
    assert CO.parse_leg({"mercado": "goles_local", "seleccion": "mas", "linea": 1.5})["kind"] == "home_goals"
    assert CO.parse_leg({"mercado": "resultado_primer_tiempo", "seleccion": "empate"}) == {"kind": "ht_result", "sel": "draw"}
    # una línea entera (puede quedar en empate de línea) o rara no se mide
    assert CO.parse_leg({"mercado": "total_goles", "seleccion": "mas", "linea": 3})["kind"] == "unknown"
    assert CO.parse_leg({"mercado": "corners_total", "seleccion": "mas", "linea": 40.5})["kind"] == "unknown"
    assert CO.parse_leg({"mercado": "empate_anula", "seleccion": "local"})["kind"] == "unknown"
    assert CO.parse_leg({"mercado": "jugador", "seleccion": "anota", "jugador": "Gonçalo Ramos"}) == \
        {"kind": "player", "who": "Gonçalo Ramos", "what": "anota"}


def test_the_query_counts_all_legs_together_in_similar_matches():
    legs = [CO.parse_leg(x) for x in GOALS_CORNERS]
    sql, params = CO.history_sql(legs, 0.55, 0.20, 0.52)
    assert "(hg > 0 AND ag > 0) AND (hg + ag) > :l1 AND (hc + ac) > :l2" in sql
    assert params["l1"] == 2.5 and params["l2"] == 8.5 and params["fh"] == 0.55 and params["fo"] == 0.52
    assert "hc IS NOT NULL" in sql and "fo IS NOT NULL" in sql and "GREATEST(ABS(fh - :fh)" in sql
    assert "FROM historical_odds" in sql and "pin_home > 1" in sql
    # sin los momios del partido: todos los partidos (un promedio)
    sql, params = CO.history_sql(legs[:1])
    assert "0 AS d" in sql and "fh" not in params
    # la tolerancia más chica con 800 partidos; la liga encogida hacia el total
    est = CO.pick_estimate({"n0": 500, "h0": 150, "n1": 1200, "h1": 360, "nl1": 300, "hl1": 120,
                            "n2": 3000, "h2": 900})
    assert est["tol"] == 0.05 and est["n"] == 1200
    assert est["p"] == pytest.approx((120 + 300 * 0.30) / (300 + 300))       # 0.35 entre la liga (0.40) y el total (0.30)
    assert CO.pick_estimate({"n0": 0, "n1": 0, "n2": 0}) is None


def test_same_two_point_rule_and_what_a_player_leg_would_need():
    no = CO.judge(3.43, 0.2878)                                     # el de la captura: ambos + 2.5 + 8.5 córners
    assert no["verdict"] == "no" and no["fair_us"] == 247 and no["min_american"] == 274
    ok = CO.judge(3.43, 0.3443)
    assert ok["verdict"] == "apuesta" and ok["fair_us"] == 190 and ok["ev"] == pytest.approx(0.181, abs=1e-3)
    # gana Portugal (55) + anota Ramos a +151: Ramos tendría que anotar en 76 de cada 100 de esos partidos
    b = CO.player_bound(2.51, 0.55)
    assert b["verdict"] == "jugador" and b["need"] == pytest.approx(0.761, abs=1e-3)
    assert CO.player_bound(2.51, 0.40)["verdict"] == "no"            # ni anotando siempre alcanza
    assert CO.player_bound(3.94, None)["target"] == pytest.approx(0.274, abs=1e-3)   # remates de Bruno a +294


# ─── la respuesta ──────────────────────────────────────────────────────────

def test_a_boost_on_a_followed_match_gets_a_verdict_from_the_history():
    rows = PP.evaluate_items([_boost(GOALS_CORNERS)], [RM], None, None, _history(0.2878))
    (r,) = rows
    assert r["combo"] and r["verdict"] == "no" and r["matches"][0]["source"] == "sistema"
    text = PP.format_reply(rows)
    assert "🚀 <b>Boosts y combinadas</b>" in text
    assert "Ambos anotan + Más de 2.5 goles + Más de 8.5 córners · <b>+243</b> 🚀 (antes +200)" in text
    assert "❌ no vale: sale 29 de cada 100 en 2,205 partidos parecidos; lo justo es +247 y necesita +274 o más" in text
    good = PP.format_reply(PP.evaluate_items([_boost(GOALS_CORNERS)], [RM], None, None, _history(0.3443)))
    assert "✅ vale: sale 34 de cada 100" in good and "deja +18 por cada 100" in good
    # condiciona con el partido: Pinnacle sin margen para el 1X2 y el promedio para más de 2.5
    h = _history(0.30)
    PP.evaluate_items([_boost(GOALS_CORNERS)], [RM], None, None, h)
    (legs, fh, fa, fo, league), = h.calls
    assert fh == pytest.approx(0.782, abs=1e-3) and fo == pytest.approx(0.808, abs=1e-3)
    assert league == "soccer_uefa_champs_league" and len(legs) == 3


def test_a_match_the_system_does_not_follow_uses_the_screenshot_or_only_gives_a_reference():
    pd_legs = [{"local": "Portugal", "visitante": "Dinamarca", "mercado": "ambos_anotan", "seleccion": "si"},
               {"local": "Portugal", "visitante": "Dinamarca", "mercado": "total_goles", "seleccion": "mas",
                "linea": 2.5}]
    alone = PP.format_reply(PP.evaluate_items([_boost(pd_legs, "+180", None)], [RM], None, None, _history(0.40)))
    assert "➖ no sigo este partido: en un partido promedio sale 40 de cada 100 (lo justo +150)" in alone
    assert "✅" not in alone.split("Boosts")[1].split("<i>")[0]
    screen = [{"local": "Portugal", "visitante": "Dinamarca", "mercado": "1x2", "seleccion": s, "momio": m}
              for s, m in (("local", "+128"), ("empate", "+230"), ("visitante", "+320"))]
    h = _history(0.40)
    rows = PP.evaluate_items(screen + [_boost(pd_legs, "+180", None)], [RM], None, None, h)
    assert rows[-1]["verdict"] == "apuesta" and rows[-1]["matches"][0]["source"] == "captura"
    assert h.calls[0][1] == pytest.approx(0.448, abs=1e-3)           # PlayDoit sin su margen
    assert "medido con los momios de PlayDoit de la captura" in PP.format_reply(rows)


def test_a_boost_with_a_player_says_what_the_player_would_need():
    legs = [{"local": "Portugal", "visitante": "Dinamarca", "mercado": "1x2", "seleccion": "local"},
            {"local": "Portugal", "visitante": "Dinamarca", "mercado": "jugador", "seleccion": "anota",
             "jugador": "Gonçalo Ramos"}]
    screen = [{"local": "Portugal", "visitante": "Dinamarca", "mercado": "1x2", "seleccion": s, "momio": m}
              for s, m in (("local", "-120"), ("empate", "+260"), ("visitante", "+380"))]
    t = PP.format_reply(PP.evaluate_items(screen + [_boost(legs, "+151", "+128")], [], None, None, _history(0.55)))
    assert "Gana Portugal + Gonçalo Ramos: anota · <b>+151</b> 🚀 (antes +128)" in t
    assert "➖ depende de Gonçalo Ramos: Gana Portugal sale 55 de cada 100; solo vale si Gonçalo Ramos anota " \
           "en más de 76 de cada 100 de esos partidos" in t
    t = PP.format_reply(PP.evaluate_items(screen + [_boost(legs, "+151")], [], None, None, _history(0.40)))
    assert "❌ no vale: aunque Gonçalo Ramos cumpliera siempre, Gana Portugal sale 40 de cada 100" in t
    # sin el 1X2 en la captura (partido que no sigo): sin ❌ firme, y lo dice
    t = PP.format_reply(PP.evaluate_items([_boost(legs, "+151")], [], None, None, _history(0.40)))
    assert "ni cumpliendo siempre Gonçalo Ramos alcanzaría" in t and "medido con un partido promedio" in t
    assert "❌" not in t.split("Boosts")[1]
    only = [{"local": "Portugal", "visitante": "Dinamarca", "mercado": "jugador",
             "seleccion": "más de 1.5 remates a puerta", "jugador": "Bruno Fernandes"}]
    t = PP.format_reply(PP.evaluate_items([_boost(only, "+294", "+250")], [], None, None, _history(0.5)))
    assert "➖ es de un jugador y no lo puedo medir: para que valga tendría que salir más de 27 de cada 100 veces" in t


def test_single_bets_still_go_the_old_way_and_other_lines_are_measured():
    rows = PP.evaluate_items([{"patas": [{"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "1x2",
                                          "seleccion": "local"}], "momio": "-300", "mejorado": False}],
                             [RM], None, None, _history(0.5))
    assert rows[0]["verdict"] == "apuesta" and not rows[0].get("combo") and rows[0]["key"] == "home_win"
    three = PP.evaluate_items([{"local": "Real Madrid", "visitante": "RB Leipzig", "mercado": "total_goles",
                                "seleccion": "mas", "linea": 3.5, "momio": "+120"}], [RM], None, None, _history(0.52))
    assert three[0]["combo"] and three[0]["verdict"] == "apuesta"
    # sin la consulta del historial: sin referencia, nunca un ✅
    assert PP.evaluate_items([_boost(GOALS_CORNERS)], [RM])[0]["verdict"] == "sin_referencia"


def test_a_boost_is_saved_to_learn_what_playdoit_pays():
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    rows = PP.evaluate_items([_boost(GOALS_CORNERS)], [RM], None, None, _history(0.2878))
    assert PP.save_rows(eng, rows, 9) == 1
    (sql, params), = eng.statements("INSERT INTO playdoit_prices")
    assert "'combinada'" in sql and params["am"] == 243 and params["p"] == pytest.approx(0.2878)
    assert params["boost"] is True and params["verdict"] == "no" and params["match"] == "real madrid vs leipzig"
    assert json.loads(params["raw"])["n"] == [2205]


@pytest.fixture
def boost_checker(monkeypatch):
    import scripts.playdoit_checker as C
    import src.utils.anthropic_budget as AB
    monkeypatch.setattr(AB, "can_call", lambda engine, est: (True, ""))
    monkeypatch.setattr(AB, "record_call", lambda engine, **kw: 0.0004)
    monkeypatch.setattr(C, "_today_spent", lambda engine: 0.0)
    monkeypatch.setattr(C, "_add_spent", lambda cost: None)
    monkeypatch.setattr(PP, "save_rows", lambda engine, rows, mid=None: len(rows))
    return C


def test_the_checker_reads_a_boost_and_answers_with_the_history(boost_checker, monkeypatch):
    C = S = boost_checker
    import src.utils.team_normalizer as tn
    monkeypatch.setattr(tn, "normalize_team", lambda x: str(x).lower().replace("rb ", ""))
    monkeypatch.setattr(CO, "history_probability", lambda engine, legs, fh, fa, fo, league:
                        {"p": 0.3443, "n": 1420, "n_league": 50, "tol": 0.05, "p_all": 0.34})
    client = FakeClaude(json.dumps({"items": [_boost(GOALS_CORNERS)]}))
    out = C.handle_message(PHOTO, "TOKEN", "555", engine=object(), client=client, http=FakeHttp(), fixtures=[RM])
    assert "✅ vale: sale 34 de cada 100 en 1,420 partidos parecidos" in out
    assert "patas" in S.SYSTEM and "corners_total" in S.SYSTEM and "momio_antes" in S.SYSTEM
    assert client.calls[0]["max_tokens"] == 6000
    assert "Boosts y combinadas" in S.HELP
