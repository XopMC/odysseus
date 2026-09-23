"""The canonical SQLite engine tolerates bounded checkpoint/write contention."""

from sqlalchemy import create_engine

import core.database  # noqa: F401 - registers the Engine connect listener


def test_sqlite_connections_wait_for_writer_without_disabling_foreign_keys(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'busy.db'}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30000
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    engine.dispose()
