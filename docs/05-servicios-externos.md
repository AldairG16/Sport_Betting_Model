# 05 · Servicios externos

## Resumen

| Servicio | Para qué | Si se cae |
|---|---|---|
| **The Odds API** (plan de pago, 20,000 créditos al mes) | Cuotas y marcadores. Es la fuente crítica | Sin cuotas no hay picks; el presupuesto diario de créditos avisa una vez al día |
| **Neon** (PostgreSQL, plan gratis desde el 1-dic-26) | Todo el estado | Todo se detiene; el freno del presupuesto lo evita ([06](06-base-de-datos.md)) |
| **Anthropic** (Claude) | El analista previo al partido, el resolvedor de apuestas sin dato y el revisor de capturas de PlayDoit | Esos tres no corren; el resto sí |
| **Telegram** | Toda la salida al dueño y al canal | Una corrida que no logra mandar sale en rojo |
| **ESPN** (gratis, sin clave) | Marcadores en vivo, alineaciones, tiros, córners y tarjetas, segundas divisiones | Se pierde el marcador provisional y las alineaciones; el pick sale sin ellas |
| **football-data.co.uk** (gratis) | Historia de partidos, córners y tarjetas, momios históricos | La historia no se actualiza |
| **Understat / FotMob** (gratis) | xG real y goleadores | Sigue el último xG (sirve 21 días) |
| **TheSportsDB** (gratis) | Marcadores de la K-League | Esos partidos esperan resultado |
| **Kalshi** (gratis) | Segunda referencia sin margen | Solo se pierde la comparación |
| **ClubElo** (gratis) | Fuerza de cada liga | Sigue la foto del 22-oct (sirve hasta el 19-feb-27) |
| **OpenWeatherMap** (gratis) | Clima del partido | Sin clima |
| **cron-job.org** (gratis) | Dispara las corridas | Nada corre; el watchdog lo detecta |
| **GitHub** (repo público: minutos ilimitados) | Corre todo y compila el `.exe` | Nada corre |

Las credenciales son *repository secrets* en GitHub y un `.env` en local (`.env.example` tiene la lista).
Nunca se imprimen ni se copian.

<!-- detalle movido de OPERACION.md -->

## Servicios externos

El modelo no funciona solo: depende de servicios de terceros. Si uno se cae,
el pipeline **sigue saliendo verde en Actions** y deja de producir en silencio —
ver [04](04-corridas-y-horarios.md).

| Servicio | Para qué | Credencial | Coste |
|---|---|---|---|
| **The Odds API** | Cuotas (`/odds`) y resultados (`/scores`). Es la fuente crítica: sin ella no hay apuestas ni resolución. | `ODDS_API_KEY` | Plan de pago de 20,000 créditos que se renueva el **día 1** de cada mes a las 00:00 UTC (6:00 pm del último día del mes en México). La API tarda unos minutos: el 1-oct a las 00:02 UTC ya marcaba 0 usados; el 1-nov a las 00:00:52 todavía daba 349 y a las 00:32, 19,988. Una liga cuesta 3 (h2h, totals, spreads) y un evento hasta 8 (cobra solo los mercados que devuelve). El closing tiene presupuesto diario ([16](16-lecciones-cuotas-y-creditos.md), "Presupuesto diario de créditos"). |
| **PostgreSQL** | Todo el estado: `matches`, `upcoming_matches`, `bets_history`, `shadow_bets`, `bankroll`, `pre_kickoff_analyses`, `analyst_heartbeat`, `anthropic_usage`, y lo aprendido en `model_state` ([09](09-aprendizaje.md)). | `DB_URL` | Alojada fuera del repo. Ver [06](06-base-de-datos.md). |
| **Anthropic** | Los dos agentes LLM: analista pre-kickoff y resolver de pendientes. | `ANTHROPIC_API_KEY` | ~$0.014-0.020 por llamada. Ver más abajo, "Presupuesto de Anthropic". |
| **Telegram** | Único canal de salida: picks, resumen diario, alertas del watchdog. Ante un 429 se espera lo que pide su JSON (`parameters.retry_after`, tope 60 s al enviar y 120 s en el revisor de PlayDoit); otros fallos del revisor esperan 5, 10, 20, 40 y 60 s. | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; el analista usa `*_PREKICKOFF` y cae al bot principal si no están. | Gratis. |
| **football-data.co.uk** | Respaldo de resultados y **única** fuente de córners y tarjetas. Publica con 24-36 h de retraso. Si un archivo no existe, **manda otro parecido** sin avisar ([15](15-lecciones-datos.md)). | Ninguna, son CSV públicos. | Gratis. |
| **OpenWeatherMap** | Clima del partido (`src/features/weather_impact.py`). | `WEATHER_API_KEY` | Gratis, 1000 llamadas/día. |
| **TheSportsDB** | Marcadores de respaldo de las ligas que ni ESPN ni football-data tienen: hoy solo la K-League (`scripts/fetch_results_tsdb.py`, 2-nov-26). The Odds API dejó sin marcador su jornada 34. Corre en el evening y en resultados (solo partidos analizados sin marcador) y en el weekly (las jornadas de la última semana completas). La historia 2023-2026 se cargó una vez con `--history`. Con la clave libre: ~10 llamadas seguidas y luego 429 con `Retry-After` de 120 s, que se respeta. | `THESPORTSDB_KEY` opcional; sin ella usa la clave pública de prueba `123`. | Gratis. |
| **ClubElo** | Fuerza de cada liga para los partidos entre ligas ([07](07-modelo.md), "Fuerza de liga"). Su API (`api.clubelo.com`) responde 502; las páginas públicas `clubelo.com/<PAÍS>` piden un reto anti-bots desde el 29-oct-26: sigue la foto del 22-oct (sirve 120 días, hasta el 19-feb). | Ninguna. | Gratis. |
| **Understat** | xG real de las 5 grandes y goleadores de clubes (`src/features/soccerdata_feed.py`). Lo lee el weekly; si no pudo, el morning reintenta cada día. El xG sirve 21 días: mientras quede más de una semana es aviso, después ERROR. El 2-nov-26 el sitio no respondía (ni desde GitHub ni desde la PC): siguió el xG del 26-oct. Desde el 6-nov-26, la liga que Understat no da sale de **FotMob** (`fetch_fotmob`, el mismo xG de la temporada por equipo y por jugador, `source` 'fotmob'), con aviso en "Avisos de la corrida". | Ninguna. | Gratis. |

`RAPIDAPI_KEY` solo la usa `test_api.py`, que no forma parte del pipeline.

La lista completa de variables, con sus defaults, está en `.env.example`. En GitHub
son *repository secrets*; en local, un `.env` (que está en `.gitignore`).

> **Regla de oro de las variables numéricas:** un secret sin configurar se expande a
> cadena vacía, y `int("")` revienta. Usa siempre `env_int` / `env_float` / `env_str`
> de `config/settings.py`. Ver [17](17-lecciones-codigo.md).

---

## Presupuesto de Anthropic

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
