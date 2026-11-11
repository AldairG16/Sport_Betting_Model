# CLAUDE.md

## Qué es este programa

Un sistema automático que analiza partidos de fútbol y dice al dueño **qué apostar en PlayDoit** (su casa de
apuestas), con datos, historia y cálculos en vez de corazonadas.

Cada día:

1. Descarga las cuotas de los partidos de 19 ligas (The Odds API) y las compara con su propio modelo
   (Dixon-Coles + ensemble de Poisson, Elo, xG, entre-ellos y forma).
2. Da **un pick por cada partido analizado** (nunca esconde uno) y marca con 🎯 los recomendados: hoy, un
   favorito muy claro según Pinnacle (la casa más precisa) que PlayDoit paga a -500 o mejor, en una liga con
   pago anticipado.
3. Arma parlays, revisa las alineaciones antes de cada partido y fija el pick final.
4. Avisa por Telegram (al dueño y a su canal «Dulopatia») y lo muestra en un dashboard en la PC del dueño.
5. Cobra los resultados, mide contra el mercado y **aprende** de lo que junta (apuestas "sombra" y picks
   guardados) para recalibrarse solo.

Corre en GitHub Actions (lo dispara cron-job.org cada 30 min y en horarios fijos). Todo el estado vive en
PostgreSQL (Neon, plan gratis). Claude se usa en dos agentes: el analista previo al partido y el resolvedor
de apuestas sin dato. El dueño no escribe ni registra nada: todo es automático.

`master` es producción. La versión del dashboard está en `VERSION`.

## Dónde está todo lo demás

**Antes de tocar código, lee [`docs/README.md`](docs/README.md)**. Es el índice: qué hace cada parte, cómo
corre, la base de datos, el modelo, la regla de los picks, lo que aprende, el dashboard, Telegram, las
auditorías, los comandos, las preferencias del dueño y los errores que ya rompieron producción una vez.
