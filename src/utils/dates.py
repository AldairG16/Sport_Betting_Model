"""
src/utils/dates.py
==================
Fechas de los archivos de resultados, con formato EXPLÍCITO (23-oct-26).

pd.to_datetime(..., dayfirst=True) sin formato cae a dateutil elemento por
elemento, y dateutil con dayfirst lee "2025-02-06" como 2-JUN: el día y el
mes cruzados en todo partido con día ≤ 12. Así entraron 2,271 partidos
argentinos duplicados con la fecha espejo (scripts/fix_argentina_swapped_dates.py).
Aquí cada forma se lee con su formato y lo que no encaja queda NaT.
"""

import pandas as pd

FORMATS = (
    (r"^\d{4}-\d{2}-\d{2}", "%Y-%m-%d", 10),      # ISO (con o sin hora): los 10 primeros
    (r"^\d{1,2}/\d{1,2}/\d{4}$", "%d/%m/%Y", None),  # football-data
    (r"^\d{1,2}/\d{1,2}/\d{2}$", "%d/%m/%y", None),  # football-data, temporadas viejas
)


def parse_match_dates(values) -> pd.Series:
    """Serie de fechas (día primero en las de barra; ISO como ISO). NaT si
    no encaja en ninguna forma."""
    s = pd.Series(values, dtype="object").astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for pattern, fmt, cut in FORMATS:
        mask = s.str.match(pattern) & out.isna()
        if mask.any():
            part = s[mask].str[:cut] if cut else s[mask]
            out[mask] = pd.to_datetime(part, format=fmt, errors="coerce")
    return out


def parse_match_date(value) -> pd.Timestamp | None:
    """Una sola fecha; None si no encaja."""
    d = parse_match_dates([value]).iloc[0]
    return None if pd.isna(d) else d
