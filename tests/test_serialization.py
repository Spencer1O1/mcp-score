"""Concurrent operations keep response streams and legacy edit targets separate."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from mcp_score.bridge import BridgeRegistry
from mcp_score.bridge.musescore import MuseScoreBridge
from mcp_score.bridge.results import CursorPosition, Duration, NoteAdded
from mcp_score.bridge.websocket import TransportError, WebSocketTransport
from mcp_score.tools.connection import connect_to_musescore
from mcp_score.tools.manipulation import add_live_note
from tests.fakes import (
    MUSESCORE_HANDSHAKE,
    WEBSOCKETS_CONNECT,
    FakeBridge,
    fake_connection,
    score_context,
    sent_payloads,
)

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection


class PausedCursorBridge(FakeBridge):
    """A legacy cursor operation paused where another tool used to overtake it."""

    def __init__(self) -> None:
        super().__init__()
        self.measure = 1
        self.navigated = asyncio.Event()
        self.release = asyncio.Event()
        self.applied: list[tuple[int, int]] = []

    async def go_to_measure(self, measure: int) -> CursorPosition:
        self.measure = measure
        if measure == 1:
            self.navigated.set()
            await self.release.wait()
        return CursorPosition(measure=measure, staff=0)

    async def add_note(
        self, pitch: int, duration: Duration, advance_cursor: bool = True
    ) -> NoteAdded:
        self.applied.append((self.measure, pitch))
        return NoteAdded(measure=self.measure, staff=0, pitch=pitch, duration=duration)


@pytest.mark.anyio()
async def test_transport_pairs_concurrent_requests_with_their_own_replies() -> None:
    async with asyncio.timeout(5):
        received: list[int] = []
        first_received = asyncio.Event()
        release = asyncio.Event()

        async def handle(connection: ServerConnection) -> None:
            async for message in connection:
                request = json.loads(message)
                received.append(request["number"])
                if request["number"] == 1:
                    first_received.set()
                    await release.wait()
                await connection.send(json.dumps({"result": request["number"]}))

        async with websockets.serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}", 2)
            await transport.open()
            first = asyncio.create_task(transport.request({"number": 1}))
            await first_received.wait()
            second = asyncio.create_task(transport.request({"number": 2}))
            try:
                await asyncio.sleep(0)
                assert received == [1]
                release.set()
                assert await first == {"result": 1}
                assert await second == {"result": 2}
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await transport.close()
        assert received == [1, 2]


@pytest.mark.anyio()
async def test_legacy_edit_workflows_do_not_overtake_each_others_cursor() -> None:
    async with asyncio.timeout(5):
        registry = BridgeRegistry()
        bridge = PausedCursorBridge()
        registry.active = bridge
        context = score_context(registry)
        first = asyncio.create_task(add_live_note(context, 1, 60))
        await bridge.navigated.wait()
        second = asyncio.create_task(
            add_live_note(context=context, measure=2, pitch=64)
        )
        try:
            await asyncio.sleep(0)
            assert bridge.measure == 1
            bridge.release.set()
            results = await asyncio.gather(first, second)
        finally:
            bridge.release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        assert bridge.applied == [(1, 60), (2, 64)]
        assert [result.measure for result in results] == [1, 2]


@pytest.mark.anyio()
async def test_registry_switch_waits_for_whole_tool_workflow() -> None:
    async with asyncio.timeout(5):
        registry = BridgeRegistry()
        bridge = PausedCursorBridge()
        replacement = FakeBridge("Replacement", is_connected=False)
        registry.active = bridge
        edit = asyncio.create_task(add_live_note(score_context(registry), 1, 60))
        await bridge.navigated.wait()
        switch = asyncio.create_task(registry.activate(replacement))
        try:
            await asyncio.sleep(0)
            assert registry.connected() is bridge
            assert replacement.calls == []
            bridge.release.set()
            await edit
            assert await switch
        finally:
            bridge.release.set()
            await asyncio.gather(edit, switch, return_exceptions=True)
        assert bridge.applied == [(1, 60)]
        assert registry.connected() is replacement
        assert not bridge.is_connected


@pytest.mark.anyio()
async def test_child_task_and_cancelled_waiter_cannot_bypass_registry_owner() -> None:
    async with asyncio.timeout(5):
        registry = BridgeRegistry()
        bridge = FakeBridge()
        context = score_context(registry)
        async with registry.operation():
            # Same-task activation may nest, but a newly created task must wait.
            assert await registry.activate(bridge)
            waiter = asyncio.create_task(add_live_note(context, 2, 64))
            await asyncio.sleep(0)
            assert bridge.calls_to("go_to_measure") == []
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        await add_live_note(context, 3, 65)
        assert [call.arguments for call in bridge.calls_to("go_to_measure")] == [(3,)]


@pytest.mark.anyio()
async def test_direct_disconnect_waits_for_active_bridge_exchange() -> None:
    async with asyncio.timeout(5):
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        receiving = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def receive() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(MUSESCORE_HANDSHAKE)
            receiving.set()
            await release.wait()
            return json.dumps({"result": "pong"})

        connection.recv.side_effect = receive
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            await bridge.connect()
        request = asyncio.create_task(bridge.ping())
        await receiving.wait()
        disconnect = asyncio.create_task(bridge.disconnect())
        try:
            await asyncio.sleep(0)
            connection.close.assert_not_awaited()
            release.set()
            assert await request
            await disconnect
        finally:
            release.set()
            await asyncio.gather(request, disconnect, return_exceptions=True)
        connection.close.assert_awaited_once()
        assert not bridge.is_connected


@pytest.mark.anyio()
async def test_cancelled_mutation_discards_stream_before_next_request() -> None:
    async with asyncio.timeout(5):
        receiving = asyncio.Event()
        closing = asyncio.Event()
        close_release = asyncio.Event()
        dropped = fake_connection(MUSESCORE_HANDSHAKE)
        replacement = fake_connection(MUSESCORE_HANDSHAKE, {"result": "pong"})
        receive_count = 0

        async def receive() -> str:
            nonlocal receive_count
            receive_count += 1
            if receive_count == 1:
                return json.dumps(MUSESCORE_HANDSHAKE)
            receiving.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled receive unexpectedly returned")

        async def close() -> None:
            closing.set()
            await close_release.wait()

        dropped.recv.side_effect = receive
        dropped.close.side_effect = close
        bridge = MuseScoreBridge()
        connect = AsyncMock(side_effect=[dropped, replacement])
        with patch(WEBSOCKETS_CONNECT, connect):
            mutation = asyncio.create_task(bridge.append_measures(1))
            await receiving.wait()
            following = asyncio.create_task(bridge.ping())
            mutation.cancel()
            try:
                await closing.wait()
                await asyncio.sleep(0)
                assert connect.await_count == 1
                assert sent_payloads(replacement) == []
                close_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await mutation
                assert await following
            finally:
                close_release.set()
                await asyncio.gather(mutation, following, return_exceptions=True)
                await bridge.disconnect()
        assert [item["command"] for item in sent_payloads(dropped)] == [
            "getCapabilities",
            "appendMeasures",
        ]
        assert [item["command"] for item in sent_payloads(replacement)] == [
            "getCapabilities",
            "ping",
        ]


@pytest.mark.anyio()
async def test_cancelled_handshake_cleans_socket_and_allows_next_connection() -> None:
    async with asyncio.timeout(5):
        receiving = asyncio.Event()
        first = fake_connection()
        replacement = fake_connection(MUSESCORE_HANDSHAKE)

        async def receive() -> str:
            receiving.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled handshake unexpectedly returned")

        first.recv.side_effect = receive
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, AsyncMock(side_effect=[first, replacement])):
            attempt = asyncio.create_task(bridge.connect())
            await receiving.wait()
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
            first.close.assert_awaited_once()
            assert not bridge.is_connected
            assert await bridge.connect()
            await bridge.disconnect()


@pytest.mark.anyio()
async def test_concurrent_lazy_calls_share_one_connection_and_handshake() -> None:
    async with asyncio.timeout(5):
        opening = asyncio.Event()
        release = asyncio.Event()
        connection = fake_connection(
            MUSESCORE_HANDSHAKE, {"result": "first"}, {"result": "second"}
        )

        async def open_connection(uri: str) -> AsyncMock:
            opening.set()
            await release.wait()
            return connection

        connect = AsyncMock(side_effect=open_connection)
        bridge = MuseScoreBridge()
        with patch(WEBSOCKETS_CONNECT, connect):
            first = asyncio.create_task(bridge.send_command("ping", {"number": 1}))
            await opening.wait()
            second = asyncio.create_task(bridge.send_command("ping", {"number": 2}))
            try:
                await asyncio.sleep(0)
                assert connect.await_count == 1
                release.set()
                assert await first == {"result": "first"}
                assert await second == {"result": "second"}
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await bridge.disconnect()
        assert [item["command"] for item in sent_payloads(connection)] == [
            "getCapabilities",
            "ping",
            "ping",
        ]


@pytest.mark.anyio()
async def test_cancelled_disconnect_closes_socket_and_clears_active_registry() -> None:
    async with asyncio.timeout(5):
        disconnecting = asyncio.Event()
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        bridge = MuseScoreBridge()
        registry = BridgeRegistry(musescore=bridge)
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            assert await registry.activate(bridge)

        async def disconnect_hook(transport: WebSocketTransport) -> None:
            disconnecting.set()
            await asyncio.Event().wait()

        with patch.object(bridge, "_on_disconnecting", disconnect_hook):
            attempt = asyncio.create_task(registry.deactivate(bridge))
            await disconnecting.wait()
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        connection.close.assert_awaited_once()
        assert registry.active is None
        assert not bridge.is_connected
        replacement = FakeBridge()
        assert await registry.activate(replacement)


@pytest.mark.anyio()
async def test_direct_lifecycle_change_waits_between_steps_of_mcp_workflow() -> None:
    async with asyncio.timeout(5):
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        bridge = MuseScoreBridge()
        registry = BridgeRegistry(musescore=bridge)
        with patch(WEBSOCKETS_CONNECT, AsyncMock(return_value=connection)):
            assert await registry.activate(bridge)
        navigated = asyncio.Event()
        release = asyncio.Event()

        async def navigate(measure: int) -> CursorPosition:
            navigated.set()
            await release.wait()
            return CursorPosition(measure=measure, staff=0)

        note = NoteAdded(
            measure=1, staff=0, pitch=60, duration=Duration(numerator=1, denominator=4)
        )
        with (
            patch.object(bridge, "go_to_measure", navigate),
            patch.object(bridge, "go_to_staff", AsyncMock()),
            patch.object(bridge, "add_note", AsyncMock(return_value=note)),
        ):
            edit = asyncio.create_task(add_live_note(score_context(registry), 1, 60))
            await navigated.wait()
            disconnect = asyncio.create_task(bridge.disconnect())
            try:
                await asyncio.sleep(0)
                connection.close.assert_not_awaited()
                release.set()
                assert await edit == note
                await disconnect
            finally:
                release.set()
                await asyncio.gather(edit, disconnect, return_exceptions=True)
        connection.close.assert_awaited_once()
        assert registry.connected() is None


@pytest.mark.anyio()
@pytest.mark.parametrize("other_active", [False, True])
async def test_connect_tool_waits_before_reconfiguring_an_owned_inactive_bridge(
    other_active: bool,
) -> None:
    async with asyncio.timeout(5):
        bridge = MuseScoreBridge(host="original-host", port=8765)
        registry = BridgeRegistry(musescore=bridge)
        if other_active:
            registry.active = FakeBridge("Other application")
        connection = fake_connection(MUSESCORE_HANDSHAKE)
        connect = AsyncMock(return_value=connection)
        with patch(WEBSOCKETS_CONNECT, connect):
            try:
                async with bridge.operation():
                    switch = asyncio.create_task(
                        connect_to_musescore(
                            score_context(registry), host="replacement-host", port=9000
                        )
                    )
                    await asyncio.sleep(0)
                    assert not switch.done()
                    assert bridge.host == "original-host"
                    assert bridge.port == 8765
                    connect.assert_not_awaited()
                result = await switch
                assert result.uri == "ws://replacement-host:9000"
                assert registry.connected() is bridge
                connect.assert_awaited_once_with("ws://replacement-host:9000")
            finally:
                await asyncio.gather(switch, return_exceptions=True)
                await bridge.disconnect()


@pytest.mark.anyio()
async def test_timed_out_transport_discards_late_reply_before_queued_request() -> None:
    async with asyncio.timeout(5):
        received: list[int] = []
        first_received = asyncio.Event()
        release = asyncio.Event()

        async def handle(connection: ServerConnection) -> None:
            try:
                async for message in connection:
                    request = json.loads(message)
                    received.append(request["number"])
                    if request["number"] == 1:
                        first_received.set()
                        await release.wait()
                    await connection.send(json.dumps({"result": request["number"]}))
            except ConnectionClosed:
                pass  # The first caller timed out before this late reply was sent.

        async with websockets.serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}", 0.1)
            await transport.open()
            first = asyncio.create_task(transport.request({"number": 1}))
            await first_received.wait()
            second = asyncio.create_task(transport.request({"number": 2}))
            try:
                with pytest.raises(TransportError) as first_failure:
                    await first
                assert first_failure.value.may_have_been_sent is True
                with pytest.raises(TransportError) as queued_failure:
                    await second
                assert queued_failure.value.may_have_been_sent is False
                assert not transport.is_open
                assert received == [1]
                release.set()
                transport.receive_timeout = 1
                await transport.open()
                assert await transport.request({"number": 2}) == {"result": 2}
                assert received == [1, 2]
            finally:
                release.set()
                await asyncio.gather(first, second, return_exceptions=True)
                await transport.close()
