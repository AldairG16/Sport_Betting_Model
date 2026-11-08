"""El canal de los partidos (8-nov-26). El dueño: "como puedo unir a personas a
mi chat de telegram donde mandamos los partidos"; eligió "un canal solo para
los partidos". Se guarda reenviándole al bot un mensaje del canal y recibe
una copia de la mañana, los picks finales, la noche y "Partidos de hoy"."""

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from src.models import telegram_channel as TC

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
CANAL = {"id": -1001234567890, "title": "Picks del Aldo", "type": "channel"}


class State(dict):
    def save(self, k, v, history=True):
        self[k] = v


def _forward(chat=CANAL, **extra):
    return {"message_id": 40, "chat": {"id": 555},
            "forward_origin": {"type": "channel", "chat": chat, "message_id": 3}, **extra}


class FakeTg:
    """Telegram para el registro: el bot es (o no) administrador del canal."""

    def __init__(self, status="administrator", can_post=True, can_edit=True, fail_send=False):
        self.calls, self.member = [], {"status": status, "can_post_messages": can_post,
                                       "can_edit_messages": can_edit}
        self.fail_send = fail_send

    def __call__(self, method, **p):
        self.calls.append((method, p))
        if method == "getMe":
            return {"id": 4242, "is_bot": True}
        if method == "getChatMember":
            return self.member
        if method == "sendMessage":
            if self.fail_send:
                raise RuntimeError("Telegram sendMessage: Forbidden: bot is not a member of the channel chat")
            return {"message_id": 1}
        raise AssertionError(method)


# ─── se guarda reenviándole un mensaje del canal ────────────────────────────

def test_a_forwarded_channel_post_is_recognised_in_both_telegram_formats():
    assert TC.forwarded_channel(_forward())["id"] == CANAL["id"]
    old = {"forward_from_chat": CANAL, "chat": {"id": 555}}                # Bot API anterior a la 7.0
    assert TC.forwarded_channel(old)["title"] == "Picks del Aldo"
    assert TC.forwarded_channel({"forward_origin": {"type": "user", "sender_user": {"id": 1}}}) is None
    assert TC.forwarded_channel({"text": "hola"}) is None
    assert TC.is_stop(" Quitar canal ") and TC.is_stop("/quitar_canal") and not TC.is_stop("quitar")


def test_the_channel_is_saved_only_when_the_bot_can_post_there():
    st, tg = State(), FakeTg(status="member")
    out = TC.register(CANAL, tg, st.get, st.save, now=NOW)
    assert out.startswith("⚠️ Todavía no puedo publicar en el canal") and "administrador" in out
    assert TC.KEY not in st and not any(m == "sendMessage" for m, _ in tg.calls)

    tg = FakeTg()
    out = TC.register(CANAL, tg, st.get, st.save, now=NOW)
    assert out.startswith("✅ Listo: los partidos también van a llegar al canal «Picks del Aldo».")
    assert "quitar canal" in out and "enlace de invitación" in out and "Lo técnico" in out
    (welcome,) = [p for m, p in tg.calls if m == "sendMessage"]
    assert welcome["chat_id"] == str(CANAL["id"]) and welcome["text"] == TC.WELCOME
    assert st[TC.KEY]["chat_id"] == "-1001234567890" and st[TC.KEY]["title"] == "Picks del Aldo"
    assert TC.current(st.get)["ok_at"] == NOW.isoformat()

    tg = FakeTg()                                                          # el mismo otra vez: sin otra bienvenida
    assert TC.register(CANAL, tg, st.get, st.save, now=NOW).startswith("✅ El canal «Picks del Aldo» ya estaba")
    assert not any(m == "sendMessage" for m, _ in tg.calls)
    out = TC.register({"id": -100999, "title": "Otro"}, FakeTg(can_edit=False), st.get, st.save, now=NOW)
    assert "ahora los partidos van al canal «Otro» (antes iban a «Picks del Aldo»)" in out
    assert "editar mensajes" in out                                         # sin ese permiso no se fija el tablero


def test_if_the_welcome_cannot_be_posted_nothing_is_saved():
    st = State()
    out = TC.register(CANAL, FakeTg(fail_send=True), st.get, st.save, now=NOW)
    assert out.startswith("⚠️ No pude publicar en el canal: el bot ya no está en el canal") and TC.KEY not in st


def test_quitar_canal_forgets_it():
    st = State({TC.KEY: {"chat_id": "-100", "title": "Picks"}, TC.BOARD_KEY: {"day": "x", "ids": [1]}})
    assert TC.unregister(st.get, st.save).startswith("✅ Ya no mando nada al canal «Picks»")
    assert TC.current(st.get) is None and st[TC.BOARD_KEY] == {}
    assert TC.unregister(st.get, st.save).startswith("No hay ningún canal guardado")


# ─── el revisor de capturas lo reconoce en el chat del dueño ────────────────

class FakeHttp:
    def __init__(self, member=None):
        self.posts, self.member = [], member or {"status": "administrator", "can_post_messages": True,
                                                 "can_edit_messages": True}

    def post(self, url, json=None, timeout=None, headers=None):
        method = url.rsplit("/", 1)[-1]
        self.posts.append((method, json))
        result = {"getMe": {"id": 4242}, "getChatMember": self.member, "sendMessage": {"message_id": 9},
                  "getFile": {"file_path": "photos/f.jpg"}}.get(method)
        return SimpleNamespace(status_code=200, json=lambda r=result: {"ok": True, "result": r})

    def get(self, url, timeout=None):
        return SimpleNamespace(content=b"\xff\xd8\xff fake jpeg", status_code=200)


def test_the_checker_saves_the_channel_from_a_forward_in_the_owner_chat():
    import scripts.playdoit_checker as C
    st, http = State(), FakeHttp()
    out = C.handle_message(_forward(), "TOKEN", "555", engine=object(), http=http, load_state=st.get,
                           save_state=st.save)
    assert out.startswith("✅ Listo") and st[TC.KEY]["chat_id"] == str(CANAL["id"])
    sent = [p for m, p in http.posts if m == "sendMessage"]
    assert sent[0]["chat_id"] == str(CANAL["id"]) and sent[1]["chat_id"] == "555"   # bienvenida y respuesta
    # de otro chat no se guarda nada
    st2 = State()
    assert C.handle_message({**_forward(), "chat": {"id": 999}}, "TOKEN", "555", engine=object(),
                            http=FakeHttp(), load_state=st2.get, save_state=st2.save) is None and not st2
    # «quitar canal»
    out = C.handle_message({"message_id": 41, "chat": {"id": 555}, "text": "quitar canal"}, "TOKEN", "555",
                           engine=object(), http=http, load_state=st.get, save_state=st.save)
    assert out.startswith("✅ Ya no mando nada") and TC.current(st.get) is None
    assert "reenvíame cualquier mensaje de ahí" in C.HELP


def test_a_forwarded_screenshot_is_still_checked_not_taken_as_the_channel(monkeypatch):
    import scripts.playdoit_checker as C
    st, http = State(), FakeHttp()
    monkeypatch.setattr(C, "read_screenshot", lambda img, media, engine=None, client=None: (None, "no se lee"))
    photo = _forward(photo=[{"file_id": "a", "file_size": 10}])
    out = C.handle_message(photo, "TOKEN", "555", engine=object(), http=http, load_state=st.get, save_state=st.save)
    assert out == "⚠️ no se lee" and not st


# ─── las fallas del canal: un aviso al día y el chequeo §19 ─────────────────

def test_a_failing_channel_alerts_the_owner_once_a_day():
    st = State({TC.KEY: {"chat_id": "-100", "title": "Picks", "ok_at": NOW.isoformat()}})
    assert TC.mark(False, st.get, st.save, error="Forbidden: bot was kicked", now=NOW) is True
    assert TC.mark(False, st.get, st.save, error="Forbidden: bot was kicked", now=NOW + timedelta(hours=1)) is False
    assert TC.mark(False, st.get, st.save, error="x", now=NOW + timedelta(days=1)) is True     # otro día
    assert TC.problems(st[TC.KEY], NOW + timedelta(days=1))[0].startswith(
        "Canal «Picks»: el último envío falló")
    assert TC.mark(True, st.get, st.save, now=NOW + timedelta(days=1, hours=1)) is False
    assert st[TC.KEY]["error"] is None and TC.problems(st[TC.KEY], NOW + timedelta(days=1, hours=2)) == []
    txt = TC.alert_text("los picks finales", st[TC.KEY], "Forbidden: bot was kicked from the channel chat")
    assert "No pude mandar los picks finales al canal «Picks»: el bot ya no está en el canal" in txt
    assert "quitar canal" in txt and "una vez al día" in txt
    assert TC.mark(False, State().get, State().save) is False                 # sin canal no hay nada que avisar


def test_the_output_check_flags_a_channel_that_stopped_receiving():
    assert TC.problems(None) == [] and TC.problems({}) == []
    quiet = {"chat_id": "-100", "title": "Picks", "set_at": "2026-11-06T10:00:00+00:00",
             "ok_at": "2026-11-07T10:00:00+00:00"}
    assert "no le llega nada desde 07-11 10:00 UTC" in TC.problems(quiet, NOW)[0]
    assert TC.problems({**quiet, "ok_at": "2026-11-08T17:30:00+00:00"}, NOW) == []
    import src.utils.output_checks as OC
    src = inspect.getsource(OC.run_output_checks)
    assert "# 19) el canal de los partidos" in src and "TC.problems(ch)" in src


# ─── qué se copia al canal ──────────────────────────────────────────────────

@pytest.fixture
def nt_world(monkeypatch):
    import scripts.notify_telegram as nt
    from src.utils import model_state as ms
    st = State()
    monkeypatch.setattr(ms, "load_state", st.get)
    monkeypatch.setattr(ms, "save_state", st.save)
    monkeypatch.setattr(nt, "TELEGRAM_BOT_TOKEN", "TOKEN")
    monkeypatch.setattr(nt, "TELEGRAM_CHAT_ID", "555")
    posts, fail = [], {"on": False}

    def post(url, json=None, timeout=None):
        posts.append((url.rsplit("/", 1)[-1], json))
        if fail["on"] and json.get("chat_id") != "555":
            return SimpleNamespace(status_code=403, text='{"ok":false,"description":"Forbidden: bot was kicked"}',
                                   json=lambda: {"ok": False})
        return SimpleNamespace(status_code=200, text="{}", json=lambda: {"ok": True, "result": {"message_id": 77}})
    monkeypatch.setattr(nt.requests, "post", post)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return nt, st, posts, fail


def test_without_a_channel_nothing_is_copied(nt_world):
    nt, st, posts, _ = nt_world
    assert nt.share_message("hola") is None and posts == []


def test_the_copy_goes_to_the_channel_and_its_failure_does_not_turn_the_run_red(nt_world):
    nt, st, posts, fail = nt_world
    st[TC.KEY] = {"chat_id": "-100", "title": "Picks", "ok_at": NOW.isoformat()}
    nt.SEND_FAILURES.clear()
    from src.utils.log import RUN_ISSUES
    RUN_ISSUES.reset()
    assert nt.share_message("☀️ hoy", "el mensaje de la mañana") is True
    assert posts[-1][1]["chat_id"] == "-100" and st[TC.KEY]["error"] is None
    fail["on"] = True
    posts.clear()
    assert nt.share_message("🌙 noche", "el resumen de la noche") is False
    assert nt.SEND_FAILURES == []                                           # no pone la corrida en rojo
    assert RUN_ISSUES.errors() == []            # ni sale en "errores tolerados" cada 30 min
    owner = [p for m, p in posts if p.get("chat_id") == "555"]
    assert len(owner) == 1 and "No pude mandar el resumen de la noche al canal «Picks»" in owner[0]["text"]
    assert "el bot ya no está en el canal" in owner[0]["text"]
    posts.clear()
    nt.share_message("otra", "los picks finales")                           # el mismo día: sin otro aviso
    assert not [p for m, p in posts if p.get("chat_id") == "555"]
    # el tablero editado en el canal tampoco pone la corrida en rojo
    assert nt.edit_or_send("📋", None, chat_id="-100") is None and nt.SEND_FAILURES == []
    assert RUN_ISSUES.errors() == []
    nt.pin_message(77, unpin=[70], chat_id="-100")
    assert [p["chat_id"] for m, p in posts if m in ("pinChatMessage", "unpinChatMessage")] == ["-100", "-100"]


def test_the_morning_and_the_night_are_copied_without_the_dashboard(monkeypatch):
    import scripts.notify_telegram as nt
    from src.models import telegram_reports as TR
    shared, sent = [], []
    monkeypatch.setattr(nt, "share_message", lambda t, what="": shared.append((what, t)) or True)
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(nt, "_remember_announced", lambda day, stars: None)
    monkeypatch.setattr(nt, "_format_paper_bets_section", lambda *a, **k: "🌍 MUNDIAL EN PAPEL")
    day = "🤖 Otros 3 partidos de hoy no tienen 🎯: salen en \"Partidos de hoy\" (mensaje fijado)" \
          f"{TR.DASHBOARD_BIT}, solo para seguirlos."
    monkeypatch.setattr(nt, "_day_message", lambda d, when="Hoy": (day, 0, []))
    nt.notify_best_bets()
    nt.send_tomorrow_preview()
    assert [w for w, _ in shared] == ["el mensaje de la mañana", "el avance de mañana"]
    assert all("dashboard" not in t and "MUNDIAL" not in t for _, t in shared)
    assert "dashboard" in sent[0] and "MUNDIAL" in sent[0]                   # al dueño, completo
    assert TR.for_channel(day).endswith("(mensaje fijado), solo para seguirlos.")
    # la noche
    monkeypatch.setattr(TR, "load_picks", lambda *a: [])
    monkeypatch.setattr(TR, "load_parlays", lambda *a: [])
    monkeypatch.setattr(TR, "load_since", lambda *a: [])
    monkeypatch.setattr(TR, "tomorrow_counts", lambda *a: {})
    monkeypatch.setattr(TR, "evening_text", lambda *a: "🌙 Así terminó")
    nt.send_evening_summary(target_date=pd.Timestamp("2026-11-08").date())
    assert shared[-1] == ("el resumen de la noche", "🌙 Así terminó")
    # el reporte del domingo y los avisos técnicos no van al canal
    for fn in (nt.send_weekly_report, nt.send_health_check):
        assert "share_message" not in inspect.getsource(fn)


def test_final_picks_are_copied_only_after_they_reached_the_owner(monkeypatch):
    from src.models import final_alerts as FA
    monkeypatch.setattr(FA, "plan", lambda *a, **k: ([{"kind": "star"}], {"m": {"x": "y"}}))
    monkeypatch.setattr(FA, "final_texts", lambda *a, **k: ["⏰ <b>Picks finales</b> 1", "⏰ (sigue)"])
    monkeypatch.setattr(FA.pd, "read_sql", lambda *a, **k: pd.DataFrame())
    st, shared = State(), []
    kw = dict(engine=object(), now=NOW, load_state=st.get, save_state=st.save, covered=set(),
              share=lambda t, what: shared.append((what, t)))
    FA.run_final_alerts(send=lambda t: False, **kw)
    assert shared == []                                                     # no le llegó al dueño: se reintenta
    FA.run_final_alerts(send=lambda t: True, **kw)
    assert shared == [("los picks finales", "⏰ <b>Picks finales</b> 1"), ("los picks finales", "⏰ (sigue)")]


def test_the_board_is_kept_in_the_channel_too(monkeypatch):
    from src.models import today_board as TB
    monkeypatch.setattr(pd, "read_sql", lambda *a, **k: pd.DataFrame(
        [{"match": "a vs b", "display": "A vs B", "match_date": NOW + timedelta(hours=2), "label": "Gana A",
          "family": "Ganador", "market": "home_win", "reason": "Sin ventaja", "min_us": None, "lineup": None,
          "prev_label": None, "prev_star": None, "changed_at": None, "odds_fetched_at": NOW,
          "specialty_fetched_at": None}]))
    st, edits, pins, results = State(), [], [], []
    st[TC.KEY] = {"chat_id": "-100", "title": "Picks"}
    ids = iter(range(100, 200))

    def edit(t, mid=None, silent=True, chat_id=None):
        edits.append((chat_id, mid))
        return mid or next(ids)
    kw = dict(engine=object(), edit=edit, pin=lambda mid, unpin=None, chat_id=None: pins.append((chat_id, mid)),
              load_state=st.get, save_state=st.save, channel_result=lambda ok, what, ch: results.append(ok))
    TB.run_today_board(now=NOW, **kw)
    assert edits == [(None, None), ("-100", None)] and pins == [(None, 100), ("-100", 101)]
    assert st[TB.BOARD_KEY]["ids"] == [100] and st[TC.BOARD_KEY]["ids"] == [101] and results == [True]
    TB.run_today_board(now=NOW + timedelta(minutes=30), **kw)
    assert edits[-2:] == [(None, 100), ("-100", 101)] and len(pins) == 2      # se editan, no se repiten
    kw["edit"] = lambda t, mid=None, silent=True, chat_id=None: None if chat_id else (mid or 100)
    TB.run_today_board(now=NOW + timedelta(hours=1), **kw)
    assert results[-1] is False                                             # el canal falló: se anota y se avisa
    del st[TC.KEY]
    edits.clear()
    kw["edit"] = edit
    TB.run_today_board(now=NOW + timedelta(hours=1, minutes=30), **kw)
    assert [c for c, _ in edits] == [None]                                  # sin canal, solo el del dueño
