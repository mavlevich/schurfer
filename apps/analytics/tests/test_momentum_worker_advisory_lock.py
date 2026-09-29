"""Worker lock checks preserve task cancellation while dropping a lost session."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from schurfer_analytics.momentum_flow_paper_repository import MomentumFlowPaperRepository
from schurfer_analytics.momentum_flow_watch_repository import MomentumFlowWatchRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_class", (MomentumFlowPaperRepository, MomentumFlowWatchRepository)
)
async def test_lock_check_preserves_cancellation_and_discards_connection(
    repository_class: type[MomentumFlowPaperRepository] | type[MomentumFlowWatchRepository],
) -> None:
    repository = repository_class(Mock())
    connection = Mock()
    connection.execute = AsyncMock(side_effect=asyncio.CancelledError)
    connection.invalidate = AsyncMock()
    connection.close = AsyncMock()
    repository._lock_connection = connection
    repository._lock_backend_identity = (123, datetime(2026, 9, 29, tzinfo=UTC))

    with pytest.raises(asyncio.CancelledError):
        await repository.assert_worker_lock()

    connection.invalidate.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert repository._lock_connection is None
    assert repository._lock_backend_identity is None
