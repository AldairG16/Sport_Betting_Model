# archive/

Regla del repo: los módulos que dejan de ser alcanzables se **archivan, no se
borran**. Cada entrada dice por qué se retiró, qué lo sustituye y si es seguro
volver a ejecutarlo.

## prototipo_v1/ (retirado el 22-sep-2026)

Esqueleto del commit inicial (12-abr-2026) que nunca se conectó al pipeline:
ningún archivo de producción, script ni test lo importaba (verificado con
`git grep` sobre módulo, paquete y nombre). Tres de ellos estaban vacíos.

| Archivo | Qué hacía | Lo sustituye | ¿Re-ejecutable? |
|---|---|---|---|
| `src/markets/market_1x2.py`, `btts.py`, `over_under.py`, `correct_score.py` | Probabilidades por mercado desde una matriz de marcadores | `src/models/dixon_coles_model.py`, `src/models/poisson_markets.py` | No aporta nada: lógica duplicada y más simple |
| `src/markets/asian_handicap.py` | Vacío | `src/models/asian_handicap_model.py` | — |
| `src/probability/score_matrix.py` | Matriz de Poisson independiente | `dixon_coles_model` (con corrección tau) | No |
| `src/probability/poisson_model.py` | Vacío | — | — |
| `src/betting/probabilities.py` | Probabilidades implícitas del mercado | `src/features/market_odds.py` (Shin) | No |
| `src/betting/value_bets.py`, `src/models/value_bet.py` | Detección de value | `src/models/betting_engine.py` | No |
| `src/features/team_strength.py` | Fuerza de equipo por promedios | `src/features/team_form.py` (Kalman) | No |
| `src/features/team_matcher.py`, `team_mapping_engine.py` | Emparejado difuso de nombres | `src/utils/team_normalizer.py` | No: escribía su propio mapeo |
| `src/data/queries.py` | Vacío | — | — |
