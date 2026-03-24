"""Tests for aitaem.connectors.backend_specs."""

import pytest

from aitaem.connectors.backend_specs import (
    BACKEND_SPECS,
    BigQueryConfig,
    DuckDBConfig,
    PostgresConfig,
    validate_backend_config,
)
from aitaem.utils.exceptions import ConfigurationError, UnsupportedBackendError


class TestBackendSpecsRegistry:
    def test_registry_contains_all_backends(self):
        assert set(BACKEND_SPECS.keys()) == {"duckdb", "bigquery", "postgres"}

    def test_registry_maps_to_correct_classes(self):
        assert BACKEND_SPECS["duckdb"] is DuckDBConfig
        assert BACKEND_SPECS["bigquery"] is BigQueryConfig
        assert BACKEND_SPECS["postgres"] is PostgresConfig


class TestDuckDBConfig:
    def test_required_field(self):
        cfg = DuckDBConfig(path="analytics.db")
        assert cfg.path == "analytics.db"

    def test_default_read_only(self):
        cfg = DuckDBConfig(path="analytics.db")
        assert cfg.read_only is False

    def test_explicit_read_only(self):
        cfg = DuckDBConfig(path="analytics.db", read_only=True)
        assert cfg.read_only is True

    def test_missing_path_raises(self):
        with pytest.raises(TypeError):
            DuckDBConfig()  # type: ignore[call-arg]


class TestBigQueryConfig:
    def test_required_field(self):
        cfg = BigQueryConfig(project_id="my-project")
        assert cfg.project_id == "my-project"

    def test_default_dataset_id(self):
        cfg = BigQueryConfig(project_id="my-project")
        assert cfg.dataset_id is None

    def test_explicit_dataset_id(self):
        cfg = BigQueryConfig(project_id="my-project", dataset_id="my_dataset")
        assert cfg.dataset_id == "my_dataset"

    def test_missing_project_id_raises(self):
        with pytest.raises(TypeError):
            BigQueryConfig()  # type: ignore[call-arg]


class TestPostgresConfig:
    def test_required_fields(self):
        cfg = PostgresConfig(database="mydb", user="myuser", password="secret")
        assert cfg.database == "mydb"
        assert cfg.user == "myuser"
        assert cfg.password == "secret"

    def test_default_host_and_port(self):
        cfg = PostgresConfig(database="mydb", user="myuser", password="secret")
        assert cfg.host == "localhost"
        assert cfg.port == 5432

    def test_explicit_host_and_port(self):
        cfg = PostgresConfig(
            database="mydb", user="myuser", password="secret",
            host="db.example.com", port=5433,
        )
        assert cfg.host == "db.example.com"
        assert cfg.port == 5433

    def test_missing_database_raises(self):
        with pytest.raises(TypeError):
            PostgresConfig(user="myuser", password="secret")  # type: ignore[call-arg]

    def test_missing_user_raises(self):
        with pytest.raises(TypeError):
            PostgresConfig(database="mydb", password="secret")  # type: ignore[call-arg]

    def test_missing_password_raises(self):
        with pytest.raises(TypeError):
            PostgresConfig(database="mydb", user="myuser")  # type: ignore[call-arg]


class TestValidateBackendConfig:
    def test_valid_duckdb_config(self):
        cfg = validate_backend_config("duckdb", {"path": "analytics.db"})
        assert isinstance(cfg, DuckDBConfig)
        assert cfg.path == "analytics.db"

    def test_valid_bigquery_config(self):
        cfg = validate_backend_config("bigquery", {"project_id": "my-project"})
        assert isinstance(cfg, BigQueryConfig)
        assert cfg.project_id == "my-project"

    def test_valid_postgres_config(self):
        cfg = validate_backend_config(
            "postgres", {"database": "mydb", "user": "myuser", "password": "secret"}
        )
        assert isinstance(cfg, PostgresConfig)
        assert cfg.database == "mydb"
        assert cfg.host == "localhost"
        assert cfg.port == 5432

    def test_postgres_with_all_fields(self):
        cfg = validate_backend_config(
            "postgres",
            {
                "database": "mydb",
                "user": "myuser",
                "password": "secret",
                "host": "db.example.com",
                "port": 5433,
            },
        )
        assert cfg.host == "db.example.com"
        assert cfg.port == 5433

    def test_extra_keys_are_ignored(self):
        # Extra keys (pass-through kwargs) should not cause errors
        cfg = validate_backend_config(
            "duckdb", {"path": "analytics.db", "read_only": True, "unknown_key": "value"}
        )
        assert isinstance(cfg, DuckDBConfig)
        assert cfg.read_only is True

    def test_missing_required_field_raises_configuration_error(self):
        with pytest.raises(ConfigurationError, match="postgres"):
            validate_backend_config("postgres", {"user": "myuser", "password": "secret"})

    def test_error_message_contains_field_name(self):
        with pytest.raises(ConfigurationError, match="database"):
            validate_backend_config("postgres", {"user": "myuser", "password": "secret"})

    def test_error_message_contains_yaml_snippet(self):
        with pytest.raises(ConfigurationError, match="POSTGRES_PASSWORD"):
            validate_backend_config("postgres", {"user": "myuser", "password": "secret"})

    def test_unsupported_backend_raises(self):
        with pytest.raises(UnsupportedBackendError, match="mysql"):
            validate_backend_config("mysql", {"host": "localhost"})

    def test_missing_bigquery_project_id_raises(self):
        with pytest.raises(ConfigurationError, match="project_id"):
            validate_backend_config("bigquery", {})

    def test_missing_duckdb_path_raises(self):
        with pytest.raises(ConfigurationError, match="path"):
            validate_backend_config("duckdb", {})
