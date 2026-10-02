"""
src/utils/team_identity.py
==========================
Un mismo club escrito distinto según la fuente (1-oct-26).

football-data escribe "Man United", "Paris SG", "Leeds"; The Odds API,
"Manchester United", "Paris Saint Germain", "Leeds United". Los mapas a mano
(team_alias_map, team_name_map) cubrían una parte; el resto quedaba partido:
el mismo partido guardado dos veces y la historia del equipo repartida en dos
nombres (Dixon-Coles los trataba como dos equipos). Medido el 1-oct-26: 104
pares de nombres, algunos con ~800 partidos repetidos (leeds / leeds united).

Cómo se descubre, sin juicio humano: un equipo no juega dos partidos el mismo
día. Dos filas con la misma fecha (±1 día, por zona horaria), el mismo
marcador y el mismo equipo en el mismo lado son el mismo partido, así que el
rival tiene dos nombres. Se aceptan solos los pares de nombres compatibles
("leeds" ⊂ "leeds united"); los incompatibles, solo verificados a mano
(ALLOW). Si los DOS equipos estaban escritos distinto, el par sale en una
segunda ronda, con el primero ya unido (scripts/learn_team_aliases.py).
DENY existe porque PSG y Paris FC —clubes distintos— salían como "el mismo
partido": football-data guardó partidos de Paris FC también como "paris" y
"paris saint germain" (scripts/fix_team_identities.py los corrige con el
CSV original).

Canónico = el nombre que usa la API de cuotas (upcoming_matches): es con el
que el pipeline busca la historia del equipo.

Funciones puras; la lectura de la base y la escritura del JSON están en
scripts/learn_team_aliases.py.
"""

import json
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

ALIASES_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "team_aliases.json"
# Un solo partido basta si los nombres son compatibles: la regla del mismo día
# ya es fuerte, y compatible() exige que un nombre contenga al otro o que sean
# casi iguales. Los pares con soporte 1 y nombres distintos fueron todos
# errores de datos (filas argentinas mal etiquetadas, medido el 1-oct-26).
MIN_SUPPORT = 1


def _pair(x: str, y: str) -> tuple[str, str]:
    return tuple(sorted((x, y)))


# Clubes distintos que aparecían como el mismo partido por filas mal
# etiquetadas, o un nombre ambiguo ("paris" = PSG o Paris FC según la fuente)
DENY = {
    _pair("paris fc", "paris saint germain"),
    _pair("paris", "paris saint germain"),
    _pair("paris", "paris fc"),
    _pair("colon santa fe", "racing"),
    _pair("argentinos jrs", "rosario central"),
}
# Nombres que no se parecen lo suficiente pero son el mismo club (verificado
# el 1-oct-26 con la evidencia de los partidos repetidos)
ALLOW = {
    _pair("ath bilbao", "athletic club"),        # Athletic Club de Bilbao
    _pair("wolverhampton", "wolves"),
    _pair("guimaraes", "vitoria sc"),            # Vitória SC = Vitória de Guimarães
    _pair("qpr", "queens park rangers"),
    _pair("basaksehir", "buyuksehyr"),           # İstanbul Başakşehir
    _pair("ael", "larisa"),                      # AEL Larissa
    _pair("man city", "manchester city"),
    _pair("man united", "manchester united"),
    _pair("paris sg", "paris saint germain"),
    _pair("fc cologne", "fc koln"),
    _pair("st gilloise", "union saint gilloise"),
    _pair("sp lisbon", "sporting lisbon"),
    _pair("shanghai port", "shanghai sipg fc"),  # SIPG pasó a llamarse Port
    _pair("henan fc", "henan songshan longmen"),
    _pair("shenzhen peng city fc", "shenzhen xinpengcheng"),  # "Xinpengcheng" = Peng City en pinyin
    _pair("beijing fc", "beijing guoan"),        # The Odds API: 4 de 4 partidos = Guoan
}


def _plain(s: str) -> str:
    return s.replace("'", "")


def compatible(x: str, y: str) -> bool:
    """Nombres que pueden ser el mismo club sin revisión humana: uno contiene
    al otro ("leeds" ⊂ "leeds united") o son casi iguales ("goztep" /
    "goztepe"). Compartir una palabra NO alcanza: "real madrid" y "real
    betis" no son compatibles. Es condición necesaria, no prueba: lo que
    decide es la evidencia del mismo partido, que dos clubes distintos
    ("independiente" e "independiente rivadavia") no pueden dar."""
    a, b = _plain(x), _plain(y)
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.75


def find_alias_pairs(rows) -> Counter:
    """
    rows: DataFrame con id, d (fecha), h, a (nombres en minúsculas), hg, ag.
    Devuelve {(nombre1, nombre2): partidos de soporte}.
    """
    import pandas as pd
    seen: dict = {}
    if rows is None or len(rows) == 0:
        return Counter()
    base = rows.copy()
    base["d"] = pd.to_datetime(base["d"])
    for shift in (0, 1):
        other = base.copy()
        other["d"] = other["d"] + pd.Timedelta(days=shift)
        for same, diff in (("h", "a"), ("a", "h")):
            j = base.merge(other, on=["d", same, "hg", "ag"], suffixes=("_1", "_2"))
            j = j[(j[f"{diff}_1"] != j[f"{diff}_2"]) & (j["id_1"] != j["id_2"])]
            for x, y, i1, i2 in zip(j[f"{diff}_1"], j[f"{diff}_2"], j["id_1"], j["id_2"]):
                # soporte = pares de filas distintos (cada partido aparece
                # desde las dos filas el mismo día, y una vez si hay ±1 día)
                seen.setdefault(_pair(x, y), set()).add(frozenset((int(i1), int(i2))))
    return Counter({k: len(v) for k, v in seen.items()})


def classify_pairs(pairs: Counter, min_support: int = MIN_SUPPORT):
    """(aceptados, para_revisar). Para revisar: soporte suficiente pero
    nombres incompatibles y sin verificación (o en DENY)."""
    accepted, review = [], []
    for p, n in pairs.items():
        if n < min_support:
            continue
        if p in DENY:
            continue
        if p in ALLOW or compatible(*p):
            accepted.append(p)
        else:
            review.append((p, n))
    return sorted(accepted), sorted(review, key=lambda t: -t[1])


def build_alias_map(accepted, live_names, freq, base_normalize):
    """
    accepted: pares (a, b) del mismo club. live_names: nombres que usa la API
    de cuotas. freq: {nombre: filas}. base_normalize: el normalizador SIN la
    capa aprendida (las claves del mapa son su salida).

    Devuelve (alias -> canónico, componentes, conflictos).
    """
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in accepted:
        parent[find(a)] = find(b)
    comps: dict = {}
    for n in list(parent):
        comps.setdefault(find(n), set()).add(n)

    live = set(live_names)
    alias_map, components, conflicts = {}, [], []
    for names in comps.values():
        in_live = [n for n in names if n in live or base_normalize(n) in live]
        pool = in_live or list(names)
        choice = max(pool, key=lambda n: (freq.get(n, 0), n))
        canonical = base_normalize(choice)
        components.append((canonical, sorted(names)))
        for n in names:
            k = base_normalize(n)
            if k == canonical:
                continue
            if alias_map.get(k, canonical) != canonical:
                conflicts.append((k, alias_map[k], canonical))
                continue
            alias_map[k] = canonical
    # un canónico nunca puede ser a la vez alias de otro (cadenas o ciclos)
    for k in [k for k in alias_map if k in alias_map.values()]:
        conflicts.append((k, alias_map.pop(k), "es canónico de otro grupo"))
    return dict(sorted(alias_map.items())), sorted(components), conflicts


def load_learned_aliases(path: Path = ALIASES_FILE) -> dict:
    """Mapa aprendido (alias -> canónico); {} si el archivo no existe."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return dict(data.get("aliases", {}))
