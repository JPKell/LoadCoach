"""loadcoach.infrastructure.db.repositories.settings — the ``settings`` table's only writer.

Security-relevant configuration stays in ``config.toml``/environment variables only; this table is
the runtime key-value store for small, non-secret facts LoadCoach records about its own operation
(data model §2, ``settings``), mirroring FreeWeight's own repository of the same name and shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weightsdb import upsert

from loadcoach.infrastructure.db.models import Setting

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = ["SettingsRepository"]


class SettingsRepository:
    """Reads and writes :class:`~loadcoach.infrastructure.db.models.Setting` rows.

    Stateless: holds no session and no cache, so one instance is safely shared across requests.
    """

    def get(self, session: Session, key: str) -> Any | None:  # noqa: ANN401 — a JSON value's shape is the caller's
        """Return the JSON value stored under ``key``, or ``None`` if it has never been set."""
        setting = session.get(Setting, key)
        return setting.value_json if setting is not None else None

    def set(self, session: Session, key: str, value: Any, *, now: datetime) -> None:  # noqa: ANN401
        """Store ``value`` under ``key``, replacing whatever was there.

        Args:
            session: The caller's active session.
            key: The setting's name.
            value: A JSON-serializable value (already rendered — this method does not canonicalize
                it).
            now: The instant to record as ``updated_at``. Injected so callers are deterministic in
                tests, exactly as every other upsert in the suite is.
        """
        upsert(
            session,
            Setting,
            values={"key": key, "value_json": value, "updated_at": now},
            index_elements=["key"],
        )
