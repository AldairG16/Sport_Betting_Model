# Inventario de scripts

Registro operativo de **todo** archivo `.py` que vive en `scripts/`. Existe
para responder, sin leer codigo, las tres preguntas que importan antes de
ejecutar cualquier cosa a mano:

1. **Trigger** — quien lo dispara: el nombre del workflow de
   `.github/workflows/` que lo invoca, `manual` si solo lo corre el operador,
   o `one-shot-applied` si ya se aplico contra produccion.
2. **Idempotent** — si volver a correrlo es seguro. `unknown` es una
   respuesta valida y honesta; una suposicion no lo es.
3. **Writes DB** — si el script escribe en PostgreSQL. **Esta columna se
   deriva de la fuente**, no se redacta a mano: es la salida de
   `tools.audit.checks.inventory.detect_db_writes()` sobre el codigo del
   script (busca `INSERT INTO`, `DELETE FROM`, `UPDATE ... SET`, `to_sql(`,
   DDL y escrituras ORM, ignorando comentarios).

**Owner loop** ata el script a uno de los tres loops de `CLAUDE.md`
(`daily-pipeline`, `pre-kickoff-analyst`, `pending-resolver`); todo lo demas
es `manual` o `one-shot`.

## Como se mantiene

El documento no se mantiene solo por disciplina: el checker `inventory` del
auditor estatico falla cuando un archivo de `scripts/` no tiene fila aqui, o
cuando una fila apunta a un script que ya no existe. Un script nuevo no puede
entrar sin dueño.

```bash
python -m pytest tests/test_scripts_inventory.py -v   # verifica el inventario
python -m tools.audit.checks.inventory --write        # regenera la tabla
```

La regeneracion **preserva** las columnas curadas (`Idempotent`, `Owner loop`,
`Notes`) de las filas que ya existen y solo recalcula lo derivable, para que
volver a generar nunca borre conocimiento operativo escrito a mano.

## Inventario

| Script | Trigger | Idempotent | Writes DB | Owner loop | Notes |
|---|---|---|---|---|---|
| `capture_golden_fixture.py` | manual | yes | no | manual | Solo lectura: ejecuta un unico `SELECT` sobre el slate y **nunca** consulta ni escribe `bets_history`. Imprime a stdout, no sobrescribe nada. **No refresca el fixture congelado** `tests/fixtures/golden_input.json`: existe para que el operador inspeccione un slate real a mano. CI no lo corre. |
| `update_upcoming_matches.py` | manual | yes | yes | daily-pipeline | Fetch de odds de The Odds API. Escribe `upcoming_matches` (`INSERT INTO` en :846, `DELETE FROM` de limpieza en :937 y :961) — **no toca `bets_history`**. Re-ejecutar es seguro: el upsert es por clave y el cache TTL evita el refetch, pero **consume creditos de API**. Gotcha conocido: el upsert incluye `match_day`, asi que una correccion de horario crea fila nueva en vez de actualizar; todo consumidor debe usar `DISTINCT ON (...) ORDER BY updated_at DESC`. Su docstring lo describe como paso del pipeline diario, pero ningun workflow presente en `.github/workflows/` lo invoca (`weekly.yml` solo corre `orchestrator.py` y `clv_audit.py`), por eso el Trigger derivado es `manual`. |
| `watchdog.py` | manual | yes | no | manual | Monitoreo de ausencia: solo lee (DB accesible, frescura de `upcoming_matches`, bets `pending` viejas, gap del heartbeat) y alerta por Telegram. **Silencio = salud.** No escribe en la base, asi que re-ejecutarlo solo puede repetir una alerta. Fuera de los tres loops de `CLAUDE.md`, de ahi `Owner loop = manual`. Su docstring documenta un cron de 6 h via `watchdog.yml`; ese workflow no existe en esta rama, por eso el Trigger derivado es `manual` y no el nombre del archivo. |
