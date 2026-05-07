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

---

## Kill-Switch — Mundial 2026 (Paper-Trading)

`soccer_fifa_world_cup` se activa automáticamente el **2026-06-11** desde
`scripts/orchestrator.py::_check_world_cup_activation()`. Para evitar que el
modelo apueste dinero real con datos de selecciones nacionales aún no validados,
está protegido por un **kill-switch**.

### Cómo funciona

| `WORLD_CUP_BETTING_ENABLED` | Comportamiento de bets del Mundial |
|---|---|
| `false` (default) | Predicciones se loguean a `data/paper_trades.jsonl` y aparecen en Telegram bajo la sección `📝 [PAPER]`. **NO entran a `bets_history`** → no afectan bankroll, ROI, ni CLV. |
| `true` | Se tratan como cualquier otra liga: insertadas en `bets_history`, descontadas del bankroll y rastreadas para CLV. |

El pre-kickoff analyst (`scripts/pre_kickoff_analyst.py`) **NO analiza** paper
bets porque solo lee de `bets_history`.

### Cómo activar (para apostar dinero real en el Mundial)

**Pre-requisitos antes de activar**:

1. **Walk-forward 2022 con ROI ≥ 0** — backtest contra el Mundial pasado.
2. **Paper-trading 1-2 semanas durante el torneo** — revisar `data/paper_trades.jsonl`
   contra resultados reales:
   ```sql
   -- Cuando el Mundial empiece, comparar paper picks vs bets_history.results
   -- (los partidos del Mundial sí se cargan a `matches` aunque no se apueste)
   ```
3. **Calibración Brier ≤ 0.25** específica del Mundial.
4. **Lineups data-loader** funcionando para selecciones nacionales.

**Activar**:

1. **GitHub Settings** → Secrets and variables → Actions → New repository secret
   - **Name**: `WORLD_CUP_BETTING_ENABLED`
   - **Value**: `true`
2. Re-ejecutar el workflow `daily.yml` (manual `workflow_dispatch` o esperar al cron).
3. Verificar en Telegram que las apuestas del Mundial ya **no** llevan tag `[PAPER]`.

**Desactivar de emergencia** (si el modelo empieza a perder feo en el Mundial):

1. Actions → Settings del workflow → editar el secret → cambiar a `false`
2. Re-ejecutar `daily.yml`. Cualquier bet **ya colocada** sigue resolviéndose
   normal — solo se detienen las nuevas.

### Auditoría de paper-trades

```bash
# Ver cuántas predicciones paper hay del día de hoy
python -c "from scripts.notify_telegram import _read_paper_trades_for_date; \
           from datetime import date; \
           print(len(_read_paper_trades_for_date(date.today())))"

# Estadísticas globales
wc -l data/paper_trades.jsonl

# Después del Mundial: matchear paper trades contra resultados de `matches`
# para validar performance del modelo en selecciones nacionales antes de
# levantar el kill-switch en futuras ediciones.
```

### Walk-forward Mundial 2022

Antes de levantar el kill-switch hay que validar que la calibración funciona
contra un Mundial real. El script `scripts/validate_world_cup.py` corre un
walk-forward sobre los **64 partidos del Mundial 2022** (cargados via
`load_international_data.py`) y reporta Brier, win-rate predicho vs real,
ROI hipotético flat-stake (edge ≥ 5%) y factor de calibración sugerido.

```bash
# Validación completa (todos los mercados)
python scripts/validate_world_cup.py

# Solo un mercado
python scripts/validate_world_cup.py --market over25

# Otro año (cuando se carguen más mundiales históricos)
python scripts/validate_world_cup.py --year 2018
```

Ejemplo de output:

```
🌍 VALIDACIÓN MUNDIAL 2022
============================================================
Parámetros MLE: 534 equipos | fitted_at=2026-04-09
Partidos cargados: 64

Mercado      Brier   Pred%   Real%   Bias  ROI(flat)     WR    N
─────────────────────────────────────────────────────────────────
home_win    0.3065   36.1%   43.8%  -7.7pp     -10.0%  37.5%   24
draw        0.1944   28.4%   23.4%  +5.0pp     -52.9%  14.3%   14
away_win    0.2112   35.5%   32.8%  +2.7pp     +54.5%  48.3%   29
over25      0.3019   36.6%   46.9% -10.3pp     -44.3%  28.6%    7
```

**Criterio go/no-go**:

- ✅ **Activar kill-switch**: Brier ≤ 0.25 en los 4 mercados **y** ROI flat ≥ 0% **y** calibración ±15%.
- ⚠️ **No activar**: si algún mercado sale de rango → seguir en paper-trading hasta que se acumulen más datos previos al torneo o se ajuste la calibración por liga (`by_league` en `calibration_factors.json`).

El script es **read-only** sobre `matches` y no toca `bets_history`.
