# MuseScore live score bridge specification

## Purpose and scope

The bridge shall expose the musical contents of the active MuseScore score and
support precise, verifiable edits to that score, including unsaved changes.

The specification covers the MuseScore plugin, Python bridge, and MCP tools.
Dorico integration, score generation, and offline rendering are outside its
scope. Existing capabilities in those areas shall remain compatible.

**Shall** denotes a required behavior. Acceptance criteria define observable
outcomes; implementation mechanisms are not prescribed unless stated explicitly.

## Definitions

- **Live score:** the in-memory score currently open in MuseScore.
- **Event:** a note, chord, rest, or other musical element at a defined position.
- **Position:** a measure, staff, voice, and exact offset within the measure.
- **Score identity:** an identifier that distinguishes open score instances,
  including instances with identical titles or file paths.
- **Revision:** a token or equivalent precondition that detects relevant changes
  since a score was read.
- **Mutation:** an operation that changes musical contents or notation.

## Live score reading

| ID     | Requirement                                                                                                                                                                                                                    |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| READ-1 | Reads shall return the live score, including unsaved changes. Responses derived from saved files shall identify that source and shall not claim to represent live state.                                                       |
| READ-2 | A bounded passage read shall return every event across the requested measures, staves, and voices, preserving simultaneous chord tones.                                                                                        |
| READ-3 | Each event shall include its type, measure identity, displayed measure number, staff, voice, absolute tick, exact rational measure offset, and exact duration where applicable.                                                |
| READ-4 | Note data shall include MIDI pitch, written pitch spelling, octave, and accidentals. Written and sounding pitch shall be distinguished for transposing instruments.                                                            |
| READ-5 | Reads shall represent grace notes, dots, tuplets, ties, articulations, lyrics, dynamics, chord symbols, and spanners with their endpoints.                                                                                     |
| READ-6 | Reads shall include effective instrument, clef, key signature, time signature, and tempo context, including changes within passages, pickups, and irregular measures.                                                          |
| READ-7 | The API shall expose score summaries, measure reads, passage reads, and the application's actual selection. Selection metadata alone shall not constitute a content read.                                                      |
| READ-8 | Reads shall preserve score contents, selection, viewport, dirty state, undo/redo history, and the position used by subsequent operations.                                                                                      |
| READ-9 | Responses shall identify unsupported or omitted information and indicate whether the requested range is complete. Size-limited responses shall provide continuation without missing or duplicating events within one revision. |

## Addressing and concurrency

| ID     | Requirement                                                                                                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ADDR-1 | Requests shall identify the intended score instance, and responses shall identify the accessed score instance. Titles alone shall not establish identity.                             |
| ADDR-2 | Edits shall accept explicit positions without dependence on a previous tool call or hidden cursor state. Individual chord tones and events sharing an onset shall be distinguishable. |
| ADDR-3 | The protocol shall define indexing conventions, time units, measure numbering, and range boundary semantics consistently across operations.                                           |
| ADDR-4 | Mutations shall check the expected score identity and revision before changing the score. A stale revision or switched score shall produce a conflict error without mutation.         |
| ADDR-5 | Event identifiers, when exposed, shall have a documented lifetime. References invalidated by edits, undo, or score reload shall be rejected rather than redirected to another event.  |

## Musical editing

| ID     | Requirement                                                                                                                                                                                                 |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| EDIT-1 | A targeted pitch or spelling edit shall preserve duration and all notation outside its declared change.                                                                                                     |
| EDIT-2 | The API shall support changing duration, inserting and deleting notes and rests, adding and removing individual chord tones, and replacing bounded passages with structured events.                         |
| EDIT-3 | Replacement, insertion, overwrite, and chord extension shall have explicit, distinct semantics. Duration and passage edits shall validate occupied time, rhythmic coverage, and bar overflow.               |
| EDIT-4 | The API shall support adding, changing, and removing dynamics, articulations, lyrics, chord symbols, tempo, signatures, ties, and spanners.                                                                 |
| EDIT-5 | Each operation shall define its behavior for tuplets, tied notes, grace notes, transposing instruments, and barline crossings. Unsupported cases and ambiguous targets shall fail without partial mutation. |
| EDIT-6 | Operations shall not discard notation or alter spelling outside the requested change. Save and export shall be explicit operations; ordinary mutations shall not save the score file.                       |

## Transactions and verification

| ID   | Requirement                                                                                                                                                                                                    |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| TX-1 | Batches shall validate their operations before mutation and apply successful changes as one named undo step.                                                                                                   |
| TX-2 | A failed batch shall restore the previous musical contents. Errors shall state whether any changes remain applied.                                                                                             |
| TX-3 | Successful mutations shall return actual read-back events and a before/after change description. Echoing request parameters shall not constitute verification.                                                 |
| TX-4 | Verification failures shall be reported distinctly from successful edits and shall indicate whether changes remain applied.                                                                                    |
| TX-5 | Mutation requests shall support duplicate detection. Retrying a request after a lost response shall not apply the change twice. The protocol shall define request ID scope, retention, and reconnect behavior. |

## Protocol and operation

| ID    | Requirement                                                                                                                                                                                             |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API-1 | Connection setup shall negotiate plugin/server compatibility and capabilities. Unsupported operations and incompatible versions shall be rejected before mutation.                                      |
| API-2 | Tool inputs and outputs shall use documented, typed schemas. Tool descriptions shall accurately describe their behavior and limitations.                                                                |
| API-3 | Errors shall identify the operation, target, cause, and mutation status. Diagnostics shall expose active plugin/server versions and score identity.                                                     |
| API-4 | Routine score reads and edits shall not require arbitrary Python execution, filesystem searches, or parsing saved score archives.                                                                       |
| API-5 | Installation documentation shall cover reproducible Python server and QML plugin setup, including Windows MuseScore and an MCP client on a separate host.                                               |
| API-6 | The fork shall retain upstream license notices and attribution and document its upstream base. Changes to tool contracts shall update the generated reference through the existing reference generator. |

## Acceptance criteria

All mutation scenarios shall run against disposable scores in an isolated
MuseScore profile. Tests shall not modify working scores or normal application
configuration. Protocol tests shall run offline; application behavior shall be
verified against real MuseScore instances.

### Complete live measure read

**Requirements:** READ-1 through READ-4, READ-8, READ-9, API-4.

Given a fixture with B4, A4, G4, F4 as sequential quarter notes on the piano's
upper staff and full-measure rests on the remaining staves:

1. A single measure read returns every event with the correct pitch, duration,
   staff, voice, and offset.
2. A manual pitch change made without saving appears in the next read.
3. Selection, viewport, dirty state, undo/redo history, and subsequent operation
   positions remain unchanged by either read.
4. Neither read requires inspecting the saved file.

### Notation coverage and continuation

**Requirements:** READ-2 through READ-7, READ-9, ADDR-3.

Fixtures containing chords, multiple voices and staves, grace notes, rests,
accidentals, dotted durations, tuplets, ties, spanners, lyrics, pickups,
irregular measures, context changes, and transposing instruments shall return
the corresponding notation and context. Unsupported cases shall be identified
explicitly. Reading a passage through continuation responses shall produce the
same events as an unrestricted read of the same revision.

### Precise edit and undo

**Requirements:** ADDR-2, EDIT-1, EDIT-6, TX-1, TX-3.

Given the opening fixture, changing only the third quarter note from G4 to A4
shall produce B4, A4, A4, F4 with unchanged durations and surrounding notation.
The response shall contain the actual resulting events. One undo shall restore
the prior musical contents. The scenario shall also pass for an unsaved score
and for a targeted tone within a chord.

### Editing coverage

**Requirements:** EDIT-2 through EDIT-6.

Each supported edit operation shall have integration scenarios verifying the
requested notation change and preservation of unrelated notation. Scenarios
shall cover occupied time, overflow, tuplets, ties, grace notes, transposition,
and barline crossings. Rejected cases shall leave no partial mutations.

### Batch failure and retry

**Requirements:** TX-1 through TX-5, API-3.

A valid multi-event batch shall create one undo step. Invalid input shall fail
before mutation. A failure injected during mutation shall restore the prior
musical contents and report the mutation outcome. An injected read-back failure
shall produce a verification error. Retrying a successful mutation after a
simulated lost response shall leave exactly one applied change.

### Stale targets and incompatible clients

**Requirements:** ADDR-1, ADDR-4, ADDR-5, API-1 through API-3.

Manual edits between a read and a mutation, switching to a different score, and
reusing invalid event references shall produce conflict or target errors
without mutation. Scores sharing titles or file paths shall remain distinct.
Incompatible plugin/server versions and unsupported capabilities shall be
reported before any edit is accepted.

## Delivery order

1. Complete live reads and non-interference with application state.
2. Explicit addressing, score identity, and revision checks.
3. Targeted pitch editing, verified read-back, and undo.
4. Batch failure recovery, duplicate detection, and remaining notation edits.

A milestone is complete only when its applicable acceptance criteria pass.
