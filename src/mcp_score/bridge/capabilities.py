"""The MuseScore plugin protocol handshake and its public diagnostics."""

from __future__ import annotations

from pydantic import Field, StrictInt, StrictStr

from mcp_score.bridge.receipts import ReceiptCapabilities  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = ["PROTOCOL_VERSION", "PluginCapabilities", "MuseScoreDiagnostics"]

PROTOCOL_VERSION = 1
"""Wire protocol revision; incompatible changes require a new revision."""


class PluginCapabilities(Result):
    """Commands supported by the connected plugin, not promises about notation."""

    protocol_version: StrictInt = Field(ge=1)
    plugin_version: StrictStr = Field(min_length=1)
    commands: list[StrictStr]
    """Supported top-level protocol commands."""
    sequence_commands: list[StrictStr]
    """Commands accepted as steps of processSequence."""
    receipts: ReceiptCapabilities | None = None
    """Optional receipt protocol; null plugins cannot execute receipted mutations."""


class MuseScoreDiagnostics(PluginCapabilities):
    """Negotiated plugin capabilities and the Python server's package version."""

    server_version: str
