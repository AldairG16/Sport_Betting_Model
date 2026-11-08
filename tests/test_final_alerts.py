"""
Los picks finales en Telegram (v2.11.0, 8-nov-26).

El dueño: "actualmente me llegan muchas alertas de cambios de pick, a veces
también son 2 veces antes del partido, y todo eso se me pierde en el telegram,
quiero que solo llegue el último cambio, necesito que revises cuánto tiempo
antes del partido cambia el pick para que ese sea el límite de mandar el pick
al telegram, y si hay parley también mandarlo". Eligió solo los 🎯 y los
parlays (con aviso si uno de la mañana dejó de ser 🎯) y la última revisión
antes del partido. Sin DB ni Telegram.
"""

import inspect
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.models import final_alerts as FA
from src.utils.log import RUN_ISSUES

MX = ZoneInfo("America/Mexico_City")
NOW = datetime(2026, 11, 8, 18, 31, 30, tzinfo=timezone.utc)          # 12:31 en México, la revisión de las 12:30
COVERED = {"soccer_germany_bundesliga", "soccer_italy_serie_a"}
JARGON = ("ROI", "Profit", "unidades", "pasa la regla", "1u", "Cambió el pick")


def _t(hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"2026-11-08 {hhmm}", tz="UTC")


def _p(match, ko, market="home_win", reason=None, star_note=None, min_us=-308, lineup=None,
       league="soccer_germany_bundesliga", label=None, result=None):
    h, a = match.split(" vs ")
    return {"match": match.lower(), "display": match, "match_date": _t(ko), "league": league,
            "label": label or {"home_win": "Gana el local", "away_win": "Gana el visitante"}.get(market, market),
            "family": "Ganador", "market": market, "reason": reason, "star_note": star_note, "min_us": min_us,
            "lineup": lineup, "result": result}


def _leg(rank, match, slot="19:00", kind="horario", seq=1, lineup="confirmada", odds=1.45):
    return {"slot": _t(slot), "rank": rank, "kind": kind, "seq": seq, "match": match, "display": match.title(),
            "match_date": _t(slot), "league": "soccer_italy_serie_a",
            "label": f"Gana {match.split(' vs ')[0].title()}", "market": "home_win", "model_prob": 0.72,
            "market_prob": 0.70, "playdoit_odds": odds, "lineup": lineup, "pa": True}


TORINO_OUT = "Alineación: a Torino le faltan 5 titulares habituales (A, B, C, D, E)"


def _world():
    picks = [_p("Dortmund vs Elversberg", "19:00", lineup="confirmada: completo"),
             _p("Leipzig vs Augsburg", "18:45", min_us=-234),                         # 🎯 nuevo, sin alineación
             _p("Torino vs Lecce", "19:00", reason=TORINO_OUT, league="soccer_italy_serie_a"),   # dejó de ser 🎯
             _p("Parma vs Bologna", "19:00", market="under_2.5", label="Menos de 2.5 goles",
                reason="Solo 🎯 al favorito", star_note="No hay favorito claro: x"),   # 🤖 de siempre: nada
             _p("Roma vs Sassuolo", "19:15"),                                         # lo toca la de las 13:00
             _p("Ya vs Empezó", "18:20"),                                             # ya empezó
             _p("Viejo vs Horario", "19:00", result="stale")]                         # reprogramado
    legs = [_leg(1, "napoli vs lecce"), _leg(2, "inter vs genoa"), _leg(3, "milan vs como"),
            _leg(1, "a vs b", slot="20:00", kind="dia"), _leg(2, "c vs d", slot="20:00", kind="dia"),
            _leg(3, "e vs f", slot="20:00", kind="dia"), _leg(4, "g vs h", slot="20:00", kind="dia")]
    announced = {"keys": {FA.key_of(picks[0]): "Gana el local", FA.key_of(picks[2]): "Gana el local"},
                 "days": ["2026-11-08"]}
    return picks, legs, announced


# ─── cuándo ──────────────────────────────────────────────────────────────────

def test_the_limit_is_the_last_regular_revision_before_kickoff():
    # la revisión de las 12:30 (México) es la última para lo que empieza hasta las 13:05
    assert FA.cutoff(NOW) == _t("19:05").to_pydatetime()
    assert FA.cutoff(datetime(2026, 11, 8, 18, 1, tzinfo=timezone.utc)) == _t("18:35").to_pydatetime()
    # una corrida de más (los cron de GitHub, 12:54) no adelanta nada: lo de las 13:15 lo hace la de las 13:00
    assert FA.cutoff(datetime(2026, 11, 8, 18, 54, tzinfo=timezone.utc)) == _t("19:05").to_pydatetime()


# ─── qué ─────────────────────────────────────────────────────────────────────

def test_only_targets_their_losses_and_the_parlay_that_locks_now():
    picks, legs, announced = _world()
    items, st = FA.plan(picks, legs, {}, announced, NOW, COVERED, MX)
    got = [(i["kind"], i["row"]["display"] if "row" in i else i["legs"][0]["match"]) for i in items]
    assert got == [("star", "Leipzig vs Augsburg"), ("star", "Dortmund vs Elversberg"), ("lost", "Torino vs Lecce"),
                   ("parlay", "napoli vs lecce")]
    assert items[0]["new"] and not items[1]["new"] and not any(i["late"] for i in items)
    # cada partido a tiempo queda anotado, también el 🤖 que no se dice; el parlay del día (14:00) todavía no
    assert sorted(k.split("|")[0] for k in st["m"]) == ["dortmund vs elversberg", "leipzig vs augsburg",
                                                        "parma vs bologna", "torino vs lecce"]
    assert list(st["p"]) == [f"{_t('19:00').isoformat()}|horario|1"]
    # la siguiente corrida no repite nada
    assert FA.plan(picks, legs, st, announced, NOW, COVERED, MX)[0] == []


def test_a_change_after_the_final_message_is_sent_as_the_last_change():
    picks, legs, announced = _world()
    _, st = FA.plan(picks, legs, {}, announced, NOW, COVERED, MX)
    later = datetime(2026, 11, 8, 18, 54, tzinfo=timezone.utc)                     # corrida de más
    picks[0] = {**picks[0], "reason": "Alineación: a Dortmund le faltan 4 titulares habituales (a, b, c, d)"}
    picks[3] = {**picks[3], "reason": None, "market": "home_win", "label": "Gana el local"}   # ahora es 🎯
    legs[2] = {**legs[2], "match": "lazio vs cagliari"}
    items, _ = FA.plan(picks, legs, st, announced, later, COVERED, MX)
    assert [(i["kind"], i["late"]) for i in items] == [("lost", True), ("star", True), ("parlay", True)]
    assert items[0]["row"]["display"] == "Dortmund vs Elversberg" and items[1]["row"]["display"] == "Parma vs Bologna"


def test_new_is_said_only_on_a_day_whose_morning_message_was_saved():
    picks, legs, _ = _world()
    items, _ = FA.plan(picks, [], {}, {}, NOW, COVERED, MX)                     # el primer día: sin mensaje guardado
    assert [i["kind"] for i in items] == ["star", "star"] and not any(i["new"] for i in items)
    items, _ = FA.plan(picks, [], {}, {"keys": {}, "days": ["2026-11-08"]}, NOW, COVERED, MX)
    assert all(i["new"] for i in items)                                          # la mañana dijo "no hay 🎯"


# ─── cómo se lee ─────────────────────────────────────────────────────────────

def test_the_message_says_the_final_pick_who_is_missing_and_the_parlay_in_plain_words():
    picks, legs, announced = _world()
    items, _ = FA.plan(picks, legs, {}, announced, NOW, COVERED, MX)
    missing = FA.missing_lines([
        {"home": "torino", "away": "lecce", "kickoff": _t("19:00"), "weakened": "torino", "market": m,
         "missing": ["A", "B", "C", "D", "E", "F"]} for m in ("away_win", "dc_x2")])
    (msg,) = FA.final_texts(items, missing, MX, COVERED)
    assert msg.startswith("⏰ <b>Picks finales · 12:45 y 13:00</b>\n<i>Es la última revisión antes del partido")
    assert ("🎯 <b>12:45</b> Leipzig vs Augsburg · <i>nuevo: no estaba en el mensaje de la mañana</i>\n"
            "   Gana Leipzig ⚡ · apuesta si PlayDoit paga <b>-234</b> o más\n"
            "   ⏳ todavía sin alineación: revísala antes de apostar") in msg
    assert ("🎯 <b>13:00</b> Dortmund vs Elversberg\n   Gana Dortmund ⚡ · apuesta si PlayDoit paga <b>-308</b> o más\n"
            "   📋 ya revisado con la alineación") in msg
    assert ("❌ <b>13:00</b> Torino vs Lecce\n   Ya no es 🎯: a Torino le faltan 5 titulares habituales. No lo apuestes.\n"
            "   📋 A Torino le faltan 6 titulares: A, B, C, D, E…") in msg
    assert msg.count("📋 A Torino") == 1                                         # una línea aunque sean dos mercados
    assert "Parlay de las 13:00" in msg and "Paga <b>+" in msg and "Parma" not in msg
    assert msg.count("⚡ = pago anticipado") == 1                                 # la del parlay, no dos
    assert not re.search(r"\b\d\.\d{2}\b", msg)                                  # momios americanos, nunca decimales
    for j in JARGON:
        assert j not in msg, j


def test_a_late_change_and_a_long_message_read_right():
    picks, legs, announced = _world()
    _, st = FA.plan(picks, legs, {}, announced, NOW, COVERED, MX)
    picks[0] = {**picks[0], "reason": "Región América en pausa", "star_note": "No hay favorito claro: x"}
    items, _ = FA.plan(picks, [], st, announced, NOW, COVERED, MX)
    (msg,) = FA.final_texts(items, {}, MX, COVERED)
    assert "🔄 ❌ <b>13:00</b> Dortmund vs Elversberg\n   Ya no es 🎯: no hay favorito claro. No lo apuestes." in msg
    assert "⚡ =" not in msg                                                     # sin ⚡ en el mensaje, sin nota
    (one,) = FA.final_texts([{"kind": "star", "row": picks[1], "new": False, "late": False}], {}, MX, COVERED)
    assert one.endswith("<i>⚡ = pago anticipado: si el equipo va 2 goles arriba, PlayDoit la paga como ganada.</i>")
    many = [{"kind": "star", "row": _p(f"Equipo Local {i} vs Equipo Visitante {i}", "19:00"), "new": False,
             "late": False} for i in range(60)]
    texts = FA.final_texts(many, {}, MX, COVERED)
    assert len(texts) > 1 and all(len(t) <= FA.CHUNK_CHARS for t in texts)
    assert texts[1].startswith("⏰ <b>Picks finales</b> (sigue)")
    assert FA.final_texts([], {}, MX) == []                                      # nada que decir: sin mensaje


def test_why_it_is_no_longer_a_target():
    assert FA.why_not({"reason": TORINO_OUT}) == "a Torino le faltan 5 titulares habituales"
    assert FA.why_not({"reason": "x", "star_note": "No hay favorito claro: el 🎯 es…"}) == "no hay favorito claro"
    assert FA.why_not({"reason": "x", "star_note": "Napoli paga muy poco (-650): es muy favorito"}) == \
        "Napoli paga muy poco (-650)"
    assert FA.why_not({}) == "cambiaron los momios"


# ─── el mensaje de la mañana deja su lista ───────────────────────────────────

def test_the_morning_message_leaves_its_targets_and_its_day():
    state = {FA.ANNOUNCED_KEY: {"keys": {"viejo vs partido|2026-11-01T19:00:00+00:00": "x"},
                                "days": ["2026-11-01", "2026-11-07"]}}
    picks, _, _ = _world()
    n = FA.remember_announced(pd.Timestamp("2026-11-08").date(), picks[:1], now=NOW, load_state=state.get,
                              save_state=lambda k, v, history=True: state.__setitem__(k, v))
    assert n == 1
    assert state[FA.ANNOUNCED_KEY] == {"keys": {FA.key_of(picks[0]): "Gana el local"},
                                       "days": ["2026-11-07", "2026-11-08"]}       # lo de hace más de 2 días se va
    FA.remember_announced(pd.Timestamp("2026-11-09").date(), [], now=NOW, load_state=state.get,
                          save_state=lambda k, v, history=True: state.__setitem__(k, v))
    assert state[FA.ANNOUNCED_KEY]["days"][-1] == "2026-11-09"                  # un día sin 🎯 también cuenta


# ─── la corrida ──────────────────────────────────────────────────────────────

def _fake_db(monkeypatch, picks, legs, lineups):
    def read_sql(sql, engine=None, params=None, **kw):
        s = str(sql)
        if "FROM program_parlays" in s:
            return pd.DataFrame(legs)
        if "FROM lineup_alerts" in s:
            return pd.DataFrame(lineups, columns=["home", "away", "kickoff", "weakened", "missing"])
        return pd.DataFrame(picks)
    monkeypatch.setattr(FA.pd, "read_sql", read_sql)


def test_the_closing_sends_once_and_retries_if_telegram_fails(monkeypatch):
    picks, legs, announced = _world()
    _fake_db(monkeypatch, picks, legs, [])
    state = {FA.ANNOUNCED_KEY: announced}
    kw = dict(engine=object(), now=NOW, load_state=state.get, covered=COVERED,
              save_state=lambda k, v, history=True: state.__setitem__(k, v))
    RUN_ISSUES.reset()
    out = FA.run_final_alerts(send=lambda t: False, **kw)
    assert out["messages"] == 0 and FA.STATE_KEY not in state                   # no quedó como mandado
    assert any("no se mandaron a Telegram" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()
    sent = []
    out = FA.run_final_alerts(send=lambda t: sent.append(t) or True, **kw)
    assert out == {"star": 2, "lost": 1, "parlay": 1, "messages": 1} and len(sent) == 1
    assert FA.run_final_alerts(send=lambda t: sent.append(t) or True, **kw)["messages"] == 0   # una sola vez
    assert len(sent) == 1


def test_the_closing_sends_only_the_final_message():
    import scripts.orchestrator as O
    from src.models import lineup_alerts as LA
    from src.models import parlay_alerts as PA
    src = inspect.getsource(O.run_closing)
    assert src.index('"Picks del programa"') < src.index('"Alertas de alineación"') < \
        src.index('"Partidos de hoy (Telegram)"') < src.index('"Picks finales (Telegram)"')
    assert "Parlay listo" not in src and "run_final_alerts()" in inspect.getsource(O.step_final_alerts)
    # la alerta de alineación se guarda (el dashboard la muestra) pero no sale aparte
    assert "notify=False" in inspect.getsource(O.step_lineup_alerts)
    assert "if notify:" in inspect.getsource(LA.run_lineup_alerts)
    assert not hasattr(PA, "run_parlay_alerts") and not hasattr(PA, "to_alert")
