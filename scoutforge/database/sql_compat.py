"""Small dialect-compatibility helpers.

Production is MySQL; the test-suite and local scratch databases are sqlite. SQL
has no portable "hour of day" or "day of" function, so the couple of places that
need one ask here instead of guessing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, func


def dialect_name(bind: Any) -> str:
    """Best-effort dialect name for an engine, connection or session."""
    for holder in (bind, getattr(bind, "sync_engine", None), getattr(bind, "engine", None)):
        name = getattr(getattr(holder, "dialect", None), "name", None)
        if isinstance(name, str):
            return name
    url = getattr(getattr(bind, "url", None), "drivername", "")
    return str(url)


def hour_expression(column: Any, dialect: str) -> ColumnElement[Any]:
    """UTC hour-of-day (0-23) for ``column``.

    MySQL has ``HOUR()`` and sqlite has ``strftime('%H', ...)``; neither is
    portable, and PostgreSQL's ``EXTRACT`` is a third dialect. Callers are
    expected to normalise the result to ``"00".."23"`` in Python.
    """
    if dialect.startswith("mysql") or dialect.startswith("mariadb"):
        return func.hour(column)
    return func.strftime("%H", column)


def date_expression(column: Any, dialect: str) -> ColumnElement[Any]:
    """Calendar date of ``column``.

    ``DATE()`` and sqlite's ``date()`` agree, so this only exists to keep the
    day/hour pair symmetrical and to give one place to change if that ever
    stops being true.
    """
    return func.date(column)


__all__ = ["date_expression", "dialect_name", "hour_expression"]
