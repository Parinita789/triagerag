import psycopg
from loginsights.config import settings

def get_conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)