"""WebSocket plumbing shared by every bridge.

``WebSocketTransport`` owns one connection and exchanges JSON messages over
it. ``WebSocketBridge`` builds the bridge behaviour on top: lazy connect,
one reconnect attempt for allowlisted read-only requests, and hooks for a
protocol handshake. Mutations and unknown requests are never replayed after
an uncertain exchange.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import websockets
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from mcp_score.bridge.base import BridgeError, CommandResult, ScoreBridge

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_RECEIVE_TIMEOUT_SECONDS",
    "TransportError",
    "WebSocketBridge",
    "WebSocketTransport",
]

logger = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_RECEIVE_TIMEOUT_SECONDS = 30.0

# Everything the websockets library and the OS raise when a connection
# cannot be made or breaks mid-exchange.
_CONNECTION_FAILURES: tuple[type[Exception], ...] = (
    OSError,
    WebSocketException,
    TimeoutError,
)


class TransportError(Exception):
    """A transport failure, with whether a request could have reached the peer."""

    def __init__(self, message: str, *, may_have_been_sent: bool = False) -> None:
        super().__init__(message)
        self.may_have_been_sent = may_have_been_sent


class WebSocketTransport:
    """One WebSocket connection carrying JSON request/response messages."""

    def __init__(self, uri: str, receive_timeout: float) -> None:
        self.uri = uri
        self.receive_timeout = receive_timeout
        self._connection: ClientConnection | None = None

    @property
    def is_open(self) -> bool:
        connection = self._connection
        return connection is not None and connection.state is State.OPEN

    async def open(self) -> None:
        """Open the connection.

        Raises:
            TransportError: If the server cannot be reached.
        """
        try:
            self._connection = await websockets.connect(self.uri)
        except _CONNECTION_FAILURES as exception:
            raise TransportError(f"cannot connect to {self.uri}: {exception}") from None

    async def close(self) -> None:
        """Close the connection; safe to call when already closed."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            with contextlib.suppress(*_CONNECTION_FAILURES):
                await connection.close()

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON message without waiting for a reply.

        Raises:
            TransportError: If the connection is not open or sending fails.
        """
        connection = self._require_open()
        try:
            await connection.send(json.dumps(payload))
        except _CONNECTION_FAILURES as exception:
            raise TransportError(f"send failed: {exception}") from None

    async def request(self, payload: dict[str, Any]) -> CommandResult:
        """Send one JSON message and return the decoded JSON reply.

        Raises:
            TransportError: If the connection is not open, breaks, times
                out, or the reply is not a JSON text message.
        """
        connection = self._require_open()
        message = json.dumps(payload)
        logger.debug("-> %s: %s", self.uri, message)
        try:
            await connection.send(message)
            reply = await asyncio.wait_for(
                connection.recv(), timeout=self.receive_timeout
            )
        except _CONNECTION_FAILURES as exception:
            raise TransportError(
                f"exchange failed: {exception}", may_have_been_sent=True
            ) from None
        logger.debug("<- %s: %s", self.uri, reply)
        if not isinstance(reply, str):
            raise TransportError(
                "received a binary message, expected JSON text", may_have_been_sent=True
            )
        try:
            decoded: object = json.loads(reply)
        except json.JSONDecodeError as exception:
            raise TransportError(
                f"received invalid JSON: {exception}", may_have_been_sent=True
            ) from None
        if not isinstance(decoded, dict):
            raise TransportError(
                "received JSON that is not an object", may_have_been_sent=True
            )
        return cast("CommandResult", decoded)

    def _require_open(self) -> ClientConnection:
        connection = self._connection
        if connection is None or connection.state is not State.OPEN:
            raise TransportError("the connection is not open")
        return connection


class WebSocketBridge(ScoreBridge):
    """A bridge to an application that serves a WebSocket on a host and port.

    Subclasses implement how commands are framed (:meth:`send_command`) and
    may override :meth:`_on_connected` for a handshake and
    :meth:`_on_disconnecting` for a goodbye message.
    """

    def __init__(
        self,
        application_name: str,
        host: str,
        port: int,
        receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
    ) -> None:
        self._application_name = application_name
        self.host = host
        self.port = port
        self.receive_timeout = receive_timeout
        self._transport: WebSocketTransport | None = None
        self.connection_error: str | None = None
        """Why the last connection attempt failed, or None after success."""

    @property
    def application_name(self) -> str:
        return self._application_name

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.is_open

    async def connect(self) -> bool:
        if self._transport is not None:
            await self.disconnect()
        self.connection_error = None
        transport = WebSocketTransport(self.uri, self.receive_timeout)
        try:
            await transport.open()
            self._transport = transport
            await self._on_connected(transport)
        except TransportError as exception:
            self.connection_error = str(exception)
            logger.error("%s: %s", self.application_name, exception)
            await transport.close()
            self._transport = None
            return False
        logger.info("Connected to %s at %s", self.application_name, self.uri)
        return True

    async def disconnect(self) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        if transport.is_open:
            with contextlib.suppress(TransportError):
                await self._on_disconnecting(transport)
        await transport.close()
        logger.info("Disconnected from %s", self.application_name)

    async def _on_connected(self, transport: WebSocketTransport) -> None:
        """Hook run right after the connection opens (a handshake, typically).

        Raise :class:`TransportError` to fail the connection.
        """

    async def _on_disconnecting(self, transport: WebSocketTransport) -> None:
        """Hook run right before the connection closes."""

    def _validate_payload(self, payload: dict[str, Any]) -> None:
        """Reject unsupported requests before sending, including after reconnect."""

    def _is_replay_safe(self, payload: dict[str, Any]) -> bool:
        """Whether repeating this exact payload is known not to change the score.

        Protocol implementations opt in only documented read-only operations.
        Unknown commands and batches remain unsafe even if a peer accepts them.
        """
        return False

    def _failed_request_error(
        self, payload: dict[str, Any], exception: TransportError
    ) -> BridgeError:
        """Describe an unsafe request without claiming its effects were reverted."""
        unknown = exception.may_have_been_sent
        operation = str(
            payload.get("command")
            or payload.get("commandName")
            or payload.get("message")
            or "unknown"
        )
        request_id = str(uuid4())
        message = (
            f"{self.application_name} {operation} outcome is unknown: "
            "the request may have been applied, but no valid reply was received. "
            "It was not replayed. Inspect the score before retrying or undoing."
            if unknown
            else f"{self.application_name} {operation} was not sent; "
            "the connection was already closed. It was not replayed."
        )
        return BridgeError(
            message,
            code="outcome_unknown" if unknown else "request_not_sent",
            mutation_status="unknown" if unknown else "not_applied",
            operation=operation,
            request_id=request_id,
            request_id_scope="client_correlation_only",
            target=payload.get("params", payload.get("parameters")),
            replayed=False,
            cause=str(exception),
        )

    async def _exchange(self, payload: dict[str, Any]) -> CommandResult:
        """Send once, retrying a lost exchange only for allowlisted safe reads.

        Mutations and unknown commands are never automatically replayed. A
        failure after attempting to send one leaves its application outcome
        unknown; the caller must inspect the score before deciding what to do.
        """
        if not self.is_connected and not await self.connect():
            raise BridgeError(
                f"Cannot connect to {self.application_name} at {self.uri}. "
                f"{self.connection_error}"
            )
        transport = self._transport
        assert transport is not None  # noqa: S101 - established by the line above
        self._validate_payload(payload)
        try:
            return await transport.request(payload)
        except TransportError as exception:
            if not self._is_replay_safe(payload):
                await self.disconnect()
                raise self._failed_request_error(payload, exception) from exception
            logger.warning(
                "%s: %s; reconnecting once for a safe read",
                self.application_name,
                exception,
            )
        await self.disconnect()
        if not await self.connect():
            raise BridgeError(
                f"Lost connection to {self.application_name} and could not reconnect. "
                f"{self.connection_error}"
            )
        transport = self._transport
        assert transport is not None  # noqa: S101 - established by connect()
        self._validate_payload(payload)
        try:
            return await transport.request(payload)
        except TransportError as exception:
            await self.disconnect()
            raise BridgeError(
                f"{self.application_name} request failed: {exception}"
            ) from None
