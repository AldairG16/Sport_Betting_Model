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

El weekly corre check_new_aliases con la fusión de repetidos del auditor: un
club recién ascendido o una fuente nueva puede traer otro nombre. Busca los
pares ANTES de fusionar (el partido repetido es la evidencia), fusiona, y
avisa como ERROR (llega a Telegram) solo si el nombre raro sigue en la base o
ya se había fusionado en otra corrida: ahí falta el alias (docs/OPERACION.md).
Si estaba solo en partidos repetidos y es la primera vez, la fusión lo
resolvió y queda como nota en el log (5-oct-26: "atl madrid", un partido de
agosto que entró con otro nombre).
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


def variants_of(missing, alias_map: dict, freq: Counter, pairs: Counter) -> list:
    """[(nombre raro, canónico, partidos de soporte)] de los pares sin cubrir.
    Canónico: el que elige el mapa aprendido (el nombre de la API de cuotas);
    si el par no queda resuelto en el mapa (conflicto), el de más partidos."""
    out = []
    for a, b in missing:
        ca, cb = alias_map.get(a, a), alias_map.get(b, b)
        canonical = ca if ca == cb else max((a, b), key=lambda n: (freq.get(n, 0), n))
        out += [(n, canonical, pairs[(a, b)]) for n in (a, b) if n != canonical]
    return out


def classify_new_pairs(found, left: dict, seen_before: set) -> tuple[list, list]:
    """(resueltos, pendientes). found: variants_of(...) de antes de fusionar;
    left: {nombre: partidos que le quedan después}; seen_before: nombres que
    ya se habían fusionado o renombrado en corridas anteriores.

    Resuelto: el raro estaba solo en partidos repetidos, la fusión los borró
    y es la primera vez. Pendiente: sigue en la base (la historia del club
    partida en dos nombres) o vuelve (la fuente lo sigue mandando)."""
    resolved, pending = [], []
    for variant, canonical, n in found:
        k = left.get(variant, 0)
        again = variant in seen_before
        if k == 0 and not again:
            resolved.append((variant, canonical, n))
        else:
            pending.append((variant, canonical, k, again))
    return resolved, pending


def names_left(names) -> dict:
    """{nombre: partidos que tiene hoy en matches} (sin distinguir mayúsculas:
    las selecciones se guardan con ellas y el aprendizaje compara en minúsculas)."""
    from config.database import engine
    if not names:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT name, COUNT(*) FROM (
                SELECT LOWER(home_team) AS name FROM matches WHERE LOWER(home_team) = ANY(:n)
                UNION ALL
                SELECT LOWER(away_team) FROM matches WHERE LOWER(away_team) = ANY(:n)
            ) t GROUP BY name
        """), {"n": sorted(names)}).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def merged_before(names) -> set:
    """Nombres que ya pasaron por una fusión o un renombre (sus filas viejas
    están en matches_identity_backup; los reetiquetados de liga y las stats
    restauradas no cuentan). Se consulta antes de fusionar, así que lo de esta
    corrida no cuenta."""
    from config.database import engine
    if not names:
        return set()
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('matches_identity_backup')")).scalar() is None:
            return set()
        rows = conn.execute(text("""
            SELECT LOWER(name) FROM (
                SELECT home_team AS name, backup_action FROM matches_identity_backup
                UNION ALL
                SELECT away_team, backup_action FROM matches_identity_backup
            ) t
            WHERE LOWER(name) = ANY(:n)
              AND backup_action IN ('delete_duplicate', 'update_identity')
            GROUP BY 1
        """), {"n": sorted(names)}).fetchall()
    return {r[0] for r in rows}


def check_new_aliases(merge=None, verbose: bool = True) -> list:
    """
    Para el weekly: avisa (ERROR) los pares nuevos que siguen pendientes.

    merge: la fusión de repetidos (audit_duplicate_matches del auditor). Corre
    DESPUÉS de buscar los pares, porque el partido repetido es la evidencia.
    Sin merge (--check a mano), todo par nuevo queda pendiente.
    """
    rows, live, freq = read_inputs()
    pairs, accepted, review, alias_map, *_ = learn(rows, live, freq)
    found = variants_of(uncovered(accepted), alias_map, freq, pairs)
    names = {v for v, _, _ in found}
    seen = merged_before(names)
    if merge is not None:
        _, msg = merge()
        print(f"   Partidos repetidos: {msg}")
    resolved, pending = classify_new_pairs(found, names_left(names), seen)
    for variant, canonical, n in resolved:
        print(f"ℹ️  '{variant}' (= '{canonical}') estaba solo en {n} partido(s) guardado(s) "
              f"dos veces; la fusión los unió y ese nombre ya no está en la base")
    if pending:
        log.error("❌ Mismo club con dos nombres: " + "; ".join(
            f"'{v}' → '{c}' ({k} partido(s) con el nombre raro"
            + (", ya se había fusionado antes: la fuente lo sigue mandando" if again else "") + ")"
            for v, c, k, again in pending[:6]) + " → falta el alias (docs/OPERACION.md)")
    elif verbose and not resolved:
        print("✅ Nombres de equipo: sin pares nuevos por unificar")
    if review and verbose:
        print("ℹ️  Pares para revisar a mano (nombres que no se parecen):")
        for (a, b), n in review[:10]:
            print(f"   {n:>4}x  {a} = {b}")
    return pending


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
