# Operación

Todo lo que hace falta saber para trabajar en este repo sin romper producción.
`CLAUDE.md` solo dice de qué va el programa; el detalle está aquí.

---

## 1. Servicios externos

El modelo no funciona solo: depende de siete servicios de terceros. Si uno se cae,
el pipeline **sigue saliendo verde en Actions** y deja de producir en silencio —
ver §2.

| Servicio | Para qué | Credencial | Coste |
|---|---|---|---|
| **The Odds API** | Cuotas (`/odds`) y resultados (`/scores`). Es la fuente crítica: sin ella no hay apuestas ni resolución. | `ODDS_API_KEY` | Plan de pago de 20,000 créditos que se renueva el **día 1** de cada mes (medido: 9,548 → 19,958 el 1-oct-26 a las 03:06 UTC). Una liga cuesta 3 (h2h, totals, spreads) y un evento 7. El closing tiene presupuesto diario (§5, "Presupuesto diario de créditos"). |
| **PostgreSQL** | Todo el estado: `matches`, `upcoming_matches`, `bets_history`, `shadow_bets`, `bankroll`, `pre_kickoff_analyses`, `analyst_heartbeat`, `anthropic_usage`, y lo aprendido en `model_state` (§7). | `DB_URL` | Alojada fuera del repo. Ver §7. |
| **Anthropic** | Los dos agentes LLM: analista pre-kickoff y resolver de pendientes. | `ANTHROPIC_API_KEY` | ~$0.014-0.020 por llamada. Ver §6. |
| **Telegram** | Único canal de salida: picks, resumen diario, alertas del watchdog. | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; el analista usa `*_PREKICKOFF` y cae al bot principal si no están. | Gratis. |
| **football-data.co.uk** | Respaldo de resultados y **única** fuente de córners y tarjetas. Publica con 24-36 h de retraso. Si un archivo no existe, **manda otro parecido** sin avisar (§5). | Ninguna, son CSV públicos. | Gratis. |
| **OpenWeatherMap** | Clima del partido (`src/features/weather_impact.py`). | `WEATHER_API_KEY` | Gratis, 1000 llamadas/día. |
| **ClubElo** | Fuerza de cada liga para los partidos entre ligas (§7, "Fuerza de liga"). Su API (`api.clubelo.com`) responde 502; se leen las páginas públicas `clubelo.com/<PAÍS>`, una vez por semana, con pausa de 2 s. | Ninguna. | Gratis. |

`RAPIDAPI_KEY` solo la usa `test_api.py`, que no forma parte del pipeline.

La lista completa de variables, con sus defaults, está en `.env.example`. En GitHub
son *repository secrets*; en local, un `.env` (que está en `.gitignore`).

> **Regla de oro de las variables numéricas:** un secret sin configurar se expande a
> cadena vacía, y `int("")` revienta. Usa siempre `env_int` / `env_float` / `env_str`
> de `config/settings.py`. Ver §5.

---

## 2. Cómo se dispara realmente

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
selección y stake. Entre las probabilidades del modelo y el mercado, la etapa 3b
descuenta el sesgo por lado aprendido contra Pinnacle (§7); la selección aplica el
**filtro Pinnacle** del modo recolección (§7). Después, `_apply_portfolio_limits`
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
**Gasta tokens.** Corre a las 07:00 y 19:00 MX (lo dispara el latido del closing, §2) y
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
aprende de los datos que recolecta solo, y lo guarda en `model_state` (§7):
calibración, CLV gate, caché de CLV para Kelly, **peso del modelo frente al
mercado**, **sesgo por lado contra Pinnacle**, **reactivaciones por shadow**,
**evidencia de las reglas contra resultados reales**, **escala de los ajustes
manuales**, **filtros de selección** y **horario de apuesta** (§7). Las corridas diarias lo leen de la DB
al arrancar (`load_learned_state()` en el pipeline). Solo aprende de datos de la
cohorte actual (`LEARNING_SINCE`, §7). Estos pasos corren **al inicio** del weekly,
antes de las cargas de datos: las cargas tardan ~62 min (histórico 11', eventos 21',
ligas extra 25') y el 22-sep-2026 el job se cortó en el límite de 60 min antes de
llegar a aprender. Límite actual del job: 120 min.

### Cómo auditar

El pipeline **nunca borra** de `bets_history`. Para «¿por qué no apostó esto?»: si no
existe la fila, el modelo no lo puntuó o el filtro Pinnacle lo dejó solo en sombra
(`shadow_bets`, con su `pin_prob`); si existe en `pending`, apostó y espera
resolución; si está resuelta, ya está. Una fila `stale` con la razón en
`decision_log` (`revalidation.decision` = `cancelled_*`, o `lineup_guard`) es una
bet **cancelada** antes del kickoff: no se apostó, no cuenta en el ROI y los scripts
que recuperan stale no la tocan (`src/utils/bet_status.py`). Antes del 25-sep-2026 sí
la tocaban: `scripts/fix_resurrected_cancelled.py` (ensayo por defecto, `--apply`)
devuelve a cancelada la que se liquidó así y le regresa su profit al bankroll (el
28-sep: real betis vs getafe | over25, +0.99u). Todo lo que mide rendimiento (ROI,
acierto, conteo de bets) filtra con `RESOLVED_SQL`: una `stale` tiene stake y profit 0
y, contada, diluye el ROI. Para «¿por qué no habló el analista?»: mira
`analyst_heartbeat` (que el cron disparó) y su `error_msg` (fallos por bet).

Una auditoría revisa **lo que sale**, no solo que corra: el evening ya lo hace solo cada
día (`src/utils/output_checks.py`, ver §5 "Auditoría de salidas"). A mano:
`run_output_checks(days=7)` revisa una semana.

**En los logs de Actions faltan los "7", los "12" y los "eu".** GitHub tapa con `***`
cualquier texto igual al valor de un secret: `ODDS_REGION` vale eu, `FETCH_DAYS_AHEAD` 7 y
`API_TTL_HOURS` 12. Por eso "finales de ESPN (87 de 87)" sale como "(8*** de 8***)",
"neutral" como "n***tral" y un saldo de 1912 créditos como "19***". Telegram y la base no se
tapan. Los números se leen de la base o corriendo el chequeo a mano, no del log. Para el
saldo de créditos, cada línea "usados: U | restantes: R" suma 20,000: con eso se recupera
qué tapó cada `***`.

**La auditoría del sistema, siempre la misma (`scripts/system_audit.py`, 23-oct-2026).** Cada
auditoría hecha a mano encontraba una clase de error nueva porque revisaba otra cosa. Aquí
están todas las clases que ya pasaron, como chequeos que corren igual cada vez:

| Chequeo | Qué revisa | Cuándo corre |
|---|---|---|
| Salidas y datos de entrada | Liquidación contra un cálculo independiente; resultados que faltan; pendientes atorados; xG fuera de escala; lo aprendido del weekly; fechas espejo, resultados con fecha futura, repetidos, tablas de otras fuentes viejas; marcador provisional de ESPN contra el oficial; picks contra alineaciones; finales de ESPN; un partido reprogramado contado en dos fechas; el 🎯 de cada familia contra lo que esperaba el mercado | Evening (`output_checks`) |
| Equipos próximos con historia | Que tengan historia y que no esté partida en dos nombres | Weekly |
| Nombres de Understat y ESPN | Que cuadren con `matches` | Weekly |
| Lo que entra al modelo | La cobertura de cada feature en la última corrida (`model_state` `pipeline_coverage`) | Weekly |
| Elo | Ningún NaN | Weekly |
| Ligas de ESPN | Cada código apunta a la liga correcta (`chi.1` era Chile) | Weekly |
| Sesgo del modelo | Lo que queda de sesgo después de la corrección (`side_bias.residuals`), global y por región, en el 1X2, más de 2.5 y ambos anotan: avisa si pasa de 3 pt a 3 errores estándar (n ≥ 30). También si lo aprendido tiene más de 8 días | Weekly |
| Alineaciones de ESPN | Que cada alineación sea de su partido y su equipo (`espn_lineups.identity_problems`): un partido de ESPN guardado con más de dos equipos, o alineaciones que no comparten jugadores con su equipo | Weekly |
| Dixon-Coles al día | Que ningún equipo de los próximos 3 días con la historia que pide el ajuste (8+ partidos en 3 años) falte del ajuste Dixon-Coles: si falta, el ajuste es más viejo que la historia y sus partidos se saltan | Weekly |
| Kalshi con su partido | Que el 1X2 guardado de Kalshi no esté a más de 10 pt del de Pinnacle (`kalshi.stored_mismatches`): si lo está, es otro partido. Se limpia con `scripts/fix_kalshi_mismatches.py --apply` | Weekly |
| Nombres de los próximos partidos | Que ninguna fila de `upcoming_matches` siga con el nombre normalizado de antes de un alias (`update_upcoming_matches.stale_norms`): sería el mismo partido dos veces. Cada recarga de momios los re-normaliza antes de limpiar duplicados | Weekly |

- Corre en el weekly (paso "Auditoría del sistema", después del ajuste Dixon-Coles); las
  salidas ya las revisó el evening.
- Cada problema es ERROR y llega a Telegram.
- A mano: `python scripts/system_audit.py` (sale con código 1 si algo falla).
- **Cuando aparezca una clase de error nueva, se agrega aquí como chequeo**, para que la
  próxima auditoría no la tenga que volver a encontrar.

---

## 4. Watchdog

`scripts/watchdog.py`: lo dispara el latido del closing (03:00, 09:00, 15:00 y 21:00
MX) y conserva su cron `17 0,6,12,18 * * *` como respaldo independiente. Solo lee (y
crea `pipeline_runs` si falta), no gasta créditos ni tokens. **Silencio = salud**: solo
manda Telegram si algo va mal. Termina en **rojo** si la DB no responde o si su alerta
no se pudo entregar (revisa la respuesta de Telegram; un 400 por HTML se reintenta en
texto plano): el correo de GitHub es el segundo canal.

| Check | Qué mira |
|---|---|
| 1 | La DB responde. |
| 2 | `MAX(updated_at)` de `upcoming_matches` es reciente. |
| 3 | Alguna apuesta real sigue `pending` 4+ días después del partido: la liquidación no corre (su timeout la pasa a `unresolved` a los 3). |
| 4 | Gap del `analyst_heartbeat` — **se omite** si `PRE_KICKOFF_ANALYST_ENABLED=false`. |
| 5 | Hay partidos futuros cargados. |
| 6 | Antigüedad de la última **candidata** en `shadow_bets` (umbral `DIAS_SIN_CANDIDATAS_ALERTA`, 4 días). En modo recolección pueden pasar semanas sin una apuesta real. |
| 7 | El **closing** corre: alerta si la última corrida tiene más de 2 h, o si hubo menos de 6 en las últimas 6 h (se esperan ~12). |
| 8 | El **weekly** corre: alerta si la última corrida tiene más de 8 días. |
| 9-10 | El **reintento de resultados** y el **resolvedor** corren: alerta si pasan 30 h sin una corrida, o si no hay ninguna registrada. |

Una apuesta que termina sin resultado (`stale`) no se repite aquí cada 6 horas: avisa
**una vez**, como ERROR, en la corrida que la marca.

El 2 mide **actividad** y se deja engañar: el cleanup de `update_all()` toca la tabla
al borrar filas viejas aunque no entre ninguna nueva. El 5 y el 6 miden **producto**,
y son los que detectan un apagón de datos.

El 7 y el 8 existen desde el 24-sep-2026 porque closing y weekly los dispara
cron-job.org: si ese servicio deja de disparar, nada falla, simplemente no corren. Sin
closing no llegan las confirmaciones antes de cada partido y el aprendizaje no recibe
cierres; sin weekly el sistema no aprende. Cada corrida del orquestador deja una fila en
`pipeline_runs` (modo, hora, pasos fallidos, duración; no se borra nada), escrita por
`src/utils/pipeline_runs.py`. El conteo de 6 h importa porque el cron propio de GitHub
sigue disparando el closing a ratos: una corrida reciente no prueba que el disparo cada
30 min siga vivo. Con menos de 6 h de historial (recién instalado) el conteo no opina.
Mientras el weekly no tenga su primera fila, sirve de respaldo la hora de
`model_state.anchor_weights`.

---

## 5. Gotchas que ya rompieron producción

**Un paso "OK" no prueba que hizo algo.** El paso del xG real imprimió ✅ OK desde el
17 hasta el 25-sep-2026 sin cargar nunca un dato: `soccerdata` se instalaba sin sus
dependencias y el `import` fallaba, el código lo trataba como "no instalado — omitido"
y seguía. Hoy lee Understat directo (`/getLeagueData/<liga>/<temporada>`) y, si no llega
ninguna liga, registra un **error** que llega a Telegram. Ante un "OK" sospechoso, busca
en el log qué cargó.
- Desde el 27-oct-2026 el ✅ del chequeo de salidas dice lo que revisó: cuántos picks vieron
  su alineación publicada y cuántos partidos jugados tienen su final de ESPN.

**Un partido reprogramado contaba dos o tres veces (27-oct-2026).** Cada tabla guarda por
(partido, fecha). Cuando el feed cambia el horario, la fecha vieja se quedaba con su pick, sus
candidatas sombra y su análisis contra Pinnacle. La liquidación busca el resultado a ±1 día y le
daba el del partido real.
- Casos: PSG vs Lyon pasó del 25 al 26-oct. América vs Pachuca tenía tres horarios (23:00,
  01:00 y 01:05); el feed llegó a traer dos en la misma foto y se jugó a la 01:05. Toulouse vs
  Troyes pasó del 24 al 25.
- v2.4.3 solo lo escondía de Anteriores.
- Ahora `src/models/rescheduled.void_rescheduled` corre antes de liquidar, en el evening y en el
  weekly:
  - La fecha que vale es la de ESPN (`live_scores`) si la tiene. Si no, la de la fila escrita más
    recientemente en cualquier tabla, y en empate la más tardía.
  - Las otras fechas del partido a 3 días o menos quedan así: picks, sombra y propuestas pasan a
    'stale'; `model_vs_sharp` recibe `skipped_at`.
- `bets_history` no se toca: dos apuestas al mismo partido a distinta hora son dos apuestas y el
  bankroll las contó así. Hay 13 pares de abril y mayo.
- `backfill_picks` ya no reconstruye picks de análisis con `skipped_at`.
- El chequeo de salidas (sección 10, `counted_twice`) da ERROR si un partido vuelve a contar en
  dos fechas.
- La línea del log dice "N nuevos" (los que tuvieron filas anuladas en esta corrida) y "M ya
  anulados antes". Un partido anulado se vuelve a ver durante 30 días, y antes del 28-oct se
  anunciaba cada noche como si fuera nuevo.

**Una cobertura se mide donde la feature puede existir (27-oct-2026).** El chequeo de lo que
entra al modelo dividía el xG entre todos los partidos evaluados. Una noche con 32 de 42
partidos de Brasil, MLS, Suecia y Liga MX (ligas sin tiros en `matches`) dio "xG en 10 de
42", con los 10 de Serie A cubiertos: falsa alarma. Ahora xG, córners y tiros se juzgan
sobre `with_shots` (ver "Lo que entra al modelo se vigila", más abajo).

**El ajuste Dixon-Coles nunca convergía.** Con ~2,500 parámetros y gradiente por
diferencias finitas, L-BFGS-B agotaba `MAX_FUN` tras pocos pasos (`converged=False` en
todos los fits; el "ajuste" solo arrastraba el warm-start de la semana anterior). Desde
el 25-sep-2026 `dc_objective` da el gradiente **exacto**: converge en segundos (medido:
489 iteraciones, 4 s, gradiente máx. 0.02 frente a 65.8). Si vuelve a salir
`converged=False`, algo cambió en los datos o en el objetivo: no es "normal".

**Fechas DD/MM de football-data.** Sin `dayfirst=True`, "03/11/2026" se lee como
11-mar → 3-nov. `scripts/load_historical_fbref.py` (cargador viejo, fuera del pipeline)
metió así 15 partidos argentinos con fecha futura; `scripts/fix_swapped_dates.py` los
detecta y corrige (ensayo por defecto, `--apply`). Un partido con resultado y fecha
futura es siempre un error de fecha.

**Temporadas de football-data.** La lista de temporadas del weekly era fija y terminaba
en `2526`: desde agosto-2026 el weekly no cargaba la 2026/27 y solo la cubría el
respaldo diario de los últimos 10 días. Hoy se calcula hasta la temporada en curso
(`season_codes()` en `scripts/load_historical_data.py`). En las ligas "extra" (un CSV
por liga) la columna `Season` puede venir como `2026/2027` (la J-League juega de
agosto a mayo desde 2026-27): se guarda el año de inicio (`season_start()` en
`scripts/load_extra_leagues.py`); el texto rompía la columna entera y el weekly del
28-sep perdió 80 partidos.

**Segundas divisiones y ligas americanas (20-oct-2026).** Un ascendido llegaba con 6-9
partidos (los de esta temporada): Schalke, Elversberg, Venezia, Monza, Frosinone, Troyes,
Le Mans, Racing de Santander, Málaga, Cardiff o Bolton. El weekly carga ahora como
historia League One, 2. Bundesliga, Serie B, Segunda, Ligue 2 y la Championship escocesa
(`HISTORY_LEAGUES` en `src/utils/football_data.py`; no se predicen). Se cargaron 25,000
partidos desde 2015-16. Bélgica, Países Bajos, Portugal, Grecia y Turquía no tienen
segunda división en football-data: sus ascendidos siguen con poca historia, y el weekly
lo avisa como WARNING. MLS, Liga MX, Brasil y Argentina solo tenían los marcadores de
The Odds API, desde abril de 2026 y con huecos: la MLS no tenía ni un partido de 2025.
Ahora salen de football-data (`/new/USA.csv`, `MEX`, `BRA`, `ARG`). Se agregaron 6,016
partidos de la MLS y 6,085 de Argentina. Esos archivos traen la fecha local y The Odds API
la de UTC: un partido ya guardado con los mismos equipos a ±1 día no se repite
(`already_stored`). La K-League no está en football-data: solo tiene los marcadores de la
API.

**La segunda de Holanda sale de ESPN (24-oct-2026).** Willem II vs Cambuur y Den Haag vs
Heerenveen no se evaluaban: Cambuur y Den Haag tenían 9 partidos, y hacen falta 15 para que
Dixon-Coles pese 55% (`DC_WEIGHT_BY_HISTORY`, `MIN_DC_WEIGHT`).
- ESPN publica la Keuken Kampioen Divisie (`ned.2`).
- `espn_history.load_second_divisions` la carga en `matches` como
  `soccer_netherlands_eerste_divisie`. Los nombres salen de los ids ya emparejados
  (`espn_teams`) o de `normalize_team`, más 3 alias de football-data (waalwijk, graafschap,
  roda).
- Carga inicial: 1,230 partidos desde sep-2023. Desde entonces el weekly trae los últimos 2
  meses (paso "Segundas divisiones (ESPN)", antes del ajuste Dixon-Coles).
- Con el ajuste rehecho, los dos partidos se evalúan, y la distancia de Dixon-Coles a
  Pinnacle bajó de 13.0 a 12.0 pt de mediana.
- **Sin fuente gratis:** Bélgica, Grecia, Turquía (ESPN lista `tur.2` sin partidos), Portugal
  y la K-League. Sus ascendidos entran solos al llegar a 15 partidos en primera; el 24-oct
  tenían entre 7 y 10.

**football-data manda OTRO archivo cuando el pedido no existe.** El servidor corrige el
nombre y redirige a uno que difiere en una letra, con HTTP 200. Así entraron, sin un solo
error, partidos con la etiqueta de otra liga (medido el 1-oct-2026):

- `KOR.csv` → `NOR.csv`: 3,541 partidos **noruegos** guardados como K-League desde 2012.
  La calibración de la K-League (r16) era noruega, y Noruega quedó bloqueada por "solo
  32 partidos".
- `ECL.csv` → `EC.csv`: 5,850 partidos de la **National League inglesa** (5ª división)
  como Champions League desde 2015. Salían de ahí la tasa BTTS de la Champions y su
  "cobertura" de córners y tarjetas.
- Temporada nueva sin publicar (agosto-2026): `E0` → `EC` (12 partidos de la National
  League como Premier) y `SP1` → `SC1` / `P1` (Escocia y Portugal como La Liga).

Regla (`src/utils/football_data.py`): el archivo final tiene que ser el pedido (si no,
"no existe", igual que un 404), y su columna `Div` (temporada) o `Country` (`/new/`) la
esperada (si no, ERROR y no se carga). Los tres cargadores la usan. Lo ya guardado lo
reetiquetó `scripts/fix_league_labels.py`. Desde r17 la K-League usa factores neutros
(~60 partidos coreanos reales), Noruega los que r16 midió y la BTTS de la Champions el
default.

**El cargador histórico alteraba córners y tiros.** Hasta el 2-oct-2026,
`load_historical_data` pasaba las estadísticas por `advanced_impute()`. Rellenaba los
huecos con promedios de liga y multiplicaba córners y tiros, también los reales, por
`0.8 + 0.4·goles/2.5`; en Burnley 0-3 City, los córners 6-5 quedaron guardados como 8-6.
En la base, 28,993 de 45,001 partidos tenían estadísticas que no eran las del CSV (solo
13% de los córners de la Premier 2023-24 eran los reales), con 9,684 valores inventados.
Los modelos de córners y tiros y el xG proxy aprendían de esos números, y una apuesta de
córners se liquidó con un dato alterado. Hoy se guarda lo que publica football-data y, sin
dato, NULL. Si un modelo quiere imputar, lo hace al calcular sus features, nunca al
guardar. `scripts/fix_match_stats.py` restauró los valores reales con respaldo en
`matches_identity_backup`, y `scripts/fix_stat_settlements.py --motivo "..."` corrigió las
liquidaciones afectadas. El cargador además **completa** las estadísticas que falten en un
partido que ya existe (`fill_null` de `src/utils/db_batch.py`): antes, un partido que había
insertado The Odds API sin estadísticas no las recibía nunca.

**El mismo club con dos nombres.** football-data escribe "Man United", "Leeds", "Paris
SG"; The Odds API, "Manchester United", "Leeds United", "Paris Saint Germain". El mismo
partido quedaba dos veces y la historia del club repartida en dos nombres (Dixon-Coles
veía dos equipos; las búsquedas de historia, con el nombre de la API, no encontraban la
otra mitad). Medido el 1-oct-2026: 5,936 partidos repetidos (la Premier tenía 456 por
temporada en vez de 380). La capa aprendida de `normalize_team` lo une: un equipo no
juega dos partidos el mismo día, así que dos filas con la misma fecha (±1 día), el mismo
marcador y el mismo equipo del mismo lado son el mismo partido y el rival tiene dos
nombres (`src/utils/team_identity.py`). Se aceptan solos los nombres compatibles (uno
contiene al otro o casi iguales); los demás, solo verificados a mano (`ALLOW`);
`DENY` guarda los clubes distintos que los datos mezclaban (PSG y Paris FC). Si los dos
equipos estaban escritos distinto, el par sale en una segunda ronda. El mapa vive en
`config/team_aliases.json` (canónico = el nombre de la API); `scripts/learn_team_aliases.py
--write` lo regenera. El aprendizaje necesita el mismo partido guardado con los dos nombres.
Cuando cambia la fuente no hay repetidos y la historia queda partida. El 20-oct-2026 había 7
casos: Atlético Mineiro tenía 16 partidos como "atletico mineiro mg" y 539 como
"atleticomg"; igual Athletico, Pumas, Atlante, Huracán, Aldosivi y Beveren. Desde entonces el
weekly busca ese patrón (`split_identities`): un equipo próximo con menos de 60 partidos con
su nombre y, en su liga, un nombre parecido con 20 o más que casi no aparece mientras
aparece este. Dos clubes de verdad de la misma liga juegan las mismas jornadas. Lo avisa
como ERROR con el alias que falta. Esos alias van en `TEAM_ALIASES` (`src/utils/team_alias_map.py`),
con canónico = el nombre que tiene la historia; `scripts/fix_team_identities.py --apply`
pasa las filas guardadas al canónico, con respaldo. El weekly (paso "Nombres de equipo y repetidos") busca pares nuevos
sin unir (un ascendido, una fuente nueva), fusiona los partidos repetidos antes del ajuste
Dixon-Coles y avisa como ERROR solo si el nombre raro sigue en la base o ya se había
fusionado en otra corrida (la fuente lo sigue mandando). Si estaba solo en partidos
repetidos y es la primera vez, la fusión lo resolvió y queda una nota en el log. Así pasó el
5-oct-2026 con "atl madrid": un partido de agosto que entró con ese nombre. La primera carga
completa de la temporada lo volvió a meter como "ath madrid", el aviso salió y la auditoría
lo fusionó un minuto después. Cuando avisa, hay que agregar el alias: `--write` si la
evidencia sigue en la base, o a mano en `TEAM_NAME_MAP` si la fusión ya la borró. Después,
pasar lo guardado con `fix_team_identities.py` (en seco primero). La fusión conserva el
nombre de siempre (el de más partidos), no la fila que salga primero. Si conservaba la
rara, la carga siguiente volvía a meter el partido cada semana. El aprendizaje no ve a un
ascendido que la API escribe distinto ("FC Schalke 04" y el historial "schalke 04"): nunca
queda el mismo partido con los dos nombres. Para eso el mismo paso avisa como ERROR si un
equipo de los próximos 10 días (ligas de club) no tiene ni un partido con su nombre, y sugiere
el parecido de su liga. El 8-oct-2026 había 12 así y el modelo los veía sin historia; van en
`TEAM_NAME_MAP`. `scripts/fix_team_identities.py`
pasó lo guardado al canónico y unió los repetidos, con respaldo en `matches_identity_backup`. Efecto
colateral que dejó de pasar: el "temporada ya cargada" del weekly contaba los repetidos,
así que dejó de cargar la 2025/26 antes de terminar.

**`model_proposals` guarda lo que el modelo propuso y por qué no se apostó.** Desde el
12-oct-2026, cada apuesta que llega a la selección final (`_stage_select_bets`) queda con su
decisión: `apostada` o el motivo del descarte en palabras (`no le gana a Pinnacle por 2 pt`,
`ventaja menor al mínimo`, `límite de cartera del día`…; ver `src/models/proposals.py`). Antes
solo quedaba el conteo en el log ("55 sin valor frente a Pinnacle"). Un partido evaluado
varias veces conserva la última decisión. Se liquida con la misma función que las apuestas
(stake 1) en el paso de resultados sombra, y el reporte semanal resume qué habría pasado si
se jugaban todas.

El resumen semanal no cuenta los mercados desactivados (desde el 19-oct-2026). Su cuota es
una constante inventada, no la de una casa: más/menos 1.5 y 3.5 goles, tiros. El reporte
del 19-oct sumaba "+8.10u" con ellos, y esa ganancia no se podía tomar. En la tabla siguen
guardados.

**El laboratorio de estrategias mide sin dinero.** Desde el 13-oct-2026, el weekly mide
contra el cierre válido de Pinnacle varias formas de elegir apuestas entre las candidatas
shadow (`src/models/strategy_lab.py`): lo que quiere el modelo, el filtro actual, precio sobre
Pinnacle sin el modelo, etc. Usa la misma estadística y el mismo veredicto que
`sharp_reference`, con 100 cierres válidos en 30 partidos. El resultado va en el mensaje
semanal de la referencia Pinnacle y en `model_state` (`strategy_lab`, con historia). Solo
mide: pasar una estrategia a dinero real es decisión del dueño.

El 19-oct-2026 se sumó **"precio sobre Pinnacle sin cuotas altas"** (hasta +250).
- **Por qué:** "precio sobre Pinnacle" ganaba al cierre (+1.4%), pero a 1u perdía. Toda esa
  pérdida vino de cuotas mayores a +250: acertaron 12% cuando Pinnacle daba 20%.
- **Cómo se mide:** la idea salió de esos mismos resultados, así que solo cuentan partidos
  desde el 20-oct (`NO_LONGSHOTS_SINCE`). Con los datos viejos se vería ganar por
  construcción.

**"La mejor cuota" no es la cuota del dueño.** `home_odds`, `over25_odds`, etc. son la más
alta entre ~20 casas europeas, incluidos los intercambios (Betfair, Matchbook). Medido el
13-oct-2026 sobre 205 partidos, contra el precio justo de Pinnacle:

| Precio | Valor medio | Apuestas con +2% o más |
|---|---|---|
| Mejor cuota | −0.4% | 19% |
| Promedio entre casas | −7.7% | 0.2% |

Una casa típica como PlayDoit paga cerca del promedio. Por eso las candidatas guardan
también `odds_avg`, el promedio entre casas para 1X2 y over/under 2.5 (`consensus_*` en
`upcoming_matches`), y el laboratorio mide a los dos precios. Cuenta el del promedio.
`odds_history` guarda además Pinnacle y el promedio, y solo antes del kickoff (después la API
trae cuotas en vivo). Es la base para medir cuándo Pinnacle se mueve y las casas comerciales
tardan en seguirlo.

**Kalshi es la segunda referencia sin margen.** Desde el 14-oct-2026, `src/features/kalshi.py`
lee la API pública de Kalshi, sin clave y sin créditos. Es un mercado de predicciones de EE. UU.
cuyo precio es la probabilidad.

- **1X2:** difiere de Pinnacle 0.75 pt en promedio (104 partidos, correlación 0.998), así que
  no aporta información nueva.
- **Donde sí sirve:** ambos anotan y ganador del primer tiempo, que Pinnacle no publica en el
  feed.
- **Captura:**
  - morning y evening guardan en `kalshi_history` los cambios de los próximos 4 días;
  - el closing guarda todo lo que arranca en las próximas 3 h, porque esa última foto es el
    cierre.
- **Emparejamiento:** Kalshi abrevia nombres ("bilbao", "m gladbach") y publica la hora de FIN
  estimada (+3 h en ganador y 1T, +4 h en ambos anotan y totales). Por eso se empareja por liga,
  inicio a ±90 min y parecido de nombres, y se guarda con los nombres y la hora del sistema.
  - **Desde el 23-oct-2026:**
    - Cada nombre tiene que parecerse por separado (`KALSHI_MIN_SIDE` 0.5) y las palabras
      genéricas no bastan. Antes, con el partido verdadero fuera de la ventana, Gimnasia
      Mendoza vs Platense se guardó como Gimnasia LP vs Sarmiento.
    - Un evento cuyo 1X2 queda a más de 10 pt del de Pinnacle no se guarda (`price_mismatches`):
      Kalshi y Pinnacle difieren 0.74 pt en promedio.
    - Con los 163 eventos abiertos ese día empareja igual que antes (3 alias: "atletico",
      "new york rb", "jeju sk").
    - El partido mal guardado se quitó con `scripts/fix_kalshi_mismatches.py` (respaldo en
      `kalshi_history_backup`).
- **Medición:** el laboratorio mide esos mercados contra el cierre de Kalshi.
- **Qué no cubre:** córners y tarjetas casi no se operan en Kalshi (25-46 c de diferencia
  entre compra y venta). Apostar ahí no está al alcance del dueño (cuentas de EE. UU.).

**El filtro de alineaciones lee los onces de ESPN.** Cancela la bet de un lado si su
goleador de élite no sale de titular (`lineup_guard` en `scripts/revalidate_pending_bets.py`).
Hasta el 20-oct-2026 los onces venían de API-Football; la cuenta gratuita quedó suspendida el
16-oct y el filtro estaba apagado (el chequeo diario solo avisaba). Ahora lee la tabla
`lineups`, que `capture_upcoming` llena en el mismo closing, antes de revalidar; ESPN es gratis
y no pide clave. Se quitó el chequeo diario de API-Football. El ajuste por clima está
apagado: `WEATHER_API_KEY` nunca se cargó en GitHub y consultaba el clima actual, no el de la
hora del partido.

**`odds_history` guarda cambios, no fotos.** Con el closing cada 30 min, el 97% de las
fotos eran copias idénticas (~8 MB/día; en el plan gratuito de Neon, la base se llenaba
en ~2 meses). Desde el 25-sep-2026 solo entra una foto si la cuota de ese partido cambió
y nada se borra.

**La base corta una conexión inactiva.** Un script que intercala consultas con
descargas largas deja la conexión del pool quieta, y la siguiente consulta revienta con
`server closed the connection unexpectedly` (2-oct-2026, en el primer intento de
`fix_league_labels.py`). En scripts largos: descargar todo primero y después la parte de
base de datos seguida, con `engine.dispose()` antes para empezar con conexiones nuevas.

**No edites archivos con `Get-Content`/`Set-Content` de Windows PowerShell 5.1.** Lee
UTF-8 sin BOM como ANSI y escribe con BOM: rompe todos los acentos del archivo.

**Runner fijo: `ubuntu-24.04`.** GitHub anunció que `ubuntu-latest` pasa a Ubuntu 26.04
desde el 19-oct-2026. Un cambio de imagen puede romper la instalación de dependencias o
el Python de **todas** las corridas a la vez sin tocar el código, así que los workflows
fijan `ubuntu-24.04` (25-sep-2026). Subirlo es una decisión: en una rama, con `tests` y
el smoke test en verde. `release.yml` compila en `windows-latest`: si falla, solo se ve
al publicar una versión del dashboard.

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

**El modelo estuvo roto del 28-sep al 21-oct-2026 en las 5 grandes ligas y la Champions.**
La demostración con los partidos del 21-oct destapó dos errores silenciosos.

- **xG de Understat.** `team_xg_real` guarda el total de la temporada, y `get_real_xg` lo
  devolvía como si fuera por partido: el Real Madrid tenía 23 xG "por partido". Se mezclaba
  al 40% en el ataque y la defensa, y los goles esperados chocaban con el tope (3.5 y 3.5).
  El 1X2 del modelo salía ~40/17/43 en todos esos partidos: Real Madrid–Leipzig 42/18/40
  contra 79/13/8 de Pinnacle. Ahora `get_real_xg` divide entre los partidos, y el pipeline
  descarta cualquier xG por partido fuera de 0-4 (`_credible_xg`).
- **Peso de Dixon-Coles.** Se elegía por la historia del equipo (25%, 40% o 55%), pero esa
  historia se contaba con la ventana de la forma, que tiene 8 partidos como máximo: pesaba
  25% siempre. La forma son goles crudos sin ajustar por la fuerza del rival ni de la liga.
  - Ahora cuenta los partidos de los últimos 3 años (`load_team_history_counts`): 30 o más
    → 75%, 15 o más → 55%, si no 25%.
  - Prueba fuera de muestra con los ajustes semanales guardados (526 partidos, del 23-sep al
    20-oct), log-loss del 1X2: 1.066 con 25%, 1.014 con 55%, 0.996 con 75%. Mejoró en 21 de
    27 ligas.
  - En los 165 partidos con precio de Pinnacle, la distancia al mercado bajó de +0.167 a
    +0.080. Sigue peor que Pinnacle: el mercado manda.
- **Confianza ajustada.** `_stage_market_context` pisaba la confianza ya ajustada (Monte
  Carlo, acuerdo del ensemble, córners y tiros) con la cruda.
- **Historia corta.** Un equipo con menos de 5 partidos en la base (`MIN_FORM_MATCHES`) ya no
  se evalúa. En la Champions, Shakhtar y LASK tenían 2 porque sus ligas no se cargan, y el
  modelo daba 46/22/32 al Inter contra 77/14/9 de Pinnacle. Antes solo se saltaban los que
  tenían 0.

**Lo aprendido de la opinión del modelo empieza de nuevo.** El peso del ancla (`anchor_learner`)
y el sesgo por lado (`side_bias`) solo leen partidos desde `MODEL_SINCE` (2026-10-22). Con las
semanas del modelo roto, el sistema había aprendido que el modelo valía 1-7%. El resto del
aprendizaje sigue desde `LEARNING_SINCE`: no depende de la opinión del modelo.

**Auditoría de salidas (21-oct-2026): revisar lo que sale, no solo que corra.** Las
auditorías anteriores miraban flujos en verde y excepciones; los errores graves corrían
"bien" con números equivocados. Esta volvió a liquidar todo con un cálculo propio (1,142
apuestas reales, 5,576 sombra y 228 propuestas: coinciden al 100%) y revisó los números
del modelo partido por partido.

- **Hándicap y empate anula, del mismo reparto que el 1X2.** El 1X2 sale del ensemble y
  el hándicap y el empate anula salían de la Poisson: en 14 de 88 partidos el empate anula
  no cuadraba con el 1X2 por más de 5 pts. Ahora `consistent_score_matrix` escala cada
  región de la Poisson (gana local, empate, gana visitante) al 1X2 final, y el hándicap y
  el empate anula se calculan de esa matriz (`prob_ah_from_matrix`). Hándicap −0.5 = gana
  local, exacto.
- **Sin Dixon-Coles confiable, el partido no se evalúa** (`MIN_DC_WEIGHT` = 0.5). Con 25%
  o nada de Dixon-Coles manda la forma cruda: 6 partidos a más de 20 pts de Pinnacle
  (K-League, Beveren, Suecia). Se saltan ~1 de cada 5 (`Sin Dixon-Coles` en el resumen
  del pipeline). Si se salta más del 40% del día es ERROR: el ajuste del weekly quedó
  incompleto. Si falla el conteo de historia no se salta nada: el peso saldría de la
  ventana de la forma (25% siempre) y se perdería el día entero.
- **La forma se topa antes de mezclar.** Un ascendido que goleaba en segunda llegaba con
  5-6 goles esperados y, aun con 75% de Dixon-Coles, chocaba con el tope de 3.5.
- **Sin momios inventados.** Más/menos 1.5 y 3.5 goles y tiros tenían precios fijos
  (1.35, 3.20, 2.20, 1.65, 1.80). Estaban desactivados, pero si otra regla los frenaba
  antes (liga bloqueada) quedaban en propuestas y sombra. Ya no se cotizan, y los
  resúmenes descartan las filas viejas por nombre de mercado
  (`proposals.FAKE_ODDS_MARKETS`).
- **El historial del dashboard usa nombres canónicos.** `model_vs_sharp` guarda el nombre
  del día del análisis; si después se aprendía un alias, el partido no encontraba su
  resultado.
- **Tarjetas: amarilla = 1 y roja = 1, como PlayDoit** (el dueño, 21-oct-2026). Hasta
  entonces se liquidaban solo con amarillas.
  - `save_bets.card_total` liquida así. Una roja sin dato cuenta 0: falta en 1 de cada
    ~7,600 partidos, y el resolver con Claude también la llena.
  - Los dos modelos (`cards_stats`, `props_fair`: columnas `home_cards` y `away_cards`) y el
    chequeo independiente cuentan igual.
  - Se re-liquidaron 3 apuestas reales (−0.57u, con `fix_stat_settlements.py` y nota en
    `bankroll_history`) y 12 de sombra.

**Chequeo diario de salidas** (`src/utils/output_checks.py`, paso "Chequeo de salidas" del
evening, antes del resumen de Telegram). Cada problema es un ERROR y llega en "errores
tolerados":

- liquidación de los últimos 3 días contra el cálculo independiente (`settle_independent`);
- **córners y tarjetas de ESPN** (sección 9, v2.5.1, `espn_final_coverage`): de los partidos de
  los últimos 3 días en ligas de ESPN, al menos 85% con su final en `live_scores` (10+ partidos);
- **picks y alineaciones** (sección 8, v2.5.0, `lineup_pick_problems`, desde el 27-oct):
  - ningún 🎯 a que gana un equipo al que le faltaron 4+ titulares habituales;
  - si más del 30% de los picks con las dos alineaciones publicadas no las vio (10+ picks),
    el closing no los está recalculando;
- partidos analizados que ya se jugaron y no tienen marcador (más del 15%);
- pendientes atorados más allá de su ventana (reales 8 días, sombra y propuestas 11);
- xG real de más de 4 por partido;
- claves del weekly sin actualizar en 9 días o más;
- **el 🎯 contra el mercado** (sección 11, 28-oct-26, `star_vs_market`): por familia, los
  picks 🎯 liquidados contra la probabilidad del mercado sin margen. Es ERROR con 30+ picks
  y 2.5 desviaciones o más por debajo: el 🎯 de esa familia elige peor que el mercado, o
  una probabilidad salió volteada.
  - La línea de ✅ dice cada familia con 10+ picks, por ejemplo "Goles 13 de 28 (esperado
    15.0)".
  - El umbral se midió sobre toda la historia el 28-oct: el z más bajo con 30+ picks fue
    +0.35 (Ambos anotan) y +1.18 (Ganador). Goles iba en −0.78 con 28 picks, que es ruido.
  - Solo avisa: no quita el 🎯. Si se apaga solo o no es decisión del dueño.

Al final de cada corrida del pipeline, `pipeline_invariants` revisa los goles esperados en
el tope (más del 5% de los partidos), los partidos a más de 20 pts de Pinnacle (más del
10%) y la distancia típica a Pinnacle (más de 9 pts). Con el xG mal escalado, los tres
habrían avisado el 28-sep.

**Goleadores (papel, `scripts/goalscorer_picks.py`).** Hasta el 20-oct-2026 todos los picks
salían en 70% y ninguno se liquidaba. La probabilidad dividía entre la tasa del propio
jugador, así que λ quedaba igual a los goles esperados de todo el equipo y el tope de 1.2
daba 70% para cualquiera. Ahora λ = tasa del jugador × (goles esperados hoy / goles por
partido del equipo en el último año), con la escala entre 0.5 y 2. La liquidación solo
buscaba en `match_events` (selecciones). Ahora usa los goles por jugador de ESPN (tabla
`lineups`, mismo equipo y mismo rival, ±1 día). Si el jugador no estuvo en el partido, el
pick queda 'void', como en las casas. Sin datos a los 10 días queda 'stale'. **Los picks
anteriores al 20-oct llevan el 70% del error: no sirven para medir el modelo.**

**Resumen nocturno: el día de hoy.** Se anclaba al último día con apuestas reales resueltas.
En modo recolección no hay, así que caía a "ayer": el resumen de las 21:00 hablaba del día
anterior. Ahora resume hoy, y ayer solo si el run se atrasó pasada la medianoche
(`evening_target_day` en `scripts/orchestrator.py`).

**Kalshi: reintento ante el límite de llamadas.** Desde GitHub, 5-9 ligas por corrida
fallaban con HTTPError, y las mismas respondían bien desde otra máquina. Ahora `_get`
espera 0.25 s entre llamadas y reintenta ante 429/5xx (1, 2 y 4 s).

**The Odds API: reintento ante 5xx y errores de red (24-oct-2026).** Un 502 pasajero dejó la
MLS sin recargar en un closing. `update_upcoming_matches.get_with_retry` reintenta 2 veces,
tras 3 y 10 s, en los momios por liga y por evento. Un 5xx no cobra créditos; un 4xx (clave,
plan, sin créditos) no se reintenta.

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
tope propio (`CLOSING_MAX_SHADOW_EVENTS_PER_RUN`, 2 desde el 20-oct; antes 6) y solo corre
con más de `CLOSING_SHADOW_MIN_CREDITS` (3000) créditos. Antes quedaban sin cierre válido: el
24-sep, ambos anotan y empate anulado de Seattle–Salt Lake tenían "cierre" de 12 h
antes, y así el aprendizaje de esas familias nunca recibía datos. El costo estimado eran
8-12 eventos por día de fin de semana; medido del 10 al 20 de octubre fueron 55-78 (× 7).

**Presupuesto diario de créditos (20-oct-2026).** Del 10 al 20 de octubre se gastaron ~925
créditos por día, el 90% en el closing: unas 90 recargas de liga (× 3) y 55-78 recargas por
evento solo para candidatas sombra (× 7) en un día de fin de semana. A ese ritmo el saldo
se acababa el 26-oct y los topes fijos (sin cierres bajo 1,500, sin cuotas bajo 500) dejaban
al sistema sin aprender la última semana del mes. Ahora el closing gasta como máximo
(saldo al empezar el día UTC − 600 de reserva) / días hasta el día 1
(`src/utils/credit_budget.py`; el estado del día va en `model_state.odds_credit_day`). El
gasto del día cuenta todo lo que bajó el saldo (morning, evening y closing); lo que no se
gasta queda para los días siguientes. Orden: primero las recargas de liga; los eventos
sombra, solo con lo que sobre. Los partidos con una **bet real** se recargan siempre,
aunque el presupuesto del día ya se haya usado (marca `bet` de `plan_closing_targets`).
Morning y evening no se frenan: sin sus cuotas no hay predicciones. Variables:
`ODDS_API_RESET_DAY` (1) y `ODDS_API_RESERVE` (600).

**El presupuesto no cubría lo que más gastaba (29-oct-2026).** El saldo cruzó el umbral de
parada (500) el 28-oct a las 14:30 UTC, 3.5 días antes de la renovación, con el presupuesto
puesto. Desde ahí no se descargaron cuotas, y cada corrida mandaba su aviso por Telegram
(cada 30 min).
- El presupuesto solo frenaba la recarga dirigida. La actualización normal del closing
  (`update_all`: ligas cuando vence su caché de 12 h y mercados por evento de las próximas
  48 h) no tenía tope. El 23 y 24-oct fue ~85% del gasto (~2,200 créditos, ~60% en mercados
  por evento), contra ~440 por día de presupuesto.
- Morning, evening y resultados (~200 por día) salían del mismo presupuesto, así que el
  último día el saldo terminaba ~200 por debajo de la reserva, bajo el umbral.
- `fetch_results` pedía las 19 ligas en cada corrida (2 créditos cada una con `daysFrom`,
  ~38 por corrida, ~114 al día) y contaba 1 por liga con resultados.

Ahora:
- El closing llama `update_all(budgeted=True)`: solo ligas y eventos que empiezan en las
  próximas `CLOSING_HORIZON_HOURS` (6), y con el presupuesto del día. Lo demás lo
  actualizan el morning y el evening, sin cambios.
- El presupuesto del closing es el del día menos `ODDS_API_MANDATORY_PER_DAY` (200).
- `fetch_results` pide solo las ligas con un partido analizado (o una apuesta pendiente) ya
  jugado y sin marcador en `matches` (`leagues_needing_results`). Si no puede leer la base,
  pide todas, como antes. Cuenta los créditos con el header `x-requests-last`.
- Los avisos de créditos bajos van una vez por día UTC (`credit_budget.alert_once`, estado
  en `model_state.odds_credit_alert`).
- El comentario de costo en `.github/workflows/late_results.yml` quedó viejo. No se editó
  porque un push a ese YAML dispara una corrida.

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
- **Con un alias nuevo pasaba lo mismo (23-oct-2026).** La limpieza de duplicados reconoce
  el partido por los nombres normalizados. Cuando un alias cambiaba uno ("erzurum bb" →
  "erzurumspor"), la fila vieja quedaba con el nombre anterior y el pipeline analizaba el
  partido dos veces, hasta 7 días después.
- Ahora cada recarga corre `_renormalize_names` antes de `_cleanup_stale_duplicates`, y la
  auditoría del sistema lo vigila (chequeo "Nombres de los próximos partidos").
- Las tablas derivadas (`model_vs_sharp`, `program_picks`) pueden tener nombres de antes de un
  alias en partidos ya jugados. No importa: quien las lee normaliza (`match_key`, la
  liquidación).

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

**Los nombres de equipo pasan por `normalize_team()` antes de consultar y de guardar.**
`matches` y `upcoming_matches` guardan el nombre canónico en minúsculas (las selecciones
del cargador internacional conservan sus mayúsculas: "China PR"). Un desajuste deja bets
en `pending` para siempre aunque `fetch_results` haya corrido, o duplica el partido con
otro nombre. Todo `INSERT` en `matches` usa `normalize_team`.

**`fetch_results.py` no tiene umbral de créditos**, pero desde el 29-oct-2026 solo pide las
ligas a las que les falta un marcador (`leagues_needing_results`); ver "El presupuesto no
cubría lo que más gastaba".

**Un análisis viejo no es un análisis (22-oct-2026).** `model_vs_sharp` guarda una fila por
partido y solo la pisa un análisis nuevo. Si una corrida ya no evalúa el partido (historia
corta, sin Dixon-Coles confiable, sin momios), la fila de una corrida anterior seguía ahí, y
de ahí salían el pick, el sesgo por lado y lo que mostraba el dashboard. El 22-oct eran 16
partidos, uno del modelo con el error del xG. Ahora el pipeline los marca
(`side_bias.mark_skipped` → `skipped_at`) y un análisis nuevo lo limpia. Toda lectura de la
opinión del modelo filtra `skipped_at IS NULL` (`side_bias.is_skipped` en Python). El pick
queda "sin pick" con el motivo `STALE`.

**`matches` solo trae las ligas (22-oct-2026).** Se llena con football-data (ligas) y con los
resultados de The Odds API de las ligas que predecimos. Nunca llegaban la Europa League, la
Conference, las copas nacionales ni los amistosos, y de la Champions faltaban partidos. El
dueño lo vio al abrir Stuttgart–Gladbach ("te estás saltando partidos"): les faltaban la
DFB-Pokal y dos amistosos de la fecha FIFA.
- Ahora `src/features/espn_history.py` (paso "Historial ESPN" del evening, antes de
  Predictions) guarda esas competiciones en `espn_matches`: marcador, córners y tarjetas
  (roja = 1) del scoreboard público de ESPN, una petición por competición y mes.
  - La primera carga (3 meses, unos 7 min) dejó 1,302 partidos. Después solo lee el mes en
    curso de las ligas y dos meses de las copas.
- **Nombres.** El id de ESPN se empareja con el nombre de `matches` con los partidos de liga
  (misma fecha ±1 día, mismo marcador, el nombre más parecido; tabla `espn_teams`). Así un
  "Barcelona SC" no se confunde con el Barcelona.
- **Quién la usa:**
  - el partido abierto (los últimos 5) y Equipos, con la competición de cada partido. Un
    partido que ya está en `matches` no se repite (±1 día, mismo marcador, un equipo igual).
    Los amistosos se ven atenuados y no cuentan en récord ni promedios;
- **El modelo no la usa para la fuerza de los equipos** (forma, Elo, Dixon-Coles): un amistoso
  o un 4-0 de copa contra uno de cuarta no son su liga.
- **Sí la usan los días de descanso** (`fixture_congestion`, sin amistosos; 23-oct-2026).
  Antes el jueves de Europa League no contaba y el equipo llegaba "fresco" al domingo.
  - Cambian 52 equipos de los próximos partidos.
  - A/B en seco contra Pinnacle en los 39 partidos afectados: cambian 28 (1-2 pts), con
    mediana 5.7 → 5.7 y media 7.2 → 7.0 (16 mejoran, 11 empeoran). Los goles esperados
    pasan de 3.20 a 3.13.
  - Sin la tabla, usa solo las ligas, como antes.

**La base está en Oregon: cada consulta es un viaje de red (22-oct-2026).** Neon está en
`us-west-2`. Una consulta típica tarda 3.5 ms dentro del servidor y ~165 ms en total desde la
PC del dueño. Esa PC sale a internet por el exit node de Tailscale "GDL-VPN" (ping a 8.8.8.8:
97 ms), y una consulta de pandas son ~3 viajes (pedir la conexión, la consulta, el rollback).
Un ensayo del pipeline desde esa PC (miles de consultas) tarda ~1 h; en GitHub, ~7 min.
- `config/database.py` lleva `pool_recycle=240` (Neon corta conexiones inactivas) y
  keepalives de TCP, sin `pool_pre_ping`: agrega un viaje por consulta (+166 ms medidos).
- El 22-oct se agregó `pool_pre_ping` creyendo que los ensayos se colgaban en conexiones
  muertas. Eran lentos, no estaban colgados; se quitó el mismo día.

**Fechas: siempre con formato explícito (`src/utils/dates.py`, 23-oct-2026).**
`pd.to_datetime(x, dayfirst=True)` sin formato cae a dateutil elemento por elemento, y dateutil
con dayfirst lee "2025-02-06" como 2-jun. El cargador de FBref dejó así toda la historia
argentina con día ≤ 12. `fix_swapped_dates.py` (25-sep) solo corrigió las que caían en el
futuro; después `load_extra_leagues` cargó la historia buena y su chequeo de repetidos (±1 día)
no vio los espejos.
- Resultado: 2,271 partidos argentinos dos veces, uno con la fecha mala. La forma, Dixon-Coles,
  el Elo y el entre-ellos los contaban doble.
- Limpieza: `scripts/fix_argentina_swapped_dates.py`, con respaldo en `matches_swap_backup`.
  El árbitro es el propio `/new/ARG.csv`, no ESPN: ESPN no trae la Copa de la Liga, y
  Talleres–Sarmiento 1-0 se jugó dos veces en 2022.
- Los cargadores usan `parse_match_dates`, se borró `load_historical_fbref.py` y el chequeo
  diario avisa si vuelven los espejos o hay resultados con fecha futura.
- Verificado contra ESPN: en Brasil, Liga MX y MLS el cargador viejo dejó las fechas bien.

**Las otras fuentes, con nuestros nombres (23-oct-2026, parte 2).**
- **ESPN mal apuntado.** `ESPN_LEAGUES` mandaba la liga china a `chi.1`, que es CHILE: los
  resultados y las alineaciones "chinas" eran partidos chilenos. Ahora es `chn.1`. La K
  League 1 no está en ESPN (`kor.1` responde 400) y se quitó.
- **Alineaciones partidas.** 24 equipos tenían su historia de alineaciones en dos nombres
  ("houston dynamo fc", "talleres (cordoba)", "afc bournemouth", "brighton & hove
  albion"…).
  - `TEAM_NAME_MAP` trae 26 nombres de ESPN. Se migraron 129 filas de `lineups` y los
    nombres de `espn_matches`, con respaldo en `lineups_name_backup`.
  - Se borró el partido de estrellas MLS–Liga MX, y `parse_scoreboard` lo ignora.
- **La cadena de alias tiene dos pasos.** "red bull new york" debe apuntar a "new york red
  bulls": el segundo paso de `TEAM_ALIASES` lo deja en "ny red bulls". Apuntarlo directo a
  "ny red bulls" lo devolvía a "new york red bulls".
- **Goleadores que cambiaron de club.** Understat escribe "Chelsea,Manchester City"; cuenta
  el último.
- **Chequeo semanal `check_source_names`** (paso "Nombres de equipo y repetidos"): ERROR si un
  nombre de `team_xg_real`, `player_club_goals` o `lineups` (ligas que predecimos) no es
  ningún equipo de `matches`.
- **Comisión de los intercambios.** The Odds API da el momio de Betfair, Matchbook y Smarkets
  sin su comisión, y el "mejor momio" salía de ahí con un valor que nadie cobra.
  `update_upcoming_matches.net_of_commission` lo descuenta (Betfair 5%, Matchbook y
  Smarkets 2%) antes del mejor momio y del promedio.
- **Descargas de football-data**: un fallo de red se reintenta a los 10 y 30 s
  (`fetch_csv`). El weekly del 19-oct perdió 10 descargas por timeouts.
- **Selecciones**: "Czech Republic" 2-2 y "Czechia" 1-1 vs Dinamarca el 31-mar-26 eran el mismo
  partido. Se dejó el 1-1 (90 minutos); el otro está en `matches_swap_backup`.

**"Enrich data" se quitó (23-oct-2026).** Escribía en `upcoming_matches` promedios de tiros y
córners que nadie lee, en una sola transacción de ~70 s. Chocaba con el closing:
`DeadlockDetected` en 20 partidos de la mañana del 23-oct, con el paso en "OK".

**Un error que solo se imprime no existe (23-oct-2026).** `print("❌ …")` no llega a Telegram.
- Pasaron a `log.error`: el bankroll (que además devolvía 100 como saldo si fallaba), el
  registro del gasto de Anthropic, las descargas de goleadores y selecciones, el holdout y el
  monitor de mercado.

**Lo que entra al modelo se vigila (23-oct-2026).**
- `output_checks.feature_coverage`, al final de cada corrida: ERROR si H2H llega a menos del
  25% de los partidos evaluados, o el xG real a menos del 80% en las 5 grandes.
- **xG, córners y tiros se miden solo sobre las ligas que traen tiros (27-oct-2026).**
  `xg_proxy.leagues_with_shots` = las ligas con tiros en `matches` el último año (las 19 de
  football-data). Brasil, MLS, Suecia, Liga MX, Argentina… nunca los traen.
  - Medido sobre todos los partidos, la noche del 27-oct marcó "xG en 10 de 42": 32 eran de
    esas ligas, y los 10 de Serie A tenían todo.
  - Ahora: ERROR si el xG llega a menos del 60% de los partidos de esas ligas (`with_shots`),
    o córners o tiros a menos del 50%. Desde agosto: xG 98% (la peor semana 90%), córners 93%
    (la peor 82%). No se juzga con menos de 10 partidos de esas ligas.
  - Si la consulta de las ligas falla es ERROR ese mismo día. Si en el estado guardado hay
    menos partidos de ligas con tiros que de las 5 grandes, también: el conjunto se rompió.
- `output_checks.data_health`, diario: fechas espejo, resultados con fecha futura, partidos
  repetidos en 120 días, y que cada fuente esté al día (`FEED_MAX_AGE_DAYS`):
  - `upcoming_matches`, `odds_history` y `program_picks`: 1 día;
  - `model_vs_sharp` y `kalshi_history`: 2 días;
  - `espn_matches`: 3 días;
  - `team_xg_real`: 9 días;
  - `club_elo`: 10 días.

  Una tabla que todavía no existe (instalación nueva) no cuenta.

**Nombres de otra fuente: siempre por `normalize_team` (23-oct-2026).** `team_xg_real` guarda el
nombre de Understat ("Bayern Munich", "VfB Stuttgart") y `get_real_xg` lo comparaba tal cual con
el canónico ("bayern munchen", "stuttgart"). 24 de los 96 equipos de las 5 grandes (Bayern,
Atlético, Inter, Leipzig, Newcastle…) caían sin aviso al xG estimado.
- Ahora se comparan los dos normalizados, y `TEAM_NAME_MAP` tiene los 5 nombres de Understat
  que no se normalizaban (Gladbach, Mainz, Leipzig, Parma, Racing Santander → "real racing club
  de santander"; "racing" a secas es Racing Club de Argentina). Con eso, 96 de 96.

**La motivación contaba una sola vuelta (23-oct-2026).** `motivation_factor` calculaba las
jornadas restantes como equipos − 1 − jugados. En las ligas de ida y vuelta el "final de
temporada" empezaba en la jornada 11 (título +5%, Europa +4%, descenso +6%, partidos "sin nada
en juego" que el pipeline salta). Desde la jornada 19, el líder quedaba "campeón" (−8%) y la
zona de descenso "descendida" (−8%).
- Ahora hay `SEASON_GAMES` por liga, y `current_season_start` empieza en enero en Brasil (antes
  julio: la tabla traía la mitad de los partidos y 16 de 20 equipos tenían ajuste) y en
  enero/julio en Liga MX.
- Argentina sale del factor: 30 equipos en dos zonas; una tabla de 30 no dice quién pelea qué.

**Liquidar córners y tiros 0-0 (23-oct-2026).** `resolve_market` tomaba un 0-0 como dato, y en
Brasil, Liga MX y Argentina (sin córners ni tiros en football-data) un "menos de" salía ganado.
Ahora es "unresolved" (`_has_pair`), igual que el chequeo independiente. No había ninguna
apuesta así liquidada.

**Un NaN no falla: se propaga (22-oct-2026).**
- **Elo.** `compute_elo` leía también los partidos sin marcador: K = NaN contagiaba a los dos
  equipos y luego a cada rival. Dos partidos (Grecia 2019, Ligue 2 dic-2025) dejaron 107
  equipos en NaN: toda la Ligue 1 y Grecia, 34 de 40 en LaLiga, 32 de 36 en Champions. El
  ensemble descartaba el Elo sin avisar.
  - Ahora solo cuenta partidos con marcador, y un Elo NaN es ERROR a Telegram.
  - Con el arreglo, en los 35 partidos que cambiaban, el 1X2 quedó más cerca de Pinnacle:
    mediana 7.1 → 5.5 pts.
- **Córners.** Los archivos `/new/` de football-data (Brasil, Liga MX, Argentina) no traen
  córners y el loader los guardó como 0-0. `props_fair` veía ligas de 0 córners ("más de 9.5"
  = 0%); ahora 0-0 es sin dato. `corners_stats` ya filtraba los ceros.

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
| `side_bias` | `side_bias.run_side_bias` | **sesgo del modelo** (por lado, por región, en más de 2.5 y ambos anotan) que se descuenta antes del ancla |
| `shadow_reactivation` | `clv_gate.run_shadow_reactivation` | `away_win`, AH con local favorito y grupos `sinref:*` |
| `shade_scales` | `shade_learner.run_shade_learning` | **escala de los ajustes manuales** (FLB, tabla, empates) |
| `rule_evidence` | `rule_evidence.run_rule_evidence` | veredicto de cada regla contra resultados (reporte; los filtros lo usan abajo) |
| `filter_switches` | `filter_learner.run_filter_learning` | filtros de selección prendidos/apagados (entre semana, rango de cuota, ligas difíciles) |
| `timing_windows` | `timing_learner.run_timing_learning` | horarios antes del kickoff sin dinero real |
| `edge_finder` | `edge_finder.run_edge_finder` | solo reporte: qué ideas le ganan al momio histórico en los tres periodos |
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
CLV ≥ 0 con n ≥ 30; se re-bloquean con CLV significativamente negativo. Con el mismo
criterio vuelven los mercados **sin referencia de Pinnacle** (grupos `sinref:<familia>`:
btts, córners/tarjetas, medio tiempo, goles sin línea...): cuentan sus candidatas que
no tenían `pin_prob`, solo desde el 25-sep-2026 03:00 UTC (`PIN_RECORDING_SINCE`:
antes el vacío significaba "no se guardaba", no "Pinnacle no cotiza").

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
  n < 100 o < 30 partidos.
- **Filtros que se apagan solos** (`filter_learner`, clave `filter_switches`, desde el
  19-oct-2026, a pedido del dueño). Los tres filtros afirman que su grupo es peor. Cada
  uno se **apaga** solo si los resultados lo contradicen con fuerza:
  - qué cuenta como evidencia fuerte: n ≥ 100 en ≥ 30 partidos en cada grupo, y el IC
    entero de la diferencia de brechas por debajo de 0 (su grupo acierta mejor que el
    resto);
  - cuándo vuelve: apagado, regresa cuando la media deja de ser negativa;
  - con pocos datos, o si el estado no se lee, quedan prendidos.

  Nunca agrega ni endurece un filtro. El filtro Pinnacle sigue igual. Al 12-oct ninguno
  tenía datos suficientes (ligas difíciles 57-63, entre semana 31-36 apostables).
  Telegram solo cuando uno cambia.

Llega a Telegram en el weekly solo si algún veredicto ya tiene datos suficientes o si
la escala se movió. Push y medias (AH de cuarto, DNB con empate) cuentan en el ROI pero
no en Brier ni calibración. Las candidatas sin datos del partido a los 10 días quedan
`result = 'stale'`.

### Referencia Pinnacle (¿hay ventaja real?)

Desde el 24-sep-2026 el sistema se mide también contra **Pinnacle**, la casa de
referencia de los profesionales. Su precio sin margen es el mejor estimador público de
la probabilidad real, y su cierre es el patrón estándar para saber si alguien tiene
ventaja. Viene en la misma descarga (región `eu`), sin créditos extra. Desde el
25-sep-2026 además **decide qué lleva dinero real** (modo recolección, abajo) y corrige
el sesgo por lado del modelo; en las probabilidades del ancla y en los stakes no entra.

- **Precios** (`src/features/pinnacle.py`): cada fetch guarda en `upcoming_matches`
  las cuotas de Pinnacle de 1X2 y de su línea principal de goles (`pin_*`). No usa
  COALESCE: un fetch con hora escribe el precio tal cual (NULL si ya no cotiza), para que
  un precio viejo nunca quede con hora nueva. Se derivan exactamente del 1X2: empate
  anulado (P(local | no empate), la misma definición del modelo), hándicap ±0.5 y doble
  oportunidad. Más/menos 2.5: directo si Pinnacle cotiza 2.5 (solo ~14% de los partidos);
  si no, derivado de su línea principal (2.25, 2.75, 3…) despejando el λ de Poisson que
  hace justa su cuota, con devolución en líneas enteras y mitad y mitad en las de cuarto
  (`over25_from_line`). El resto de mercados queda sin referencia.
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

### Modo recolección: el filtro Pinnacle (25-sep-2026)

La auditoría del 25-sep midió que 7 de 11 apuestas pendientes tenían valor **negativo**
contra Pinnacle aun a la mejor cuota europea, y el simulacro de revalidación de ese día
no dejó en pie ninguna de las 14 pendientes. El modelo todavía no demuestra ventaja
sobre Pinnacle, así que el dinero real solo va donde Pinnacle confirma el valor, y todo
lo demás se sigue midiendo en sombra:

- **Pipeline** (`_stage_select_bets`): una apuesta real exige
  `p_pinnacle − 1/cuota ≥ 2 pt` (`SHARP_MIN_EDGE` = `MIN_EDGE_TO_PLACE`). Sin precio de
  Pinnacle solo pasa si su grupo `sinref:<familia>` ya fue reactivado con evidencia
  (§7, reactivación). Las candidatas shadow no cambian: se registran igual, con su
  `pin_prob`. El resumen de la corrida imprime `🔒 Filtro Pinnacle: N sin valor · M sin
  referencia`.
- **Revalidación pre-kickoff** (`scripts/revalidate_pending_bets.py`, cada closing):
  la misma regla a la cuota fresca, con el precio fresco de Pinnacle (o, si la fila ya
  no lo trae, el guardado con la bet). Cancela con `cancelled_no_value_pinnacle` o
  `cancelled_no_reference`; Telegram dice "canceladas: sin valor real frente a
  Pinnacle (no las apuestes)" y la confirmación pre-kickoff ya no sale.
- **Días sin picks son lo esperado.** Los mensajes lo dicen: "Sin apuestas con valor
  real frente a Pinnacle. No apuestes nada: el sistema sigue recolectando datos en
  sombra." Si el modelo demuestra ventaja (referencia Pinnacle con veredicto, peso del
  modelo al alza), el filtro se puede relajar; es una decisión del dueño.

### Horario de apuesta (`src/models/timing_learner.py`, 19-oct-2026)

El filtro Pinnacle compara con el precio de Pinnacle **de ese momento**, pero Pinnacle se
sigue moviendo hasta el kickoff (13-19 oct: 2.4 pt de media con más de 48 h, 1.2 pt en
las últimas 6 h). El weekly mide, por horario (menos de 12 h, 12-24, 24-48, más de 48 h
antes del kickoff), cuánto valían al cierre de Pinnacle las cuotas que pasaban el
filtro, y bloquea para dinero real el horario que pierde.

- **Datos:** `odds_history` en 1X2 y más/menos 2.5, desde el 13-oct-2026, cuando se le
  sumaron Pinnacle y la mejor cuota. El cierre es la última foto de los 150 min previos
  al kickoff; sin ella el partido no cuenta.
- **Señal:** una por partido, mercado y horario: la primera foto del horario que pasaba
  el filtro. Su valor es `p_pinnacle_cierre × cuota − 1`.
- **Política:** la de siempre (n ≥ 100 en 30 partidos, IC 95% por partido). Se bloquea
  si el IC entero es negativo; vuelve cuando la media deja de ser negativa. Con pocos
  datos no se bloquea nada.
- **Pipeline:** la apuesta que ya pasó el filtro Pinnacle en un horario bloqueado queda
  como propuesta con el motivo "a esta hora la ventaja no aguanta al cierre". Se vuelve
  a evaluar en la corrida siguiente (morning 12:00 UTC, evening 03:00 UTC). Rige en
  todos los mercados con precio de Pinnacle; los demás no pasan por aquí.
- **Ritmo:** la primera semana hubo 19 señales en 13 partidos (11 con más de 48 h). Con
  ese ritmo, el horario de más de 48 h tiene veredicto en ~2 meses; los demás tardan
  más. Contar cada foto como señal daba cientos por semana, pero repetía la misma
  cuota.
- **Telegram:** si un horario se bloquea o vuelve, o si alguno ya tiene veredicto.

### Momios históricos y buscador de ventajas (19-oct-2026)

Hasta esta fecha la base tenía 94k partidos con resultado y **ningún momio**. El modelo
nunca se había comparado con el mercado en años de datos, solo en las semanas de sombra.
Los mismos CSV de football-data que descarga el weekly traen el cierre de Pinnacle (desde
2012-13), el promedio y la mejor cuota entre casas, más/menos 2.5 y hándicap. Se tiraban.

**Tabla `historical_odds`** (`scripts/load_historical_odds.py`, paso "Momios históricos"):
- **Qué guarda:** una fila por partido con goles, tiros a puerta, córners y los momios.
  Aparte de `matches`: es la base de investigación y no toca nada de lo que ya funciona.
- **Qué precio guarda:** el de cierre cuando existe (`pin_closing`, `avg_closing`). Si no, el
  de apertura, o el promedio Betbrain en las temporadas viejas.
- **Ligas:**
  - las 12 de `SEASON_LEAGUES`;
  - las segundas divisiones que ofrece PlayDoit (League One y Two, 2. Bundesliga, Serie B,
    Segunda, Ligue 2);
  - los archivos `/new/` de México, EE. UU., Brasil, Argentina, Japón, Noruega, Suecia y
    China.
- **Qué descarga el weekly:** la temporada en curso y las que falten. Una descarga fallida se
  reintenta sola la semana siguiente.
- **Carga completa:** `python scripts/load_historical_odds.py --backfill`.
- **Si una fila ya existe:** se completan los momios que faltaban (`fill_null`) y nunca se
  pisa un dato existente.

**Buscador de ventajas** (`src/models/edge_finder.py`, paso "Buscador de ventajas", clave
`edge_finder`):
- **Qué prueba:** más de 30 ideas en palabras del dueño (racha, goles, descanso, recién
  ascendidos, suerte, resultado anterior, pelea por el descenso y sesgos del momio) contra
  esos momios.
- **Precio:** el promedio entre casas, porque PlayDoit paga casi igual. Medido el 19-oct en la
  Bundesliga: PlayDoit −2.2 pt contra el precio justo de Pinnacle, promedio −2.4.
- **Sin mirar el futuro:** cada rasgo usa solo partidos anteriores de la misma liga y
  temporada.
- **Veredicto:**
  - **pasa** si gana en 2012-18, en 2019-22 y en 2023 en adelante (≥ 50 apuestas en cada
    periodo), y la ganancia junta no puede ser suerte aun probando todas las ideas
    (Bonferroni);
  - **prometedora** si gana en los tres, pero todavía puede ser suerte.
- **Por qué tanto rigor:** "el recién ascendido de local pierde" ganaba +10% y +15% en
  2019-25 y perdió −11% en 2012-19.
- **Solo mide.** Una idea que pase va primero al laboratorio y después al dinero real.
- **Telegram:** solo cuando cambia lo que pasa o lo prometedor.
- **Primera corrida (20-oct-2026):** 132,218 partidos de 26 ligas, 2012-2026. Ninguna de las
  32 ideas pasó y casi todas pierden lo que cobra la casa (−4% a −8%).
  - Lo menos malo: el local muy favorito (+0.4%), sin repetirse en los tres periodos.
  - Lo peor: el visitante con 15% o menos (−24%), por el sesgo favorito-longshot.
  - "Local en racha" gana 59% cuando la casa esperaba 58%: −4.8% a precio común.
  - Apostar temprano no cambia nada: la casa común arriba de Pinnacle en el mismo momento
    salió en 181 de 10,706 partidos y valía −4.8% contra el cierre.
- **Equipos goleables (26-oct-2026).** El dueño: "si un equipo es propenso a recibir muchos
  goles, que también pienses en un pick de goles". Cinco ideas nuevas con los goles a favor y en
  contra de los últimos 5 (`{h,a}_gf5` / `{h,a}_ga5`, la suma; 10 = 2 por partido). Sobre
  136,257 partidos, al precio promedio:
  - alguno recibe 2+ → más de 2.5: n=22,512, sale 50.8% y el mercado esperaba 52.3%: −7.5%
    (Pinnacle −5.7%), peor que apostar el más de 2.5 siempre (−5.3%);
  - local que recibe 2+: −8.1%; visitante que recibe 2+: −7.1%; visitante goleable contra local
    goleador: −8.3%; los dos reciben 2+: −8.5%.
  - Al revés (alguno recibe 2+ → menos de 2.5): sale 1.4 pt más de lo que espera el mercado, pero
    igual pierde −4.4% (Pinnacle −2.0%). **El mercado exagera los goles de una defensa que
    recibe mucho**, y no alcanza para ganarle al margen.
  - Quedan en el buscador semanal ("alguno_goleable" sale en el reporte de Telegram) y en el
    detalle de cada partido (el ángulo de goles, v2.4.7).
- **Ojo con las temporadas:** los códigos de mmz4281 se leen con `code_start` ('1920' →
  2019). El primer backfill guardó '1920' y '2021' como años y se corrigió en la tabla.

### Córners y tarjetas calibrados (`src/models/props_fair.py`, 20-oct-2026)

**El modelo de córners del pipeline exagera.** Medido sobre 25,046 partidos de 2022 en
adelante (18 ligas): cuando dice 15% de "más de 9.5", salen 42%; cuando dice 85%, salen 60%.
Acierta menos que usar solo el promedio de la liga (pérdida logarítmica 0.7119 contra
0.6898). Tiene tres fallas:
- usa un promedio europeo fijo (5 córners por equipo);
- le cree del todo a los últimos 20 partidos de cada equipo;
- supone Poisson.

En el pipeline casi no pesa, porque los córners van anclados al mercado con 2% de peso del
modelo. Se reemplaza fuera del pipeline, en `props_fair`:
- **base:** el promedio de local y de visitante de la liga en el último año;
- **equipos:** su desvío, encogido y creído solo en parte; ALPHA ajustado en 2012-21: 0.3
  en córners y 0.4 en tarjetas;
- **distribución:** binomial negativa en córners (k = 48) y Poisson en tarjetas.

Resultado en 2022+:
- **córners:** 0.6487 contra 0.6515 del promedio de liga;
- **tarjetas:** 0.6196 contra 0.6273;
- **partidos que se apartan 4 pt o más de su liga** (30% en córners, 50% en tarjetas):
  - córners: el modelo dice 56%, la liga diría 50%, salen 55.4%;
  - tarjetas: el modelo dice 59%, la liga 52%, salen 59.8%;
  - se repite igual en cada temporada.

**Que esté calibrado no prueba que le gane a una casa:** solo que sabe más que un precio
"de promedio de liga". Contra la mejor cuota europea (candidatas sombra, 20-oct):
- 95 apuestas, 55G/39P, +4.31u;
- acierto 59%, cuando la cuota daba 55% y el modelo 63%;
- todavía sin veredicto.

El laboratorio lo mide cada semana con resultados, porque no hay un cierre sin margen de
referencia (sección "Córners y tarjetas"). Mandarle al dueño el momio mínimo de córners y
tarjetas para PlayDoit espera a que salga ✅.

**Datos:**
- **Lectura:** una sola consulta (`load_history`) y todo en memoria. Antes, una consulta
  por equipo tardaba 7 min por 122 partidos.
- **Nombre del equipo:** se lee `home_team` (canónico, en minúsculas). `team_home_norm`
  quedó vacío en los partidos recientes de `matches`, y solo lo usan dos scripts viejos de
  limpieza.
- **Tarjetas:** `matches` solo las tiene completas desde 2025-26 (Premier: 616 de 4,250
  partidos desde 2015). Alcanza para los últimos 20 partidos de cada equipo. Para
  investigar el pasado se usan los CSV.

### Alineaciones: el momio final ya las cuenta (20-oct-2026)

Se probaron las alineaciones de ESPN (API pública, titulares, formación y goles por
jugador) en 4,389 partidos de 2024-26 de 7 ligas, contra el momio de cierre:
- **Al local le faltan 2+ titulares habituales** (70%+ de titularidades en la
  temporada): gana 42.5% y el cierre esperaba 45.5%. Apostar al visitante en esos casos da
  −2.4% a precio promedio: el margen se come los 3 pt.
- **Sin su goleador:** el cierre ya lo cuenta.

Lo único que podría quedar es el tiempo: los minutos entre que sale la alineación y una
casa como PlayDoit ajusta. No se puede medir con historia, solo en vivo.

**Alertas de alineación** (`src/features/espn_lineups.py` + `src/models/lineup_alerts.py`,
20-oct-2026):
- **Historial** (weekly, paso "Alineaciones (ESPN)"):
  - Guarda en la tabla `lineups` los titulares y los goles de los partidos terminados de los
    últimos 10 días, en las 24 ligas de `ESPN_LEAGUES`. La carga inicial cubrió 120 días.
  - Los nombres son los de `matches` (`system_names`: misma fecha ±1 día y parecido de
    nombres), para que el historial se encuentre con el nombre de `upcoming_matches`.
- **En vivo** (closing, cada 30 min; desde v2.5.0 en dos pasos: "Alineaciones publicadas"
  guarda el XI con `capture_upcoming`, luego se recalcula el pick y "Alertas de alineación"
  corre `run_lineup_alerts(capture=False)` con lo guardado):
  - Para los partidos que arrancan en 5-80 min, lee la alineación publicada.
  - **Titular habitual:** 70%+ de las titularidades en los últimos 15 partidos terminados,
    con al menos 6.
  - **Goleador:** el que más goles lleva en esos partidos, con 3 o más.
  - **Cuándo avisa:** si faltan 4+ titulares habituales, una rotación grande. Manda a
    Telegram el momio mínimo que debe pagar PlayDoit por el rival y por "empate o rival".
  - **Por qué 4:** contra el cierre, el rival gana más de lo esperado cuanto más faltan
    (2 fuera +0.8 pt, 3 +1.9, 4 +2.6, 5+ +8.2 con n=101). Con 2 saldría en la mitad de los
    partidos: es rotación normal. Con 4 sale en el 4%, unas 10 alertas en un fin de
    semana completo.
  - **Longshots:** "el rival gana" solo sale si Pinnacle le da 20% o más (con 15% o menos,
    el buscador de ventajas mide −24%); "empate o rival" sale siempre.
  - **Momio mínimo de 1.40** (`MIN_ALERT_ODDS`, 24-oct-2026), el mismo piso de los picks: un
    renglón por debajo no sale ("empate o AEK a −1579" no es una apuesta). Si no queda
    ninguno, no hay aviso.
  - **El goleador no avisa:** cuando falta, la casa exagera (el rival ganó 34.4% contra
    36.0% esperado). Solo se menciona en el mensaje.
  - **Cómo calcula el mínimo:** con el precio de Pinnacle de `upcoming_matches` y 2 pt de
    margen (`min_american`).
  - **Si no hay precio fresco:** con más de 12 h no sale alerta, y el mensaje dice la
    antigüedad del precio.
  - Una alerta por equipo, partido y mercado (`lineup_alerts`, `UNIQUE`). Una falla es
    ERROR.
  - **Un mensaje por partido (23-oct-2026, a pedido del dueño):** llegaban dos, uno por
    equipo, largos. Ahora es uno corto (`format_match`):
    - quién falta en cada equipo (⚽ = su goleador; "completo" si no falta nadie);
    - el momio mínimo del rival;
    - la revisión de Claude, una por partido;
    - el **pick del programa** (el del dashboard, `program_picks`) con cuándo sí apostarlo
      (`pick_line`):
      - si pasa la regla 🎯: "apuesta solo si PlayDoit paga X o más";
      - si no la pasa: "no lo apuestes" y el motivo;
      - si el pick es el equipo debilitado y Claude no descartó las bajas: no lo apuestes.
  - **Emparejamiento estricto con ESPN (23-oct-2026).** El de Kalshi sumaba el parecido de
    los dos nombres, y una palabra larga compartida contaba como nombre igual. Así, la
    alineación de Gimnasia Mendoza vs Platense se guardó también como la de Gimnasia LP vs
    Sarmiento, y llegaron avisos de 8 y 9 titulares fuera que no existían. En el historial
    había 9 partidos de Argentina de ago-sep con el equipo equivocado.
    - Ahora cada nombre tiene que parecerse por separado (`MIN_SIDE_SCORE` 0.75).
    - Las palabras que comparten clubes distintos no bastan (`GENERIC_WORDS`: central,
      independiente, gimnasia…).
    - Cada partido de ESPN va a un solo partido del sistema y viceversa (`assign_events`).
    - Medido con 2,129 partidos terminados de jun-oct: empareja igual que el viejo. Hicieron
      falta 15 alias de ESPN (Deportivo, Internazionale, Sporting CP, Heart of Midlothian…).
  - **Alineación ajena:** si el XI comparte menos de 5 jugadores con la plantilla de la
    historia del equipo (`KNOWN_MIN`), no es la suya: no hay aviso, solo un warning.
  - **Partido recién jugado sin resultado en `matches`:** se reintenta en la próxima corrida
    (`UNNAMED_RETRY_DAYS`), en vez de guardarlo con el nombre de ESPN.
  - **Limpieza del 23-oct** (`scripts/fix_lineup_identity.py --apply`; respaldo en
    `lineups_identity_backup` y `lineup_alerts_backup`):
    - 11 alineaciones ajenas a su equipo, re-descargadas con el emparejamiento nuevo;
    - los 7 avisos falsos de Gimnasia Mendoza–Platense y Gimnasia LP–Sarmiento;
    - 142 alineaciones de Chile guardadas como China (de cuando el código apuntaba a
      `chi.1`);
    - 144 filas con un nombre de ESPN que ahora tiene alias.
  - **Lo vigila la auditoría del sistema** (chequeo "Alineaciones de ESPN",
    `identity_problems`): un partido de ESPN guardado con más de dos equipos, o alineaciones
    que no comparten jugadores con su equipo.
- **Medición** (weekly, paso "Referencia Pinnacle", `resolve_alerts`):
  - **Precio de cierre:** la última foto de `odds_history` en los 150 min previos.
  - **Resultado:** de `matches`.
  - **Reporte:** el mensaje semanal suma "Alertas de alineación": cuánto valía el momio
    mínimo contra el cierre de Pinnacle y cómo terminaron.
  - Si en semanas el valor contra el cierre no es positivo, PlayDoit no tarda lo
    suficiente y las alertas no sirven.
- **Costo:** gratis. En el closing son 1-3 consultas a ESPN por liga con partidos en la
  ventana, más una por partido.

**Revisión con Claude** (`src/models/lineup_review.py`, 20-oct-2026). El dueño la aprobó con
tope de **$0.50 por semana**.
- **Qué hace:** antes de mandar cada alerta, Claude Haiku 5.5 (`claude-haiku-5-5`) busca
  noticias con hasta 2 búsquedas web (`web_search_20250305`; Haiku no tiene la versión con
  filtrado dinámico).
- **Qué responde:** en JSON, si las bajas son lesión o sanción (`CONFIRMA`), descanso
  planeado con suplentes de nivel (`DESCARTA`) o falta de información (`DUDA`).
- **Dónde queda:** el veredicto va en el mensaje ("Claude: confirma las bajas / no apuestes al
  rival", una revisión por partido desde el 23-oct) y en `lineup_alerts` (`analyst_*`).
- **Costo:** ~2-3 centavos por revisión, casi todo en búsquedas ($0.01 cada una).
- **Topes:**
  - el semanal (`LINEUP_REVIEW_WEEKLY_USD`) se cuenta en `lineup_alerts.analyst_cost` de los
    últimos 7 días;
  - el diario de la cuenta es `anthropic_budget.can_call`;
  - sin tope, sin clave o con error, la alerta sale igual, sin revisión. Una falla de Claude
    es ERROR.
- **Clave:** `closing.yml` recibe `ANTHROPIC_API_KEY` desde el 20-oct.
- **Medición:** el reporte semanal separa las alertas por veredicto (confirmó / sin info /
  descartó) y suma el gasto. Si las que Claude confirma no rinden más que las que descarta,
  la revisión no vale lo que cuesta y se apaga.

### Sesgo por lado (`src/models/side_bias.py`)

Medido el 25-sep: el modelo daba en promedio +5.1 pt al local, −2.2 al empate y −3.3 al
visitante frente a Pinnacle. Pinnacle no tiene sesgo por lado, así que es un error
sistemático del modelo. Cada corrida guarda, en **cada** partido con precio de Pinnacle
(no solo en las candidatas, que sesgarían la muestra), el 1X2 **crudo** del modelo y el
de Pinnacle (tabla `model_vs_sharp`, una fila por partido). El weekly estima el sesgo
medio por lado en los últimos 21 días (`WINDOW_DAYS`), lo encoge hacia 0
(`n/(n+60)`), limita el cambio a ±3 pt por semana y el total a ±8 pt, y con < 30
partidos conserva el anterior (arranca en 0). El pipeline lo descuenta del 1X2 del
modelo y de lo que se deriva de él (doble oportunidad, empate anulado, hándicap) antes
del ancla; el `decision_log.model.side_bias` guarda el cambio aplicado. Telegram solo
cuando la corrección se mueve.

- **El pick y el dashboard usan el 1X2 corregido** (`program_picks.model_1x2`, 22-oct-2026).
  Antes usaban el crudo de `model_vs_sharp`, que se guarda así para aprender el sesgo.
- **Recalculado a mano el 22-oct** con el modelo corregido (Elo arreglado): 141 partidos con
  Pinnacle, `learn()` normal. Medido local +1.6 / empate −0.8 / visita −0.8 pts → corrección
  +1.1 / −0.5 / −0.6. La de antes, +3.0 / −1.8 / −1.2, venía del modelo con el error y
  sobrecorregía; respaldo en `model_state` `side_bias_backup_1019`.
**Por región y en goles (23-oct-2026).** Con el modelo del 22-oct (142 partidos) el sesgo no
era parejo y una sola corrección global (+1.1 al local) no servía:

| | local vs Pinnacle | más de 2.5 vs promedio de las casas | ambos anotan vs mejor precio |
|---|---|---|---|
| 5 grandes | +3.9 ± 1.1 | −0.1 ± 1.1 | −1.8 ± 1.1 |
| Resto de Europa | +1.3 ± 0.9 | −0.4 ± 0.9 | −1.9 ± 1.1 |
| América | −0.6 ± 1.2 | −3.8 ± 1.3 | −4.2 ± 1.3 |
| Todo | +1.5 ± 0.6 | −1.3 ± 0.6 | −2.7 ± 0.7 |

Con el modelo bajo en goles, los picks se iban al "menos" y al "no anotan ambos" sin razón.
- **Regiones** (`region_of`): 5 grandes (`BIG5`, la misma lista del pipeline), América (MLS,
  México, Brasil, Argentina, Conmebol, Concacaf), Asia (Japón, Corea, China, AFC) y Europa
  (el resto, con las copas de la UEFA).
- **Cómo aprende una región:** se separa de lo global en (su media − la global)·n/(n + 60)
  (`REGION_SHRINK_N`); con menos de 15 partidos (`MIN_N_REGION`) usa lo global. Su paso
  semanal parte de su valor anterior o, la primera vez, del global que se le venía aplicando
  (±3 pt por semana, como siempre).
- **Goles:** más de 2.5 y ambos anotan se aprenden igual (encogido, ±3 pt por semana, tope
  ±6 pt `GOAL_MAX_ABS`), contra la **misma referencia de los picks**: promedio de las casas
  sin margen en goles (si no hay, el mejor precio) y el mejor precio sin margen en ambos
  anotan (Pinnacle no lo cotiza). Cada corrida la guarda en `model_vs_sharp.mkt_over25` y
  `mkt_btts` (`market_goal_refs`). Los partidos analizados desde el 22-oct la tomaron de su
  foto de momios con `scripts/backfill_goal_refs.py --apply` (una vez, solo llena vacíos).
- **Dónde se aplica:** el pipeline descuenta el sesgo de la región del partido
  (`bias_for` + `debias`): 1X2 y derivados como antes, y además más/menos de 2.5 y ambos
  anotan / no anotan ambos, antes del ancla. Las líneas 1.5 y 3.5 del pipeline siguen con el
  Poisson crudo; en los picks salen de la matriz calibrada al 2.5 ya corregido.
- **Todo lo que lee el modelo guardado usa el corregido** (`side_bias.program_probs`): el pick,
  el dashboard y el revisor de capturas de PlayDoit. El revisor usaba el crudo hasta el
  23-oct.
- **Primera corrida (23-oct):** desde la global +1.1 / −0.5 / −0.6 → 5 grandes +2.1 / −0.8 /
  −1.4, resto de Europa +1.0 / −0.6 / −0.4, América +0.4 / −0.3 / −0.2; más de 2.5 −1.0
  (América −2.1), ambos anotan −1.7 (América −2.3). Fuera de muestra (aprende con 82
  partidos, mide en otros 66): el local en las 5 grandes queda a +0.3 de Pinnacle (antes
  +1.3) y ambos anotan a −0.7 (antes −2.7); el resto, parejo.
- **Lo vigila la auditoría del sistema** (chequeo "Sesgo del modelo", §3): lo que queda después
  de corregir, global y por región, y que el weekly lo siga aprendiendo.

### Fuerza de liga (`src/features/clubelo.py`, 22-oct-2026)

El programa mide a cada equipo dentro de su liga. En Champions, Europa League y Conference
eso no alcanza: el 21-oct le dio a Viking 20% contra el Aston Villa (Pinnacle 10%) porque
mete 2.3 por partido en Noruega.

- **Datos.** `refresh_if_stale()` corre en el morning (paso "ClubElo (fuerza de liga)",
  antes de Predictions) y, si la última foto tiene más de 6 días, lee las páginas públicas
  de ClubElo de 53 países (tabla `club_elo`, una foto por fecha). Un país que falla se salta;
  ninguno → ERROR a Telegram. Una foto de más de 10 días no se usa (`MAX_AGE_DAYS`).
  - La API de datos de ClubElo responde 502 desde el 22-oct-26 y la de partidos dice
    "Fixtures API deactivated": por eso las páginas. No tienen verificación de bots.
- **Fuerza de la liga** = Elo promedio de sus 5 mejores clubes (`league_strengths`; con menos
  de 3 clubes en ClubElo la liga no tiene fuerza). Foto del 22-oct: Inglaterra 1965, España
  1912, Alemania 1891, Italia 1860, Francia 1830 … Noruega 1642, Ucrania 1583.
- **Ajuste** (`league_factor`, solo partidos entre ligas, `prediction_pipeline._stage_lambdas`):
  el reparto f que cuadra un partido promedio con la diferencia de fuerza de las dos ligas
  (sin ventaja local: la de cada equipo ya está en sus goles), elevado a `GAMMA` = 0.2.
  Goles esperados λ local × f, λ visita / f; la forma del ensemble con el mismo reparto y su
  Elo con 0.2 de la diferencia de fuerza. Misma liga: f = 1.
- **Por qué la liga y amortiguado.** En los 5 partidos de copas con goles esperados del
  programa y Pinnacle, distancia del Dixon-Coles a Pinnacle: 5.2 pts sin ajuste; 5.6 con el
  Elo de cada club; 12.3 con la diferencia de ligas completa (se pasa); 1.6 con el 20%
  (media 5.7 → 2.9). En 24 partidos el Elo de ClubElo solo quedó a 6.3 pts de Pinnacle y el
  programa a 13.3.
- **Provisional.** Cinco partidos es poco. `model_vs_sharp.league_factor` guarda el factor de
  cada partido ajustado (NULL = sin ajuste): con más partidos de copas se recalibra `GAMMA`
  contra Pinnacle.
- **Nombres.** `match_team` empareja por palabras (sin "fc", "fk"…) y solo con un candidato
  único; lo que no alcanza va en `SLUG_ALIAS` ("inter milan" también tiene "milan"; "bod/glimt"
  perdió la ø). Equipo sin club o liga sin fuerza: el partido va sin ajuste y sin pick.

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

Otro fichero de parámetros calibrados que el código lee en runtime:
`config/ensemble_weights.json` (`src/models/ensemble_model.py`, que cae a pesos por
defecto **en silencio** si falta). `config/thresholds.json` y su optimizador semanal se
quitaron el 20-oct-26: nadie leía ese archivo, y en GitHub Actions se escribía en una
máquina que se borra al terminar.

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
python scripts/resolve_pending_bets.py --include-stale   # una vez: suma las reales sin fuente

# Salud y auditoría (sin gasto)
python scripts/watchdog.py
python scripts/heartbeat_dispatch.py             # lo corre el closing; necesita gh y GH_TOKEN
python scripts/learn_team_aliases.py             # en seco: nombres del mismo club; --write reescribe el mapa
python scripts/learn_team_aliases.py --check     # solo avisa pares nuevos sin unir; no fusiona (el weekly fusiona antes de avisar)
python scripts/fix_league_labels.py              # en seco; --apply reetiqueta ligas (respaldo antes)
python scripts/fix_team_identities.py            # en seco; --apply une nombres y repetidos (después del anterior)
python scripts/audit_analyst_calibration.py --days 60
python scripts/db_smoke_test.py                  # 36 checks contra la base real, solo lectura
python -c "from scripts.revalidate_pending_bets import revalidate_pending_bets as r; r(dry_run=True)"  # qué cancelaría ahora
python scripts/fix_stat_settlements.py           # en seco; --apply corrige liquidaciones (--motivo "...")
python scripts/fix_match_stats.py                # en seco; --apply restaura córners y tiros reales del CSV

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

**Se mantiene abierto solo (v1.6.0).** El `.exe` es un supervisor
(`dashboard/supervisor.py`): lanza el servidor (el mismo `.exe` con `--serve`), le
pregunta cada 15 s a `/api/health` y lo vuelve a abrir si se cierra (esperas de 5, 15,
30 y 60 s si se cae seguido) o si pasa ~45 s sin responder (lo cierra con todo su
árbol: el `.exe` de un solo archivo son dos procesos). Si el puerto lo ocupa un
dashboard anterior colgado, lo cierra solo si es `BettingDashboard.exe`; otro programa
no se toca. Cada evento queda en `dashboard_supervisor.log` junto al `.exe`. Un segundo
doble clic solo abre el navegador. La consola desactiva la "edición rápida" (un clic en
la ventana congelaba el servidor) y ya no escribe una línea por petición. La página,
si pierde la conexión, conserva lo último que mostró, avisa arriba y se recarga sola
cuando el programa vuelve (antes: "HTTP 0" en cada sección). Si el 5050 lo usa otro
programa: `DASHBOARD_PORT=` en el `.env` junto al `.exe`. `release.yml` prueba el
relanzamiento: cierra el servidor a la fuerza y exige que vuelva.

**Lo que el programa analiza aunque no apueste (v1.7.0, 21-oct-2026).** En modo recolección
casi no hay apuestas reales y el dashboard se veía vacío. Tres secciones nuevas arriba
(`dashboard/visibility.py`, funciones puras; rutas en `dashboard/app.py`):

- **Lo que dice el programa (próximos 3 días)** — `/api/predictions`:
  - por partido: goles esperados, marcador más probable, 1X2 del programa contra Pinnacle
    (la última recarga del closing), lectura y "PlayDoit mínimo" de cada resultado;
  - sale de `model_vs_sharp`, que desde v1.7.0 también guarda `lam_home`, `lam_away`,
    `p_over25` y `p_btts`. Una fila sin goles esperados es de antes de la corrección del
    modelo y dice "modelo anterior";
  - el mismo partido con un alias viejo se une con `normalize_team`.
- **¿Acertó el programa? (14 días)** — `/api/predictions/history`: resultado real contra
  el favorito del programa y el de Pinnacle, y la probabilidad que cada uno le dio a lo
  que pasó, "desde la corrección" y en total.
- **Lo que el modelo propuso** — `/api/proposals`: cada propuesta de `model_proposals`
  con su decisión, su resultado y lo que habría dado a 1u, más un resumen por decisión.
  Los mercados desactivados (momio de prueba) no cuentan.

**Veredicto de alineaciones (v1.7.1).** Sección arriba de todo (`/api/lineups`,
`dashboard/lineup_verdict.py`) para los partidos de las próximas ~14 h. Usa los onces que
el closing guarda en `lineups` (`capture_upcoming`, ~1 h antes) y el perfil de cada
equipo (`load_profiles`: titulares habituales, 70%+ de sus últimos 15 partidos).

- **Por equipo:** quién falta y si está en la banca o fuera de la convocatoria.
- **Por partido, el veredicto:**
  - la alerta de alineación con su momio mínimo y la revisión de Claude;
  - 4+ fuera sin alerta (el rival tiene menos de 20%);
  - rotación parcial (2-3 fuera);
  - normal;
  - o "sin publicar".
- **Movimiento de Pinnacle:** el más grande desde la corrida de la mañana, solo antes del
  inicio (después el momio es de en vivo).

El banner de actualización sale solo si la versión publicada es MÁS NUEVA (`_newer`).
Antes bastaba con que fuera distinta.

**Dashboard v2.0 (22-oct-2026): partidos, equipos y gráficas.** El dueño: "necesito un mejor
dashboard, que sea entendible, sin tanto texto innecesario… quiero ver solo los partidos,
equipos, gráficas y los partidos anteriores desde septiembre y los próximos". Reemplaza las
pestañas de v1.9-1.10 (Hoy, Dinero real, Sistema). Poco texto: barras, iconos y tooltips.

- **Próximos** (`/api/predictions`): filtros por día, por liga y "✅ Con valor". Cada partido es
  una fila con:
  - la barra local / empate / visitante del programa y, abajo, la de Pinnacle;
  - el pick con su mínimo en PlayDoit y lo que paga aprox.;
  - ✅ / 🟡 / ❌, y ⚠️ si hay alerta de alineaciones.
  - Tocándolo se abre `/api/match`: racha G/E/P, últimos 5 (marcador, córners, tarjetas; el
    once en el tooltip), la alineación y los partidos entre ellos.
- **Anteriores** (`/api/board/past`, `dashboard/board.py`): cada partido jugado desde
  `board.SINCE` (1-sep) en las ligas que analiza el programa (las de `model_vs_sharp` y
  `upcoming_matches`).
  - Por fila: marcador, córners, tarjetas, ¿acertó el favorito del programa (🤖) y el de
    Pinnacle (🏦)?, el pick y su resultado, y las apuestas reales.
  - El día es el de México con la hora del partido: un 19:00 de México en `matches` ya cae
    en la fecha UTC del día siguiente.
  - Arriba van los totales. Las consultas tardan ~5 s, con caché de 5 min (`_PAST_CACHE`).
- **Equipos** (`/api/board/teams`, `/api/board/team`): récord desde septiembre, promedios,
  gráficas de goles y córners por partido, próximos y la tabla de partidos.
- **Gráficas** (`/api/board/charts`):
  - bankroll (`bankroll_history`);
  - acierto semanal del favorito, programa contra Pinnacle;
  - picks del programa acumulados;
  - propuestas por semana (las del modelo con error, más tenues).

**La página se recarga sola al cambiar la versión.** El 21-oct el dueño "no veía nada": su
pestaña se abrió antes de actualizar y solo refrescaba datos.
- `/` se sirve con `Cache-Control: no-store` y la versión en `window.APP_V`.
- Cada minuto, `ui_es5.js` compara la versión con `/api/health`; si cambió, recarga.
- El JS se pide como `/ui_es5.js?v=<versión>`, así que no queda uno viejo en caché.
- Una pestaña de antes de v2.0 solo se recarga si nota la caída del programa: tiene que
  pedir datos mientras está apagado (auto-refresco de 5 min).

**Picks del programa guardados (`src/models/program_picks.py`, tabla `program_picks`).**
Hasta v1.8.0 el pick de cada partido se calculaba al vuelo y no quedaba en ningún lado:
`upcoming_matches` se poda a los ~7 días y `model_vs_sharp` se sobrescribe. No había forma
de responder "cómo nos fue con los picks".

- **Una sola regla:** `pick()`, que el dashboard también usa vía `visibility.program_pick`.
- **Cuándo se guarda:** `snapshot_picks` corre en morning, evening y cada closing para los
  próximos 3 días, con los momios del momento. Hace upsert mientras el partido no empiece,
  así que cuenta el último pick antes del inicio.
- **Cómo se liquida:** `resolve_picks`, en "Shadow results" del evening, con la misma
  función que las apuestas. Es a 1u al momio estimado de PlayDoit (`playdoit_odds`: el
  promedio de las casas por `playdoit_ratio`). "Ambos anotan" no tiene promedio de casas:
  cuenta en ganados y perdidos pero no en unidades.
- **Chequeo:** el chequeo diario de salidas también re-liquida esta tabla.
- **Picks reconstruidos** (`backfill_picks`, `source = 'reconstruido'`, 22-oct-2026). Con lo
  que había ANTES de cada partido (la predicción de `model_vs_sharp` y las fotos de
  `odds_history`) se aplica la regla de hoy a los partidos ya jugados, sin mirar el resultado.
  - Solo toca partidos ya jugados, y solo los que no tienen pick (con `overwrite=True`, todos:
    para pisar los de una regla vieja).
  - Hasta el 21-oct `model_vs_sharp` solo guardaba el 1X2.
  - Hay precio de PlayDoit desde el 12-oct, cuando `odds_history` empezó a guardar el
    promedio de las casas.

**La regla del pick (v2.1, 22-oct-2026): una apuesta real entre todos los mercados.**

- **Por qué cambió:** el dueño, sobre los partidos de Champions: "no siempre es gana local o
  gana visitante… ambos anotan o 2.5 goles o 3.5, también están los córners o tarjetas", y que
  el pick sea "lo más real para ver las tendencias". La regla vieja (el resultado del 1X2 donde
  más se separaba del mercado) perdió: 155 de 452, −38.85u. Mientras más se separaba, peor:
  - 3-5 pts: 49% contra 44% que esperaba el mercado;
  - más de 20 pts: 25% contra 28%.
- **Candidatos** (`program_picks.candidates`), cada uno con precio real:
  - ganador: Pinnacle sin margen; PlayDoit = promedio de las casas;
  - más/menos 2.5: promedio de las casas;
  - más/menos 1.5 o 3.5, cuando esa es la línea principal de Pinnacle. Sale de la matriz de
    marcadores del programa: sus goles esperados, escalados a su mismo más de 2.5 y
    repartidos en su mismo 1X2 (`goals_matrix`);
  - ambos anotan;
  - córners y tarjetas en la línea de la API, con `props_fair`.
  - Donde solo hay mejor momio, PlayDoit se estima × `TWO_WAY_AVG_RATIO` (0.96: el promedio
    de las casas sobre el mejor momio en más/menos 2.5, medido el 22-oct).
- **Elección** (`choose`): entre los que cumplen todo, el de mayor diferencia.
  - El programa lo cree: su favorito del 1X2, o 50% o más en un mercado de dos.
  - Diferencia con el mercado de 3 a 8 pts (`MIN_GAP`, `MAX_GAP`).
  - Momio de PlayDoit entre 1.40 y 3.80.
  - El mercado no se movió 3 pts o más en contra desde que el programa analizó el partido
    (`MOVED_AGAINST`; foto de `odds_history` de antes de `created_at`). En el Bayern–Arsenal
    del 21-oct, Pinnacle pasó de 46% a 40% por la rotación; el programa no usa alineaciones.
  - **El ganador no trae 4+ titulares habituales fuera** (v2.5.0, 26-oct-2026). El dueño:
    "ponemos los picks mucho antes de que la lista de alineaciones esté lista… no es correcto
    poner que un equipo ganará cuando no tiene a jugadores importantes o sale con equipo
    suplente".
    - El closing guarda la alineación de ESPN (paso "Alineaciones publicadas", ~1 h antes) y
      DESPUÉS recalcula el pick ("Picks del programa"); el aviso de alineación sale al final
      ("Alertas de alineación") con ese pick.
    - `lineup_check`: si al ganador le faltan 4+ titulares habituales (`TRIGGER_MISSING`, el
      umbral de las alertas), su motivo es `WEAKENED` ("Alineación: a X le faltan N titulares
      habituales (…)"). El pick pasa a lo mejor que quede de lo que el programa cree; solo si no
      queda nada sigue siendo ese ganador, con el motivo, nunca 🎯.
    - `program_picks.lineup`: NULL = el pick todavía no vio la alineación; "confirmada" (y
      quién trae 4+ fuera) = se recalculó con ella. Lo que se mide es el último pick.
    - Por qué 4: contra el cierre, el rival del equipo debilitado gana +2.6 pt con 4 fuera y
      +8.2 con 5+ (4,389 partidos, 20-oct), y el programa ni siquiera ve la rotación.
    - Medido el 26-oct: en 415 picks a que gana un equipo (desde septiembre) los de 4+ fuera
      salieron 23/41 (+0.78u), igual que el resto; la muestra es chica. En los últimos 4 días
      bloquea al ganador en 9 de 173 partidos con alineación (Stuttgart, Besiktas, Benfica,
      Leipzig, Palmeiras…), y uno era 🎯 (Palmeiras–Bragantino).
    - Dashboard: 📋 el pick ya vio la alineación; ⏳ todavía no (sale ~1 h antes: para
      apostar, esperar ese pick); "· sin alineación" en las ligas que ESPN no cubre
      (`espn_leagues`), donde el pick nunca la ve.
  - Si es un partido entre ligas distintas (`CROSS_LEAGUE`: Champions, Europa League…), el
    análisis tiene que traer el ajuste por fuerza de liga (`model_vs_sharp.league_factor` no
    NULL; §7, "Fuerza de liga"). Sin él el programa sobrevalora a quien golea en una liga débil:
    Viking mete 2.3 por partido en Noruega y Aston Villa 1.6 en la Premier. Las copas de
    América (Libertadores, Sudamericana, Concacaf) nunca pasan la regla: ClubElo es solo Europa.
- **Pick en todos los partidos (v2.2, 22-oct-2026).** El dueño: "quiero que todos tengan picks
  no importa si se perdió o ganó, quiero saber para dónde tiende tu aprendizaje". La regla de
  arriba ya no esconde el pick, lo marca:
  - lo que pasa la regla es el pick 🎯 (`reason` NULL);
  - si nada la pasa, el pick es, de lo que el programa cree, donde más se separa del mercado,
    guardado con el motivo por el que no pasa (`reason`);
  - sin momios del 1X2 (antes del 12-oct `odds_history` no tiene Pinnacle ni promedio), el 1X2
    contra el Pinnacle de `model_vs_sharp`, sin precio: cuenta acierto, no unidades;
  - solo un partido que el programa no analizó (sin fila en `model_vs_sharp`) se queda sin pick.
  - **El motivo se ve escrito** (v2.4.1, 24-oct-2026), no solo al pasar el mouse: "Sin análisis
    · poca historia" (K-League y ascendidos con menos de 15 partidos), "Sin pick · sin momio
    en rango"… El dueño veía los partidos de la K-League vacíos sin saber por qué.
  - **Pick aunque haya poca historia** (v2.4.2, 24-oct-2026; el dueño: "aunque tenga poca
    historia o no cumpla, yo quiero ver el pick, con una breve descripción").
    - **Cuándo aplica:** un equipo con menos de 5 partidos en la base, o Dixon-Coles por debajo
      del 50% (algún equipo con menos de 15 partidos).
    - **Qué hace:** el partido se analiza igual y se guarda marcado
      (`model_vs_sharp.low_history`, con el motivo). Su pick se ve, nunca 🎯, con el motivo
      `LOW_HISTORY` ("Poca historia para apostar: …").
    - **Qué no hace:** no apuesta, no entra a la sombra ni a las propuestas, no aprende el sesgo
      (`side_bias.LEARNABLE`), no cuenta en la auditoría ni en los aciertos del dashboard.
    - Sin ninguna historia (sin forma) no hay análisis ni pick.
  - **Cada pick que no pasa la regla dice por qué, corto, a la vista:** "⚠️ poca historia",
    "sin ventaja", "muy lejos del mercado", "el mercado se movió en contra", "sin momio en
    rango".
  - **Los partidos que ya empezaron salen en Anteriores** con su pick y ⏳ en el marcador, hasta
    que el marcador llega en la corrida de la noche. Antes los de hoy no estaban ni en Próximos
    (ya empezaron) ni en Anteriores (sin marcador): el 24-oct el dueño no veía ninguno de los 19
    partidos de las 5 grandes, aunque todos tenían su pick guardado. Un partido reprogramado (con
    pick en una fecha posterior) no sale como empezado en la fecha vieja (v2.4.3).
  - **Marcadores del día (v2.4.4, 24-oct-2026).** El dueño: "también quiero ver si se ganó o
    perdió".
    - Cada closing (paso "Marcadores del día (ESPN)", `src/features/live_scores.py`) lee de
      ESPN, gratis, el marcador de los partidos del sistema que empezaron en las últimas 6 h, con
      córners y tarjetas. Lo guarda en `live_scores`, aparte: `matches` y la liquidación oficial
      no cambian. El emparejamiento es el estricto de las alineaciones.
    - En Anteriores se ve el marcador ("● 67'" en juego, "final" al terminar). Con el final, el
      pick sale ✅ o ❌ con la misma regla de liquidación (`provisional_result` →
      `save_bets.resolve_market`), al momio estimado de PlayDoit.
    - Es provisional: no cuenta en los totales hasta que la corrida de la noche lo confirma.
    - **Córners y tarjetas de los partidos pasados (v2.5.1, 26-oct-2026).** El dueño: "¿por qué solo
      los de hoy tienen tarjetas y córners y los de ayer no? ni los del pasado".
      - `matches` no los trae en MLS, Liga MX, Argentina, Brasil, Noruega, Suecia, Japón, China ni
        Champions (football-data no los publica), y en las ligas grandes llegan días después.
      - `live_scores.backfill_finals`: el final de ESPN (marcador, córners, tarjetas) de lo jugado,
        con los nombres de `matches` (`system_names`, ±1 día, estricto; sin su partido no se
        guarda). El evening lo repasa (paso "Córners y tarjetas (ESPN)", últimos 3 días, después
        de "Fetch results"); la primera carga fue desde el 29-ago (1,350 partidos).
      - El dashboard los usa donde `matches` no los tiene (`board.fill_from_espn`: mismo partido
        ±1 día y mismo marcador) en Anteriores, Equipos y el detalle del partido
        (`merge_history(fill=…)`, solo completa, nunca suma partidos). `matches` no cambia: el
        modelo y la liquidación siguen igual.
      - Validado el 26-oct: 0 marcadores distintos del oficial en 1,318; contra football-data,
        córners iguales en 95% de 586 partidos y tarjetas en 83% (ESPN suele contar una más:
        64 de 101 diferencias son +1). Desde septiembre: con córners de 1,073 a 1,798 de 2,113
        partidos; quedan sin ellos las selecciones y la K-League (ESPN no las cubre) y los que
        ESPN publica con 0-0 en córners (hueco de la fuente).
    - **Lo vigila el chequeo diario de salidas** (sección 7, `output_checks.live_score_mismatches`):
      un marcador final de ESPN distinto del oficial del mismo partido es ERROR, porque sería un
      emparejamiento o un dato malo. Al 26-oct coincidían los 139 que ya tenían oficial.
    - El 24-oct se emparejaron los 59 partidos que habían empezado.
  - **Una consulta que falla no deja la página a medias (v2.4.5, 25-oct-2026).** La consulta de
    los picks falló (la base tardó, por la VPN de la PC) y Anteriores salió con todos los
    partidos "Sin análisis" y los totales de picks en "—", sin avisar; además se guardó así en
    la caché 5 minutos.
    - Ahora `_q` reintenta una vez un error de conexión.
    - Si falla igual, toda la respuesta dice "No se pudieron leer todos los datos… recarga"
      (`_partial_is_an_error`) y no se guarda en la caché.
    - Las tablas que pueden no existir todavía van con `optional=True` (`live_scores`,
      `weekly_narrative`).
    - Lo vigila `tests/test_dashboard_db_down.py`: recorre todos los endpoints de datos con cada
      consulta fallando. Ninguno puede contestar ok con datos vacíos ni tronar con un 500. La
      clase entera queda vigilada, no solo la pantalla donde apareció.
  - **Lo que vio el programa (v2.4.6, 26-oct-2026).** El dueño dudó del pick de LAFC vs LA Galaxy
    (gana el local, terminó 2-2): "yo hubiera propuesto ambos anotan".
    - El detalle de cada partido muestra todos los mercados que el programa pesó
      (`program_picks.market_view`, los mismos candidatos que el pick): programa, mercado sin
      margen y diferencia, con el pick marcado.
    - En ese partido: local +6.1 (el pick); ambos anotan −4.4 y más de 2.5 −4.6. El programa
      veía menos goles que el mercado y solo propone donde ve más.
    - Medido el 26-oct en 140 picks jugados desde el 22-oct:
      - 🎯 ganador 16/26 (+6.7u), el tipo que mejor va;
      - 🎯 ambos anotan 19/31 (+1.4u);
      - 🎯 goles 11/25 (−5.8u), el más débil. En 153 partidos el mercado predice los goles
        mejor que el programa, y la forma de los últimos 5 sola predice peor que los dos.
  - **Líneas 1.5 y 3.5 y el momio mínimo en la vista de mercados (v2.5.2, 26-oct-2026).** El
    dueño, sobre Flamengo vs Atlético-MG: "también se deben valorar el 2.5, ambos anotan o el
    menos 3.5".
    - `program_picks.estimated_lines`: más y menos de 1.5 y 3.5 cuando no tienen momio real
      (la línea principal de Pinnacle sí lo tiene). El programa, con su matriz de marcadores
      (`goals_matrix`, la misma de los picks); el mercado, estimado de su más/menos 2.5 sin
      margen (Poisson con el mismo más de 2.5, `_market_goals_lambda`). Se marcan con * y no
      entran al pick: sin precio real no se puede saber si PlayDoit paga.
    - Cada fila donde el programa ve más que el mercado trae el momio mínimo de PlayDoit
      (`min_american`, el mismo margen de 2 pt), para valorar cualquier mercado, no solo el pick.
    - En ese partido: menos de 3.5, programa 76% contra 62% estimado (+14 pt, PlayDoit −147 o
      más); terminó con 3 goles al medio tiempo.
    - Por qué no se compran las líneas alternativas de goles (`alternate_totals`, +1 crédito
      por evento enriquecido): el 26-oct quedaban ~1,350 créditos para 5 días (el presupuesto
      llega al 1-nov con la reserva de 600). Se puede reconsiderar con el plan renovado.
  - **Más de 2.5 y ambos anotan por equipo (v2.5.0, 26-oct-2026).** El resumen de cada equipo
    en el detalle dice en cuántos de sus últimos 5 hubo más de 2.5 y ambos anotaron. El dueño
    dudó del "más de 2.5" de Unión vs Estudiantes ("hay demasiados partidos donde no meten
    gol"), y en los últimos 5 eran 3 de 5 en cada uno (7 de 10 y 6 de 10 en los últimos 10).
    El programa veía 51% (48% crudo + 3 pt del sesgo de goles de América, donde el modelo
    queda 4.3 pt abajo del mercado en Argentina y en 52 partidos salió 42% contra 37.5% del
    programa) y el mercado 43%.
  - **Ángulo de goles (v2.4.7, 26-oct-2026).** Si alguno recibe 2+ goles por partido en sus
    últimos 5 oficiales (4+ partidos), el detalle lo dice (`match_detail.goals_angle`):
    - lo que ve el programa en más de 2.5 y ambos anotan contra el mercado;
    - **alternativa de goles** solo si el programa la cree (50%+) y ve 3+ pt más que el mercado,
      el mismo umbral del pick; si ya es el pick, lo dice;
    - si no, que el mercado ya cuenta con esos goles;
    - abajo, lo que midió el buscador del lunes sobre esa situación (`_goals_evidence`, lee
      `model_state/edge_finder`; sin reporte nuevo no sale la línea).
    - No cambia el pick: la regla sola pierde (arriba). LAFC vs LA Galaxy (2-2): la Galaxy
      recibía 2.4, el mercado daba más de 2.5 al 67% y el programa 63%, sin alternativa.
  - Al 22-oct: 461 partidos jugados con pick (antes 24); 24 pasan la regla; 246 sin precio.
- **Nombres en pantalla** (`dashboard/display.team_name`, solo para mostrar; la clave sigue en
  minúsculas): las siglas van en `_UPPER_WORDS` y la primera palabra siempre con mayúscula. El
  26-oct salían "la Galaxy", "Lafc", "Aik", "Psv Eindhoven", "Malmo Ff". Telegram usa la misma
  función (aviso de alineación, títulos de partido y doble oportunidad), no `.title()`.
- **Tiros al arco** no tiene precio en ninguna fuente: no es pick.
- **El dashboard muestra el pick guardado** (`visibility.saved_pick`), no lo recalcula: lo que
  se ve es lo que se mide. 🎯 marca los que pasan la regla; el motivo de los demás va en el
  tooltip. En Anteriores, los totales de todos los picks y de los 🎯; en Gráficas, unidades y
  acierto por tipo de apuesta (solo del modelo corregido) y la línea de unidades de los 🎯.

**Quién tuvo razón y el partido abierto (v1.10.0, 21-oct-2026).** El dueño pidió ver, en cada
apuesta, "quién ganó de los 2", y poder abrir el partido para ver sus últimos 5 partidos.

- **¿Quién tuvo razón?** (`visibility.who_was_right`): el programa o el mercado, el que le
  dio más probabilidad a lo que pasó. Si el pick ganó y el programa le daba más que el
  mercado, tuvo razón el programa; si perdió, el mercado.
  - En los picks se compara contra el mercado del pick: Pinnacle en el 1X2 y el promedio de
    las casas en goles y ambos anotan.
  - En las propuestas se compara contra Pinnacle (`pin_prob`).
  - Sin resultado, con devolución o sin la probabilidad del mercado: "—".
  - Cada pick se abre con la explicación y las estadísticas de antes de ese partido.
- **El partido abierto** (`/api/match`, `dashboard/match_detail.py`, ~1.6 s):
  - por equipo, sus últimos 5 partidos con marcador (G/E/P), córners a favor y en contra,
    tarjetas del partido (roja = 1) y el once que usó (`lineups`), más los promedios;
  - la alineación del partido: la confirmada si ya salió (±3 h del inicio); si no, la
    probable (los 11 con más titularidades en sus últimos 10 partidos con alineación);
  - los últimos 5 entre ellos.
  - Un 0-0 en córners es un hueco de la fuente (Liga MX 2025) y se muestra "—". Liga MX,
    MLS, Brasil y Argentina no tienen córners ni tarjetas en `matches`.
- Los partidos abiertos siguen abiertos después del auto-refresco.

**Revisor de PlayDoit por captura (21-oct-2026).** PlayDoit no está en ninguna API y su
página bloquea a los programas automáticos (no se raspa). El dueño manda una captura de
PlayDoit al bot, en el mismo chat de los avisos, y el bot responde momio por momio:
"✅ vale la pena" o "❌ no (necesita X o mejor)".

- **Cómo decide** (`src/models/playdoit_prices.py`): la regla de siempre. La probabilidad
  más baja entre el programa y el mercado sin margen (Pinnacle en 1X2, doble oportunidad y
  empate anula; promedio de las casas en más/menos 2.5 y ambos anotan), menos 2 pts.
- **Cómo lee la captura** (`scripts/playdoit_checker.py`): Claude Haiku 5.5, una fracción
  de centavo por captura. Tope diario propio `PLAYDOIT_CHECKER_DAILY_USD` (0.25) además del
  de la cuenta.
- **Dónde corre** (`.github/workflows/playdoit_checker.yml`):
  - en GitHub Actions, donde están las claves;
  - escucha ~5 h 45 min con long polling y dispara su relevo;
  - `heartbeat_dispatch.py` (en cada closing) lo relanza si no hay ninguno corriendo;
  - el offset de Telegram va en `model_state.telegram_checker_offset`, así que un mensaje
    nunca se procesa dos veces.
- **Seguridad:** solo responde al chat `TELEGRAM_CHAT_ID`.
- **Lo que guarda:** cada momio leído va a `playdoit_prices`. Con 10 o más capturas sin
  momio mejorado se aprende la mediana de (momio PlayDoit / promedio de las casas)
  (`playdoit_ratio`). El dashboard estima con ella el momio de PlayDoit ("PlayDoit est.");
  sin muestra suficiente usa el promedio tal cual (medido el 19-oct: PlayDoit ≈ promedio).

**Arranque y reinicio en la PC del dueño:** `installer\dist\BettingDashboard.exe`,
lanzado por el acceso directo de la carpeta Inicio de Windows (`Betting Dashboard.lnk`,
carpeta de trabajo `installer\dist`, donde vive el `.env`). Desde una sesión de agente
se lanza con `Start-Process explorer.exe -ArgumentList "<Inicio>\Betting Dashboard.lnk"`,
nunca con `Start-Process` directo sobre el `.exe`: el 24-sep-2026 Windows lo cerró junto
con la sesión que lo había lanzado.

**PlayDoit (la casa del dueño) no está en ninguna API de cuotas** (ni The Odds API, ni
Odds-API.io, ni OddsPapi; revisado el 24-sep-2026). La cuota que guarda el sistema es la
mejor entre ~20 casas europeas. Por eso cada pick, cada confirmación pre-kickoff y el
avance de mañana dicen "PlayDoit: apuesta si paga -138 o mejor (-125 ✅ · -150 ❌)":
**formato americano**, como lo muestra PlayDoit, porque el dueño no lee cuotas
decimales (`src/utils/min_odds.py`: edge ≥ 2%, el mismo umbral de la revalidación,
redondeado del lado seguro). Desde el 25-sep-2026 el mínimo usa la probabilidad **más
conservadora** entre el modelo y Pinnacle (`pin_close_prob` si el closing ya la
capturó; si no, la del pick): apostar por debajo de lo que Pinnacle considera justo no
tiene valor real. En americano un número más alto siempre paga más. El dashboard
muestra lo mismo (columnas **Mejor cuota** y **PlayDoit**), marca las canceladas
("Cancelada", "no apostar"; en la lista hasta el kickoff, después en el filtro
**Canceladas**) y no pide nada a mano. "Bets 90d", el CLV medio y el % de acierto
cuentan solo apuestas jugadas (acierto = ganadas / (ganadas + perdidas), igual que el
reporte semanal, que pone las canceladas en una línea aparte). El registro de la cuota tomada
(v1.4.0) se quitó en v1.5.0 por pedido del dueño. La columna `bets_history.odds_placed`
sigue en la base, sin uso.

**Las apuestas reales se liquidan a ese mínimo de PlayDoit** (23-oct-2026), no a la
mejor cuota europea: el dueño apuesta en PlayDoit cuando paga el mínimo o más, así que
liquidar a la mejor de Europa inflaba el bankroll del sistema.
- `save_bets.playdoit_settle_odds` lo calcula al guardar la apuesta: `min_odds` con el
  modelo y el Pinnacle del momento de apostar (`pin_prob` de la bet o, si no viene,
  `decision_log.market_ctx.pin_prob`, el mismo que lee Telegram), sin pasar de la mejor
  cuota europea. Se guarda en `bets_history.odds_playdoit_min`.
- `plan_bet_settlements` liquida con `settle_odds`: `odds_playdoit_min` si existe, si no
  `odds` (las apuestas anteriores al 23-oct).
- `scripts/clv_audit.py` (weekly) cuenta la ganancia con el mismo precio.
- El valor contra Pinnacle (`sharp_reference`) se sigue midiendo a la mejor cuota europea:
  mide al modelo, no lo que cobra el dueño.

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
en el commit. El escenario extendido (`extended_kwargs`) apaga el salto por falta de
Dixon-Coles (`MIN_DC_WEIGHT` = 0): sus equipos sintéticos no tienen ajuste a propósito, y
`tests/test_output_checks.py` vuelve a encenderlo para probarlo.

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
