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

### Sin DDL en este árbol

No hay `CREATE TABLE` ni `ALTER TABLE` para `bets_history` en el código
presente: igual que `upcoming_matches`, es una tabla de creación implícita por
`to_sql`, y su escritura vive en el módulo de guardado de apuestas, fuera de
este árbol. Aquí solo se observa su lectura.

**Definida en**

- `scripts/watchdog.py:120` — `SELECT COUNT(*) FROM bets_history WHERE
  result = 'pending'`; el chequeo de salud que detecta bets bloqueadas.
- Creación implícita: **no hay sitio DDL** en este árbol.

---

## `analyst_heartbeat`

Latido del pre-kickoff analyst. Cada corrida del cron escribe una fila; el
watchdog la usa para distinguir «no había nada que analizar» de «el cron dejó
de dispararse». Es la tabla que hace observable un fallo silencioso.

| Columna | Tipo | Descripción |
|---|---|---|
| `ran_at` | TIMESTAMP | Momento de la corrida. El watchdog alerta si el `MAX(ran_at)` tiene más de 3 h **y** estamos en horario de partidos. |

### Sin DDL en este árbol

El analyst y su escritura del heartbeat viven fuera del código presente; aquí
solo se observa la lectura del watchdog.

**Definida en**

- `scripts/watchdog.py:145` — `SELECT MAX(ran_at) AS last_ran FROM
  analyst_heartbeat`; la lectura que gobierna la alerta de gap.
- Creación implícita: **no hay sitio DDL** en este árbol.
