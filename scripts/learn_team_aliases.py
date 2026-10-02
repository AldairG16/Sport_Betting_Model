"""
scripts/learn_team_aliases.py
=============================
Aprende de `matches` los nombres distintos del mismo club y escribe
config/team_aliases.json, que normalize_team aplica al final (1-oct-26).
La lógica pura y el porqué están en src/utils/team_identity.py.

Uso:
    python scripts/learn_team_aliases.py            # muestra el mapa (no escribe)
    python scripts/learn_team_aliases.py --write    # escribe config/team_aliases.json
    python scripts/learn_team_aliases.py --check    # solo avisa pares NUEVOS sin cubrir

`--check` lo corre el weekly: un club recién ascendido o una fuente nueva
puede traer otro nombre. Los pares compatibles que el normalizador todavía no
une se reportan como ERROR (llegan a Telegram); se agregan revisando y
corriendo --write en una rama.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from sqlalchemy import text

from src.utils.log import get_logger
from src.utils.team_identity import (ALIASES_FILE, MIN_SUPPORT, build_alias_map,
                                     classify_pairs, find_alias_pairs)
from src.utils.team_normalizer import base_normalize_team, normalize_team

log = get_logger(__name__)


def read_inputs():
    from config.database import engine
    rows = pd.read_sql(text("""
        SELECT id, date::date AS d, LOWER(home_team) AS h, LOWER(away_team) AS a,
               home_goals AS hg, away_goals AS ag
        FROM matches
        WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
          AND (league IS NULL OR league NOT LIKE 'baseball%%')
    """), engine)
    live = pd.read_sql(text("""
        SELECT DISTINCT LOWER(home_team_norm) AS n FROM upcoming_matches
        UNION SELECT DISTINCT LOWER(away_team_norm) FROM upcoming_matches
    """), engine)["n"].dropna().tolist()
    freq = Counter(list(rows["h"]) + list(rows["a"]))
    return rows, live, freq


def learn(rows, live, freq, rounds: int = 4):
    """
    Varias rondas. La regla pide el mismo equipo del mismo lado, así que un
    partido con los DOS nombres distintos ("stoke / sheffield weds" y "stoke
    city / sheffield wednesday") no deja evidencia en la primera; con "stoke"
    ya unido a "stoke city", en la segunda el local coincide y sale el par
    del visitante. Se repite hasta que no aparecen pares nuevos.
    """
    pairs: Counter = Counter()
    accepted: list = []
    review: list = []
    alias_map, components, conflicts = {}, [], []
    for _ in range(rounds):
        def canon(n, m=alias_map):
            b = base_normalize_team(n)
            return m.get(b, b)
        found = find_alias_pairs(rows.assign(h=rows["h"].map(canon), a=rows["a"].map(canon)))
        for p, n in found.items():
            pairs[p] = max(pairs[p], n)
        acc, review = classify_pairs(found)
        new = sorted(set(acc) - set(accepted))
        if not new:
            break
        accepted = sorted(set(accepted) | set(new))
        alias_map, components, conflicts = build_alias_map(accepted, live, freq, base_normalize_team)
    return pairs, accepted, review, alias_map, components, conflicts


def uncovered(accepted) -> list:
    """Pares aceptados que el normalizador ACTUAL todavía no une."""
    return [(a, b) for a, b in accepted if normalize_team(a) != normalize_team(b)]


def check_new_aliases(verbose: bool = True) -> list:
    """Para el weekly: avisa (ERROR) los pares nuevos sin cubrir."""
    rows, live, freq = read_inputs()
    pairs, accepted, review, *_ = learn(rows, live, freq)
    missing = uncovered(accepted)
    if missing:
        log.error(f"❌ Nombres de equipo sin unificar: {len(missing)} par(es) nuevos — "
                  + "; ".join(f"{a} = {b} ({pairs[(a, b)]}x)" for a, b in missing[:6])
                  + " → correr scripts/learn_team_aliases.py --write")
    elif verbose:
        print("✅ Nombres de equipo: sin pares nuevos por unificar")
    if review and verbose:
        print("ℹ️  Pares para revisar a mano (nombres que no se parecen):")
        for (a, b), n in review[:10]:
            print(f"   {n:>4}x  {a} = {b}")
    return missing


def main(write: bool) -> int:
    rows, live, freq = read_inputs()
    pairs, accepted, review, alias_map, components, conflicts = learn(rows, live, freq)
    print(f"Pares con soporte ≥ {MIN_SUPPORT}: {sum(1 for n in pairs.values() if n >= MIN_SUPPORT)} · "
          f"aceptados: {len(accepted)} · para revisar: {len(review)} · conflictos: {len(conflicts)}")
    print(f"Alias en el mapa: {len(alias_map)} (grupos: {len(components)})\n")
    for canonical, names in components:
        print(f"   {canonical:<28} ← {', '.join(n for n in names if n != canonical)}")
    for (a, b), n in review:
        print(f"   ?? revisar {n}x: {a} = {b}")
    for c in conflicts:
        print(f"   ⚠️  conflicto: {c}")
    if not write:
        print("\nEN SECO: no se escribió nada. Repetir con --write.")
        return 0
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "policy": {"min_support": MIN_SUPPORT,
                   "rule": "misma fecha (±1 día), mismo marcador y mismo equipo del mismo lado",
                   "canonical": "el nombre de la API de cuotas (upcoming_matches)"},
        "aliases": alias_map,
        "groups": [{"canonical": c, "names": n} for c, n in components],
    }
    ALIASES_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n✅ Escrito {ALIASES_FILE} ({len(alias_map)} alias)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--write", action="store_true", help="escribe config/team_aliases.json")
    ap.add_argument("--check", action="store_true", help="solo avisa pares nuevos sin cubrir")
    args = ap.parse_args()
    if args.check:
        sys.exit(1 if check_new_aliases() else 0)
    sys.exit(main(args.write))
