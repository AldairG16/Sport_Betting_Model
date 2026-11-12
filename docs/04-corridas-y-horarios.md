# 04 · Corridas y horarios

## Resumen

Todo corre en GitHub Actions. **No lo dispara el cron de GitHub** (se atrasa horas): lo dispara
**cron-job.org** con `workflow_dispatch` contra `master`. El cron propio de GitHub solo queda de respaldo en el
cierre y el watchdog.

| Workflow | Quién lo dispara y cuándo (UTC → México) | Qué corre |
|---|---|---|
| `closing.yml` | cron-job.org a las :00 y :30; respaldo de GitHub a las :07 y :37 | `orchestrator.py --mode closing` y después `heartbeat_dispatch.py` |
| `morning.yml` | cron-job.org, 12:00 → 6:00 | `orchestrator.py --mode morning` |
| `evening.yml` | cron-job.org, 03:05 → 21:05 | `orchestrator.py --mode evening` |
| `weekly.yml` | cron-job.org, lunes 19:00 → 13:00 | `orchestrator.py --mode weekly`, `weekly_sanity_audit.py`, `clv_audit.py` |
| `late_results.yml` | `heartbeat_dispatch` desde el cierre, 00:00 y 06:30 México | `orchestrator.py --mode results` |
| `resolve_pending.yml` | `heartbeat_dispatch`, 07:00 y 19:00 México | `resolve_pending_bets.py` (el resolvedor con Claude) |
| `watchdog.yml` | cron de GitHub 00:17/06:17/12:17/18:17 y `heartbeat_dispatch` 4 veces al día | `watchdog.py` |
| `playdoit_checker.yml` | `heartbeat_dispatch` lo mantiene vivo (tandas de ~5 h 45 min) | `playdoit_checker.py --listen` (el revisor de capturas y los comandos del canal) |
| `pre_kickoff.yml` | El agente externo, cerca de cada partido | `pre_kickoff_analyst.py` (el analista con Claude) |
| `db_smoke.yml` | A mano | `db_smoke_test.py` (prueba de la base) |
| `tests.yml` | Cada push y PR a `master`/`main` | Todas las pruebas |
| `release.yml` | Un tag `v*` (publica) o a mano (sin publicar) | Pruebas y compilación del `.exe` del dashboard |

Un push que cambia un `.yml` de producción (`closing`, `morning`, `evening`, `weekly`, `late_results`,
`resolve_pending`, `pre_kickoff`) **lo dispara**. Por eso los YAML no se tocan sin permiso del dueño.

## Los modos del orquestador, paso por paso

`scripts/orchestrator.py --mode <modo>` corre una lista de pasos con `run_step`. Antes de todo:

1. `_db_preflight`: si la base no contesta, la corrida termina ahí, sin error. El aviso lo da el watchdog
   ([06](06-base-de-datos.md)).
2. `_budget_check`: el modo del presupuesto de la base (normal, ahorro o esencial) y sus avisos.
3. El candado: no corren dos orquestadores a la vez.

Al final guarda la corrida en `pipeline_runs` (con lo que leyó de la base) y sale en rojo si falló algún paso.

| Modo | Pasos (en orden) |
|---|---|
| `closing` (cada 30 min) | Pre-kickoff closing odds (recarga dirigida de cuotas y cierres) · Marcadores del día (ESPN) · Alineaciones publicadas · Picks del programa (anula horarios viejos de reprogramados, rehace picks y parlays) · Alertas de alineación · Partidos de hoy (Telegram) · Picks finales (Telegram). En modo ahorro corre uno por hora salvo partidos en las próximas 2 h. |
| `morning` | Fetch odds (y una foto en `odds_history`) · Precios Kalshi · xG real (si el weekly no pudo) · ClubElo · Predictions · Picks del programa · Goalscorer picks · Telegram (la mañana) |
| `evening` | Closing odds · Fetch results · resultados de respaldo (football-data y TheSportsDB) · Córners y tarjetas (ESPN) · Tiros y córners (ESPN) · Partidos repetidos · Results · Shadow results (anula reprogramados y cobra picks, parlays, sombra y propuestas) · Resolve pending (Claude) · Goalscorer resolve · CLV update · Backtest · Chequeo de salidas · Telegram (la noche) · y el avance de mañana: Fetch odds, Kalshi, Historial ESPN, Predictions, Picks del programa |
| `results` | Fetch results · respaldos de football-data y TheSportsDB · Partidos repetidos · Results · Shadow results · CLV update · Backtest |

**Archivados mientras no se cobre una apuesta propia (12-nov-2026).** Backtest (evening y results), Holdout,
Drift y Walk-forward (weekly) solo analizan las apuestas propias ya cobradas de `bets_history`. Desde el modo
recolección (25-sep-26) casi no se registran apuestas y la última cobrada es del 22-sep: repetían cada noche
y cada lunes el mismo reporte de las mismas 1,160 y dejaban en el log líneas con ❌ ("ROI negativo") que
parecen errores. El dueño pidió archivarlos (3c). `run_step(..., own_bets=True)` los salta con "🗄️ …
archivado" cuando la última apuesta cobrada (`win`, `loss`, `push`, medias) tiene más de 45 días
(`own_bets_idle`, una lectura por corrida); si vuelve a cobrarse una, corren solos otra vez. El código y sus
pruebas siguen en su lugar. Siguen corriendo, aunque toquen `bets_history`: CLV update (una apuesta nueva lo
necesita: hubo 2 en octubre, que quedaron sin cobrar), Calibration monitor (su estado lo vigila el chequeo
diario), CLV gate (también lee la sombra), CLV cache y "Apuestas sin fuente".
| `weekly` | Primero lo que aprende (segundos, solo base): Calibration monitor · CLV gate · Reactivación por sombra · Peso del modelo · Sesgo por lado · Evidencia de reglas · Filtros aprendidos · Referencia Pinnacle · Horario de apuesta · CLV cache · Holdout. Luego las cargas: historia, internacionales, eventos, ligas extra, segundas divisiones (ESPN), K-League, momios históricos, alineaciones (ESPN), buscador de ventajas, nombres de equipo y repetidos, apuestas sin fuente, xG real, ajuste Dixon-Coles · Auditoría del sistema · Drift · Market regime · Walk-forward · Reporte semanal de Telegram |

Los pasos marcados como opcionales se saltan en modo ahorro o esencial ([06](06-base-de-datos.md)).

<!-- detalle movido de OPERACION.md -->

## Cómo se dispara realmente

Esto **no** es lo que uno esperaría, y confundirlo cuesta caro:

- **El cron de GitHub casi no se usa.** Solo `watchdog.yml` (como respaldo) y
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
- **El latido del closing dispara los trabajos de horario fijo** (1-oct-2026). El
  closing es la corrida más puntual (336 seguidas sin un hueco de más de 45 min del 25-sep
  al 1-oct); el cron de GitHub corría el reintento de resultados y el watchdog con 4-6 h
  de atraso y se saltaba algunos, y el resolvedor no tenía quién lo disparara. Al final
  de cada closing, `scripts/heartbeat_dispatch.py` hace `gh workflow run` (token del
  workflow, permiso `actions: write`) de: `late_results.yml` a las 00:00 y 06:30,
  `resolve_pending.yml` a las 07:00 y 19:00, y `watchdog.yml` a las 03:00, 09:00, 15:00 y
  21:00 (hora de México). Cada horario tiene 30 min de ventana; no dispara si el trabajo
  arrancó hace menos de 25 min. Un disparo fallido deja el closing en rojo. El watchdog
  conserva además su cron: si el closing muere, el watchdog lo detecta igual.

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

**Un WARNING no le llega a nadie.** Solo los `log.error` van a Telegram. La auditoría
del 1-oct-2026 encontró fallas reales registradas como WARNING o `print`, y las pasó a
ERROR: el estado aprendido que no se pudo guardar o leer (`model_state`), las candidatas
en sombra rechazadas, el cierre de las sombras, las apuestas sin cierre por error de
búsqueda, el gate de CLV por liga y una apuesta real que termina `stale` (sin resultado,
fuera del bankroll: así pasaron 34 en abril-mayo sin un aviso). Regla: WARNING es solo
para lo que no cambia ni dinero ni aprendizaje.

**Fallar cerrado, no abierto.** Si falta una parte del estado aprendido (bloqueos, peso
del modelo, sesgos), la corrida sigue midiendo en sombra pero **no registra apuestas
reales** (`real_bets_with_state`). Si no se puede leer el bankroll, la corrida falla
(antes devolvía 100u y Kelly inflaba los stakes un 68%). Si el gate de CLV no tiene
datos, conserva los bloqueos anteriores (antes escribía la lista vacía).

**Telegram que no entrega = corrida en rojo.** Telegram es el único canal: si un
mensaje no se entrega (token vencido, bot bloqueado, 3 intentos fallidos), se anota en
`SEND_FAILURES` y el orquestador, el resolvedor y el watchdog terminan con código 1.
Así el aviso llega por el otro canal que queda: el correo de GitHub por la corrida roja.

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
"funcionó". De ahí los CHECK 5 y 6 del watchdog ([12](12-auditoria-y-chequeos.md)): miden **producto**, no
actividad.

---

## Los tres loops

Cooperan a través de PostgreSQL; entre corridas no sobrevive estado en memoria.
Cada uno es su propio workflow.

**1 · Pipeline diario** — `orchestrator.py`, modos `morning` / `closing` / `evening` / `weekly`.
`run_prediction_pipeline()` evalúa cada partido en 11 etapas con entradas y salidas
explícitas (`_stage_*` en `src/pipeline/prediction_pipeline.py`): fuerzas → λ →
probabilidades del modelo → mercado sin margen → combinación → cuotas → probabilidad
final (ancla, calibración, sesgos) → shadow → contexto de mercado → candidatas →
selección y stake. Entre las probabilidades del modelo y el mercado, la etapa 3b
descuenta el sesgo por lado aprendido contra Pinnacle ([09](09-aprendizaje.md)); la selección aplica el
**filtro Pinnacle** del modo recolección ([09](09-aprendizaje.md)). Después, `_apply_portfolio_limits`
(correlación, sospechosas, exposición y tope por slate). Cada etapa es la misma lógica de antes del 22-sep-2026
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
Toma bets atascadas en `pending` >6 h tras el kickoff (típicamente córners y tarjetas
esperando el CSV de football-data, o ligas que The Odds API no puntúa). Pide a Claude el
resultado FT vía `web_search`, completa `matches` y llama a `update_bet_results()`.
**Gasta tokens.** Corre a las 07:00 y 19:00 MX (lo dispara el latido del closing, más arriba) y
como paso del evening antes del resumen. Una respuesta con `confidence: low` no se usa:
con ella se liquidaría dinero. En su corrida suelta no existe el reporte de errores
tolerados del orquestador: si una consulta a Claude falla, avisa por Telegram y sale en
rojo. Con búsqueda web la respuesta llega en varios bloques de texto y a veces con una
frase antes del JSON: se busca el JSON en todo el texto, y una respuesta sin JSON cuenta
como falla (hasta el 2-oct-2026 se leía solo el último bloque y 9 de cada 10 respuestas
pagadas se perdían). Claude verifica que los equipos sean los pedidos: un partido de otro
club se guardaría con el nombre de la apuesta.

`scripts/backfill_espn_results.py` (gratis, API pública de ESPN) completa goles y medio
tiempo de las apuestas sin fuente: llena la fila que ya existe (±1 día) y solo si el
marcador final coincide; inserta solo si el partido no tiene fila. El marcador del día no
trae los tiempos de los partidos viejos: el medio tiempo sale del detalle del partido
(`/summary?event=`).

> **No corrió nunca hasta el 1-oct-2026.** `resolve_pending.yml` no tenía disparador
> desde el 11-may y el paso del evening no recibía `ANTHROPIC_API_KEY` (`evening.yml`
> no la pasaba): imprimía "abortando" y salía verde. 34 apuestas reales de abril-mayo
> (19.54u apostadas) quedaron `stale`. Hoy, sin key la corrida es un error; cada corrida
> deja su fila en `pipeline_runs` (modo `resolve_pending`) y el watchdog avisa si pasan
> 30 h sin una. `--include-stale` (casilla `include_stale` del workflow) suma, una vez,
> las apuestas reales que quedaron sin fuente; nunca las canceladas antes del kickoff.

`late_results.yml` cubre el caso barato: re-corre `--mode results` a las 00:00 y 06:30
MX para partidos que acaban después del evening, sin llamar a la LLM. Y el weekly
liquida las apuestas reales `stale` cuyo dato ya llegó con sus cargas
(`resolve_stale_bets`, paso "Apuestas sin fuente con dato nuevo", gratis). Antes no lo
corría nadie.

**4 · Aprendizaje semanal** — el modo `weekly` recalcula todo lo que el sistema
aprende de los datos que recolecta solo, y lo guarda en `model_state` ([09](09-aprendizaje.md)):
calibración, CLV gate, caché de CLV para Kelly, **peso del modelo frente al
mercado**, **sesgo por lado contra Pinnacle**, **reactivaciones por shadow**,
**evidencia de las reglas contra resultados reales**, **escala de los ajustes
manuales**, **filtros de selección** y **horario de apuesta** ([09](09-aprendizaje.md)). Las corridas diarias lo leen de la DB
al arrancar (`load_learned_state()` en el pipeline). Solo aprende de datos de la
cohorte actual (`LEARNING_SINCE`, [09](09-aprendizaje.md)). Estos pasos corren **al inicio** del weekly,
antes de las cargas de datos: las cargas tardan ~62 min (histórico 11', eventos 21',
ligas extra 25') y el 22-sep-2026 el job se cortó en el límite de 60 min antes de
llegar a aprender. Límite actual del job: 120 min.
