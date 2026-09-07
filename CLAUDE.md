# CLAUDE.md

Modelo automatizado de apuestas de fútbol: Dixon-Coles + ensemble (Poisson, ELO, xG, H2H, forma)
sobre PostgreSQL, con The Odds API para las cuotas y Claude para los dos agentes LLM.
`scripts/orchestrator.py` corre el pipeline diario en GitHub Actions y avisa por Telegram.
Todo el estado vive en la base: entre corridas no sobrevive nada en memoria.

**Antes de tocar código lee [`docs/OPERACION.md`](docs/OPERACION.md)** — servicios externos,
los tres loops, comandos, y los gotchas que ya rompieron producción una vez.
