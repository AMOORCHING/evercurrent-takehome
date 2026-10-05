from typer.testing import CliRunner

from digest.cli import app
from digest.db import connect, create_schema
from digest.models import TABLE_MODELS


def _schema_snapshot(conn):
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def test_create_schema_twice(tmp_path):
    db_path = tmp_path / "digest.db"

    conn = connect(db_path)
    create_schema(conn)
    first = _schema_snapshot(conn)
    create_schema(conn)
    assert _schema_snapshot(conn) == first
    conn.close()

    conn = connect(db_path)
    create_schema(conn)
    assert _schema_snapshot(conn) == first
    conn.close()


def test_every_table_has_a_matching_model(tmp_path):
    conn = connect(tmp_path / "digest.db")
    create_schema(conn)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == set(TABLE_MODELS)

    for table, model in TABLE_MODELS.items():
        columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
        assert columns == list(model.model_fields), table
    conn.close()


def test_help_lists_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("ingest", "run", "eval"):
        assert command in result.output
