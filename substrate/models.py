# substrate/models.py
from pathlib import Path
from substrate.db import get_connection

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "storage"
    / "postgres"
    / "schema.sql"
)

def initialize_database():
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)

        conn.commit()