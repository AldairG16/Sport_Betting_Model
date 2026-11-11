# 08 · Picks y parlays

## Resumen

**El pick** (`src/models/program_picks.py`, tabla `program_picks`):

- Cada partido analizado tiene **un** pick: el mercado que más le gusta al programa entre todos (ganador,
  goles 1.5/2.5/3.5, ambos anotan, córners, tarjetas…), con el **momio mínimo** que debe pagar PlayDoit, en
  americano. **Nunca se esconde un pick**: los filtros solo marcan.
- Se guarda en el morning y se **rehace en cada cierre** (cada 30 min) con los momios nuevos y la alineación
  publicada. Se guarda también el pick de antes de la alineación, para medir el cambio.
- **Queda fijo al empezar el partido** (la hora de ESPN si llega antes): el último antes del partido es el que
  cuenta y el que se cobra.
- Se cobra **al momio mínimo de PlayDoit**, apostando 50.

**El 🎯 (recomendado)**, la regla vigente desde v2.9.0 (6-nov-26):

1. Un equipo **muy favorito**: según Pinnacle gana 65 de cada 100 partidos así o más.
2. PlayDoit lo paga a **-500 o mejor** (cada pierna de parlay con bono también debe ser -500 o mejor).
3. En una liga con **pago anticipado** de PlayDoit (si va 2 goles arriba, se paga como ganada).
4. Sin **4 o más titulares habituales fuera**.
5. Fuera de una **región en pausa** por el filtro aprendido (hoy, América).

El partido sin 🎯 dice por qué en palabras (`star_note`): "no hay favorito claro", "paga muy poco (-667)",
"esta liga no tiene pago anticipado", "le faltan 5 titulares".

**Los parlays** (`src/models/program_parlays.py`, tabla `program_parlays`):

- Aprovechan el **bono de PlayDoit** sobre la ganancia: 3 piernas 5%, 4 = 7.5%, 5 = 10%, 6 = 15%, 7 = 20%,
  8 = 25%, 9 = 35%.
- **Parlay del día** con 4 a 6 🎯, y **parlays de un mismo horario** con 3 o más, esperando las alineaciones.
- Cada pierna es el 🎯 de su partido. Se fija al empezar la primera pierna y se avisa una vez por Telegram.

**PlayDoit** no está en ninguna API. Su precio se toma como el promedio de las casas europeas (con evidencia de
que coincide: PlayDoit/promedio ≈ 1.01 en favoritos). El revisor de capturas (`playdoit_checker.py`) lee lo
que el dueño mande por Telegram; nunca se raspa PlayDoit.

<!-- detalle movido de OPERACION.md -->

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
    - **¿Los cambios con la alineación ayudan o estorban? (v2.8.0, 5-nov-2026).** El dueño: "mide
      si los cambios por alineación ayudan o estorban".
      - Medido a mano el 4-nov con lo que había (`prev_label`, solo el último cambio de cada
        partido, sin momio del pick de antes en 1.5/3.5, córners y tarjetas): 34 cambios en la
        última hora del 1 al 4 de nov. En aciertos, empate (15 → 17). En los 🎯, −5.4u → +2.0u:
        los 5 "mismo pick, quitó la 🎯" perdieron y 4 "🎯 → otro sin 🎯" habrían ganado
        (−3.7u). La regla de 4+ fuera no causó ningún cambio: todos venían del mercado de la
        última hora, que ya incorporó la alineación. ~1.7 desviaciones: no decide.
      - Desde v2.8.0, la primera vez que el pick ve la alineación (`lineup` pasa de NULL a
        "confirmada…") el upsert guarda el pick que había, tal cual (`pre_lineup_label`,
        `pre_lineup_market`, `pre_lineup_star`, `pre_lineup_odds` = su momio de PlayDoit de ese
        momento, `pre_lineup_at`). Una sola vez: si la alineación no se pudo leer y vuelve, no
        se pisa. La reconstrucción (`backfill_picks`) no lo toca.
      - `resolve_picks` lo liquida igual que el pick (`_resolve_pre_lineup`: misma función,
        stake 1; sin momio, el resultado cuenta y las unidades no) en `pre_lineup_result` /
        `pre_lineup_profit`.
      - El reporte semanal de Telegram trae "CAMBIOS CON LA ALINEACIÓN"
        (`src/models/lineup_changes.py`): de la semana y acumulado, aciertos de los que
        cambiaron y lo que dejaron los 🎯 de antes contra los de después (lo que se apuesta;
        sin 🎯 = 0). Veredicto con 20+ cambios con algún 🎯 y ±2 desviaciones: "AYUDAN",
        "ESTORBAN" o "sin diferencia clara"; antes, cuántos faltan. Solo mide: no cambia el pick.
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
    NULL; [07](07-modelo.md), "Fuerza de liga"). Sin él el programa sobrevalora a quien golea en una liga débil:
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
    - Desde v2.8.0 también guarda la hora de inicio de ESPN de lo que empieza en las próximas
      3 h (`espn_kickoffs`): el pick y el parlay quedan fijos a la más temprana ([16](16-lecciones-cuotas-y-creditos.md), "La hora de
      The Odds API puede venir tarde").
    - En Anteriores se ve el marcador ("● 67'" en juego, "final" al terminar). Con el final, el
      pick sale ✅ o ❌ con la misma regla de liquidación (`provisional_result` →
      `save_bets.resolve_market`), al momio estimado de PlayDoit.
    - Es provisional: no cuenta en los totales hasta que la corrida de la noche lo confirma.
    - **También con el marcador oficial (v2.5.4, 3-nov-2026).** El dueño: "¿por qué todavía no
      tenemos ni tarjetas ni algunos resultados?". El evening carga el marcador oficial, pero el
      pick se liquida después:
      - los que empiezan pasada la medianoche UTC (liquidación con 3 h de margen), en el reintento
        de las 06:00 UTC;
      - córners y tarjetas de las ligas grandes, cuando football-data los publica (1-3 días).

      Mientras, salía ⏳ con el final ya en la tabla (5 picks el 2-nov). Ahora un pick sin liquidar
      con su partido en `matches` sale ✅ o ❌ provisional con el marcador oficial y los córners y
      tarjetas de `matches` o, donde faltan, de ESPN (`board._final_of` → `_provisional`, la misma
      regla: roja = 1, córners 0-0 = sin dato). Sin el dato de su mercado sigue ⏳. Tampoco cuenta
      en los totales ni en Ganadas y perdidas hasta que se liquida.
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

---

**Pestaña "Parlay" (v2.6.0, 3-nov-26).** El dueño: "quiero agregar la función parley, donde seleccione
los partidos más seguros, de las mejores ligas, de los que más se ganan y los agrupe en una pestaña".

- **Lo que eligió:**
  - mejores ligas = donde más ganan los picks: unidades a favor desde el modelo corregido (22-oct) con
    10 picks liquidados o más (`program_parlays.winning_leagues`, sin los de poca historia). La lista se
    actualiza sola;
  - de cada partido, la apuesta más segura aunque no sea el pick;
  - un botón de 2, 3 o 4 partidos (empieza en 3).
- **La regla** (`src/models/program_parlays.py`):
  - segura = la probabilidad más baja entre el programa y el mercado sin margen, 55% o más
    (`MIN_SAFE`);
  - entre los mismos candidatos con momio real del pick (`_candidates_of`: 1X2, 2.5, 1.5/3.5 reales,
    ambos anotan, córners, tarjetas), al momio estimado de PlayDoit, que pague −400 o mejor
    (`MIN_LEG_ODDS` 1.25). Sin piso, la más segura sería un más de 1.5 a −1400 que no suma nada:
    Galatasaray–Stuttgart del 3-nov pagaba 1.07;
  - fuera: poca historia, análisis que ya no valen, partidos entre ligas sin el ajuste de ClubElo y
    el ganador al que le faltan 4+ titulares (`lineup_check`);
  - por horario (v2.7.0, abajo), las 4 más seguras (una por partido) con su `rank`. El parlay de k
    son las k primeras.
- **Cuándo se guarda:** `snapshot_parlays` corre en el paso "Picks del programa" (morning, evening,
  cada closing), justo después de `snapshot_picks`. Tabla `program_parlays`, una fila por pierna,
  `UNIQUE (slot, rank)` (`slot` = la hora UTC del primer partido del horario).
  - Mientras no empieza su primer partido, el parlay es un borrador: los borradores (sin resultado y
    `slot` después de ahora + 5 min) se borran y se vuelven a escribir enteros.
  - Desde 5 min antes de su primer partido queda fijo: es la apuesta que se pudo hacer. Sus partidos
    no entran a otro horario (`exclude`).
  - Si ninguna liga va ganando, los borradores se borran.
- **Un parlay por horario (v2.7.0, 4-nov-26).** El dueño: "en los parlays necesito que se tomen en
  cuenta ya que se hagan las alineaciones… si son al mismo horario sería mejor porque las
  alineaciones ya estarían para todos los picks del parlay". Antes era uno por día y quedaba fijo 5
  min antes de su primer partido: el del 6-nov juntaba 12:00, 13:00 y 21:00, y el de las 21:00 se
  elegía 9 horas antes de su alineación. Eligió (1a, 2a, 3a):
  - mismo horario = los partidos que empiezan hasta 30 min después del primero (`BLOCK_MINUTES`,
    `blocks_of`: cada horario arranca con el primer partido que queda). Un horario con un solo
    partido no es parlay (`MIN_BLOCK_LEGS` 2);
  - un parlay por cada horario, con su récord;
  - aviso de Telegram cuando el parlay ya vio las alineaciones de todos (abajo).
  - Por qué alcanza: el closing (cada 30 min) guarda la alineación de ESPN de 80 a 5 min antes de cada
    partido ("Alineaciones publicadas") y después rehace picks y parlays con ella. En el último
    snapshot antes de fijarse, los partidos de 30 min después ya tienen la suya. Medido el 4-nov: el
    último pick con alineación se calculó entre 6 y 33 min antes del partido (casi siempre ~28).
  - Lo que cuesta: menos parlays grandes. De las piernas que cumplían del 28-oct al 7-nov (9 días),
    un horario juntaba 2+ partidos 7 días, 3+ en 3 y 4 en 1 (uno por día juntaba 4 casi siempre).
    Hora exacta en vez de 30 min: 6, 1 y 0.
  - Migración: `_ensure` agrega `slot` una vez (sin la columna). Lo guardado toma como horario el
    primer partido de su día, y `UNIQUE (day, rank)` pasa a `UNIQUE (slot, rank)`.
- **Aviso "Parlay listo" (v2.7.0, `src/models/parlay_alerts.py`):** paso "Parlay listo (Telegram)"
  del closing, después de "Partidos de hoy".
  - Listo = todas sus piernas se eligieron con la alineación (`lineup` "confirmada…") o su liga no
    la tiene en ESPN (`espn_leagues`; ahí nunca llega y esperarla dejaría el parlay sin aviso).
  - Manda los partidos (de la más segura a la menos), lo que paga el de 2, 3 y 4 en americano, la
    probabilidad de que salgan todas y "queda fijo a las HH:MM".
  - Una vez por parlay; otra ("🔄 Cambió el parlay") si cambia antes de quedar fijo. Estado en
    `model_state` 'telegram_parlays' ({horario: partidos y apuestas avisados}, se olvida a los 2
    días).
  - Si Telegram falla, error y se reintenta en el siguiente closing.
  - El parlay que nunca junta todas sus alineaciones antes de fijarse no avisa; la pestaña lo marca
    ⏳.
- **Cómo se liquida:** `resolve_parlays`, en "Shadow results", con la misma función que los picks
  (`plan_shadow_resolutions`, stake 1 al momio de PlayDoit).
  - Una pierna sin datos a los 10 días, o de un partido reprogramado (`rescheduled.VOID_SQL`), queda
    `stale` y se anula: el parlay sigue con las demás.
  - El chequeo diario la revisa como a los picks (sección 1, la liquidación contra el cálculo
    independiente; sección 3, las piernas atoradas).
- **Dashboard** (`/api/board/parlay`, `dashboard/parlay.py`):
  - los parlays de hoy y de los próximos días, uno por horario ("Parlay de las 19:00"): las piernas
    por hora, con su momio americano y 📋 (vio la alineación) o ⏳; en gris las que entran solo en uno
    más grande;
  - lo que paga, la probabilidad de que salgan todas (programa y mercado) y "📋 ya vio las
    alineaciones de todos · queda fijo a las HH:MM", "⏳ esperando alineaciones (falta 1 de 2) ·
    puede cambiar hasta las HH:MM" o "🔒 ya empezó";
  - el historial del tamaño elegido: solo los horarios que tenían las k piernas y ya liquidados, más
    cuántas piernas salieron;
  - las ligas que entran hoy, con sus unidades.
  - Como en Anteriores, una pierna con el marcador final (ESPN o `matches`) sale ✅/❌ provisional y
    el parlay no cuenta hasta la liquidación.
- **No hay días reconstruidos:** `odds_history` no guarda las líneas 1.5/3.5 ni córners y tarjetas,
  así que la regla saldría otra. El historial empieza el 3-nov.
- **Medido antes de hacerlo (3-nov):**
  - con los picks de cada partido (no la apuesta más segura) y 3 piernas, en las ligas que iban
    ganando habría salido parlay 7 de 12 días, 5 ganados (+8u); en las ligas grandes, 9 días con 2
    ganados (−1.8u). Muestra chica;
  - con la regla final, ensayada sobre el 28-oct al 2-nov con los últimos momios guardados en
    `upcoming_matches`: salieron 16 de 23 piernas (70%; el mercado esperaba ~67%). El de 2 ganó 2
    de 6 días (−2.0u), el de 3 ganó 1 de 6 (−3.3u) y el de 4 ganó 1 de 5 (−1.2u).
  - Los partidos de 60%+ apostados sueltos dejaron −3.4%, y el parlay multiplica el margen de cada
    pierna. Por eso se guarda y se mide: el historial de la pestaña dice si estas apuestas seguras de
    verdad salen.

**Parlay con bono y 🎯 de favoritos fuertes (v2.9.0, 6-nov-26).** El dueño: "necesito que encuentres
mejoras al sistema en donde sí podamos tener una ventaja ante el mercado". Lo que se encontró (todo de
solo lectura antes de tocar código):

- **Lo que PlayDoit ofrece**, revisado por la extensión de Claude en el navegador del dueño, sin apostar
  (5-nov):
  - un bono de parlay que solo se ve en el boleto: 3 selecciones 5%, 4 7.5%, 5 10%, 6 15%, 7 20%, 8 25%,
    9 35% (la barra llega a 40%), **sobre la ganancia**, cada selección a −500 o más largo;
  - "Pago Anticipado Futbol – 2 Goles de Ventaja" (etiqueta "PA"): si el equipo va 2 goles arriba en
    cualquier momento, la apuesta se paga ganada. Solo 1X2 a 90 min, antes del partido, en sencillas,
    parlays y "Crear apuesta"; no al empate. Salió en Premier, LaLiga, Serie A, Bundesliga, Ligue 1,
    Eredivisie, Portugal, Turquía, Bélgica, Grecia y Champions; **no** en Escocia. La Europa League no se
    pudo revisar y el sistema no la baja;
  - lo que no sirve: empate + menos 2.5 del mismo partido solo entra como "Crea tu apuesta" o "1x2 y
    total", con el momio ya corregido (−2% a −17% contra lo justo); el súper pago anticipado de 1 gol
    (solo México) es otro mercado con su propio momio (−4% a −11%); los momios mejorados son casi todos
    combinadas con jugadores y córners, y los de goles pierden aun mejorados (−4% a −21%).
- **El precio de PlayDoit**, medido por primera vez: en 13 favoritos paga ≈ 1% más que el promedio de las
  casas; empates igual; menos 2.5 ≈ 2% menos.
- **El sesgo del favorito:** Pinnacle sin margen (proporcional) subestima a los de 65%+ por 2.5-4 pts en
  los tres periodos (Europa 1ª división, 77.1% contra 74.3%, n=11,596); en vivo desde el 12-oct, 77.8%
  contra 73.1% (n=108). Al precio promedio quedan ≈ 0%; los momios altos pierden 10-17%.
- **El historial del parlay** (`historical_odds` 2012-2026, ligas con pago anticipado, precio = promedio,
  combinaciones al azar de cada semana; los partidos de una misma semana son independientes, verificado):
  con pago anticipado el de 4 deja +17%, el de 5 +21% y el de 6 +33%, positivo en 2012-17, 2018-22 y
  2023-26 (el más flojo); si PlayDoit pagara 3% menos que el promedio, +4% a +10%. Pega 34%, 26% y 20%.
  Hacen falta ~100 parlays para verlo en resultados reales.

Lo que eligió el dueño (1b, 2c, 3a, 4a):

- **El 🎯** (`program_picks.strong_favourite`, `decide`): solo el ganador de 65%+ en Pinnacle sin margen
  (`STRONG_FAV`) en una liga con pago anticipado (`early_payout.PA_LEAGUES`), con momio de PlayDoit de
  −500 o más largo (`MIN_FAV_ODDS` 1.20) y sin 4+ titulares fuera. En esos partidos el pick es ese
  favorito. Los demás siguen mostrando su pick, sin 🎯 (`NOT_STRONG`, o el motivo que ya tenían); el 🎯
  de goles y el del modelo se acaban. El mínimo de PlayDoit del favorito sale del mercado:
  1 / (Pinnacle + 3.5 pts) (`FAV_EDGE`: el sesgo más el pago anticipado; ≈ el promedio de las casas).
- **El parlay** (`src/models/program_parlays.py`, reemplaza al de "la apuesta más segura"):
  - piernas = esos favoritos (`strong_leg`), una por partido, marcadas `pa`;
  - **del día** (`kind` 'dia'): los más fuertes del día de México, de 4 a 6 (`DAY_MIN_LEGS`, `MAX_LEGS`);
    si sobran 4 o más, otro (`seq` 2…). Se fija 5 min antes de su primer partido, así que solo los de su
    primer horario llevan alineación: con 4+ en un mismo horario casi nunca se junta (medido del 14-oct
    al 5-nov: 0 horarios con 4; 3 en uno, una vez cada dos semanas; por día, 4+ en 1.6 días por semana);
  - **de un horario** (`kind` 'horario'): 3 o más que empiezan juntos (hasta 30 min), con las alineaciones
    de todos. Repite partidos del del día; si son los mismos, es uno solo;
  - tabla `program_parlays`, columnas nuevas `rule` ('bono'; lo de antes quedó 'seguro'), `kind`, `seq`,
    `pa`, `pa_paid`, y `UNIQUE (slot, rule, kind, seq, rank)` (`MIGRATE_BONO_SQL`, corre una vez).
- **Pago anticipado al liquidar** (`src/models/early_payout.py`): `live_scores` guarda la ventaja más
  grande de cada equipo (`home_max_lead`, `away_max_lead`) con los goles del marcador de ESPN que el
  closing ya lee; ESPN da cada gol con el equipo al que se le cuenta (en un autogol, el rival del que lo
  metió: verificado en 300 partidos, 24 con autogol) y si la suma no da el marcador queda sin dato.
  `resolve_picks` y `resolve_parlays` cobran como ganada (`pa_paid`) la apuesta al ganador de una liga con
  pago anticipado que fue 2 goles arriba. Una perdida sin la ventaja espera hasta 30 h
  (`WAIT_HOURS`); después se liquida perdida. Aplica a todo pick de ganador en esas ligas, no solo al 🎯
  (es lo que pagaría PlayDoit).
- **Aviso "Parlay listo"** (`parlay_alerts.py`): el del horario cuando todas sus piernas vieron la
  alineación; el del día cuando la vieron las de su primer horario; a 40 min de fijarse avisa igual y lo
  dice (`READY_LATE_MIN`). Trae cada partido con 📋/⏳ y "PA", lo que paga y con el bono, y la probabilidad
  de que salgan todas según Pinnacle. Estado `telegram_parlays` con clave `horario|tipo|número`.
- **Pestaña Parlay:** cada parlay del día y de un horario con sus piernas, "paga +X · con bono Y%: +Z",
  ⚡ en la pierna cobrada por pago anticipado; el historial de los liquidados (todos, del día, de un
  horario) a 1u con bono. Ya no hay botón de 2, 3 o 4. Lo de la regla anterior no se muestra.
- **Chequeo diario** (`output_checks`): la liquidación independiente acepta la ganada por pago anticipado
  (`pa_paid`); la 15 revisa que cada una tenga en `live_scores` la ventaja de 2+; la 16, que cada parlay
  con bono cumpla la regla (piernas, momio, probabilidad, liga, sin repetidos).
- Lo que no se puede: leer el precio real de PlayDoit (el widget de Altenar responde 403 a scripts: no se
  raspa). El parlay usa el promedio de las casas; el dueño ve el precio real al apostarlo.

---

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

---

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

---

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
