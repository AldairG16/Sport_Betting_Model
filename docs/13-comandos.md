# 13 · Comandos

## Resumen

En local, todos necesitan el `.env` con `DB_URL` (la base de producción: cuidado con lo que escribe). En
GitHub las credenciales son secrets. Las corridas de producción se lanzan desde Actions ("Run workflow"), no
desde la PC.

| Para qué | Comando |
|---|---|
| Correr un modo | `python scripts/orchestrator.py --mode morning\|closing\|evening\|results\|weekly` |
| Auditoría completa | `python scripts/system_audit.py` (código 0 = todo bien) |
| Chequeo de salidas solo | `python -c "from src.utils.output_checks import run_output_checks; run_output_checks()"` |
| Presupuesto de la base | `python -c "from src.utils import db_budget as B; print(B.describe(B.status()))"` |
| Respaldo de la base a mano | `python -c "from config.database import engine; from src.utils.db_backup import run_backup; run_backup(engine)"` (en `respaldos/`) |
| Ensayo de restauración | `python scripts/restore_backup.py <carpeta> --schema respaldo_prueba` (se deshace solo) |
| Restaurar de verdad (base vacía) | `python scripts/restore_backup.py <carpeta> --apply` |
| Pruebas | `python -m pytest -q tests` (~14 min en la PC; ~1.5 min en GitHub) |
| Dashboard local | `python scripts/run_dashboard.py` → http://127.0.0.1:5050 |
| Lanzar un workflow | `gh workflow run <archivo>.yml --ref master` |

<!-- detalle movido de OPERACION.md -->

## Comandos

```bash
# Pipeline diario (los mismos modos que cablean los workflows)
python scripts/orchestrator.py --mode morning   # cuotas + predicciones + Telegram
python scripts/orchestrator.py --mode closing   # odds de cierre para CLV
python scripts/orchestrator.py --mode evening   # resultados + CLV + preview
python scripts/orchestrator.py --mode results   # solo re-fetch de resultados y resolver
python scripts/orchestrator.py --mode weekly    # recarga histórica + calibración + walk-forward

# Los dos agentes LLM (gastan tokens)
python scripts/pre_kickoff_analyst.py
python scripts/pre_kickoff_analyst.py --debug   # 1 sola bet, verifica la integración
python scripts/resolve_pending_bets.py --hours-lag 6 --limit 15
python scripts/resolve_pending_bets.py --include-stale   # una vez: suma las reales sin fuente

# Salud y auditoría (sin gasto)
python scripts/watchdog.py
python scripts/heartbeat_dispatch.py             # lo corre el closing; necesita gh y GH_TOKEN
python scripts/learn_team_aliases.py             # en seco: nombres del mismo club; --write reescribe el mapa
python scripts/learn_team_aliases.py --check     # solo avisa pares nuevos sin unir; no fusiona (el weekly fusiona antes de avisar)
python scripts/fix_league_labels.py              # en seco; --apply reetiqueta ligas (respaldo antes)
python scripts/fix_team_identities.py            # en seco; --apply une nombres y repetidos (después del anterior)
python scripts/audit_analyst_calibration.py --days 60
python scripts/db_smoke_test.py                  # 36 checks contra la base real, solo lectura
python -c "from scripts.revalidate_pending_bets import revalidate_pending_bets as r; r(dry_run=True)"  # qué cancelaría ahora
python scripts/fix_stat_settlements.py           # en seco; --apply corrige liquidaciones (--motivo "...")
python scripts/fix_match_stats.py                # en seco; --apply restaura córners y tiros reales del CSV

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_bet_filters.py -v
python -m pytest tests/test_kelly.py::test_kelly_caps -v
```

`pre_kickoff.yml` tiene una casilla `debug_mode` que corre el analista contra 1 bet
futura, para verificar la integración sin gastar un ciclo de cron completo.
