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
| **PostgreSQL** | Todo el estado: `matches`, `upcoming_matches`, `bets_history`, `bankroll`, `pre_kickoff_analyses`, `analyst_heartbeat`, `anthropic_usage`. | `DB_URL` | Alojada fuera del repo. Ver §7. |
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

- **El cron de GitHub casi no se usa.** Solo `watchdog.yml` y `late_results.yml`
  llevan `schedule:`. Los demás lo perdieron a propósito.
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

Los scripts de Windows Task Scheduler se retiraron: apuntaban a una ruta muerta y,
arreglados, habrían abierto una segunda vía de ejecución en paralelo a Actions.

### Un fallo verde se ve igual que un día normal

`run_step()` captura el error de cada paso y continúa. Un apagón total de datos
**no** pone el run en rojo. Ocurrió: la key de The Odds API se desactivó el
17-jun-2026 (`401 DEACTIVATED_KEY`, pago fallido) y las 15 ligas devolvieron
«0 partidos procesados» durante 82 días sin que nada avisara.

De ahí los CHECK 5 y 6 del watchdog (§4): miden **producto**, no actividad.

---

## 3. Los tres loops

Cooperan a través de PostgreSQL; entre corridas no sobrevive estado en memoria.
Cada uno es su propio workflow.

**1 · Pipeline diario** — `orchestrator.py`, modos `morning` / `closing` / `evening` / `weekly`.
Trae cuotas de The Odds API, `src/pipeline/prediction_pipeline.py` puntúa cada par
(partido, mercado), aplica calibración, filtros de edge y Kelly, e inserta en
`bets_history` con `result='pending'`. El modo `evening` vuelve a pedir resultados,
llama a `update_bet_results()` en `src/models/save_bets.py` para pasar pending →
win/loss, calcula CLV y manda el resumen a Telegram.

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

### Cómo auditar

El pipeline **nunca borra** de `bets_history`. Para «¿por qué no apostó esto?»: si no
existe la fila, el modelo no lo puntuó; si existe en `pending`, apostó y espera
resolución; si está resuelta, ya está. Para «¿por qué no habló el analista?»: mira
`analyst_heartbeat` (que el cron disparó) y su `error_msg` (fallos por bet).

---

## 4. Watchdog

`scripts/watchdog.py`, cron `17 0,6,12,18 * * *`. Solo lee, no escribe, no gasta
créditos ni tokens. **Silencio = salud**: solo manda Telegram si algo va mal.

| Check | Qué mira |
|---|---|
| 1 | La DB responde. |
| 2 | `MAX(updated_at)` de `upcoming_matches` es reciente. |
| 3 | Bets en `pending` de más de 5 días. |
| 4 | Gap del `analyst_heartbeat` — **se omite** si `PRE_KICKOFF_ANALYST_ENABLED=false`. |
| 5 | Hay partidos futuros cargados. |
| 6 | Antigüedad de la apuesta más reciente (umbral `DIAS_SIN_BETS_ALERTA`, 7 días). |

El 2 mide **actividad** y se deja engañar: el cleanup de `update_all()` toca la tabla
al borrar filas viejas aunque no entre ninguna nueva. El 5 y el 6 miden **producto**,
y son los que detectan un apagón de datos.

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

**`upcoming_matches` acumula duplicados stale.** Su clave de upsert incluye
`match_day`, derivado del kickoff, así que cuando la API corrige un horario se inserta
fila nueva en vez de actualizar. Todo consumidor debe leer con
`DISTINCT ON (home_team_norm, away_team_norm, sport_key) … ORDER BY updated_at DESC`.

**El cron de GH descarta `*/N` en los minutos punta** (`:00/:15/:30/:45`). Para algo
más frecuente que cada hora usa minutos off-peak — `pre_kickoff.yml` usa
`7,22,37,52 * * * *`. El síntoma es heartbeats con `bets_found > 0, bets_analyzed = 0`
durante horas.

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

> `_ensure_table()` envuelve su DDL en `try/except: pass` y `get_daily_spent_usd()`
> devuelve `0.0` ante cualquier excepción. Si la tabla no se puede crear, el guard
> **lee 0.0 y deja pasar todas las llamadas**. Falla abierto, no cerrado.

Cuando el analista revienta en una bet, el traceback queda en
`analyst_heartbeat.error_msg` y salta alerta si fallan ≥3 bets en una corrida. Nunca
vuelvas a poner un `except Exception: pass` alrededor de la llamada a la LLM — así se
perdió un día entero en silencio.

---

## 7. Calibración

`config/calibration_factors.json` se regenera cada lunes en `step_calibration`.
`apply_calibration(prob, market, league)` de `src/models/calibration_monitor.py` es el
**único** punto de entrada: corrección isotónica o escalar con suavizado bayesiano
(prior=30) y clamp adaptativo (0.75-1.30 con n≥30, 0.85-1.20 con n<30). El sub-dict
`by_league` se rellena para ligas con ≥25 bets en 90 días; se mira primero el nivel de
liga y se cae al global, con la cadena de fallback de grupos AH encima.

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

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_bet_filters.py -v
python -m pytest tests/test_kelly.py::test_kelly_caps -v
```

`pre_kickoff.yml` tiene una casilla `debug_mode` que corre el analista contra 1 bet
futura, para verificar la integración sin gastar un ciclo de cron completo.

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

`archive/` en la rama `main` documenta la regla de **archivar, nunca borrar** para
módulos que dejan de ser alcanzables. Cada módulo retirado lleva su motivo, qué lo
sustituye y si es seguro re-ejecutarlo.

### Ramas

- **`master`** — producción y rama por defecto. Es la que dispara el agente externo.
- **`main`** — rama de un build de blueprint: trae herramienta de auditoría estática
  (`tools/audit`), 288 tests y documentación de esquema, pero le faltan 8 de los 9
  workflows. **No es desplegable.**
