# 18 · Auditorías de noviembre de 2026

Lo que encontraron las auditorías del 6 y del 9 de noviembre comparando el dashboard con la base: fantasmas de reprogramados, nombres gemelos, repetidos con un día de diferencia y pospuestos con la fecha equivocada. Cada defecto quedó con su chequeo. La caída de Neon del 9-nov y lo que se hizo para que no vuelva está en [06](06-base-de-datos.md).

<!-- detalle movido de OPERACION.md -->

**Auditoría del 6-nov-26 (v2.9.1).** El dueño: "corre una auditoría completa del sistema, necesito que me
entregues el sistema 100% funcional sin ningún bug silencioso". Los logs de producción (60 closings,
morning, evening, weekly, resultados, resolvedor, watchdog), `system_audit` (11/11), `db_smoke_test`
(36/36) y todos los endpoints del dashboard estaban en verde; lo que se encontró revisando lo que sale:

- **El horario viejo de un reprogramado se seguía viendo.** Tigres vs Cruz Azul pasó de las 23:00 a la
  01:00: `void_rescheduled` anuló el viejo, pero Próximos (`visibility.predictions`), "Partidos de hoy"
  (`today_board._SQL`) y el conteo de picks lo mostraban otra vez, y `build_parlays` no revisaba
  `skipped_at` (un partido de liga con pago anticipado habría entrado dos veces al parlay, o con un
  análisis que ya no vale). En Anteriores, Lyon vs Angers, PSG vs Lyon y América vs Pachuca salían con el
  pick anulado en vez del de verdad, según el orden en que llegaran de la base: "Ganadas y perdidas" no
  contaba 3 picks ganados. Ahora `side_bias.drop_phantoms` quita la fila anulada cuando el partido tiene
  otra que vale (la que no tiene otra se queda: su "sin pick" se ve), el parlay salta toda fila con
  `skipped_at`, los tableros y el aviso de alineación filtran `result = 'stale'` y Anteriores prefiere el
  pick de verdad (`board._shown_rank`). El closing anula el horario viejo antes de guardar los picks
  (`step_program_picks`); antes solo el evening y los resultados.
- **Antes del partido, el horario bueno se decidía por un empate.** El closing reescribe cada 30 min el
  pick de las dos fechas con el mismo `NOW()`, y en empate ganaba la más tardía: un partido adelantado
  habría anulado su horario real y le habría dado su resultado al viejo. Ahora decide ESPN, luego el feed
  de momios de ahora (`upcoming_matches`, `phantom_dates(feed=)`) y solo al final lo escrito.
- **El nombre de antes de un alias contaba doble.** Santos Laguna vs Pumas (22-oct) y Paderborn vs
  Stuttgart (10-oct) estaban con el nombre viejo y el nuevo a la misma hora en picks, sombra y
  `model_vs_sharp`: doble en el sesgo aprendido, el filtro por región y los totales. `alias_twins` anula
  la fila del nombre viejo solo en la tabla donde está la del nuevo (las propuestas de Santos vs Pumas
  solo tienen el viejo y se quedan).
- **Un partido repetido con un equipo escrito distinto vivía hasta 7 días.** Preston vs "sheffield utd"
  entró el 3-nov junto a Preston vs Sheffield United; la fusión (con respaldo) solo corría el lunes en el
  weekly y el partido contaba doble en la forma de Preston. Ahora corre en el evening y en los
  resultados antes de liquidar (paso "Partidos repetidos"), "sheffield utd" es alias de "sheffield
  united" y el chequeo de datos cuenta los que la fusión no unió (`renamed`).
- **El pick de antes de la alineación se liquidaba sin el pago anticipado**: un ganador que fue 2 goles
  arriba contaba perdido antes y ganado después, y "CAMBIOS CON LA ALINEACIÓN" salía cargado.
- **El xG real dependía solo de Understat**, caído desde el 2-nov (su página dice "We will come back
  soon"): el del 26-oct vencía el 16-nov y desde ahí las 5 grandes iban con el xG estimado con tiros.
  Ahora la liga que Understat no da se pide a FotMob (`soccerdata_feed.fetch_fotmob`: JSON estático,
  sin bloqueo; FBref y Sofascore responden 403 a scripts y no se usan). Cubre los 96 equipos de los
  próximos partidos de las 5 grandes; contra el Understat del 26-oct correlaciona 0.96 (a favor) y 0.91
  (en contra), y su promedio (1.55 por equipo y partido) está más cerca de los goles reales de la
  temporada (1.53) que el de Understat (1.68). Si el mismo equipo queda con las dos fuentes, vale la
  fila más reciente (`real_xg_of`). El pipeline cuenta los dos como xG real (`xg_source_label`).

Chequeos nuevos: `renamed` en los datos de entrada, la 17 (lo que se ve por jugar, una sola vez) y la 18
(cada pierna de un parlay que todavía no se fija es el 🎯 de su partido, `legs_off_target`). Con dos
fuentes de goleadores, el mismo jugador puede quedar con la fila vieja de Understat y la nueva de FotMob:
`scorer_model._merge_club_scorers` toma la más reciente. La fusión de repetidos se probó de punta a punta
contra Postgres en un esquema aislado (borra el repetido, pasa sus datos a la fila que queda, lo respalda
y la segunda corrida no hace nada).

**Repetidos con un día de diferencia o con fecha vieja (auditoría 9-nov-26).** La auditoría del
sistema dio "1 partidos repetidos". Era Nacional vs Académico de Viseu, que pasaron del sábado 7 al domingo
8. The Odds API mandó el 1-0 con la fecha de antes y football-data con la buena. La fusión
(`weekly_sanity_audit.find_duplicate_pairs`) tenía dos huecos:
- **Solo juntaba el mismo día**, así que el repetido no se iba nunca, aunque el chequeo
  (`data_health 'duplicate'`) mira ±1 día.
- **Miraba 60 días por la fecha del partido.** Al buscar en toda la historia salieron otros 27 que nadie
  veía:
  - 19 partidos de China de 2023-25 repetidos como "China" y "China PR", metidos por una carga de historia
    ~20-oct;
  - 8 del Mundial de junio, con mayúsculas y sin ellas, el mismo día o uno después.

Los dos contaban doble en la historia de los equipos y de sus rivales (forma, Elo, Dixon-Coles). Ahora:
- **La fusión junta también un día de diferencia**, con más condiciones: misma liga de fútbol, mismo
  marcador, un equipo escrito igual y el otro parecido. Un equipo no juega dos partidos de liga en días
  seguidos. Queda la fila con más datos; si empatan, la fecha de después, porque un partido se pospone,
  no se adelanta.
- **La fusión y el chequeo miran toda la historia.**
  - El chequeo cuenta en la base, ~6 s.
  - La fusión lee completos solo los últimos 60 días. De lo de antes, la base le pasa solo los candidatos
    (`_OLD_CANDIDATES_SQL`: un equipo escrito igual, ±1 día y el mismo marcador), que son unos cuantos.
  - La primera versión (#124) leía los ~120 mil partidos en cada corrida de resultados, y ese mismo día
    Neon cortó la base por pasarse de la cuota del plan gratis (v2.11.1 lo corrigió).
  - La búsqueda usa diccionarios: con el groupby de pandas, toda la historia tardaba 75 s.
- **Béisbol nunca.** `matches` guarda la MLB de 2021-25, y ahí una serie juega días seguidos y un doble
  juego el mismo día, con los mismos equipos y a veces el mismo marcador. Con la regla del chequeo
  salían unos 90 que no lo son.
- **La descarga de marcadores ya no vuelve a meter la fecha vieja** (`fetch_results._INSERT_SOCCER`):
  en fútbol, el INSERT no entra si ya está el mismo partido ±1 día con el mismo marcador. `ON CONFLICT`
  solo ve la misma fecha, y la siguiente descarga lo traía de vuelta. TheSportsDB usa el mismo INSERT.
- **Los 28 que había los fusiona la primera corrida con la regla nueva** (paso "Partidos repetidos"),
  con respaldo en `matches_identity_backup`.

**Pospuestos y reprogramados con la fecha equivocada (v2.11.1, auditoría 9-nov-26).** Siguiendo por qué
Portugal y Noruega se pedían en cada corrida de resultados, salieron tres defectos silenciosos:
- **ESPN da un pospuesto por terminado sin jugarse** (estado `post`, sin terminar, "Postponed", 0-0).
  `live_scores.parse_live` lo guardaba `live` 0-0, así que el dashboard lo mostraba en vivo. Ahora se
  guarda `postponed`, sin marcador, y el dashboard dice "se pospuso" (⏸).
- **Ese horario pospuesto valía como la fecha de ESPN de un reprogramado.** Nacional vs Académico de
  Viseu pasó del 7 al 8-nov. `rescheduled.void_rescheduled` se quedó con el 7: anuló el pick y el
  análisis del 8, y el pick del 7 se cobró (perdido) con el 1-0 del 8. Ahora la fecha de ESPN no
  cuenta un pospuesto.
- **Anular no tenía vuelta.** Kristiansund vs Vålerenga pasó del 7 al 9-nov:
  - a las 15:02 del 7 ya había empezado la primera fecha y valía la escrita más recientemente, la del 7,
    así que se anularon las 8 candidatas sombra del 9;
  - a las 15:55 el feed ya solo traía el 9 y se anularon las del 7. El partido real se quedó sin ellas.

  Ahora, en la fecha que vale, se recupera (`RESTORE_SQL`) lo que se anuló ANTES de su partido. Solo esta
  regla anula antes del partido: las demás ("sin datos") lo hacen días después, así que son justo las
  que anuló una decisión anterior.
- **El análisis también se recupera (11-nov-26).** En `model_vs_sharp`, `skipped_at` lo pone además
  `side_bias.mark_skipped` cuando la última corrida ya no evaluó el partido. Por eso la anulación de esta
  regla deja `skip_reason = 'reprogramado'` y solo eso se recupera; `mark_skipped` y un análisis nuevo lo
  dejan en NULL. Lo descubrió la reparación del 11-nov: Nacional vs Académico de Viseu recuperó su pick
  del 8, pero su análisis del 8 (anulado por el morning del 8-nov a las 12:04) seguía fuera del
  aprendizaje. Se recuperó a mano; era el único en todo el historial de reprogramados. El chequeo 20 lo
  ve en `VOIDED_BEFORE_SQL`.

Además:
- El horario viejo de un reprogramado y un pospuesto ya no cuentan como "marcadores que faltan"
  (`output_checks.PLAYED_ANALYSIS_SQL`). Antes hacían pedir su liga en cada corrida de resultados, a 2
  créditos cada una.
- **Chequeo nuevo §20** (`rescheduled.check_dates`, en la noche):
  - filas anuladas en la fecha que vale (quedan solo si la recuperación falló);
  - picks cobrados de un horario que ESPN dio por pospuesto.
- Los datos de antes se arreglaron una vez: los 2 pospuestos de `live_scores` (Nacional el 7, Estoril
  vs Marítimo el 8) pasaron a `postponed`, y `void_rescheduled` recuperó el 8 de Nacional y el 9 de
  Kristiansund, y anuló el 7 de Nacional.
