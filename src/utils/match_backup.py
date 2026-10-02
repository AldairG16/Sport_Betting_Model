"""
src/utils/match_backup.py
=========================
Respaldo de filas de `matches` antes de cambiarlas o borrarlas (1-oct-26).

En `matches` no se borra nada sin copia: cada fila que se reetiqueta, se
renombra o se fusiona con su repetida se copia antes, tal cual, a
matches_identity_backup, con la hora y la acción. Deshacer es copiar de vuelta.
"""

from sqlalchemy import text

BACKUP_DDL = """
    CREATE TABLE IF NOT EXISTS matches_identity_backup AS
    SELECT m.*, NOW()::timestamptz AS backup_at, ''::text AS backup_action
    FROM matches m WITH NO DATA
"""


def backup_matches(conn, ids, action: str, delete_ids=()) -> None:
    """Copia las filas `ids` de matches a matches_identity_backup dentro de la
    transacción de `conn`. Las de `delete_ids` quedan con la acción
    'delete_duplicate'; el resto, con `action`."""
    conn.execute(text(BACKUP_DDL))
    conn.execute(text("""
        INSERT INTO matches_identity_backup
        SELECT m.*, NOW(), CASE WHEN m.id = ANY(:del) THEN 'delete_duplicate' ELSE :action END
        FROM matches m WHERE m.id = ANY(:ids)
    """), {"ids": [int(i) for i in ids], "del": [int(i) for i in delete_ids], "action": action})
