from sqlalchemy import create_engine
from config.settings import DB_URL

DATABASE_URL = DB_URL
# Neon corta las conexiones inactivas (~5 min). pool_recycle las cambia antes
# de eso sin costo por consulta; los keepalives de TCP convierten en error la
# que muere a media consulta. Sin pool_pre_ping (22-oct-26): agrega un viaje
# de red a cada consulta (+166 ms medidos desde la PC del dueño, +~65 ms desde
# GitHub) y el pipeline hace miles.
_KEEPALIVE = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 5}
engine = create_engine(DATABASE_URL, pool_recycle=240,
                       connect_args=_KEEPALIVE if str(DATABASE_URL).startswith("postgres") else {})
