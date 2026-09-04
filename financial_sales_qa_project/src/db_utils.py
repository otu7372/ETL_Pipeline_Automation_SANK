"""
db_utils.py
-----------
Thin wrapper around mysql-connector so every validation script connects
the same way. Every validation stage (2-6) will import get_connection()
from here rather than opening its own raw connections.
"""

import mysql.connector
import pandas as pd
from config_loader import load_config

CFG = load_config()


def get_connection(db_key: str):
    """
    db_key is one of the logical names in config.yaml -> mysql.databases,
    e.g. 'source_retail', 'staging', 'datamart', 'reporting'.
    """
    databases = CFG["mysql"]["databases"]
    if db_key not in databases:
        raise KeyError(
            f"'{db_key}' is not a known database key. "
            f"Valid keys: {list(databases.keys())}"
        )
    return mysql.connector.connect(
        host=CFG["mysql"]["host"],
        port=CFG["mysql"]["port"],
        user=CFG["mysql"]["user"],
        password=CFG["mysql"]["password"],
        database=databases[db_key],
    )


def run_query_df(db_key: str, sql: str, params=None) -> pd.DataFrame:
    """Run a SQL query against a given logical database and return a DataFrame."""
    conn = get_connection(db_key)
    try:
        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
    return df


def table_row_count(db_key: str, table_name: str) -> int:
    df = run_query_df(db_key, f"SELECT COUNT(*) AS cnt FROM {table_name}")
    return int(df["cnt"].iloc[0])
