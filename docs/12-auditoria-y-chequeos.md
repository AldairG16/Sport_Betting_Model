# 12 · Auditoría y chequeos

## Resumen

Regla del proyecto (el dueño, 23-oct-26: "que al correr auditorías no estemos encontrando errores nuevos que
auditorías pasadas no encontraron"): **cada clase de error que ya pasó se vuelve un chequeo que corre solo**.
Una auditoría empieza por los registros de producción (GitHub Actions y la base), no por el código.

Hay cuatro capas:

| Capa | Cuándo | Qué revisa |
|---|---|---|
| **Watchdog** (`scripts/watchdog.py`) | 4 veces al día | Que la base conteste (y por qué no), que las cuotas estén al día, que cada corrida corra a su ritmo (`pipeline_runs`), el latido del analista |
| **Chequeo de salidas** (`src/utils/output_checks.py`) | Cada noche (evening) | Lo que sale: 23 chequeos, abajo |
| **Auditoría del sistema** (`scripts/system_audit.py`) | Cada lunes (weekly) y a mano | 11 secciones, abajo |
| **Auditoría semanal de sanidad** (`scripts/weekly_sanity_audit.py`) | Cada lunes | Repetidos, xG, calibración rodante, apuestas dobles, liquidación atorada, integridad numérica |

Además, cada corrida avisa sus **errores tolerados** (los que no la tumbaron) y guarda lo que leyó de la base.

### Los 23 chequeos de salidas

| # | Qué revisa |
|---|---|
| 1 | La liquidación de lo resuelto en los últimos días, contra un cálculo independiente |
| 2 | Resultados que faltan de partidos analizados (sin pospuestos ni anulados) |
| 3 | Pendientes atorados más allá de la ventana de "stale" |
| 4 | xG real fuera de escala |
| 5 | Lo aprendido por el weekly al día |
| 6, 6b | Los datos que alimentan al modelo (fechas espejo, fechas futuras, repetidos, tablas viejas) y los tiros de ESPN. `model_vs_sharp` y `program_picks` no cuentan como viejas si la última corrida no tenía partidos que analizar (`analysis_idle`, 11-nov-26) |
| 7 | El marcador provisional del dashboard (ESPN) contra el oficial |
| 8 | Ningún 🎯 a un equipo con 4+ titulares fuera |
| 9 | Córners y tarjetas de ESPN de lo jugado |
| 10 | Un partido reprogramado no cuenta dos veces |
| 11 | El 🎯 contra el mercado (una familia que gana muy por encima de lo esperado es sospechosa) |
| 12 | Cierres guardados de los partidos |
| 13 | El ritmo de créditos de The Odds API |
| 14 | Ni el pick ni el parlay se rehacen con el partido ya empezado |
| 15 | El pago anticipado cobrado fue 2 goles arriba |
| 16 | La regla del parlay con bono |
| 17 | Lo que se ve por jugar, una vez (sin horarios viejos ni nombres de antes de un alias) |
| 18 | Cada pierna de un parlay por jugar es el 🎯 de su partido |
| 19 | Al canal le llega lo que se le manda |
| 20 | Un reprogramado con la fecha que vale (y su análisis) |
| 21 | Un cierre que de golpe lee el doble de la base |
| 22 | El medidor de la base guardó algo en 24 h; modo esencial |
| 23 | El respaldo semanal en la PC tiene menos de 10 días |
| 5 (ampliado el 11-nov) | Lo aprendido por el weekly incluye la vigilancia de los supuestos del 🎯 (`favourite_watch`): si deja de correr 9 días, avisa |

### Las 11 secciones de la auditoría del sistema

1. Salidas (los chequeos de arriba; en el weekly se omite porque ya corrió en el evening).
2. Equipos próximos con historia y sin la historia partida en dos nombres.
3. Nombres de Understat y ESPN que cuadran con `matches`.
4. Lo que entró al modelo en la última corrida (cobertura de cada feature).
5. Elo sin NaN.
6. Cada liga de ESPN apunta a la liga correcta.
7. Sesgo del modelo que queda después de la corrección, y que el weekly lo siga aprendiendo.
8. Alineaciones de ESPN con su partido y su equipo.
9. Dixon-Coles al día con la historia.
10. Kalshi con su partido (a más de 10 puntos de Pinnacle es otro partido).
11. Los próximos partidos con los nombres de hoy.

`python scripts/system_audit.py` la corre a mano. Sale con código 0 si todo está bien.

<!-- detalle movido de OPERACION.md -->

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
día (`src/utils/output_checks.py`, ver "Auditoría de salidas" más abajo). A mano:
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
| Salidas y datos de entrada | Liquidación contra un cálculo independiente; resultados que faltan; pendientes atorados; xG fuera de escala; lo aprendido del weekly; fechas espejo, resultados con fecha futura, repetidos, repetidos con un equipo escrito distinto, tablas de otras fuentes viejas; marcador provisional de ESPN contra el oficial; picks contra alineaciones; finales de ESPN; un partido reprogramado contado en dos fechas; el 🎯 de cada familia contra lo que esperaba el mercado; lo cobrado por el pago anticipado; la regla del parlay con bono; lo que se ve por jugar a dos horas distintas; cada pierna de un parlay por jugar es el 🎯 de su partido | Evening (`output_checks`) |
| Equipos próximos con historia | Que tengan historia y que no esté partida en dos nombres | Weekly |
| Nombres de Understat y ESPN | Que cuadren con `matches` | Weekly |
| Lo que entra al modelo | La cobertura de cada feature en la última corrida (`model_state` `pipeline_coverage`) | Weekly |
| Elo | Ningún NaN | Weekly |
| Ligas de ESPN | Cada código apunta a la liga correcta (`chi.1` era Chile) | Weekly |
| Sesgo del modelo | Lo que queda de sesgo después de la corrección (`side_bias.residuals`), global y por región, en el 1X2, más de 2.5 y ambos anotan: avisa si pasa de 3 pt a 3 errores estándar (n ≥ 30). También si lo aprendido tiene más de 8 días | Weekly |
| Alineaciones de ESPN | Que cada alineación sea de su partido y su equipo (`espn_lineups.identity_problems`): un partido de ESPN guardado con más de dos equipos, o alineaciones que no comparten jugadores con su equipo | Weekly |
| Dixon-Coles al día | Que ningún equipo de los próximos 3 días con la historia que pide el ajuste (8+ partidos en 3 años) falte del ajuste Dixon-Coles: si falta, el ajuste es más viejo que la historia y sus partidos se saltan | Weekly |
| Kalshi con su partido | Que el 1X2 guardado de Kalshi no esté a más de 10 pt del de Pinnacle a la misma hora (`kalshi.stored_mismatches`, la foto de Pinnacle más cercana, a 3 h o menos): si lo está, es otro partido. Antes comparaba con la última foto de Pinnacle: Panathinaikos–AEK (1-nov) salía como otro partido porque Pinnacle se movió de +114 a -333 después de la última foto de Kalshi. Se limpia con `scripts/fix_kalshi_mismatches.py --apply`. La captura (`capture_kalshi`) usa la misma regla antes de guardar: solo juzga con un Pinnacle descargado hace 3 h o menos (`odds_fetched_at`). El 1-nov tiró el cierre de FC Seoul–Incheon contra un Pinnacle de 5.5 h antes | Weekly |
| Nombres de los próximos partidos | Que ninguna fila de `upcoming_matches` siga con el nombre normalizado de antes de un alias (`update_upcoming_matches.stale_norms`): sería el mismo partido dos veces. Cada recarga de momios los re-normaliza antes de limpiar duplicados | Weekly |

- Corre en el weekly (paso "Auditoría del sistema", después del ajuste Dixon-Coles); las
  salidas ya las revisó el evening.
- Cada problema es ERROR y llega a Telegram.
- A mano: `python scripts/system_audit.py` (sale con código 1 si algo falla).
- **Cuando aparezca una clase de error nueva, se agrega aquí como chequeo**, para que la
  próxima auditoría no la tenga que volver a encontrar.

---

## Watchdog

`scripts/watchdog.py`: lo dispara el latido del closing (03:00, 09:00, 15:00 y 21:00
MX) y conserva su cron `17 0,6,12,18 * * *` como respaldo independiente. Solo lee (y
crea `pipeline_runs` si falta), no gasta créditos ni tokens. **Silencio = salud**: solo
manda Telegram si algo va mal. Termina en **rojo** si la DB no responde o si su alerta
no se pudo entregar (revisa la respuesta de Telegram; un 400 por HTML se reintenta en
texto plano): el correo de GitHub es el segundo canal.

| Check | Qué mira |
|---|---|
| 1 | La DB responde. |
| 2 | Las cuotas (`MAX(updated_at)` de `upcoming_matches`) tienen menos de 26 h. Si hoy ya salió el aviso de "sin créditos" (`model_state.odds_credit_alert`), no avisa: sin créditos la tabla no se escribe aunque todo corra. La marca se borra cuando el saldo vuelve arriba del umbral. |
| 3 | Alguna apuesta real sigue `pending` 4+ días después del partido: la liquidación no corre (su timeout la pasa a `unresolved` a los 3). |
| 4 | Gap del `analyst_heartbeat` — **se omite** si `PRE_KICKOFF_ANALYST_ENABLED=false`. |
| 5 | Hay partidos futuros cargados. |
| 6 | Antigüedad de la última **candidata** en `shadow_bets` (umbral `DIAS_SIN_CANDIDATAS_ALERTA`, 4 días). En modo recolección pueden pasar semanas sin una apuesta real. |
| 7 | El **closing** corre: alerta si la última corrida tiene más de 2 h, o si hubo menos de 6 en las últimas 6 h (se esperan ~12). |
| 8 | El **weekly** corre: alerta si la última corrida tiene más de 8 días. |
| 9-10 | El **reintento de resultados** y el **resolvedor** corren: alerta si pasan 30 h sin una corrida, o si no hay ninguna registrada. |
| 11-12 | El **morning** y el **evening** corren (`pipeline_runs`): alerta si pasan 26 h sin una corrida. Antes se medía con el 2 y, sin créditos de The Odds API, el 29-oct mandó "PIPELINE SIN CORRER" cada corrida con los dos sanos. |

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

**Avisos de cada corrida (29-oct-26).** Los WARNING no van a Telegram. Al final de cada corrida
el orchestrator imprime una línea `Avisos de la corrida (modo, N distintos): …` con todos,
sin repetir (`_report_warnings`). Para auditar desde los logs, busca esa línea: un aviso que
se repite corrida tras corrida es una falla silenciosa candidata. El "⏸ en pausa" que dejó sin
cierres del 24 al 28 de octubre era un `print`, ni siquiera un aviso. Lo que importa de esos
casos se mide por su consecuencia (sección 12).

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
- partidos analizados que ya se jugaron y no tienen marcador (más del 15%), y desde el
  29-oct-26 también por liga (`leagues_without_results`): una liga con 4+ partidos a la que le
  falta la mitad o más es ERROR aunque el total pase (si se cae la fuente de una sola liga,
  las demás la diluían);
- pendientes atorados más allá de su ventana (reales 8 días, sombra, propuestas y, desde el
  29-oct-26, los picks del programa con mercado: 11);
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
- **sin créditos de The Odds API** (29-oct-26, `credits_out_today`): si hoy ya salió el aviso
  de "sin créditos", que `upcoming_matches` u `odds_history` no se actualicen es WARNING y no
  ERROR. La causa ya se avisó una vez y no debe llegar como errores nuevos cada día. Cuando
  el saldo vuelve arriba del umbral, el preflight borra la marca y todo vuelve a ser ERROR
  (`credit_budget.clear_stop`, 1-nov-26).
- **ritmo de créditos** (sección 13, 29-oct-26, `credit_budget.credit_pace`): cada día UTC guarda lo
  que bajó el saldo en `model_state.odds_credit_day.history` (14 días; el día de la renovación no
  se mide). El ✅ dice "créditos ayer: X de Y". Si al ritmo de los últimos 3 días el saldo cruza
  el umbral de parada 1+ días antes de la renovación, es ERROR. En octubre habría avisado el 15,
  cuando faltaban ~6 días de créditos. Sin créditos (aviso de hoy), es WARNING.
- **cierres guardados** (sección 12, 29-oct-26, `closing_coverage`): de los partidos sombra
  jugados entre 27 y 3 horas antes, al menos 80% con un cierre válido (10+ partidos). Lo
  normal es 98-100%. Si el aviso de "sin créditos" salió hoy, es WARNING y no ERROR.
- **picks en juego** (sección 14, 5-nov-26, `repicked_after_start`): ningún pick ni pierna de
  parlay de los últimos 3 días guardado (`picked_at`) más de 2 min después del inicio real, la
  hora más temprana entre la del sistema y la de ESPN (`espn_kickoffs`). Los reconstruidos se
  saltan: se guardan después del partido a propósito. Ver [16](16-lecciones-cuotas-y-creditos.md), "La hora de The Odds API puede
  venir tarde".

Al final de cada corrida del pipeline, `pipeline_invariants` revisa los goles esperados en
el tope (más del 5% de los partidos), los partidos a más de 20 pts de Pinnacle (más del
10%) y la distancia típica a Pinnacle (más de 9 pts). Con el xG mal escalado, los tres
habrían avisado el 28-sep.
