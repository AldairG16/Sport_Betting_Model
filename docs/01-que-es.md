# 01 · Qué es y cómo se usa

## En una frase

Un programa que, con datos, historia y cálculos, le dice al dueño qué partido de fútbol apostar en
**PlayDoit** y a partir de qué momio vale la pena, y que se va corrigiendo solo con lo que junta.

## Para quién y cómo lo usa el dueño

El dueño apuesta en PlayDoit (casa mexicana, momios americanos: +128 / -140). No quiere escribir, registrar
ni confirmar nada: solo recibir el pick, ver PlayDoit y apostar. Todo lo que el programa le muestra está en
español de todos los días y en momios americanos.

Lo recibe por dos vías:

- **Telegram:** el bot le escribe a él y copia los picks finales a su canal «Dulopatia» (para quien se una).
- **Dashboard:** un `.exe` que corre en su PC (http://127.0.0.1:5050) con los próximos partidos, el parlay,
  lo que ya pasó, las ganadas y perdidas, cada liga, cada equipo y las gráficas.

## Un día normal (hora de México)

| Hora | Qué pasa | Dónde se ve |
|---|---|---|
| 6:00 | **Mañana**: baja cuotas, analiza los partidos del día y de los siguientes, guarda un pick por partido | Telegram ("la mañana") y dashboard |
| Cada 30 min | **Cierre**: vuelve a bajar cuotas, guarda la alineación ~1 h antes, rehace el pick con ella y lo fija al empezar el partido | "Partidos de hoy" en Telegram (se edita sin sonar), dashboard |
| Antes de cada partido | **Pick final**: un solo mensaje por partido con el 🎯 (o por qué ya no lo es), quién falta y el parlay de ese horario | Telegram del dueño y canal |
| ~21:00 | **Noche**: baja resultados, cobra los picks, corre los chequeos y manda el resumen con un avance de mañana | Telegram ("la noche") |
| Lunes 13:00 | **Semanal**: aprende de lo juntado, recarga datos, ajusta Dixon-Coles, audita todo y manda el reporte | Telegram ("la semana") |
| Todo el día | **Vigilancia**: el watchdog (4 veces al día) avisa si algo dejó de correr | Telegram, solo si hay problema |

Los horarios exactos y quién dispara cada corrida están en [04](04-corridas-y-horarios.md).

## Qué decide y cómo

1. **El modelo** calcula, para cada partido, cuántas veces de cada 100 gana el local, empata o gana el
   visitante, cuántos goles habrá, si anotan los dos, córners y tarjetas ([07](07-modelo.md)).
2. **Las casas** dicen lo mismo con sus momios. Pinnacle es la referencia: es la casa más precisa, y sin su
   margen da la probabilidad "real" del mercado.
3. **El pick** es lo que más le gusta al programa de cada partido, entre todos los mercados. **Nunca se
   esconde un pick**: cada partido analizado tiene el suyo, se gane o se pierda, para que el dueño vea hacia
   dónde tiende el aprendizaje.
4. **El 🎯 (recomendado)** es la apuesta que sí se sugiere jugar. Hoy: un equipo muy favorito (según Pinnacle
   gana 65 de cada 100 partidos así o más) que PlayDoit paga a -500 o mejor, en una liga con **pago
   anticipado** (PlayDoit da la apuesta por ganada si el equipo va 2 goles arriba). Si gana menos de 70 de
   cada 100, solo cuando su momio llega al mínimo (12-nov). Los partidos sin 🎯 dicen
   por qué no lo son ("no hay favorito claro", "paga muy poco (-667)", "esta liga no tiene pago anticipado").
5. **El parlay** junta los 🎯 de un día o de un horario para cobrar el bono de PlayDoit por parlay (5% con 3
   piernas, 7.5% con 4, 10% con 5…, sobre la ganancia) ([08](08-picks-y-parlays.md)).

Todo se mide "apostando 50 a cada uno".

## Qué aprende

- Guarda **apuestas sombra** (candidatas que no se juegan) y cada pick, con su precio y el de Pinnacle al
  cerrar. Con eso mide si el modelo le gana al mercado.
- Cada semana recalcula cuánto creerle al modelo frente a Pinnacle, el sesgo por lado y por región, qué
  regiones pausar (hoy, el 🎯 de América está en pausa porque perdía) y otros filtros ([09](09-aprendizaje.md)).

## En qué estado está (noviembre de 2026)

- **En producción**, versión del dashboard 2.12.1. `master` es producción.
- **Modo recolección desde el 25-sep-26**: el programa ya no registra apuestas propias en `bets_history`
  (congelada); junta picks (`program_picks`) y sombra para aprender.
- **Lo que dicen los datos (auditoría del 5-nov):** el modelo no le gana a Pinnacle en predecir; la única
  ventaja con evidencia es la del **precio**: los favoritos fuertes que Pinnacle subestima, el pago anticipado
  y el bono de parlay. Por eso el 🎯 es lo que es.
- **Neon**: el 9-nov la base se bloqueó por pasarse de la transferencia del plan gratis. Se pagó el plan
  Launch hasta el 1-dic para medir y corregir; desde el 1-dic vuelve al gratis con presupuesto, freno
  automático y respaldo semanal ([06](06-base-de-datos.md)).

## Decisiones del dueño que siguen vigentes

| Tema | Decisión |
|---|---|
| Picks | Un pick por partido analizado, siempre; los filtros solo marcan 🎯, nunca quitan (22-oct) |
| 🎯 | Favorito fuerte (≥65 de cada 100 según Pinnacle), -500 o mejor, liga con pago anticipado; el 🎯 de goles se quitó (5-nov). De 65 a 70, solo si el momio estimado llega al mínimo (12-nov) |
| Parlays | Bono de PlayDoit: parlay del día de 4 a 6 piernas y parlays de un mismo horario de 3+, esperando alineaciones (5-nov) |
| Dinero | Todo "apostando 50 a cada uno" (8-nov) |
| Momios | Siempre americanos; los decimales solo internos |
| Textos | Palabras normales: "de cada 100", "las casas", "recomendado", "ganancia"; nada de ROI, unidades, "pt" ni "mercado sin margen" |
| Pasos manuales | Ninguno. Si algo necesita que el dueño escriba o registre, se busca una fuente automática o no se hace |
| Aprendizaje | Que se recalibre solo con lo que junta, con frenos estadísticos (mínimo de datos, encogimiento, histéresis) |
| Filtro Pinnacle | Se queda el margen de 2 puntos contra Pinnacle |
| Tarjetas | Roja = 1 tarjeta, como cobra PlayDoit |
| Plan gratis | Todo debe funcionar con planes gratis; el freno de la base cierra los cierres a uno por hora si hace falta (11-nov) |
| Base de reserva | Reservada: por ahora no se hace (11-nov) |
| Preguntas | Si hay que decidir algo, preguntas numeradas en texto (1a/1b…) con datos y una recomendación |

El registro completo de lo que el dueño decidió y cuándo está en la memoria del proyecto
(`pending-owner-decisions`) y en los documentos de cada tema.

## Lo que no es

- No es consejo financiero ni garantiza ganar: mide y dice dónde hay evidencia y dónde no.
- No apuesta solo: el dueño apuesta a mano en PlayDoit.
- No lee PlayDoit: no hay API, y su widget (Altenar) bloquea programas. Se usa el promedio de las casas
  europeas como su precio, y el revisor de capturas de Telegram para lo que el dueño mande.
