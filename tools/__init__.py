"""Herramientas internas del repositorio (no forman parte del runtime de apuestas).

Nada bajo `tools/` puede importar `config.settings` ni `config.database`:
`config/settings.py` lanza RuntimeError en tiempo de import cuando `DB_URL`
no esta definida, y estas herramientas deben correr sin ningun secreto.
"""
