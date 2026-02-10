"""
Tests for squad_lead.brain — LangGraph state machine nodes and routing.

Tests the pure logic nodes (monitor, executor, updater, router) and the
_detect_task_failure helper. The planner node requires LLM invocation
and is tested separately with a mocked LLM.
"""

import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage


def _make_state(
    messages=None,
    plan=None,
    assignments=None,
    available_squad_agents=None,
    squad_status="IDLE",
    next_actions=None,
    completed_results=None,
    current_stage_index=0,
):
    """Build a SquadState dict with sensible defaults."""
    return {
        "messages": messages or [],
        "plan": plan or [],
        "assignments": assignments or {},
        "available_squad_agents": available_squad_agents or [
            "accounting-agent: currency and math tasks",
            "generalist-agent: general knowledge tasks",
        ],
        "squad_status": squad_status,
        "next_actions": next_actions or [],
        "completed_results": completed_results or {},
        "current_stage_index": current_stage_index,
    }


class TestNodeMonitor:
    """Test the monitor node's status routing logic."""

    def test_planning_on_mission(self):
        from brain import node_monitor

        msg = HumanMessage(content="MISSION: Convert 7 USD to MYR and summarize")
        state = _make_state(messages=[msg])

        result = node_monitor(state)
        assert result.get("squad_status") == "PLANNING"

    def test_planning_on_please(self):
        from brain import node_monitor

        msg = HumanMessage(content="Please convert this currency for me")
        state = _make_state(messages=[msg])

        result = node_monitor(state)
        assert result.get("squad_status") == "PLANNING"

    def test_updating_on_difficulty(self):
        from brain import node_monitor

        msg = HumanMessage(content="I'm having difficulty with a task", name="worker-agent")
        state = _make_state(messages=[msg])

        result = node_monitor(state)
        assert result.get("squad_status") == "UPDATING"

    def test_updating_on_need_help(self):
        from brain import node_monitor

        msg = HumanMessage(content="I need help with this conversion", name="worker-agent")
        state = _make_state(messages=[msg])

        result = node_monitor(state)
        assert result.get("squad_status") == "UPDATING"

    def test_updating_status_passthrough(self):
        from brain import node_monitor

        msg = HumanMessage(content="Some update message")
        state = _make_state(messages=[msg], squad_status="UPDATING")

        result = node_monitor(state)
        assert result.get("squad_status") == "UPDATING"

    def test_executing_not_overridden_to_idle(self):
        from brain import node_monitor

        msg = HumanMessage(content="Some message without keywords")
        state = _make_state(messages=[msg], squad_status="EXECUTING")

        result = node_monitor(state)
        # Should NOT change to IDLE while EXECUTING
        assert result.get("squad_status") is None or result.get("squad_status") != "IDLE"

    def test_idle_on_unrecognized_message(self):
        from brain import node_monitor

        msg = HumanMessage(content="Random chitchat")
        state = _make_state(messages=[msg], squad_status="DONE")

        result = node_monitor(state)
        assert result.get("squad_status") == "IDLE"


class TestNodeExecutor:
    """Test the executor node's task dispatch and context injection."""

    def test_empty_plan_sets_done(self):
        from brain import node_executor

        state = _make_state(plan=[])
        result = node_executor(state)

        assert result["squad_status"] == "DONE"
        assert result["next_actions"] == []

    def test_dispatches_tasks_from_first_stage(self):
        from brain import node_executor

        state = _make_state(
            plan=[
                ["accounting-agent: Convert 7 USD to MYR"],
                ["generalist-agent: Summarize the result"],
            ]
        )

        result = node_executor(state)

        assert len(result["next_actions"]) == 1
        assert result["next_actions"][0]["target"] == "accounting-agent"
        assert "Convert 7 USD to MYR" in result["next_actions"][0]["instruction"]

    def test_skips_already_assigned_tasks(self):
        from brain import node_executor

        task = "accounting-agent: Convert 7 USD to MYR"
        state = _make_state(
            plan=[[task]],
            assignments={"accounting-agent": [task]},
        )

        result = node_executor(state)

        # Task already assigned — should not create new action
        assert len(result["next_actions"]) == 0

    def test_injects_context_from_completed_results(self):
        from brain import node_executor

        state = _make_state(
            plan=[["generalist-agent: Summarize the conversion result"]],
            completed_results={
                "accounting-agent": "7 USD = 27.67 MYR at rate 3.952"
            },
        )

        result = node_executor(state)

        assert len(result["next_actions"]) == 1
        instruction = result["next_actions"][0]["instruction"]
        # Should contain context from previous stage
        assert "Previous Results" in instruction
        assert "accounting-agent" in instruction
        assert "27.67 MYR" in instruction

    def test_parallel_tasks_in_same_stage(self):
        from brain import node_executor

        state = _make_state(
            plan=[[
                "accounting-agent: Convert 7 USD to MYR",
                "generalist-agent: What is the capital of Malaysia?",
            ]]
        )

        result = node_executor(state)

        assert len(result["next_actions"]) == 2
        targets = {a["target"] for a in result["next_actions"]}
        assert "accounting-agent" in targets
        assert "generalist-agent" in targets


class TestDetectTaskFailure:
    """Test the failure detection helper."""

    def test_difficulty_detected(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("I am having difficulty with a task") is True

    def test_cannot_help(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("I cannot help with currency conversion") is True

    def test_outside_capabilities(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("This is outside my capabilities.") is True

    def test_unable_to(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("I'm unable to process this request") is True

    def test_beyond_scope(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("That is beyond my scope of expertise") is True

    def test_not_equipped(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("I'm not equipped to handle database queries") is True

    def test_clean_result_not_failure(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("7 USD = 27.67 MYR at the rate of 3.952") is False

    def test_case_insensitive(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("I CANNOT HELP with this") is True

    def test_empty_string(self):
        from brain import _detect_task_failure

        assert _detect_task_failure("") is False


class TestNodeUpdater:
    """Test the updater node's task completion and re-delegation logic."""

    def test_successful_completion_stores_result(self):
        from brain import node_updater

        msg = AIMessage(content="7 USD = 27.67 MYR", name="accounting-agent")
        task = "accounting-agent: Convert 7 USD to MYR"

        state = _make_state(
            messages=[msg],
            plan=[[task]],
            assignments={"accounting-agent": [task]},
        )

        result = node_updater(state)

        assert "accounting-agent" in result["completed_results"]
        assert "27.67 MYR" in result["completed_results"]["accounting-agent"]

    def test_successful_completion_removes_assignment(self):
        from brain import node_updater

        msg = AIMessage(content="Done", name="accounting-agent")
        task = "accounting-agent: Convert 7 USD to MYR"

        state = _make_state(
            messages=[msg],
            plan=[[task]],
            assignments={"accounting-agent": [task]},
        )

        result = node_updater(state)

        assert "accounting-agent" not in result["assignments"]

    def test_stage_advances_when_empty(self):
        from brain import node_updater

        msg = AIMessage(content="Done", name="accounting-agent")
        task = "accounting-agent: Convert 7 USD to MYR"
        next_stage = ["generalist-agent: Summarize"]

        state = _make_state(
            messages=[msg],
            plan=[[task], next_stage],
            assignments={"accounting-agent": [task]},
        )

        result = node_updater(state)

        # First stage should be removed, leaving only second stage
        assert len(result["plan"]) == 1
        assert result["plan"][0] == next_stage

    def test_failure_triggers_redelegation(self):
        from brain import node_updater

        msg = AIMessage(
            content="I cannot help with currency conversion",
            name="accounting-agent",
        )
        task = "accounting-agent: Convert 7 USD to MYR"

        state = _make_state(
            messages=[msg],
            plan=[[task]],
            assignments={"accounting-agent": [task]},
        )

        result = node_updater(state)

        # Should re-delegate to generalist
        assert len(result["plan"]) == 1
        new_stage = result["plan"][0]
        assert any("generalist-agent" in t for t in new_stage)
        assert not any("accounting-agent" in t for t in new_stage)

    def test_failure_removes_from_assignments(self):
        from brain import node_updater

        msg = AIMessage(
            content="I cannot help with this task",
            name="accounting-agent",
        )
        task = "accounting-agent: Convert 7 USD to MYR"

        state = _make_state(
            messages=[msg],
            plan=[[task]],
            assignments={"accounting-agent": [task]},
        )

        result = node_updater(state)

        assert "accounting-agent" not in result["assignments"]

    def test_multiple_tasks_per_agent_removes_only_completed(self):
        from brain import node_updater

        msg = AIMessage(content="First task done", name="accounting-agent")
        task1 = "accounting-agent: Task 1"
        task2 = "accounting-agent: Task 2"

        state = _make_state(
            messages=[msg],
            plan=[[task1, task2]],
            assignments={"accounting-agent": [task1, task2]},
        )

        result = node_updater(state)

        # Should keep second task
        assert "accounting-agent" in result["assignments"]
        assert len(result["assignments"]["accounting-agent"]) == 1


class TestRouter:
    """Test the router function's status-to-node mapping."""

    def test_planning_routes_to_planner(self):
        from brain import router

        state = _make_state(squad_status="PLANNING")
        assert router(state) == "planner"

    def test_updating_routes_to_updater(self):
        from brain import router

        state = _make_state(squad_status="UPDATING")
        assert router(state) == "updater"

    def test_executing_routes_to_executor(self):
        from brain import router

        state = _make_state(squad_status="EXECUTING")
        assert router(state) == "executor"

    def test_idle_routes_to_end(self):
        from brain import router, END

        state = _make_state(squad_status="IDLE")
        assert router(state) == END

    def test_done_routes_to_end(self):
        from brain import router, END

        state = _make_state(squad_status="DONE")
        assert router(state) == END

    def test_blocked_routes_to_end(self):
        from brain import router, END

        state = _make_state(squad_status="BLOCKED")
        assert router(state) == END


class TestStageIndexTracking:
    """Test that current_stage_index is properly tracked across nodes."""

    def test_executor_preserves_stage_index(self):
        from brain import node_executor

        state = _make_state(
            plan=[["accounting-agent: Do something"]],
            current_stage_index=2,
        )
        result = node_executor(state)

        assert result["current_stage_index"] == 2

    def test_executor_preserves_stage_index_on_done(self):
        from brain import node_executor

        state = _make_state(plan=[], current_stage_index=3)
        result = node_executor(state)

        assert result["squad_status"] == "DONE"
        assert result["current_stage_index"] == 3

    def test_updater_increments_stage_index_on_advance(self):
        from brain import node_updater

        msg = AIMessage(content="Done", name="accounting-agent")
        task = "accounting-agent: Convert 7 USD to MYR"
        next_stage = ["generalist-agent: Summarize"]

        state = _make_state(
            messages=[msg],
            plan=[[task], next_stage],
            assignments={"accounting-agent": [task]},
            current_stage_index=0,
        )

        result = node_updater(state)

        assert result["current_stage_index"] == 1
        assert len(result["plan"]) == 1

    def test_updater_preserves_stage_index_when_stage_not_empty(self):
        from brain import node_updater

        msg = AIMessage(content="Done", name="accounting-agent")
        task1 = "accounting-agent: Task 1"
        task2 = "generalist-agent: Task 2"

        state = _make_state(
            messages=[msg],
            plan=[[task1, task2]],
            assignments={"accounting-agent": [task1]},
            current_stage_index=1,
        )

        result = node_updater(state)

        # Stage still has tasks — should NOT increment
        assert result["current_stage_index"] == 1

    def test_updater_preserves_stage_index_on_redelegation(self):
        from brain import node_updater

        msg = AIMessage(
            content="I cannot help with currency conversion",
            name="accounting-agent",
        )
        task = "accounting-agent: Convert 7 USD to MYR"

        state = _make_state(
            messages=[msg],
            plan=[[task]],
            assignments={"accounting-agent": [task]},
            current_stage_index=2,
        )

        result = node_updater(state)

        # Re-delegation should NOT change the stage index
        assert result["current_stage_index"] == 2
