# Sports Betting Model

Sistema automatizado de predicción de apuestas deportivas basado en modelos estadísticos, Kelly Criterion y seguimiento de CLV (Closing Line Value).

## Stack

- **Modelo**: Dixon-Coles + Ensemble (Poisson, ELO, xG, H2H, forma reciente)
- **Mercados**: 1X2, Over/Under, BTTS, Asian Handicap, DNB, Doble Oportunidad, Córners, Tiros, Tarjetas
- **Ligas**: 20 ligas activas (Europa, Asia, América)
- **DB**: PostgreSQL + SQLAlchemy
- **Alertas**: Telegram Bot
- **Automatización**: Windows Task Scheduler

---

## Instalación

### 1. Clonar e instalar dependencias

```bash
git clone https://github.com/AldairG16/Sport_Betting_Model.git
cd Sport_Betting_Model
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

Crea un archivo `.env` en la raíz del proyecto:

```env
# Base de datos PostgreSQL
DB_URL=postgresql+psycopg2://usuario:contraseña@localhost:5432/sports_betting

# The Odds API (https://the-odds-api.com)
ODDS_API_KEY=tu_api_key_aqui

# Telegram Bot (obtener con @BotFather)
TELEGRAM_BOT_TOKEN=tu_bot_token
TELEGRAM_CHAT_ID=tu_chat_id

# Configuración opcional
USER_TIMEZONE=America/Mexico_City
API_TTL_HOURS=8
FETCH_DAYS_AHEAD=7
INITIAL_BANKROLL=100
API_CREDITS_ALERT_THRESHOLD=100
API_CREDITS_STOP_THRESHOLD=50
```

### 3. Crear la base de datos

```sql
CREATE DATABASE sports_betting;
```

Las tablas se crean automáticamente la primera vez que corre el pipeline.

---

## Uso

### Modos del Orchestrator

```bash
# Mañana (06:00 AM) — fetch odds + predicciones + Telegram
python scripts/orchestrator.py --mode morning

# Cierre (12:00 PM) — captura closing odds antes de los partidos
python scripts/orchestrator.py --mode closing

# Noche (23:00 PM) — resultados + CLV + resumen + preview mañana
python scripts/orchestrator.py --mode evening

# Solo resultados — actualiza bets pendientes
python scripts/orchestrator.py --mode results

# Semanal (Lunes 07:00 AM) — recarga histórica + calibración + reporte
python scripts/orchestrator.py --mode weekly

# Forzar fetch aunque el cache sea válido
python scripts/orchestrator.py --mode morning --force-fetch
```

### Schedule recomendado (Windows Task Scheduler)

| Hora | Modo | Descripción |
|------|------|-------------|
| 06:00 AM diario | `morning` | Fetch odds + predicciones + Telegram |
| 12:00 PM diario | `closing` | Captura odds de cierre |
| 23:00 PM diario | `evening` | Resultados + CLV + preview mañana |
| Lunes 07:00 AM | `weekly` | Recarga histórica + calibración |

Para configurar el scheduler automáticamente:

```powershell
powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1
```

---

## Estructura del Proyecto

```
Sports_Betting_Model/
├── config/
│   ├── settings.py          # Variables de configuración (lee de .env)
│   └── database.py          # Conexión a PostgreSQL
├── scripts/
│   ├── orchestrator.py      # Runner principal automatizado
│   ├── notify_telegram.py   # Notificaciones Telegram
│   ├── update_upcoming_matches.py  # Fetch odds desde The Odds API
│   ├── fetch_results.py     # Descarga resultados de partidos
│   ├── load_historical_data.py     # Carga datos históricos
│   └── load_extra_leagues.py       # Ligas Asia/Scandinavia
├── src/
│   ├── pipeline/
│   │   └── prediction_pipeline.py  # Pipeline principal de predicción
│   ├── models/
│   │   ├── dixon_coles_model.py    # Modelo Dixon-Coles
│   │   ├── ensemble_model.py       # Ensemble de modelos
│   │   ├── betting_engine.py       # Edge calculation + Kelly
│   │   ├── bankroll_manager.py     # Gestión de bankroll en DB
│   │   ├── save_bets.py            # Guardar y resolver apuestas
│   │   ├── clv_tracker.py          # Closing Line Value
│   │   ├── walkforward_backtest.py # Backtest walk-forward
│   │   └── calibration_monitor.py  # Calibración Brier
│   ├── features/
│   │   ├── elo_rating.py           # ELO ratings
│   │   ├── team_form.py            # Forma reciente
│   │   ├── xg_proxy.py             # Expected Goals proxy
│   │   ├── h2h_stats.py            # Head-to-head
│   │   ├── line_movement.py        # Movimiento de líneas
│   │   └── weather_impact.py       # Impacto del clima
│   └── utils/
│       └── team_normalizer.py      # Normalización de nombres
├── tests/
│   ├── test_bet_filters.py   # Tests para filtros de calidad
│   ├── test_kelly.py         # Tests para Kelly Criterion
│   └── test_pipeline_safety.py  # Tests de robustez del pipeline
├── logs/                     # Logs de ejecución (auto-rotación 30 días)
├── data/
│   └── processed/            # Features calculadas
├── requirements.txt          # Dependencias con versiones fijadas
└── .env                      # Secrets (NO commitear)
```

---

## Tablas de la Base de Datos

| Tabla | Descripción |
|-------|-------------|
| `matches` | Resultados históricos (~79K partidos) |
| `upcoming_matches` | Partidos próximos con odds en vivo |
| `bets_history` | Historial completo de apuestas |
| `bankroll` | Estado actual del bankroll |
| `bankroll_history` | Log de movimientos del bankroll |

---

## Tests

```bash
python -m pytest tests/ -v
```

33 tests cubriendo:
- Filtros de calidad de apuestas
- Kelly Criterion (casos límite: bankroll 0, odds None, etc.)
- Robustez del pipeline (Dixon-Coles, Poisson, normalización)

---

## Protecciones de Producción

| Protección | Descripción |
|------------|-------------|
| **Circuit Breaker** | Si bankroll < 10u → pausa apuestas + alerta Telegram |
| **API Auto-Stop** | Si créditos < 50 → detiene fetch de ligas |
| **Crash Reports** | Errores fatales → notificación Telegram con traceback |
| **Task Locking** | Previene ejecuciones concurrentes (lock file) |
| **Log Rotation** | Limpia logs automáticamente > 30 días |
| **Stale Cache Warning** | Avisa si las odds tienen > 24h de antigüedad |
| **Unresolved Timeout** | Bets sin resultado > 7 días → marcadas como unresolved |
| **Telegram Retry** | Reintentos con backoff exponencial (3 intentos) |

---

## Ligas Activas

| Tier | Ligas |
|------|-------|
| **Tier 1** (ROI positivo confirmado) | Brasil, EFL Championship, México, Bundesliga, Argentina, Portugal, La Liga, Escocia |
| **Tier 2** (Monitoreando) | Premier League, Ligue 1, Serie A |
| **Tier 3** (Mercados blandos) | Turquía, Bélgica, Grecia |
| **Tier 4** (Asia/Escandinavia) | Japón, Corea, Noruega, Suecia, China |
| **Américas** | MLS |
