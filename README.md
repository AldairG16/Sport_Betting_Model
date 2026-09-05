# Sports Betting Model

Sistema automatizado de predicción de apuestas deportivas basado en modelos estadísticos, Kelly Criterion y seguimiento de CLV (Closing Line Value).

## Stack

- **Modelo**: Dixon-Coles + Ensemble (Poisson, ELO, xG, H2H, forma reciente)
- **Mercados**: 1X2, Over/Under, BTTS, Asian Handicap, DNB, Doble Oportunidad, Córners, Tiros, Tarjetas
- **Ligas**: 15 ligas activas (Europa, Asia, América) — la lista viva es `SPORT_KEYS` en `config/settings.py`
- **DB**: PostgreSQL + SQLAlchemy
- **Alertas**: Telegram Bot
- **Automatización**: GitHub Actions cron (`.github/workflows/`)

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

# Configuración opcional — estos son los defaults vigentes en
# config/settings.py. Omitir la variable deja el default; NO la pongas
# vacía en GitHub Actions esperando otro valor.
USER_TIMEZONE=America/Mexico_City
API_TTL_HOURS=12
FETCH_DAYS_AHEAD=7
INITIAL_BANKROLL=100.0
API_CREDITS_ALERT_THRESHOLD=2000
API_CREDITS_STOP_THRESHOLD=500
```

La plantilla completa, con todas las variables y sus defaults, está en
[`.env.example`](.env.example). La fuente de verdad de cualquier default es
`config/settings.py`: si un número de este README y uno de ahí no coinciden,
manda `config/settings.py` y el README está mal.

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

# Evening (07:30 AM MX) — resultados + CLV + resumen + preview del día
python scripts/orchestrator.py --mode evening

# Solo resultados — actualiza bets pendientes
python scripts/orchestrator.py --mode results

# Semanal (Lunes 07:00 AM) — recarga histórica + calibración + reporte
python scripts/orchestrator.py --mode weekly

# Forzar fetch aunque el cache sea válido
python scripts/orchestrator.py --mode morning --force-fetch
```

### Schedule real (GitHub Actions)

Producción corre **entera** en GitHub Actions: ninguna máquina local ni
programador del sistema operativo participa. Cada loop es su propio workflow y el
`cron` de ese archivo es la única fuente de verdad del horario. La columna
UTC es la que aparece literalmente en el YAML; la columna MX es UTC-6.

| Workflow | cron (UTC) | Hora MX | Qué corre |
|---|---|---|---|
| `morning.yml` | `0 12 * * *` | 06:00 diario | `orchestrator.py --mode morning` — fetch odds + predicciones + Telegram |
| `closing.yml` | `0 18 * * *` | 12:00 diario | `orchestrator.py --mode closing` — captura closing odds para CLV |
| `evening.yml` | `30 13 * * *` | 07:30 diario | `orchestrator.py --mode evening` — resultados + CLV + resumen + preview |
| `weekly.yml` | `0 13 * * 1` | Lunes 07:00 | `orchestrator.py --mode weekly`, después `clv_audit.py` (continue-on-error) |
| `late_results.yml` | `0 6 * * *` y `30 12 * * *` | 00:00 y 06:30 | `orchestrator.py --mode results` — atrapa partidos que cerraron tarde |
| `pre_kickoff.yml` | `7,22,37,52 * * * *` | cada 15 min | `pre_kickoff_analyst.py` — minutos off-peak a propósito (ver abajo) |
| `resolve_pending.yml` | `0 16 * * *` y `0 4 * * *` | 10:00 y 22:00 | `resolve_pending_bets.py --hours-lag 6 --limit 15` |
| `watchdog.yml` | `17 0,6,12,18 * * *` | 4×/día | `watchdog.py` — alerta si un loop no corrió |
| `tests.yml` | por push/PR | — | `pytest tests/` |

> **Por qué minutos raros**: GitHub Actions descarta schedules `*/N` en los
> slots pico (`:00/:15/:30/:45`). Cualquier cron más frecuente que horario
> usa minutos off-peak — de ahí `7,22,37,52` en `pre_kickoff.yml`.

La auditoría estática (`python -m tools.audit`) todavía no tiene workflow
propio: hoy se corre a mano y escribe `audits/latest.json` + `audits/latest.md`.

Para lanzar un workflow a mano: **Actions** → elegir el workflow → *Run
workflow*.

---

## Estructura del Proyecto

Sólo los directorios de primer nivel y los archivos que hace falta nombrar.
El inventario completo de `scripts/` — con trigger, idempotencia y si escribe
a la DB — vive en [`docs/scripts_inventory.md`](docs/scripts_inventory.md).

```
Sports_Betting_Model/
├── .github/workflows/       # Toda la automatización de producción (cron)
├── config/
│   ├── settings.py          # ÚNICA fuente de verdad de los defaults
│   ├── database.py          # Conexión a PostgreSQL
│   └── calibration_factors.json  # Regenerado los lunes por step_calibration
├── scripts/                 # Entry points ejecutables (orchestrator, agents,
│                            # fetchers, watchdog, auditorías read-only)
├── src/
│   ├── pipeline/            # prediction_pipeline.py + bet_decision.py
│   ├── models/              # Dixon-Coles, ensemble, Kelly, CLV, calibración
│   ├── features/            # ELO, forma, xG, H2H, line movement, clima
│   ├── dashboard/           # betting_dashboard.py (lo usa el pipeline)
│   └── utils/               # Normalización de equipos, budget de Anthropic
├── tools/
│   └── audit/               # Auditoría estática del repo (`python -m tools.audit`)
├── audits/                  # Salida de la auditoría: latest.json + latest.md
├── docs/
│   ├── schema.md            # Esquema autoritativo de la DB
│   └── scripts_inventory.md # Qué hace cada script y si escribe a la DB
├── archive/                 # Módulos fuera de uso, con su disposición en
│                            # ARCHIVE.md. No se borra nada: se archiva.
├── tests/                   # pytest — conftest.py arranca sin DB
├── data/                    # Generado en runtime (paper_trades.jsonl,
│                            # clv_cache.json, features procesadas)
├── logs/                    # Generado en runtime (auto-rotación 30 días)
├── requirements.txt         # Dependencias con versiones fijadas
├── .env.example             # Plantilla de configuración (commiteada)
└── .env                     # Secrets reales (NUNCA commitear)
```

---

## Base de Datos

El esquema autoritativo — cada tabla, cada columna y el sitio DDL que la crea —
está en **[`docs/schema.md`](docs/schema.md)**. Se mantiene sincronizado con el
código por `tests/test_schema_doc_sync.py`, así que es el único lugar donde hay
que buscar y el único que hay que actualizar.

Las tablas se crean automáticamente la primera vez que corre el pipeline.

---

## Tests

```bash
python -m pytest tests/ -v                       # todo
python -m pytest tests/test_kelly.py -v          # un archivo
python -m pytest tests/test_kelly.py::test_kelly_caps -v   # un test
```

`tests/conftest.py` arranca la suite sin base de datos ni secretos, así que
corre igual en local y en `tests.yml`. La cobertura se agrupa en:

- **Decisión de apuesta** — filtros de calidad, Kelly (bankroll 0, odds `None`),
  y el fixture congelado de `tests/fixtures/` que prueba que un refactor no
  cambió ninguna apuesta.
- **Robustez del pipeline** — Dixon-Coles, Poisson, normalización de equipos,
  resolución de mercados.
- **Auditoría estática** — un archivo `test_audit_*.py` por checker de
  `tools/audit/`.
- **Contratos del repo** — imports que resuelven, constantes centralizadas en
  `config/settings.py`, y los documentos de `docs/` en sincronía con el código.

No se declara aquí un número de tests: ese número se desactualiza al día
siguiente. `pytest` lo reporta al correr.

---

## Protecciones de Producción

| Protección | Descripción |
|------------|-------------|
| **Circuit Breaker** | Si bankroll < 10u → pausa apuestas + alerta Telegram |
| **Auditoría estática** | `python -m tools.audit` rankea los defectos que pueden costar dinero; `--ci` bloquea el PR |
| **API Auto-Stop** | Si créditos < `API_CREDITS_STOP_THRESHOLD` (500) → detiene fetch de ligas |
| **Crash Reports** | Errores fatales → notificación Telegram con traceback |
| **Task Locking** | Previene ejecuciones concurrentes (lock file) |
| **Log Rotation** | Limpia logs automáticamente > 30 días |
| **Stale Cache Warning** | Avisa si las odds tienen > 24h de antigüedad |
| **Unresolved Timeout** | Bets sin resultado > 7 días → marcadas como unresolved |
| **Telegram Retry** | Reintentos con backoff exponencial (3 intentos) |

---

## Ligas Activas

15 ligas activas. La lista vigente es `SPORT_KEYS` en `config/settings.py`,
donde cada liga bloqueada quedó comentada con la fecha y el motivo — esta tabla
sólo la resume.

| Tier | Ligas activas |
|------|-------|
| **Tier 1** (ROI positivo confirmado) | Brasil, EFL Championship, México, Bundesliga, Argentina, Portugal, La Liga |
| **Tier 2** (Monitoreando) | Premier League, Ligue 1, Serie A |
| **Tier 3** (Mercados blandos) | Bélgica, Grecia |
| **Tier 4** (Asia/Escandinavia) | Corea, Suecia |
| **Américas** | MLS |

**Bloqueadas** (comentadas en `SPORT_KEYS`, no se apuestan): Escocia, Turquía,
Japón, Noruega, China. El Mundial 2026 se auto-agrega el 2026-06-11 pero entra
en paper-trading — ver el kill-switch abajo.

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
2. Re-ejecutar el workflow `morning.yml` (manual `workflow_dispatch` o esperar al cron).
3. Verificar en Telegram que las apuestas del Mundial ya **no** llevan tag `[PAPER]`.

**Desactivar de emergencia** (si el modelo empieza a perder feo en el Mundial):

1. Actions → Settings del workflow → editar el secret → cambiar a `false`
2. Re-ejecutar `morning.yml`. Cualquier bet **ya colocada** sigue resolviéndose
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
