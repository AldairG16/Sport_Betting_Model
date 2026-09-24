# Operación

Todo lo que hace falta saber para trabajar en este repo sin romper producción.
`CLAUDE.md` solo dice de qué va el programa; el detalle está aquí.

---

## 1. Servicios externos

El modelo no funciona solo: depende de seis servicios de terceros. Si uno se cae,
el pipeline **sigue saliendo verde en Actions** y deja de producir en silencio —
ver §2.

| Servicio | Para qué | Credencial | Coste |
|---|---|---|---|
| **The Odds API** | Cuotas (`/odds`) y resultados (`/scores`). Es la fuente crítica: sin ella no hay apuestas ni resolución. | `ODDS_API_KEY` | Plan de pago. 1 crédito por liga y llamada; ~600-700 créditos/día sobre 20K al mes. |
| **PostgreSQL** | Todo el estado: `matches`, `upcoming_matches`, `bets_history`, `shadow_bets`, `bankroll`, `pre_kickoff_analyses`, `analyst_heartbeat`, `anthropic_usage`, y lo aprendido en `model_state` (§7). | `DB_URL` | Alojada fuera del repo. Ver §7. |
| **Anthropic** | Los dos agentes LLM: analista pre-kickoff y resolver de pendientes. | `ANTHROPIC_API_KEY` | ~$0.014-0.020 por llamada. Ver §6. |
| **Telegram** | Único canal de salida: picks, resumen diario, alertas del watchdog. | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; el analista usa `*_PREKICKOFF` y cae al bot principal si no están. | Gratis. |
| **football-data.co.uk** | Respaldo de resultados y **única** fuente de córners y tarjetas. Publica con 24-36 h de retraso. | Ninguna, son CSV públicos. | Gratis. |
| **OpenWeatherMap** | Clima del partido (`src/features/weather_impact.py`). | `WEATHER_API_KEY` | Gratis, 1000 llamadas/día. |

`RAPIDAPI_KEY` solo la usa `test_api.py`, que no forma parte del pipeline.

La lista completa de variables, con sus defaults, está en `.env.example`. En GitHub
son *repository secrets*; en local, un `.env` (que está en `.gitignore`).

> **Regla de oro de las variables numéricas:** un secret sin configurar se expande a
> cadena vacía, y `int("")` revienta. Usa siempre `env_int` / `env_float` / `env_str`
> de `config/settings.py`. Ver §5.

---

## 2. Cómo se dispara realmente

Esto **no** es lo que uno esperaría, y confundirlo cuesta caro:

- **El cron de GitHub casi no se usa.** Solo `watchdog.yml`, `late_results.yml` y
  `closing.yml` (`7,37 * * * *`) llevan `schedule:`. Los demás lo perdieron a
  propósito.
- **GitHub descarta muchos crons programados.** Medido el 20-22 sep: el closing
  "cada hora" corrió 18 veces en 73 h (hueco medio 4.2 h). El closing captura el
  precio de cierre y manda las confirmaciones oficiales, así que **debe
  dispararlo también el dispatcher externo cada 30 min** (`workflow_dispatch` de
  `closing.yml` contra `master`, mismo PAT que los demás). Duplicar corridas no
  daña: no recarga lo descargado hace <20 min, el cierre solo se reemplaza por
  uno más cercano al kickoff y las confirmaciones se marcan como enviadas.
  **Sin ese disparo, el aprendizaje del peso del modelo no recibe datos.** Un cierre
  solo vale si se descargó entre 150 min antes y 2 min después del kickoff. Con
  closings cada ~4 h, casi ningún partido cae en esa ventana: el 23-sep-2026 había
  0 cierres válidos de 33 candidatas shadow, con 6 corridas en 20 h.
- **Producción la dispara un agente externo** vía `workflow_dispatch` contra `master`,
  con un PAT de la cuenta dueña. Lanza `morning`, `closing`, `evening` y `weekly` a
  las 12:00 / 18:00 / 09:00 UTC y los lunes.
- Por eso **quitar los `schedule:` fue deliberado**: `master` es la rama por defecto,
  así que un cron ahí dispararía *además* del agente externo — doble corrida, doble
  gasto de API y dos pasadas escribiendo apuestas. Los crons originales están anotados
  en el commit que los retiró, por si algún día se apaga ese agente.
- `watchdog.yml` y `late_results.yml` son la excepción porque el agente externo **no**
  los dispara: ahí un cron no duplica nada, enciende lo que faltaba.

Para lanzar uno a mano: Actions → el workflow → «Run workflow», o
`scripts/_trigger_workflow.ps1`, que hace el `workflow_dispatch` por API.

> **Cuidado al hacer push a `master`:** `morning.yml`, `evening.yml`,
> `closing.yml`, `weekly.yml` y `late_results.yml` tienen un trigger `push`
> filtrado a su propio archivo. Cambiar uno de esos YAML y hacer push **lanza
> esa corrida** (apuestas, Telegram y créditos incluidos). Si no se quiere,
> el commit lleva `[skip ci]`.

Los scripts de Windows Task Scheduler se retiraron: apuntaban a una ruta muerta y,
arreglados, habrían abierto una segunda vía de ejecución en paralelo a Actions.

### Fallos: rojo en Actions + Telegram, pero un apagón de datos sigue siendo "verde"

`run_step()` captura el error de cada paso y continúa con los demás. Desde el
22-sep-2026, al terminar, si **algún paso falló** el orchestrator sale con
código 1 (run en **rojo**, con `::error::` listando los pasos) y manda a
Telegram qué pasos cayeron — en todos los modos, no solo morning/evening (el
weekly del 21-sep perdió "Load historical data" sin que nadie se enterara).
El caché de cuotas se guarda aunque el run falle (`actions/cache/save` con
`if: always()`).

**Errores tolerados.** Los módulos centrales registran avisos y errores con nivel
(`src/utils/log.py`: `log.warning` / `log.error`, mismo texto en consola). Un error
que el código atrapa y deja pasar ya no se pierde en el log: al final de cada corrida
el orchestrator lo reporta como `::warning::` en Actions y por Telegram
("N errores tolerados"). Así se habría visto el primer día el apagón de 82 días
(`❌ API error 401` en cada liga dentro de un paso "OK"). Un `except` que atrapa un
fallo real debe usar `log.error`, no `print`.

**Los scripts se lanzan como `python scripts/X.py`.** En ese modo la raíz del repo no
está en `sys.path` hasta que el script la agrega: todo `from src...`/`from config...`
va DESPUÉS del `sys.path.append`. pytest agrega la raíz por su cuenta y no lo detecta;
`tests/test_entrypoints.py` lanza cada entrypoint como lo hace Actions.

Dentro de las predicciones, cada partido se evalúa aislado: una fila corrupta
se omite (con traceback en el log y aviso "Predicciones parciales") y el resto
del slate sigue. Si fallan **todos**, el paso falla.

Lo que el código de salida **no** detecta es un apagón de datos: la key de The
Odds API se desactivó el 17-jun-2026 (`401 DEACTIVATED_KEY`, pago fallido) y
las 15 ligas devolvieron «0 partidos procesados» durante 82 días — cada paso
"funcionó". De ahí los CHECK 5 y 6 del watchdog (§4): miden **producto**, no
actividad.

---

## 3. Los tres loops

Cooperan a través de PostgreSQL; entre corridas no sobrevive estado en memoria.
Cada uno es su propio workflow.

**1 · Pipeline diario** — `orchestrator.py`, modos `morning` / `closing` / `evening` / `weekly`.
`run_prediction_pipeline()` evalúa cada partido en 11 etapas con entradas y salidas
explícitas (`_stage_*` en `src/pipeline/prediction_pipeline.py`): fuerzas → λ →
probabilidades del modelo → mercado sin margen → combinación → cuotas → probabilidad
final (ancla, calibración, sesgos) → shadow → contexto de mercado → candidatas →
selección y stake. Después, `_apply_portfolio_limits` (correlación, sospechosas,
exposición y tope por slate). Cada etapa es la misma lógica de antes del 22-sep-2026
(verificado con dos goldens); cambiarla exige regenerar el golden con el diff a la vista.
Trae cuotas de The Odds API, `src/pipeline/prediction_pipeline.py` puntúa cada par
(partido, mercado), aplica calibración, filtros de edge y Kelly, e inserta en
`bets_history` con `result='pending'`. El modo `evening` vuelve a pedir resultados,
llama a `update_bet_results()` en `src/models/save_bets.py` para pasar pending →
win/loss, liquida también las candidatas shadow (`resolve_shadow_outcomes()`, misma
función de liquidación con stake 1 y sin bankroll), calcula CLV y manda el resumen a
Telegram. `--mode results` (`late_results.yml`) hace lo mismo sin el resumen.

**2 · Analista pre-kickoff** — `scripts/pre_kickoff_analyst.py`, `pre_kickoff.yml`.
Cada 15 min busca bets con kickoff dentro de `[PRE_KICKOFF_WINDOW_MIN,
PRE_KICKOFF_WINDOW_MAX]` (30-60 min). Las agrupa por partido y llama a Anthropic
**una vez por partido**, no por bet, con el dossier cuantitativo de la DB más
`data/analyst_lessons.md` y un memo de aprendizaje de los últimos 30 días. Devuelve
un veredicto por mercado y un `best_pick`. Escribe en `pre_kickoff_analyses` y avisa
por su canal de Telegram. **Nunca toca `bets_history`.**

> Hoy está **apagado a propósito** para no gastar tokens: el workflow está
> `disabled_manually` y la variable `PRE_KICKOFF_ANALYST_ENABLED` está en `false`,
> lo que además silencia la alerta de heartbeat del watchdog. Para reactivarlo:
> habilitar el workflow y poner la variable en `true`.

**3 · Resolver de pendientes** — `scripts/resolve_pending_bets.py`, `resolve_pending.yml`.
Dos veces al día toma bets atascadas en `pending` >6 h tras el kickoff (típicamente
córners y tarjetas esperando el CSV de football-data, o ligas que The Odds API no
puntúa). Pide a Claude el resultado FT vía `web_search`, completa `matches` y llama a
`update_bet_results()`. **Gasta tokens.** `late_results.yml` cubre el caso barato:
re-corre `--mode results` a las 00:00 y 06:30 MX para partidos que acaban después
del evening, sin llamar a la LLM.

**4 · Aprendizaje semanal** — el modo `weekly` recalcula todo lo que el sistema
aprende de los datos que recolecta solo, y lo guarda en `model_state` (§7):
calibración, CLV gate, caché de CLV para Kelly, **peso del modelo frente al
mercado**, **reactivaciones por shadow**, **evidencia de las reglas contra resultados
reales** y **escala de los ajustes manuales**. Las corridas diarias lo leen de la DB
al arrancar (`load_learned_state()` en el pipeline). Solo aprende de datos de la
cohorte actual (`LEARNING_SINCE`, §7). Estos pasos corren **al inicio** del weekly,
antes de las cargas de datos: las cargas tardan ~62 min (histórico 11', eventos 21',
ligas extra 25') y el 22-sep-2026 el job se cortó en el límite de 60 min antes de
llegar a aprender. Límite actual del job: 120 min.

### Cómo auditar

El pipeline **nunca borra** de `bets_history`. Para «¿por qué no apostó esto?»: si no
existe la fila, el modelo no lo puntuó; si existe en `pending`, apostó y espera
resolución; si está resuelta, ya está. Para «¿por qué no habló el analista?»: mira
`analyst_heartbeat` (que el cron disparó) y su `error_msg` (fallos por bet).

---

## 4. Watchdog

`scripts/watchdog.py`, cron `17 0,6,12,18 * * *`. Solo lee (y crea `pipeline_runs` si
falta), no gasta créditos ni tokens. **Silencio = salud**: solo manda Telegram si algo
va mal.

| Check | Qué mira |
|---|---|
| 1 | La DB responde. |
| 2 | `MAX(updated_at)` de `upcoming_matches` es reciente. |
| 3 | Bets en `pending` de más de 5 días. |
| 4 | Gap del `analyst_heartbeat` — **se omite** si `PRE_KICKOFF_ANALYST_ENABLED=false`. |
| 5 | Hay partidos futuros cargados. |
| 6 | Antigüedad de la apuesta más reciente (umbral `DIAS_SIN_BETS_ALERTA`, 7 días). |
| 7 | El **closing** corre: alerta si la última corrida tiene más de 2 h, o si hubo menos de 6 en las últimas 6 h (se esperan ~12). |
| 8 | El **weekly** corre: alerta si la última corrida tiene más de 8 días. |

El 2 mide **actividad** y se deja engañar: el cleanup de `update_all()` toca la tabla
al borrar filas viejas aunque no entre ninguna nueva. El 5 y el 6 miden **producto**,
y son los que detectan un apagón de datos.

El 7 y el 8 existen desde el 24-sep-2026 porque closing y weekly los dispara
cron-job.org: si ese servicio deja de disparar, nada falla, simplemente no corren. Sin
closing no llegan las confirmaciones antes de cada partido y el aprendizaje no recibe
cierres; sin weekly el sistema no aprende. Cada corrida del orquestador deja una fila en
`pipeline_runs` (modo, hora, pasos fallidos, duración; se conservan 60 días), escrita por
`src/utils/pipeline_runs.py`. El conteo de 6 h importa porque el cron propio de GitHub
sigue disparando el closing a ratos: una corrida reciente no prueba que el disparo cada
30 min siga vivo. Con menos de 6 h de historial (recién instalado) el conteo no opina.
Mientras el weekly no tenga su primera fila, sirve de respaldo la hora de
`model_state.anchor_weights`.

---

## 5. Gotchas que ya rompieron producción

**Secrets vacíos en GH Actions.** `int(os.environ.get("X", "default"))` devuelve
`int("")` y lanza `ValueError`, matando el pipeline antes de escribir nada. Usa
`env_int` / `env_float` / `env_str` de `config/settings.py`. El último módulo que lo
violó fue `src/utils/anthropic_budget.py` y tiró todas las corridas del analista un día.

**`bet["edge"]` no es el edge.** Es `edge_ev = prob*odds − 1`. El real es
`bet["edge_market"] = prob − 1/odds`. Filtrar por `edge` deja pasar longshots y costó
una semana a −23 % de ROI. Filtra siempre por `edge_market`.

**Las claves de handicap asiático están parametrizadas.** El mercado de una bet es
`ah_home_-0.5`, pero `MIN_EDGE_BY_MARKET` y `calibration_factors.json` se indexan por
grupo: `ah_home_fav` / `ah_home_pk` / `ah_home_dog`. Usa `_ah_group()` de
`src/models/calibration_monitor.py`. Hay precedente de un `dict.get(mkt, default)`
silencioso cayendo al default en todas las bets AH.

**La línea embebida en la clave AH es SIEMPRE la del local**, también en el lado
visitante: `ah_away_-0.50` es el visitante *recibiendo* +0.5. `_ah_group()` la
invierte para el visitante (`ah_away_-0.50` → `ah_away_dog`); hasta el 22-sep-2026
no lo hacía y los grupos del visitante estaban cruzados. El bloqueo de "AH con el
local favorito" es `("ah_home_fav", "ah_away_dog")`: las dos patas de la misma línea.

**AH, DNB y 1X2 apuestan al mismo resultado.** `ah_home_-0.50` es literalmente
`home_win`. Comparten grupo de exclusión (`_exclusive_group()`): máximo una bet por
partido entre ellos.

**Nada que escriba un runner sobrevive a su runner.** Cada corrida de Actions arranca
limpia: un archivo que genera el weekly no existe para el morning. Todo estado que
otra corrida deba leer va a `model_state` con `src/utils/model_state.py`
(`save_state` / `load_state`). Hasta el 22-sep-2026 la calibración y el caché de CLV
de producción eran la copia commiteada del 7-may, y el CLV gate nunca aplicó.

**Un cierre solo vale si se DESCARGÓ cerca del kickoff.** La cuota de una fila de
`upcoming_matches` puede tener horas: sin una recarga cerca del partido, el "cierre"
de una bet o candidata shadow era la misma cuota de apertura (CLV 0 exacto — se veía
en tarjetas, DNB, 1T y AH). Desde el 22-sep-2026 cada fila guarda
`odds_fetched_at` (fetch de la liga: h2h/totals/spreads) y `specialty_fetched_at`
(fetch por evento: btts, DNB, DC, 1T/2T, córners, tarjetas); cada cierre guarda
`closing_fetched_at`. El CLV gate, el caché de Kelly, la reactivación y el peso
del modelo usan **solo** cierres descargados entre 150 min antes y 2 min después
del kickoff (y después de la apertura): `src/utils/closing_quality.py`. El closing
recarga de forma dirigida las ligas con kickoff en 10-80 min que tengan bets o
shadow (`refresh_for_closing`), con topes por corrida: `CLOSING_MAX_LEAGUES_PER_RUN`
(6 × 3 créditos), `CLOSING_MAX_EVENTS_PER_RUN` (5 × ~7) y `CLOSING_MIN_CREDITS`
(1500). Los mercados por evento (btts, DNB, DC, 1T/2T, córners, tarjetas) se recargan
en **cada** corrida para los eventos con **bets** en ellos. Para los que solo tienen
candidatas **shadow** en esos mercados, desde el 24-sep-2026 hay **una** recarga por
evento: si su último enrichment ya cae en la ventana de cierre no paga otra vez. Tiene
tope propio (`CLOSING_MAX_SHADOW_EVENTS_PER_RUN`, 6) y solo corre con más de
`CLOSING_SHADOW_MIN_CREDITS` (3000) créditos. Antes quedaban sin cierre válido: el
24-sep, ambos anotan y empate anulado de Seattle–Salt Lake tenían "cierre" de 12 h
antes, y así el aprendizaje de esas familias nunca recibía datos. Costo medido: 8-12
eventos por día de fin de semana × ~5-7 créditos.

**Cuotas de cierre: mismo mercado, misma línea.** Hay UN mapeo mercado → cierre
(`_closing_odds_for` en `src/models/save_bets.py`) para apuestas y shadow. Si la línea
se movió (AH −0.5 → −0.75, córners 9.5 → 10.5) no hay cierre comparable y devuelve
`None`; nunca compares contra otra línea. El closing de producción tenía su propia
copia que buscaba `"btts_yes"` cuando la bet se llama `"btts"`: ninguna bet BTTS tuvo
CLV hasta el 22-sep-2026.

**`upcoming_matches` acumula duplicados stale.** Su clave de upsert incluye
`match_day`, derivado del kickoff, así que cuando la API corrige un horario se inserta
fila nueva en vez de actualizar. Todo consumidor debe leer con
`DISTINCT ON (home_team_norm, away_team_norm, sport_key) … ORDER BY updated_at DESC`.

**El cron de GH descarta `*/N` en los minutos punta** (`:00/:15/:30/:45`). Para algo
más frecuente que cada hora usa minutos off-peak — `pre_kickoff.yml` usa
`7,22,37,52 * * * *`. El síntoma es heartbeats con `bets_found > 0, bets_analyzed = 0`
durante horas.

**pandas convierte los NULL de columnas enteras en `NaN` (float).** Si ese valor vuelve
a la DB en un `UPDATE`/`INSERT`, Postgres lo trata como `double` y al guardarlo en una
columna `INTEGER` revienta con *integer out of range* — y la transacción entera se
revierte. Convierte a `None` antes de escribir (`_sql_value` en
`scripts/weekly_sanity_audit.py`). Así estuvo roto el auto-fusionador de partidos
duplicados al menos desde el 21-sep-2026: detectaba, fallaba y no fusionaba nada.

**Un NULL leído con pandas puede ser `NaN`, no `None`.** Si otra fila de la misma
columna tiene valor, la columna es `float64` y el NULL llega como `NaN`: `x is not None`
es `True` y toda comparación con `NaN` da `False`. Así `resolve_market` liquidaba como
**perdidas** (over y under a la vez) las apuestas de córners, tarjetas y tiros cuyo
partido aún no tenía esos datos, hasta el 22-sep-2026. Para "¿hay dato?" usa `_has()` de
`src/models/save_bets.py` (o `pd.isna`). `audit_stat_settlements()` (en el smoke test)
cuenta, en solo lectura, cuántas se liquidaron así. El 23-sep-2026 se corrigieron las 4 que
encontró. Eran tarjetas del 12-sep, verificadas contra el CSV de football-data (+0.57u).
Se usó `scripts/fix_stat_settlements.py`: en seco por defecto, con `--apply` escribe. Ajusta
el bankroll por la diferencia y deja nota en `bankroll_history`.

**Un `try/except` dentro de una transacción no aísla nada.** En Postgres el primer
`execute` fallido aborta la transacción: los siguientes fallan también y el COMMIT final
se convierte en ROLLBACK de todo el lote, sin excepción visible. Aislar una fila exige
un savepoint: `with conn.begin_nested():` alrededor del `execute`. Así estaban
`fetch_results`, el respaldo de fbdata, las cargas del weekly y la creación de índices.

**`conn.execute(text(...), lista_de_dicts)` NO agrupa.** Con SQL textual, psycopg2 hace
un viaje a Neon por fila (~80 ms desde Actions): el weekly tardaba 25 min en 18k filas
de ligas extra y 21 min en 15k eventos. Para cargas masivas usa
`src/utils/db_batch.insert_ignore_conflicts`: una sentencia multi-VALUES por lote,
cuenta exacta de filas nuevas (RETURNING) y aislamiento de filas malas.

**Las `unresolved` se liquidan en cuanto llegan sus datos.** `update_bet_results`
toma `pending` (kickoff hace >3 h) **y** `unresolved`, y las liquida en la misma
pasada (`plan_bet_settlements`). Hasta el 22-sep-2026 las `unresolved` se re-marcaban
`pending` y el timeout de 3 días las devolvía a `unresolved` en la misma corrida: nunca
se liquidaban y terminaban `stale`, fuera del bankroll.

**Los nombres de equipo pasan por `normalize_team()` antes de consultar.** `matches` y
`upcoming_matches` guardan minúsculas normalizadas. Un desajuste deja bets en `pending`
para siempre aunque `fetch_results` haya corrido.

**`fetch_results.py` no tiene guarda de créditos.** Los cuenta pero no se detiene por
umbral, a diferencia de `update_upcoming_matches`. Con 15 ligas son hasta 15 créditos
por corrida.

---

## 6. Presupuesto de Anthropic

Dos helpers en `src/utils/anthropic_budget.py` controlan cada llamada:

- `can_call(engine, estimated_cost_usd)` la rechaza si el gasto de hoy más la
  estimación supera `ANTHROPIC_DAILY_BUDGET_USD`.
- `record_call(engine, …)` se llama después con el `usage` real y escribe en
  `anthropic_usage`.

Ambos agentes usan `claude-haiku-4-5` con `max_uses=1-2` en `web_search_20250305`.
La búsqueda web cuesta $0.01 y domina el coste. No añadas llamadas a `web_search` a
la ligera.

> Falla **cerrado** (desde el 22-sep-2026): si `anthropic_usage` no se puede leer,
> `get_daily_spent_usd()` devuelve `None` y `can_call()` rechaza la llamada. Antes
> devolvía `0.0` ante cualquier error y dejaba pasar todo.

Cuando el analista revienta en una bet, el traceback queda en
`analyst_heartbeat.error_msg` y salta alerta si fallan ≥3 bets en una corrida. Nunca
vuelvas a poner un `except Exception: pass` alrededor de la llamada a la LLM — así se
perdió un día entero en silencio.

---

## 7. Aprendizaje y calibración

### Dónde vive lo aprendido

Tabla `model_state` (clave → JSON), historizada en `model_state_history`. La DB es la
fuente de verdad; los archivos de `config/` y `data/` son espejo local.

| Clave | Lo escribe (weekly) | Lo usa |
|---|---|---|
| `dc_params` | `dc_mle_fitter` | lambdas DC-MLE |
| `calibration_factors` | `calibration_monitor.compute_calibration` | mercados sin ancla (hoy: doble oportunidad) |
| `clv_gate_markets` / `clv_gate_leagues` | `clv_gate.run_clv_gate` | kill-switch por mercado / liga |
| `clv_cache` | `betting_engine.refresh_clv_cache` | fracción de Kelly por CLV |
| `anchor_weights` | `anchor_learner.run_anchor_learning` | **peso del modelo** frente al mercado |
| `shadow_reactivation` | `clv_gate.run_shadow_reactivation` | `away_win` y AH con local favorito |
| `shade_scales` | `shade_learner.run_shade_learning` | **escala de los ajustes manuales** (FLB, tabla, empates) |
| `rule_evidence` | `rule_evidence.run_rule_evidence` | solo reporte: veredicto de cada regla contra resultados |
| `sharp_reference` | `sharp_reference.run_sharp_reference` | solo reporte: apuestas y modelo contra Pinnacle |

**Cierres válidos:** todo lo que aprende del CLV (gate, caché de Kelly, reactivación,
peso del modelo) usa solo cierres descargados cerca del kickoff (§5). Los cierres
anteriores al 22-sep-2026 no tienen hora de descarga y no cuentan: los learners
arrancan con esa muestra en cero y se llenan con cada closing.

**Cohorte:** todo aprende solo de datos desde `LEARNING_SINCE` (`config/settings.py`,
default `2026-09-14`, inicio de la arquitectura anclada). Si la arquitectura vuelve a
cambiar, se mueve esa fecha (variable de GitHub, sin tocar código). Con cohorte nueva
los learners arrancan en neutro y se van llenando: es a propósito.

### Peso del modelo (lo que más importa)

Un mercado anclado vale `p = p_mercado + w·(p_modelo − p_mercado)`. `w` era 0.35 fijo;
ahora `anchor_learner` lo aprende por familia (1x2, totales, BTTS, AH/DNB, medio tiempo,
córners/tarjetas): regresa el movimiento del mercado hasta el cierre (Δ = 1/cierre −
1/apertura) sobre el desvío crudo del modelo en las candidatas shadow. La pendiente es la
fracción de la opinión del modelo que el mercado termina confirmando. Límites:
encogimiento bayesiano hacia 0.35, rango [0, 0.50], ±0.10 por semana, mínimo 100
candidatas con cierre por familia (si no, estimado agregado; si no, 0.35). Si los datos
dicen que el modelo no anticipa al mercado, `w` baja y el sistema **apuesta menos**:
es el comportamiento correcto, y el shadow sigue midiendo.

### Reactivación por shadow

`away_win` y los AH con el local favorito están bloqueados de forma fija. Vuelven solos
si sus candidatas shadow que se habrían apostado (desvío > 0, edge ≥ 5 pt) muestran
CLV ≥ 0 con n ≥ 30; se re-bloquean con CLV significativamente negativo.

### Reglas manuales contra resultados reales

El CLV juzga bien el peso del modelo, pero **no** los ajustes escritos a mano (sesgo
favorito-longshot, "la tabla miente", empates contextuales): esas reglas afirman que el
precio está sesgado incluso al cierre, así que solo el resultado real puede
confirmarlas. Por eso cada candidata shadow guarda la probabilidad anclada antes de los
ajustes (`p_pre_shade`) y cuánto la movieron (`shade_delta`, ya acotado por el tope D12
y **sin escalar**), y el evening la liquida (`result`, `profit` por unidad).

- **Escala de los ajustes** (`shade_learner`): la probabilidad final de un mercado
  anclado es `p = p_ancla + s·δ`. El weekly estima `s` por familia con la regresión sin
  intercepto `y − p_ancla = s·δ` (la escala que minimiza el Brier), con la misma política
  que el peso del modelo: prior `s = 1` (las reglas tal como se diseñaron, sd 0.5),
  rango [0, 1] (puede apagarlas, no amplificarlas), ±0.25 por semana, mínimo 300
  candidatas win/loss con δ ≠ 0 y 30 partidos por familia (si no, agregado; si no, 1).
  Un win/loss es mucho más ruidoso que el CLV: con δ típico de 2-5 pt hacen falta
  ~1,000 candidatas para que los datos pesen lo mismo que el prior. Se mueve despacio a
  propósito.
- **Evidencia por regla** (`rule_evidence`, solo reporte): modelo vs mercado (Brier),
  ajustes con vs sin, sesgo favorito-longshot del propio mercado por lado y banda de
  cuota, si el edge de las apostables es real (brecha prob. − acierto y ROI), y los
  filtros de selección (entre semana, sweet spots, ligas duras) comparando la brecha de
  su grupo penalizado contra el resto. IC 95% robusto por partido; sin veredicto con
  n < 100 o < 30 partidos. Los filtros **no** se ajustan solos: son decisiones discretas
  y quedan a criterio del dueño con esta evidencia a la vista.

Llega a Telegram en el weekly solo si algún veredicto ya tiene datos suficientes o si
la escala se movió. Push y medias (AH de cuarto, DNB con empate) cuentan en el ROI pero
no en Brier ni calibración. Las candidatas sin datos del partido a los 10 días quedan
`result = 'stale'`.

### Referencia Pinnacle (¿hay ventaja real?)

Desde el 24-sep-2026 el sistema se mide también contra **Pinnacle**, la casa de
referencia de los profesionales. Su precio sin margen es el mejor estimador público de
la probabilidad real, y su cierre es el patrón estándar para saber si alguien tiene
ventaja. Viene en la misma descarga (región `eu`), sin créditos extra. **Solo mide:**
no entra en las probabilidades, los filtros ni los stakes.

- **Precios** (`src/features/pinnacle.py`): cada fetch guarda en `upcoming_matches`
  las cuotas de Pinnacle de 1X2 y más/menos 2.5 (`pin_*`). No usa COALESCE: un fetch
  con hora escribe el precio tal cual (NULL si ya no cotiza), para que un precio viejo
  nunca quede con hora nueva. De ahí se derivan exactamente empate anulado
  (P(local | no empate), la misma definición del modelo) y hándicap ±0.5. Esos
  mercados son más del 80% de las candidatas; el resto queda sin referencia.
- **Captura**: cada candidata shadow guarda `pin_prob` al registrarse, y cada apuesta
  lo guarda en `decision_log.market_ctx`. El closing guarda `pin_close_prob` y
  `pin_close_at` en ambas tablas (`save_bets.closing_updates`), con **su propia**
  frescura: el cierre del mercado puede venir de otro fetch (btts, DNB de la API).
- **Reporte** (`src/models/sharp_reference.py`, weekly, clave `sharp_reference`):
  valor esperado por unidad contra el cierre sin margen de Pinnacle (apuestas y
  candidatas; no espera el resultado), cuánto se mueve Pinnacle hacia el modelo entre
  apertura y cierre, y modelo vs cierre de Pinnacle en Brier. Usa solo cierres válidos
  (§5), IC 95% robusto por partido y nada con n < 100 o < 30 partidos. Llega a Telegram
  cada semana en cuanto hay cierres válidos. Las apuestas se miden a la mejor cuota
  europea; en PlayDoit suele ser menor.

### Calibración por mercado

`apply_calibration(prob, market, league)` de `src/models/calibration_monitor.py` es el
**único** punto de entrada: corrección isotónica o escalar con suavizado bayesiano
(prior=30) y clamp adaptativo (0.75-1.30 con n≥30, 0.85-1.20 con n<30). El sub-dict
`by_league` se rellena para ligas con ≥25 bets en la ventana; se mira primero el nivel de
liga y se cae al global, con la cadena de fallback de grupos AH encima. **Alcance real:**
los mercados anclados no pasan por aquí (heredan la calibración del precio sin margen);
hoy solo la usan los mercados sin ancla. Con el holdout de 45 días y la cohorte del
14-sep, la ventana de ajuste está vacía hasta finales de octubre: se guardan factores
neutros.

Cuando un mercado cruza los umbrales de alerta (factor <0.82 o >1.20 con n≥10), el
cron semanal manda `check_calibration_alert()` a Telegram. Trata esas alertas como
señal para **subir `MIN_EDGE_BY_MARKET`** en ese mercado hasta que el sesgo se
estabilice, no necesariamente para reentrenar. Precedente: `ah_home_fav` y
`ah_away_fav` saltaron con factores ≈0.78 en mayo; se arregló subiendo MIN_EDGE y
corrigiendo un bug de lookup, sin reentrenar.

Otros ficheros de parámetros calibrados que el código lee en runtime:
`config/ensemble_weights.json` (`src/models/ensemble_model.py`, que cae a pesos por
defecto **en silencio** si falta) y `config/thresholds.json`
(`src/models/threshold_optimizer.py`).

---

## 8. Comandos

```bash
# Pipeline diario (los mismos modos que cablean los workflows)
python scripts/orchestrator.py --mode morning   # cuotas + predicciones + Telegram
python scripts/orchestrator.py --mode closing   # odds de cierre para CLV
python scripts/orchestrator.py --mode evening   # resultados + CLV + preview
python scripts/orchestrator.py --mode results   # solo re-fetch de resultados y resolver
python scripts/orchestrator.py --mode weekly    # recarga histórica + calibración + walk-forward

# Los dos agentes LLM (gastan tokens)
python scripts/pre_kickoff_analyst.py
python scripts/pre_kickoff_analyst.py --debug   # 1 sola bet, verifica la integración
python scripts/resolve_pending_bets.py --hours-lag 6 --limit 15

# Salud y auditoría (sin gasto)
python scripts/watchdog.py
python scripts/audit_analyst_calibration.py --days 60
python scripts/db_smoke_test.py                  # 31 checks contra la base real, solo lectura
python scripts/fix_stat_settlements.py           # en seco; --apply corrige liquidaciones

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_bet_filters.py -v
python -m pytest tests/test_kelly.py::test_kelly_caps -v
```

`pre_kickoff.yml` tiene una casilla `debug_mode` que corre el analista contra 1 bet
futura, para verificar la integración sin gastar un ciclo de cron completo.

### Dashboard local

`python scripts/run_dashboard.py` → http://127.0.0.1:5050. Solo lectura. Es
`dashboard/app.py` (Flask: página + API JSON) con `dashboard/ui_es5.js` y
`dashboard/chart2.min.js`, servidos localmente. La ganancia sale de la columna
`profit` de `bets_history` (la de la liquidación, la misma del bankroll). El `.exe`
lo compila `release.yml`:

- con un tag `v*` compila, prueba y publica el Release;
- disparado a mano compila y prueba **sin publicar**.

El `.exe` lleva dentro el JS, Chart.js y `VERSION`. Una copia de `ui_es5.js` junto al
`.exe` tiene prioridad, para arreglos en vivo sin recompilar.

**PlayDoit (la casa del dueño) no está en ninguna API de cuotas** (ni The Odds API, ni
Odds-API.io, ni OddsPapi; revisado el 24-sep-2026). La cuota que guarda el sistema es la
mejor entre ~20 casas europeas. Por eso cada pick, cada confirmación pre-kickoff y el
avance de mañana dicen "PlayDoit: apuesta si paga -138 o mejor (-125 ✅ · -150 ❌)":
**formato americano**, como lo muestra PlayDoit, porque el dueño no lee cuotas
decimales (`src/utils/min_odds.py`: edge ≥ 2%, el mismo umbral de la revalidación,
redondeado del lado seguro). En americano un número más alto siempre paga más. El
dashboard muestra lo mismo (columnas **Mejor cuota** y **PlayDoit**) y no pide nada a
mano: el registro de la cuota tomada (v1.4.0) se quitó en v1.5.0 por pedido del dueño.
La columna `bets_history.odds_placed` sigue en la base, sin uso.

Hasta el 22-sep-2026 el `.exe` en uso no se podía reconstruir desde el repo: `app.py`
era una reescritura a medias, sin rutas.

---

## 9. Mundial 2026

`_check_world_cup_activation()` añade `soccer_fifa_world_cup` a `SPORT_KEYS` **solo
entre `WORLD_CUP_START` y `WORLD_CUP_END`** (11-jun a 19-jul-2026). El torneo terminó,
así que hoy la función sale antes y la key no se añade.

La versión original solo tenía fecha de inicio: seguía metiendo la liga en `SPORT_KEYS`
indefinidamente, y `update_upcoming_matches` pide cuotas por liga. Si vuelve a haber un
torneo, replica el patrón con las dos fechas.

`WORLD_CUP_BETTING_ENABLED` sigue en `false` por defecto: los picks de torneo van a
`data/paper_trades.jsonl` con etiqueta `[PAPER]` y **no** entran a `bets_history`.

---

## 10. Convenciones

El código está comentado en español (histórico del proyecto), con nombres de fichero y
función en inglés. Sigue ese estilo: comentarios en español, mensajes de commit en
inglés.

`archive/` documenta la regla de **archivar, nunca borrar** para módulos que dejan
de ser alcanzables. Cada módulo retirado lleva su motivo, qué lo sustituye y si es
seguro re-ejecutarlo (`archive/README.md`).

### Tests

`tests/pipeline_harness.py` corre `run_prediction_pipeline()` de punta a punta sin DB
ni APIs (dependencias sustituidas por datos fijos). Los cambios de lógica se prueban
**ejecutando** el pipeline, no buscando texto en el código:
`tests/test_pipeline_end_to_end.py` congela la salida completa en
`tests/golden/pipeline_baseline.json`; si un cambio intencional la altera, se regenera
con `UPDATE_GOLDEN=1 python -m pytest tests/test_pipeline_end_to_end.py` y el diff va
en el commit.

Ningún test busca texto en el código fuente (el 23-sep-2026 se convirtieron los que
quedaban). Uno de ellos exigía que `setdefault` y `player_club_goals` aparecieran en
`load_scorer_rates`, y pasaba mientras los goleadores de clubes se descartaban siempre.
Para el código que escribe en la base, `tests/fake_db.py` es un motor falso que
registra cada sentencia; las consultas se verifican sobre el SQL que de verdad se envía.

Dos redes de seguridad corren con la suite:

- `tests/test_static_analysis.py` exige cero hallazgos de pyflakes en `src/`,
  `scripts/`, `config/`, `dashboard/` y `tests/`: nombres sin definir, imports o
  variables sin uso.
- `tests/test_entrypoints.py` resuelve cada `from src|scripts|config|dashboard import x`
  del proyecto, también los que están dentro de funciones. Esos imports solo fallan
  cuando se ejecuta su rama, a veces una vez por semana y en producción.

### Ramas

- **`master`** — producción y rama por defecto. Es la que dispara el agente externo.
- **`main`** — rama de un build de blueprint: trae herramienta de auditoría estática
  (`tools/audit`), 288 tests y documentación de esquema, pero le faltan 8 de los 9
  workflows. **No es desplegable.**
