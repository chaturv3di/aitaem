#!/usr/bin/env python3
"""
Create the ad_campaigns DuckDB database from the CSV source file.

Run once from the project root before using the examples:
    python examples/data/setup_db.py

Two approaches are provided (both produce identical results):
  - via_raw_sql()      : uses the duckdb Python package directly
  - via_connector()    : uses the aitaem IbisConnector (ibis read_csv)
"""

from pathlib import Path

DATA_DIR = Path(__file__).parent
CSV_PATH = DATA_DIR / "ad_campaigns.csv"
DB_PATH = DATA_DIR / "ad_campaigns.duckdb"

COLUMN_TYPES = {
    "date": "DATE",
    "impressions": "INTEGER",
    "clicks": "INTEGER",
    "conversions": "INTEGER",
    "ad_spend": "DOUBLE",
    "revenue": "DOUBLE",
}


def via_raw_sql() -> None:
    """Create the database using the duckdb package and raw SQL."""
    import duckdb

    conn = duckdb.connect(str(DB_PATH))
    conn.execute(f"""
        CREATE OR REPLACE TABLE ad_campaigns AS
        SELECT * FROM read_csv_auto('{CSV_PATH}', types={COLUMN_TYPES})
    """)
    row_count = conn.execute("SELECT COUNT(*) FROM ad_campaigns").fetchone()[0]
    conn.close()
    print(f"Created {DB_PATH}")
    print(f"Table 'ad_campaigns' loaded with {row_count:,} rows.")


def via_connector() -> None:
    """Create the database using the aitaem IbisConnector (ibis read_csv)."""
    from aitaem.connectors.ibis_connector import IbisConnector

    connector = IbisConnector("duckdb")
    connector.connect(str(DB_PATH))
    t = connector.connection.read_csv(
        str(CSV_PATH),
        table_name="ad_campaigns",
        types=COLUMN_TYPES,
    )
    print(f"Created {DB_PATH}")
    print(f"Table 'ad_campaigns' loaded with {t.count().execute():,} rows.")
    connector.close()


if __name__ == "__main__":
    if not CSV_PATH.exists():
        raise FileNotFoundError(
            f"CSV not found: {CSV_PATH}\n"
            "Make sure examples/data/ad_campaigns.csv exists."
        )

    print(f"Reading {CSV_PATH} ...")
    via_connector()
