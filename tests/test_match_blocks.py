"""
20-nov-26: los partidos en Telegram por bloques (src/models/match_blocks.py). El dueño: "necesito que cada que
mires el partido se mueve mandes ese partido no que me actualices el mismo mensaje … vayas mandando los partidos
por bloques … tampoco quiero el mensaje de no hay partidos recomendados … dime cuál es recomendado y cuál no …
de las 12 pm para abajo mándame unos y de las 12 para arriba otros … si el partido ya tiene alineación ponlo con
una palomita en verde". Eligió 1a, 2a, los dos bloques del día y al canal solo los partidos con alineación.
"""

import inspect
from zoneinfo import ZoneInfo

import pandas as pd

from src.models import match_blocks as MB

MX = ZoneInfo("America/Mexico_City")
UTC = lambda s: pd.Timestamp(s, tz="UTC").to_pydatetime()
SIX = UTC("2026-11-21 12:00")            # sábado 06:00 de México
NOON = UTC("2026-11-21 18:00")           # sábado 12:00 de México


def _row(match, kick, market="home_win", reason=None, lineup=None, league="soccer_germany_bundesliga",
         label=None, min_us=-250, display=None):
    return {"match": match, "display": display, "match_date": pd.Timestamp(kick, tz="UTC"), "league": league,
            "label": label or {"home_win": "Gana el local", "under25": "Menos de 2.5 goles"}.get(market, market),
            "family": "Ganador", "market": market, "reason": reason, "min_us": min_us, "lineup": lineup,
            "form_note": None, "result": None}


DAY = [_row("bayern munich vs fc koln", "2026-11-21 14:30", display="Bayern Munich vs 1. FC Köln"),
       _row("leeds vs fulham", "2026-11-21 15:00", market="under25", reason="No es el muy favorito",
            league="soccer_epl", display="Leeds vs Fulham"),
       _row("america vs pumas", "2026-11-22 03:00", market="under25", reason="x", league="soccer_mexico_ligamx",
            display="América vs Pumas"),
       _row("vissel kobe vs kashima", "2026-11-22 05:00", market="under25", reason="x",
            league="soccer_japan_j_league", display="Vissel Kobe vs Kashima")]   # 23:00 sábado de México


def test_the_morning_block_has_every_match_before_noon_marked_recommended_or_not():
    pl = MB.plan(DAY, {}, SIX, MX)
    assert [r["match"] for r in pl["am"]] == ["bayern munich vs fc koln", "leeds vs fulham"]
    assert pl["pm"] is None                                   # el de la tarde sale a las 12:00
    text, = MB.day_block_texts(SIX.astimezone(MX).date(), "am", pl["am"], [], MX)
    assert text.startswith("☀️ <b>Partidos de hoy · antes de las 12:00</b>")
    assert "sábado 21 nov · 2 partidos, 1 🎯 recomendado" in text
    assert "🎯 <b>08:30</b> Bayern Munich vs 1. FC Köln" in text
    assert "<b>RECOMENDADO</b> · Gana Bayern Munich ⚡ · apuesta si PlayDoit paga <b>-250</b> o más" in text
    assert "<b>09:00</b> Leeds vs Fulham\n   Menos de 2.5 goles · solo para seguir" in text
    assert "no hay recomendados" not in text.lower() and "No hace falta apostar" not in text
    # una vez al día
    again = MB.plan(DAY, pl["state"], UTC("2026-11-21 12:30"), MX)
    assert again["am"] is None and again["pm"] is None


def test_the_afternoon_block_has_the_rest_until_six_the_next_morning():
    st = MB.plan(DAY, {}, SIX, MX)["state"]
    pl = MB.plan(DAY, st, NOON, MX)
    assert [r["match"] for r in pl["pm"]] == ["america vs pumas", "vissel kobe vs kashima"]
    text, = MB.day_block_texts(NOON.astimezone(MX).date(), "pm", pl["pm"], [], MX)
    assert text.startswith("🌤️ <b>Partidos de hoy · de las 12:00 en adelante</b>")
    assert "<b>21:00</b> América vs Pumas" in text and "<b>23:00</b> Vissel Kobe vs Kashima" in text
    late = _row("ulsan vs pohang", "2026-11-22 08:00", reason="x", league="soccer_korea_kleague1",
                display="Ulsan vs Pohang")                    # 02:00 del domingo de México
    pl = MB.plan(DAY + [late], st, NOON, MX)
    text, = MB.day_block_texts(NOON.astimezone(MX).date(), "pm", pl["pm"], [], MX)
    assert "<b>02:00</b> (domingo) Ulsan vs Pohang" in text


def test_a_match_with_its_lineup_is_sent_once_with_a_green_check_and_what_changed():
    st = MB.plan(DAY, {}, SIX, MX)["state"]
    now = UTC("2026-11-21 13:31")                             # 07:31 de México, 1 h antes del Bayern
    moved = [{**DAY[0], "lineup": "confirmada", "label": "Más de 2.5 goles", "market": "over25",
              "reason": "No es el muy favorito"}] + DAY[1:]
    pl = MB.plan(moved, st, now, MX)
    assert [i["row"]["match"] for i in pl["lineup"]] == ["bayern munich vs fc koln"] and pl["final"] == []
    text, = MB.lineup_texts(pl["lineup"], MX, {"bayern munich vs fc koln": ["📋 A 1. FC Köln le faltan 4 titulares"]})
    assert text.startswith("📋 <b>Con alineación · 08:30</b>")
    assert "<b>08:30</b> Bayern Munich vs 1. FC Köln ✅" in text
    assert "Más de 2.5 goles · solo para seguir" in text
    assert "🔄 antes: Gana el local (🎯 recomendado)" in text and "le faltan 4 titulares" in text
    # ya mandado: no se repite si no cambia
    assert MB.plan(moved, pl["state"], UTC("2026-11-21 14:01"), MX)["lineup"] == []


def test_the_last_review_sends_what_changed_after_and_what_never_had_a_lineup():
    st = MB.plan(DAY, {}, SIX, MX)["state"]
    with_lineup = [{**DAY[0], "lineup": "confirmada"}] + DAY[1:]
    st = MB.plan(with_lineup, st, UTC("2026-11-21 13:31"), MX)["state"]
    # 14:01 UTC: última revisión del Bayern (14:30); el pick cambió después de mandarse
    changed = [{**with_lineup[0], "reason": "Alineación: x", "label": "Gana el local"}] + DAY[1:]
    pl = MB.plan(changed, st, UTC("2026-11-21 14:01"), MX)
    assert [(i["kind"], i["row"]["match"]) for i in pl["final"]] == [("changed", "bayern munich vs fc koln")]
    text, = MB.final_texts(pl["final"], MX, {})
    assert text.startswith("⏰ <b>Último aviso · 08:30</b>")
    assert "🔄 <b>08:30</b> Bayern Munich vs 1. FC Köln ✅ · <i>cambió a última hora</i>" in text
    assert "Gana Bayern Munich · solo para seguir" in text and "🔄 antes: Gana el local (🎯 recomendado)" in text
    # 14:31: el Leeds (15:00) nunca tuvo alineación: es su pick final
    pl = MB.plan(changed, pl["state"], UTC("2026-11-21 14:31"), MX)
    assert [(i["kind"], i["row"]["match"]) for i in pl["final"]] == [("nolineup", "leeds vs fulham")]
    text, = MB.final_texts(pl["final"], MX, {})
    assert "⏳ sin alineación a tiempo: este es su pick final" in text
    assert MB.plan(changed, pl["state"], UTC("2026-11-21 14:45"), MX)["final"] == []      # una sola vez


def test_the_blocks_go_to_the_owner_and_only_the_matches_to_the_channel(monkeypatch):
    sent, shared, unpinned, saved = [], [], [], {}
    state = {"telegram_today": {"day": "2026-11-20", "ids": [101]}}

    def read_sql(sql, engine, params=None):
        s = str(sql)
        if "FROM program_picks" in s:
            return pd.DataFrame([{**DAY[0], "lineup": "confirmada"}] + DAY[1:])
        if "FROM lineup_alerts" in s:
            return pd.DataFrame(columns=["home", "away", "kickoff", "weakened", "missing"])
        return pd.DataFrame()
    monkeypatch.setattr(MB.pd, "read_sql", read_sql)
    monkeypatch.setattr(MB.TR, "load_parlays", lambda engine, day: [])
    out = MB.run_match_blocks(engine=object(), now=SIX, send=lambda t: sent.append(t) or True,
                              share=lambda t, what="": shared.append(t) or True,
                              load_state=lambda k: (saved.get(k) or state.get(k)),
                              save_state=lambda k, v, history=False: saved.__setitem__(k, v),
                              unpin=lambda mid, chat_id=None: unpinned.append((mid, chat_id)) or True)
    assert out["am"] == 2 and out["lineup"] == 1
    assert sent[0].startswith("☀️ <b>Partidos de hoy · antes de las 12:00</b>")       # solo al dueño
    assert sent[1].startswith("📋 <b>Con alineación")
    assert shared == [sent[1]]                                                        # al canal, solo partidos
    assert unpinned == [(101, None)] and saved["telegram_today"]["retired"] is True   # el tablero de antes
    assert saved[MB.STATE_KEY]["days"] == {"am": "2026-11-21"}


def test_nothing_says_there_are_no_targets_and_nothing_is_edited_or_pinned():
    texts = "".join(inspect.getsource(f) for f in (MB.match_lines, MB.day_block_texts, MB.lineup_texts,
                                                   MB.final_texts)) + MB.LEGEND
    assert "no hay recomendados" not in texts and "No hace falta apostar" not in texts
    run = inspect.getsource(MB.run_match_blocks)
    assert "edit_or_send" not in run and "nt.pin_message" not in run and "unpin_message" in run
    from scripts import orchestrator as O
    closing = inspect.getsource(O.run_closing)
    assert "step_match_blocks" in closing and "step_today_board" not in closing
    assert "step_notify" not in inspect.getsource(O.run_morning)
