"""
src/utils/national_team_aliases.py
==================================
Aliases para selecciones nacionales del Mundial 2026 + clasificatorias.

CONTEXTO
--------
El CSV histórico (martj42/international_results) y the-odds-api usan nombres
levemente distintos para las mismas selecciones. Sin un map dedicado, la
forma reciente de la selección y los partidos próximos del Mundial vivirían
en silos separados → el modelo entrenaría con "United States" mientras que
las apuestas vivas dirían "USA" y nunca se cruzarían los datos.

CONVENCIÓN
----------
Las **claves** son la salida de `clean_name()`: lowercase, sin acentos,
sin puntos/comas/guiones. Es lo que llega al normalizer luego del
preproceso. Por eso `"USA"` se ve como `"usa"`, `"Türkiye"` se ve como
`"turkiye"` (acento removido), y `"United States of America"` queda igual.

Los **valores** son la forma canónica usada por el CSV histórico (también
ya pasada por `clean_name()`). De ese modo, después del normalize, ambos
lados (entrenamiento y predicción) hablan el mismo nombre.

CÓMO AGREGAR UN ALIAS NUEVO
---------------------------
Cuando the-odds-api publique partidos del Mundial 2026 y notes que un
nombre no matchea, agrega aquí la variante:

    "<como llega la variante post clean_name>": "<canonical post clean_name>",

Ejemplo: si the-odds-api manda "Republic of Korea" y el CSV usa
"South Korea", agregar:
    "republic of korea": "south korea",
"""

# Forma canónica = la que usa el CSV histórico (post clean_name).
# Si el CSV cambia, ajustar aquí también.
NATIONAL_TEAM_ALIASES: dict[str, str] = {
    # ── América del Norte (3 sedes WC 2026) ──────────────────────────
    "usa":                              "united states",
    "us":                               "united states",
    "united states of america":         "united states",
    "united states men":                "united states",
    "team usa":                         "united states",

    # ── Asia: Coreas ─────────────────────────────────────────────────
    "korea republic":                   "south korea",
    "korea rep":                        "south korea",
    "republic of korea":                "south korea",
    "korea south":                      "south korea",
    "kor":                              "south korea",
    "korea dpr":                        "north korea",
    "dpr korea":                        "north korea",
    "korea north":                      "north korea",

    # ── Europa central/este ──────────────────────────────────────────
    "czech republic":                   "czechia",
    "cze":                              "czechia",

    # Bosnia: tres variantes comunes
    "bosnia and herzegovina":           "bosnia & herzegovina",
    "bosnia herzegovina":               "bosnia & herzegovina",
    "bosnia-herzegovina":               "bosnia & herzegovina",
    "bosnia":                           "bosnia & herzegovina",

    "fyr macedonia":                    "north macedonia",
    "macedonia":                        "north macedonia",
    "macedonia fyr":                    "north macedonia",

    # Türkiye (acento ya removido por clean_name → turkiye)
    "turkiye":                          "turkey",
    "tur":                              "turkey",

    # Irlanda — el CSV usa "Republic of Ireland", odds-api a veces "Ireland"
    "ireland":                          "republic of ireland",
    "irl":                              "republic of ireland",
    "eire":                             "republic of ireland",
    "republic of ireland":              "republic of ireland",   # idempotente

    # ── África: variantes comunes ────────────────────────────────────
    # Côte d'Ivoire (post clean_name → "cote d'ivoire" o "ivory coast")
    "ivory coast":                      "cote d'ivoire",
    "cote divoire":                     "cote d'ivoire",   # apóstrofe perdida
    "civ":                              "cote d'ivoire",

    "cape verde":                       "cape verde islands",
    "cabo verde":                       "cape verde islands",
    "cv":                               "cape verde islands",

    # DR Congo (RD del Congo)
    "democratic republic of the congo": "dr congo",
    "democratic republic of congo":     "dr congo",
    "congo dr":                         "dr congo",
    "drc":                              "dr congo",
    "zaire":                            "dr congo",
    "rd congo":                         "dr congo",

    # Congo (Brazzaville) — ojo de no confundirla con DR Congo
    "republic of the congo":            "congo",
    "congo brazzaville":                "congo",

    # Egipto, Marruecos, etc. — no necesitan alias (igual nombre)

    # ── Caribe / CONCACAF ────────────────────────────────────────────
    "trinidad and tobago":              "trinidad and tobago",   # canonical
    "trinidad & tobago":                "trinidad and tobago",
    "trinidad":                         "trinidad and tobago",

    "antigua and barbuda":              "antigua and barbuda",
    "antigua & barbuda":                "antigua and barbuda",

    "saint kitts and nevis":            "saint kitts and nevis",
    "st kitts and nevis":               "saint kitts and nevis",
    "saint lucia":                      "saint lucia",
    "st lucia":                         "saint lucia",
    "saint vincent and the grenadines": "saint vincent and the grenadines",
    "st vincent and the grenadines":    "saint vincent and the grenadines",

    "curacao":                          "curacao",   # idempotente; encoding-safe

    # ── Oceanía ──────────────────────────────────────────────────────
    # NZ vs New Zealand
    "nz":                               "new zealand",
    "nzl":                              "new zealand",

    # ── América Latina abreviada ─────────────────────────────────────
    "arg":                              "argentina",
    "bra":                              "brazil",
    "uru":                              "uruguay",
    "col":                              "colombia",
    "ecu":                              "ecuador",
    "par":                              "paraguay",
    "ven":                              "venezuela",
    "bol":                              "bolivia",
    "chi":                              "chile",
    "per":                              "peru",
    "mex":                              "mexico",
    "crc":                              "costa rica",

    # ── Europa: códigos FIFA comunes ─────────────────────────────────
    "esp":                              "spain",
    "ger":                              "germany",
    "fra":                              "france",
    "ita":                              "italy",
    "eng":                              "england",
    "ned":                              "netherlands",
    "por":                              "portugal",
    "bel":                              "belgium",
    "cro":                              "croatia",
    "den":                              "denmark",
    "swe":                              "sweden",
    "nor":                              "norway",
    "swi":                              "switzerland",
    "sui":                              "switzerland",
    "pol":                              "poland",
    "aut":                              "austria",
    "rus":                              "russia",
    "ukr":                              "ukraine",
    "srb":                              "serbia",
    "rou":                              "romania",
    "bul":                              "bulgaria",
    "wal":                              "wales",
    "sco":                              "scotland",
    "nir":                              "northern ireland",

    # ── Otros nombres comunes ────────────────────────────────────────
    "great britain":                    "england",   # cuando aplique como GB
    # NOTA: en partidos olímpicos o Commonwealth puede haber "Team GB" — el
    # modelo no apuesta esos torneos, pero se queda como referencia.
}


def normalize_national_team(name: str) -> str:
    """
    Atajo: aplica solo el alias nacional sobre un nombre ya pasado por
    clean_name(). No reemplaza al normalize_team() general; este map se
    consulta DESDE team_normalizer.py.
    """
    return NATIONAL_TEAM_ALIASES.get(name, name)
