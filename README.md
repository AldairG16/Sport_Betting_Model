# Sports Betting Model

Un programa hecho para dar resultados sobre apuestas de fútbol a través de datos, historia y cálculos.

Analiza cada día los partidos de 19 ligas, compara su modelo (Dixon-Coles + ensemble de Poisson, Elo, xG,
entre-ellos y forma) con las casas y con Pinnacle, da un pick por partido, marca con 🎯 los recomendados para
PlayDoit, arma parlays con bono y avisa por Telegram y en un dashboard local. Cobra los resultados y aprende
de lo que junta. Corre solo en GitHub Actions sobre PostgreSQL (Neon).

## Documentación

- [`CLAUDE.md`](CLAUDE.md): qué es el programa, en una página.
- [`docs/README.md`](docs/README.md): el índice de todo lo demás. Qué es y cómo se usa, la arquitectura, cada
  componente, las corridas y horarios, los servicios externos, la base de datos, el modelo, la regla de los
  picks, el aprendizaje, el dashboard, Telegram, las auditorías, los comandos, las convenciones y los errores
  que ya rompieron producción.

## Arranque rápido (desarrollo)

```bash
pip install -r requirements.txt
cp .env.example .env          # y llenar DB_URL, ODDS_API_KEY, TELEGRAM_*, ANTHROPIC_API_KEY
python -m pytest -q tests     # ~1,670 pruebas, sin base ni APIs
python scripts/system_audit.py
```

El `.env` local apunta a la base de **producción**: lo que escribas ahí es real. Las corridas de producción se
lanzan desde GitHub Actions, no desde la PC ([`docs/04-corridas-y-horarios.md`](docs/04-corridas-y-horarios.md)).
Los comandos están en [`docs/13-comandos.md`](docs/13-comandos.md).

## Estado

En producción. `master` es producción; todo cambio entra por PR con las pruebas en verde. La versión del
dashboard está en [`VERSION`](VERSION).
