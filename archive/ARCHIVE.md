# Archive — registro de disposiciones

Este directorio es el destino de todo modulo que deja de ser alcanzable desde
un entry point real (un `run:` de `.github/workflows/*.yml` o la suite de
tests). La regla del proyecto es **archivar, nunca borrar**: un modulo que hoy
parece muerto puede ser la unica documentacion de como se cargo una temporada
historica, y un `git rm` convierte esa informacion en arqueologia.

`archive/` esta excluido del recorrido del auditor
(`tools/audit/graph.py::EXCLUDED_DIRS`), asi que el codigo retirado no puede
volver a disparar hallazgos de `broken-reference`, `orphan-code` ni
`circular-dependency`. Esa exclusion es justo lo que hace que archivar sea una
disposicion valida y no un barrido bajo la alfombra: la fila de abajo es la
que rinde cuentas.

## Registro

| Module | Archived on | Reason | Disposition | Safe to re-run? |
|--------|-------------|--------|-------------|-----------------|
| `load_10_seasons.py` | 2026-09-03 | Su primera linea es `from src.data.collector import download_season` y `src/data/collector.py` no existe en ninguna revision del repositorio: `src/data/` solo contiene `queries.py`. El script revienta con ImportError antes de ejecutar una sola instruccion, de modo que era una referencia rota permanente (S1) en la raiz del proyecto. | Movido sin modificar a `archive/load_10_seasons.py`. El blob es identico al original (`27892b1`), no se reescribio ni una linea. La carga historica de temporadas la cubre hoy `scripts/load_historical_data.py`, que si tiene su collector real y corre desde el modo `weekly` del orchestrator. | **No — nunca corrio.** No es un one-shot ya aplicado: al fallar en el import jamas escribio una fila en `matches` ni gasto un credito de API. Re-ejecutarlo tal cual es imposible (mismo ImportError); reconstruir `src.data.collector` para revivirlo duplicaria la carga historica que ya hace `load_historical_data.py` y meteria partidos repetidos. Usar ese script en su lugar. |

## Como se agrega una entrada

1. Mover el archivo con `git mv <ruta> archive/<ruta>` — **mover, no reescribir**.
   El diff tiene que salir como rename puro; si aparece contenido cambiado, la
   disposicion deja de ser auditable.
2. Agregar una fila a la tabla con las cinco columnas llenas. Ninguna puede
   quedar vacia: una disposicion sin motivo es un borrado disfrazado.
3. La columna **Safe to re-run?** exige una respuesta explicita, y
   *"one-shot ya aplicado"* y *"nunca corrio"* son respuestas distintas:
   - **one-shot ya aplicado** — el script corrio, escribio en produccion y
     volver a correrlo duplicaria datos o gastaria creditos.
   - **nunca corrio** — fallo antes de tocar nada; no hay estado que revertir.
   - **si** — es idempotente y volver a correrlo es inofensivo.
4. `tests/test_no_broken_imports.py` verifica que todo `.py` bajo `archive/`
   tenga su fila, asi que un archivo depositado aqui sin registrar rompe la
   suite.
