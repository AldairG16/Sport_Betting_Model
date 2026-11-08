"""
scripts/notify_telegram.py
============================
Envia las mejores value bets del dia a Telegram.

Setup (una sola vez):
  1. Crea un bot con @BotFather en Telegram → copia el token
  2. Habla con @userinfobot → copia tu chat_id
  3. Pega ambos en el archivo .env:
       TELEGRAM_BOT_TOKEN=xxxx
       TELEGRAM_CHAT_ID=yyyyyyy

Uso:
  python scripts/notify_telegram.py           → envia las mejores bets de hoy
  python scripts/notify_telegram.py --test    → envia mensaje de prueba
"""

import sys
import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from config.settings import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_BOT_TOKEN_PREKICKOFF,
    TELEGRAM_CHAT_ID_PREKICKOFF,
    USER_TIMEZONE,
)
from src.utils.log import get_logger
from src.utils.min_odds import fmt_american, to_american

log = get_logger(__name__)

# Mensajes que no se pudieron entregar en esta corrida. El orquestador termina
# en rojo si hay alguno (1-oct-26): Telegram es el único canal de alertas, y si
# es él el que falla, el aviso solo puede llegar por el correo de GitHub.
SEND_FAILURES: list[str] = []
# Lo último que contestó Telegram cuando algo no pasó: el aviso del canal
# de los partidos lo dice en palabras normales (8-nov-26).
LAST_ERROR = {"text": ""}

# ── Helpers de fecha en hora local del usuario ────────────────────────────────
def _local_today():
    """Fecha de HOY en la zona horaria del usuario (no UTC)."""
    return datetime.now(ZoneInfo(USER_TIMEZONE)).date()

def _local_tomorrow():
    """Fecha de MAÑANA en la zona horaria del usuario."""
    return _local_today() + timedelta(days=1)

# ── Contador de picks mostrados en la última notificación ─────────────────────
# Lo lee `orchestrator.py` en el `finally` para construir el health-check con
# el número REAL de picks confiables del día (no el total de pending en DB,
# que incluye partidos de los próximos 3-7 días y daba la falsa sensación de
# "se generaron 10 pero solo veo 1").
_LAST_PICKS_SHOWN: int = 0


def get_last_picks_shown() -> int:
    """Devuelve cuántos picks se incluyeron en la última notificación enviada."""
    return _LAST_PICKS_SHOWN


def _set_last_picks_shown(n: int) -> None:
    global _LAST_PICKS_SHOWN
    _LAST_PICKS_SHOWN = int(n)


# ============================================================
# SEND MESSAGE
# ============================================================
TELEGRAM_MAX_CHARS = 4000   # límite real de Telegram es 4096, dejamos margen


MAX_RETRY_AFTER_S = 60


def _retry_after(r) -> int:
    """Segundos que pide esperar un 429 de Telegram. Telegram los manda en el
    JSON (parameters.retry_after); hasta el 2-nov-26 solo se leía el
    encabezado Retry-After y, sin él, se esperaban 5 s."""
    wait = None
    try:
        wait = (r.json().get("parameters") or {}).get("retry_after")
    except (ValueError, AttributeError):
        pass
    if wait is None:
        wait = (getattr(r, "headers", None) or {}).get("Retry-After", 5)
    try:
        return max(1, min(int(wait), MAX_RETRY_AFTER_S))
    except (TypeError, ValueError):
        return 5


def _send_to_bot(text: str, bot_token: str, chat_id: str, label: str = "Telegram", record: bool = True) -> bool:
    """
    Implementación interna que manda un texto a (bot_token, chat_id) específicos.
    Maneja chunking, retry con backoff exponencial y rate-limiting (429).
    `label` se usa solo para los logs (distinguir bot principal vs prekickoff).
    record=False (el canal de los partidos): una falla no va a SEND_FAILURES,
    no pone la corrida en rojo.
    """
    if not bot_token or not chat_id:
        log.error(f"❌ {label} no configurado (token o chat_id vacíos): mensaje no enviado")
        SEND_FAILURES.append(f"{label}: sin configurar")
        return False

    # Dividir en chunks si es muy largo
    chunks = []
    while len(text) > TELEGRAM_MAX_CHARS:
        cut = text.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        if cut == -1:
            cut = TELEGRAM_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    all_ok = True

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        # Retry con backoff exponencial: 3 intentos (2s, 4s, 8s)
        sent = False
        for attempt in range(3):
            try:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    sent = True
                    break
                elif r.status_code == 429:
                    retry_after = _retry_after(r)
                    print(f"⏳ {label} rate-limited, esperando {retry_after}s...")
                    import time
                    time.sleep(retry_after)
                    continue
                else:
                    LAST_ERROR["text"] = r.text[:200]
                    print(f"❌ {label} error {r.status_code} (intento {attempt+1}/3): {r.text[:200]}")
            except requests.RequestException as e:
                LAST_ERROR["text"] = str(e)[:200]
                print(f"❌ Error de red {label} (intento {attempt+1}/3): {e}")

            if attempt < 2:
                import time
                wait = 2 ** (attempt + 1)
                print(f"   Reintentando en {wait}s...")
                time.sleep(wait)

        if not sent:
            if record:
                log.error(f"❌ {label}: mensaje no enviado tras 3 intentos")
                SEND_FAILURES.append(f"{label}: {chunk[:60]!r}")
            else:                     # el canal: un error iría a "errores tolerados" en cada corrida
                log.warning(f"⚠️  {label}: mensaje no enviado tras 3 intentos")
            all_ok = False

    return all_ok


def send_message(text: str) -> bool:
    """
    Envia un mensaje al bot PRINCIPAL (canal de morning/evening/health/etc).
    Si el texto supera TELEGRAM_MAX_CHARS lo divide en múltiples mensajes.
    Retorna True si todos los fragmentos se enviaron correctamente.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Telegram no configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): mensaje no enviado")
        SEND_FAILURES.append("Telegram: sin configurar")
        return False
    return _send_to_bot(text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "Telegram")


def edit_or_send(text: str, message_id: int | None = None, silent: bool = True, chat_id=None) -> int | None:
    """Edita el mensaje `message_id` del bot principal con `text` (un solo
    fragmento, < TELEGRAM_MAX_CHARS) o, si no hay o ya no se puede editar
    (borrado, de hace más de 48 h), manda uno nuevo. Devuelve el id del mensaje
    que quedó, o None si no se pudo (30-oct-26: "Partidos de hoy", que se
    actualiza en cada closing sin mandar un mensaje nuevo cada vez).
    chat_id: otro chat (el canal de los partidos, 8-nov-26); su falla no va a
    SEND_FAILURES."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Telegram no configurado (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): mensaje no enviado")
        SEND_FAILURES.append("Telegram: sin configurar")
        return None
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    payload = {"chat_id": chat_id or TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if message_id:
        try:
            r = requests.post(f"{base}/editMessageText", json={**payload, "message_id": message_id}, timeout=10)
            if r.status_code == 200 or "message is not modified" in r.text:
                return message_id
            print(f"   (no se pudo editar el mensaje {message_id}: {r.status_code} {r.text[:120]}; va uno nuevo)")
        except requests.RequestException as e:
            print(f"   (no se pudo editar el mensaje {message_id}: {e}; va uno nuevo)")
    for attempt in range(3):
        wait = 2 ** (attempt + 1)
        try:
            r = requests.post(f"{base}/sendMessage", json={**payload, "disable_notification": silent}, timeout=10)
            if r.status_code == 200:
                return int(r.json()["result"]["message_id"])
            if r.status_code == 429:
                wait = _retry_after(r)
            LAST_ERROR["text"] = r.text[:200]
            print(f"❌ Telegram error {r.status_code} (intento {attempt + 1}/3): {r.text[:200]}")
        except (requests.RequestException, KeyError, ValueError) as e:
            LAST_ERROR["text"] = str(e)[:200]
            print(f"❌ Error de red Telegram (intento {attempt + 1}/3): {e}")
        if attempt < 2:
            import time
            time.sleep(wait)
    if chat_id:
        log.warning("⚠️  Canal: mensaje no enviado tras 3 intentos")
    else:
        log.error("❌ Telegram: mensaje no enviado tras 3 intentos")
        SEND_FAILURES.append(f"Telegram: {text[:60]!r}")
    return None


def pin_message(message_id: int, unpin: list | None = None, chat_id=None) -> bool:
    """Fija `message_id` arriba del chat (sin sonido) y quita el de `unpin`.
    Mejor esfuerzo: si Telegram no deja, el mensaje sigue igual. chat_id:
    otro chat (el canal de los partidos)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    chat = chat_id or TELEGRAM_CHAT_ID
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        for old in unpin or []:
            requests.post(f"{base}/unpinChatMessage", json={"chat_id": chat, "message_id": old},
                          timeout=10)
        r = requests.post(f"{base}/pinChatMessage", json={"chat_id": chat, "message_id": message_id,
                                                          "disable_notification": True}, timeout=10)
        if r.status_code != 200:
            print(f"   (no se pudo fijar el mensaje {message_id}: {r.status_code} {r.text[:120]})")
        return r.status_code == 200
    except requests.RequestException as e:
        print(f"   (no se pudo fijar el mensaje {message_id}: {e})")
        return False


def channel_result(ok: bool, what: str, st: dict) -> None:
    """Anota cómo le fue al canal y, en la primera falla del día, le avisa al
    dueño en su chat (src/models/telegram_channel.py)."""
    from src.models import telegram_channel as TC
    from src.utils import model_state as ms
    err = LAST_ERROR["text"]
    try:
        alert = TC.mark(ok, ms.load_state, ms.save_state, error=err)
    except Exception as e:
        log.warning(f"⚠️  Canal: no se pudo anotar el envío ({type(e).__name__}: {str(e)[:120]})")
        alert = not ok
    if not ok:                        # warning: un error saldría en "errores tolerados" cada 30 min;
                                      # el dueño recibe este aviso una vez al día y el chequeo §19
        log.warning(f"⚠️  Canal «{st.get('title')}»: {what} no llegó ({TC.plain_error(err)}: {err[:120]})")
        if alert:
            send_message(TC.alert_text(what, st, err))


def share_message(text: str, what: str = "los partidos") -> bool | None:
    """Copia `text` al canal de los partidos, si el dueño guardó uno (8-nov-26,
    src/models/telegram_channel.py). None si no hay canal. Una falla no pone
    la corrida en rojo: se anota y se le avisa al dueño una vez al día."""
    from src.models import telegram_channel as TC
    from src.utils import model_state as ms
    try:
        st = TC.current(ms.load_state)
    except Exception as e:
        log.warning(f"⚠️  Canal: no se pudo leer cuál es ({type(e).__name__}): {what} no se copió al canal")
        return False
    if not st:
        return None
    LAST_ERROR["text"] = ""
    ok = bool(TELEGRAM_BOT_TOKEN) and _send_to_bot(text, TELEGRAM_BOT_TOKEN, st["chat_id"], "Canal", record=False)
    channel_result(ok, what, st)
    if ok:
        print(f"📣 Canal «{st.get('title')}»: {what} copiado")
    return ok


def send_message_prekickoff(text: str) -> bool:
    """
    Envia un mensaje al bot DEDICADO de pre-kickoff.
    Si las vars _PREKICKOFF no están configuradas, cae al bot principal
    (backward compatible). Esto evita silencio total si el usuario aún no
    creó el bot dedicado en Telegram.
    """
    bot_token = TELEGRAM_BOT_TOKEN_PREKICKOFF or TELEGRAM_BOT_TOKEN
    chat_id   = TELEGRAM_CHAT_ID_PREKICKOFF   or TELEGRAM_CHAT_ID
    label     = "Telegram-PreKickoff" if TELEGRAM_BOT_TOKEN_PREKICKOFF else "Telegram"
    return _send_to_bot(text, bot_token, chat_id, label)


# ============================================================
# FORMATEAR BETS
# ============================================================
MARKET_LABELS = {
    "home_win":   "Local gana",
    "away_win":   "Visitante gana",
    "draw":       "Empate",
    "over25":     "Over 2.5 Goles",
    "under25":    "Under 2.5 Goles",
    "over_1.5":   "Over 1.5 Goles",
    "under_1.5":  "Under 1.5 Goles",
    "over_3.5":   "Over 3.5 Goles",
    "under_3.5":  "Under 3.5 Goles",
    "btts":       "Ambos Anotan Si",
    "btts_no":    "Ambos Anotan No",
    "dnb_home":   "DNB Local",
    "dnb_away":   "DNB Visitante",
    "dc_1x":      "Doble Oportunidad 1X",
    "dc_x2":      "Doble Oportunidad X2",
    "dc_12":      "Doble Oportunidad 12",
    "shots_over_5.5":  "Tiros al Arco Over 5.5",
    "shots_under_5.5": "Tiros al Arco Under 5.5",
    "h1_home":  "1er Tiempo Local gana",
    "h1_draw":  "1er Tiempo Empate",
    "h1_away":  "1er Tiempo Visitante gana",
    "h2_home":  "2do Tiempo Local gana",
    "h2_draw":  "2do Tiempo Empate",
    "h2_away":  "2do Tiempo Visitante gana",
}


def _get_market_label(market: str, match: str = "") -> str:
    """Convierte el market key en etiqueta legible. Maneja AH dinámico y DC con equipo."""
    # Doble Oportunidad: agregar nombre del equipo protegido
    if market.startswith("dc_") and " vs " in match:
        from dashboard.display import team_name
        home, away = match.split(" vs ", 1)
        if market == "dc_1x":
            return f"DC 1X ({team_name(home)} no pierde)"
        elif market == "dc_x2":
            return f"DC X2 ({team_name(away)} no pierde)"
        elif market == "dc_12":
            return "DC 12 (no empate)"
    if market in MARKET_LABELS:
        return MARKET_LABELS[market]
    # Asian Handicap: "ah_home_-1.5" → "AH Local -1.5"
    if market.startswith("ah_"):
        parts = market.split("_")          # ["ah", "home"/"away", "-1.5"]
        if len(parts) >= 3:
            side  = "Local" if parts[1] == "home" else "Visitante"
            line  = parts[2]               # e.g. "-1.5"
            return f"AH {side} {line}"
    # Corners: "corners_over_9.5" → "Corners Over 9.5"
    if market.startswith("corners_over_"):
        return f"Corners Over {market.split('_')[-1]}"
    if market.startswith("corners_under_"):
        return f"Corners Under {market.split('_')[-1]}"
    # Cards: "cards_over_4.5" → "Tarjetas Over 4.5"
    if market.startswith("cards_over_"):
        return f"Tarjetas Over {market.split('_')[-1]}"
    if market.startswith("cards_under_"):
        return f"Tarjetas Under {market.split('_')[-1]}"
    return market

LEAGUE_LABELS = {
    "soccer_epl":                               "Premier League",
    "soccer_spain_la_liga":                     "La Liga",
    "soccer_germany_bundesliga":                "Bundesliga",
    "soccer_italy_serie_a":                     "Serie A",
    "soccer_france_ligue_one":                  "Ligue 1",
    "soccer_efl_champ":                         "Championship",
    "soccer_netherlands_eredivisie":            "Eredivisie",
    "soccer_portugal_primeira_liga":            "Primeira Liga",
    "soccer_spl":                               "Scottish PL",
    "soccer_uefa_champs_league":                "UCL",
    "soccer_uefa_europa_league":                "UEL",
    "soccer_usa_mls":                           "MLS",
    "soccer_mexico_ligamx":                     "Liga MX",
    "soccer_brazil_campeonato":                 "Brasileirao",
    "soccer_argentina_primera_division":        "Argentina",
    "soccer_conmebol_copa_libertadores":        "Libertadores",
    "soccer_fifa_world_cup_qualifiers_europe":  "WCQ Europa",
    "soccer_fifa_world_cup":                    "Mundial 2026",
    # Nuevas ligas europeas (mercados blandos)
    "soccer_turkey_super_league":               "Super Lig Turquia",
    "soccer_belgium_first_div":                 "Jupiler Pro League",
    "soccer_greece_super_league":               "Super League Grecia",
    # Asia + Escandinavia
    "soccer_japan_j_league":                    "J-League",
    "soccer_korea_kleague1":                    "K-League 1",
    "soccer_norway_eliteserien":                "Eliteserien",
    "soccer_sweden_allsvenskan":                "Allsvenskan",
    "soccer_china_superleague":                 "Super League China",
    # Beisbol
    "baseball_mlb":                             "MLB",
}


def _is_suspicious(bet) -> bool:
    """
    Detecta si una apuesta es sospechosa.
    Criterio: edge >= 0.499 (tope maximo del modelo) o
    probabilidad del modelo > 2x la probabilidad implicita del mercado.
    """
    edge = float(bet.get("edge", 0))
    if edge >= 0.499:
        return True
    odds = float(bet.get("odds", 0))
    prob = float(bet.get("probability", 0))
    if odds > 0 and prob > 0:
        market_implied = 1.0 / odds
        if prob > 2.0 * market_implied:
            return True
    return False


# ── Mercados que expresan la misma opinion por lado ──────────────────────────
_RESULT_GROUPS = [
    ("away_win", "dnb_away"),   # visitante gana  vs  visitante no pierde
    ("home_win", "dnb_home"),   # local gana       vs  local no pierde
]


def _dedup_correlated(bets: pd.DataFrame) -> pd.DataFrame:
    """
    Si un partido tiene tanto 'away_win' como 'dnb_away' (o 'home_win'/'dnb_home'),
    conserva solo el de mayor edge. El otro expresa la misma opinion con menos retorno.
    over25/under25/btts no se tocan — son mercados independientes.
    """
    if bets.empty:
        return bets
    to_drop = []
    for match, group in bets.groupby("match"):
        for market_a, market_b in _RESULT_GROUPS:
            rows_a = group[group["market"] == market_a]
            rows_b = group[group["market"] == market_b]
            if rows_a.empty or rows_b.empty:
                continue
            edge_a = float(rows_a.iloc[0]["edge"])
            edge_b = float(rows_b.iloc[0]["edge"])
            drop_idx = rows_b.index.tolist() if edge_a >= edge_b else rows_a.index.tolist()
            to_drop.extend(drop_idx)
    return bets.drop(index=to_drop).reset_index(drop=True) if to_drop else bets


# ============================================================
# PAPER-TRADING SECTION (Mundial 2026, etc.)
# ============================================================
# Las apuestas de ligas en `PAPER_ONLY_LEAGUES` (típicamente
# `soccer_fifa_world_cup`) NO entran a `bets_history` mientras el kill-switch
# `WORLD_CUP_BETTING_ENABLED=false`. Se loguean a `data/paper_trades.jsonl`
# para auditoría. Esta sección las muestra en Telegram con tag [PAPER] para
# que el usuario vea las predicciones del modelo sin que afecten bankroll.
PAPER_LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "paper_trades.jsonl"


def _read_paper_trades_for_date(target_date, days_ahead: int = 0) -> list:
    """
    Lee data/paper_trades.jsonl y filtra entradas cuyo match_date (UTC) caiga
    entre `target_date` y `target_date + days_ahead` (en USER_TIMEZONE).

    days_ahead=0  → solo el día exacto (comportamiento original)
    days_ahead=2  → target_date + los 2 días siguientes (útil para picks de Mundial
                    que se generan 1-3 días antes del partido)
    Devuelve lista de dicts (puede ser vacía).
    """
    from datetime import timedelta
    if not PAPER_LOG_PATH.exists():
        return []

    rows = []
    try:
        with open(PAPER_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"⚠️  No se pudo leer paper trades log: {e}")
        return []

    date_to = target_date + timedelta(days=days_ahead)

    # Filtrar por rango de fechas locales del usuario
    out = []
    seen = set()      # (match, market) — evita duplicados de reruns del pipeline
    tz = ZoneInfo(USER_TIMEZONE)
    for r in rows:
        md = r.get("match_date")
        if not md:
            continue
        try:
            dt = pd.to_datetime(md)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local_date = dt.astimezone(tz).date()
        except Exception:
            continue
        if not (target_date <= local_date <= date_to):
            continue
        key = (r.get("match", ""), r.get("market", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    # Orden: por match_date ASC, luego por edge DESC
    out.sort(key=lambda x: (x.get("match_date", ""), -float(x.get("edge", 0) or 0)))
    return out


def _format_paper_bets_section(target_date, days_ahead: int = 0) -> str:
    """
    Construye un bloque Telegram con las apuestas paper-trading.
    days_ahead=0 → solo el día exacto; days_ahead=2 → rango de 3 días.
    Devuelve "" si no hay paper trades.
    """
    paper_rows = _read_paper_trades_for_date(target_date, days_ahead=days_ahead)
    if not paper_rows:
        return ""

    lines = [
        "",
        "━" * 25,
        "📝 <b>[PAPER-TRADING — sin dinero real]</b>",
        "<i>Predicciones del modelo NO incluidas en bankroll.</i>",
        "",
    ]

    # Agrupar por liga
    by_league = {}
    for r in paper_rows:
        lg = r.get("league", "")
        by_league.setdefault(lg, []).append(r)

    bet_num = 1
    for league_key in sorted(by_league.keys()):
        league_label = LEAGUE_LABELS.get(league_key, league_key or "Otras")
        lines.append(f"🏆 <b>{league_label}</b>")

        for r in by_league[league_key]:
            market_label = _get_market_label(r.get("market", ""), r.get("match", ""))
            try:
                edge_pct = round(float(r.get("edge", 0)) * 100, 1)
            except Exception:
                edge_pct = 0.0
            try:
                prob_pct = round(float(r.get("probability", 0)) * 100, 1)
            except Exception:
                prob_pct = 0.0
            odds_us = fmt_american(to_american(r.get("odds")))
            try:
                stake_val = round(float(r.get("stake", 0)), 2)
            except Exception:
                stake_val = 0.0

            date_match = ""
            md = r.get("match_date")
            if md:
                try:
                    dt = pd.to_datetime(md)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt_local = dt.astimezone(ZoneInfo(USER_TIMEZONE))
                    date_match = dt_local.strftime("%d/%m %H:%M")
                except Exception:
                    pass

            lines.append(
                f"📝 {bet_num}. <b>{r.get('match', '')}</b>  {date_match}\n"
                f"   🎯 {market_label}  @{odds_us}\n"
                f"   📈 Edge: +{edge_pct}%  |  Prob: {prob_pct}%  |  💰 {stake_val}u <i>(simulada)</i>"
            )
            bet_num += 1

        lines.append("")

    total = bet_num - 1
    lines.append(
        f"<i>Total paper: {total} predicción{'es' if total != 1 else ''} "
        f"(activar con WORLD_CUP_BETTING_ENABLED=true).</i>"
    )
    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================
def _day_message(day, when: str = "Hoy") -> tuple[str, int, list]:
    """El mensaje de un día con los 🎯 y los parlays del programa (los del
    dashboard), en palabras normales (src/models/telegram_reports.py), y sus 🎯."""
    from src.models import telegram_reports as TR
    tz = ZoneInfo(USER_TIMEZONE)
    a, b = TR.day_bounds(day, tz)
    picks = TR.load_picks(engine, a, b)
    msg, n_star = TR.morning_text(day, picks, TR.load_parlays(engine, day), tz, when=when)
    return msg, n_star, [r for r in picks if TR.is_star(r)]


def _remember_announced(day, stars: list) -> None:
    """Los 🎯 que salieron en el mensaje (v2.11.0): el de los picks finales,
    ~30 min antes de cada partido, avisa si alguno deja de serlo."""
    try:
        from src.models.final_alerts import remember_announced
        remember_announced(day, stars)
    except Exception as e:
        log.error(f"❌ No se guardaron los 🎯 del mensaje: los picks finales no sabrán si alguno deja de serlo "
                  f"({type(e).__name__}: {str(e)[:120]})")


def _share_day(msg: str, what: str) -> None:
    """El mensaje del día al canal, sin lo que solo tiene el dueño (sin la
    sección del Mundial en papel ni el dashboard)."""
    from src.models import telegram_reports as TR
    share_message(TR.for_channel(msg), what)


def notify_best_bets():
    """La mañana: los 🎯 recomendados de HOY (hora local) y sus parlays.
    Hasta v2.9.3 leía bets_history, que dejó de llenarse con el modo
    colección (25-sep-26), y decía cada día "No apuestes nada" aunque el
    dashboard sí recomendaba 🎯 (6-nov-26)."""
    print("\n📲 ENVIANDO NOTIFICACION TELEGRAM...\n")
    today = _local_today()
    try:
        msg, n_star, stars = _day_message(today, "Hoy")
    except Exception as e:
        log.error(f"❌ Telegram: no se pudieron leer los picks de hoy — mensaje no enviado: {e}")
        return
    base = msg
    # Paper-trading (Mundial 2026): days_ahead=2 muestra hoy + mañana + pasado
    paper_section = _format_paper_bets_section(today, days_ahead=2)
    if paper_section:
        msg = msg + "\n" + paper_section
    if send_message(msg):
        _set_last_picks_shown(n_star)
        _remember_announced(today, stars)
        print(f"✅ Mensaje de la mañana enviado: {n_star} 🎯 recomendados")
        _share_day(base, "el mensaje de la mañana")
    else:
        print("❌ No se pudo enviar a Telegram")


# ============================================================
# PREVIEW DE MAÑANA (para recibir de noche)
# ============================================================
def send_tomorrow_preview():
    """Los 🎯 recomendados de MAÑANA y sus parlays (si la corrida de la
    mañana cae después del mediodía)."""
    print("\n🌙 ENVIANDO PREVIEW DE MAÑANA...\n")
    tomorrow = _local_tomorrow()
    try:
        msg, n_star, stars = _day_message(tomorrow, "Mañana")
    except Exception as e:
        log.error(f"❌ Telegram: no se pudieron leer los picks de mañana — preview no enviado: {e}")
        return
    base = msg
    paper_section = _format_paper_bets_section(tomorrow)
    if paper_section:
        msg = msg + "\n" + paper_section
    if send_message(msg):
        _set_last_picks_shown(n_star)
        _remember_announced(tomorrow, stars)
        print(f"✅ Preview de mañana enviado: {n_star} 🎯 recomendados")
        _share_day(base, "el avance de mañana")
    else:
        print("❌ No se pudo enviar preview a Telegram")


# ============================================================
# PRE-KICKOFF VERDICT (analista deportivo automático)
# ============================================================
def _format_kickoff_label(match_date) -> str:
    """
    Devuelve 'Hoy HH:MM' / 'Mañana HH:MM' / 'DD/MM HH:MM' según corresponda
    (en zona horaria del usuario).
    """
    try:
        dt = pd.to_datetime(match_date)
        if dt.tzinfo is None:
            dt = dt.tz_localize(timezone.utc)
        dt_local = dt.tz_convert(ZoneInfo(USER_TIMEZONE))
    except Exception:
        return ""
    today = _local_today()
    tomorrow = _local_tomorrow()
    d = dt_local.date()
    if d == today:
        return f"Hoy {dt_local.strftime('%H:%M')}"
    if d == tomorrow:
        return f"Mañana {dt_local.strftime('%H:%M')}"
    return dt_local.strftime("%d/%m %H:%M")


def _format_lineups_label(raw: str) -> str:
    """L0/L1/L2 -> 'Sin info' / 'Confirmadas' / 'Probables'."""
    if not raw:
        return "Sin info"
    s = str(raw).strip().upper()
    if s.startswith("L1"): return "Confirmadas"
    if s.startswith("L2"): return "Probables"
    if s.startswith("L0"): return "Sin info"
    return raw  # fallback: mostrar lo que mande el modelo


def _format_match_title(match: str) -> str:
    """'gazisehir gaziantep vs besiktas jk' -> 'Gazisehir Gaziantep vs Besiktas JK'
    (los nombres como en el dashboard: siglas en mayúsculas, 26-oct-26)."""
    from dashboard.display import team_name
    if not match or " vs " not in match:
        return team_name(match or "")
    home, away = match.split(" vs ", 1)
    return f"{team_name(home)} vs {team_name(away)}"


def _format_single_match_message(match: str, verdicts_for_match: list) -> str:
    """
    Construye el mensaje de Telegram para UN SOLO partido. Si el modelo
    generó varios mercados sobre el mismo partido, todos van juntos en
    el mismo mensaje (porque son del mismo evento — agrupar tiene
    sentido). Cada match → 1 mensaje separado.
    """
    icon_map = {"STRONG": "🟢", "MEDIUM": "🟡", "SKIP": "🔴", "TIMEOUT": "⏱️"}

    # Tomamos el primer verdict para encabezado (kickoff es el mismo)
    head = verdicts_for_match[0]
    kickoff     = _format_kickoff_label(head.get("match_date"))
    match_title = _format_match_title(match)

    # Decidimos el icono "principal" del mensaje: si hay STRONG → verde,
    # si solo MEDIUM → amarillo, si solo SKIP → rojo. Le sirve al usuario
    # para escanear visualmente la inbox de Telegram.
    verdicts_set = {(v.get("verdict") or "").upper() for v in verdicts_for_match}
    if "STRONG" in verdicts_set:
        head_icon = "🟢"
    elif "MEDIUM" in verdicts_set:
        head_icon = "🟡"
    else:
        head_icon = "🔴"

    # ── Detectar el BEST PICK del partido ─────────────────────────────
    # Si hay varios mercados, ponemos el best primero y lo destacamos.
    # El campo `is_best` lo setea el agente en _analyze_match_group.
    best = next((v for v in verdicts_for_match if v.get("is_best")), None)
    others = [v for v in verdicts_for_match if not v.get("is_best")]
    # Si no hay best explícito, ordenamos por verdict (STRONG > MEDIUM > SKIP)
    # y probability descendiente — preserva orden razonable.
    rank = {"STRONG": 0, "MEDIUM": 1, "SKIP": 2, "TIMEOUT": 3}
    others.sort(key=lambda v: (rank.get((v.get("verdict") or "MEDIUM").upper(), 4),
                                -int(v.get("probability", 0) or 0)))
    ordered = ([best] if best else []) + others

    # Compartido del partido (lineups L1/L2/L0, match_notes)
    lineups_share = _format_lineups_label(
        (best or verdicts_for_match[0]).get("lineups", ""))
    match_notes = (verdicts_for_match[0].get("match_notes") or "").strip()

    n_markets = len(verdicts_for_match)
    lines = [
        f"{head_icon} <b>PRE-KICKOFF — {match_title}</b>",
        f"🕐 {kickoff}   👥 {lineups_share}"
        + (f"   ({n_markets} mercados)" if n_markets > 1 else ""),
    ]
    if match_notes:
        lines.append(f"📋 {match_notes}")
    lines.append("")

    # Si hay best_pick, destacar arriba antes del listado
    if best and n_markets > 1:
        best_reason = (best.get("best_reason") or "").strip()
        best_mkt_label = _get_market_label(best.get("market", ""), match)
        lines.append(f"⭐ <b>MEJOR APUESTA: {best_mkt_label}</b>")
        if best_reason:
            lines.append(f"   <i>{best_reason}</i>")
        lines.append("")

    # Un bloque por mercado analizado de este partido
    for i, v in enumerate(ordered):
        verdict      = (v.get("verdict") or "MEDIUM").upper()
        decision     = (v.get("decision") or "").upper().strip()
        icon         = icon_map.get(verdict, "⚪")
        market_label = _get_market_label(v.get("market", ""), match)

        try:    confidence = int(v.get("confidence", 1) or 1)
        except (TypeError, ValueError): confidence = 1
        stars = "⭐" * max(1, min(5, confidence))

        try:    odds_val = float(v.get("odds", 0))
        except (TypeError, ValueError): odds_val = 0.0
        try:    edge_pct = float(v.get("edge", 0)) * 100
        except (TypeError, ValueError): edge_pct = 0.0

        try:    prob_pct = int(v.get("probability", 0) or 0)
        except (TypeError, ValueError): prob_pct = 0

        implied_pct = (1.0 / odds_val) * 100 if odds_val > 1 else 0.0

        dec_icon = "✅" if decision == "APUESTA" else "❌"
        dec_label = decision or ("APUESTA" if verdict in ("STRONG", "MEDIUM")
                                  else "NO APUESTA")

        if i > 0:
            lines.append("─────────")
        # Si es el best y hay >1 mercado, marcar con estrella
        header_prefix = "⭐ " if (v.get("is_best") and n_markets > 1) else ""
        lines.append(f"{header_prefix}{icon} <b>{verdict}</b>  {stars}")
        lines.append(f"🎯 {market_label} @{fmt_american(to_american(odds_val))}  (modelo +{edge_pct:.1f}%)")
        if prob_pct > 0:
            lines.append(f"📊 Probabilidad estimada: <b>{prob_pct}%</b>  "
                         f"(cuota implica {implied_pct:.0f}%)")
        lines.append(f"{dec_icon} <b>{dec_label}</b>")

        reasoning = (v.get("reasoning") or "").strip()
        if reasoning:
            lines.append(f"💡 {reasoning}")

        factors = v.get("key_factors") or []
        if isinstance(factors, list) and factors:
            shown = [str(f).strip() for f in factors if str(f).strip()][:4]
            if shown:
                lines.append("🔑 " + " · ".join(shown))

    lines.append("")
    lines.append("<i>Veredicto del analista — las apuestas siguen activas "
                 "en el modelo y se resuelven normal al final del día.</i>")
    return "\n".join(lines)


def send_pre_kickoff_verdict(verdicts: list) -> bool:
    """
    Envía un mensaje de Telegram POR PARTIDO (no batch).

    Antes: todos los verdicts iban en 1 solo mensaje grande, lo que era
    confuso cuando 5+ partidos caían en la ventana 30-90 min al mismo
    tiempo. Además, si todos los verdicts eran SKIP, el cron silenciaba
    el mensaje → el usuario nunca veía el veredicto.

    Ahora:
      • Se agrupa por `match`. Cada partido → 1 mensaje separado.
      • Si un match tiene varios markets, todos van juntos en el mismo
        mensaje de ese partido (con divisores).
      • Se envía SIEMPRE, incluso si todos son SKIP. El usuario quiere
        saber el veredicto antes de cada partido, sea cual sea.
    """
    if not verdicts:
        return False

    # Agrupar por match preservando orden de llegada
    by_match: dict[str, list] = {}
    for v in verdicts:
        m = v.get("match", "")
        by_match.setdefault(m, []).append(v)

    sent_ok = 0
    for match, vs in by_match.items():
        try:
            msg = _format_single_match_message(match, vs)
            if send_message_prekickoff(msg):
                sent_ok += 1
        except Exception as e:
            print(f"⚠️  Error enviando mensaje pre-kickoff de '{match}': {e}")

    if sent_ok > 0:
        print(f"✓ Pre-kickoff: {sent_ok}/{len(by_match)} mensaje(s) enviado(s)")
    return sent_ok > 0


# ============================================================
# HEALTH CHECK
# ============================================================
MODE_NAMES = {"morning": "de la mañana", "evening": "de la noche", "full": "completa"}


def send_health_check(
    mode: str,
    picks_today: int,
    pending_total: int | None = None,
    elapsed_seconds: float = 0.0,
    steps_ok: int = 0,
    steps_total: int = 0,
    picks_label: str | None = None,
):
    """
    El aviso de que una corrida terminó con pasos fallidos (solo se manda si
    algo falló), en palabras normales (6-nov-26). Ya no muestra el bankroll ni
    las "pending" de bets_history, que dejó de llenarse el 25-sep-26 (decía
    "Bankroll 63.40u (ROI …)" y "Pending totales en DB: 0").

    picks_today: los 🎯 que llevó el mensaje de la mañana (get_last_picks_shown);
    solo se dice en la corrida de la mañana. pending_total y picks_label se
    aceptan por compatibilidad y no se usan.
    """
    from scripts.update_upcoming_matches import CREDITS_LOG

    credits_remaining = "?"
    try:
        if CREDITS_LOG.exists():
            lines = CREDITS_LOG.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                if "remaining=" in line:
                    credits_remaining = line.split("remaining=")[-1].strip()
                    break
    except Exception:
        pass

    failed = max(steps_total - steps_ok, 0)
    elapsed_str = f"{elapsed_seconds:.0f} s" if elapsed_seconds < 120 else f"{elapsed_seconds / 60:.0f} min"
    name = MODE_NAMES.get(mode, mode)
    msg = (f"{'⚠️' if failed else '✅'} <b>La corrida {name} "
           f"{'tuvo fallas' if failed else 'terminó bien'}</b>\n\n")
    if failed:
        msg += f"⚙️ {failed} de {steps_total} pasos fallaron: hay que revisar el log.\n"
    if mode == "morning":
        msg += f"🎯 Recomendados en el mensaje de hoy: <b>{picks_today}</b>\n"
    msg += (f"💳 Créditos de The Odds API que quedan: {credits_remaining}\n"
            f"⏱️ Tardó: {elapsed_str}")
    send_message(msg)
    print("📋 Health check enviado a Telegram")


# ============================================================
# REPORTE SEMANAL
# ============================================================
def send_weekly_report():
    """El reporte de la semana en palabras normales: los 🎯 recomendados, los
    parlays y los demás picks de los últimos 7 días, lo acumulado desde el
    22-oct y los cambios con la alineación. Lee lo mismo que el dashboard
    (program_picks, program_parlays); hasta v2.9.3 leía bets_history, que
    dejó de llenarse el 25-sep-26 ("Sin apuestas jugadas esta semana")."""
    from src.models import telegram_reports as TR
    print("\n📊 GENERANDO REPORTE SEMANAL...\n")
    tz = ZoneInfo(USER_TIMEZONE)
    now = datetime.now(timezone.utc)
    loc = now.astimezone(tz)
    lines = [f"📊 <b>REPORTE DE LA SEMANA — {TR.day_name(loc.date())}</b>",
             f"<i>Los últimos 7 días · la ganancia es apostando {TR.STAKE} a cada uno al momio de PlayDoit</i>", ""]
    try:
        week = TR.load_picks(engine, now - timedelta(days=7), now)
        parlays = TR.load_parlays(engine, (loc - timedelta(days=7)).date(), loc.date())
        lines += TR.weekly_lines(week, parlays, TR.load_since(engine, now))
    except Exception as e:
        log.error(f"❌ Reporte semanal: no se pudieron leer los picks — no enviado: {e}")
        return

    # ── ¿Los cambios de pick con la alineación ayudan o estorban? (v2.8.0) ──
    from src.models import lineup_changes
    lines += lineup_changes.weekly_lines(engine)
    lines.append("<i>Apuesta con responsabilidad.</i>")

    msg = "\n".join(lines)
    if send_message(msg):
        print("✅ Reporte semanal enviado a Telegram")
    else:
        print("❌ Error enviando reporte semanal")
        print(msg)


# ============================================================
# RESUMEN NOCTURNO
# ============================================================
def send_evening_summary(target_date=None):
    """
    El resumen de la noche (ciclo 21:00 MX) en palabras normales: cómo
    terminó cada 🎯 recomendado de hoy, los parlays, los demás picks en una
    línea, lo acumulado desde el 22-oct y cuántos 🎯 hay mañana. Lee lo mismo
    que el dashboard (src/models/telegram_reports.py); hasta v2.9.3 leía
    bets_history y decía "Sin apuestas para hoy" (6-nov-26).

    target_date: fecha local que este run debe resumir. El orchestrator
    la pasa anclada a la HORA PROGRAMADA del cron (no a la hora real de
    ejecución) — así, si GitHub retrasa el run pasadas las 12 de la
    noche, el resumen NO salta un día atrás ni el avance uno adelante
    (bug del 12-sep).
    """
    from src.models import telegram_reports as TR
    print("\n📲 ENVIANDO RESUMEN NOCTURNO...\n")
    tz = ZoneInfo(USER_TIMEZONE)
    now = datetime.now(timezone.utc)
    if target_date is not None:
        today = target_date
    else:
        # sin target_date: antes del mediodía local recapitula el día anterior
        loc = now.astimezone(tz)
        today = (loc - timedelta(days=1)).date() if loc.hour < 12 else loc.date()
    try:
        a, b = TR.day_bounds(today, tz)
        picks = TR.load_picks(engine, a, b)
        msg = TR.evening_text(today, picks, TR.load_parlays(engine, today), TR.load_since(engine, now),
                              TR.tomorrow_counts(engine, today, tz), now, tz)
    except Exception as e:
        log.error(f"❌ Resumen nocturno: no se pudieron leer los picks — no enviado: {e}")
        return
    n_star = sum(1 for r in picks if TR.is_star(r))
    print(f"🌙 Resumen del día: {today} ({USER_TIMEZONE}) · {len(picks)} picks, {n_star} 🎯")
    ok = send_message(msg)
    print("✅ Resumen nocturno enviado" if ok else "❌ Error enviando resumen nocturno")
    if ok:
        share_message(TR.for_channel(msg), "el resumen de la noche")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",   action="store_true", help="Envia mensaje de prueba")
    parser.add_argument("--report", action="store_true", help="Envia reporte semanal")
    args = parser.parse_args()

    if args.test:
        ok = send_message(
            "✅ <b>Sports Betting Model</b>\n\n"
            "Conexion con Telegram funcionando correctamente.\n"
            f"<i>{datetime.now(ZoneInfo(USER_TIMEZONE)).strftime('%Y-%m-%d %H:%M')}</i>"
        )
        print("✅ Test enviado" if ok else "❌ Test fallido")
    elif args.report:
        send_weekly_report()
    else:
        notify_best_bets()
