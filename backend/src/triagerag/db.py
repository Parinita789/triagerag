import psycopg
from triagerag.config import settings

def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)