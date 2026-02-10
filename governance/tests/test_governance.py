"""
Tests for governance router — message validation, routing, and global events archival.

Uses direct function testing with mocked broker/context rather than FastStream TestClient
to keep the test suite lightweight and dependency-free.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from main import governance_router


def _make_kafka_headers(
    source_agent="test-agent",
    dest_topic="session.test",
    interaction_type="TASK_RESULT",
    conversation_id="session.test",
):
    """Build Kafka headers dict (bytes values, like real aiokafka)."""
    return {
        "x-source-agent": source_agent.encode("utf-8"),
        "x-dest-topic": dest_topic.encode("utf-8"),
        "x-interaction-type": interaction_type.encode("utf-8"),
        "x-conversation-id": conversation_id.encode("utf-8"),
    }


def _make_message(content="Hello", msg_id="msg-1", msg_type="MESSAGE", headers=None):
    """Build a message payload dict."""
    return {
        "id": msg_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "content": content,
        "type": msg_type,
        "headers": headers or {
            "source_agent_id": "test-agent",
            "interaction_type": "TASK_RESULT",
            "conversation_id": "session.test",
            "governance_status": "PENDING",
        },
    }


@pytest.fixture
def mock_broker():
    b = AsyncMock()
    b.publish = AsyncMock()
    return b


@pytest.fixture
def mock_logger():
    return MagicMock()


@pytest.fixture
def mock_kafka_message():
    """Creates a mock Kafka message with configurable headers."""
    def _factory(headers=None):
        msg = MagicMock()
        msg.headers = headers or _make_kafka_headers()
        return msg
    return _factory


class TestMessageValidation:
    @pytest.mark.asyncio
    async def test_valid_message_passes(self, mock_broker, mock_logger, mock_kafka_message):
        """Normal messages should be routed."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="7 USD = 27.67 MYR")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)
            assert mock_broker.publish.call_count >= 2

    @pytest.mark.asyncio
    async def test_blocked_message_not_routed(self, mock_broker, mock_logger, mock_kafka_message):
        """Messages containing DROP_ME should be blocked."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="This should DROP_ME immediately")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)
            mock_broker.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_dest_topic_returns_early(self, mock_broker, mock_logger):
        """Missing x-dest-topic header should log error and return."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Hello")
            kafka_msg = MagicMock()
            kafka_msg.headers = {"x-source-agent": b"test-agent"}
            await governance_router(msg, logger=mock_logger, message=kafka_msg)
            mock_broker.publish.assert_not_called()


class TestRouting:
    @pytest.mark.asyncio
    async def test_routes_to_single_destination(self, mock_broker, mock_logger, mock_kafka_message):
        """Message should be published to the destination topic."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Result data")
            kafka_msg = mock_kafka_message(
                headers=_make_kafka_headers(dest_topic="session.squad-1")
            )
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            dest_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "session.squad-1"
            ]
            assert len(dest_calls) == 1

    @pytest.mark.asyncio
    async def test_fan_out_to_multiple_destinations(self, mock_broker, mock_logger, mock_kafka_message):
        """Comma-separated x-dest-topic should fan out to all destinations."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Broadcast message")
            kafka_msg = mock_kafka_message(
                headers=_make_kafka_headers(
                    dest_topic="session.squad-1,mesh.agent.worker-1.inbox,mesh.agent.worker-2.inbox"
                )
            )
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            topics_published = [c.kwargs.get("topic") for c in mock_broker.publish.call_args_list]
            assert "session.squad-1" in topics_published
            assert "mesh.agent.worker-1.inbox" in topics_published
            assert "mesh.agent.worker-2.inbox" in topics_published
            assert "mesh.global.events" in topics_published


class TestGovernanceHeaders:
    @pytest.mark.asyncio
    async def test_injects_verified_status(self, mock_broker, mock_logger, mock_kafka_message):
        """Outbound headers should include x-governance-status: VERIFIED."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Test")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            dest_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "session.test"
            ]
            assert len(dest_calls) == 1
            headers = dest_calls[0].kwargs.get("headers", {})
            assert headers.get("x-governance-status") == "VERIFIED"

    @pytest.mark.asyncio
    async def test_injects_verified_at_timestamp(self, mock_broker, mock_logger, mock_kafka_message):
        """Outbound headers should include x-verified-at timestamp."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Test")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            dest_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "session.test"
            ]
            headers = dest_calls[0].kwargs.get("headers", {})
            assert "x-verified-at" in headers

    @pytest.mark.asyncio
    async def test_stamps_payload_governance_status(self, mock_broker, mock_logger, mock_kafka_message):
        """Message payload headers should also be stamped with VERIFIED."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Test")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)
            assert msg["headers"]["governance_status"] == "VERIFIED"

    @pytest.mark.asyncio
    async def test_header_normalization_bytes_values(self, mock_broker, mock_logger):
        """Byte-encoded Kafka header values should be decoded to strings in outbound headers."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Test")
            kafka_msg = MagicMock()
            # aiokafka provides string keys with bytes values
            kafka_msg.headers = {
                "x-source-agent": b"test-agent",
                "x-dest-topic": b"session.test",
                "x-interaction-type": b"TASK_RESULT",
            }
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            dest_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "session.test"
            ]
            assert len(dest_calls) == 1
            # Outbound headers should be strings, not bytes
            headers = dest_calls[0].kwargs.get("headers", {})
            assert isinstance(headers.get("x-source-agent"), str)


class TestGlobalEventsArchival:
    @pytest.mark.asyncio
    async def test_archives_to_global_events(self, mock_broker, mock_logger, mock_kafka_message):
        """Every valid message should be archived to mesh.global.events."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Important result")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            global_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "mesh.global.events"
            ]
            assert len(global_calls) == 1

    @pytest.mark.asyncio
    async def test_global_event_structure(self, mock_broker, mock_logger, mock_kafka_message):
        """Archived event should contain GlobalEventRecord fields."""
        with patch("main.broker", mock_broker):
            msg = _make_message(
                content="Result", msg_id="test-msg-1", msg_type="MESSAGE"
            )
            kafka_msg = mock_kafka_message(
                headers=_make_kafka_headers(
                    source_agent="accounting-agent",
                    interaction_type="TASK_RESULT",
                    conversation_id="session.squad-1",
                )
            )
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            global_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "mesh.global.events"
            ]
            event_data = global_calls[0].args[0]

            assert event_data["event_id"] == "test-msg-1"
            assert event_data["event_type"] == "TASK_RESULT"
            assert event_data["source_agent_id"] == "accounting-agent"
            assert event_data["session_id"] == "session.squad-1"
            assert "payload" in event_data
            assert event_data["metadata"]["governance_status"] == "VERIFIED"
            assert "session.test" in event_data["metadata"]["destinations"]

    @pytest.mark.asyncio
    async def test_global_event_headers(self, mock_broker, mock_logger, mock_kafka_message):
        """Global event publish should include event-type and session-id headers."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Test")
            kafka_msg = mock_kafka_message(
                headers=_make_kafka_headers(
                    interaction_type="TASK_ASSIGNMENT",
                    conversation_id="session.squad-2",
                )
            )
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            global_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "mesh.global.events"
            ]
            headers = global_calls[0].kwargs.get("headers", {})
            assert headers.get("event-type") == "TASK_ASSIGNMENT"
            assert headers.get("session-id") == "session.squad-2"

    @pytest.mark.asyncio
    async def test_system_command_event_type(self, mock_broker, mock_logger, mock_kafka_message):
        """SYSTEM_COMMAND messages should archive with correct event_type."""
        with patch("main.broker", mock_broker):
            msg = _make_message(content="Join", msg_type="SYSTEM_COMMAND")
            kafka_msg = mock_kafka_message()
            await governance_router(msg, logger=mock_logger, message=kafka_msg)

            global_calls = [
                c for c in mock_broker.publish.call_args_list
                if c.kwargs.get("topic") == "mesh.global.events"
            ]
            event_data = global_calls[0].args[0]
            assert event_data["event_type"] == "SYSTEM_COMMAND"
