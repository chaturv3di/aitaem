"""
aitaem.helpers.csv_to_duckdb - Utility for loading CSV files into a DuckDB database.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import duckdb

from aitaem.connectors.ibis_connector import IbisConnector

logger = logging.getLogger(__name__)

_VALID_TABLE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def load_csvs_to_duckdb(
    csv_path: str | Path,
    db_path: str | Path,
    overwrite: bool = True,
) -> IbisConnector:
    """Load one or more CSV files into a DuckDB database.

    Args:
        csv_path: Path to a single CSV file or a directory containing CSV files.
            For directories, only top-level CSV files are processed.
        db_path: Path where the DuckDB database file will be written.
        overwrite: If True (default), existing tables with the same name are
            replaced. If False, rows from the CSV are appended to existing
            tables; tables that do not yet exist are created.

    Returns:
        An IbisConnector connected to the database at db_path.

    Raises:
        FileNotFoundError: If csv_path does not exist.
        ValueError: If csv_path points to a single file whose stem is not a
            valid table name.
    """
    csv_path = Path(csv_path)
    db_path = Path(db_path)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV path not found: {csv_path}")

    files: list[tuple[Path, str]] = []

    if csv_path.is_file():
        stem = csv_path.stem
        if not _VALID_TABLE_NAME.match(stem):
            raise ValueError(
                f"Filename stem '{stem}' is not a valid table name. "
                "Table names must start with a letter or underscore and "
                "contain only letters, digits, and underscores."
            )
        files.append((csv_path, stem))
    else:
        for csv_file in sorted(csv_path.glob("*.csv")):
            stem = csv_file.stem
            if not _VALID_TABLE_NAME.match(stem):
                logger.warning(
                    "Skipping '%s': filename stem '%s' is not a valid table name "
                    "(must start with a letter or underscore and contain only "
                    "letters, digits, and underscores).",
                    csv_file.name,
                    stem,
                )
                continue
            files.append((csv_file, stem))

    if not files:
        logger.warning("No valid CSV files found at '%s'.", csv_path)

    con = duckdb.connect(str(db_path))
    try:
        for csv_file, table_name in files:
            csv_str = str(csv_file)
            if overwrite:
                con.execute(
                    f"CREATE OR REPLACE TABLE {table_name} AS "
                    f"SELECT * FROM read_csv_auto(?)",
                    [csv_str],
                )
                logger.debug("Created table '%s' from '%s'.", table_name, csv_file.name)
            else:
                exists = con.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_name = ?",
                    [table_name],
                ).fetchone()
                if exists:
                    con.execute(
                        f"INSERT INTO {table_name} SELECT * FROM read_csv_auto(?)",
                        [csv_str],
                    )
                    logger.debug(
                        "Appended rows to table '%s' from '%s'.", table_name, csv_file.name
                    )
                else:
                    con.execute(
                        f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto(?)",
                        [csv_str],
                    )
                    logger.debug("Created table '%s' from '%s'.", table_name, csv_file.name)
    finally:
        con.close()

    connector = IbisConnector("duckdb")
    connector.connect(str(db_path))
    return connector
