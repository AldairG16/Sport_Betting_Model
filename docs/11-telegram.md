# 11 · Telegram

## Resumen

El bot le escribe al dueño (`TELEGRAM_CHAT_ID`) y copia lo de los partidos al **canal «Dulopatia»**
(registrado desde el 8-nov-26 reenviándole al bot un mensaje del canal; se guarda en `model_state`). Todo en
palabras normales y momios americanos.

| Mensaje | Cuándo | A quién | Módulo |
|---|---|---|---|
| **Bloque de la mañana** | En la primera revisión desde las 06:00: todos los partidos de hoy de antes de las 12:00, cada uno con su pick, "🎯 RECOMENDADO" y su momio mínimo o "solo para seguir", ✅ si ya tiene alineación, y los parlays que empiezan ahí | Dueño | `match_blocks.py` |
| **Bloque de la tarde** | En la primera revisión desde las 12:00: los de las 12:00 hasta las 06:00 del día siguiente | Dueño | `match_blocks.py` |
| **Con alineación** | En cada revisión, un mensaje nuevo con los partidos que acaban de tener alineación (1, 3, los que haya), con su pick ya revisado y "antes: …" si cambió desde el bloque | Dueño y canal | `match_blocks.py` |
| **Último aviso** | En la última revisión antes del partido: el que cambió después de mandarse ("cambió a última hora") y el que nunca tuvo alineación (su pick final) | Dueño y canal | `match_blocks.py` |
| **Parlays finales** | En la última revisión antes de su primer partido | Dueño | `final_alerts.py` |
| **La noche** | ~21:05: cómo terminó cada pick y cuántos partidos hay mañana | Dueño | `notify_telegram.py`, `telegram_reports.py` |
| **La semana** | Lunes, con el weekly | Dueño | `telegram_reports.py`, `weekly_narrator.py` |
| **Avisos de la base** | Al pasar del 50% y del 75% del plan gratis y al cambiar de modo | Dueño | `db_budget.py` |
| **Errores tolerados** | Al final de una corrida con errores que no la tumbaron | Dueño | `orchestrator.py` |
| **Chequeo de salidas** | Si algún chequeo diario falla | Dueño | `output_checks.py` |
| **Watchdog** | Solo si algo dejó de correr o la base no contesta (4 veces al día como máximo) | Dueño | `watchdog.py` |
| **Créditos de The Odds API** | Una vez al día, si se acaban | Dueño | `credit_budget.py` |

**El revisor de capturas** (`playdoit_checker.py`, siempre escuchando en tandas de ~5 h 45 min): si el dueño
le manda una captura de PlayDoit, Claude Haiku la lee y dice si el precio conviene. Desde el 16-nov-26 también los
boosts y las combinadas (varias cosas en un solo momio): cuenta cada cuánto salieron todas juntas en partidos
parecidos del historial ([08](08-picks-y-parlays.md)). También registra el canal.

**Capturas grandes (23-nov-26).** La primera captura real del dueño, una lista de 8 partidos con 7 a 9 momios
cada uno (más de 60), dio "no pude leer la captura (stop=max_tokens)". El tope era 6,000 tokens: cada momio ocupa
unos 100, y Haiku 5.5 razona por defecto dentro de ese tope. El arreglo:
- tope de 32,000, con streaming (el SDK no deja un tope así sin él) y esfuerzo bajo, porque leer momios no
  necesita razonar. A $0.50 por millón de tokens de salida, el tope cuesta unos 1.6 centavos de dólar;
- Claude ya no escribe los campos vacíos, y la descripción solo en boosts y combinadas;
- si aun así se corta, se usan los momios que alcanzó a leer completos (`salvage_items`) y se le dice al dueño
  que mande aparte lo que falte.

Ante un 429 de Telegram se espera lo que pide (`retry_after`). Una corrida que no logra mandar su mensaje sale
en rojo.

### Los partidos por bloques (20-nov-2026)

El dueño, al ver "Partidos de hoy" (un mensaje fijado que se editaba cada 30 min) y luego "Hoy no hay
recomendados… No hace falta apostar": "esto se me hace una incongruencia y estar llenando de basura el canal,
necesito que cada que mires el partido se mueve mandes ese partido no que me actualices el mismo mensaje, se
pierde … tampoco quiero el mensaje de no hay partidos recomendados … mejor en los partidos dime cuál es
recomendado y cuál no … de las 12 pm para abajo mándame unos y de las 12 para arriba otros … si el partido ya
tiene alineación ponlo con una palomita en verde". Eligió:

- (1a) cada partido cuando sale su alineación, con aviso si cambia en los últimos 30 min;
- (2a) todos los partidos en esos mensajes, cada uno recomendado o no;
- (3) un bloque con todos los partidos de antes de las 12:00 y otro con todos los demás;
- (4) al canal, solo los partidos cuando ya tienen alineación.

Cómo quedó (`src/models/match_blocks.py`, paso "Partidos por bloques (Telegram)" del closing). **Todo son
mensajes nuevos: nada se edita ni se fija.**

- **Los bloques del día** salen en la primera revisión desde las 06:00 y desde las 12:00 (hora de México), una
  vez al día cada uno (`model_state` 'telegram_blocks').
- **Cada partido sale una vez** con su alineación o en el último aviso, y otra vez solo si cambia.
  - Un partido que ya tiene alineación cuando sale su bloque llega en los dos: el canal no recibe los bloques.
  - La alineación se ve casi siempre en la última revisión (mediana 28 min antes, medido el 8-nov), así que el
    mensaje "con alineación" suele ser ya el pick final.
- **Se quitaron:**
  - el mensaje de la mañana y el avance de mañana (`notify_best_bets`, `send_tomorrow_preview`);
  - el tablero fijado "Partidos de hoy" (`today_board.py`; la primera corrida lo quita de arriba del chat y
    del canal, `retire_board`);
  - los picks finales de cada 🎯 (`final_alerts` quedó solo con los parlays, al dueño);
  - la copia de la noche al canal;
  - el "🎯 Recomendados en el mensaje de hoy" del aviso de fallas de la mañana.
- **La noche** ya no dice "por ahora sin 🎯": dice cuántos partidos hay mañana y cuándo llegan.
- Un sábado normal (7-nov) tuvo 80 partidos en 26 horarios: el bloque de la mañana del 21-nov tiene 46.

### Antes: "Partidos de hoy" en Telegram (30-oct al 19-nov-2026)

<!-- detalle movido de OPERACION.md -->


**Desde el 14-nov-2026 (v2.13.0)** el tablero separa lo que es para apostar de lo que no: el 🎯 lleva su momio
mínimo ("🎯 Gana Arsenal · mín. -260"); los demás picks dicen "solo para seguir" y ya no llevan mínimo, porque
con un mínimo al lado parecían una recomendación y esos picks quedan tablas con las casas. El pie lo repite:
"🎯 = para apostar, si PlayDoit paga el mínimo o más. ⭐ = de los más firmes (un grande de su liga que viene
ganando). Lo demás es solo para seguir el partido." Desde v2.14.0 el 🎯 de un grande que viene ganando lleva ⭐
en el tablero y en el pick final.

El dueño quiere ver en el teléfono los partidos del día, con su hora, y notar si un pick
cambia. `src/models/today_board.py`, paso "Partidos de hoy (Telegram)" del closing (después
de los picks y las alertas de alineación):
- **Un mensaje que se edita** en cada closing (`notify_telegram.edit_or_send`, sin sonar): los
  partidos de hoy (hora de México) que no han empezado.
  - Cada uno con su hora de inicio, su pick (🎯 si pasa la regla), el momio mínimo de PlayDoit,
    de cuándo son sus cuotas (`cuotas 13:00`, o `28 oct 03:31` si son de otro día) y
    "alineación ✓" si el pick ya la vio.
  - Al empezar el día va uno nuevo, fijado arriba del chat (`pin_message`, mejor esfuerzo). Si
    no cabe (3,800 caracteres), va en varios.
  - Estado en `model_state.telegram_today` (día e ids).
- **🔄** en la lista si el pick cambió en las últimas 24 h, con "cambió HH:MM · antes: …".
  `snapshot_picks` anota el cambio (`program_picks.prev_label`, `prev_star`, `changed_at`):
  cambia el texto del pick o gana/pierde el 🎯. La reconstrucción de picks pasados no lo
  anota (`_upsert(track_changes=False)`).
- **Un aviso aparte, con sonido**, cuando cambia el pick de un partido que empieza en las
  próximas 6 h (`CHANGE_ALERT_HOURS`). Cada cambio sale una vez (`model_state.telegram_pick_changes`).

"Actualizar" es abrir el chat: el pick no cambia más rápido que el closing (cada 30 min).

---

**Mensajes de la mañana, de la noche y semanal con los picks del programa (6-nov-26).** El dueño, del
resumen de la noche: "cámbialo también". Al revisarlo salió algo peor que las palabras: los tres mensajes
leían `bets_history`, que dejó de llenarse con el modo colección (25-sep-26). Desde entonces la mañana
decía cada día "No apuestes nada: el sistema sigue recolectando datos en sombra", la noche "Sin apuestas
para hoy" y el semanal "Sin apuestas jugadas esta semana", aunque el dashboard y "Partidos de hoy"
recomendaban 🎯 y parlays. Ahora los tres salen de `src/models/telegram_reports.py`, que lee lo mismo que
el dashboard: `program_picks` (🎯 = sin `reason`) y los parlays con bono de `program_parlays`, armados con
`dashboard.parlay.parlay_board`.
- **Mañana** (`notify_best_bets`): los 🎯 de hoy con "apuesta si PlayDoit paga X o más" (`min_us`), los
  parlays con lo que pagan y "se gana más o menos 1 de cada N veces", y cuántos partidos más hay sin 🎯.
  - **Sin 🎯 todavía** (19-nov-26, `no_targets_text`). El dueño: "mandemos los partidos pero después mandemos
    otro donde dice que no hay partidos recomendados, no sabemos si habrá partidos recomendados porque no
    tenemos las alineaciones". Antes decía "Hoy no hay recomendados… No hace falta apostar" como si fuera
    seguro.
  - Un partido de una liga con 🎯 (pago anticipado o las 12 de Europa y Asia) todavía puede serlo: el pick se
    rehace con los momios y la alineación hasta unos 30 min antes, y si queda 🎯 sale en los picks finales.
  - Ahora dice "por ahora no hay recomendados" y cuántos partidos todavía pueden tener 🎯. Solo dice "no
    habrá" cuando ningún partido es de una liga con 🎯 (el 19-nov: Brasil y la MLS).
- **Noche** (`send_evening_summary`): cómo terminó cada 🎯, cada parlay, cada uno de los demás picks (desde el
  18-nov; antes iban en una sola línea), los 🎯 desde el 22 oct (111 de 203 el 6-nov, lo mismo que el
  dashboard) y cuántos 🎯 hay mañana.
  - **Partidos que terminan tarde** (18-nov-26): a las 21:05 de México los de las 18:30 o más tarde todavía
    no se cobran (la liquidación espera 3 h y el resultado oficial). La noche los dejaba fuera sin decirlo:
    el 18-nov dijo "4 ganados y 0 perdidos · +177", con Corinthians 4-0 y Grêmio 0-0 perdidos (+77).
  - Ahora van con el marcador final de ESPN (`with_espn_finals`, la misma regla del dashboard): "perdido
    según ESPN" y "(2 con el marcador final de ESPN: falta el resultado oficial)". El que no tiene ni eso
    se cuenta aparte ("· 1 todavía sin resultado") y sale con ⏳.
  - Lo acumulado desde el 22 oct sigue con lo cobrado, como el dashboard.
- **Semanal** (`send_weekly_report`): la semana de 🎯 (mejor y peor liga), los parlays y los demás picks,
  lo acumulado y los cambios con la alineación (`lineup_changes.format_lines`, ahora con "¿Conviene cambiar
  el pick con la alineación?" en vez de z-scores).

La ganancia se dice "apostando 50 a cada uno" (hasta el 8-nov-26, 100): el pick a 1 unidad al momio de PlayDoit × 50. Si la base
falla no se manda nada (antes salía "Sin apuestas"). El aviso de una corrida con pasos fallidos (`send_health_check`) ya no
muestra "Bankroll 63.40u (ROI …)" ni las "pending" de `bets_history`: dice cuántos pasos fallaron, los 🎯
del mensaje de la mañana, los créditos y cuánto tardó. Se quitaron los formatos viejos de `bets_history`
(`format_bets_message`, `_build_bets_by_league`, `_diversified_top`, `_tomorrow_line`, `NO_PICKS_TEXT`). Siguen
en jerga técnica los avisos para el que mantiene el sistema: calibración, laboratorio de estrategias, salud.

**Picks finales en Telegram: un solo aviso por partido (v2.11.0, 8-nov-26).** El dueño: "me llegan muchas alertas
de cambios de pick, a veces también son 2 veces antes del partido, y todo eso se me pierde en el telegram, quiero
que solo llegue el último cambio, necesito que revises cuánto tiempo antes del partido cambia el pick para que ese
sea el límite… y si hay parley también mandarlo".

Lo medido en 281 partidos (31-oct a 7-nov, `program_picks.changed_at`): el último cambio llega hasta la última
revisión antes del partido y nunca después.
- 30 de esos cambios fueron entre 5 y 35 min antes.
- No hay cambios más tarde porque el closing corre a las :00 y :30 (cron-job.org) y a `KICK_LOCK_MIN` del partido
  ya no lo toca.
- La alineación se ve casi siempre en esa revisión (mediana 28 min antes).
- El sábado 7-nov llegaron 22 avisos de "Cambió el pick" (36 cambios), 2 de parlay y hasta 11 de alineación.

El dueño eligió solo 🎯 y parlays, en la última revisión. `src/models/final_alerts.py`, paso "Picks finales
(Telegram)" del closing, después de "Picks del programa":
- **Cuándo** (`cutoff`): van los partidos que ya no toca ninguna revisión regular, los que empiezan antes de la
  siguiente (:00 o :30) + `KICK_LOCK_MIN`. Un partido de las 13:00 sale en la de las 12:30 (~28 min antes); uno de
  las 13:15 o 13:45, ~13 min antes.
- **Qué**: un mensaje por corrida, y solo si hay algo que decir.
  - Los 🎯, con el momio mínimo, ⚡, si ya vio la alineación y quién falta (de `lineup_alerts`).
  - Los que salieron 🎯 en el mensaje de la mañana o en el avance de la noche y ya no lo son, con el porqué.
  - Los parlays con bono que se fijan ahí (`parlay_alerts.alert_text`). Desde v2.15.0 también los de la
    escalera del fin de semana ("Parlay de 3 del fin de semana #2", "Parlay de 9 del fin de semana"), con la
    nota de que cada uno se apuesta aparte.
  - El 🎯 que no estaba en la mañana va marcado "nuevo". Solo en un día cuyo mensaje quedó guardado:
    `notify_telegram._remember_announced` guarda en `model_state 'telegram_announced'` los 🎯 y el día.
- **Una vez**: cada partido y cada parlay sale una sola vez (`model_state 'telegram_final_picks'`). Si una corrida
  de más (los cron de GitHub) lo cambia antes del partido, sale otra vez marcado "cambió a última hora". Si
  Telegram falla, no queda como mandado y se reintenta en la siguiente corrida.
- **Lo que ya no sale**:
  - "Cambió el pick": `today_board` solo edita "Partidos de hoy", sin sonar, con 🔄.
  - "Parlay listo" y "Cambió el parlay": `parlay_alerts` ya no manda nada, solo arma el texto.
  - El aviso de alineación aparte: `run_lineup_alerts(notify=False)` la guarda y la revisa con Claude, y el
    dashboard la muestra.
- Los textos de la mañana y los del dashboard (pestaña Parlay) dicen cuándo llega el pick final.

---

**El canal de los partidos (8-nov-26).** El dueño: "como puedo unir a personas a mi chat de telegram donde mandamos
los partidos". El chat del bot es privado y además lleva lo que es solo del dueño (avisos técnicos, el revisor de
capturas), así que eligió "un canal solo para los partidos". `src/models/telegram_channel.py`:
- **Cómo se guarda, sin copiar números ni tocar secretos o workflows:**
  1. El dueño crea el canal y agrega al bot como administrador (publicar, editar y fijar).
  2. Le reenvía al bot, en su chat, cualquier mensaje del canal.
  3. El revisor de capturas (`playdoit_checker._channel_command`, solo el chat del dueño) revisa con
     `getChatMember` que el bot pueda publicar, manda la bienvenida al canal y lo guarda en
     `model_state 'telegram_channel'`.
  - Una captura reenviada de un canal se sigue revisando como captura. «quitar canal» lo borra.
  - **El revisor escucha en tandas de ~5 h 45 min.** Un cambio a `playdoit_checker.py` entra hasta su siguiente
    relevo (la concurrencia del workflow no corta la escucha en curso).
- **Qué se copia** (`notify_telegram.share_message`, solo si al dueño le llegó):
  - la mañana y el avance de mañana, sin la sección del Mundial ni "y en el dashboard" (`telegram_reports.for_channel`);
  - los picks finales (`final_alerts.run_final_alerts(share=…)`);
  - el resumen de la noche;
  - "Partidos de hoy", con sus propios mensajes fijados y editados (`today_board.post_board`, estado
    `telegram_today_channel`).
- **Qué no se copia:** el reporte del domingo, los avisos técnicos y las respuestas del revisor.
- **Si el canal falla:**
  - No va a `SEND_FAILURES` (`_send_to_bot(record=False)`, `edit_or_send(chat_id=…)`), así que no pone la corrida
    en rojo.
  - Se registra como warning, no como error: un error saldría en "errores tolerados", con un Telegram en cada corrida
    (cada 30 min).
  - `notify_telegram.channel_result` lo anota (`TC.mark`) y le avisa al dueño en su chat una vez al día, en
    palabras normales (`TC.plain_error`).
  - El chequeo de salidas §19 (`TC.problems`) lo marca mientras no vuelva a llegar nada, o si un canal guardado pasa
    26 h sin recibir nada.
- Lo cubre `tests/test_telegram_channel.py`.

---

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
