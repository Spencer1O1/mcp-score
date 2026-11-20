"""The interface every score application bridge implements.

A bridge talks to one running notation application. The MCP tools only
depend on this interface, so an application is supported by adding a
bridge, not by touching the tools. Every operation returns one of the
result models in :mod:`mcp_score.bridge.results`; an operation the
application cannot perform raises :class:`BridgeError` with the
application's explanation, which the tools report as a tool error.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from mcp_score.bridge.current_selection import CurrentSelection
    from mcp_score.bridge.duration_edit import DurationEdit, ExpectedDurationEvent
    from mcp_score.bridge.passage import Passage
    from mcp_score.bridge.pitch import ExpectedChord, PitchEdit
    from mcp_score.bridge.receipts import (
        MutationReceipt,
        ReceiptExecution,
        ReceiptIdentity,
    )
    from mcp_score.bridge.results import (
        BarlineSet,
        ChordSymbolAdded,
        CursorInfo,
        CursorPosition,
        Duration,
        DynamicAdded,
        KeySignatureSet,
        MeasuresAppended,
        NoteAdded,
        RehearsalMarkAdded,
        ScoreInfo,
        SelectedRange,
        SelectionProperties,
        TempoSet,
        TimeSignatureSet,
        Transposed,
    )
    from mcp_score.bridge.staff_context import StaffContext

__all__ = ["BridgeError", "CommandResult", "ScoreBridge"]

type CommandResult = dict[str, Any]
"""An application's decoded JSON reply, before a bridge reads a result out of it."""


class BridgeError(Exception):
    """The application could not do what was asked; the message says why.

    Raised for the application's own refusals (``details`` carries any
    other fields of its reply), for operations its protocol cannot
    express, and for a connection that could not be made or kept.
    """

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ScoreBridge(ABC):
    """Interface for communicating with a score notation application.

    Every operation returns the result model that describes what the
    application reported, and raises :class:`BridgeError` when it could
    not do what was asked.
    """

    def operation(self) -> AbstractAsyncContextManager[None]:
        """Own a cursor-dependent workflow; stateful bridges override this scope."""
        return nullcontext()

    @property
    @abstractmethod
    def application_name(self) -> str:
        """Human-readable name of the application, for messages to the model."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether commands can be sent right now."""

    @property
    def content_reading_limitation(self) -> str | None:
        """Why reading score content is limited, or ``None`` when it is not.

        Analysis tools attach this as a warning so the model knows the data
        it got is the best the application can give.
        """
        return None

    # ── Connection ──────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the application. Returns whether it succeeded."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection to the application."""

    @abstractmethod
    async def ping(self) -> bool:
        """Whether the application answers."""

    @abstractmethod
    async def send_command(
        self, action: str, params: dict[str, Any] | None = None
    ) -> CommandResult:
        """Send an application-native command and return its raw reply."""

    # ── Reading ─────────────────────────────────────────────────────

    @abstractmethod
    async def get_score(self) -> ScoreInfo:
        """Metadata about the open score."""

    @abstractmethod
    async def get_cursor_info(self) -> CursorInfo:
        """The current position and what is there."""

    async def read_staff_context(
        self,
        start_measure: int,
        end_measure: int,
        staff: int | None = None,
    ) -> StaffContext:
        """Read bounded effective native staff context without a cursor."""
        raise BridgeError(f"{self.application_name} cannot read native staff context.")

    async def read_passage(
        self,
        start_measure: int,
        end_measure: int,
        staff: int | None = None,
        voice: int | None = None,
    ) -> Passage:
        """Read live events without navigating or selecting score elements.

        Applications without a content-reading protocol reject the operation.
        """
        raise BridgeError(f"{self.application_name} cannot read live score content.")

    @abstractmethod
    async def get_properties(self) -> SelectionProperties:
        """Properties of the current selection."""

    async def read_current_selection(self) -> CurrentSelection:
        """Read the actual native UI selection without changing it."""
        raise BridgeError(
            f"{self.application_name} cannot read the actual UI selection."
        )

    async def execute_mutation_once(self, request: ReceiptExecution) -> MutationReceipt:
        """Execute only supported, explicitly identified mutations."""
        raise BridgeError(
            f"{self.application_name} does not support mutation receipts."
        )

    async def read_mutation_receipt(self, request: ReceiptIdentity) -> MutationReceipt:
        """Retrieve historical operation state without executing it."""
        raise BridgeError(
            f"{self.application_name} does not support mutation receipts."
        )

    # ── Navigation and selection ────────────────────────────────────

    @abstractmethod
    async def go_to_measure(self, measure: int) -> CursorPosition:
        """Move to a measure (1-indexed)."""

    @abstractmethod
    async def go_to_staff(self, staff: int) -> CursorPosition:
        """Move to a staff (0-indexed)."""

    @abstractmethod
    async def select_measure(self) -> CursorPosition:
        """Select the measure at the current position."""

    @abstractmethod
    async def select_range(
        self, start_measure: int, end_measure: int, start_staff: int, end_staff: int
    ) -> SelectedRange:
        """Select a range of measures (1-indexed) and staves (0-indexed), inclusive."""

    # ── Writing ─────────────────────────────────────────────────────

    async def set_event_duration(
        self,
        measure: int,
        staff: int,
        voice: int,
        offset: Duration,
        duration: Duration,
        expected: list[ExpectedDurationEvent],
    ) -> DurationEdit:
        """Shorten one guarded chord using native duration rewriting."""
        raise BridgeError(
            f"{self.application_name} cannot perform guarded duration edits."
        )

    async def set_note_pitch(
        self,
        measure: int,
        staff: int,
        voice: int,
        offset: Duration,
        note_index: int,
        pitch: int,
        tpc: int,
        expected: ExpectedChord,
    ) -> PitchEdit:
        """Atomically match a target chord snapshot and edit one tone's pitch."""
        raise BridgeError(
            f"{self.application_name} cannot perform guarded pitch edits."
        )

    @abstractmethod
    async def add_note(
        self, pitch: int, duration: Duration, advance_cursor: bool = True
    ) -> NoteAdded:
        """Add a note (MIDI pitch) at the current position."""

    @abstractmethod
    async def add_rehearsal_mark(self, text: str) -> RehearsalMarkAdded:
        """Add a rehearsal mark at the current position."""

    @abstractmethod
    async def add_chord_symbol(self, text: str) -> ChordSymbolAdded:
        """Add a chord symbol at the current position."""

    @abstractmethod
    async def add_dynamic(self, dynamic: str) -> DynamicAdded:
        """Add a dynamic marking (``"mf"``, ``"p"``, ...) at the current position."""

    @abstractmethod
    async def set_barline(self, barline_type: str) -> BarlineSet:
        """Set the bar line at the end of the current measure."""

    @abstractmethod
    async def set_key_signature(self, fifths: int) -> KeySignatureSet:
        """Set the key signature (positive = sharps, negative = flats)."""

    @abstractmethod
    async def set_time_signature(
        self, numerator: int, denominator: int
    ) -> TimeSignatureSet:
        """Set the time signature at the current position."""

    @abstractmethod
    async def set_tempo(self, bpm: int, text: str | None = None) -> TempoSet:
        """Set the tempo at the current position."""

    @abstractmethod
    async def append_measures(self, count: int) -> MeasuresAppended:
        """Append empty measures to the end of the score."""

    @abstractmethod
    async def transpose(self, semitones: int) -> Transposed:
        """Transpose the current selection by a number of semitones."""

    @abstractmethod
    async def undo(self) -> CursorPosition:
        """Undo the last change; the cursor may move if the change removed measures."""
