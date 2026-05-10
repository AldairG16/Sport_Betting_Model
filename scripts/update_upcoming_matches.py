"""
scripts/update_upcoming_matches.py
===================================
Descarga partidos proximos + odds desde the-odds-api.com

OPTIMIZACIONES DE CREDITOS:
  - 1 sola region (eu) en lugar de eu,uk,us  → ahorra 67% de creditos
  - TTL cache: no re-fetcha si los datos tienen menos de API_TTL_HOURS horas
  - Solo consulta ligas que tienen partidos en los proximos FETCH_DAYS_AHEAD dias
  - Registra creditos usados/restantes en cada llamada
  - Delay entre llamadas para no saturar la API
"""

import sys
import os
import time
import json
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

sys.path.append(str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config.database import engine
from config.settings import (
    ODDS_API_KEY, ODDS_REGION, ODDS_REGION_MLB, ODDS_MARKETS,
    API_TTL_HOURS, FETCH_DAYS_AHEAD,
    API_CREDITS_ALERT_THRESHOLD, SPORT_KEYS,
    env_int,
)
from src.utils.team_normalizer import normalize_team

# ============================================================
# AUTO-STOP: umbral mínimo de créditos para detener fetch
# ============================================================
# Plan $30/mes = 20K créditos. Umbral default = 500 = ~2.5% del plan, deja
# margen para finalizar el mes sin agotar. IMPORTANTE: usar `env_int` en
# lugar de `int(os.environ.get(...))` porque si el secret de GitHub Actions
# no está definido, `${{ secrets.X }}` se expande a "" y `int("")` truena
# con ValueError — eso fue la causa real del freeze de upcoming_matches
# desde 17-abr-26.
API_CREDITS_STOP_THRESHOLD = env_int("API_CREDITS_STOP_THRESHOLD", 500)
# Cap global de créditos a gastar en UNA invocación. Hard-cap de seguridad
# para prevenir fugas tipo la del 21-abr-26 (~20K créditos en un día).
MAX_CREDITS_PER_RUN = env_int("MAX_CREDITS_PER_RUN", 800)
# Cap de eventos enriquecidos por invocación. Cada enrichment call = ~5
# créditos (5 markets × 1 region). 40 eventos × 5 × 3 runs/día = 600/día.
MAX_ENRICHMENTS_PER_RUN = env_int("MAX_ENRICHMENTS_PER_RUN", 40)

_last_known_remaining: int | None = None   # se actualiza en cada respuesta API
_initial_remaining:    int | None = None   # remaining al inicio de la invocación
_credits_used_this_run: int = 0            # acumulador local (estimado)

# ============================================================
# ARCHIVO DE CACHE LOCAL
# ============================================================
CACHE_FILE = Path(__file__).parent.parent / "data" / "api_cache.json"
CREDITS_LOG = Path(__file__).parent.parent / "logs" / "api_credits.log"

CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
CREDITS_LOG.parent.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def is_cache_valid(cache: dict, sport_key: str) -> bool:
    """Devuelve True si el cache de esa liga tiene menos de API_TTL_HOURS horas."""
    if sport_key not in cache:
        return False
    last_fetch = datetime.fromisoformat(cache[sport_key]["fetched_at"])
    age = datetime.now(timezone.utc) - last_fetch
    return age.total_seconds() < API_TTL_HOURS * 3600


def log_credits(sport_key: str, used: str, remaining: str):
    """Guarda un registro de uso de creditos en logs/api_credits.log"""
    global _last_known_remaining, _initial_remaining

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {sport_key:<40} used={used:<6} remaining={remaining}\n"

    with open(CREDITS_LOG, "a") as f:
        f.write(line)

    remaining_int = int(remaining) if remaining.isdigit() else 9999
    _last_known_remaining = remaining_int
    # Capturar remaining inicial (para enforcar MAX_CREDITS_PER_RUN)
    if _initial_remaining is None:
        _initial_remaining = remaining_int

    if remaining_int < API_CREDITS_ALERT_THRESHOLD:
        print(f"  ⚠️  ALERTA: solo quedan {remaining} creditos en la API!")

    # Detector de fuga: si gastamos más de MAX_CREDITS_PER_RUN en esta
    # invocación, matar todo. Protege contra bugs que disparen loops.
    if _initial_remaining is not None:
        burn = _initial_remaining - remaining_int
        if burn > MAX_CREDITS_PER_RUN:
            try:
                from scripts.notify_telegram import send_message
                send_message(
                    f"🚨 <b>FUGA DE CRÉDITOS DETECTADA</b>\n\n"
                    f"Gastado en esta invocación: <b>{burn}</b>\n"
                    f"Cap configurado: {MAX_CREDITS_PER_RUN}\n"
                    f"Restantes: {remaining_int}\n\n"
                    f"⛔ Pipeline abortando para proteger el balance."
                )
            except Exception:
                pass
            raise RuntimeError(
                f"Credit burn ({burn}) exceeded MAX_CREDITS_PER_RUN ({MAX_CREDITS_PER_RUN})"
            )

    # Auto-stop: enviar alerta Telegram cuando se acerca al límite
    if remaining_int <= API_CREDITS_STOP_THRESHOLD:
        try:
            from scripts.notify_telegram import send_message
            send_message(
                f"🚨 <b>API CREDITS CRÍTICOS</b>\n\n"
                f"Créditos restantes: <b>{remaining_int}</b>\n"
                f"Umbral de parada: {API_CREDITS_STOP_THRESHOLD}\n\n"
                f"⛔ Se detendrán los fetch de ligas restantes.\n"
                f"Recarga créditos en the-odds-api.com"
            )
        except Exception:
            pass


# ============================================================
# FETCH CON CACHE + TRACKING
# ============================================================
def fetch_data(sport_key: str, cache: dict, force: bool = False) -> list:
    """
    Llama a la API si el cache esta vencido o force=True.
    Retorna la lista de partidos (desde cache o API).
    """
    if not force and is_cache_valid(cache, sport_key):
        age_sec = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(cache[sport_key]["fetched_at"])
        ).total_seconds()
        age_min = age_sec / 60
        age_hrs = age_sec / 3600
        if age_hrs > 24:
            print(f"  ⚠️  Cache STALE ({age_hrs:.1f}h) — odds pueden estar desactualizadas")
        else:
            print(f"  📦 Cache valido ({age_min:.0f} min) — omitiendo llamada API")
        return cache[sport_key]["data"]

    # MLB usa libros de EE.UU. (region "us"); futbol usa "eu"
    region = ODDS_REGION_MLB if sport_key == "baseball_mlb" else ODDS_REGION

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": region,
        "markets": ODDS_MARKETS,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException as e:
        print(f"  ❌ Error de red: {e}")
        return cache.get(sport_key, {}).get("data", [])

    # --- Tracking de creditos ---
    used      = r.headers.get("x-requests-used", "?")
    remaining = r.headers.get("x-requests-remaining", "?")
    print(f"  💳 Creditos — usados: {used}  |  restantes: {remaining}")
    log_credits(sport_key, used, remaining)

    if r.status_code != 200:
        print(f"  ❌ API error {r.status_code}: {r.text[:200]}")
        return cache.get(sport_key, {}).get("data", [])

    data = r.json()
    if isinstance(data, dict):  # error JSON
        print(f"  ❌ API devolvio error: {data}")
        return cache.get(sport_key, {}).get("data", [])

    # --- Actualizar cache ---
    cache[sport_key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "credits_remaining": remaining,
    }
    save_cache(cache)

    return data


# ============================================================
# ENRICH: SPECIALTY MARKETS (btts, dnb, dc, h1/h2, corners, cards)
# ============================================================
# El endpoint /odds solo devuelve mercados "featured" (h2h, totals, spreads).
# Los demás requieren un call per-event a /events/{id}/odds. Ahí pagamos
# 1 crédito por cada market ÚNICO devuelto × regions.
# Con region=eu y 5 markets con buena cobertura, costo típico = ~5 créditos
# por evento. Solo llamamos para eventos en próximos ENRICH_DAYS_AHEAD días
# para no gastar créditos en matches lejanos cuyos precios cambiarán mucho.

# Importante: btts y draw_no_bet TAMBIÉN son specialty markets — el endpoint
# /odds los rechaza con 422 INVALID_MARKET (verificado 26-abr-26 con la API
# en producción). Por eso van aquí, no en ODDS_MARKETS.
# Total: 7 markets × 1 region = 7 créditos por evento enriquecido.
SPECIALTY_MARKETS = (
    "btts,draw_no_bet,double_chance,h2h_h1,h2h_h2,"
    "alternate_totals_corners,alternate_totals_cards"
)
ENRICH_DAYS_AHEAD = env_int("ENRICH_DAYS_AHEAD", 2)
ENRICH_CACHE_TTL_HOURS = env_int("ENRICH_CACHE_TTL_HOURS", 12)


def _enrich_cache_key(event_id: str) -> str:
    return f"_enrich:{event_id}"


def fetch_event_specialty_markets(sport_key: str, event_id: str, cache: dict) -> list:
    """
    Llama /events/{id}/odds con markets specialty, retorna la lista de
    'bookmakers' parseable por parse_match(). Usa cache con TTL corto.
    Si la API falla o no hay créditos, retorna [] (enrichment opcional).
    """
    global _last_known_remaining

    cache_key = _enrich_cache_key(event_id)
    if cache_key in cache:
        try:
            last = datetime.fromisoformat(cache[cache_key]["fetched_at"])
            if (datetime.now(timezone.utc) - last).total_seconds() < ENRICH_CACHE_TTL_HOURS * 3600:
                return cache[cache_key].get("bookmakers", [])
        except Exception:
            pass

    # Auto-stop: no gastar créditos si quedan pocos
    if _last_known_remaining is not None and _last_known_remaining <= API_CREDITS_STOP_THRESHOLD:
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey":     ODDS_API_KEY,
        "regions":    ODDS_REGION,
        "markets":    SPECIALTY_MARKETS,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }

    try:
        r = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        return []

    used      = r.headers.get("x-requests-used",      "?")
    remaining = r.headers.get("x-requests-remaining", "?")
    # Loguear también enrichment calls para visibilidad completa.
    # log_credits() también dispara el "fuga detector" y auto-stop.
    log_credits(f"enrich:{sport_key}", used, remaining)

    if r.status_code != 200:
        return []

    try:
        data = r.json()
    except Exception:
        return []

    if not isinstance(data, dict) or "bookmakers" not in data:
        return []

    bookmakers = data.get("bookmakers", [])

    cache[cache_key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bookmakers": bookmakers,
    }

    return bookmakers


# Markets que SOLO vienen del endpoint /events/{id}/odds (no de /odds).
# Si un evento ya tiene AL MENOS UNO de estos → ya se enriqueció antes.
# btts y draw_no_bet AHORA están aquí porque /odds los rechaza con 422
# (verificado 26-abr-26): ya no son featured, son specialty per-event.
_SPECIALTY_KEYS = {
    "btts", "draw_no_bet",
    "double_chance",
    "h2h_h1", "h2h_h2", "h2h_3_way_h1", "h2h_3_way_h2",
    "alternate_totals_corners", "alternate_totals_cards",
}


def _event_already_enriched(m: dict) -> bool:
    """True si el evento ya trae markets specialty (del cache principal)."""
    for bk in m.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") in _SPECIALTY_KEYS:
                return True
    return False


# Contador GLOBAL (entre llamadas de sports) para respetar MAX_ENRICHMENTS_PER_RUN
_enrichments_this_run: int = 0


def enrich_events_with_specialty(sport_key: str, events: list, cache: dict) -> int:
    """
    Para cada evento en 'events' cuyo match comienza dentro de ENRICH_DAYS_AHEAD,
    llama a la API per-event con specialty markets y appendea los bookmakers
    al dict del evento (in-place). El parser parse_match() ya sabe procesar
    esos mercados.

    Skip si el evento ya viene enriquecido (cache principal caliente).

    Respeta MAX_ENRICHMENTS_PER_RUN globalmente (entre sports) para evitar
    fugas de créditos cuando hay muchos eventos.

    Retorna el número de eventos enriquecidos.
    """
    global _last_known_remaining, _enrichments_this_run

    if not events:
        return 0

    # Cap global alcanzado — no hacer más calls
    if _enrichments_this_run >= MAX_ENRICHMENTS_PER_RUN:
        return 0

    cutoff = datetime.now(timezone.utc) + timedelta(days=ENRICH_DAYS_AHEAD)
    now    = datetime.now(timezone.utc)
    enriched = 0
    skipped_cached = 0

    # Ordenar por fecha ASC para priorizar partidos más próximos
    events_sorted = sorted(
        events,
        key=lambda m: m.get("commence_time", "")
    )

    for m in events_sorted:
        # Cap global
        if _enrichments_this_run >= MAX_ENRICHMENTS_PER_RUN:
            print(f"  ⛔ Cap enrichment alcanzado ({MAX_ENRICHMENTS_PER_RUN}) — parando")
            break

        # Solo próximos N días
        try:
            dt = datetime.fromisoformat(m["commence_time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < now or dt > cutoff:
            continue

        # Si el evento ya trae markets specialty (viene de cache principal
        # enriquecido en la sesión previa), no repetimos el call.
        if _event_already_enriched(m):
            skipped_cached += 1
            continue

        event_id = m.get("id")
        if not event_id:
            continue

        # Auto-stop si quedan pocos créditos
        if _last_known_remaining is not None and _last_known_remaining <= API_CREDITS_STOP_THRESHOLD:
            print(f"  ⛔ AUTO-STOP enrichment: {_last_known_remaining} créditos restantes")
            break

        extra_bookmakers = fetch_event_specialty_markets(sport_key, event_id, cache)
        if not extra_bookmakers:
            continue

        # Merge: appendear al campo bookmakers del evento. Si un bookmaker
        # ya existe, le agregamos sus markets specialty.
        existing = {bk["key"]: bk for bk in m.get("bookmakers", [])}
        for bk in extra_bookmakers:
            key = bk["key"]
            if key in existing:
                existing[key].setdefault("markets", []).extend(bk.get("markets", []))
            else:
                m.setdefault("bookmakers", []).append(bk)

        enriched += 1
        _enrichments_this_run += 1
        # Delay cortés entre requests
        time.sleep(0.2)

    if enriched:
        save_cache(cache)

    if skipped_cached:
        print(f"   (cache hit: {skipped_cached} eventos ya enriquecidos)")

    return enriched


# ============================================================
# LIGAS CON PARTIDOS PROXIMOS (evita fetch innecesario)
# ============================================================
def get_active_leagues_in_db() -> set:
    """
    Devuelve las sport_keys que ya tienen partidos en los proximos FETCH_DAYS_AHEAD dias.
    Sirve como referencia, pero siempre buscamos todas para no perder partidos nuevos.
    """
    try:
        cutoff = datetime.now(timezone.utc) + timedelta(days=FETCH_DAYS_AHEAD)
        df = pd.read_sql(
            text("""
                SELECT DISTINCT sport_key
                FROM upcoming_matches
                WHERE match_date::timestamp <= :cutoff
                  AND match_date::timestamp >= NOW()
            """),
            engine,
            params={"cutoff": cutoff},
        )
        return set(df["sport_key"].tolist())
    except Exception:
        return set()


# ============================================================
# PARSEO DE UN PARTIDO
# ============================================================
def parse_match(m: dict, sport: str) -> dict | None:
    try:
        home = m["home_team"]
        away = m["away_team"]
        home_norm = normalize_team(home).lower().strip()
        away_norm = normalize_team(away).lower().strip()

        raw_date = m["commence_time"]
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        dt_utc = dt.astimezone(timezone.utc)
        # ISO con offset explícito (+00:00) — upcoming_matches.match_date ahora
        # es TIMESTAMPTZ. Pasar un naive string provocaría que Postgres use la
        # session timezone, causando desfase si ésta no es UTC.
        match_date = dt_utc.isoformat()          # ej: '2026-04-22T01:00:00+00:00'
        match_day  = dt_utc.strftime("%Y-%m-%d")
        match_key  = f"{home_norm}_{away_norm}_{match_day}"

        home_odds = draw_odds = away_odds = None
        over25_odds = under25_odds = None
        btts_yes_odds = btts_no_odds = None
        ah_home_odds = ah_away_odds = ah_line = None
        all_ah_data = []   # lista de (home_line, home_price, away_price)

        # ── Nuevos mercados Phase 1 ──────────────────────────────────────
        dnb_home_odds = dnb_away_odds = None
        dc_1x_odds = dc_x2_odds = dc_12_odds = None
        h1_home_odds = h1_draw_odds = h1_away_odds = None
        h2_home_odds = h2_draw_odds = h2_away_odds = None

        # ── Nuevos mercados Phase 2 (corners + cards de API) ────────────
        corners_over_odds = corners_under_odds = corners_line = None
        cards_over_odds = cards_under_odds = cards_line = None
        all_corners_data = []   # lista de (line, over_price, under_price)
        all_cards_data = []     # lista de (line, over_price, under_price)
        CORNERS_TARGET_LINE = 9.5
        CARDS_TARGET_LINE   = 4.5

        # ── LINE SHOPPING + CONSENSO DE BOOKMAKERS ────────────────────────
        # Line shopping:  guarda el MEJOR precio para el apostador (max odd)
        # Consenso:       guarda el precio PROMEDIO de todos los bookmakers
        #
        # El spread (max - min) / consensus indica qué tan dividido está el
        # mercado:
        #   spread < 5%  → mercado eficiente, poca incertidumbre
        #   spread > 15% → mercado dividido, mayor incertidumbre
        #
        # Si best_odds > consensus * 1.10 → línea blanda (soft line) detectada

        all_home_prices = []
        all_draw_prices = []
        all_away_prices = []
        bookmaker_count = 0

        for bookmaker in m.get("bookmakers", []):
            bk_home = bk_draw = bk_away = None

            for market in bookmaker.get("markets", []):
                if market["key"] == "h2h":
                    for o in market["outcomes"]:
                        name  = o["name"]
                        price = o["price"]
                        if name == home:
                            bk_home = price
                            if home_odds is None or price > home_odds:
                                home_odds = price
                        elif name == away:
                            bk_away = price
                            if away_odds is None or price > away_odds:
                                away_odds = price
                        elif name.lower() == "draw":
                            bk_draw = price
                            if draw_odds is None or price > draw_odds:
                                draw_odds = price

                elif market["key"] == "totals":
                    for o in market["outcomes"]:
                        if o.get("point") == 2.5:
                            price = o["price"]
                            if o["name"].lower() == "over":
                                if over25_odds is None or price > over25_odds:
                                    over25_odds = price
                            elif o["name"].lower() == "under":
                                if under25_odds is None or price > under25_odds:
                                    under25_odds = price

                elif market["key"] == "btts":
                    for o in market["outcomes"]:
                        name  = o["name"].lower()
                        price = o["price"]
                        if name == "yes":
                            if btts_yes_odds is None or price > btts_yes_odds:
                                btts_yes_odds = price
                        elif name == "no":
                            if btts_no_odds is None or price > btts_no_odds:
                                btts_no_odds = price

                elif market["key"] == "spreads":
                    # Asian Handicap: recoge (línea del local, precio local, precio visitante)
                    bk_ah_home = bk_ah_away = bk_ah_line = None
                    for o in market["outcomes"]:
                        price = o.get("price")
                        point = o.get("point")
                        if price is None or point is None:
                            continue
                        if o["name"] == home:
                            bk_ah_home = price
                            bk_ah_line = point  # línea aplicada al local (ej: -1.5)
                        elif o["name"] == away:
                            bk_ah_away = price
                    if bk_ah_home and bk_ah_away and bk_ah_line is not None:
                        all_ah_data.append((bk_ah_line, bk_ah_home, bk_ah_away))

                elif market["key"] == "draw_no_bet":
                    for o in market["outcomes"]:
                        price = o.get("price")
                        if price is None:
                            continue
                        if o["name"] == home:
                            if dnb_home_odds is None or price > dnb_home_odds:
                                dnb_home_odds = price
                        elif o["name"] == away:
                            if dnb_away_odds is None or price > dnb_away_odds:
                                dnb_away_odds = price

                elif market["key"] == "double_chance":
                    for o in market["outcomes"]:
                        price = o.get("price")
                        name  = o.get("name", "").strip()
                        if price is None:
                            continue
                        # API usa "1X", "12", "X2"
                        if name == "1X":
                            if dc_1x_odds is None or price > dc_1x_odds:
                                dc_1x_odds = price
                        elif name == "12":
                            if dc_12_odds is None or price > dc_12_odds:
                                dc_12_odds = price
                        elif name == "X2":
                            if dc_x2_odds is None or price > dc_x2_odds:
                                dc_x2_odds = price

                elif market["key"] == "h2h_h1":
                    for o in market["outcomes"]:
                        price = o.get("price")
                        name  = o.get("name", "")
                        if price is None:
                            continue
                        if name == home:
                            if h1_home_odds is None or price > h1_home_odds:
                                h1_home_odds = price
                        elif name == away:
                            if h1_away_odds is None or price > h1_away_odds:
                                h1_away_odds = price
                        elif name.lower() == "draw":
                            if h1_draw_odds is None or price > h1_draw_odds:
                                h1_draw_odds = price

                elif market["key"] == "h2h_h2":
                    for o in market["outcomes"]:
                        price = o.get("price")
                        name  = o.get("name", "")
                        if price is None:
                            continue
                        if name == home:
                            if h2_home_odds is None or price > h2_home_odds:
                                h2_home_odds = price
                        elif name == away:
                            if h2_away_odds is None or price > h2_away_odds:
                                h2_away_odds = price
                        elif name.lower() == "draw":
                            if h2_draw_odds is None or price > h2_draw_odds:
                                h2_draw_odds = price

                elif market["key"] == "alternate_totals_corners":
                    bk_co = bk_cu = bk_cl = None
                    for o in market["outcomes"]:
                        price = o.get("price")
                        point = o.get("point")
                        name  = o.get("name", "").lower()
                        if price is None or point is None:
                            continue
                        if name == "over":
                            if bk_cl is None or abs(point - CORNERS_TARGET_LINE) < abs(bk_cl - CORNERS_TARGET_LINE):
                                bk_cl = point
                                bk_co = price
                        elif name == "under" and bk_cl is not None and point == bk_cl:
                            bk_cu = price
                    if bk_co and bk_cu and bk_cl is not None:
                        all_corners_data.append((bk_cl, bk_co, bk_cu))

                elif market["key"] == "alternate_totals_cards":
                    bk_co = bk_cu = bk_cl = None
                    for o in market["outcomes"]:
                        price = o.get("price")
                        point = o.get("point")
                        name  = o.get("name", "").lower()
                        if price is None or point is None:
                            continue
                        if name == "over":
                            if bk_cl is None or abs(point - CARDS_TARGET_LINE) < abs(bk_cl - CARDS_TARGET_LINE):
                                bk_cl = point
                                bk_co = price
                        elif name == "under" and bk_cl is not None and point == bk_cl:
                            bk_cu = price
                    if bk_co and bk_cu and bk_cl is not None:
                        all_cards_data.append((bk_cl, bk_co, bk_cu))

            # Agregar precios a las listas solo si el bookmaker tiene h2h completo
            if bk_home and bk_away:
                all_home_prices.append(bk_home)
                all_away_prices.append(bk_away)
                if bk_draw:
                    all_draw_prices.append(bk_draw)
                bookmaker_count += 1

        # ── Consenso de Asian Handicap: línea más común + mejor precio ───
        if all_ah_data:
            from collections import Counter as _Counter
            lines   = [x[0] for x in all_ah_data]
            ah_line = _Counter(lines).most_common(1)[0][0]
            matching = [(h, a) for l, h, a in all_ah_data if l == ah_line]
            if matching:
                ah_home_odds = max(h for h, a in matching)
                ah_away_odds = max(a for h, a in matching)

        # ── Consenso de corners: línea más común + mejor precio ─────────────
        if all_corners_data:
            from collections import Counter as _Counter2
            c_lines = [x[0] for x in all_corners_data]
            corners_line = _Counter2(c_lines).most_common(1)[0][0]
            c_matching = [(ov, un) for l, ov, un in all_corners_data if l == corners_line]
            if c_matching:
                corners_over_odds  = max(ov for ov, _ in c_matching)
                corners_under_odds = max(un for _, un in c_matching)

        # ── Consenso de cards: línea más común + mejor precio ────────────────
        if all_cards_data:
            from collections import Counter as _Counter3
            cd_lines = [x[0] for x in all_cards_data]
            cards_line = _Counter3(cd_lines).most_common(1)[0][0]
            cd_matching = [(ov, un) for l, ov, un in all_cards_data if l == cards_line]
            if cd_matching:
                cards_over_odds  = max(ov for ov, _ in cd_matching)
                cards_under_odds = max(un for _, un in cd_matching)

        # ── Calcular consenso y spread ─────────────────────────────────────
        def _avg(lst):
            return round(sum(lst) / len(lst), 3) if lst else None

        def _spread_pct(lst):
            if not lst or len(lst) < 2:
                return None
            avg = sum(lst) / len(lst)
            if avg == 0:
                return None
            return round((max(lst) - min(lst)) / avg * 100, 2)

        consensus_home_odds = _avg(all_home_prices)
        consensus_draw_odds = _avg(all_draw_prices)
        consensus_away_odds = _avg(all_away_prices)
        h2h_spread_pct      = _spread_pct(all_home_prices)   # spread del local como proxy

        return {
            "fixture_id":           m["id"],
            "match_date":           match_date,
            "league":               sport,
            "sport_key":            sport,
            "home_team":            home,
            "away_team":            away,
            "home_team_norm":       home_norm,
            "away_team_norm":       away_norm,
            "match_key":            match_key,
            "home_odds":            home_odds,
            "draw_odds":            draw_odds,
            "away_odds":            away_odds,
            "over25_odds":          over25_odds,
            "under25_odds":         under25_odds,
            "btts_yes_odds":        btts_yes_odds,
            "btts_no_odds":         btts_no_odds,
            "consensus_home_odds":  consensus_home_odds,
            "consensus_draw_odds":  consensus_draw_odds,
            "consensus_away_odds":  consensus_away_odds,
            "bookmaker_count":      bookmaker_count,
            "h2h_spread_pct":       h2h_spread_pct,
            "ah_home_odds":         ah_home_odds,
            "ah_away_odds":         ah_away_odds,
            "ah_line":              ah_line,
            # Phase 1: nuevos mercados
            "dnb_home_odds":        dnb_home_odds,
            "dnb_away_odds":        dnb_away_odds,
            "dc_1x_odds":           dc_1x_odds,
            "dc_x2_odds":           dc_x2_odds,
            "dc_12_odds":           dc_12_odds,
            "h1_home_odds":         h1_home_odds,
            "h1_draw_odds":         h1_draw_odds,
            "h1_away_odds":         h1_away_odds,
            "h2_home_odds":         h2_home_odds,
            "h2_draw_odds":         h2_draw_odds,
            "h2_away_odds":         h2_away_odds,
            # Phase 2: corners + cards de API
            "corners_over_odds":    corners_over_odds,
            "corners_under_odds":   corners_under_odds,
            "corners_line":         corners_line,
            "cards_over_odds":      cards_over_odds,
            "cards_under_odds":     cards_under_odds,
            "cards_line":           cards_line,
        }

    except Exception as e:
        print(f"  ❌ Error parseando partido: {e}")
        return None


# ============================================================
# AUTO-MIGRACIÓN: garantiza que la tabla tiene todas las columnas
# ============================================================
def ensure_schema():
    with engine.begin() as conn:
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS home_team_norm TEXT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS away_team_norm TEXT
        """))
        # Opening odds — se escriben UNA sola vez (primera vez que se ve el partido)
        # Usadas por el Line Movement Tracker para detectar movimiento sharp
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS opening_home_odds   FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS opening_draw_odds   FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS opening_away_odds   FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS opening_over25_odds FLOAT
        """))
        # Consenso de bookmakers — detecta líneas blandas y spread de mercado
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS consensus_home_odds FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS consensus_draw_odds FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS consensus_away_odds FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS bookmaker_count INT DEFAULT 0
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS h2h_spread_pct FLOAT
        """))
        # Asian Handicap — obtenido del mercado spreads de The Odds API
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS ah_home_odds FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS ah_away_odds FLOAT
        """))
        conn.execute(text("""
            ALTER TABLE upcoming_matches
            ADD COLUMN IF NOT EXISTS ah_line FLOAT
        """))
        # ── Phase 1: Draw No Bet (API directo) ──────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS dnb_home_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS dnb_away_odds FLOAT"))
        # ── Phase 1: Double Chance ───────────────────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS dc_1x_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS dc_x2_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS dc_12_odds FLOAT"))
        # ── Phase 1: First Half 1X2 ──────────────────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h1_home_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h1_draw_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h1_away_odds FLOAT"))
        # ── Phase 1: Second Half 1X2 ─────────────────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h2_home_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h2_draw_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS h2_away_odds FLOAT"))
        # ── Phase 2: Corners from API ─────────────────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS corners_over_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS corners_under_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS corners_line FLOAT"))
        # ── Phase 2: Cards from API ───────────────────────────────────────────
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS cards_over_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS cards_under_odds FLOAT"))
        conn.execute(text("ALTER TABLE upcoming_matches ADD COLUMN IF NOT EXISTS cards_line FLOAT"))


# ============================================================
# INSERTAR EN DB
# ============================================================
def upsert_matches(rows: list[dict]):
    if not rows:
        return
    with engine.begin() as conn:
        for r in rows:
            conn.execute(text("""
                INSERT INTO upcoming_matches (
                    match_key, match_date, league, sport_key,
                    home_team, away_team,
                    home_team_norm, away_team_norm,
                    home_odds, draw_odds, away_odds,
                    over25_odds, under25_odds,
                    btts_yes_odds, btts_no_odds,
                    consensus_home_odds, consensus_draw_odds, consensus_away_odds,
                    bookmaker_count, h2h_spread_pct,
                    ah_home_odds, ah_away_odds, ah_line,
                    dnb_home_odds, dnb_away_odds,
                    dc_1x_odds, dc_x2_odds, dc_12_odds,
                    h1_home_odds, h1_draw_odds, h1_away_odds,
                    h2_home_odds, h2_draw_odds, h2_away_odds,
                    corners_over_odds, corners_under_odds, corners_line,
                    cards_over_odds, cards_under_odds, cards_line
                )
                VALUES (
                    :match_key, :match_date, :league, :sport_key,
                    :home_team, :away_team,
                    :home_team_norm, :away_team_norm,
                    :home_odds, :draw_odds, :away_odds,
                    :over25_odds, :under25_odds,
                    :btts_yes_odds, :btts_no_odds,
                    :consensus_home_odds, :consensus_draw_odds, :consensus_away_odds,
                    :bookmaker_count, :h2h_spread_pct,
                    :ah_home_odds, :ah_away_odds, :ah_line,
                    :dnb_home_odds, :dnb_away_odds,
                    :dc_1x_odds, :dc_x2_odds, :dc_12_odds,
                    :h1_home_odds, :h1_draw_odds, :h1_away_odds,
                    :h2_home_odds, :h2_draw_odds, :h2_away_odds,
                    :corners_over_odds, :corners_under_odds, :corners_line,
                    :cards_over_odds, :cards_under_odds, :cards_line
                )
                ON CONFLICT (match_key)
                DO UPDATE SET
                    home_team_norm = EXCLUDED.home_team_norm,
                    away_team_norm = EXCLUDED.away_team_norm,
                    home_odds    = COALESCE(EXCLUDED.home_odds,    upcoming_matches.home_odds),
                    draw_odds    = COALESCE(EXCLUDED.draw_odds,    upcoming_matches.draw_odds),
                    away_odds    = COALESCE(EXCLUDED.away_odds,    upcoming_matches.away_odds),
                    over25_odds  = COALESCE(EXCLUDED.over25_odds,  upcoming_matches.over25_odds),
                    under25_odds = COALESCE(EXCLUDED.under25_odds, upcoming_matches.under25_odds),
                    btts_yes_odds = COALESCE(EXCLUDED.btts_yes_odds, upcoming_matches.btts_yes_odds),
                    btts_no_odds  = COALESCE(EXCLUDED.btts_no_odds,  upcoming_matches.btts_no_odds),
                    -- Consenso: se actualiza en cada fetch (precio promedio actual)
                    consensus_home_odds = EXCLUDED.consensus_home_odds,
                    consensus_draw_odds = EXCLUDED.consensus_draw_odds,
                    consensus_away_odds = EXCLUDED.consensus_away_odds,
                    bookmaker_count     = EXCLUDED.bookmaker_count,
                    h2h_spread_pct      = EXCLUDED.h2h_spread_pct,
                    -- AH: se actualiza siempre (línea de mercado puede cambiar)
                    ah_home_odds = COALESCE(EXCLUDED.ah_home_odds, upcoming_matches.ah_home_odds),
                    ah_away_odds = COALESCE(EXCLUDED.ah_away_odds, upcoming_matches.ah_away_odds),
                    ah_line      = COALESCE(EXCLUDED.ah_line,      upcoming_matches.ah_line),
                    updated_at   = NOW(),
                    -- Opening odds: solo se escriben si aún son NULL (primera vez)
                    opening_home_odds   = COALESCE(upcoming_matches.opening_home_odds,   EXCLUDED.home_odds),
                    opening_draw_odds   = COALESCE(upcoming_matches.opening_draw_odds,   EXCLUDED.draw_odds),
                    opening_away_odds   = COALESCE(upcoming_matches.opening_away_odds,   EXCLUDED.away_odds),
                    opening_over25_odds = COALESCE(upcoming_matches.opening_over25_odds, EXCLUDED.over25_odds),
                    -- Phase 1: nuevos mercados (se actualizan si llega precio mejor)
                    dnb_home_odds  = COALESCE(EXCLUDED.dnb_home_odds,  upcoming_matches.dnb_home_odds),
                    dnb_away_odds  = COALESCE(EXCLUDED.dnb_away_odds,  upcoming_matches.dnb_away_odds),
                    dc_1x_odds     = COALESCE(EXCLUDED.dc_1x_odds,     upcoming_matches.dc_1x_odds),
                    dc_x2_odds     = COALESCE(EXCLUDED.dc_x2_odds,     upcoming_matches.dc_x2_odds),
                    dc_12_odds     = COALESCE(EXCLUDED.dc_12_odds,     upcoming_matches.dc_12_odds),
                    h1_home_odds   = COALESCE(EXCLUDED.h1_home_odds,   upcoming_matches.h1_home_odds),
                    h1_draw_odds   = COALESCE(EXCLUDED.h1_draw_odds,   upcoming_matches.h1_draw_odds),
                    h1_away_odds   = COALESCE(EXCLUDED.h1_away_odds,   upcoming_matches.h1_away_odds),
                    h2_home_odds   = COALESCE(EXCLUDED.h2_home_odds,   upcoming_matches.h2_home_odds),
                    h2_draw_odds   = COALESCE(EXCLUDED.h2_draw_odds,   upcoming_matches.h2_draw_odds),
                    h2_away_odds   = COALESCE(EXCLUDED.h2_away_odds,   upcoming_matches.h2_away_odds),
                    -- Phase 2: corners + cards API
                    corners_over_odds  = COALESCE(EXCLUDED.corners_over_odds,  upcoming_matches.corners_over_odds),
                    corners_under_odds = COALESCE(EXCLUDED.corners_under_odds, upcoming_matches.corners_under_odds),
                    corners_line       = COALESCE(EXCLUDED.corners_line,       upcoming_matches.corners_line),
                    cards_over_odds    = COALESCE(EXCLUDED.cards_over_odds,    upcoming_matches.cards_over_odds),
                    cards_under_odds   = COALESCE(EXCLUDED.cards_under_odds,   upcoming_matches.cards_under_odds),
                    cards_line         = COALESCE(EXCLUDED.cards_line,         upcoming_matches.cards_line)
            """), r)


# ============================================================
# MAIN
# ============================================================
def _cleanup_old_matches():
    """Elimina partidos de upcoming_matches que ya pasaron hace más de 7 días."""
    try:
        with engine.begin() as conn:
            r = conn.execute(text("""
                DELETE FROM upcoming_matches
                WHERE match_date::timestamp < NOW() - INTERVAL '7 days'
            """))
            if r.rowcount > 0:
                print(f"🗑️  Cleanup: {r.rowcount} partidos antiguos eliminados de upcoming_matches")
    except Exception as e:
        print(f"⚠️  Cleanup error: {e}")


def _cleanup_stale_duplicates():
    """
    Elimina rows duplicadas en upcoming_matches del mismo partido (mismo
    home_norm + away_norm + sport_key) cuando existe otra row con
    `updated_at` MÁS reciente. Esto pasa cuando la API corrige la fecha
    de un fixture (placeholder 00:00 UTC → kickoff real días después):
    como `match_key` incluye la fecha, la corrección crea una row nueva
    en vez de UPDATE, y la vieja queda stale.

    Bug detectado el 8-may-2026 con Talleres vs Belgrano y otros 10 partidos
    (AR/PT/EPL) — el pipeline generaba bets sobre rows desactualizadas.
    """
    try:
        with engine.begin() as conn:
            r = conn.execute(text("""
                DELETE FROM upcoming_matches u
                WHERE EXISTS (
                    SELECT 1 FROM upcoming_matches v
                    WHERE  v.home_team_norm = u.home_team_norm
                      AND  v.away_team_norm = u.away_team_norm
                      AND  v.sport_key      = u.sport_key
                      AND  v.match_key     <> u.match_key
                      AND  v.updated_at     > u.updated_at
                )
            """))
            if r.rowcount > 0:
                print(f"🗑️  Cleanup duplicados: {r.rowcount} rows stale eliminadas "
                      f"(misma combinación de equipos con updated_at más reciente)")
    except Exception as e:
        print(f"⚠️  Cleanup duplicados error: {e}")


def _preflight_credits_check() -> int | None:
    """
    Pregunta a /v4/sports (gratis, 0 créditos) por el balance actual.
    Fija _last_known_remaining para que el auto-stop funcione desde el
    primer call real. Retorna remaining (int) o None si falla.
    """
    global _last_known_remaining, _initial_remaining
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": ODDS_API_KEY},
            timeout=10,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        if remaining.isdigit():
            rem = int(remaining)
            _last_known_remaining = rem
            _initial_remaining    = rem
            print(f"💳 Preflight: {rem} créditos disponibles")
            return rem
    except Exception as e:
        print(f"⚠️  Preflight falló: {e}")
    return None


def update_all(force: bool = False):
    global _enrichments_this_run, _initial_remaining, _credits_used_this_run
    # Reset contadores de esta invocación (importante si se llama 2×
    # en el mismo proceso — ej. evening cycle).
    _enrichments_this_run = 0
    _initial_remaining    = None
    _credits_used_this_run = 0

    print("\n📡 ACTUALIZANDO PARTIDOS + ODDS")
    print(f"   Region: {ODDS_REGION}  |  Mercados: {ODDS_MARKETS}  |  TTL: {API_TTL_HOURS}h\n")

    # Preflight: fija el auto-stop desde el primer call
    remaining = _preflight_credits_check()
    if remaining is not None and remaining <= API_CREDITS_STOP_THRESHOLD:
        print(f"⛔ Abortando: solo {remaining} créditos, umbral={API_CREDITS_STOP_THRESHOLD}")
        try:
            from scripts.notify_telegram import send_message
            send_message(
                f"⛔ <b>Pipeline abortado — créditos bajos</b>\n\n"
                f"Restantes: <b>{remaining}</b>\n"
                f"Umbral: {API_CREDITS_STOP_THRESHOLD}\n\n"
                f"Recarga créditos en the-odds-api.com"
            )
        except Exception:
            pass
        return

    _cleanup_old_matches()   # eliminar partidos viejos antes de insertar nuevos
    ensure_schema()   # migración automática — agrega columnas faltantes
    cache = load_cache()
    total_rows = 0
    api_calls  = 0

    for sport in SPORT_KEYS:
        print(f"🔎 {sport}")

        # ── Auto-stop: no gastar créditos si quedan pocos ──────────────
        if _last_known_remaining is not None and _last_known_remaining <= API_CREDITS_STOP_THRESHOLD:
            print(f"  ⛔ AUTO-STOP: solo quedan {_last_known_remaining} créditos (umbral={API_CREDITS_STOP_THRESHOLD}). Saltando {sport}.")
            continue

        cached = is_cache_valid(cache, sport) and not force
        data = fetch_data(sport, cache, force=force)

        if not cached:
            api_calls += 1
            time.sleep(0.5)  # delay cortés entre llamadas

        # ── ENRICH: specialty markets (btts, dnb, dc, h1/h2, corners, cards) ──
        # Solo para soccer (MLB no tiene estos mercados). Solo eventos próximos
        # a jugarse (ENRICH_DAYS_AHEAD). Con cache 6h.
        if sport.startswith("soccer_") and data:
            n_enr = enrich_events_with_specialty(sport, data, cache)
            if n_enr > 0:
                print(f"   ✨ {n_enr} eventos enriquecidos con specialty markets")

        rows = [r for m in data if (r := parse_match(m, sport))]
        upsert_matches(rows)
        total_rows += len(rows)
        print(f"   ✅ {len(rows)} partidos procesados")

    # Limpieza de duplicados stale: si la API corrigió la fecha de un fixture
    # (placeholder 00:00 UTC → kickoff real), elimina la row vieja para que la
    # pipeline no vea 2 versiones del mismo partido.
    _cleanup_stale_duplicates()

    print(f"\n✅ DONE — {total_rows} partidos totales | {api_calls} llamadas API realizadas")

    # Creditos estimados consumidos (1 region × 2 mercados = 2 por liga)
    n_markets = len(ODDS_MARKETS.split(","))
    n_regions = len(ODDS_REGION.split(","))
    creditos_estimados = api_calls * n_markets * n_regions
    print(f"💳 Creditos estimados usados: ~{creditos_estimados}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Ignorar cache y forzar fetch")
    args = parser.parse_args()
    update_all(force=args.force)
