# Documentación del programa

`CLAUDE.md` (en la raíz) dice qué es el programa. Aquí está todo lo demás, dividido por tema.
Cada documento empieza con un resumen; lo que sigue es el detalle, con la fecha y el motivo de cada
decisión.

## Para entender el programa

| # | Documento | Qué tiene |
|---|---|---|
| 01 | [Qué es y cómo se usa](01-que-es.md) | Para qué sirve, qué recibe el dueño cada día, en qué estado está y qué decisiones tomó el dueño |
| 02 | [Arquitectura](02-arquitectura.md) | Las piezas, cómo viajan los datos, dónde vive el estado y la estructura de carpetas |
| 03 | [Componentes](03-componentes.md) | Cada módulo del código (`config`, `scripts`, `src`, `dashboard`) con lo que hace |
| 04 | [Corridas y horarios](04-corridas-y-horarios.md) | Quién dispara qué y cuándo, los modos del orquestador paso por paso, los tres loops y qué pasa cuando algo falla |
| 05 | [Servicios externos](05-servicios-externos.md) | The Odds API, Neon, Anthropic, Telegram, ESPN, football-data, Understat/FotMob, TheSportsDB, Kalshi, ClubElo: para qué, credenciales, límites y costo |
| 06 | [Base de datos](06-base-de-datos.md) | Cada tabla y para qué sirve, el plan gratis de Neon, el presupuesto, el freno y los respaldos |

## Cómo decide

| # | Documento | Qué tiene |
|---|---|---|
| 07 | [El modelo](07-modelo.md) | Dixon-Coles, el ensemble, las features, xG, córners y tarjetas, alineaciones, fuerza de liga y calibración |
| 08 | [Picks y parlays](08-picks-y-parlays.md) | La regla del pick y del 🎯, `program_picks`, el parlay con bono, PlayDoit y cómo se cobra |
| 09 | [Aprendizaje](09-aprendizaje.md) | Las apuestas sombra, el peso del modelo, el sesgo por lado, los filtros aprendidos, Pinnacle y el modo recolección |

## Lo que ve el dueño

| # | Documento | Qué tiene |
|---|---|---|
| 10 | [Dashboard](10-dashboard.md) | El `.exe` de la PC del dueño: pestañas, supervisor, instalación y versiones |
| 11 | [Telegram](11-telegram.md) | Los mensajes de la mañana, del día, de la noche y de la semana, los picks finales y el canal |

## Para trabajar en el programa

| # | Documento | Qué tiene |
|---|---|---|
| 12 | [Auditoría y chequeos](12-auditoria-y-chequeos.md) | El watchdog, el chequeo diario de salidas (23 chequeos), la auditoría del sistema (11 secciones) y cómo auditar |
| 13 | [Comandos](13-comandos.md) | Cómo correr cada modo, los agentes, las auditorías, las pruebas y el dashboard |
| 14 | [Convenciones y preferencias del dueño](14-convenciones.md) | Cómo se trabaja: ramas, pruebas, idioma, y lo que el dueño pidió (palabras normales, momios americanos, nada a mano, nunca esconder un pick) |

## Lo que ya rompió producción una vez

Léelo antes de cambiar la parte correspondiente: cada punto es un error real, con su fecha y su arreglo.

| # | Documento | Qué tiene |
|---|---|---|
| 15 | [Lecciones: datos y fuentes](15-lecciones-datos.md) | Fechas, temporadas, nombres de equipos, football-data, ESPN, NaN y lo que entra al modelo |
| 16 | [Lecciones: cuotas y créditos](16-lecciones-cuotas-y-creditos.md) | The Odds API, presupuesto de créditos, cierres válidos y líneas |
| 17 | [Lecciones: código y base](17-lecciones-codigo.md) | Pasos "OK" que no hicieron nada, transacciones, pandas, el runner de GitHub, handicap asiático |
| 18 | [Auditorías de noviembre](18-auditorias-noviembre.md) | Auditoría del 6-nov, repetidos y pospuestos del 9-nov |
| 19 | [Mundial 2026](19-mundial-2026.md) | El torneo, el kill-switch y el paper-trading |

[Glosario](glosario.md): las palabras del proyecto (🎯, sombra, cierre, CLV, Pinnacle, PA, stale, …).

## Historial

[`historial/`](historial/) guarda las auditorías y debates viejos (rondas 4 a 11, debates 17 y 19, salida en
papel, auditoría de producción). Sirven para saber por qué algo es como es; lo vigente está en los
documentos de arriba.

`OPERACION.md` era el documento único hasta el 11-nov-26. Ahora solo dice adónde se movió cada sección,
para que las referencias viejas (`docs/OPERACION.md §5`) sigan sirviendo.

## Cómo se mantiene

- Un cambio que cambia cómo funciona algo actualiza su documento en el mismo PR.
- Un error nuevo que rompió producción va a la lección que le toca, con su fecha.
- Una clase de error nueva se vuelve un chequeo (ver [12](12-auditoria-y-chequeos.md)).
