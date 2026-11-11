# 15 · Lecciones: datos y fuentes

Cada punto es un error que ya pasó en producción con los datos de entrada (football-data, ESPN, Understat, nombres de equipos, fechas, temporadas), con su fecha y su arreglo. Léelo antes de tocar un cargador o una fuente. Lo que sigue vigilando cada clase está en [12](12-auditoria-y-chequeos.md).

<!-- detalle movido de OPERACION.md -->

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
Ahora salen de football-data (`/new/USA.csv`, `MEX`, `BRA`, `ARG`), sin tiros ni córners:
desde el 2-nov-2026 esos salen de ESPN (ver "Tiros y córners de ESPN"). Se agregaron 6,016
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

---

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

---

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

---

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
  `xg_proxy.leagues_with_shots` = las ligas con tiros en `matches` el último año: las 19 de
  football-data y, desde el 2-nov-2026, las 8 que los toman de ESPN (ver "Tiros y córners de
  ESPN", abajo).
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
  - `team_xg_real`: 21 días, lo que sirve el xG (`soccerdata_feed.STALE_DAYS`);
  - `club_elo`: 120 días, lo que sirve la foto (`clubelo.MAX_AGE_DAYS`).

  Una tabla que todavía no existe (instalación nueva) no cuenta.
- `output_checks.espn_shots_gaps`, diario: ERROR si una de las ligas que toman tiros de ESPN
  tiene tiros en menos de la mitad de sus partidos de los últimos 30 días (con 10 o más).

**Tiros y córners de ESPN (2-nov-2026).** La MLS no tenía un tiro en 512 partidos del último
año, Argentina el 5%, y Brasil y México solo ceros (0 tiros, 0 a puerta y 0 córners de los dos
equipos, de un cargador viejo; xG, tiros y córners filtran `> 0`, así que eran inertes). Suecia,
Noruega, Japón y China, nada. Sin tiros el modelo no tenía xG estimado, tiros ni córners en
esas ligas, y ahí el 🎯 perdía: MLS 8 de 23 (−10.03u), Argentina −2.30u, México −1.35u, contra
+5u de la EPL y de la Serie A.
- `src/features/espn_stats.py` lee el scoreboard mensual de ESPN de `STATS_LEAGUES`
  (`totalShots`, `shotsOnTarget`, `wonCorners`) y completa las filas de `matches` sin tiros
  (NULL o los cuatro en 0). Empareja por fecha ±1 día, mismo marcador y los dos nombres
  parecidos; si dos filas se parecen casi igual, no adivina. Nunca pisa un dato, y copia antes
  cada fila a `matches_identity_backup` (acción `espn_stats`).
- El evening refresca los dos últimos meses (paso "Tiros y córners (ESPN)", antes de
  "Results"). La historia (26 meses) se carga una vez: `python -m src.features.espn_stats
  --months 26 --apply`; sin `--apply`, en seco.
- **El xG de esas ligas se calibra con sus goles.** Con las tasas de football-data, el xG
  estimado daba 1.31 veces los goles reales en Argentina, 1.19 en Brasil y 1.10 en Suecia (la
  MLS 1.02); entra al ataque y la defensa como goles por partido, e inflado sobrevalora los
  overs (el sesgo de sep-26). `xg_proxy.espn_league_factors` mide esa relación por liga en el
  último año y divide su xG por ella; sin 100 partidos para medirla, sus tiros no entran al
  xG. Las ligas de football-data no se tocan (0.98-1.14), y el chequeo semanal del xG
  (`weekly_sanity_audit.audit_xg_sanity`) las mide solo a ellas.
- La K-League no está en ESPN: sigue sin tiros.

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
