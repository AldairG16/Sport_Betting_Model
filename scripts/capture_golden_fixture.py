"""
scripts/capture_golden_fixture.py
=================================
Regenera, SOLO BAJO DEMANDA DEL OPERADOR, un payload con el mismo contrato que
`tests/fixtures/golden_input.json`, leyendo el slate real desde la base.

    python scripts/capture_golden_fixture.py --days 3 > /tmp/candidate.json

⚠️  EL FIXTURE COMMITEADO NUNCA SE REFRESCA CON ESTE SCRIPT.
    `tests/fixtures/golden_input.json` esta CONGELADO de forma permanente: es
    la linea base contra la que se demuestra que una remediacion no cambio
    ninguna apuesta. Si se regenera, la prueba deja de probar nada (se estaria
    comparando el codigo nuevo contra si mismo). Este script existe para que el
    operador pueda INSPECCIONAR un slate real y compararlo a mano, no para
    sobrescribir la linea base. No hay cadencia de refresco y CI no lo corre.

SOLO LECTURA: este script unicamente ejecuta SELECT. No escribe en la base y no
consulta `bets_history` en absoluto.

Este modulo tambien es el hogar UNICO de las dos funciones deterministas que la
cadena de decision necesita como parametros (`ah_group` y `frozen_kelly_stake`).
El harness dorado (tests/test_golden_decision.py) las importa de aqui: si
vivieran duplicadas en el test, el fixture y el regenerador podrian divergir sin
que nada lo notara.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
from typing import Any


# ============================================================
# PARAMETROS CONGELADOS DE LA CADENA DE DECISION
# ============================================================
# Estos numeros forman parte del fixture: cambiarlos cambia los stakes y por lo
# tanto rompe (correctamente) el test dorado.
FROZEN_KELLY_FRACTION = 0.25   # Kelly fraccionario 1/4
FROZEN_MAX_STAKE_PCT = 0.05    # tope duro por bet: 5% del bankroll


def ah_group(market: str) -> str | None:
    """Mapea una llave AH parametrizada a su grupo de calibracion.

    `ah_home_-0.5` -> `ah_home_fav`, `ah_home_0.0` -> `ah_home_pk`,
    `ah_away_+1.0` -> `ah_away_dog`. Devuelve None para mercados no-AH.

    Replica deliberada de `src.models.calibration_monitor._ah_group`: se copia
    en lugar de importarse porque ese modulo arrastra la conexion a la base, y
    tanto este script como el harness tienen que poder correr sin DB_URL.
    Sin este mapeo los mercados AH caen al `min_edge_default` y dejan pasar
    bets muy por debajo del umbral que les corresponde.
    """
    if not (market.startswith("ah_home_") or market.startswith("ah_away_")):
        return None
    side = "ah_home" if market.startswith("ah_home_") else "ah_away"
    try:
        line = float(market.rsplit("_", 1)[-1])
    except ValueError:
        return None
    if line < -0.25:
        return f"{side}_fav"
    if line <= 0.25:
        return f"{side}_pk"
    return f"{side}_dog"


def frozen_kelly_stake(
    prob: float,
    odds: float,
    *,
    bankroll: float,
    market: str | None = None,
    league: str | None = None,
) -> float:
    """Kelly fraccionario puro, congelado para el fixture dorado.

    Firma identica a `src.models.betting_engine.kelly_stake` para que
    `size_stakes` la acepte tal cual.

    POR QUE NO SE USA `kelly_stake` DE PRODUCCION:
    esa funcion no es pura -- `_adjusted_kelly_fraction` lee
    `data/clv_cache.json`, un archivo que se regenera cada semana y que escala
    los stakes por 1.20 o 0.60 segun el CLV reciente. Un fixture "congelado"
    apoyado en ella cambiaria de resultado cada lunes sin que nadie tocara el
    codigo. Aqui se fija el componente determinista (la formula de Kelly) y se
    deja fuera el ajuste dependiente de estado externo.

    f* = (prob * odds - 1) / (odds - 1), escalado por FROZEN_KELLY_FRACTION y
    topado a FROZEN_MAX_STAKE_PCT del bankroll. Edge negativo -> stake 0.
    """
    if bankroll <= 0 or odds <= 1:
        return 0.0

    full_kelly = (prob * odds - 1) / (odds - 1)
    if full_kelly <= 0:
        return 0.0

    stake = bankroll * full_kelly * FROZEN_KELLY_FRACTION
    stake = min(stake, bankroll * FROZEN_MAX_STAKE_PCT)
    return round(stake, 2)


# ============================================================
# CAPTURA DESDE LA BASE (SOLO SELECT)
# ============================================================
# `upcoming_matches` acumula duplicados obsoletos: su llave de upsert incluye
# `match_day` derivado del kickoff, asi que cuando la API corrige la hora se
# escribe una fila nueva en vez de reemplazar la anterior. Todo consumidor debe
# quedarse con la fila mas reciente por (home_team_norm, away_team_norm,
# sport_key) -- de ahi el DISTINCT ON.
_SLATE_QUERY = """
    SELECT DISTINCT ON (home_team_norm, away_team_norm, sport_key)
           home_team_norm,
           away_team_norm,
           sport_key,
           market,
           side,
           odds,
           prob,
           edge,
           edge_market
      FROM upcoming_matches
     WHERE match_date >= NOW()
       AND match_date < NOW() + (:days * INTERVAL '1 day')
     ORDER BY home_team_norm, away_team_norm, sport_key, updated_at DESC
"""

_REQUIRED_COLUMNS = ("market", "side", "odds", "prob", "edge", "edge_market")


def capture_scored(days: int) -> list[dict[str, Any]]:
    """Lee el slate de los proximos `days` dias y lo mapea al contrato scored.

    El import de `config.database` es diferido a proposito: `config/settings.py`
    levanta RuntimeError en tiempo de import cuando DB_URL no esta definida, y
    este modulo tiene que poder importarse sin base (el harness dorado lo
    importa para reusar `ah_group` y `frozen_kelly_stake`).
    """
    from sqlalchemy import text  # noqa: PLC0415 -- diferido, ver docstring

    from config.database import get_engine  # noqa: PLC0415

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(_SLATE_QUERY), {"days": days}).mappings().all()

    scored: list[dict[str, Any]] = []
    for row in rows:
        missing = [c for c in _REQUIRED_COLUMNS if row.get(c) is None]
        if missing:
            # No se inventan probabilidades: si la fila no trae el scoring del
            # modelo, se omite y se avisa por stderr en vez de fabricar un
            # numero que luego se congelaria en un fixture.
            print(
                f"[skip] {row['home_team_norm']} vs {row['away_team_norm']}: "
                f"faltan columnas {missing}",
                file=sys.stderr,
            )
            continue

        scored.append(
            {
                "match": f"{row['home_team_norm']} vs {row['away_team_norm']}",
                "market": row["market"],
                "side": row["side"],
                "odds": float(row["odds"]),
                "prob": float(row["prob"]),
                "edge": float(row["edge"]),
                "edge_market": float(row["edge_market"]),
                "league": row["sport_key"],
            }
        )

    # Orden estable: la salida no debe depender del plan del planner.
    scored.sort(key=lambda b: (b["match"], b["market"], b["side"]))
    return scored


def build_payload(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Envuelve el slate en el mismo contrato que golden_input.json."""
    from config.settings import (  # noqa: PLC0415 -- diferido, requiere DB_URL
        INITIAL_BANKROLL,
        MIN_EDGE_BY_MARKET,
        MIN_EDGE_DEFAULT,
    )

    return {
        "bankroll": float(INITIAL_BANKROLL),
        "min_edge_default": float(MIN_EDGE_DEFAULT),
        "max_total_pct": float(os.environ.get("MAX_TOTAL_EXPOSURE_PCT", "0.15")),
        "min_edge_by_market": dict(MIN_EDGE_BY_MARKET),
        "scored": scored,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Captura un slate real con el contrato de golden_input.json. "
            "SOLO LECTURA. No refresca el fixture congelado."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Ventana hacia adelante en dias (default: 3)",
    )
    args = parser.parse_args()

    if args.days < 1:
        print("--days debe ser >= 1", file=sys.stderr)
        return 2

    payload = build_payload(capture_scored(args.days))
    print(json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=False))

    print(
        "\n[aviso] El fixture commiteado esta congelado a proposito. "
        "NO sobrescribas tests/fixtures/golden_input.json con esta salida.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
