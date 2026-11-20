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

`range_complete` confirms traversal of the requested rhythmic range, not full
notation fidelity. The `unsupported` field lists missing details, including grace
notes, ties and spanner endpoints, attached lyrics and articulations, written
pitch octaves for transposing instruments, and effective instrument, clef, key,
and tempo context. Opaque non-rhythmic segment elements report their type and
text where available.

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

The edit writes the selected native note's sounding pitch and concert/written
TPC properties. It preserves the exact previous written-minus-concert TPC
interval; a replacement written spelling outside the native range is rejected.
The operation returns native before/after events, the selected tone's new index,
and an explicit verification and mutation status. Chord ordering may change
when a tone crosses a peer; indices are valid only for the supplied snapshot.
Written octave remains unavailable in these results.

Only ordinary pitched chords are supported. Chords containing ties, grace notes,
tuplets, duplicate sounding tones, drum or tablature parts, microtonal tuning,
or unavailable eligibility metadata are rejected before mutation. Edits creating
a duplicate sounding chord tone are also rejected. Successful changes form one
undo step; an unchanged pitch and spelling create no step.

Native state is checked before and after committing. Verification failure inside
an open undo command cancels that command; `rolled_back` is reported only when a
native re-read matches the original target. A failure detected after commit is
reported as `applied_unverified` with actual snapshots, without blindly undoing
an earlier action. Transport failures use the separate unknown-outcome handling
below. Inspect the returned status before retrying or undoing.

The native property semantics and eligibility checks follow the supported API's
[Note and DurationElement definitions](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/elements.h)
and [undoable property setters](https://github.com/musescore/MuseScore/blob/v4.4.4/src/engraving/api/v1/scoreelement.cpp).
Live integration fixtures exercise ordinary and reordered chords, stale/no-op
requests, Bb and octave transposition, and unsupported tied, tuplet, and grace
notes across the supported MuseScore matrix.

## Protocol

The plugin accepts JSON messages over WebSocket. Each message must have a `command` field and optionally a `params` field. Responses have either a `result` field (on success) or an `error` field (on failure).

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

| Command           | Params                                       | Description                                                                                     |
| ----------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `ping`            | none                                         | Health check. Returns `"pong"`.                                                                 |
| `getCapabilities` | `{protocolVersion: int}`                     | Negotiate the protocol and report plugin version and supported top-level and sequence commands. |
| `getScore`        | none                                         | Score metadata: title, parts, measure count, key/time signatures.                               |
| `getCursorInfo`   | none                                         | Current cursor position: measure, staff, beat, element info.                                    |
| `readPassage`     | `{startMeasure, endMeasure, staff?, voice?}` | Read all live events in an inclusive measure range without navigating or selecting.             |

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

### Batch operations

| Command           | Params                                | Description                                                                                                                                                                                                                                                                         |
| ----------------- | ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `processSequence` | `{sequence: [{action, params}, ...]}` | Execute several commands in one undo step; if any step fails, all steps are rolled back. Steps may use `ping`, the navigation, writing, marking, structure, selection and transformation commands above, but not `getScore`, `getCursorInfo`, `undo` or a nested `processSequence`. |

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

- If any step in the sequence fails, all steps are undone
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
