"""
src/models/telegram_channel.py
==============================
El canal de los partidos (8-nov-26). El dueño: "como puedo unir a personas a
mi chat de telegram donde mandamos los partidos"; eligió "un canal solo para
los partidos".

- **Cómo se guarda.** El dueño crea el canal, agrega al bot como
  administrador y le reenvía al bot, en su chat, cualquier mensaje del canal.
  El revisor de capturas (scripts/playdoit_checker.py), que solo lee el chat
  del dueño, lo reconoce:
  - revisa que el bot pueda publicar ahí;
  - manda al canal un mensaje de bienvenida;
  - lo guarda en model_state 'telegram_channel'.
  No hay número que copiar, ni secreto o workflow que cambiar. «quitar canal»
  lo borra.
- **Qué va al canal.** Una copia de lo que es de los partidos:
  - el mensaje de la mañana y el avance de mañana;
  - los picks finales;
  - el resumen de la noche;
  - "Partidos de hoy", fijado y editado cada 30 min (su estado va en
    'telegram_today_channel').
- **Qué no va.** Lo técnico (créditos, fallas, el reporte del domingo) y el
  revisor de capturas siguen solo en el chat del dueño.
- **Si el canal falla.** Una falla del canal no detiene lo del dueño ni pone
  la corrida en rojo. Se anota aquí y se le avisa al dueño en su chat una vez
  al día. El chequeo de salidas §19 la marca mientras no vuelva a llegar nada
  (problems).

Las funciones son puras: las llamadas a Telegram y al estado se pasan.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KEY = "telegram_channel"
BOARD_KEY = "telegram_today_channel"
QUIET_HOURS = 26                 # el tablero se edita cada 30 min: 26 h sin nada es que no llega
STOP_WORDS = ("quitar canal", "/quitar_canal")

WELCOME = ("📣 <b>Aquí llegan los partidos</b>\n"
           "• En la mañana: los 🎯 recomendados del día y sus parlays.\n"
           "• Unos 30 min antes de cada partido: el pick final, ya con la alineación si salió.\n"
           "• En la noche: cómo terminó cada uno.\n"
           "• Fijado arriba: \"Partidos de hoy\", que se actualiza solo cada 30 min.\n"
           "<i>Apuesta con responsabilidad.</i>")


def _tz():
    from config.settings import USER_TIMEZONE
    return ZoneInfo(USER_TIMEZONE)


def forwarded_channel(message: dict) -> dict | None:
    """El canal del que viene un mensaje reenviado (o None)."""
    origin = message.get("forward_origin") or {}
    if origin.get("type") == "channel" and origin.get("chat"):
        return origin["chat"]
    chat = message.get("forward_from_chat") or {}           # Bot API anterior a la 7.0
    return chat if chat.get("type") == "channel" else None


def is_stop(text) -> bool:
    return str(text or "").strip().lower() in STOP_WORDS


def current(load_state) -> dict | None:
    """El canal guardado, o None."""
    st = load_state(KEY) or {}
    return st if st.get("chat_id") else None


def plain_error(desc) -> str:
    """Lo que dijo Telegram, en palabras normales."""
    d = str(desc or "").lower()
    if any(k in d for k in ("not a member", "kicked", "chat not found", "bot was blocked")):
        return "el bot ya no está en el canal"
    if any(k in d for k in ("rights", "not enough", "administrator", "forbidden")):
        return "el bot ya no tiene permiso de publicar en el canal"
    if "too many requests" in d or "429" in d:
        return "Telegram pidió esperar (demasiados mensajes)"
    return "Telegram no lo dejó pasar"


def register(chat: dict, tg, load_state, save_state, now: datetime | None = None) -> str:
    """Guarda el canal de un mensaje reenviado por el dueño. `tg(method,
    **params)` llama a Telegram y lanza si falla. Devuelve la respuesta."""
    now = now or datetime.now(timezone.utc)
    cid, title = str(chat.get("id")), str(chat.get("title") or "el canal")
    again = (f"Agrega este bot como <b>administrador</b> del canal «{title}», con permiso de publicar, "
             "editar y fijar mensajes, y reenvíame otro mensaje de ahí.")
    try:
        me = tg("getMe")
        member = tg("getChatMember", chat_id=cid, user_id=me["id"])
    except Exception as e:
        return f"⚠️ No pude revisar el canal: {plain_error(e)}. {again}"
    if member.get("status") != "administrator" or not member.get("can_post_messages"):
        return f"⚠️ Todavía no puedo publicar en el canal. {again}"
    old = current(load_state) or {}
    same = old.get("chat_id") == cid
    if not same:
        try:
            tg("sendMessage", chat_id=cid, text=WELCOME, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            return f"⚠️ No pude publicar en el canal: {plain_error(e)}. {again}"
    stamp = now.isoformat()
    save_state(KEY, {"chat_id": cid, "title": title, "set_at": old.get("set_at") if same else stamp,
                     "ok_at": stamp, "error_at": None, "error": None, "alerted_day": None}, history=False)
    if same:
        head = f"✅ El canal «{title}» ya estaba guardado: ahí siguen llegando los partidos."
    elif old:
        head = f"✅ Listo: ahora los partidos van al canal «{title}» (antes iban a «{old.get('title')}»)."
    else:
        head = f"✅ Listo: los partidos también van a llegar al canal «{title}»."
    lines = [head,
             "Llegan ahí: el mensaje de la mañana, el pick final de cada partido, el resumen de la noche y "
             "\"Partidos de hoy\".",
             "Lo técnico y este revisor de capturas siguen solo aquí.",
             "Para invitar gente, comparte el enlace de invitación del canal (en el canal: Administrar → "
             "Enlaces de invitación).",
             "Para dejar de mandar al canal, escríbeme «quitar canal»."]
    if not member.get("can_edit_messages"):
        lines.insert(1, "⚠️ Sin el permiso de <b>editar mensajes</b> no puedo fijar \"Partidos de hoy\" arriba "
                        "del canal: dáselo al bot si lo quieres fijado.")
    return "\n".join(lines)


def unregister(load_state, save_state) -> str:
    old = current(load_state)
    if not old:
        return "No hay ningún canal guardado: los partidos solo llegan aquí."
    save_state(KEY, {}, history=False)
    save_state(BOARD_KEY, {}, history=False)
    return (f"✅ Ya no mando nada al canal «{old.get('title')}». Para volver, reenvíame un mensaje del canal "
            "(con el bot como administrador).")


def mark(ok: bool, load_state, save_state, error=None, now: datetime | None = None) -> bool:
    """Anota cómo le fue a un envío al canal. True si hay que avisarle al
    dueño (la primera falla del día, hora de México)."""
    now = now or datetime.now(timezone.utc)
    st = current(load_state)
    if not st:
        return False
    if ok:
        save_state(KEY, {**st, "ok_at": now.isoformat(), "error_at": None, "error": None}, history=False)
        return False
    day = now.astimezone(_tz()).date().isoformat()
    alert = st.get("alerted_day") != day
    save_state(KEY, {**st, "error_at": now.isoformat(), "error": str(error or "")[:200],
                     "alerted_day": day}, history=False)
    return alert


def alert_text(what: str, st: dict, error) -> str:
    return (f"⚠️ No pude mandar {what} al canal «{st.get('title')}»: {plain_error(error)}.\n"
            "Revisa que este bot siga como administrador del canal, con permiso de publicar. Si ya no "
            "quieres el canal, escríbeme «quitar canal».\n<i>Te aviso una vez al día mientras siga fallando.</i>")


def problems(st: dict | None, now: datetime | None = None) -> list:
    """Chequeo de salidas §19: un canal guardado al que no le está llegando nada."""
    if not st or not st.get("chat_id"):
        return []
    now = now or datetime.now(timezone.utc)
    title = st.get("title")

    def ts(v):
        try:
            t = datetime.fromisoformat(str(v))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    ok, err, since = ts(st.get("ok_at")), ts(st.get("error_at")), ts(st.get("set_at"))
    if err and (not ok or err > ok):
        return [f"Canal «{title}»: el último envío falló ({plain_error(st.get('error'))}) y no ha llegado nada "
                f"desde {(ok or since or err):%d-%m %H:%M} UTC"]
    last = ok or since
    if last and now - last > timedelta(hours=QUIET_HOURS):
        return [f"Canal «{title}»: no le llega nada desde {last:%d-%m %H:%M} UTC (¿se dejó de mandar?)"]
    return []
