"""Execute explicitly identified guarded mutations and retrieve their history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from mcp_score.bridge.receipts import (
    GuardedPitchMutation,
    MutationReceipt,
    OperationId,
    PluginSession,
    ReceiptExecution,
    ReceiptIdentity,
)
from mcp_score.context import ScoreContext
from mcp_score.tools import ToolError, require_bridge, score_tool

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

__all__ = ["register"]


@score_tool
async def execute_mutation_once(
    context: ScoreContext,
    operation_id: OperationId,
    expected_plugin_session: PluginSession,
    mutation: GuardedPitchMutation,
) -> MutationReceipt:
    """Execute one guarded pitch request with a caller-retained operation ID.

    Create and retain a unique operation_id BEFORE the first call. Obtain the
    plugin session from connect_to_musescore/get_musescore_bridge_info. Repeating
    the identical ID, session and complete payload returns retained history;
    changing the payload conflicts. This command is never automatically replayed.
    After a lost response or cancellation, explicitly retry the exact request or
    read its receipt. Never replace the session token automatically after restart.
    Only set_note_pitch is supported; legacy mutation tools are not protected.

    Receipt completion means an original native reply was retained, including
    errors; it does not mean the edit succeeded. Replies are HISTORICAL, including
    after undo or further edits, and are not verification of current score state.
    Pending/unknown IDs remain reserved. The bounded non-evicting ledger lasts
    only for that plugin instance. Native score identity is not established.
    """
    try:
        request = ReceiptExecution(
            operation_id=operation_id,
            expected_plugin_session=expected_plugin_session,
            mutation=mutation,
        )
    except ValidationError as error:
        raise ToolError(f"Invalid receipted mutation: {error}") from error
    return await require_bridge(context).execute_mutation_once(request)


@score_tool
async def read_mutation_receipt(
    context: ScoreContext,
    operation_id: OperationId,
    expected_plugin_session: PluginSession,
) -> MutationReceipt:
    """Read retained operation history from the SAME plugin instance.

    Not-found means no reservation exists at observation time; an earlier queued
    request may still arrive. It does not authorize a new ID or prove no effect.
    A restarted plugin rejects the old session rather than reporting not-found.
    Stored replies are historical, not the current score. Lookup never executes
    a mutation and remains available when the non-evicting ledger is full.
    """
    try:
        request = ReceiptIdentity(
            operation_id=operation_id, expected_plugin_session=expected_plugin_session
        )
    except ValidationError as error:
        raise ToolError(f"Invalid receipt identity: {error}") from error
    return await require_bridge(context).read_mutation_receipt(request)


def register(server: MCPServer) -> None:
    server.tool()(execute_mutation_once)
    server.tool()(read_mutation_receipt)
