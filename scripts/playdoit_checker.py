"""
scripts/playdoit_checker.py
===========================
Revisor de momios de PlayDoit por captura (21-oct-26).

El dueño manda al bot de Telegram una captura de PlayDoit (un partido, una
lista o un momio mejorado). Claude lee la imagen, el sistema compara cada
momio contra el mínimo que exige su regla (src/models/playdoit_prices.py) y
responde en el mismo chat: ✅ apuesta / ❌ no, por momio. Cada momio queda
guardado para aprender cuánto paga PlayDoit.

Corre en GitHub Actions (.github/workflows/playdoit_checker.yml), donde ya
están las claves: escucha el chat ~5 h 45 min con long polling y antes de
terminar dispara su relevo. El closing (scripts/heartbeat_dispatch.py) lo
vuelve a lanzar si la cadena se corta. Solo responde al chat del dueño
(TELEGRAM_CHAT_ID); cualquier otro se ignora.

Costo: Claude Haiku 5.5 con una imagen, ~2,000 tokens de entrada y ~500 de
salida: una fracción de centavo por captura. Tope diario propio
(PLAYDOIT_CHECKER_DAILY_USD, 0.25) además del de toda la cuenta.

Uso:
    python scripts/playdoit_checker.py --listen --minutes 345
"""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from src.utils.log import get_logger

log = get_logger(__name__)

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 3000
EST_COST_USD = 0.005
OFFSET_KEY = "telegram_checker_offset"
SPEND_KEY = "playdoit_checker_spend"
DAILY_CAP_USD = float(os.environ.get("PLAYDOIT_CHECKER_DAILY_USD") or 0.25)
WORKFLOW = "playdoit_checker.yml"
REPO = os.environ.get("GITHUB_REPOSITORY", "AldairG16/Sport_Betting_Model")

HELP = ("🔎 <b>Revisor de PlayDoit</b>\n"
        "Mándame una <b>captura</b> de PlayDoit (un partido, una lista o un momio mejorado) y te digo "
        "momio por momio si vale la pena: ✅ apuesta / ❌ no, con el mínimo que necesita.\n"
        "Reviso: ganador (1X2), doble oportunidad, empate anula, más/menos 2.5 goles y ambos anotan, "
        "de los partidos que el sistema sigue en los próximos días.")

SYSTEM = """\
Lees capturas de pantalla de la casa de apuestas PlayDoit (México). Extrae CADA momio de fútbol que
se vea en la imagen. Responde SOLO con un objeto JSON, sin texto antes ni después:
{"items": [{"local": "<equipo local como aparece>", "visitante": "<equipo visitante>",
  "mercado": "1x2" | "doble_oportunidad" | "empate_anula" | "total_goles" | "ambos_anotan" | "otro",
  "seleccion": "local" | "empate" | "visitante" | "1x" | "x2" | "12" | "mas" | "menos" | "si" | "no" | "<otra>",
  "linea": <número o null>, "momio": "<tal como aparece, ej. +150, -120 o 2.50>",
  "mejorado": <true si aparece como momio mejorado / boost, si no false>,
  "descripcion": "<texto corto de la apuesta como aparece>"}]}
Reglas: el local es el equipo que aparece primero o arriba. "Más de 2.5" → mercado total_goles,
seleccion mas, linea 2.5. "Ambos equipos anotan: Sí" → ambos_anotan, si. Si no se ve el equipo de
un momio, no lo inventes: omite ese momio. Si la imagen no es de momios, responde {"items": []}."""


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def _tg(method: str, token: str, http=requests, **params):
    r = http.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def download_photo(message: dict, token: str, http=requests) -> tuple[bytes, str] | None:
    """(bytes, media_type) de la foto más grande, o de una imagen mandada
    como archivo. None si el mensaje no trae imagen."""
    file_id = None
    if message.get("photo"):
        file_id = max(message["photo"], key=lambda p: p.get("file_size", 0) or p.get("width", 0))["file_id"]
    elif str((message.get("document") or {}).get("mime_type", "")).startswith("image/"):
        file_id = message["document"]["file_id"]
    if not file_id:
        return None
    info = _tg("getFile", token, http, file_id=file_id)
    r = http.get(f"https://api.telegram.org/file/bot{token}/{info['file_path']}", timeout=60)
    raw = r.content
    media = "image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else \
        "image/webp" if raw[8:12] == b"WEBP" else "image/jpeg"
    return raw, media


def reply(token: str, chat_id, text: str, reply_to=None, http=requests):
    from scripts.notify_telegram import TELEGRAM_MAX_CHARS
    chunks, rest = [], text
    while len(rest) > TELEGRAM_MAX_CHARS:
        cut = rest.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        cut = TELEGRAM_MAX_CHARS if cut == -1 else cut
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)
    for i, c in enumerate(chunks):
        params = {"chat_id": chat_id, "text": c, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_to and i == 0:
            params["reply_to_message_id"] = reply_to
        _tg("sendMessage", token, http, **params)


# ─────────────────────────────────────────────────────────────────────────────
# Claude lee la captura
# ─────────────────────────────────────────────────────────────────────────────

def _today_spent(engine) -> float:
    from datetime import datetime, timezone
    from src.utils.model_state import load_state
    st = load_state(SPEND_KEY) or {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return float(st.get("usd", 0.0)) if st.get("day") == today else 0.0


def _add_spent(cost: float):
    from datetime import datetime, timezone
    from src.utils.model_state import load_state, save_state
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = load_state(SPEND_KEY) or {}
    usd = float(st.get("usd", 0.0)) if st.get("day") == today else 0.0
    save_state(SPEND_KEY, {"day": today, "usd": round(usd + cost, 5)}, history=False)


def read_screenshot(image: bytes, media_type: str, engine=None, client=None) -> tuple[list | None, str]:
    """(momios leídos, motivo si no se pudo). Respeta el tope diario propio y
    el de la cuenta (src/utils/anthropic_budget.py)."""
    from src.models.lineup_review import extract_json
    from src.utils.anthropic_budget import (can_call, estimate_call_cost,
                                            extract_usage_from_response, record_call)
    if engine is None:
        from config.database import engine
    if _today_spent(engine) + EST_COST_USD > DAILY_CAP_USD:
        return None, f"llegué al tope diario de ${DAILY_CAP_USD:.2f} del revisor; vuelve a intentar mañana"
    ok, why = can_call(engine, estimate_call_cost(MODEL, 2500, 600))
    if not ok:
        return None, f"tope de gasto de Claude: {why}"
    if client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None, "falta ANTHROPIC_API_KEY en el revisor"
        import anthropic
        client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode("utf-8")}},
            {"type": "text", "text": "Extrae los momios de esta captura de PlayDoit."},
        ]}],
    )
    u = extract_usage_from_response(resp)
    cost = record_call(engine, input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                       model=MODEL, web_searches=0, cache_read_tokens=u["cache_read_tokens"])
    try:
        _add_spent(float(cost or EST_COST_USD))
    except Exception as e:
        log.warning(f"⚠️  Revisor PlayDoit: gasto del día no guardado ({type(e).__name__})")
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None, f"no pude leer la captura (stop={resp.stop_reason})"
    raw = "\n".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    parsed = extract_json(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        return None, "no entendí la captura; mándala con los momios y los equipos a la vista"
    return parsed["items"], ""


# ─────────────────────────────────────────────────────────────────────────────
# Un mensaje
# ─────────────────────────────────────────────────────────────────────────────

def handle_message(message: dict, token: str, owner_chat: str, engine=None, client=None,
                   http=requests, fixtures=None) -> str | None:
    """Procesa un mensaje del chat del dueño y devuelve lo que se respondió
    (None si se ignoró)."""
    from src.models import playdoit_prices as PP
    from src.utils.team_normalizer import normalize_team
    chat = str((message.get("chat") or {}).get("id"))
    if chat != str(owner_chat):
        return None                                   # solo el chat del dueño
    if engine is None:
        from config.database import engine
    img = download_photo(message, token, http)
    if img is None:
        text = HELP
    else:
        items, why = read_screenshot(img[0], img[1], engine=engine, client=client)
        if items is None:
            text = f"⚠️ {why}"
        else:
            fixtures = fixtures if fixtures is not None else PP.load_fixtures(engine)
            from src.models.program_picks import load_bias
            rows = PP.evaluate_items(items, fixtures, normalize_team, load_bias(engine))
            text = PP.format_reply(rows)
            try:
                PP.save_rows(engine, rows, message.get("message_id"))
            except Exception as e:
                log.error(f"❌ Revisor PlayDoit: momios no guardados: {type(e).__name__}: {e}")
    reply(token, chat, text, reply_to=message.get("message_id"), http=http)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Escucha (GitHub Actions)
# ─────────────────────────────────────────────────────────────────────────────

def dispatch_successor(http=requests) -> bool:
    """Dispara la siguiente corrida antes de terminar (workflow_dispatch con
    el token del workflow; GitHub sí crea la corrida para este evento)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return False
    r = http.post(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                  json={"ref": "master"}, timeout=20)
    ok = r.status_code in (200, 204)
    print(("▶️  Relevo disparado" if ok else f"::error::Relevo no disparado: HTTP {r.status_code}"))
    return ok


def listen(minutes: float, token: str, owner_chat: str, http=requests, clock=time.time,
           successor: bool = True) -> int:
    """Long polling hasta `minutes`; devuelve los mensajes procesados."""
    from src.utils.model_state import load_state, save_state
    hook = _tg("getWebhookInfo", token, http)
    if hook.get("url"):
        log.error("❌ Revisor PlayDoit: el bot tiene un webhook configurado; getUpdates no funciona")
        return 0
    deadline = clock() + minutes * 60
    offset = int((load_state(OFFSET_KEY) or {}).get("offset", 0))
    handled = 0
    print(f"👂 Revisor PlayDoit escuchando {minutes:.0f} min")
    while clock() < deadline - 75:
        try:
            updates = _tg("getUpdates", token, http, offset=offset, timeout=50,
                          allowed_updates=["message"])
        except Exception as e:
            log.warning(f"⚠️  getUpdates: {type(e).__name__}: {str(e)[:120]}")
            time.sleep(5)
            continue
        for u in updates:
            offset = int(u["update_id"]) + 1
            save_state(OFFSET_KEY, {"offset": offset}, history=False)   # nunca se procesa dos veces
            msg = u.get("message")
            if not msg:
                continue
            try:
                if handle_message(msg, token, owner_chat, http=http) is not None:
                    handled += 1
            except Exception as e:
                log.error(f"❌ Revisor PlayDoit: {type(e).__name__}: {str(e)[:200]}")
                try:
                    reply(token, str(msg["chat"]["id"]), "⚠️ Algo falló al revisar la captura; "
                          "mándala otra vez en un momento.", reply_to=msg.get("message_id"), http=http)
                except Exception:
                    pass
    if successor:
        dispatch_successor(http)
    print(f"✅ Revisor PlayDoit: {handled} mensajes atendidos")
    return handled


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", action="store_true")
    ap.add_argument("--minutes", type=float, default=345)
    ap.add_argument("--no-successor", action="store_true")
    args = ap.parse_args()
    token, owner = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not owner:
        print("::error::Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        sys.exit(1)
    if args.listen:
        listen(args.minutes, token, owner, successor=not args.no_successor)
