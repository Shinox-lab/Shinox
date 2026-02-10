"""
Integration tests for the AssignmentStore.

Requires a running PostgreSQL instance with the squad_assignments table.
Skipped automatically when DATABASE_URL is not set.

Run manually:
    DATABASE_URL=postgresql://admin:adminpassword@localhost:5432/agentsquaddb \
        python -m pytest tests/test_assignment_store.py -v
"""

import asyncio
import os

import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set — skipping integration tests"
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def store():
    from assignment_store import AssignmentStore

    s = AssignmentStore(DATABASE_URL)
    await s.initialize()
    yield s
    # Cleanup test data
    await s._pool.execute(
        "DELETE FROM squad_assignments WHERE conversation_id LIKE 'test-%'"
    )
    await s.close()


CONV = "test-conv-001"
AGENT = "test-agent-001"


@pytest.mark.asyncio
async def test_track_and_get(store):
    await store.track_assignment(CONV, AGENT, "Do something")
    row = await store.get_assignment(CONV, AGENT)

    assert row is not None
    assert row["conversation_id"] == CONV
    assert row["agent_id"] == AGENT
    assert row["instruction"] == "Do something"
    assert row["check_sent"] is False
    assert row["collaboration_in_progress"] is False


@pytest.mark.asyncio
async def test_upsert_resets_flags(store):
    await store.track_assignment(CONV, AGENT, "First task")
    await store.mark_check_sent(CONV, AGENT)

    row = await store.get_assignment(CONV, AGENT)
    assert row["check_sent"] is True

    # Re-track should reset
    await store.track_assignment(CONV, AGENT, "Second task")
    row = await store.get_assignment(CONV, AGENT)
    assert row["check_sent"] is False
    assert row["instruction"] == "Second task"


@pytest.mark.asyncio
async def test_mark_check_sent(store):
    await store.track_assignment(CONV, AGENT, "Task")
    await store.mark_check_sent(CONV, AGENT)

    row = await store.get_assignment(CONV, AGENT)
    assert row["check_sent"] is True


@pytest.mark.asyncio
async def test_grant_collaboration_grace(store):
    await store.track_assignment(CONV, AGENT, "Task")
    await store.grant_collaboration_grace(CONV, AGENT)

    row = await store.get_assignment(CONV, AGENT)
    assert row["collaboration_in_progress"] is True


@pytest.mark.asyncio
async def test_remove_assignment(store):
    await store.track_assignment(CONV, AGENT, "Task")
    await store.remove_assignment(CONV, AGENT)

    row = await store.get_assignment(CONV, AGENT)
    assert row is None


@pytest.mark.asyncio
async def test_get_nonexistent_returns_none(store):
    row = await store.get_assignment("no-such-conv", "no-such-agent")
    assert row is None


@pytest.mark.asyncio
async def test_timed_out_first_check(store):
    # Insert with an old assigned_at by using raw SQL
    await store._pool.execute(
        """
        INSERT INTO squad_assignments
               (conversation_id, agent_id, instruction, assigned_at, check_sent, collaboration_in_progress)
        VALUES ($1, $2, $3, NOW() - interval '300 seconds', FALSE, FALSE)
        ON CONFLICT (conversation_id, agent_id) DO UPDATE
            SET assigned_at = NOW() - interval '300 seconds',
                check_sent = FALSE
        """,
        CONV,
        AGENT,
        "Old task",
    )

    rows = await store.get_timed_out_first_check(120)
    conv_agents = {(r["conversation_id"], r["agent_id"]) for r in rows}
    assert (CONV, AGENT) in conv_agents


@pytest.mark.asyncio
async def test_timed_out_second_check(store):
    await store._pool.execute(
        """
        INSERT INTO squad_assignments
               (conversation_id, agent_id, instruction, assigned_at, check_sent, collaboration_in_progress)
        VALUES ($1, $2, $3, NOW() - interval '300 seconds', TRUE, FALSE)
        ON CONFLICT (conversation_id, agent_id) DO UPDATE
            SET assigned_at = NOW() - interval '300 seconds',
                check_sent = TRUE
        """,
        CONV,
        AGENT,
        "Old task checked",
    )

    rows = await store.get_timed_out_second_check(120)
    conv_agents = {(r["conversation_id"], r["agent_id"]) for r in rows}
    assert (CONV, AGENT) in conv_agents
