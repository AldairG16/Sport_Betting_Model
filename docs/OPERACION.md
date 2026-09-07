# Operación

Todo lo que hace falta saber para trabajar en este repo sin romper producción.
`CLAUDE.md` solo dice de qué va el programa; el detalle está aquí.

---

## 1. Fuentes únicas de verdad

**No repitas estos valores en ningún otro sitio — enlaza.** Todos derivaron al
menos una vez, y la auditoría estática ahora falla si vuelven a divergir. Este
documento cumple la regla: nombra las constantes, nunca sus valores.

| Pregunta | Autoridad |
|---|---|
| Default de cualquier constante ajustable | `config/settings.py`, leído con `env_int` / `env_float` / `env_str` |
| Tablas y columnas de la base | `docs/schema.md` |
| Qué hace cada fichero de `scripts/` y si escribe en la DB | `docs/scripts_inventory.md` |
| Horarios y bloque `env:` de cada loop | `.github/workflows/*.yml` — pero ver §2 |
| Disposición de cada módulo inalcanzable | `archive/ARCHIVE.md` |
| Variables de entorno que existen | `.env.example` |

> El checker `config_docs` lee `.env.example`, `README.md` y `CLAUDE.md` buscando
> valores documentados y los compara con `config/settings.py`. **No lee este
> fichero.** Por eso aquí no se escribe ningún valor: uno puesto aquí quedaría
> fuera de esa vigilancia y podría derivar sin que nadie se enterase.

---

## 2. Esta rama no es desplegable

`main` es el resultado de un build de blueprint. Tiene la herramienta de auditoría
estática, 288 tests y la documentación de esquema, pero **le faltan 8 de los 9
workflows**: solo está `weekly.yml`, y sin sus disparadores automáticos.

**Producción corre desde `master`**, que sí tiene el árbol completo. Lo dispara un
agente externo vía `workflow_dispatch`; el cron de GitHub apenas se usa. Si necesitas
saber cómo se ejecuta el sistema de verdad, la referencia es `docs/OPERACION.md` de
`master`, no ésta.

Consecuencia práctica: la fila de «horarios» de la tabla de arriba apunta a un
directorio que aquí está casi vacío. No infieras los horarios de este árbol.

### Un fallo verde se ve igual que un día normal

`run_step()` captura el error de cada paso y continúa, así que un apagón total de
datos **no** pone el run en rojo. Ocurrió: la key de The Odds API se desactivó el
17-jun-2026 (`401 DEACTIVATED_KEY`, pago fallido) y todas las ligas devolvieron
«0 partidos procesados» durante 82 días sin que nada avisara. De ahí los chequeos de
*producto* del watchdog (§5).

---

## 3. Servicios externos

El modelo depende de seis servicios de terceros. Las credenciales son *repository
secrets* en GitHub y un `.env` en local; la lista completa está en `.env.example`.

| Servicio | Para qué | Credencial |
|---|---|---|
| **The Odds API** | Cuotas (`/odds`) y resultados (`/scores`). Fuente crítica: sin ella no hay apuestas ni resolución. Cobra por liga y llamada. | `ODDS_API_KEY` |
| **PostgreSQL** | Todo el estado. Las tablas están en `docs/schema.md`. | `DB_URL` |
| **Anthropic** | Los dos agentes LLM. Ver §7. | `ANTHROPIC_API_KEY` |
| **Telegram** | Único canal de salida: picks, resumen diario, alertas. El analista usa su propio bot y cae al principal si no está configurado. | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| **football-data.co.uk** | Respaldo de resultados y **única** fuente de córners y tarjetas. Publica con 24-36 h de retraso. | ninguna, CSV públicos |
| **OpenWeatherMap** | Clima del partido. | `WEATHER_API_KEY` |

> **Regla de oro:** un secret sin configurar se expande a cadena vacía, y `int("")`
> revienta. Usa siempre los accesores de `config/settings.py`. Ver §6.

---

## 4. Los tres loops

Cooperan a través de PostgreSQL; entre corridas no sobrevive estado en memoria.

**1 · Pipeline diario** — `orchestrator.py`, modos `morning` / `closing` / `evening` /
`weekly`. Trae cuotas, `src/pipeline/prediction_pipeline.py` puntúa cada par (partido,
mercado), aplica calibración, filtros de edge y Kelly, e inserta en `bets_history` con
`result='pending'`. El modo `evening` re-pide resultados, llama a `update_bet_results()`
en `src/models/save_bets.py` para pasar pending → win/loss, calcula CLV y manda el
resumen a Telegram.

**2 · Analista pre-kickoff** — `scripts/pre_kickoff_analyst.py`. Busca bets con kickoff
dentro de la ventana definida por `PRE_KICKOFF_WINDOW_MIN` y `PRE_KICKOFF_WINDOW_MAX`,
las agrupa por partido y llama a Anthropic **una vez por partido**, no por bet, con el
dossier cuantitativo de la DB más `data/analyst_lessons.md` y un memo de aprendizaje.
Devuelve un veredicto por mercado y un `best_pick`, escribe en `pre_kickoff_analyses` y
avisa por su canal. **Nunca toca `bets_history`.**

> Hoy está **apagado a propósito** para no gastar tokens: el workflow está
> `disabled_manually` en GitHub y la variable `PRE_KICKOFF_ANALYST_ENABLED` está en
> `false`, lo que además silencia la alerta de heartbeat del watchdog. Para
> reactivarlo: habilitar el workflow y poner la variable en `true`.

**3 · Resolver de pendientes** — `scripts/resolve_pending_bets.py`. Toma bets atascadas
en `pending` tras el kickoff (típicamente córners y tarjetas esperando el CSV de
football-data, o ligas que The Odds API no puntúa), pide a Claude el resultado FT vía
`web_search`, completa `matches` y llama a `update_bet_results()`. **Gasta tokens.**
`late_results.yml` cubre el caso barato: re-corre `--mode results` para partidos que
acaban después del evening, sin llamar a la LLM.

### Cómo auditar

El pipeline **nunca borra** de `bets_history`. Para «¿por qué no apostó esto?»: si no
existe la fila, el modelo no lo puntuó; si existe en `pending`, apostó y espera; si
está resuelta, ya está. Para «¿por qué no habló el analista?»: mira `analyst_heartbeat`
(que el cron disparó) y su `error_msg` (fallos por bet).

---

## 5. Watchdog

`scripts/watchdog.py`. Solo lee, no escribe, no gasta créditos ni tokens.
**Silencio = salud**: solo manda Telegram si algo va mal.

| Check | Qué mira |
|---|---|
| 1 | La DB responde. |
| 2 | `MAX(updated_at)` de `upcoming_matches` es reciente. |
| 3 | Bets en `pending` demasiado antiguas. |
| 4 | Gap del `analyst_heartbeat` — **se omite** si `PRE_KICKOFF_ANALYST_ENABLED=false`. |
| 5 | Hay partidos futuros cargados. |
| 6 | Antigüedad de la apuesta más reciente. |

El 2 mide **actividad** y se deja engañar: el cleanup de `update_all()` toca la tabla
al borrar filas viejas aunque no entre ninguna nueva. El 5 y el 6 miden **producto**,
y son los que detectan un apagón de datos.

---

## 6. Gotchas que ya rompieron producción

**Secrets vacíos en GH Actions.** `int(os.environ.get("X", "default"))` devuelve
`int("")` y lanza `ValueError`, matando el pipeline antes de escribir nada. Usa
`env_int` / `env_float` / `env_str`. El último módulo que lo violó fue
`src/utils/anthropic_budget.py` y tiró todas las corridas del analista un día.

**`bet["edge"]` no es el edge.** Es `edge_ev = prob*odds − 1`. El real es
`bet["edge_market"] = prob − 1/odds`. Filtrar por `edge` deja pasar longshots y costó
una semana a −23 % de ROI. Filtra siempre por `edge_market`.

**Las claves de handicap asiático están parametrizadas.** El mercado de una bet es
`ah_home_-0.5`, pero `MIN_EDGE_BY_MARKET` y `calibration_factors.json` se indexan por
grupo. Usa `_ah_group()` de `src/models/calibration_monitor.py`. Hay precedente de un
`dict.get(mkt, default)` silencioso cayendo al default en todas las bets AH.

**`upcoming_matches` acumula duplicados stale.** Su clave de upsert incluye `match_day`,
derivado del kickoff, así que cuando la API corrige un horario se inserta fila nueva en
vez de actualizar. Todo consumidor debe leer con
`DISTINCT ON (home_team_norm, away_team_norm, sport_key) … ORDER BY updated_at DESC`.

**El cron de GH descarta `*/N` en los minutos punta** (`:00/:15/:30/:45`). Para algo más
frecuente que cada hora usa minutos off-peak. El síntoma es heartbeats con
`bets_found > 0, bets_analyzed = 0` durante horas.

**Los nombres de equipo pasan por `normalize_team()` antes de consultar.** `matches` y
`upcoming_matches` guardan minúsculas normalizadas. Un desajuste deja bets en `pending`
para siempre aunque `fetch_results` haya corrido.

**`fetch_results.py` no tiene guarda de créditos.** Los cuenta pero no se detiene por
umbral, a diferencia de `update_upcoming_matches`.

---

## 7. Presupuesto de Anthropic

Dos helpers en `src/utils/anthropic_budget.py` controlan cada llamada: `can_call()` la
rechaza si el gasto de hoy más la estimación supera `ANTHROPIC_DAILY_BUDGET_USD`, y
`record_call()` se llama después con el `usage` real y escribe en `anthropic_usage`.

Ambos agentes usan Haiku con `web_search`. La búsqueda web domina el coste por llamada.
No añadas llamadas a `web_search` a la ligera.

> `_ensure_table()` envuelve su DDL en `try/except: pass` y `get_daily_spent_usd()`
> devuelve `0.0` ante cualquier excepción. Si la tabla no se puede crear, el guard
> **lee 0.0 y deja pasar todas las llamadas**. Falla abierto, no cerrado.

Cuando el analista revienta en una bet, el traceback queda en
`analyst_heartbeat.error_msg` y salta alerta si fallan varias en una corrida. Nunca
vuelvas a poner un `except Exception: pass` alrededor de la llamada a la LLM — así se
perdió un día entero en silencio.

---

## 8. Calibración

`config/calibration_factors.json` se regenera cada semana en `step_calibration`.
`apply_calibration(prob, market, league)` de `src/models/calibration_monitor.py` es el
**único** punto de entrada: corrección isotónica o escalar con suavizado bayesiano y
clamp adaptativo. El sub-dict `by_league` se rellena para ligas con muestra suficiente;
se mira primero el nivel de liga y se cae al global, con la cadena de fallback de grupos
AH encima. Los umbrales concretos están en el módulo.

Cuando un mercado cruza los umbrales de alerta, el cron semanal manda
`check_calibration_alert()` a Telegram. Trata esas alertas como señal para **subir
`MIN_EDGE_BY_MARKET`** en ese mercado hasta que el sesgo se estabilice, no
necesariamente para reentrenar. Precedente: `ah_home_fav` y `ah_away_fav` saltaron en
mayo; se arregló subiendo MIN_EDGE y corrigiendo un bug de lookup, sin reentrenar.

Otros ficheros de parámetros que el código lee en runtime:
`config/ensemble_weights.json` (`src/models/ensemble_model.py`, que cae a pesos por
defecto **en silencio** si falta) y `config/thresholds.json`
(`src/models/threshold_optimizer.py`).

---

## 9. Auditoría estática y archivo

`python -m tools.audit` recorre el repo sin DB, sin secrets y sin gasto de API, y
escribe `audits/latest.json` y `audits/latest.md`. `--ci` corre solo el subconjunto que
bloquea el build: `broken-reference` y `config-inconsistency`.

`archive/` implementa la regla de **archivar, nunca borrar**. Todo módulo que deja de
ser alcanzable desde un entry point real se mueve ahí con `git mv` —rename puro, sin
reescribir contenido— y una fila en `archive/ARCHIVE.md` con cinco columnas obligatorias:
qué es, cuándo se archivó, por qué, qué lo sustituye y si es seguro re-ejecutarlo.
`tests/test_no_broken_imports.py` falla si un fichero aterriza ahí sin su fila.

`archive/` está excluido del recorrido del auditor, que es lo que hace de archivar una
disposición válida y no un barrido bajo la alfombra: la fila rinde cuentas.

---

## 10. Comandos

```bash
# Pipeline diario
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
python -m tools.audit
python -m tools.audit --ci
python -m tools.audit.checks.inventory --write   # regenera docs/scripts_inventory.md

# Tests — conftest.py levanta un entorno sin DB, así que la suite corre
# sin .env en local e idéntica en CI.
python -m pytest tests/ -v
python -m pytest tests/test_bet_filters.py -v
python -m pytest tests/test_kelly.py::test_kelly_caps -v
```

`pre_kickoff.yml` tiene una casilla `debug_mode` que corre el analista contra 1 bet
futura, para verificar la integración sin gastar un ciclo de cron completo.

---

## 11. Mundial 2026

`_check_world_cup_activation()` añade `soccer_fifa_world_cup` a `SPORT_KEYS` **solo
entre `WORLD_CUP_START` y `WORLD_CUP_END`**. El torneo terminó, así que hoy la función
sale antes y la key no se añade.

La versión original solo tenía fecha de inicio: seguía metiendo la liga en `SPORT_KEYS`
indefinidamente, y `update_upcoming_matches` pide cuotas por liga. Si vuelve a haber un
torneo, replica el patrón con las dos fechas.

`WORLD_CUP_BETTING_ENABLED` sigue en `false` por defecto: los picks de torneo van a
`data/paper_trades.jsonl` con etiqueta `[PAPER]` y **no** entran a `bets_history`.

---

## 12. Convenciones

El código está comentado en español (histórico del proyecto), con nombres de fichero y
función en inglés. Sigue ese estilo: comentarios en español, mensajes de commit en
inglés.
