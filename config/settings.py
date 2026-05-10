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
# ENV HELPERS — robustos a string vacío
# ============================================================
# IMPORTANTE: `int(os.environ.get("X", "default"))` TRUENA si la env var X
# existe pero es string vacío. Esto pasa típicamente en GitHub Actions cuando
# `${{ secrets.X }}` se expande a "" porque el secret no está configurado.
# El ValueError resultante aborta el pipeline antes de escribir nada → fue
# la causa real del freeze de upcoming_matches desde 17-abr-26.
# Estos helpers devuelven el default si la var no existe, es "", o no parsea.

def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    if not v or not v.strip():
        return default
    try:
        return int(v.strip())
    except (ValueError, TypeError):
        return default


def env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "")
    if not v or not v.strip():
        return default
    try:
        return float(v.strip())
    except (ValueError, TypeError):
        return default


def env_str(name: str, default: str) -> str:
    """Lee un env string. Devuelve default si la var no existe o es "" / whitespace.

    Necesario para GitHub Actions: cuando un secret no está configurado,
    `${{ secrets.X }}` se expande a "" y `os.environ.get("X", "default")`
    devuelve "" (no `default`). Eso fue lo que rompió ODDS_MARKETS al
    expandirse a string vacío y mandar `markets=` (parser inválido) a la API.
    """
    v = os.environ.get(name, "")
    if not v or not v.strip():
        return default
    return v.strip()


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
ODDS_REGION      = env_str("ODDS_REGION", "eu")          # 1 region = ahorro de creditos
ODDS_REGION_MLB  = env_str("ODDS_REGION_MLB", "us")      # MLB usa libros de EE.UU.
# SOLO markets "featured" en /odds — la API responde 422 INVALID_MARKET si
# se incluyen specialty (btts, draw_no_bet, double_chance, h1/h2, corners,
# cards). Esos se piden vía /events/{id}/odds en update_upcoming_matches.py
# (función fetch_event_specialty_markets / SPECIALTY_MARKETS).
#
# NO uses os.environ.get() crudo: si el secret de GH Actions no está
# configurado, se expande a "" y "" gana al default → manda `markets=`
# vacío a la API y rompe TODAS las ligas (bug del 26-abr-26).
ODDS_MARKETS     = env_str("ODDS_MARKETS", "h2h,totals,spreads")
# TTL 12h = 2 fetches/día (morning + evening), closing reutiliza cache del morning
API_TTL_HOURS    = env_int("API_TTL_HOURS", 12)
FETCH_DAYS_AHEAD = env_int("FETCH_DAYS_AHEAD", 7)
# Alerta temprana. Plan $30/mes = 20K créditos. Avisa cuando queden ≤2000
# (~10% del total ≈ 3 días de margen al ritmo de ~600-700 créditos/día).
# Override con API_CREDITS_ALERT_THRESHOLD=... en .env o en secrets de GH.
API_CREDITS_ALERT_THRESHOLD = env_int("API_CREDITS_ALERT_THRESHOLD", 2000)

# ============================================================
# BANKROLL
# ============================================================
# Capital inicial en unidades. Agrega INITIAL_BANKROLL=1000 en .env
# para usar una cantidad diferente. Este valor solo se usa la primera
# vez que se inicializa la tabla bankroll en la DB.
INITIAL_BANKROLL = env_float("INITIAL_BANKROLL", 100.0)

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

# Bot dedicado para mensajes del pre-kickoff analyst.
# Si no se configuran, cae al bot principal (backward compatible).
# Se separó para no saturar el canal con SKIPs (cron cada 15 min puede
# generar varios mensajes/día).
TELEGRAM_BOT_TOKEN_PREKICKOFF = os.environ.get("TELEGRAM_BOT_TOKEN_PREKICKOFF", "")
TELEGRAM_CHAT_ID_PREKICKOFF   = os.environ.get("TELEGRAM_CHAT_ID_PREKICKOFF", "")

# ============================================================
# ANTHROPIC API (Pre-Kickoff Analyst)
# ============================================================
# Usado por scripts/pre_kickoff_analyst.py para validar apuestas
# 30-75 min antes del kickoff con Claude + web_search.
# Si la var no está, el script imprime warning y sale sin error.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Ventana (en minutos desde NOW) para considerar bets como "pre-kickoff".
# Default 30-90 = atrapa partidos que arrancan en ~30-90 min, justo cuando
# las alineaciones probables/confirmadas y noticias de lesiones ya están
# publicadas (típicamente 30-60 min antes del kickoff).
# Combinado con cron-job externo cada 15 min (más confiable que GH Actions),
# da 4 oportunidades de capturar cada partido y resiste delays moderados.
PRE_KICKOFF_WINDOW_MIN = env_int("PRE_KICKOFF_WINDOW_MIN", 30)
PRE_KICKOFF_WINDOW_MAX = env_int("PRE_KICKOFF_WINDOW_MAX", 90)

# Threshold de edge mínimo (en puntos porcentuales) para que el analista
# diga APUESTA. La regla es: probability_estimada >= prob_implicita + N.
# Default 3 = relativamente conservador. Subí a 5 para ser más estricto
# (menos apuestas pero mejor edge); bajá a 1-2 para ser más agresivo.
ANALYST_EDGE_THRESHOLD = env_int("ANALYST_EDGE_THRESHOLD", 3)

# ============================================================
# LIGAS ACTIVAS
# ============================================================
# Zona horaria local del usuario (para calcular "hoy" y "mañana" correctamente)
# Cambia este valor en .env si estás en otra zona: USER_TIMEZONE=America/New_York
USER_TIMEZONE = os.environ.get("USER_TIMEZONE", "America/Mexico_City")

# ============================================================
# WORLD CUP 2026 KILL-SWITCH
# ============================================================
# Default: False → modelo NO apuesta dinero real en `soccer_fifa_world_cup`.
# Cuando False, las predicciones del Mundial:
#   - NO se insertan en bets_history (no afectan bankroll/ROI/CLV)
#   - Se loguean a data/paper_trades.jsonl
#   - Se muestran en Telegram con tag [PAPER]
#   - Pre-kickoff analyst NO las analiza (no están en bets_history)
#
# Para activar dinero real en Mundial:
#   1. Validar walk-forward 2022 con ROI ≥ 0
#   2. Validar paper-trading 1-2 semanas
#   3. Setear WORLD_CUP_BETTING_ENABLED=true como secret de GitHub
#   4. Re-ejecutar el workflow
#
# Variable de entorno: WORLD_CUP_BETTING_ENABLED ("true" / "false")
WORLD_CUP_BETTING_ENABLED = env_str("WORLD_CUP_BETTING_ENABLED", "false").lower() == "true"

# Ligas tratadas como "paper-only" hasta que el kill-switch esté en true.
PAPER_ONLY_LEAGUES = set()
if not WORLD_CUP_BETTING_ENABLED:
    PAPER_ONLY_LEAGUES.add("soccer_fifa_world_cup")


SPORT_KEYS = [
    # ── Tier 1: Ligas RENTABLES (walk-forward ROI positivo) ──────────
    "soccer_brazil_campeonato",           # +53.7% ROI, mejor liga del modelo
    "soccer_efl_champ",                   # +19.2% ROI, muchos partidos, lineas blandas
    "soccer_mexico_ligamx",               # +39.7% ROI, mercado menos eficiente
    "soccer_germany_bundesliga",          # +38.6% ROI, datos muy buenos
    "soccer_argentina_primera_division",  # +10.3% ROI, alta varianza
    "soccer_portugal_primeira_liga",      # +25.8% ROI, mercados blandos
    "soccer_spain_la_liga",               # +10.0% ROI, estable
    # "soccer_spl",                       # BLOQUEADA 06-may-26: -2.45u/6 bets en 90d (-52% ROI), mercado pequeño

    # ── Tier 2: Big 5 con ROI marginal (monitoreando) ───────────────
    "soccer_epl",                         # -25% ROI pero muestra chica (7 bets)
    "soccer_france_ligue_one",            # -18% ROI, tough league
    "soccer_italy_serie_a",              # -23% ROI, tough league

    # ── Tier 3: Europa — mercados blandos, pocos sharps ────────────
    # "soccer_turkey_super_league",       # BLOQUEADA 04-may-26: -1.05u/12 bets en 60d (mercado se hizo eficiente)
    "soccer_belgium_first_div",           # 2058 matches, predecible
    "soccer_greece_super_league",         # 1865 matches, MUY blando

    # ── Tier 4: Asia + Scandinavia — calendario verano, blandos ──
    # "soccer_japan_j_league",            # BLOQUEADA 04-may-26: -12.46u/24 bets en 60d, 17% WR — peor liga del modelo
    "soccer_korea_kleague1",              # 3403 matches, predecible
    # "soccer_norway_eliteserien",        # BLOQUEADA 04-may-26: solo 32 partidos historicos — sample insuficiente
    "soccer_sweden_allsvenskan",          # 3392 matches, liga verano
    # "soccer_china_superleague",         # BLOQUEADA 06-may-26: -1.12u/5 bets en 60d (-22% ROI), liga lejana → ahorra créditos API

    # ── Americas extra ──────────────────────────────────────────────
    "soccer_usa_mls",                     # -44% ROI, tough league (monitoreando)

    # ── Mundial 2026 (auto-activado el 11 de junio de 2026) ─────────
    # La función _check_world_cup_activation() en orchestrator.py lo
    # agrega automáticamente al iniciar si la fecha >= 2026-06-11.
    # "soccer_fifa_world_cup",            # Activado vía orchestrator

    # ── NOTAS IMPORTANTES sobre keys de torneos ─────────────────────
    # • Liga MX Liguilla (playoffs): NO existe key separada en The Odds API.
    #   La fase final cae bajo "soccer_mexico_ligamx" automáticamente.
    # • UCL knockout: mismo caso — "soccer_uefa_champs_league" cubre
    #   fase de grupos Y eliminatorias.  Se removió abajo por ROI negativo.
    # • UECL (Conference League) existe como "soccer_uefa_europa_conference_league"
    #   pero no se agrega aún — sin backtest histórico con muestra suficiente.

    # ── REMOVIDAS (ahorro de creditos API) ──────────────────────────
    # ROI negativo confirmado en walk-forward de 332 bets.
    # "soccer_netherlands_eredivisie",     # -50.3% ROI
    # "soccer_conmebol_copa_libertadores", # -100% ROI
    # "soccer_fifa_world_cup_qualifiers_europe",  # -44.6% ROI
    # "soccer_uefa_champs_league",         # -100% ROI (incluye knockout)
    # "soccer_uefa_europa_league",         # -64.5% ROI
    # "soccer_uefa_europa_conference_league",  # sin backtest — no activada

    # MLB (beisbol) — desactivado temporalmente para ahorrar creditos API
    # "baseball_mlb",
]
