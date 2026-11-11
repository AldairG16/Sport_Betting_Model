# Operación (mapa)

Hasta el 11-nov-26 todo el detalle de operación estaba en este documento (2,900 líneas). Se dividió por tema
en `docs/` sin quitar nada; el índice está en [README.md](README.md).

Algunos comentarios del código y de los workflows todavía dicen `docs/OPERACION.md §N`. Esta tabla dice
adónde fue cada sección:

| Sección vieja | Ahora |
|---|---|
| §1 Servicios externos | [05 · Servicios externos](05-servicios-externos.md) |
| §2 Cómo se dispara realmente (y los fallos) | [04 · Corridas y horarios](04-corridas-y-horarios.md) |
| §3 Los tres loops | [04 · Corridas y horarios](04-corridas-y-horarios.md) |
| §3 Cómo auditar | [12 · Auditoría y chequeos](12-auditoria-y-chequeos.md) |
| §4 Watchdog | [12 · Auditoría y chequeos](12-auditoria-y-chequeos.md) |
| §5 Gotchas: datos, fuentes, nombres, fechas, NaN | [15 · Lecciones: datos y fuentes](15-lecciones-datos.md) |
| §5 Gotchas: The Odds API, créditos, cierres, líneas | [16 · Lecciones: cuotas y créditos](16-lecciones-cuotas-y-creditos.md) |
| §5 Gotchas: pasos "OK", transacciones, pandas, runner, handicap | [17 · Lecciones: código y base](17-lecciones-codigo.md) |
| §5 Modelo roto (28-sep a 21-oct) | [07 · El modelo](07-modelo.md) |
| §5 Auditoría de salidas y chequeo diario | [12 · Auditoría y chequeos](12-auditoria-y-chequeos.md) |
| §5 La base en Oregon, transferencia y presupuesto de Neon | [06 · Base de datos](06-base-de-datos.md) |
| §5 `model_proposals`, laboratorio, mejor cuota, Kalshi | [08 · Picks y parlays](08-picks-y-parlays.md) |
| §5 Goleadores y resumen nocturno | [11 · Telegram](11-telegram.md) |
| §6 Presupuesto de Anthropic | [05 · Servicios externos](05-servicios-externos.md) |
| §7 Aprendizaje y calibración | [09 · Aprendizaje](09-aprendizaje.md) |
| §7 Momios históricos, córners y tarjetas, alineaciones, fuerza de liga, calibración por mercado | [07 · El modelo](07-modelo.md) |
| §7 "Partidos de hoy" en Telegram | [11 · Telegram](11-telegram.md) |
| §8 Comandos | [13 · Comandos](13-comandos.md) |
| §8 Dashboard local, pestañas, supervisor, versiones | [10 · Dashboard](10-dashboard.md) |
| §8 Parlays, la regla del pick, PlayDoit | [08 · Picks y parlays](08-picks-y-parlays.md) |
| §8 Mensajes de Telegram y el canal | [11 · Telegram](11-telegram.md) |
| §8 Textos en palabras normales | [14 · Convenciones](14-convenciones.md) |
| §8 Auditorías del 6 y del 9 de noviembre | [18 · Auditorías de noviembre](18-auditorias-noviembre.md) |
| §9 Mundial 2026 | [19 · Mundial 2026](19-mundial-2026.md) |
| §10 Convenciones, tests y ramas | [14 · Convenciones](14-convenciones.md) |

La versión completa anterior sigue en el historial de git (`git show 03f5d13:docs/OPERACION.md`).
