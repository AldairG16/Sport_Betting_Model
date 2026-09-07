# CLAUDE.md

Modelo automatizado de apuestas de fútbol: Dixon-Coles + ensemble (Poisson, ELO, xG, H2H, forma)
sobre PostgreSQL, con The Odds API para las cuotas y Claude para los dos agentes LLM.
`scripts/orchestrator.py` corre el pipeline; todo el estado vive en la base, entre corridas
no sobrevive nada en memoria. Esta rama añade auditoría estática (`tools/audit`) y no es desplegable.

**Antes de tocar código lee [`docs/OPERACION.md`](docs/OPERACION.md)** — fuentes únicas de verdad,
servicios externos, los tres loops, y los gotchas que ya rompieron producción una vez.
