# 07 · El modelo

## Resumen

Para cada partido, el modelo calcula los goles esperados de cada equipo y, de ahí, la probabilidad de cada
mercado: ganador (1X2), más/menos de goles (1.5, 2.5, 3.5), ambos anotan, handicap asiático, empate no
apuesta, doble oportunidad, córners y tarjetas. Lo corre `src/pipeline/prediction_pipeline.py`
(`run_prediction_pipeline`) en el morning y en el avance de mañana del evening.

**Las piezas** (`src/models`, `src/features`):

| Pieza | Qué aporta |
|---|---|
| **Dixon-Coles** (`dixon_coles_model.py`, ajuste semanal en `dc_mle_fitter.py`) | Ataque y defensa de cada equipo, ventaja de local y la corrección de marcadores bajos (rho), ajustados por máxima verosimilitud con 3 años de `matches`. El 11-nov-26: 859 equipos, ventaja de local 0.21, rho -0.051 |
| **Ensemble** (`ensemble_model.py`) | Combina Dixon-Coles con Poisson, Elo (`elo_rating.py`), xG (`xg_proxy.py`, `soccerdata_feed.py`), entre-ellos (`h2h_stats.py`) y forma (`team_form.py`). Sin Dixon-Coles confiable el pick queda "informativo" (`MIN_DC_WEIGHT`) |
| **Ajustes de contexto** | Fuerza de liga (ClubElo) para partidos entre ligas, motivación de fin de temporada, congestión de partidos, clima |
| **Córners y tarjetas** (`props_fair.py`) | Modelo calibrado aparte: promedio de la liga en el año y los últimos 20 partidos de cada equipo |
| **Alineaciones** (`espn_lineups.py`, `lineup_alerts.py`) | Titulares habituales (70%+ de los últimos 15 partidos) y goleador; con 4+ fuera no hay 🎯 y se rehace el pick |
| **Comparación con el mercado** (`market_odds.py`, `pinnacle.py`, `sharp_reference.py`) | Quita el margen de las casas y compara con Pinnacle, la referencia |
| **Calibración** | Por liga y por mercado (`config/calibration_factors.json`, `market_calibration.py`) y el sesgo aprendido por lado y región ([09](09-aprendizaje.md)) |

**Lo que se sabe del modelo (auditoría del 5-nov-26):** es más plano que Pinnacle y no le gana prediciendo
(log loss 1X2 0.956 contra 0.946; en goles no aporta). Por eso el peso que el sistema le da frente a Pinnacle
es bajo (~0.07-0.15) y el 🎯 depende de Pinnacle y del precio, no de la opinión del modelo. Ideas ya
probadas que **no** funcionan: "estirar" el modelo, apostar antes de que la línea se mueva, el sesgo en
más/menos, córners y tarjetas contra el mercado.

<!-- detalle movido de OPERACION.md -->

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

---

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

---

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

---

### Fuerza de liga (`src/features/clubelo.py`, 22-oct-2026)

El programa mide a cada equipo dentro de su liga. En Champions, Europa League y Conference
eso no alcanza: el 21-oct le dio a Viking 20% contra el Aston Villa (Pinnacle 10%) porque
mete 2.3 por partido en Noruega.

- **Datos.** `refresh_if_stale()` corre en el morning (paso "ClubElo (fuerza de liga)",
  antes de Predictions) y, si la última foto tiene más de 6 días, lee las páginas públicas
  de ClubElo de 53 países (tabla `club_elo`, una foto por fecha). Un país que falla se salta;
  ninguno → aviso (ver abajo). Una foto de más de 120 días no se usa (`MAX_AGE_DAYS`; el
  chequeo de datos `FEED_MAX_AGE_DAYS["club_elo"]` usa el mismo número).
  - La API de datos de ClubElo responde 502 desde el 22-oct-26 y la de partidos dice
    "Fixtures API deactivated": por eso las páginas.
  - **Desde el 29-oct-26 las páginas piden un reto anti-bots de Cloudflare** ("Just a
    moment…", 403, `Cf-Mitigated: challenge`). No se salta. Tres 403 seguidos
    (`BLOCK_PROBE`) dan a ClubElo por bloqueado y no se piden los otros 50 países.
  - Sin foto nueva sigue la última: la del 22-oct sirve hasta el 19-feb. Es WARNING mientras
    le queden más de 7 días (`WARN_DAYS_LEFT`) y ERROR a Telegram la última semana, con
    tiempo para buscar otra fuente. Antes `MAX_AGE_DAYS` era 10: desde el 1-nov los
    partidos de Champions se habrían quedado sin ajuste, y el morning mandaba un ERROR
    cada día.
  - Por qué 120 días sirven (2-nov-26; antes 45): la fuerza de una liga (Elo promedio de sus
    5 mejores clubes) se mueve unos pocos puntos al mes, y pesa con `GAMMA` = 0.2. Un club
    grande cambia 50-100 puntos en un año (FootballDatabase, 1-nov-26); el promedio de 5,
    menos. En 4 meses son ~15 puntos, que con `GAMMA` pesan 3: los goles esperados cambian
    menos de 0.5%. El 2-nov la API seguía en 502 y las páginas en 403; con 45 días la foto
    vencía el 6-dic y desde el 29-nov salía un ERROR diario sin nada que hacer.
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
