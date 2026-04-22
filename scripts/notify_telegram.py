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
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, USER_TIMEZONE


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


# ============================================================
# SEND MESSAGE
# ============================================================
TELEGRAM_MAX_CHARS = 4000   # límite real de Telegram es 4096, dejamos margen


def send_message(text: str) -> bool:
    """
    Envia un mensaje a Telegram.
    Si el texto supera TELEGRAM_MAX_CHARS lo divide en múltiples mensajes.
    Retorna True si todos los fragmentos se enviaron correctamente.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram no configurado. Agrega TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env")
        return False

    # Dividir en chunks si es muy largo
    chunks = []
    while len(text) > TELEGRAM_MAX_CHARS:
        # Cortar en el último salto de línea antes del límite
        cut = text.rfind("\n", 0, TELEGRAM_MAX_CHARS)
        if cut == -1:
            cut = TELEGRAM_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    all_ok = True

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
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
                    # Rate limited — esperar el tiempo que indica la API
                    retry_after = int(r.headers.get("Retry-After", 5))
                    print(f"⏳ Telegram rate-limited, esperando {retry_after}s...")
                    import time
                    time.sleep(retry_after)
                    continue
                else:
                    print(f"❌ Telegram error {r.status_code} (intento {attempt+1}/3): {r.text[:200]}")
            except requests.RequestException as e:
                print(f"❌ Error de red Telegram (intento {attempt+1}/3): {e}")

            if attempt < 2:
                import time
                wait = 2 ** (attempt + 1)  # 2s, 4s
                print(f"   Reintentando en {wait}s...")
                time.sleep(wait)

        if not sent:
            print(f"❌ Telegram: mensaje no enviado tras 3 intentos")
            all_ok = False

    return all_ok


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

    # ── TOP 15 mejores bets del día (por edge) ────────────────────────
    # 115 bets es inmanejable. Mostrar solo las mejores 15.
    MAX_TELEGRAM_BETS = 15
    confiables = confiables.sort_values("edge", ascending=False).head(MAX_TELEGRAM_BETS)

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


def format_bets_message(bets: pd.DataFrame) -> str:
    now      = datetime.now(ZoneInfo(USER_TIMEZONE))
    date_str = now.strftime("%d/%m/%Y %H:%M")
    header   = f"⚽ <b>BETTING PICKS — {date_str}</b>"
    msg, _ = _build_bets_by_league(bets, header)
    return msg


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
        return

    msg = format_bets_message(bets)
    ok  = send_message(msg)

    if ok:
        print(f"✅ Enviadas {len(bets)} bets a Telegram")
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
        return

    date_str = tomorrow.strftime("%d/%m/%Y")
    header   = (
        f"🌙 <b>PICKS DE MAÑANA — {date_str}</b>\n"
        "<i>Prepara tus apuestas esta noche</i>"
    )
    msg, n_sent = _build_bets_by_league(bets, header)
    ok  = send_message(msg)

    if ok:
        print(f"✅ Preview de mañana enviado ({n_sent} bets mostradas de {len(bets)} totales)")
    else:
        print("❌ No se pudo enviar preview a Telegram")


# ============================================================
# HEALTH CHECK
# ============================================================
def send_health_check(mode: str, bets_generated: int, elapsed_seconds: float, steps_ok: int, steps_total: int):
    """
    Envia un resumen de salud del pipeline a Telegram al finalizar cada modo.

    Args:
        mode:             modo del orchestrator (morning, evening, etc.)
        bets_generated:   numero de apuestas generadas en este ciclo
        elapsed_seconds:  tiempo total de ejecucion en segundos
        steps_ok:         pasos que terminaron sin error
        steps_total:      total de pasos ejecutados
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
        f"📊 Bets generadas: <b>{bets_generated}</b>\n"
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
        df = pd.read_sql(f"""
            SELECT match, market, odds, stake, result, profit
            FROM bets_history
            WHERE match_date::date = '{today}'
            ORDER BY match_date
        """, engine)
    except Exception as e:
        print(f"❌ Error leyendo bets: {e}")
        return
    lines = [f"🌙 <b>RESUMEN DEL DÍA — {date_str}</b>", ""]

    if df.empty:
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
