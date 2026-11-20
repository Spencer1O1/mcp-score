# Required capabilities for the MuseScore fork

This fork must support a dependable read → edit → verify workflow against the
score currently open in MuseScore, including unsaved changes. Complete live
reads come first, followed by precise edits with verified results and undo.
Saved files may be inspected as an explicitly labeled fallback; they cannot
establish the current in-memory contents.

These are requirements, not claims about implemented functionality. The fork
tracks [tskovlund/mcp-score](https://github.com/tskovlund/mcp-score), retaining
its license and attribution. Changes belong in both the MuseScore plugin and
the Python bridge and MCP tools.

## Current limitations

The installed bridge used during the initial investigation returned only the
opening element of each measure. The checked-out upstream implementation also
calls `get_cursor_info` once per measure in `read_passage`; its documentation
now explicitly describes that limitation. `get_measure_content` selects a
measure and reports its location rather than its musical contents.

The installed note insertion implementation recreated its cursor from measure,
staff, and voice without retaining an offset within the measure. Consecutive
insertion cannot depend on that hidden cursor state. Check the fork's current
implementation before deciding which changes are still needed.

## Complete live reads

The bridge must read every event in a bounded measure range across requested
parts, staves, and voices. A read must include:

- Notes, chords, rests, and grace notes, preserving simultaneous chord tones.
- Measure identity and displayed numbering, staff, voice, absolute tick, exact
  rational offset within the measure, and exact duration.
- MIDI pitch and written pitch spelling, octave, and accidentals, distinguishing
  written and sounding pitch for transposing instruments.
- Dots, tuplets, ties, articulations, lyrics, dynamics, chord symbols, and
  relevant spanners with their endpoints.
- Effective instrument, clef, key signature, time signature, and tempo context,
  including pickups, irregular measures, and changes within a passage.

Expose whole-score summaries, measure reads, passage reads, and the user's
actual selection. Reading selection properties must not substitute for reading
the selected notation. Bound response size, provide continuation where needed,
and explicitly flag incomplete or unsupported information.

Reads must use independent traversal cursors and preserve score contents,
selection, viewport, dirty state, and undo/redo history.

## Explicit positions and score identity

- Identify the intended score in requests and the accessed score in responses.
  A title alone is insufficient when multiple scores share a name.
- Address edits by staff, voice, measure identity, and exact offset or tick.
  Support event identifiers where the application offers stable identities.
- Define indexing, time units, and inclusive/exclusive range boundaries once
  and use them consistently throughout the protocol.
- Distinguish individual chord tones and events sharing an onset.
- Include a revision token or equivalent precondition check. Reject stale edits
  and score switches before mutation; do not infer targets from hidden cursor
  state.

## Precise musical edits

The first operation should change a specific note's pitch or spelling while
preserving its rhythm and surrounding notation. Later operations must support:

- Changing duration with explicit handling of occupied time and bar overflow.
- Inserting and deleting notes and rests, including individual chord tones.
- Explicit replacement, chord extension, insertion, and overwrite semantics.
- Replacing a bounded passage with structured events after validating rhythm
  and coverage.
- Adding, changing, and removing dynamics, articulations, lyrics, chord symbols,
  tempo, signatures, ties, and spanners.

Define behavior for tuplets, tied notes, grace notes, transposing instruments,
and edits crossing barlines. Reject ambiguous targets and unsupported notation
with actionable errors; never silently discard notation or change spelling.

## Transactions and verification

- Prevalidate batches and apply each successful batch as one named undo step.
- Restore the previous musical state on batch failure. Verify rollback against
  actual MuseScore behavior rather than assuming the undo wrapper is sufficient.
- Return actual read-back events and a concise before/after description.
  Echoing requested parameters does not count as verification.
- Report verification failures with an explicit indication of whether changes
  remain applied.
- Use mutation request IDs and duplicate-request handling so reconnecting after
  a lost response cannot apply an edit twice.
- Keep saving and exporting explicit; editing must not silently save the file.

## Protocol and development workflow

- Negotiate plugin/server versions and supported capabilities on connection.
  Explain mismatches before accepting edits.
- Provide predictable typed event schemas, structured errors, and accurate MCP
  descriptions. Errors must identify the operation, target, cause, and whether
  mutation occurred.
- Expose active plugin/server versions and score identity for diagnostics.
- Document reproducible installation of the Python server and QML plugin,
  including Windows MuseScore and MCP client configuration on a separate host.
- Keep arbitrary Python execution out of routine score reading and editing.
- Preserve upstream attribution and track the upstream base of the fork.

## Acceptance milestones

### Complete reads without changing application state

On a disposable fixture matching the observed opening of “O Come, O Come,
Emmanuel,” a single measure read returns B4, A4, G4, F4 as sequential quarter
notes on the piano's upper staff and full-measure rests on the remaining
staves. An unsaved manual change appears immediately in the next read.
Selection, viewport, dirty state, and undo/redo history are unchanged by reads.

Exercise chords, multiple voices and staves, pickups, rests, accidentals,
dotted durations, tuplets, ties, and transposing instruments. Unsupported
information must be visible in responses.

### A precise edit with verified undo

On a disposable score, change only the third quarter note of the opening
measure from G4 to a specified pitch. Read back the complete measure and verify
that durations and surrounding notation are unchanged. One undo must restore
the previous musical state. Repeat with an unsaved score and a chord tone.

### Batches and concurrent changes

Verify a multi-event edit, reject invalid input without partial changes, and
exercise failure during mutation to confirm rollback. Reject stale revisions
and switched-score targets. Retry a mutation after a simulated lost response
and confirm that it is applied only once.

## Implementation sequence

1. Compare the checked-out upstream implementation with these requirements.
2. Add read-only plugin traversal and structured event serialization.
3. Expose complete reads through the Python bridge and MCP tools; update the
   generated reference through its existing generator when tool contracts change.
4. Add explicit position resolution, score identity, and conflict checks.
5. Add targeted pitch editing, read-back verification, and live undo tests.
6. Extend transactions and notation operations incrementally.

Keep the patch set small and reviewable. Use offline tests for schema and
protocol behavior and real MuseScore integration tests for traversal, notation
changes, undo, rollback, and preservation of application state. Run mutation
tests against disposable fixtures and an isolated MuseScore profile, never the
user's working score or normal application configuration.
