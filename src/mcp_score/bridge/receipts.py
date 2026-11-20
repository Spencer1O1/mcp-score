"""Caller-retained operation identities and historical mutation receipts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictBool, StrictInt, StrictStr, model_validator

# Pydantic evaluates nested input annotations at runtime.
from mcp_score.bridge.chord_tone import AddChordToneRequest, RemoveChordToneRequest  # noqa: TC001
from mcp_score.bridge.lyric_text import LyricTextRequest  # noqa: TC001
from mcp_score.bridge.pitch import PitchEditRequest  # noqa: TC001
from mcp_score.bridge.pitch_batch import PitchBatchRequest  # noqa: TC001
from mcp_score.bridge.results import Result

__all__ = [
    "RECEIPT_PROTOCOL_VERSION",
    "OperationId",
    "PluginSession",
    "GuardedLyricTextMutation",
    "GuardedAddChordToneMutation",
    "GuardedRemoveChordToneMutation",
    "GuardedPitchMutation",
    "GuardedPitchBatchMutation",
    "ReceiptMutation",
    "ReceiptExecution",
    "ReceiptIdentity",
    "ReceiptCapabilities",
    "MutationReceipt",
]

RECEIPT_PROTOCOL_VERSION = 1

OperationId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
PluginSession = Annotated[StrictStr, Field(min_length=1, max_length=128)]


class ReceiptIdentity(Result):
    operation_id: OperationId
    """Caller ID retained before first send; unique within this plugin session."""
    expected_plugin_session: PluginSession
    """Session from connection diagnostics; never replaced automatically on retry."""


class GuardedLyricTextMutation(Result):
    kind: Literal["set_lyric_text"]
    request: LyricTextRequest


class GuardedPitchMutation(Result):
    kind: Literal["set_note_pitch"]
    request: PitchEditRequest


class GuardedPitchBatchMutation(Result):
    kind: Literal["set_note_pitches"]
    request: PitchBatchRequest


class GuardedAddChordToneMutation(Result):
    kind: Literal["add_chord_tone"]
    request: AddChordToneRequest


class GuardedRemoveChordToneMutation(Result):
    kind: Literal["remove_chord_tone"]
    request: RemoveChordToneRequest


ReceiptMutation = Annotated[
    GuardedPitchMutation
    | GuardedPitchBatchMutation
    | GuardedAddChordToneMutation
    | GuardedRemoveChordToneMutation
    | GuardedLyricTextMutation,
    Field(discriminator="kind"),
]


class ReceiptExecution(ReceiptIdentity):
    mutation: ReceiptMutation


class ReceiptCapabilities(Result):
    protocol_version: StrictInt = Field(ge=1)
    plugin_session: PluginSession
    supported_mutations: list[StrictStr]
    capacity: StrictInt = Field(gt=0, le=1024)
    max_payload_units: StrictInt = Field(gt=0)
    max_reply_units: StrictInt = Field(gt=0)
    max_depth: StrictInt = Field(gt=0)
    max_nodes: StrictInt = Field(gt=0)
    retention: Literal["plugin_session_non_evicting"]


class MutationReceipt(Result):
    """Retained operation history, never a verification of the current score."""

    plugin_session: PluginSession
    operation_id: OperationId
    status: Literal["not_found", "pending", "completed", "unknown"]
    command: (
        Literal[
            "setNotePitch",
            "setNotePitches",
            "addChordTone",
            "removeChordTone",
            "setLyricText",
        ]
        | None
    )
    cached: StrictBool
    """Lookup/retrieval response, not proof that an operation completed or applied."""
    historical: Literal[True]
    original_reply: dict[str, JsonValue] | None
    """Original native JSON fields, including errors, preserved in native casing."""
    reason: str | None
    """Bounded cause for an unknown receipt; no automatic retry is implied."""

    @model_validator(mode="after")
    def _consistent_history(self) -> Self:
        if self.status == "not_found":
            if self.command is not None or self.original_reply is not None:
                raise ValueError(
                    "Absent receipt cannot contain an executed command/reply"
                )
        elif self.command not in {
            "setNotePitch",
            "setNotePitches",
            "addChordTone",
            "removeChordTone",
            "setLyricText",
        }:
            raise ValueError("Reserved receipt must identify its command")
        if (self.status == "completed") != (self.original_reply is not None):
            raise ValueError("Only completed receipts contain the original reply")
        if self.status == "unknown" and not self.reason:
            raise ValueError("Unknown receipt requires a cause")
        if self.status != "unknown" and self.reason is not None:
            raise ValueError("Only unknown receipts contain an unavailable-reply cause")
        return self
