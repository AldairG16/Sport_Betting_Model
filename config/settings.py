"""
config/settings.py
==================
Centraliza toda la configuracion del proyecto.
Lee variables de .env automaticamente.
"""

import os
from pathlib import Path

# Carga .env si existe (sin necesidad de python-dotenv)
def _load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_env()

# ============================================================
# DATABASE
# ============================================================
DB_URL = os.environ.get("DB_URL", "")
if not DB_URL:
    raise RuntimeError(
        "DB_URL no configurada. Agrega DB_URL=postgresql+psycopg2://user:pass@host/db en .env"
    )

# ============================================================
# ODDS API
# ============================================================
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
ODDS_REGION      = os.environ.get("ODDS_REGION", "eu")          # 1 region = ahorro de creditos
ODDS_REGION_MLB  = os.environ.get("ODDS_REGION_MLB", "us")      # MLB usa libros de EE.UU.
ODDS_MARKETS     = os.environ.get("ODDS_MARKETS", "h2h,totals,spreads")
API_TTL_HOURS    = int(os.environ.get("API_TTL_HOURS", "8"))     # 8h = ~1 fetch/dia = ~270 creditos/mes
FETCH_DAYS_AHEAD = int(os.environ.get("FETCH_DAYS_AHEAD", "7"))
API_CREDITS_ALERT_THRESHOLD = int(os.environ.get("API_CREDITS_ALERT_THRESHOLD", "100"))

# ============================================================
# BANKROLL
# ============================================================
# Capital inicial en unidades. Agrega INITIAL_BANKROLL=1000 en .env
# para usar una cantidad diferente. Este valor solo se usa la primera
# vez que se inicializa la tabla bankroll en la DB.
INITIAL_BANKROLL = float(os.environ.get("INITIAL_BANKROLL", "100"))

# ============================================================
# WEATHER API (OpenWeatherMap — gratis 1,000 calls/día)
# Registro: https://openweathermap.org/api
# Agrega WEATHER_API_KEY=tu_clave en el archivo .env
# ============================================================
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")

# ============================================================
# TELEGRAM
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ============================================================
# LIGAS ACTIVAS
# ============================================================
# Zona horaria local del usuario (para calcular "hoy" y "mañana" correctamente)
# Cambia este valor en .env si estás en otra zona: USER_TIMEZONE=America/New_York
USER_TIMEZONE = os.environ.get("USER_TIMEZONE", "America/Mexico_City")

SPORT_KEYS = [
    # ── Tier 1: Ligas RENTABLES (walk-forward ROI positivo) ──────────
    "soccer_brazil_campeonato",           # +53.7% ROI, mejor liga del modelo
    "soccer_efl_champ",                   # +19.2% ROI, muchos partidos, lineas blandas
    "soccer_mexico_ligamx",               # +39.7% ROI, mercado menos eficiente
    "soccer_germany_bundesliga",          # +38.6% ROI, datos muy buenos
    "soccer_argentina_primera_division",  # +10.3% ROI, alta varianza
    "soccer_portugal_primeira_liga",      # +25.8% ROI, mercados blandos
    "soccer_spain_la_liga",               # +10.0% ROI, estable
    "soccer_spl",                         # 2189 partidos historicos, datos ya cargados

    # ── Tier 2: Big 5 con ROI marginal (monitoreando) ───────────────
    "soccer_epl",                         # -25% ROI pero muestra chica (7 bets)
    "soccer_france_ligue_one",            # -18% ROI, tough league
    "soccer_italy_serie_a",              # -23% ROI, tough league

    # ── Tier 3: Europa — mercados blandos, pocos sharps ────────────
    "soccer_turkey_super_league",         # 2276 matches, mercado blando
    "soccer_belgium_first_div",           # 2058 matches, predecible
    "soccer_greece_super_league",         # 1865 matches, MUY blando

    # ── Tier 4: Asia + Scandinavia — calendario verano, blandos ──
    "soccer_japan_j_league",              # 4523 matches, mas citada para rentabilidad
    "soccer_korea_kleague1",              # 3403 matches, predecible
    "soccer_norway_eliteserien",          # 3403 matches, goleadora, liga verano
    "soccer_sweden_allsvenskan",          # 3392 matches, liga verano
    "soccer_china_superleague",           # 2840 matches, mercado MUY blando

    # ── Americas extra ──────────────────────────────────────────────
    "soccer_usa_mls",                     # -44% ROI, tough league (monitoreando)

    # ── REMOVIDAS (ahorro de creditos API) ──────────────────────────
    # ROI negativo confirmado en walk-forward de 332 bets.
    # "soccer_netherlands_eredivisie",     # -50.3% ROI
    # "soccer_conmebol_copa_libertadores", # -100% ROI
    # "soccer_fifa_world_cup_qualifiers_europe",  # -44.6% ROI
    # "soccer_uefa_champs_league",         # -100% ROI
    # "soccer_uefa_europa_league",         # -64.5% ROI

    # MLB (beisbol) — desactivado temporalmente para ahorrar creditos API
    # "baseball_mlb",
]
