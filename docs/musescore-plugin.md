# MuseScore plugin setup

> How-to guide -- install and configure the mcp-score MuseScore plugin for live manipulation.

## Overview

The MuseScore plugin runs inside MuseScore Studio 4 and opens a WebSocket server on `localhost:8765` using MuseScore's built-in plugin API (`api.websocketserver`). The mcp-score Python server connects to this WebSocket to read from and write to the active score.

The plugin is only needed for the **manipulation** and **analysis** tools (reading passages, arranging, transposing, etc.). The **generation** tools work without it -- they produce MusicXML files that MuseScore can open directly -- and so does `render_score`, which runs MuseScore's command line.

## Supported versions

MuseScore Studio **4.4.2 or later** is supported, and tested in CI against 4.4, 4.6 and 4.7 (Linux) and 4.7 (Windows, macOS).

Earlier versions are not supported: MuseScore 4.4 moved to Qt 6 and stopped shipping the `QtWebSockets` QML module, and versions before 4.4.2 have no replacement API. The plugin fails to load on those versions with `module "QtWebSockets" is not installed` (older plugin builds) or reports the missing API in its window (current build).

## Installation

The easy way:

```bash
mcp-score install-plugin
```

Or copy the `src/mcp_score/musescore/plugin/` directory by hand to `~/Documents/MuseScore4/Plugins/mcp-score-bridge/` (and delete a `mcp-score-bridge.qml` file left by a release before 0.2, or MuseScore lists two bridges). MuseScore uses that directory on macOS, Linux and Windows (`%USERPROFILE%\Documents\MuseScore4\Plugins\` on Windows); if you changed it in MuseScore's preferences (Folders > Plugins), use your own path.

Then:

1. Restart MuseScore, then go to **Plugins > Manage plugins** and enable "MCP Score Bridge"
2. Run the plugin: **Plugins > MCP Score Bridge**

The plugin opens a small status window and starts a WebSocket server on port 8765. **Keep the window open** -- closing it stops the plugin and the server with it (MuseScore 4 has no persistent dock plugins). Then connect from your MCP client with `connect_to_musescore`; the manipulation and analysis tools return a "not connected" error until you do.

### Updating the server and plugin together

The Python server negotiates a protocol version and supported commands before
accepting a MuseScore connection. Legacy plugins without this handshake and
plugins using an incompatible protocol are rejected with upgrade instructions.
Install the plugin from the same Python environment as the server, then close
and reopen its window. Updating Python alone does not update an already running
plugin.

When MuseScore runs on Windows and the MCP client runs on another host, install
the plugin on the Windows machine and configure the client to launch the matching
Python server environment. The server connects to the MuseScore host using the
`host` and `port` arguments to `connect_to_musescore`. This negotiation does not
provide authentication or network access control.

## Verifying the connection

The plugin window shows `Bridge running on ws://localhost:8765` when the server is up. MuseScore's log file also records:

```
[mcp-score] Bridge plugin started -- WebSocket server on port 8765
```

Log locations:

- **macOS:** `~/Library/Application Support/MuseScore/MuseScore4/logs/`
- **Linux:** `~/.local/share/MuseScore/MuseScore4/logs/`
- **Windows:** `%LOCALAPPDATA%\MuseScore\MuseScore4\logs\`

You can also test the connection with any WebSocket client:

```bash
# Using websocat (install with: cargo install websocat)
echo '{"command": "ping"}' | websocat ws://localhost:8765

# Expected response: {"result":"pong"}
```

## Reading the actual UI selection

`read_current_selection` reads `curScore.selection` directly, independently of
the bridge's logical cursor. It reports `kind: none`, `list`, or `range`, plus
every entry in the native selected-element list. An empty range remains a range,
even when filters leave its selected list empty. It does not navigate, select,
export, use the clipboard, or open an undo command.

Each selected ordinary note has its own `note` and snapshot-local `note_index`.
Its `event_context` includes the parent chord for rhythm and notation context;
the other chord tones are **not** thereby reported as selected. Whole native
chord and rest entries are distinguished from individual note entries. These
contents reuse the live passage serializer, including available lyric and
articulation metadata. Resolved entries include an ordinal measure, native tick,
staff/voice, and exact whole-note offset inside `event_context`.

Other selected types are opaque descriptors with native type, name, track and
text where exposed. Some have segment positions; others, including page/measure
attachments and spanners, can have `null` position/content fields. Grace notes
are not assigned the main chord's onset. `elements_complete` means every native
list entry was returned, not that every notation type was decoded. There are no
persistent selection/event IDs or edit preconditions.

Range tick and staff bounds are half-open. `end_tick: null` means a native range
ending at score end. `end_staff_exclusive` forwards the native `staffEnd()`
boundary; the wrapper's historical comment calls it inclusive, but native
`selectRange` and traversal use an exclusive end. Range contents come solely
from the native selected list, which applies selection and voice filters; the
reader never fills the bounding rectangle with unselected elements.
`filter_state: not_exposed` means the configured filters cannot be read from
these wrappers. `observed_voices` lists voices found in selected tracks, not a
voice filter or a promise that every voice in the range was selected.

The native routes were checked in versioned `src/engraving/api/v1/selection.h`
and `selection.cpp` for
[MuseScore 4.4.4](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/selection.h),
[4.6.2](https://github.com/musescore/MuseScore/blob/v4.6.2/src/engraving/api/v1/selection.h),
and [4.7.5](https://github.com/musescore/MuseScore/blob/v4.7.5/src/engraving/api/v1/selection.h).
The filtered-list behavior and exclusive staff boundary follow native
[`Selection::updateSelectedElements`](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/dom/select.cpp).
These wrappers do not expose dirty-state, viewport, undo-stack or selection-filter
inspection. The read uses native getters only. Oversize element lists or measure
scans fail without a partial result.

The legacy `get_selection_properties` tool continues returning the MuseScore
bridge's logical cursor; use `read_current_selection` for actual UI contents.
Dorico does not implement this native selection read. Older bridge plugins fail
with an upgrade instruction instead of substituting the logical cursor.

## Reading live passages

`read_passage` and `get_measure_content` return every rhythmic event in the
requested live measures, including unsaved changes. Omitting `staff` reads all
staves; omitting `voice` reads all voices. These reads do not select elements or
change the bridge's logical cursor. Measure numbers are ordinal, starting at
one, without expanding repeats; staff and voice indices start at zero.

Each measure includes its start tick, actual measure duration, and nominal time
signature where exposed by MuseScore. Measure duration is not used to infer the
meter: a whole-note duration alone cannot distinguish 4/4 from 2/2. Events include
absolute ticks, reduced whole-note offsets, notated durations, elapsed durations
where available, and all chord tones with concert pitch spelling. A full-measure
rest spans the actual measure length. Concert names use sounding MIDI pitch and
concert TPC even when MuseScore displays transposed notation; `displayed_tpc`
reports the current display spelling separately.

Chord/rest `lyrics` preserve the native text, zero-based `verse`, syllabic code
and symbolic name (`SINGLE`, `BEGIN`, `END`, `MIDDLE`), and placement code/name
(`ABOVE`, `BELOW`). Native text may contain MuseScore rich-text markup; it is
not converted to plain text. The verse index is not a displayed verse label.

Chord `articulations` expose the native symbol ID, localized subtype name,
placement code/name, and native anchor/direction codes where available. The
symbol ID belongs to the installed MuseScore version; the localized name is
for display, not a stable identifier. Placement is the native property, not the
final engraving coordinate. This list requires MuseScore's native
`Chord.articulations` getter, introduced in 4.6. On older wrappers it is `null`,
even when a visible articulation exists. Generic note-attached symbols and
fingerings are not inferred to be articulations.

For both attachment lists, `[]` means the native list was read and is empty;
`null` means unavailable or inapplicable (including payloads from older bridge
plugins). Individual unavailable properties remain `null`; unknown enum codes
are preserved with a `null` symbolic name. Attachment order and verse indices
are scoped to the current read, not persistent identifiers.

`range_complete` confirms traversal of the requested rhythmic range, not full
notation fidelity. The `unsupported` field lists missing details, including grace
notes, ties and spanner endpoints, lyric melisma endpoints, displayed verse
labels and rich-text interpretation, articulations when the native getter is
unavailable, note-attached symbols/fingerings, written pitch octaves for
transposing instruments, tuplet grouping, measure-attached elements outside
segments, system annotations without staff assignments, and effective
instrument, clef, key, and tempo context. Opaque non-rhythmic segment elements
report their type and text where available. Dynamics and chord symbols remain
opaque segment annotations; their notation-specific semantics are not decoded.

The supported native attachment routes are verified against the versioned
`src/engraving/api/v1/elements.h` wrappers in
[MuseScore 4.4.4](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/elements.h),
[4.6.2](https://github.com/musescore/MuseScore/blob/v4.6.2/src/engraving/api/v1/elements.h),
and [4.7.5](https://github.com/musescore/MuseScore/blob/v4.7.5/src/engraving/api/v1/elements.h).
`ChordRest.lyrics` is present in each; `Chord.articulations` is absent in the
oldest wrapper. Reads detect the actual properties instead of guessing based
on the version. They never export a score or change selection to discover
attachments.

Reads reject ranges exceeding the plugin's measure or event limits instead of
returning a truncated passage. Request smaller ranges after such an error.
Revision-bound continuation and persistent event identities are not implemented.
An old plugin that lacks `readPassage` returns an error; the Python server does
not fall back to returning only the first note. Reinstall the plugin when
updating the server.

### Looking up an exact event

`get_event_at_position` reads one rhythmic onset using explicit ordinal measure,
staff, voice, and whole-note fraction coordinates. For example, an offset of
`2/4` matches the same onset as `1/2` (beat three in a 4/4 measure). The numerator
must be a nonnegative integer and the denominator a positive integer; floats,
numeric strings, and booleans are rejected. Positions inside a sustained note
or rest do not match its onset. An absent or ambiguous onset produces an error.

Omit `note_index` to return the whole chord or rest. Supply a zero-based index
to select a tone from the returned chord's `notes` list while retaining the
whole event in the response. A rest has no tone indices. Indices describe the
ordering in that response and are not durable note identifiers.

The Python tool reuses one scoped live passage read and returns its unsupported
notation list. It does not move selection or the bridge cursor. The returned
address has `single_read` scope: it establishes no score identity, revision,
or edit precondition. After any score change, read again before interpreting
the coordinates. The tool does not implement containing-note lookup or grace
note addressing.

## Guarded pitch edits

`set_live_note_pitch` changes one chord tone at an explicit measure, staff,
voice, and exact whole-note offset. Supply the complete chord event from a
fresh `get_event_at_position` or passage read as `expected`, plus that event's
`note_index`, replacement sounding MIDI `pitch`, and concert `tpc` spelling.
For example, G4 is MIDI 67 and TPC 15; C#4 is MIDI 61 and TPC 21. The MIDI pitch
and TPC must agree. Coordinates, indices, pitch/TPC values, and fraction fields
are strict integers; booleans, numeric strings, and floating-point values are
rejected instead of converted.

The snapshot must contain all chord tones, including `written_tpc`. Older
reader replies without that field are insufficient; install the matching plugin
and read again. The handler compares the current onset, track, rhythm, pitches,
and spellings against the snapshot before opening an undo command. The check
and edit run synchronously inside MuseScore, so no separate tool call can slip
between them. This guards only target contents: it provides no persistent event
ID, application-wide score identity, or revision token, and cannot detect a
switch to another score with identical target contents.

Complete events containing `lyrics` and `articulations` can be supplied without
stripping those fields. They are typed informational metadata: the pitch guard
does not compare attachment changes or expand its precondition to protect them.

The edit writes the selected native note's sounding pitch and concert/written
TPC properties. It preserves the exact previous written-minus-concert TPC
interval; a replacement written spelling outside the native range is rejected.
The operation returns native before/after events, the selected tone's new index,
and an explicit verification and mutation status. Chord ordering may change
when a tone crosses a peer; indices are valid only for the supplied snapshot.
Written octave remains unavailable in these results.

Only ordinary pitched chords are supported. Chords containing ties, grace notes,
tuplets, duplicate sounding tones, drum or tablature parts, microtonal tuning
or accidentals are rejected before mutation. Missing eligibility metadata also
rejects the edit. Edits creating a duplicate sounding chord tone are rejected. Successful changes form one
undo step; an unchanged pitch and spelling create no step.

Accidental cents and explicit note tuning are separate native properties. The
guard requires zero explicit tuning and a verified conventional accidental type;
unknown or missing accidental metadata fails closed. The passage reader reports
base MIDI/TPC values and lists unrepresented accidental cents and playback tuning
in its unsupported metadata.

Native state is checked before and after committing. Verification failure inside
an open undo command cancels that command; `rolled_back` is reported only when a
native re-read matches the original target. A failure detected after commit is
reported as `applied_unverified`, without blindly undoing an earlier action.
That known committed status is retained even if a subsequent native read fails;
an unavailable actual snapshot is returned as null. Transport failures use the
separate unknown-outcome handling below. Inspect the returned status before retrying or undoing.

The native property semantics and eligibility checks follow the supported API's
[Note and DurationElement definitions](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/elements.h)
[undoable property setters](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/scoreelement.cpp),
and [native accidental cent offsets](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/dom/accidental.cpp).
Live integration fixtures exercise ordinary and reordered chords, stale/no-op
requests, Bb and octave transposition, and unsupported tied, tuplet, grace,
and quarter-tone notes across the supported MuseScore matrix.

## Protocol

The plugin accepts JSON objects over WebSocket. Each message must have a
nonempty string `command` naming a supported command. Optional `params` must be
an object; omitting it supplies an empty object. Explicit `null`, arrays, and
primitive values are rejected as envelopes or parameters. Malformed JSON,
invalid envelopes, and unknown commands receive an `error` response without
executing a command; the same socket remains usable. Responses have either a
`result` field (on success) or an `error` field (on failure).

### Capability negotiation

The Python bridge sends `getCapabilities` with `{"protocolVersion": 1}` when
opening a connection, including after reconnecting. The plugin returns:

```json
{
  "result": {
    "protocolVersion": 1,
    "pluginVersion": "0.2.0",
    "commands": ["getCapabilities", "ping"],
    "sequenceCommands": ["ping"]
  }
}
```

The command lists above are abbreviated. The plugin derives the actual lists
from its command and sequence dispatch tables. The protocol revision describes
the wire contract independently of the package version. A mismatched revision
or malformed capability response causes the Python bridge to close the connection.
The handshake requires no open score.

Before each request, the bridge checks that the negotiated plugin supports the
command, including each step of `processSequence`. Reconnecting renegotiates and
rechecks support before retrying an allowlisted read-only request. Unsupported operations are rejected
without sending them to the plugin.

`connect_to_musescore` returns the negotiated plugin protocol, plugin version,
server package version, and supported commands. `get_musescore_bridge_info`
returns those diagnostics for the active MuseScore connection without reading
or modifying the score. Command presence describes available protocol operations;
it does not imply complete notation coverage, precise event addressing, or
verified mutation results.

### Request format

```json
{
    "command": "commandName",
    "params": { ... }
}
```

### Response format (success)

```json
{
    "result": { ... }
}
```

### Response format (error)

```json
{
  "error": "Description of what went wrong"
}
```

## Available commands

### Score information

| Command                | Params                                       | Description                                                                                                   |
| ---------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `ping`                 | none                                         | Health check. Returns `"pong"`.                                                                               |
| `getCapabilities`      | `{protocolVersion: int}`                     | Negotiate the protocol and report plugin version and supported top-level and sequence commands.               |
| `getScore`             | none                                         | Score metadata: title, parts, measure count, key/time signatures.                                             |
| `getCursorInfo`        | none                                         | Current cursor position: measure, staff, beat, element info.                                                  |
| `readPassage`          | `{startMeasure, endMeasure, staff?, voice?}` | Read all live events in an inclusive measure range without navigating or selecting.                           |
| `readCurrentSelection` | `{}`                                         | Read the actual native selected-element list, range bounds and supported contents without changing selection. |

### Guarded mutation

| Command        | Params                                                             | Description                                                                                                                        |
| -------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `setNotePitch` | `{measure, staff, voice, offset, noteIndex, pitch, tpc, expected}` | Atomically match a complete chord snapshot and change one native note with read-back verification. Top-level only; never replayed. |

### Navigation

| Command       | Params           | Description                         |
| ------------- | ---------------- | ----------------------------------- |
| `goToMeasure` | `{measure: int}` | Move cursor to measure (1-indexed). |
| `goToStaff`   | `{staff: int}`   | Move cursor to staff (0-indexed).   |

### Writing notes

| Command   | Params                                          | Description                                                                                                                                  |
| --------- | ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `addNote` | `{pitch, duration?, advanceCursorAfterAction?}` | Add a note at cursor. Pitch is MIDI number (60 = middle C). Duration is `{numerator, denominator}` fraction, defaults to 1/4 (quarter note). |

### Score markings

| Command            | Params                      | Description                                      |
| ------------------ | --------------------------- | ------------------------------------------------ |
| `addRehearsalMark` | `{text: string}`            | Add rehearsal mark (e.g. "A", "B").              |
| `setBarline`       | `{type: string}`            | Set barline type. See barline types below.       |
| `setKeySignature`  | `{fifths: int}`             | Set key signature (-7 to 7 on circle of fifths). |
| `setTimeSignature` | `{numerator, denominator}`  | Set time signature (e.g. 4/4, 3/4, 6/8).         |
| `setTempo`         | `{bpm: int, text?: string}` | Set tempo marking. Optional text override.       |
| `addChordSymbol`   | `{text: string}`            | Add chord symbol (e.g. "Cmaj7", "Dm7b5").        |
| `addDynamic`       | `{type: string}`            | Add dynamic marking. See dynamic types below.    |

### Score structure

| Command          | Params         | Description                                    |
| ---------------- | -------------- | ---------------------------------------------- |
| `appendMeasures` | `{count: int}` | Append empty measures to the end of the score. |

### Selection and transformation

| Command                | Params                                             | Description                                                                                    |
| ---------------------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `selectCurrentMeasure` | none                                               | Select all elements in the measure at the cursor.                                              |
| `selectCustomRange`    | `{startMeasure, endMeasure, startStaff, endStaff}` | Select a range. Measures are 1-indexed (inclusive). Staves are 0-indexed (inclusive).          |
| `transpose`            | `{semitones: int}`                                 | Transpose the current selection. Positive = up, negative = down. Requires an active selection. |

### Undo

| Command | Params | Description                                                   |
| ------- | ------ | ------------------------------------------------------------- |
| `undo`  | none   | Undo the last action; reports the cursor position afterwards. |

### Guarded duration shortening

`setEventDuration` is a top-level mutation (never a sequence action or a replay-safe
command). Parameters are `{measure, staff, voice, offset, duration, policy,
expected}`. Measures are ordinal and 1-indexed; staff/voice are zero-based and
fractions use whole notes. `policy` must be `shorten_fill_rest`. `expected` must
contain **every event** in a fresh complete read of that measure filtered to the
explicit staff and voice, including annotations. Neighbor pitch/spelling,
rhythm, lyrics and articulations in that voice are checked atomically before
opening the undo command. Snapshot scope is that voice measure, not a score
instance or application revision; identical contents in another score remain
indistinguishable.

This first duration slice uses the actual native `DurationElement.duration`
setter, which calls `Score::changeCRlen`; it never deletes/reinserts a note as a
fallback. The setter was added in 4.6, but this operation requires actual
**MuseScore 4.7+ in major version 4** because complete score-spanner enumeration
was added in 4.7. The handler checks native application version and metadata,
not the bridge package version. 4.4 and 4.6 reject before any undo command.

Only ordinary pitched chords with equal notated/elapsed durations may shorten
within one measure. Original and replacement durations must be undotted whole,
half, quarter, eighth, 16th or 32nd notes. Equal durations return `unchanged`
without history; larger durations reject. Target ties, tuplets, grace notes,
beams, tremolos, arpeggios, cross-staffing, drum/tab staves, microtonality,
lyrics, articulations and other attached child elements reject. Missing native
eligibility metadata also rejects. This prevents attachment loss rather than
claiming that arbitrary notation survives native rewriting.

Spanners with unknown time/track metadata reject. A spanner whose closed time
interval intersects the original chord interval rejects, conservatively across
all staves. A known distant spanner is allowed; all enumerated spanner type,
start, length, tracks and anchor values must survive unchanged. Native
`spannerTicks` is a **length**, despite the API header's end-tick description.

Read-back traverses the entire measure and re-resolves events after native
rewriting. It verifies the shortened chord's pitch/spelling, contiguous rest
coverage through the original end, and unchanged exposed events on every track
(including neighboring lyric/articulation metadata). Responses return actual
all-track `before`/`after` events and `verified: true` only after checks both
inside the undo scope and after commit. This verifies exposed event data, not
every engraving property. Native cancellation is attempted only while this
operation's undo scope is known open; `rolled_back` requires a matching reread.
A failed verification after commit reports `applied_unverified`; no global undo
is attempted. Inspect failed outcomes before retrying.

Native rewriting may deselect items and change layout. The logical cursor is
untouched, but selection is **not restored**: applied results report
`selectionStatus: "not_preserved"`; no-ops report `"unchanged"`. This operation
does not claim UI/viewport preservation.

Native API evidence:
[duration wrapper and setter](https://github.com/musescore/MuseScore/blob/v4.7.5/src/engraving/api/v1/elements.cpp),
[native shortening implementation](https://github.com/musescore/MuseScore/blob/v4.7.5/src/engraving/editing/cmd.cpp),
[spanner property semantics](https://github.com/musescore/MuseScore/blob/v4.7.5/src/engraving/dom/spanner.cpp).
The isolated native fixture checks shortening/rest fill, chord/transposing
spelling, untouched neighbors, rejection and undo; older-version jobs exercise
explicit rejection rather than skipping their duration phase.

### Batch operations

| Command           | Params                                | Description                                                                                                                                                                                                                                                                                                                          |
| ----------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `processSequence` | `{sequence: [{action, params}, ...]}` | Validate every step's structure, then execute commands in one undo step. Failures request native rollback and restore the logical cursor. Steps may use `ping`, the navigation, writing, marking, structure, selection and transformation commands above, but not `getScore`, `getCursorInfo`, `undo` or a nested `processSequence`. |

### Barline types

`normal`, `double`, `startRepeat`, `endRepeat`, `endStartRepeat`, `final`, `dashed`, `dotted`, `tick`, `short`

### Dynamic types

`pppp`, `ppp`, `pp`, `p`, `mp`, `mf`, `f`, `ff`, `fff`, `ffff`, `fp`, `sfz`, `sffz`, `sfp`, `rfz`, `fz`

Any other text is accepted as the marking's text; only the ones listed also set the playback velocity.

### Key signature fifths values

| Value | Major key | Minor key |
| ----- | --------- | --------- |
| -7    | Cb major  | Ab minor  |
| -6    | Gb major  | Eb minor  |
| -5    | Db major  | Bb minor  |
| -4    | Ab major  | F minor   |
| -3    | Eb major  | C minor   |
| -2    | Bb major  | G minor   |
| -1    | F major   | D minor   |
| 0     | C major   | A minor   |
| 1     | G major   | E minor   |
| 2     | D major   | B minor   |
| 3     | A major   | F# minor  |
| 4     | E major   | C# minor  |
| 5     | B major   | G# minor  |
| 6     | F# major  | D# minor  |
| 7     | C# major  | A# minor  |

## Troubleshooting

### Plugin doesn't appear in Manage plugins

- Verify the whole `mcp-score-bridge/` directory is in the Plugins directory: the `.qml` file imports the `.js` modules next to it
- Restart MuseScore after adding the plugin
- Check your MuseScore version: **Help > About**. The bridge requires 4.4.2 or later
- MuseScore hides plugins that fail to compile. Look in the log file (paths above) for `ExtensionBuilder::load | Failed to load QML file` and the line after it, which names the cause. `module "QtWebSockets" is not installed` means an old copy of the plugin: re-run `mcp-score install-plugin`

### Plugin window shows an error

- `this MuseScore build has no plugin WebSocket API` -- your MuseScore is older than 4.4.2. Upgrade MuseScore

### WebSocket connection fails

- Ensure the plugin window is open and shows `Bridge running`
- Verify no other application is using port 8765: `lsof -i :8765` (macOS/Linux) or `netstat -ano | findstr 8765` (Windows)
- Check MuseScore's log file for `[mcp-score]` error messages

### Capability negotiation fails

- An `Unknown command: getCapabilities` response identifies a legacy plugin.
  Run `mcp-score install-plugin` from the server's Python environment on the
  MuseScore host, then close and reopen the plugin window.
- An incompatible protocol response means the Python server and loaded plugin
  do not share a wire contract. Install a matching pair before reconnecting.
- An unsupported command error means the connected plugin did not advertise
  that operation. Check `get_musescore_bridge_info` and install a matching plugin
  rather than retrying the same edit against the same connection.

### "No score is currently open" errors

- Open a score in MuseScore before sending commands
- The plugin operates on the currently active score (`curScore`)

### Commands seem to have no effect

- Write operations require an open, editable score (not a read-only file)
- Check that the cursor position (measure, staff) is within valid ranges
- Use `getScore` to verify the score state and `getCursorInfo` to check cursor position

### A mutation reports an unknown outcome

If sending an edit or receiving its reply fails, the Python bridge closes the
connection and does not replay the edit. This also applies to `processSequence`,
undo, navigation, and commands whose safety is unknown. Read-only requests may
reconnect and retry after negotiating support again.

The tool error text includes a JSON envelope with `details.code` set to
`outcome_unknown`, `mutation_status` set to `unknown`, and `replayed` set to
`false`. The operation, target, cause, and a diagnostic `request_id` are retained.
The request ID is client-only correlation data, not a server deduplication key.
Inspect the live score before deciding whether to retry or undo. A lost reply
does not establish that an edit failed or that a sequence rolled back.

### processSequence rolls back unexpectedly

- Every step must be an object with a nonempty string `action` from the sequence
  dispatch table. Optional `params` must be an object; explicit `null`, arrays,
  and primitives are rejected. The whole list is checked before any navigation,
  score mutation, or undo command starts. Structural rejections report
  `failedIndex`, `mutationStatus: "not_applied"`, and
  `rollbackStatus: "not_needed"`.
- Argument semantics are checked when each action executes, since a later step
  can depend on an earlier change, such as navigating to an appended measure.
  This is structural prevalidation, not complete TX-1 semantic prevalidation.
- If a step returns an error or throws, the plugin requests rollback through
  `endCmd(true)` and restores its logical measure, staff, and voice in `finally`.
  It never invokes global undo on failure. UI selection restoration is not
  guaranteed by this logical-cursor cleanup.
- Runtime errors report `mutationStatus: "unknown"` and
  `rollbackStatus: "requested"` when the native rollback call returns. These
  fields confirm the request, not a complete read-back of musical contents.
  A transaction-boundary exception reports both statuses as `"unknown"` and
  still restores the logical cursor. Inspect the score before retrying.
- Check the `failedAction` and `failedIndex` fields in the error response to identify which step failed
- For a plugin-reported failure, inspect the reported step and rollback before
  correcting and resubmitting the sequence. For a transport error with an unknown
  outcome, inspect the live score first; rollback has not been confirmed.

## Plugin development notes

Things the MuseScore 4 plugin API does not document, learned by crashing MuseScore. The live bridge integration tests (`tests/integration/test_musescore_bridge.py`) exercise these against real MuseScore.

- **Bar lines cannot be added through the cursor.** `cursor.add` crashes MuseScore for `BAR_LINE` elements. Change the existing end bar line instead (`measure.lastSegment.elementAt(staff * 4).barlineType`), or set the measure's `repeatStart` / `repeatEnd` flags for repeats.
- **Chord symbols get their text after insertion.** A `HARMONY` element must be added with `cursor.add` before its `text` is set; setting `text` first crashes MuseScore.
- **The undo action was renamed in 4.7.** `cmd("undo")` works on 4.4-4.6; 4.7 registers `cmd("action://notation/undo")` instead. The plugin picks by `mscoreMajorVersion` / `mscoreMinorVersion`.
- **`Score.transpose` does not exist in MuseScore 4.** The plugin shifts each note's `pitch`, `tpc1` and `tpc2` itself, which is why key signatures and chord symbols in the passage are not transposed.
- **`curScore.endCmd(true)` rolls back the current undo step.** Every command runs inside `startCmd` / `endCmd`; `processSequence` uses the rollback to restore the score when a step fails.
- **Integration startup opens the Plugins menu.** `scripts/musescore_harness.py` enables only the bridge in the test profile. It activates the visible application window and navigates the Plugins menu on Linux and Windows, and invokes named native menu items on macOS. No custom shortcut or screen coordinates are required. The profile suppresses update checks as well as welcome dialogs, so late modal dialogs do not steal input. English menu labels are required. The bridge must answer a real ping before tests run; a running application process alone is insufficient.

### Integration profile ownership

The test harness activates only its recorded MuseScore process. `stop` verifies
each PID's creation time and executable before signaling it. It does not use
process-name kills. Logs remain available until `collect-logs` and `clean` are
requested. See [the integration instructions](../CONTRIBUTING.md#integration-tests).

Linux isolation uses child-specific XDG paths. Windows uses the official portable
binary's executable-relative settings and app-data paths, rather than assuming
`APPDATA` overrides Qt's known-folder APIs. The portable build is identified by
its deployed data-path literal before launch. Current application logs and
persisted preferences inside the declared boundary are required for startup.
The native path decisions follow [MuseScore's portable settings implementation](https://github.com/musescore/MuseScore/blob/v4.7.5/src/framework/global/settings.cpp),
[its application data resolver](https://github.com/musescore/MuseScore/blob/v4.7.5/src/framework/global/internal/globalconfiguration.cpp),
and [Qt's Windows known-folder resolver](https://github.com/qt/qtbase/blob/v6.9.3/src/corelib/io/qstandardpaths_win.cpp).

Local/default macOS startup fails closed before native configuration writes.
CoreFoundation home/preference-daemon environment overrides did not establish
private native preferences in actual macOS CI. In particular, the seeded private
nonce was absent through the native API used by [Qt's settings backend](https://github.com/qt/qtbase/blob/v6.9.3/src/corelib/io/qsettings_mac.cpp).
Those overrides are not presented as an isolation mechanism.

The macOS integration job instead explicitly opts into the **whole disposable
GitHub-hosted runner account** boundary. Hosted Actions metadata and matching
runner account/home/temp paths are required; local and self-hosted runs are
refused. This is accidental-use protection, not a security sandbox. Native
MuseScore account preferences and data intentionally change in this mode.
Artifact/process records and plugin payloads remain under a separate marked
root. Only the exact native `session` directory is atomically moved into unique
owned backups before fixture launches; symlinked paths and failed renames fail
closed. Cleanup removes owned artifacts/backups without recursively deleting the
native data tree or home. Remaining account mutations are left for hosted-runner
disposal. An explicit boundary report records native changes, session moves,
owned-artifact removal and process cleanup; no ordinary-profile preservation
claim is made for this mode. The same real macOS bridge and fixture tests run.

Qt Quick pipeline caches use the documented `QSG_RHI_PIPELINE_CACHE_LOAD` and
`QSG_RHI_PIPELINE_CACHE_SAVE` file overrides inside the marked artifact root; this
avoids Windows known-folder cache writes without disabling snapshot assertions.
See [Qt's graphics configuration implementation](https://github.com/qt/qtdeclarative/blob/v6.9.3/src/quick/items/qquickgraphicsconfiguration.cpp).
Linux and Windows CI additionally check that the ordinary profile remains
unchanged across startup, fixture changes, and cleanup.
