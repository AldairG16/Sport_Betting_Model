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
import os
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


# ── Helpers de fecha en hora local del usuario ────────────────────────────────
def _local_today():
    """Fecha de HOY en la zona horaria del usuario (no UTC)."""
    return datetime.now(ZoneInfo(USER_TIMEZONE)).date()

def _local_tomorrow():
    """Fecha de MAÑANA en la zona horaria del usuario."""
    return _local_today() + timedelta(days=1)

def _tz_date_filter(col: str, date) -> str:
    """
    Genera el fragmento SQL que convierte una columna UTC a hora local
    y la compara contra una fecha dada.
    Ejemplo: (b.match_date AT TIME ZONE 'UTC' AT TIME ZONE 'America/Mexico_City')::date = '2026-04-04'
    """
    return f"({col} AT TIME ZONE 'UTC' AT TIME ZONE '{USER_TIMEZONE}')::date = '{date}'"


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


def _send_to_bot(text: str, bot_token: str, chat_id: str, label: str = "Telegram") -> bool:
    """
    Implementación interna que manda un texto a (bot_token, chat_id) específicos.
    Maneja chunking, retry con backoff exponencial y rate-limiting (429).
    `label` se usa solo para los logs (distinguir bot principal vs prekickoff).
    """
    if not bot_token or not chat_id:
        print(f"⚠️  {label} no configurado (token o chat_id vacíos).")
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
                    retry_after = int(r.headers.get("Retry-After", 5))
                    print(f"⏳ {label} rate-limited, esperando {retry_after}s...")
                    import time
                    time.sleep(retry_after)
                    continue
                else:
                    print(f"❌ {label} error {r.status_code} (intento {attempt+1}/3): {r.text[:200]}")
            except requests.RequestException as e:
                print(f"❌ Error de red {label} (intento {attempt+1}/3): {e}")

            if attempt < 2:
                import time
                wait = 2 ** (attempt + 1)
                print(f"   Reintentando en {wait}s...")
                time.sleep(wait)

        if not sent:
            print(f"❌ {label}: mensaje no enviado tras 3 intentos")
            all_ok = False

    return all_ok


def send_message(text: str) -> bool:
    """
    Envia un mensaje al bot PRINCIPAL (canal de morning/evening/health/etc).
    Si el texto supera TELEGRAM_MAX_CHARS lo divide en múltiples mensajes.
    Retorna True si todos los fragmentos se enviaron correctamente.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram no configurado. Agrega TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env")
        return False
    return _send_to_bot(text, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "Telegram")


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
        home, away = match.split(" vs ", 1)
        if market == "dc_1x":
            return f"DC 1X ({home.title()} no pierde)"
        elif market == "dc_x2":
            return f"DC X2 ({away.title()} no pierde)"
        elif market == "dc_12":
            return f"DC 12 (no empate)"
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


def _format_bet_line(i: int, bet, suspicious: bool) -> str:
    market_label = _get_market_label(bet.get("market", ""), bet.get("match", ""))
    league_label = LEAGUE_LABELS.get(bet.get("league", ""), bet.get("league", ""))
    edge_pct     = round(float(bet.get("edge", 0)) * 100, 1)
    prob_pct     = round(float(bet.get("probability", 0)) * 100, 1)
    odds         = round(float(bet.get("odds", 0)), 2)
    stake        = round(float(bet.get("stake", 0)), 2)

    date_match = ""
    if "match_date" in bet and bet["match_date"]:
        try:
            from datetime import timezone as _tz
            dt = pd.to_datetime(bet["match_date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            dt_local = dt.astimezone(ZoneInfo(USER_TIMEZONE))
            date_match = dt_local.strftime("%d/%m %H:%M")
        except Exception:
            pass

    icon = "⚠️" if suspicious else "✅"

    return (
        f"{icon} <b>{i}. {bet.get('match', '')}</b>\n"
        f"   {league_label}\n"
        f"   🎯 {market_label}  @{odds}\n"
        f"   📈 Edge: +{edge_pct}%  |  Prob: {prob_pct}%\n"
        f"   💰 Stake: {stake}u  |  📅 {date_match}"
    )


# ── Mercados que expresan la misma opinion por lado ──────────────────────────
_RESULT_GROUPS = [
    ("away_win", "dnb_away"),   # visitante gana  vs  visitante no pierde
    ("home_win", "dnb_home"),   # local gana       vs  local no pierde
]


# ── Ligas Tier-1 garantizadas en el top de Telegram ──────────────────────────
# Big 5 europeos: aunque su max edge histórico sea ~0.15 (vs 0.30+ de ligas
# blandas como Brasil/Argentina/China), el usuario quiere recibir picks de
# estas ligas por relevancia. Sin garantía explícita, el head(N) global por
# edge nunca los selecciona.
_TIER1_LEAGUES = {
    "soccer_epl",
    "soccer_germany_bundesliga",
    "soccer_spain_la_liga",
    "soccer_france_ligue_one",
    "soccer_italy_serie_a",
}

# Cap de bets que se muestran en el mensaje matutino y en el resumen nocturno.
# 15 es manejable sin saturar Telegram. Más allá pierde legibilidad.
# Se usa tanto en _build_bets_by_league() (morning) como en send_evening_summary()
# (evening) para que ambos mensajes muestren el MISMO conjunto de bets.
MAX_TELEGRAM_BETS = 15


def _diversified_top(confiables: pd.DataFrame, max_bets: int) -> pd.DataFrame:
    """
    Selecciona hasta `max_bets` picks priorizando cobertura de ligas tier-1.

    Algoritmo:
    1. Toma el top-N global ordenado por edge (candidato base).
    2. Para cada liga tier-1 que NO esté representada en ese top, reserva 1
       slot con su mejor pick disponible.
    3. Los slots tier-1 reservados desplazan a los picks de menor edge del
       top global (los más cercanos al corte).

    Esto garantiza recibir picks de PL/Bundesliga/LaLiga/Ligue 1/Serie A
    cuando existan, sin sacrificar más de 5 slots del top edge.
    """
    if confiables.empty:
        return confiables

    sorted_pool = confiables.sort_values("edge", ascending=False).reset_index(drop=True)

    # Si hay menos picks que el cap, devolver todo
    if len(sorted_pool) <= max_bets:
        return sorted_pool

    # Candidato base: top-N por edge
    top_global = sorted_pool.head(max_bets)
    tier1_in_top = set(top_global[top_global["league"].isin(_TIER1_LEAGUES)]["league"])

    # Tier-1 que tienen picks pero quedaron fuera del top
    missing_tier1 = []
    for league in _TIER1_LEAGUES:
        if league in tier1_in_top:
            continue
        league_picks = sorted_pool[sorted_pool["league"] == league]
        if not league_picks.empty:
            missing_tier1.append(league_picks.head(1))

    if not missing_tier1:
        return top_global

    forced_df = pd.concat(missing_tier1)
    n_forced = len(forced_df)

    # Llenar resto con top edge global, excluyendo los forzados
    remaining_pool = sorted_pool.drop(forced_df.index)
    rest = remaining_pool.head(max_bets - n_forced)

    return pd.concat([forced_df, rest]).sort_values("edge", ascending=False).reset_index(drop=True)


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


def _build_bets_by_league(bets: pd.DataFrame, header: str) -> tuple:
    """
    Construye el texto de las bets confiables agrupadas por liga.
    Descarta las sospechosas y deduplica bets correladas del mismo partido.
    El split en múltiples mensajes lo hace send_message() automáticamente.
    Returns: (message_text, num_bets_included)
    """
    # Solo confiables
    confiables = bets[bets.apply(_is_suspicious, axis=1) == False].copy()

    # Deduplicar bets correladas (away_win + dnb_away → solo el mejor)
    confiables = _dedup_correlated(confiables)

    if confiables.empty:
        return f"{header}\n\nSin value bets confiables.", 0

    # ── TOP 15 con diversificación tier-1 (Big 5 europeos garantizados) ──
    # Antes: head(15) global por edge → ligas blandas (Brasil/Argentina/China,
    # max edge 0.30-0.50) ocupaban todos los slots y las europeas (max edge
    # ~0.15) nunca aparecían. Ahora reservamos 1 slot por tier-1 con picks.
    confiables = _diversified_top(confiables, MAX_TELEGRAM_BETS)

    confiables = confiables.sort_values(["league", "edge"], ascending=[True, False])

    lines = [header, ""]

    # Agrupar por liga
    bet_num = 1
    for league_key, group in confiables.groupby("league", sort=True):
        league_label = LEAGUE_LABELS.get(league_key, league_key or "Otras")
        lines.append(f"🏆 <b>{league_label}</b>")

        for _, bet in group.iterrows():
            market_label = _get_market_label(bet.get("market", ""), bet.get("match", ""))
            edge_pct     = round(float(bet.get("edge", 0)) * 100, 1)
            prob_pct     = round(float(bet.get("probability", 0)) * 100, 1)
            odds_val     = round(float(bet.get("odds", 0)), 2)
            stake_val    = round(float(bet.get("stake", 0)), 2)

            date_match = ""
            if bet.get("match_date"):
                try:
                    dt = pd.to_datetime(bet["match_date"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt_local = dt.astimezone(ZoneInfo(USER_TIMEZONE))
                    date_match = dt_local.strftime("%d/%m %H:%M")
                except Exception:
                    pass

            lines.append(
                f"✅ {bet_num}. <b>{bet.get('match', '')}</b>  {date_match}\n"
                f"   🎯 {market_label}  @{odds_val}\n"
                f"   📈 Edge: +{edge_pct}%  |  Prob: {prob_pct}%  |  💰 {stake_val}u"
            )
            bet_num += 1

        lines.append("")   # línea en blanco entre ligas

    total = bet_num - 1
    lines += [
        f"<i>Total: {total} apuesta{'s' if total != 1 else ''} confiable{'s' if total != 1 else ''}</i>",
        "⚠️ <i>Modelo estadistico — apuesta con responsabilidad.</i>",
    ]

    return "\n".join(lines), total


def format_bets_message(bets: pd.DataFrame) -> tuple:
    """
    Devuelve (mensaje, n_picks_confiables_incluidos).
    El n_picks lo usa notify_best_bets() para registrar cuántos se mostraron
    (distinto de len(bets), que incluye sospechosas y duplicadas correladas).
    """
    now      = datetime.now(ZoneInfo(USER_TIMEZONE))
    date_str = now.strftime("%d/%m/%Y %H:%M")
    header   = f"⚽ <b>BETTING PICKS — {date_str}</b>"
    return _build_bets_by_league(bets, header)


# ============================================================
# MAIN
# ============================================================
def notify_best_bets():
    """Lee las bets de HOY (hora local) de la DB y las envia por Telegram."""
    print("\n📲 ENVIANDO NOTIFICACION TELEGRAM...\n")

    today = _local_today()

    try:
        bets = pd.read_sql(f"""
            SELECT DISTINCT ON (match, market)
                   b.match, b.match_date, b.market,
                   b.probability, b.odds, b.edge, b.stake,
                   COALESCE(u.sport_key, b.league, '') as league
            FROM bets_history b
            LEFT JOIN upcoming_matches u
              ON LOWER(u.home_team) = LOWER(SPLIT_PART(b.match, ' vs ', 1))
             AND LOWER(u.away_team) = LOWER(SPLIT_PART(b.match, ' vs ', 2))
            WHERE {_tz_date_filter('b.match_date', today)}
              AND b.result = 'pending'
            ORDER BY match, market, b.edge DESC
        """, engine)
    except Exception as e:
        print(f"❌ Error leyendo bets: {e}")
        return

    if bets.empty:
        print("⚠️  Sin value bets para hoy")
        msg = (
            f"⚽ <b>BETTING MODEL — {today.strftime('%d/%m/%Y')}</b>\n\n"
            "Sin value bets detectadas para hoy."
        )
        send_message(msg)
        _set_last_picks_shown(0)
        return

    msg, n_sent = format_bets_message(bets)
    ok  = send_message(msg)

    if ok:
        _set_last_picks_shown(n_sent)
        print(f"✅ Enviados {n_sent} picks confiables a Telegram (de {len(bets)} pending crudas)")
    else:
        print("❌ No se pudo enviar a Telegram")


# ============================================================
# PREVIEW DE MAÑANA (para recibir de noche)
# ============================================================
def send_tomorrow_preview():
    """
    Consulta las bets pendientes para MAÑANA y las envía por Telegram.
    Se llama desde run_evening() para que el usuario pueda preparar
    sus apuestas antes de dormir, aunque su laptop esté apagada de mañana.
    """
    print("\n🌙 ENVIANDO PREVIEW DE MAÑANA...\n")

    tomorrow = _local_tomorrow()

    try:
        bets = pd.read_sql(f"""
            SELECT DISTINCT ON (match, market)
                   b.match, b.match_date, b.market,
                   b.probability, b.odds, b.edge, b.stake,
                   COALESCE(u.sport_key, b.league, '') as league
            FROM bets_history b
            LEFT JOIN upcoming_matches u
              ON LOWER(u.home_team) = LOWER(SPLIT_PART(b.match, ' vs ', 1))
             AND LOWER(u.away_team) = LOWER(SPLIT_PART(b.match, ' vs ', 2))
            WHERE {_tz_date_filter('b.match_date', tomorrow)}
              AND b.result = 'pending'
            ORDER BY match, market, b.edge DESC
        """, engine)
    except Exception as e:
        print(f"❌ Error leyendo bets de mañana: {e}")
        return

    if bets.empty:
        print("Sin bets para mañana")
        msg = (
            f"🌙 <b>PICKS DE MAÑANA — {tomorrow.strftime('%d/%m/%Y')}</b>\n\n"
            "Sin value bets detectadas para mañana."
        )
        send_message(msg)
        _set_last_picks_shown(0)
        return

    date_str = tomorrow.strftime("%d/%m/%Y")
    header   = (
        f"🌙 <b>PICKS DE MAÑANA — {date_str}</b>\n"
        "<i>Prepara tus apuestas esta noche</i>"
    )
    msg, n_sent = _build_bets_by_league(bets, header)
    ok  = send_message(msg)

    if ok:
        _set_last_picks_shown(n_sent)
        print(f"✅ Preview de mañana enviado ({n_sent} bets mostradas de {len(bets)} totales)")
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
    """'gazisehir gaziantep vs besiktas jk' -> 'Gazisehir Gaziantep vs Besiktas Jk'."""
    if not match or " vs " not in match:
        return (match or "").title()
    home, away = match.split(" vs ", 1)
    return f"{home.title()} vs {away.title()}"


def send_pre_kickoff_verdict(verdicts: list) -> bool:
    """
    Envia los dictámenes del pre-kickoff analyst a Telegram.

    Formato (Opción 3 — minimal):
      • STRONG / MEDIUM se muestran con detalle (1 bloque por bet)
      • SKIP se agrupan al final en una sola sección (1 línea por bet)

    Cada verdict es un dict con las claves:
      match, match_date, market, odds, edge,
      verdict (STRONG|MEDIUM|SKIP), confidence (1-5),
      reasoning, lineups, key_factors, sources
    """
    if not verdicts:
        return False

    actionable = [v for v in verdicts if (v.get("verdict") or "").upper() in ("STRONG", "MEDIUM")]
    skipped    = [v for v in verdicts if (v.get("verdict") or "").upper() == "SKIP"]

    # ── Filtro SKIP-only: si NO hay nada accionable, silencio total ──────
    # El cron corre cada 15 min y muchos partidos generan solo SKIPs.
    # Notificar "todos descartados" cada vez sería ruido. Solo escribimos
    # cuando hay al menos 1 STRONG o MEDIUM que el usuario debería revisar.
    if not actionable:
        n_skip = len(skipped)
        print(f"✓ Pre-kickoff: {n_skip} verdicts pero 0 accionables — silencio "
              f"(no se envía mensaje)")
        return False

    icon_map = {"STRONG": "🟢", "MEDIUM": "🟡", "SKIP": "🔴"}
    lines = ["🔍 <b>PRE-KICKOFF</b>", ""]

    # ── 1) STRONG / MEDIUM con detalle ──────────────────────────────────────
    for v in actionable:
        verdict      = (v.get("verdict") or "MEDIUM").upper()
        icon         = icon_map.get(verdict, "⚪")
        kickoff      = _format_kickoff_label(v.get("match_date"))
        match_title  = _format_match_title(v.get("match", ""))
        market_label = _get_market_label(v.get("market", ""), v.get("match", ""))

        try:    confidence = int(v.get("confidence", 1) or 1)
        except (TypeError, ValueError): confidence = 1
        stars = "⭐" * max(1, min(5, confidence))

        try:    odds_val = float(v.get("odds", 0))
        except (TypeError, ValueError): odds_val = 0.0
        try:    edge_pct = float(v.get("edge", 0)) * 100
        except (TypeError, ValueError): edge_pct = 0.0

        lines.append(f"{icon} <b>{verdict}</b>  {stars}")
        lines.append(f"🏟️ <b>{match_title}</b>")
        lines.append(f"🕐 {kickoff}")
        lines.append(f"🎯 {market_label} @{odds_val:.2f}  (+{edge_pct:.1f}%)")

        reasoning = (v.get("reasoning") or "").strip()
        if reasoning:
            lines.append(f"💡 {reasoning}")

        lineups = _format_lineups_label(v.get("lineups", ""))
        lines.append(f"👥 {lineups}")
        lines.append("")

    # ── 2) SKIP agrupados (1 línea por bet) ─────────────────────────────────
    if skipped:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"🔴 <b>Descartados ({len(skipped)}):</b>")
        for v in skipped:
            match_title  = _format_match_title(v.get("match", ""))
            market_label = _get_market_label(v.get("market", ""), v.get("match", ""))
            reason       = (v.get("reasoning") or "").strip()
            # Acortar reason a ≤60 chars para mantener 1 línea
            if len(reason) > 60:
                reason = reason[:57] + "..."
            line = f"• <b>{match_title}</b> — {market_label}"
            if reason:
                line += f" ({reason})"
            lines.append(line)
        lines.append("")

    lines.append("<i>Solo informativo — las apuestas siguen activas "
                 "y se resuelven normal al final del día.</i>")

    # Bot dedicado de pre-kickoff (con fallback al principal si no está
    # configurado). Esto evita saturar el canal de morning/evening.
    return send_message_prekickoff("\n".join(lines))


# ============================================================
# HEALTH CHECK
# ============================================================
def send_health_check(
    mode: str,
    picks_today: int,
    pending_total: int,
    elapsed_seconds: float,
    steps_ok: int,
    steps_total: int,
    picks_label: str = "Picks del día",
):
    """
    Envia un resumen de salud del pipeline a Telegram al finalizar cada modo.

    Args:
        mode:             modo del orchestrator (morning, evening, etc.)
        picks_today:      picks confiables incluidos en la notificación enviada
                          (mismo número que el "Total: N apuestas confiables" del
                           mensaje de picks). 0 si no se envió notificación.
        pending_total:    total de bets pending futuras en la DB (incluye picks
                          de mañana, pasado mañana, etc.). Útil como diagnóstico
                          pero NO refleja lo que el usuario verá en el mensaje.
        elapsed_seconds:  tiempo total de ejecucion en segundos
        steps_ok:         pasos que terminaron sin error
        steps_total:      total de pasos ejecutados
        picks_label:      etiqueta a usar en el mensaje (varía morning vs evening:
                          "Picks de hoy" / "Picks de mañana").
    """
    from src.models.bankroll_manager import get_bankroll_stats
    from scripts.update_upcoming_matches import CREDITS_LOG
    from pathlib import Path as _Path

    # Leer creditos restantes del log
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

    # Bankroll stats
    try:
        stats = get_bankroll_stats()
        bankroll_line = (
            f"💰 Bankroll: <b>{stats['current']:.2f}u</b> "
            f"(ROI: {stats['roi_pct']:+.1f}%  |  DD: {stats['drawdown_pct']:.1f}%)"
        )
    except Exception:
        bankroll_line = "💰 Bankroll: no disponible"

    status_icon = "✅" if steps_ok == steps_total else "⚠️"
    elapsed_str = f"{elapsed_seconds:.0f}s" if elapsed_seconds < 120 else f"{elapsed_seconds/60:.1f}m"

    msg = (
        f"{status_icon} <b>Pipeline OK — {mode.upper()}</b>\n\n"
        f"🎯 {picks_label}: <b>{picks_today}</b>\n"
        f"📋 Pending totales en DB: {pending_total}\n"
        f"{bankroll_line}\n"
        f"💳 Créditos API restantes: {credits_remaining}\n"
        f"⚙️  Pasos: {steps_ok}/{steps_total} exitosos\n"
        f"⏱️  Tiempo: {elapsed_str}\n"
    )

    if steps_ok < steps_total:
        msg += f"\n⚠️ {steps_total - steps_ok} paso(s) con error — revisar log"

    send_message(msg)
    print(f"📋 Health check enviado a Telegram")


# ============================================================
# REPORTE SEMANAL
# ============================================================
def send_weekly_report():
    """
    Genera y envía por Telegram un reporte de rendimiento de los últimos 7 días.

    Incluye:
      - Total bets / wins / losses / pendientes
      - ROI semanal y ROI total acumulado
      - Profit en unidades
      - Bankroll actual vs inicial
      - Top 3 ligas por ROI
      - Peor liga (si hay pérdida)
      - Mejor mercado (home_win / over25 / etc.)
    """
    print("\n📊 GENERANDO REPORTE SEMANAL...\n")

    # ── Stats de la última semana ──────────────────────────────────────────
    try:
        week_df = pd.read_sql("""
            SELECT market, odds, stake, result, profit, league
            FROM bets_history
            WHERE result NOT IN ('pending')
              AND result IS NOT NULL
              AND created_at >= NOW() - INTERVAL '7 days'
        """, engine)
    except Exception:
        # Fallback sin created_at (columna puede no existir)
        week_df = pd.read_sql("""
            SELECT market, odds, stake, result, profit, league
            FROM bets_history
            WHERE result NOT IN ('pending')
              AND result IS NOT NULL
              AND match_date >= NOW() - INTERVAL '7 days'
        """, engine)

    # ── Stats totales (todo el historial) ─────────────────────────────────
    try:
        all_df = pd.read_sql("""
            SELECT stake, profit, result, league, market
            FROM bets_history
            WHERE result NOT IN ('pending')
              AND result IS NOT NULL
        """, engine)
    except Exception:
        all_df = pd.DataFrame()

    # ── Bankroll ──────────────────────────────────────────────────────────
    try:
        from src.models.bankroll_manager import get_bankroll_stats
        bk = get_bankroll_stats()
    except Exception:
        bk = {"current": 100, "initial": 100, "roi_pct": 0, "total_profit": 0, "drawdown_pct": 0}

    now_str  = datetime.now(ZoneInfo(USER_TIMEZONE)).strftime("%d/%m/%Y")
    lines    = [f"📊 <b>REPORTE SEMANAL — {now_str}</b>", ""]

    # ── Sección: esta semana ───────────────────────────────────────────────
    if week_df.empty:
        lines.append("Sin apuestas resueltas esta semana.")
    else:
        wins      = int((week_df["result"] == "win").sum())
        losses    = int((week_df["result"] == "loss").sum())
        total_w   = wins + losses
        win_rate  = wins / total_w * 100 if total_w > 0 else 0
        profit_w  = float(week_df["profit"].sum())
        staked_w  = float(week_df["stake"].sum())
        roi_w     = profit_w / staked_w * 100 if staked_w > 0 else 0
        roi_emoji = "📈" if roi_w >= 0 else "📉"

        lines += [
            "— <b>ESTA SEMANA</b> —",
            f"Bets:     {total_w}  ({wins}W / {losses}L  {win_rate:.0f}%)",
            f"Profit:   {'+' if profit_w >= 0 else ''}{profit_w:.2f}u",
            f"ROI:      {roi_emoji} {'+' if roi_w >= 0 else ''}{roi_w:.1f}%",
            "",
        ]

        # ── Top ligas esta semana ────────────────────────────────────────
        if "league" in week_df.columns:
            by_league = (
                week_df.groupby("league")
                       .agg(profit=("profit", "sum"), staked=("stake", "sum"))
                       .assign(roi=lambda x: x["profit"] / x["staked"].replace(0, 1) * 100)
                       .sort_values("roi", ascending=False)
            )
            if not by_league.empty:
                lines.append("— <b>POR LIGA (semana)</b> —")
                for lg, lr in by_league.head(3).iterrows():
                    label = LEAGUE_LABELS.get(lg, lg)
                    sign  = "+" if lr["roi"] >= 0 else ""
                    lines.append(f"  {label}: {sign}{lr['roi']:.1f}% ({'+' if lr['profit'] >= 0 else ''}{lr['profit']:.2f}u)")
                # Peor liga
                worst = by_league[by_league["roi"] < 0].tail(1)
                if not worst.empty:
                    wlg = worst.index[0]
                    wr  = worst.iloc[0]
                    lines.append(f"  ⚠️ Peor: {LEAGUE_LABELS.get(wlg, wlg)} {wr['roi']:.1f}%")
                lines.append("")

        # ── Top mercados esta semana ─────────────────────────────────────
        if "market" in week_df.columns:
            by_mkt = (
                week_df.groupby("market")
                       .agg(profit=("profit", "sum"), staked=("stake", "sum"), n=("profit", "count"))
                       .assign(roi=lambda x: x["profit"] / x["staked"].replace(0, 1) * 100)
                       .sort_values("roi", ascending=False)
            )
            if not by_mkt.empty:
                lines.append("— <b>POR MERCADO (semana)</b> —")
                for mkt, mr in by_mkt.iterrows():
                    label = _get_market_label(mkt)
                    sign  = "+" if mr["roi"] >= 0 else ""
                    lines.append(f"  {label} ({int(mr['n'])}): {sign}{mr['roi']:.1f}%")
                lines.append("")

    # ── Sección: acumulado total ───────────────────────────────────────────
    if not all_df.empty:
        total_bets   = len(all_df)
        total_profit = float(all_df["profit"].sum())
        total_staked = float(all_df["stake"].sum())
        total_roi    = total_profit / total_staked * 100 if total_staked > 0 else 0
        total_wins   = int((all_df["result"] == "win").sum())

        lines += [
            "— <b>ACUMULADO TOTAL</b> —",
            f"Bets totales: {total_bets}  (WR: {total_wins/total_bets*100:.0f}%)",
            f"Profit total: {'+' if total_profit >= 0 else ''}{total_profit:.2f}u",
            f"ROI total:    {'+' if total_roi >= 0 else ''}{total_roi:.1f}%",
            "",
        ]

    # ── Bankroll ──────────────────────────────────────────────────────────
    drawdown_str = f"  Max DD: -{bk['drawdown_pct']:.1f}%" if bk["drawdown_pct"] > 0 else ""
    lines += [
        "— <b>BANKROLL</b> —",
        f"Actual:  {bk['current']:.2f}u  (inicio: {bk['initial']:.2f}u)",
        f"ROI BR:  {'+' if bk['roi_pct'] >= 0 else ''}{bk['roi_pct']:.1f}%{drawdown_str}",
        "",
        "<i>Modelo estadístico — apuesta con responsabilidad.</i>",
    ]

    msg = "\n".join(lines)
    ok  = send_message(msg)

    if ok:
        print("✅ Reporte semanal enviado a Telegram")
    else:
        print("❌ Error enviando reporte semanal")
        print(msg)


# ============================================================
# RESUMEN NOCTURNO
# ============================================================
def send_evening_summary():
    """
    Envia resumen de resultados del día a Telegram (ciclo 11 PM).
    Muestra: bets de hoy ganadas/perdidas/pendientes + profit del día.
    """
    print("\n📲 ENVIANDO RESUMEN NOCTURNO...\n")

    from zoneinfo import ZoneInfo
    from config.settings import USER_TIMEZONE
    today    = datetime.now(ZoneInfo(USER_TIMEZONE)).date()
    date_str = today.strftime("%d/%m/%Y")

    try:
        # Fix: usar _tz_date_filter para que el día se calcule en USER_TIMEZONE
        # (consistente con notify_best_bets y send_tomorrow_preview).
        # Antes: `match_date::date = today` evaluaba la fecha en UTC, mientras
        # que `today` venía en hora local — desfase de hasta 6h en MX.
        # Incluimos edge/probability/match_date para poder aplicar el mismo
        # filtro de confiables que la notificación matutina.
        df_all = pd.read_sql(f"""
            SELECT match, match_date, market, odds, stake, result, profit,
                   edge, probability, COALESCE(league, '') AS league
            FROM bets_history
            WHERE {_tz_date_filter('match_date', today)}
            ORDER BY match_date
        """, engine)
    except Exception as e:
        print(f"❌ Error leyendo bets: {e}")
        return

    # ── Filtrar para alinear con la notificación matutina ────────────────
    # Antes: el resumen mostraba 58 bets (TODAS las del día), mientras que
    # el morning mandó solo 15 (post `_is_suspicious` + dedup + diversified
    # top 15). El usuario solo apuesta las 15 que recibió, así que el
    # resumen debe contar y mostrar exclusivamente esas. La DB sigue
    # guardando todo (las descartadas no se borran).
    if not df_all.empty:
        df = df_all[df_all.apply(_is_suspicious, axis=1) == False].copy()
        df = _dedup_correlated(df)
        # Mismo cap + diversificación que el morning para que el subset
        # mostrado aquí sea exactamente el mismo que se notificó.
        df = _diversified_top(df, MAX_TELEGRAM_BETS)
        # Re-ordenar por hora del partido (el resumen del día es cronológico,
        # no por edge — más fácil de leer al final del día).
        if not df.empty:
            df = df.sort_values("match_date").reset_index(drop=True)
    else:
        df = df_all

    # ── Diagnóstico: si todas las bets siguen pending, el problema NO es el
    # filtro de fecha sino que step_fetch_results / step_results no resolvió.
    n_total      = len(df)
    n_total_all  = len(df_all)
    n_resolved   = int(df["result"].isin(["win", "loss", "push", "half_win", "half_loss"]).sum()) if n_total else 0
    n_pending    = int((df["result"] == "pending").sum()) if n_total else 0
    n_descartado = n_total_all - n_total
    print(f"🌙 Resumen del día: today={today} ({USER_TIMEZONE})")
    print(f"   Bets totales en DB: {n_total_all}  |  confiables: {n_total}  |  descartadas: {n_descartado}")
    print(f"   Confiables: resueltas={n_resolved}  pendientes={n_pending}")
    if n_total > 0 and n_resolved == 0:
        print("   ⚠️  TODAS pendientes — investigar step_fetch_results / step_results")

    lines = [f"🌙 <b>RESUMEN DEL DÍA — {date_str}</b>", ""]

    if df.empty:
        if n_total_all > 0:
            lines.append(f"Sin apuestas confiables para hoy ({n_total_all} descartadas por filtro).")
        else:
            lines.append("Sin apuestas para hoy.")
        send_message("\n".join(lines))
        return

    wins     = df[df["result"] == "win"]
    losses   = df[df["result"] == "loss"]
    pending  = df[df["result"] == "pending"]
    resolved = df[df["result"].isin(["win", "loss"])]

    profit_day  = float(resolved["profit"].sum()) if not resolved.empty else 0
    staked_day  = float(resolved["stake"].sum())  if not resolved.empty else 0
    roi_day     = profit_day / staked_day * 100   if staked_day > 0 else 0
    roi_emoji   = "📈" if profit_day >= 0 else "📉"

    lines += [
        f"Bets hoy:   {len(df)}  ({len(wins)}✅  {len(losses)}❌  {len(pending)}⏳)",
    ]

    if not resolved.empty:
        lines += [
            f"Profit:     {'+' if profit_day >= 0 else ''}{profit_day:.2f}u",
            f"ROI hoy:    {roi_emoji} {'+' if roi_day >= 0 else ''}{roi_day:.1f}%",
        ]

    # Detalle por bet
    if not df.empty:
        lines.append("")
        for _, bet in df.iterrows():
            mkt    = _get_market_label(bet["market"])
            result = bet["result"]
            if result == "win":
                icon = "✅"
                pnl  = f"+{float(bet['profit']):.2f}u"
            elif result == "loss":
                icon = "❌"
                pnl  = f"{float(bet['profit']):.2f}u"
            else:
                icon = "⏳"
                pnl  = "pendiente"
            lines.append(f"{icon} {bet['match']}  <i>{mkt}</i>  {pnl}")

    msg = "\n".join(lines)
    ok  = send_message(msg)
    print("✅ Resumen nocturno enviado" if ok else "❌ Error enviando resumen nocturno")


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
