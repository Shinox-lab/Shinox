"""
PostgreSQL-backed assignment tracker for the Squad Lead agent.

Replaces the in-memory _assignment_tracker dict so that active task
assignments survive restarts and the timeout monitor can pick them up
after a crash.
"""

import logging
from typing import Any, Dict, List, Optional

import asyncpg

logger = logging.getLogger(__name__)


class AssignmentStore:
    """Persistent assignment storage using asyncpg."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=5)
        logger.info("Assignment store initialized (PostgreSQL)")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            logger.info("Assignment store connection pool closed")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def track_assignment(
        self, conversation_id: str, agent_id: str, instruction: str
    ) -> None:
        """Insert or update an assignment (resets check_sent and collaboration flags)."""
        await self._pool.execute(
            """
            INSERT INTO squad_assignments
                   (conversation_id, agent_id, instruction, assigned_at, check_sent, collaboration_in_progress)
            VALUES ($1, $2, $3, NOW(), FALSE, FALSE)
            ON CONFLICT (conversation_id, agent_id) DO UPDATE
                SET instruction = EXCLUDED.instruction,
                    assigned_at = NOW(),
                    check_sent  = FALSE,
                    collaboration_in_progress = FALSE
            """,
            conversation_id,
            agent_id,
            instruction[:500],
        )

    async def mark_check_sent(self, conversation_id: str, agent_id: str) -> None:
        await self._pool.execute(
            """
            UPDATE squad_assignments
               SET check_sent = TRUE
             WHERE conversation_id = $1 AND agent_id = $2
            """,
            conversation_id,
            agent_id,
        )

    async def grant_collaboration_grace(
        self, conversation_id: str, agent_id: str
    ) -> None:
        """Reset the timer and flag collaboration so the timeout monitor backs off."""
        await self._pool.execute(
            """
            UPDATE squad_assignments
               SET assigned_at = NOW(),
                   collaboration_in_progress = TRUE
             WHERE conversation_id = $1 AND agent_id = $2
            """,
            conversation_id,
            agent_id,
        )

    async def remove_assignment(self, conversation_id: str, agent_id: str) -> None:
        await self._pool.execute(
            """
            DELETE FROM squad_assignments
             WHERE conversation_id = $1 AND agent_id = $2
            """,
            conversation_id,
            agent_id,
        )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_assignment(
        self, conversation_id: str, agent_id: str
    ) -> Optional[Dict[str, Any]]:
        row = await self._pool.fetchrow(
            """
            SELECT conversation_id, agent_id, instruction, assigned_at,
                   check_sent, collaboration_in_progress
              FROM squad_assignments
             WHERE conversation_id = $1 AND agent_id = $2
            """,
            conversation_id,
            agent_id,
        )
        return dict(row) if row else None

    async def get_timed_out_first_check(
        self, timeout_secs: int
    ) -> List[Dict[str, Any]]:
        """Assignments that exceeded the first timeout and haven't been checked."""
        rows = await self._pool.fetch(
            """
            SELECT conversation_id, agent_id, instruction, assigned_at
              FROM squad_assignments
             WHERE check_sent = FALSE
               AND assigned_at < NOW() - make_interval(secs => $1::double precision)
            """,
            float(timeout_secs),
        )
        return [dict(r) for r in rows]

    async def get_timed_out_second_check(
        self, timeout_secs: int
    ) -> List[Dict[str, Any]]:
        """Assignments that exceeded 2x timeout after first check was already sent."""
        rows = await self._pool.fetch(
            """
            SELECT conversation_id, agent_id, instruction, assigned_at
              FROM squad_assignments
             WHERE check_sent = TRUE
               AND assigned_at < NOW() - make_interval(secs => $1::double precision)
            """,
            float(timeout_secs * 2),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Session continuity queries
    # ------------------------------------------------------------------

    async def get_pending_blockers(
        self, conversation_id: str
    ) -> List[Dict[str, Any]]:
        """Return pending tasks and HITL requests that block session completion.

        Queries ``tasks`` for rows still in-flight and ``hitl_requests``
        for unresolved human approvals tied to this conversation.
        """
        blockers: List[Dict[str, Any]] = []

        task_rows = await self._pool.fetch(
            """
            SELECT id, agent_id, title, status
              FROM tasks
             WHERE group_chat_name = $1
               AND status IN ('PENDING', 'IN_PROGRESS', 'HITL_REVIEW')
            """,
            conversation_id,
        )
        for r in task_rows:
            blockers.append({
                "type": "task",
                "id": str(r["id"]),
                "agent_id": r["agent_id"],
                "title": r["title"],
                "status": r["status"],
            })

        hitl_rows = await self._pool.fetch(
            """
            SELECT id, agent_id, title, status
              FROM hitl_requests
             WHERE conversation_id = $1
               AND status = 'PENDING'
            """,
            conversation_id,
        )
        for r in hitl_rows:
            blockers.append({
                "type": "hitl",
                "id": str(r["id"]),
                "agent_id": r["agent_id"],
                "title": r["title"],
                "status": r["status"],
            })

        return blockers

    async def update_group_chat_status(
        self, squad_name: str, status: str
    ) -> None:
        """Update the group_chat row to reflect the current session status."""
        await self._pool.execute(
            """
            UPDATE group_chat
               SET status = $2
             WHERE squad_name = $1
            """,
            squad_name,
            status,
        )

    async def get_expired_hitl_requests(self) -> List[Dict[str, Any]]:
        """Return HITL requests that have passed their expiry deadline."""
        rows = await self._pool.fetch(
            """
            SELECT id, conversation_id, agent_id, title, status, expires_at
              FROM hitl_requests
             WHERE status = 'PENDING'
               AND expires_at IS NOT NULL
               AND expires_at < NOW()
            """,
        )
        return [dict(r) for r in rows]
