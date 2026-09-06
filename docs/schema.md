# Esquema de base de datos

Descripción autoritativa de las tablas de PostgreSQL que el sistema lee o
escribe. Este documento es la **única** fuente de verdad sobre el esquema:
`README.md` lista tablas de memoria y está incompleto, y no existe ningún
directorio de migraciones contra el cual comparar.

## Alcance y honestidad del documento

El proyecto **no tiene framework de migraciones** y esta tarea no introduce
uno: la decisión se difirió a propósito. El esquema real es, hoy, la suma de
tres mecanismos dispersos:

1. **Creación implícita por pandas.** Las tablas base nacen la primera vez que
   un cargador hace `DataFrame.to_sql(...)`. No hay ningún `CREATE TABLE`
   explícito en el árbol para ellas — por eso `README.md` dice que «las tablas
   se crean automáticamente la primera vez que corre el pipeline». La forma
   inicial de la tabla la dicta el DataFrame, no un DDL revisable.
2. **Auto-migración por `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.** Cada
   columna añadida después de la creación implícita se parchea en caliente al
   arrancar el fetch. Es aditivo e idempotente, pero significa que el esquema
   solo está completo *después* de que ese código corrió al menos una vez.
3. **Uso sin definición.** Algunas tablas solo aparecen en `SELECT` /
   `INSERT`; su DDL vive fuera de este árbol.

Documentar esto no es un detalle de estilo: un lector que busque el
`CREATE TABLE` de `upcoming_matches` no lo va a encontrar, y sin esta nota
concluiría que la tabla no existe.

## Cómo se mantiene

El conjunto de tablas **no se redacta a mano**. Se deriva de
`tools.audit.checks.schema.table_inventory()` — el mismo inventario que
alimenta el checker de deriva de esquema del auditor estático. El test de
sincronía falla cuando aparece DDL de una tabla que aquí no tiene sección, o
cuando aquí se documenta una tabla que el código ni define ni consulta:

```bash
python -m pytest tests/test_schema_doc_sync.py -v
```

Dos límites conocidos del escaneo, que explican por qué el conjunto
autoritativo se restringe al código de producción:

- El DDL sintético de `tests/` y las expresiones regulares de `tools/` se ven
  como si fueran esquema. No lo son: el auditor jamás abre una conexión a
  PostgreSQL. Ambos prefijos se excluyen.
- El escaneo es textual, así que un `FROM` en prosa castellana o en un
  comentario puede parecer SQL. En concreto, el comentario
  `# Phase 2: Corners from API` produce una «tabla» fantasma llamada
  `api`. **No es una tabla** y por eso no tiene sección aquí.

---

## `upcoming_matches`

Partidos futuros con las cuotas vivas de The Odds API. Es la tabla de entrada
del pipeline de predicción: cada fila es un (partido, snapshot de precios) y
de aquí salen las oportunidades que se evalúan.

Se **crea implícitamente** — no hay `CREATE TABLE` para ella en ningún punto
del árbol. Nace del `to_sql` del cargador histórico y a partir de ahí solo se
extiende por `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` desde `ensure_schema()`.

### Columnas base

Creadas implícitamente (tipos inferidos por pandas desde el DataFrame). Se
reconocen porque el `INSERT` las escribe pero ningún `ALTER TABLE` las añade.

| Columna | Tipo | Descripción |
|---|---|---|
| `match_key` | TEXT | Clave natural del partido; incluye la fecha derivada del kickoff. Es el objetivo del `ON CONFLICT`. |
| `match_date` | TEXT/TIMESTAMP | Kickoff. Se castea a `::timestamp` en cada consulta, señal de que se almacena como texto. |
| `league` | TEXT | Nombre de la liga. |
| `sport_key` | TEXT | Clave de liga de The Odds API (p. ej. `soccer_epl`). |
| `home_team` / `away_team` | TEXT | Nombres tal cual los devuelve la API, sin normalizar. |
| `home_odds` / `draw_odds` / `away_odds` | FLOAT | Mercado 1X2. |
| `over25_odds` / `under25_odds` | FLOAT | Over/Under 2.5 goles. |
| `btts_yes_odds` / `btts_no_odds` | FLOAT | Both Teams To Score. |

### Columnas añadidas por auto-migración

Todas se añaden en `ensure_schema()`, cada una con su propio
`ADD COLUMN IF NOT EXISTS`. La línea es el sitio DDL exacto.

| Columna | Tipo | Sitio DDL | Descripción |
|---|---|---|---|
| `updated_at` | TIMESTAMP DEFAULT NOW() | `scripts/update_upcoming_matches.py:749` | Marca de frescura. Es el desempate del cleanup de duplicados. |
| `home_team_norm` | TEXT | `scripts/update_upcoming_matches.py:753` | Nombre normalizado por `normalize_team()`. Toda consulta cruzada usa esta columna, no `home_team`. |
| `away_team_norm` | TEXT | `scripts/update_upcoming_matches.py:757` | Ídem para el visitante. |
| `opening_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:763` | Precio de apertura. Se escribe **una sola vez**, la primera que se ve el partido. |
| `opening_draw_odds` | FLOAT | `scripts/update_upcoming_matches.py:767` | Ídem, empate. |
| `opening_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:771` | Ídem, visitante. |
| `opening_over25_odds` | FLOAT | `scripts/update_upcoming_matches.py:775` | Ídem, over 2.5. Alimenta al Line Movement Tracker. |
| `consensus_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:780` | Precio promedio entre casas. Se refresca en cada fetch. |
| `consensus_draw_odds` | FLOAT | `scripts/update_upcoming_matches.py:784` | Ídem, empate. |
| `consensus_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:788` | Ídem, visitante. |
| `bookmaker_count` | INT DEFAULT 0 | `scripts/update_upcoming_matches.py:792` | Cuántas casas cotizaron. Proxy de liquidez del mercado. |
| `h2h_spread_pct` | FLOAT | `scripts/update_upcoming_matches.py:796` | Dispersión de precios 1X2; detecta líneas blandas. |
| `ah_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:801` | Asian Handicap local. |
| `ah_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:805` | Asian Handicap visitante. |
| `ah_line` | FLOAT | `scripts/update_upcoming_matches.py:809` | Línea del hándicap. Parametriza la market key (`ah_home_-0.5`). |
| `dnb_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:813` | Draw No Bet local. |
| `dnb_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:814` | Draw No Bet visitante. |
| `dc_1x_odds` | FLOAT | `scripts/update_upcoming_matches.py:816` | Doble oportunidad 1X. |
| `dc_x2_odds` | FLOAT | `scripts/update_upcoming_matches.py:817` | Doble oportunidad X2. |
| `dc_12_odds` | FLOAT | `scripts/update_upcoming_matches.py:818` | Doble oportunidad 12. |
| `h1_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:820` | 1X2 del primer tiempo, local. |
| `h1_draw_odds` | FLOAT | `scripts/update_upcoming_matches.py:821` | 1X2 del primer tiempo, empate. |
| `h1_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:822` | 1X2 del primer tiempo, visitante. |
| `h2_home_odds` | FLOAT | `scripts/update_upcoming_matches.py:824` | 1X2 del segundo tiempo, local. |
| `h2_draw_odds` | FLOAT | `scripts/update_upcoming_matches.py:825` | 1X2 del segundo tiempo, empate. |
| `h2_away_odds` | FLOAT | `scripts/update_upcoming_matches.py:826` | 1X2 del segundo tiempo, visitante. |
| `corners_over_odds` | FLOAT | `scripts/update_upcoming_matches.py:828` | Córners over. |
| `corners_under_odds` | FLOAT | `scripts/update_upcoming_matches.py:829` | Córners under. |
| `corners_line` | FLOAT | `scripts/update_upcoming_matches.py:830` | Línea de córners. |
| `cards_over_odds` | FLOAT | `scripts/update_upcoming_matches.py:832` | Tarjetas over. |
| `cards_under_odds` | FLOAT | `scripts/update_upcoming_matches.py:833` | Tarjetas under. |
| `cards_line` | FLOAT | `scripts/update_upcoming_matches.py:834` | Línea de tarjetas. |

### Índices y restricciones

No hay ningún `CREATE INDEX` en el árbol para esta tabla. La única
restricción observable es la que el upsert da por hecha:

- **Unicidad sobre `match_key`** — el `INSERT ... ON CONFLICT (match_key)`
  exige un índice único o clave primaria sobre esa columna. Como la tabla se
  crea implícitamente, esa restricción no está declarada en ningún DDL del
  repositorio: existe solo en la base viva.

### Gotcha operativo: duplicados stale

`match_key` incluye la fecha derivada del kickoff. Cuando la API corrige la
hora de un partido, el upsert **inserta una fila nueva** en vez de actualizar
la vieja, que queda stale. De ahí dos consecuencias que ningún consumidor
puede ignorar:

- Todo lector debe seleccionar con
  `DISTINCT ON (home_team_norm, away_team_norm, sport_key) ... ORDER BY updated_at DESC`.
- El fetch limpia al final de cada corrida: borra partidos pasados hace más de
  7 días y elimina las filas duplicadas cuya `updated_at` es más antigua.

**Definida en**

- `scripts/update_upcoming_matches.py:746` — `ensure_schema()`, la
  auto-migración completa: 32 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  entre las líneas 749 y 834.
- `scripts/update_upcoming_matches.py:840` — `upsert_matches()`; el
  `INSERT ... ON CONFLICT (match_key)` de la línea 846 es la evidencia de las
  columnas base y de la restricción de unicidad.
- `scripts/update_upcoming_matches.py:932` — `_cleanup_old_matches()`,
  `DELETE` de partidos con más de 7 días.
- `scripts/update_upcoming_matches.py:946` — `_cleanup_stale_duplicates()`,
  `DELETE` de filas duplicadas por corrección de kickoff.
- `scripts/update_upcoming_matches.py:415` — `SELECT DISTINCT sport_key`, la
  lectura que decide qué ligas refrescar.
- `scripts/capture_golden_fixture.py:131` — lectura con el `DISTINCT ON`
  canónico documentado arriba.
- Creación implícita: **no hay sitio DDL**. La tabla la crea `to_sql` en el
  cargador histórico, fuera de este árbol.

---

## `bets_history`

Historial completo de apuestas. El pipeline inserta cada bet con
`result='pending'` y el ciclo de resultados la voltea a `win` / `loss` /
`unresolved`. **Nunca se borra de esta tabla**: la ausencia de una fila
significa que el modelo no puntuó ese (partido, mercado), que es información
distinta de una fila `pending`.

Columnas conocidas por el uso en este árbol:

| Columna | Tipo | Descripción |
|---|---|---|
| `result` | TEXT | `pending` mientras no se resuelve; luego el desenlace. |
| `match_date` | TIMESTAMP | Kickoff. Base del criterio de «bet atascada» (>5 días en `pending`). |

### Sin `CREATE TABLE` en este árbol

No hay `CREATE TABLE` ni `ALTER TABLE` para `bets_history`: igual que
`upcoming_matches` y `matches`, es una tabla de creación implícita, y ningún
DDL del repositorio declara sus columnas. Sí hay, en cambio, tres `CREATE
INDEX` y el módulo completo que la escribe.

**Definida en**

- `src/models/save_bets.py:62` — `INSERT INTO bets_history (...)`; la
  escritura que define de facto el conjunto de columnas.
- `src/models/save_bets.py:384` — `UPDATE bets_history`; el paso que voltea
  `pending` a `win` / `loss` / `unresolved`.
- `scripts/orchestrator.py:194` — `CREATE INDEX IF NOT EXISTS
  idx_bets_result` sobre `result`.
- `scripts/orchestrator.py:195` — `CREATE INDEX IF NOT EXISTS
  idx_bets_match_date` sobre `match_date`.
- `scripts/orchestrator.py:196` — `CREATE INDEX IF NOT EXISTS
  idx_bets_league` sobre `league`.
- `scripts/watchdog.py:120` — `SELECT COUNT(*) FROM bets_history WHERE
  result = 'pending'`; el chequeo de salud que detecta bets bloqueadas.
- Creación implícita: **no hay sitio `CREATE TABLE`** en este árbol.

---

## `analyst_heartbeat`

Latido del pre-kickoff analyst. Cada corrida del cron escribe una fila; el
watchdog la usa para distinguir «no había nada que analizar» de «el cron dejó
de dispararse». Es la tabla que hace observable un fallo silencioso.

| Columna | Tipo | Descripción |
|---|---|---|
| `ran_at` | TIMESTAMP | Momento de la corrida. El watchdog alerta si el `MAX(ran_at)` tiene más de 3 h **y** estamos en horario de partidos. |

### Índices y restricciones

- `idx_heartbeat_ran_at` sobre `(ran_at DESC)` — sirve exactamente la consulta
  del watchdog, que solo pide el `MAX(ran_at)`.

**Definida en**

- `scripts/orchestrator.py:177` — `CREATE TABLE IF NOT EXISTS
  analyst_heartbeat`; el DDL canónico.
- `scripts/orchestrator.py:186` — `CREATE INDEX IF NOT EXISTS
  idx_heartbeat_ran_at`.
- `scripts/pre_kickoff_analyst.py:1054` — `INSERT INTO analyst_heartbeat`; la
  escritura, una por corrida del cron.
- `scripts/watchdog.py:145` — `SELECT MAX(ran_at) AS last_ran FROM
  analyst_heartbeat`; la lectura que gobierna la alerta de gap.

---

## `pre_kickoff_analyses`

Dictamen del pre-kickoff analyst: una fila por `(match, market, match_date)`.
Es un canal **informativo paralelo** — el analista nunca toca `bets_history`,
así que una fila aquí no crea, mueve ni resuelve ninguna apuesta. Sirve para
dos cosas: mandar el veredicto a Telegram y, con `probability` y `decision`,
alimentar el memo de aprendizaje que se construye cruzando esta tabla con
`bets_history` de los últimos 30 días.

A diferencia de las tres tablas anteriores, esta **sí tiene DDL en el árbol**:
es la única que se crea explícitamente en el código presente. El `UNIQUE(match,
market, match_date)` la hace idempotente — el cron corre cada 15 min y puede
volver a analizar el mismo partido varias veces dentro de la ventana de
kickoff sin duplicar el dictamen.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | SERIAL | Clave primaria. |
| `match` | TEXT | Partido analizado. `NOT NULL`. |
| `match_date` | TIMESTAMP | Kickoff. Parte de la clave de unicidad. `NOT NULL`. |
| `market` | VARCHAR(50) | Mercado dictaminado. Parte de la clave de unicidad. `NOT NULL`. |
| `verdict` | VARCHAR(20) | Dictamen del analista. `NOT NULL`. |
| `confidence` | INT | Confianza declarada. `NOT NULL`. |
| `reasoning` | TEXT | Razonamiento en prosa. |
| `lineups` | TEXT | Alineaciones consideradas. |
| `sources` | JSONB | Fuentes citadas por la búsqueda web. |
| `analyzed_at` | TIMESTAMP | `DEFAULT NOW()`. |
| `probability` | INT | Probabilidad estimada (0-100). Migración aditiva del 09-may-26, nullable → las filas anteriores la tienen vacía. |
| `decision` | VARCHAR(15) | `APUESTA` / `NO APUESTA`. Migración aditiva del 09-may-26, nullable por el mismo motivo. |

### Índices y restricciones

- **`UNIQUE(match, market, match_date)`** — declarada dentro del `CREATE
  TABLE`. Es la que hace segura la re-ejecución del analista.
- **`idx_prekickoff_match_date`** sobre `match_date` — soporta la consulta por
  ventana de kickoff, que es el único patrón de lectura del analista.

Las dos columnas añadidas usan `ADD COLUMN IF NOT EXISTS`, así que la
migración es idempotente y no rompe filas viejas: ambas son nullable a
propósito.

**Definida en**

- `scripts/orchestrator.py:150` — `CREATE TABLE IF NOT EXISTS
  pre_kickoff_analyses`; el DDL canónico, con el `UNIQUE(match, market,
  match_date)` en su última línea.
- `scripts/orchestrator.py:164` — `CREATE INDEX IF NOT EXISTS
  idx_prekickoff_match_date`.
- `scripts/orchestrator.py:169` — `ALTER TABLE … ADD COLUMN IF NOT EXISTS
  probability INT`.
- `scripts/orchestrator.py:170` — `ALTER TABLE … ADD COLUMN IF NOT EXISTS
  decision VARCHAR(15)`.

---

## `matches`

Historial de partidos jugados: la base de entrenamiento del modelo. De aquí
salen la fuerza de cada equipo, el ELO, la forma reciente y el H2H. Es la
tabla que los cargadores históricos llenan y la que el ciclo de resultados
completa cuando un partido termina.

Igual que `upcoming_matches`, se **crea implícitamente** — no hay `CREATE
TABLE` para ella en ningún punto del árbol. Nace del primer `INSERT` de los
cargadores y a partir de ahí solo se extiende por `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS`.

| Columna | Tipo | Descripción |
|---|---|---|
| `date` | DATE | Fecha del partido. Parte de la clave del `ON CONFLICT`. |
| `league` | TEXT | Liga en formato `sport_key`. |
| `season` | TEXT | Temporada de la que procede la fila. |
| `home_team` / `away_team` | TEXT | Nombres **ya normalizados** (minúsculas). Parte de la clave del `ON CONFLICT`. |
| `home_goals` / `away_goals` | INT | Marcador final. |
| `home_shots` / `away_shots` | INT | Tiros totales. |
| `home_shots_target` / `away_shots_target` | INT | Tiros a puerta; alimentan el proxy de xG. |
| `home_corners` / `away_corners` | INT | Córners; base del modelo de córners. |
| `home_yellow` / `away_yellow` / `home_red` / `away_red` | INT | Tarjetas. Añadidas por migración aditiva, nullable. |

### Índices y restricciones

- **Unicidad sobre `(date, home_team, away_team)`** — el `INSERT ... ON
  CONFLICT (date, home_team, away_team) DO NOTHING` la da por hecha, así que
  exige un índice único que **ningún DDL del árbol declara**: existe solo en
  la base viva. Es lo que hace idempotentes a los cargadores históricos.
- `idx_matches_home_date`, `idx_matches_away_date` sobre `(equipo, date)` —
  sirven las consultas de forma reciente y H2H, que siempre filtran por equipo
  y ordenan por fecha.
- `idx_matches_league` sobre `league`.

### Gotcha: los nombres tienen que venir normalizados

`home_team` y `away_team` guardan la forma normalizada. Un cargador que
inserte el nombre crudo de una API crea una fila que ninguna consulta
posterior encuentra, y las bets de ese partido se quedan en `pending` para
siempre. Por eso todo cargador pasa por `normalize_team()` antes del `INSERT`.

**Definida en**

- `scripts/load_historical_data.py:19` — `ALTER TABLE matches ADD COLUMN IF
  NOT EXISTS {col} INT` sobre las cuatro columnas de tarjetas; la única
  migración de esquema de la tabla.
- `scripts/load_historical_data.py:191` — el `INSERT INTO matches (...)` que
  define de facto el conjunto de columnas, con su `ON CONFLICT`.
- `scripts/orchestrator.py:190` — `CREATE INDEX IF NOT EXISTS
  idx_matches_home_date`.
- `scripts/orchestrator.py:191` — `CREATE INDEX IF NOT EXISTS
  idx_matches_away_date`.
- `scripts/orchestrator.py:192` — `CREATE INDEX IF NOT EXISTS
  idx_matches_league`.
- Creación implícita: **no hay sitio `CREATE TABLE`** en este árbol.

---

## `bankroll`

Estado del capital, **en una sola fila**. El dimensionado de Kelly lee de aquí
cuánto hay antes de calcular el tamaño de cada apuesta, así que una fila de
más aquí es un error de contabilidad, no de estilo.

Se crea explícitamente y, si la tabla queda vacía, se siembra con
`INITIAL_BANKROLL` de `config/settings.py`. Esa siembra ocurre **una sola
vez**: el `COUNT(*)` la protege de re-inicializarse en cada arranque, que
borraría el historial de ganancias al volver al capital inicial.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | SERIAL | Clave primaria. |
| `initial_bankroll` | FLOAT | Capital de partida. `NOT NULL`. |
| `current_bankroll` | FLOAT | Capital vigente; el que usa Kelly. `NOT NULL`. |
| `peak_bankroll` | FLOAT | Máximo histórico. Base del cálculo de drawdown. `NOT NULL`. |
| `total_deposited` | FLOAT | Suma de aportaciones. `NOT NULL`. |
| `last_updated` | TIMESTAMP | `DEFAULT NOW()`. |

**Definida en**

- `src/models/bankroll_manager.py:47` — `CREATE TABLE IF NOT EXISTS bankroll`.

---

## `bankroll_history`

Log de movimientos del capital: una fila por evento que mueve el saldo. Es la
tabla que permite reconstruir cómo se llegó al `current_bankroll` de arriba;
sin ella, el estado sería un número sin procedencia.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | SERIAL | Clave primaria. |
| `date` | TIMESTAMP | `DEFAULT NOW()`. |
| `event` | TEXT | Qué movió el saldo. |
| `amount` | FLOAT | Importe del movimiento, con signo. |
| `balance` | FLOAT | Saldo resultante. Redundante a propósito: congela el estado tras el movimiento. |
| `notes` | TEXT | Nota libre. |

**Definida en**

- `src/models/bankroll_manager.py:59` — `CREATE TABLE IF NOT EXISTS
  bankroll_history`.

---

## `match_events`

Eventos por partido descargados de `goalscorers.csv`: goleadores, tarjetas y
córners. Complementa a `matches` con el detalle que los CSV de resultados no
traen, y alimenta los modelos de tarjetas y córners.

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | SERIAL | Clave primaria. |
| `date` | DATE | Fecha del partido. Parte de la clave de unicidad. `NOT NULL`. |
| `home_team` / `away_team` | TEXT | Equipos. Parte de la clave de unicidad. `NOT NULL`. |
| `league` | TEXT | Liga. |
| `home_scorers` / `away_scorers` | TEXT | Goleadores. |
| `home_yellow` / `away_yellow` | INT | Amarillas. `DEFAULT 0`. |
| `home_red` / `away_red` | INT | Rojas. `DEFAULT 0`. |
| `home_corners` / `away_corners` | INT | Córners. Nullable: el CSV no siempre los trae. |
| `penalty_in_match` | BOOLEAN | `DEFAULT FALSE`. |

### Índices y restricciones

- **`UNIQUE(date, home_team, away_team)`** — declarada dentro del `CREATE
  TABLE`. Hace idempotente la recolección: volver a descargar el CSV no
  duplica eventos.

**Definida en**

- `scripts/collect_match_events.py:40` — `CREATE TABLE IF NOT EXISTS
  match_events`, con el `UNIQUE` en su última línea.

---

## `anthropic_usage`

Contabilidad diaria del gasto en la API de Anthropic, **una fila por día**.
Es la tabla que sostiene el guard de presupuesto: antes de cada llamada del
analista se consulta cuánto se lleva gastado hoy y se compara con
`ANTHROPIC_DAILY_BUDGET_USD`.

`day` es la clave primaria, así que el upsert diario es idempotente por
construcción.

| Columna | Tipo | Descripción |
|---|---|---|
| `day` | DATE | Clave primaria. Un día, una fila. |
| `calls` | INT | Llamadas del día. `DEFAULT 0`. |
| `input_tokens` | BIGINT | Tokens de entrada. `DEFAULT 0`. |
| `output_tokens` | BIGINT | Tokens de salida. `DEFAULT 0`. |
| `cache_read_tokens` | BIGINT | Tokens leídos de caché. `DEFAULT 0`. |
| `web_searches` | INT | Búsquedas web (se facturan aparte). `DEFAULT 0`. |
| `cost_usd` | NUMERIC(10,5) | Coste acumulado del día. `DEFAULT 0`. |

### El DDL falla en silencio a propósito

`_ensure_table()` envuelve el `CREATE TABLE` en un `try/except Exception: pass`
con el comentario «no bloqueamos por DDL», y `get_daily_spent_usd()` devuelve
`0.0` ante cualquier excepción. La consecuencia hay que tenerla presente: si
la tabla no se puede crear, **el guard de presupuesto lee 0.0 y deja pasar
todas las llamadas** en vez de frenarlas. Es un fallo abierto, no cerrado.

**Definida en**

- `src/utils/anthropic_budget.py:65` — `CREATE TABLE IF NOT EXISTS
  anthropic_usage`, dentro del `_ensure_table()` perezoso.
