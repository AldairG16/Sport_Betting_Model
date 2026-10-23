from sqlalchemy import create_engine
from config.settings import DB_URL

DATABASE_URL = DB_URL
# Neon corta las conexiones inactivas. Sin pre_ping ni keepalives, una
# conexión muerta dejaba la consulta esperando para siempre (22-oct-26: dos
# ensayos colgados una hora en get_team_cards). pre_ping descarta la del pool
# que ya no responde; los keepalives de TCP convierten en error, en ~1.5 min,
# la que muere a media consulta.
_KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 5}
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       connect_args=_KEEPALIVE if str(DATABASE_URL).startswith("postgres") else {})
