# Lecciones manuales del analista

Este archivo se inyecta tal cual en el prompt del Pre-Kickoff Analyst en
cada corrida. Editalo cuando detectes un patrón de error o quieras
imponer una regla. **Sé conciso**: cada palabra cuesta tokens. Idealmente
< 600 palabras totales.

Formato sugerido: viñetas cortas, una regla por línea, en español.
Si una regla deja de aplicar, borrala.

---

## REGLAS DURAS (no negociables)

- Si web_search no devuelve fuente confiable de alineación NI de
  lesiones, marcá `lineups="L0"` y `decision="NO APUESTA"`. Nunca
  apuestes a ciegas.
- Si la apuesta es Over 2.5 o BTTS y el top-scorer del equipo local
  está confirmado fuera, bajá la `probability` 8-12 puntos.
- En clásicos / derbys, asumí varianza alta: nunca des `probability`
  > 70 salvo que lineups confirmadas y forma reciente alineen.

## PATRONES OBSERVADOS

- *(agregá acá patrones reales que veas en los reportes de calibración)*

## LIGAS CON MATICES

- **MLS**: viajes cross-country (>2000 km en <72h) bajan rendimiento
  visitante ~15%. Si la apuesta es away_win y aplica este patrón,
  bajá `probability` 10 puntos.
- **Brasileirão**: equipos en Libertadores rotando entre semana →
  rotación esperada incluso si congestión = 2/10d.
- **Championship**: muchos partidos por semana en marzo-abril, asumí
  rotación si congestión >= 2 partidos/7d.

---

*Última edición: 2026-05-09 (template inicial)*
