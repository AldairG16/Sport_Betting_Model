# 10 · Dashboard

## Resumen

Un `.exe` que corre en la PC del dueño y se abre en el navegador: **http://127.0.0.1:5050**. Es
`dashboard/app.py` (Flask) con `dashboard/ui_es5.js`; lee la base (no escribe, salvo su consumo y el
respaldo). Versión en `VERSION` (2.12.2 en noviembre de 2026).

**Pestañas:**

| Pestaña | Qué muestra |
|---|---|
| Encabezado | Los 🎯 cobrados desde el 22-oct (cuántos se ganaron y lo que van dejando apostando 50) y la hora de la última actualización |
| Próximos | Cada partido de los próximos 3 días con su pick, el 🎯 o por qué no, de cada 100 (programa y Pinnacle), la alineación y el marcador en vivo |
| Parlay | Los parlays por jugar y los ya cobrados |
| Anteriores | Lo que ya pasó, con el resultado de cada pick |
| Ganadas y perdidas | Por día, por tipo de pick y por liga |
| Por liga | Cómo le va al programa en cada liga |
| Equipos / Gráficas | El historial de cada equipo y las gráficas |

**Cómo se actualiza:** cada 5 minutos, solo con la página a la vista (cada 15 en modo ahorro de la base), y con
el botón "🔄 Actualizar". La página se recarga sola cuando cambia la versión.

**Cómo corre en la PC:**

- Es un **supervisor** (3 procesos): si el servidor se cae o deja de contestar ~45 s, lo vuelve a abrir.
- Arranca con Windows desde el acceso **"Betting Dashboard"** de la carpeta Inicio. Siempre se abre así
  (explorer + acceso directo), nunca lanzando el `.exe` desde una terminal: el proceso muere con ella.
- Lee el `.env` que está junto al `.exe` (`installer\dist\`).
- Hace el **respaldo semanal** de la base en `installer\dist\respaldos\` ([06](06-base-de-datos.md)).

**Cómo se instala una versión nueva** (lo hace Claude, nunca el dueño):

1. Tag `vX.Y.Z` → `release.yml` prueba, compila y publica el `.exe`.
2. Bajar el `.exe` en su propio paso y comparar su tamaño con el del Release. Si no coincide, no se toca nada.
3. Copiar el `.exe` actual a `installer\dist\respaldo_vANTERIOR\` (nunca se borra).
4. `taskkill /IM BettingDashboard.exe /T /F` y copiar el nuevo **dentro de un ciclo de reintentos** (el
   archivo sigue bloqueado unos segundos).
5. Abrir con explorer y el acceso de Inicio. Verificar: 3 procesos, padre `explorer`, `/api/health` con la
   versión nueva y el encabezado con datos.

Para probar sin tocar el del dueño: otra copia con `DASHBOARD_PORT=5071` (5060 y 5061 los bloquea el
navegador).

<!-- detalle movido de OPERACION.md -->

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

**Botón "🔄 Actualizar" (v2.5.3, 29-oct-26).** El dueño: "necesito un botón de refresh para estar
monitoreando los resultados cuando cambia algo". Vuelve a leer el encabezado, Próximos y la
pestaña abierta (las otras, al abrirlas) y dice "Actualizado HH:MM · hay cambios / sin cambios"
(compara cada respuesta con la anterior; en verde si cambió algo). Pide `/api/board/past` y
`/api/board/charts` con `?fresh=1`, que se salta la caché de 5 min del servidor; sin eso el botón
devolvía lo mismo. Solo, cada 5 min, hace lo mismo menos Equipos y Gráficas. Los marcadores
en vivo salen de `live_scores`, que llena el closing cada 30 min: el botón no puede verlos más
frescos que eso.

**Pestaña "Ganadas y perdidas" (v2.5.3, 29-oct-26).** El dueño: "necesito gráficas y datos de
ganadas y perdidas, ver los picks más ganadores y perdedores, y las ligas, cómo van en las
apuestas, si se ganan bien ahí o no". `/api/board/results` → `board.results_board`, sobre las
mismas filas de Anteriores (`_past_rows`, con `?fresh=1` del botón). Tres grupos:
- todos los picks del modelo corregido (desde el 22-oct);
- los 🎯 (pasan la regla);
- las apuestas reales.

Mismo criterio que `summary`: solo lo liquidado (no el provisional de ESPN) y sin los de poca
historia. Por grupo muestra:
- indicadores: ganadas, perdidas, empates, acierto, unidades y rendimiento en %;
- ganadas y perdidas por día con las unidades acumuladas;
- ranking por tipo de pick y por liga (con menos de 5 picks, en gris, "pocos datos");
- gráfica de unidades por liga;
- mejores aciertos, con su momio americano;
- fallos donde el programa estaba más seguro.

**Pestaña "Por liga" (v2.10.0, 7-nov-26).** El dueño: "necesito que me des un resumen de los partidos que
tenemos hasta hoy, pero me lo ordenes por ligas, dime cuántas se ganaron y cuántas se perdieron, y necesito
que eso también me lo muestres en el dashboard", y luego: "solamente quiero ver los partidos de hoy".
`/api/board/leagues` → `board.league_board(rows, now, upcoming)`. Dos periodos, cada uno con 🤖 todos los
partidos y 🎯 solo recomendados:
- **📅 Hoy** (abre aquí; el día de México, UTC-6): todos los picks del día, también los de poca historia.
  Cuentan como ganados o perdidos los ya cobrados y los terminados que todavía no se cobran (el provisional
  de `_provisional`, "por confirmar"). En "⏳ Faltan" van:
  - los que se están jugando (● en vivo, con el reloj de ESPN);
  - los que esperan el resultado;
  - los que todavía no empiezan (`program_picks` de hoy con `match_date > NOW()`, 🕒 hora).
- **📊 Desde el 22 oct:** los mismos picks que "Ganadas y perdidas" (`board._played`, que ahora usan las
  dos): ya cobrados, sin los de poca historia ni los provisionales.

Lo que se ve:
- indicadores: partidos, ganados, perdidos, anulados (si hay), los que faltan (en Hoy), ganados de cada 100
  y la ganancia apostando 50 a cada uno;
- una fila por liga en orden alfabético (sin que los acentos manden al final); un toque al título ordena
  por esa columna, y en el celular los títulos son solo ✅ ❌ ⏳ para que la tabla quepa;
- tocar la liga abre sus partidos: en Hoy, primero los que se juegan, luego los terminados y al final los
  que no empiezan; desde el 22 oct, el más reciente primero, con la fecha;
- "*" junto a la ganancia: picks sin momio de PlayDoit, que cuentan en ganados y perdidos pero no en la
  ganancia;
- desde el 22 oct, las ligas con menos de 5 partidos van en gris ("pocos partidos").

Un partido que empezó y se suspendió hasta el día siguiente (Nacional vs Académico de Viseu, 7-nov) no sale
en Hoy: su pick tiene otro renglón más tarde y `past_matches` lo trata como reprogramado.

Con 192 picks (29-oct) cada liga tiene 3-29: el ranking describe lo que pasó, no predice.

---

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

---

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

---

**Encabezado del dashboard sin el bankroll congelado (v2.11.0, 8-nov-26).** El dueño: "arregla también lo del
bankroll del encabezado". Decía "💰 Bankroll 63.4u", de la tabla `bankroll`, quieta desde el modo colección
(25-sep-26). La gráfica "Bankroll" de Gráficas (`bankroll_history`) era una línea plana desde esa fecha.
- Ahora el encabezado dice "🎯 Desde el 22 oct: 118 de 210 ganados · +112" (`/api/board/header`). Es la misma
  consulta que la noche de Telegram (`telegram_reports.SINCE_SQL`) y da lo mismo que "Por liga" (🎯, desde el
  22 oct).
- La gráfica del bankroll se quitó, junto con `board.bankroll_series`.
- `/api/kpis` sigue (lo usan las pruebas de la API vieja), pero la página ya no lo pide.

**Todo apostando 50 (v2.11.0, 8-nov-26).** El dueño: "1, todo a 50". Toda ganancia, en Telegram y en el dashboard
(encabezado, Por liga, Anteriores, Ganadas y perdidas, Gráficas, Parlay), se dice "apostando 50 a cada uno": el pick a
1 unidad al momio de PlayDoit × 50.
- **Dónde está el 50:** `telegram_reports.STAKE` y `STAKE` en `ui_es5.js`.
- **Redondeo:** el mismo en los dos, al entero, hacia arriba en el medio y sin el ruido del decimal
  (-0.55 × 50 = -27.500000000000004 es -27).
- **Lo que se quitó:** "u", "Unidades" y "Rendimiento" (el ROI en %) en Ganadas y perdidas y en Parlay. Las gráficas
  multiplican sus datos por 50.
- **Apuestas reales de `bets_history`:** se dicen por unidad apostada (profit / stake, `board._per_unit`), porque cada
  una tenía stake distinto.
- **Sin cambio:** los reportes técnicos del domingo (laboratorio de estrategias, Pinnacle, alertas de alineación)
  siguen en unidades.
- **La gráfica "Ganancia acumulada de los picks"** (`board.picks_series`) usa las mismas filas que Anteriores y
  Ganadas y perdidas (`past_matches`). Antes leía `program_picks` aparte y no cuadraba:
  - "todos" terminaba en -660 contra -784, porque contaba los de poca historia;
  - "antes del 22 oct" terminaba en -833 contra -894, por 7 picks de segundas divisiones que Anteriores no lista.

  Ahora las tres dicen -784, +112 (🎯) y -894.
Lo cubre `tests/test_telegram_reports.py`, que también revisa que no salgan ROI, "u", Profit ni momios decimales.

---

**Arranque y reinicio en la PC del dueño:** `installer\dist\BettingDashboard.exe`,
lanzado por el acceso directo de la carpeta Inicio de Windows (`Betting Dashboard.lnk`,
carpeta de trabajo `installer\dist`, donde vive el `.env`). Desde una sesión de agente
se lanza con `Start-Process explorer.exe -ArgumentList "<Inicio>\Betting Dashboard.lnk"`,
nunca con `Start-Process` directo sobre el `.exe`: el 24-sep-2026 Windows lo cerró junto
con la sesión que lo había lanzado.
