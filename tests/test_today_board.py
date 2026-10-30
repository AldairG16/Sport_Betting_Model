"""
tests/test_today_board.py
=========================
"Partidos de hoy" en Telegram (30-oct-26). El dueño: "yo quiero los partidos
que vamos teniendo y poder actualizarlos, por si en algún momento cambia el
pick poder notarlo"; "que lleven hora, para saber cuándo empiezan y así poder
ver su última actualización".
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import today_board as TB

MX = ZoneInfo("America/Mexico_City")
NOW = datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc)          # 13:30 en México


def _r(display, ko_h, label="Gana el local", reason=None, min_us=120, family="Ganador", odds=None,
       event=None, prev=None, prev_star=None, changed_h=None, lineup=None):
    return {"match": display.lower(), "display": display, "match_date": NOW + timedelta(hours=ko_h),
            "label": label, "family": family, "market": "x" if label else None, "reason": reason,
            "min_us": min_us, "lineup": lineup, "prev_label": prev, "prev_star": prev_star,
            "changed_at": NOW - timedelta(hours=changed_h) if changed_h is not None else None,
            "odds_fetched_at": odds, "specialty_fetched_at": event}


def test_each_match_has_its_kickoff_its_pick_and_when_its_odds_are_from():
    rows = [_r("Arsenal vs Chelsea", 1.5, odds=NOW - timedelta(minutes=30)),
            _r("Milan & Bologna", 3, label="Ambos anotan", reason="Sin ventaja: igual", min_us=None,
               family="Ambos anotan", odds=NOW - timedelta(minutes=30),
               event=datetime(2026, 10, 28, 9, 31, tzinfo=timezone.utc)),
            _r("Ya Empezó vs Otro", -1),
            _r("América vs Tijuana", 6, label=None, reason="El programa ya no lo evalúa: poca historia",
               odds=NOW - timedelta(hours=1))]
    text = "\n".join(TB.board_texts(rows, NOW, MX))
    assert "📋 <b>Partidos de hoy</b> · Viernes 30 oct" in text and "Actualizado 13:30" in text
    assert "<b>15:00</b> Arsenal vs Chelsea" in text and "🎯 Gana el local · mín. +120 · cuotas 13:00" in text
    assert "Milan &amp; Bologna" in text                                    # HTML escapado
    assert "Ambos anotan · cuotas 28 oct 03:31" in text                     # cuotas del evento, de otro día
    assert "🎯 Ambos anotan" not in text                                    # no pasa la regla: sin 🎯
    assert "Sin pick · El programa ya no lo evalúa" in text
    assert "Ya Empezó" not in text and "Ya empezaron: 1" in text


def test_a_changed_pick_says_what_it_was_and_when():
    rows = [_r("Arsenal vs Chelsea", 2, label="Más de 2.5 goles", family="Goles", min_us=-110,
               odds=NOW, prev="Gana el local", prev_star=True, changed_h=0.5,
               lineup="confirmada: Chelsea con 4 fuera")]
    text = TB.board_texts(rows, NOW, MX)[0]
    assert "Arsenal vs Chelsea 🔄" in text and "alineación ✓" in text
    assert "🔄 cambió 13:00 · antes: 🎯 Gana el local" in text
    old = [_r("Arsenal vs Chelsea", 2, prev="X", prev_star=False, changed_h=30, odds=NOW)]
    assert "🔄" not in TB.board_texts(old, NOW, MX)[0]                       # cambió hace más de 24 h


def test_a_long_day_is_split_in_messages_that_fit_telegram():
    rows = [_r(f"Equipo Local Número {i} vs Equipo Visitante Número {i}", 1 + i * 0.1, odds=NOW)
            for i in range(90)]
    texts = TB.board_texts(rows, NOW, MX)
    assert len(texts) > 1 and all(len(t) <= TB.CHUNK_CHARS for t in texts)
    assert sum(t.count("<b>1") + t.count("<b>2") for t in texts) >= 90 - 5   # todos los partidos salen
    assert texts[1].startswith("📋 <b>Partidos de hoy</b> (sigue)")


def test_only_new_changes_of_what_kicks_off_soon_are_alerted():
    rows = [_r("Pronto vs A", 2, changed_h=0.2, prev="Gana el local"),
            _r("Mañana vs B", 30, changed_h=0.2, prev="Gana el local"),          # lejos: solo en la lista
            _r("Viejo vs C", 2, changed_h=3, prev="Gana el local"),              # ya avisado
            _r("Sin cambio vs D", 2)]
    got = TB.changes_to_alert(rows, NOW - timedelta(hours=1), NOW)
    assert [r["display"] for r in got] == ["Pronto vs A"]
    msg = TB.change_alert(got, NOW, MX)
    assert msg.startswith("🔄 <b>Cambió el pick</b>") and "Antes: Gana el local" in msg
    assert "Ahora: 🎯 Gana el local · mín. +120" in msg and "<b>15:30</b> Pronto vs A" in msg   # ganó el 🎯


class _World:
    def __init__(self, monkeypatch, rows):
        self.rows, self.state, self.edits, self.sent, self.pins = rows, {}, [], [], []
        self.next_id = 100
        monkeypatch.setattr(pd, "read_sql", lambda sql, con=None, params=None, **kw: pd.DataFrame(self.rows))

    def edit(self, text, mid=None, silent=True):
        self.edits.append((mid, text))
        if mid:
            return mid
        self.next_id += 1
        return self.next_id

    def run(self, now):
        return TB.run_today_board(engine=object(), now=now, edit=self.edit, send=self.sent.append,
                                  pin=lambda mid, unpin=None: self.pins.append((mid, unpin)),
                                  load_state=self.state.get,
                                  save_state=lambda k, v, **kw: self.state.__setitem__(k, v))


def test_the_message_is_edited_all_day_and_a_new_one_starts_each_day(monkeypatch):
    w = _World(monkeypatch, [_r("Arsenal vs Chelsea", 3, odds=NOW)])
    w.run(NOW)
    assert w.edits[-1][0] is None and w.pins == [(101, [])]                  # el primero del día, fijado
    w.run(NOW + timedelta(minutes=30))
    assert w.edits[-1][0] == 101 and len(w.pins) == 1                        # misma día: se edita
    w.run(NOW + timedelta(hours=12))                                         # 01:30 del día siguiente
    assert w.edits[-1][0] is None and w.pins[-1] == (102, [101])             # nuevo, fija y quita el de ayer
    assert w.state[TB.BOARD_KEY]["ids"] == [102]


def test_a_change_is_alerted_once(monkeypatch):
    w = _World(monkeypatch, [_r("Arsenal vs Chelsea", 2, prev="Gana el visitante", changed_h=0.1, odds=NOW)])
    w.run(NOW)
    assert len(w.sent) == 1 and "Antes: Gana el visitante" in w.sent[0]
    w.run(NOW + timedelta(minutes=30))
    assert len(w.sent) == 1                                                  # ya avisado


def test_the_closing_runs_the_board_after_the_picks_and_their_alerts():
    import inspect
    import scripts.orchestrator as O
    src = inspect.getsource(O)
    i = src.index('run_step(logger, "Picks del programa"')
    j = src.index('run_step(logger, "Alertas de alineación"')
    k = src.index('run_step(logger, "Partidos de hoy (Telegram)"')
    assert i < j < k


def test_the_live_snapshot_records_when_a_pick_changes():
    from src.models import program_picks as PP

    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, stmt, params=None):
            self.sql.append(str(stmt))
    row = {c: None for c in PP._COLS + ("source",)}
    live, rebuilt = Conn(), Conn()
    PP._upsert(live, [row], "TRUE", track_changes=True)
    PP._upsert(rebuilt, [row], "TRUE")
    assert "changed_at = CASE WHEN" in live.sql[0] and "prev_label = CASE WHEN" in live.sql[0]
    assert "changed_at" not in rebuilt.sql[0]                                # reconstruir no es "cambiar"
    assert any("changed_at TIMESTAMPTZ" in a for a in PP.PICKS_ALTERS)
