"""
Los parlays finales en Telegram (v2.11.0, 8-nov-26; desde el 20-nov-26 solo los parlays, al dueño).

El dueño, el 8-nov: "quiero que solo llegue el último cambio … y si hay parley también mandarlo". Desde el 20-nov
los partidos (🎯 o no) van por bloques (src/models/match_blocks.py, tests/test_match_blocks.py); aquí quedan los
parlays, en la última revisión antes de su primer partido, y lo que match_blocks usa de aquí (cutoff,
missing_lines, form_line). Sin DB ni Telegram.
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


def _leg(rank, match, slot="19:00", kind="horario", seq=1, lineup="confirmada", odds=1.45):
    return {"slot": _t(slot), "rank": rank, "kind": kind, "seq": seq, "match": match, "display": match.title(),
            "match_date": _t(slot), "league": "soccer_italy_serie_a",
            "label": f"Gana {match.split(' vs ')[0].title()}", "market": "home_win", "model_prob": 0.72,
            "market_prob": 0.70, "playdoit_odds": odds, "lineup": lineup, "pa": True}


def _legs():
    return [_leg(1, "napoli vs lecce"), _leg(2, "inter vs genoa"), _leg(3, "milan vs como"),
            _leg(1, "a vs b", slot="20:00", kind="dia"), _leg(2, "c vs d", slot="20:00", kind="dia"),
            _leg(3, "e vs f", slot="20:00", kind="dia"), _leg(4, "g vs h", slot="20:00", kind="dia")]


# ─── cuándo ──────────────────────────────────────────────────────────────────

def test_the_limit_is_the_last_regular_revision_before_kickoff():
    # la revisión de las 12:30 (México) es la última para lo que empieza hasta las 13:05
    assert FA.cutoff(NOW) == _t("19:05").to_pydatetime()
    assert FA.cutoff(datetime(2026, 11, 8, 18, 1, tzinfo=timezone.utc)) == _t("18:35").to_pydatetime()
    # una corrida de más (los cron de GitHub, 12:54) no adelanta nada: lo de las 13:15 lo hace la de las 13:00
    assert FA.cutoff(datetime(2026, 11, 8, 18, 54, tzinfo=timezone.utc)) == _t("19:05").to_pydatetime()


# ─── qué ─────────────────────────────────────────────────────────────────────

def test_only_the_parlay_that_locks_now_and_once():
    items, st = FA.plan(_legs(), {}, NOW, COVERED)
    assert [(i["kind"], i["legs"][0]["match"], i["late"]) for i in items] == [("parlay", "napoli vs lecce", False)]
    assert list(st["p"]) == [f"{_t('19:00').isoformat()}|horario|1"]           # el del día (14:00) todavía no
    assert FA.plan(_legs(), st, NOW, COVERED)[0] == []                           # la siguiente corrida no repite
    later = datetime(2026, 11, 8, 18, 54, tzinfo=timezone.utc)                   # corrida de más, cambió una pierna
    legs = _legs()
    legs[2] = {**legs[2], "match": "lazio vs cagliari"}
    assert [i["late"] for i in FA.plan(legs, st, later, COVERED)[0]] == [True]


def test_the_message_reads_in_plain_words_and_splits_when_long():
    items, _ = FA.plan(_legs(), {}, NOW, COVERED)
    (msg,) = FA.final_texts(items, MX)
    assert msg.startswith("⏰ <b>Parlays finales · 13:00</b>\n<i>Es la última revisión antes de su primer partido")
    assert "Parlay de las 13:00" in msg and "Paga <b>+" in msg
    assert not re.search(r"\b\d\.\d{2}\b", msg)                                  # momios americanos, nunca decimales
    for j in JARGON:
        assert j not in msg, j
    assert FA.final_texts([], MX) == []                                          # nada que decir: sin mensaje
    many = [{"kind": "parlay", "legs": [_leg(r, f"local {i}{r} vs visita {i}{r}", seq=i) for r in range(1, 6)],
             "late": False, "ready": True} for i in range(25)]
    texts = FA.final_texts(many, MX)
    assert len(texts) > 1 and all(len(t) <= FA.CHUNK_CHARS for t in texts)
    assert texts[1].startswith("⏰ <b>Parlays finales</b> (sigue)")


def test_who_is_missing_is_one_line_per_team():
    missing = FA.missing_lines([
        {"home": "torino", "away": "lecce", "kickoff": _t("19:00"), "weakened": "torino", "market": m,
         "missing": ["A", "B", "C", "D", "E", "F"]} for m in ("away_win", "dc_x2")])
    assert missing == {"torino vs lecce": ["📋 A Torino le faltan 6 titulares: A, B, C, D, E…"]}


# ─── la corrida ──────────────────────────────────────────────────────────────

def test_the_closing_sends_once_and_retries_if_telegram_fails(monkeypatch):
    monkeypatch.setattr(FA.pd, "read_sql", lambda sql, engine=None, params=None, **kw: pd.DataFrame(_legs()))
    state = {}
    kw = dict(engine=object(), now=NOW, load_state=state.get, covered=COVERED,
              save_state=lambda k, v, history=True: state.__setitem__(k, v))
    RUN_ISSUES.reset()
    out = FA.run_final_alerts(send=lambda t: False, **kw)
    assert out["messages"] == 0 and FA.STATE_KEY not in state                   # no quedó como mandado
    assert any("no se mandaron a Telegram" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()
    sent = []
    out = FA.run_final_alerts(send=lambda t: sent.append(t) or True, **kw)
    assert out == {"parlay": 1, "messages": 1} and len(sent) == 1
    assert FA.run_final_alerts(send=lambda t: sent.append(t) or True, **kw)["messages"] == 0   # una sola vez
    assert len(sent) == 1


def test_the_parlays_go_only_to_the_owner_and_after_the_picks():
    import scripts.orchestrator as O
    from src.models import lineup_alerts as LA
    from src.models import parlay_alerts as PA
    src = inspect.getsource(O.run_closing)
    assert src.index('"Picks del programa"') < src.index('"Alertas de alineación"') < \
        src.index('"Partidos por bloques (Telegram)"') < src.index('"Parlays finales (Telegram)"')
    assert "Parlay listo" not in src and "run_final_alerts()" in inspect.getsource(O.step_final_alerts)
    assert "share" not in inspect.getsource(FA.run_final_alerts)                # el canal recibe solo partidos
    # la alerta de alineación se guarda (el dashboard la muestra) pero no sale aparte
    assert "notify=False" in inspect.getsource(O.step_lineup_alerts)
    assert "if notify:" in inspect.getsource(LA.run_lineup_alerts)
    assert not hasattr(PA, "run_parlay_alerts") and not hasattr(PA, "to_alert")
