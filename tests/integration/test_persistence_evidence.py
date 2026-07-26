"""PostgreSQL enforcement for cross-aggregate and evidence invariants."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.database import Database
from app.models import (
    AnalyticsEvent,
    AuditRecord,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("model", [AuditRecord, AnalyticsEvent])
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_audit_and_analytics_evidence_is_append_only(
    model: type[AuditRecord] | type[AnalyticsEvent], operation: str
) -> None:
    database = Database(get_settings())
    record_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            if model is AuditRecord:
                session.add(
                    AuditRecord(
                        id=record_id,
                        event_type="integrity_test",
                        entity_type="test",
                        entity_id=uuid4(),
                        evidence={"immutable": True},
                    )
                )
            else:
                session.add(
                    AnalyticsEvent(
                        id=record_id,
                        event_key=f"integrity_test:{record_id}",
                        event_type="integrity_test",
                        player_id=None,
                        payload={"immutable": True},
                    )
                )

        async with database.session_factory() as session:
            statement = (
                update(model).where(model.id == record_id).values(event_type="mutated")
                if operation == "update"
                else delete(model).where(model.id == record_id)
            )
            with pytest.raises(DBAPIError, match="append-only"):
                await session.execute(statement)
            await session.rollback()
    finally:
        await database.dispose()
