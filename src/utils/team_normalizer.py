import unicodedata

from src.utils.team_name_map import TEAM_NAME_MAP
from src.utils.team_alias_map import TEAM_ALIASES
from src.utils.national_team_aliases import NATIONAL_TEAM_ALIASES
from src.utils.team_identity import load_learned_aliases

# Alias aprendidos de los datos (config/team_aliases.json, 1-oct-26): el mismo
# club escrito distinto según la fuente ("man united" → "manchester united").
# Ver src/utils/team_identity.py y scripts/learn_team_aliases.py.
LEARNED_TEAM_ALIASES = load_learned_aliases()


# =========================
# CLEAN BASE
# =========================

def clean_name(name):

    if not name:
        return ""

    name = name.lower().strip()

    # quitar acentos
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()

    # limpiar caracteres
    name = (
        name.replace(".", "")
        .replace(",", "")
        .replace("'", "")      # "Nott'm Forest" → "nottm forest" (igual que la API)
        .replace("’", "")      # apóstrofo tipográfico
        .replace("-", " ")
        .replace("_", " ")
        .strip()
    )

    # quitar dobles espacios
    name = " ".join(name.split())

    return name


# =========================
# MAIN NORMALIZER
# =========================

def normalize_team(name):
    """Nombre canónico: los mapas a mano y, al final, los alias aprendidos."""
    name = base_normalize_team(name)

    # STEP 5: alias aprendidos de los datos (mismo club, otra fuente)
    return LEARNED_TEAM_ALIASES.get(name, name)


def base_normalize_team(name):
    """Los mapas a mano, sin la capa aprendida (sus salidas son las claves
    de config/team_aliases.json)."""

    name = clean_name(name)

    # STEP 0: aliases de selecciones nacionales (preferente — corre antes
    # que el resto porque "korea republic" → "south korea" debe resolverse
    # antes de que TEAM_ALIASES lo tome por error como un club).
    name = NATIONAL_TEAM_ALIASES.get(name, name)

    # STEP 1 alias
    name = TEAM_ALIASES.get(name, name)

    # STEP 2 map
    name = TEAM_NAME_MAP.get(name, name)

    # 🔥 STEP 3 (CRÍTICO): segundo alias pass
    name = TEAM_ALIASES.get(name, name)

    # STEP 4: segundo pass nacional — captura cuando STEP 2 dejó un nombre
    # canónico que aún tiene alias (raro pero defensivo).
    name = NATIONAL_TEAM_ALIASES.get(name, name)

    return name