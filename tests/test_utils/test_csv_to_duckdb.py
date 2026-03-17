"""
tests.test_utils.test_csv_to_duckdb - Tests for load_csvs_to_duckdb utility.
"""

import pytest

from aitaem.connectors.ibis_connector import IbisConnector
from aitaem.helpers.csv_to_duckdb import load_csvs_to_duckdb


@pytest.fixture
def single_csv(tmp_path):
    """A single CSV file with valid filename."""
    f = tmp_path / "sales.csv"
    f.write_text("id,amount\n1,100\n2,200\n3,300\n")
    return f


@pytest.fixture
def csv_dir(tmp_path):
    """A directory with multiple CSV files, one with an invalid name."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "orders.csv").write_text("order_id,total\n1,50\n2,75\n")
    (d / "customers.csv").write_text("customer_id,name\n1,Alice\n2,Bob\n")
    (d / "bad-name.csv").write_text("x,y\n1,2\n")  # invalid: contains hyphen
    return d


class TestSingleFile:
    def test_loads_single_csv(self, single_csv, tmp_path):
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(single_csv, db_path)
        assert isinstance(conn, IbisConnector)
        assert conn.is_connected
        table = conn.get_table("sales")
        df = table.execute()
        assert list(df.columns) == ["id", "amount"]
        assert len(df) == 3

    def test_returns_connected_connector(self, single_csv, tmp_path):
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(single_csv, db_path)
        assert conn.is_connected

    def test_db_file_created(self, single_csv, tmp_path):
        db_path = tmp_path / "test.db"
        load_csvs_to_duckdb(single_csv, db_path)
        assert db_path.exists()

    def test_invalid_filename_raises(self, tmp_path):
        csv = tmp_path / "bad-name.csv"
        csv.write_text("a,b\n1,2\n")
        db_path = tmp_path / "test.db"
        with pytest.raises(ValueError, match="bad-name"):
            load_csvs_to_duckdb(csv, db_path)

    def test_missing_path_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_csvs_to_duckdb(tmp_path / "nonexistent.csv", tmp_path / "test.db")


class TestDirectory:
    def test_loads_valid_csvs(self, csv_dir, tmp_path):
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(csv_dir, db_path)
        assert isinstance(conn, IbisConnector)
        orders = conn.get_table("orders").execute()
        customers = conn.get_table("customers").execute()
        assert len(orders) == 2
        assert len(customers) == 2

    def test_skips_invalid_filename(self, csv_dir, tmp_path):
        """bad-name.csv should be skipped without error."""
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(csv_dir, db_path)
        from aitaem.utils.exceptions import TableNotFoundError
        with pytest.raises(TableNotFoundError):
            conn.get_table("bad-name")

    def test_skips_invalid_logs_warning(self, csv_dir, tmp_path, caplog):
        import logging
        db_path = tmp_path / "test.db"
        with caplog.at_level(logging.WARNING, logger="aitaem.utils.csv_to_duckdb"):
            load_csvs_to_duckdb(csv_dir, db_path)
        assert any("bad-name.csv" in r.message for r in caplog.records)

    def test_empty_dir_no_error(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(empty, db_path)
        assert isinstance(conn, IbisConnector)

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_csvs_to_duckdb(tmp_path / "no_such_dir", tmp_path / "test.db")


class TestOverwriteAndAppend:
    def test_overwrite_replaces_table(self, tmp_path):
        csv1 = tmp_path / "data.csv"
        csv1.write_text("val\n1\n2\n")
        db_path = tmp_path / "test.db"
        load_csvs_to_duckdb(csv1, db_path, overwrite=True)

        csv2 = tmp_path / "data2.csv"
        csv2.write_text("val\n99\n")
        # Rename to same table name to test overwrite
        csv2.rename(tmp_path / "data.csv")

        conn = load_csvs_to_duckdb(tmp_path / "data.csv", db_path, overwrite=True)
        df = conn.get_table("data").execute()
        assert len(df) == 1
        assert df["val"].iloc[0] == 99

    def test_append_adds_rows(self, tmp_path):
        csv1 = tmp_path / "events.csv"
        csv1.write_text("id\n1\n2\n")
        db_path = tmp_path / "test.db"
        load_csvs_to_duckdb(csv1, db_path, overwrite=True)

        csv2 = tmp_path / "events.csv"
        csv2.write_text("id\n3\n4\n")
        conn = load_csvs_to_duckdb(csv2, db_path, overwrite=False)
        df = conn.get_table("events").execute()
        assert len(df) == 4

    def test_append_creates_table_if_absent(self, tmp_path):
        csv = tmp_path / "new_table.csv"
        csv.write_text("x\n10\n20\n")
        db_path = tmp_path / "test.db"
        conn = load_csvs_to_duckdb(csv, db_path, overwrite=False)
        df = conn.get_table("new_table").execute()
        assert len(df) == 2
