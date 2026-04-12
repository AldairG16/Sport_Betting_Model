import unicodedata

from src.utils.team_name_map import TEAM_NAME_MAP
from src.utils.team_alias_map import TEAM_ALIASES


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

    name = clean_name(name)

    # STEP 1 alias
    name = TEAM_ALIASES.get(name, name)

    # STEP 2 map
    name = TEAM_NAME_MAP.get(name, name)

    # 🔥 STEP 3 (CRÍTICO): segundo alias pass
    name = TEAM_ALIASES.get(name, name)

    return name