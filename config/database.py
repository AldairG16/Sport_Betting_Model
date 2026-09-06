from sqlalchemy import create_engine
from config.settings import DB_URL

DATABASE_URL = DB_URL
engine = create_engine(DATABASE_URL)