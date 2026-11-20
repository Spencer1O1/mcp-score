// A target snapshot is checked and edited in one synchronous handler.
// No note wrappers or target indices survive the request.
.pragma library
.import "constants.js" as Constants
.import "passage.js" as Passage
.import "score.js" as Score

var snapshotLimitations = [
    "target-chord contents only; no application-wide score identity or revision",
    "switching to a score with identical target contents is not detected",
    "chord-tone indices are valid only for the supplied snapshot",
    "written octave is not returned"
];
var maxExactInteger = 9007199254740991;

function integer(value) {
    return Passage.integer(value) && Math.abs(value) <= maxExactInteger;
}

function fractionKey(value) {
    var reduced = Passage.duration(value);
    return reduced ? [reduced.numerator, reduced.denominator] : null;
}

function toneKey(note) {
    return [note.pitch, note.tpc, note.writtenTpc, note.displayedTpc, note.name];
}

function eventKey(event, includeNotes) {
    return JSON.stringify([
        event.kind, event.type, event.staff, event.voice, event.tick,
        fractionKey(event.offset), fractionKey(event.duration),
        fractionKey(event.actualDuration), event.text || null,
        includeNotes ? event.notes.map(toneKey) : null
    ]);
}

function target(params) {
    return { measure: params.measure, staff: params.staff, voice: params.voice,
        offset: params.offset, note_index: params.noteIndex };
}

function failure(params, code, message, status, extra) {
    var reply = { error: message, code: code, mutation_status: status,
        verified: false, target: target(params), snapshot_scope: "target_chord" };
    Object.keys(extra || {}).forEach(function(key) { reply[key] = extra[key]; });
    return reply;
}

function validTpc(tpc) {
    return integer(tpc) && tpc >= Constants.minTpc && tpc <= Constants.maxTpc;
}

function congruentPitch(pitch, tpc) {
    return ((7 * (tpc - 14) - pitch) % 12 + 12) % 12 === 0;
}

/// Resolve one chord at an exact tick, without touching cursor or selection.
function resolve(plugin, params) {
    var measure = plugin.curScore.firstMeasure;
    for (var index = 1; measure && index < params.measure; index++) {
        measure = measure.nextMeasure;
    }
    if (!measure || !measure.firstSegment) return { error: "Measure out of range" };
    var offset = params.offset;
    var scaled = offset.numerator * Constants.ticksPerWholeNote;
    if (!integer(scaled) || scaled % offset.denominator !== 0) {
        return { error: "Offset cannot be represented exactly in MuseScore ticks" };
    }
    var tick = measure.firstSegment.tick + scaled / offset.denominator;
    var segment = measure.firstSegment;
    var chords = [];
    var track = params.staff * Constants.voicesPerStaff + params.voice;
    while (segment) {
        if (segment.tick === tick) {
            var element = segment.elementAt(track);
            if (element && element.type === plugin.Element.CHORD) chords.push(element);
        }
        segment = segment.nextInMeasure;
    }
    if (chords.length !== 1) {
        return { error: chords.length ? "Ambiguous chord onset" : "No chord at target onset" };
    }
    return { chord: chords[0], tick: tick, measureTick: measure.firstSegment.tick };
}

/// Accidental cents are separate from note.tuning. Note.accidentalType reads
/// the native accidental object, so only verified conventional zero-cent
/// symbols are eligible; missing metadata or future symbols fail closed.
function conventionalAccidental(plugin, note) {
    if (!plugin.Accidental || !integer(note.accidentalType)
            || note.accidental === undefined) return false;
    var names = ["NONE", "FLAT", "NATURAL", "SHARP", "SHARP2", "FLAT2",
                 "NATURAL_FLAT", "NATURAL_SHARP", "SHARP_SHARP"];
    return names.some(function(name) {
        return integer(plugin.Accidental[name]) && note.accidentalType === plugin.Accidental[name];
    });
}

/// All queried eligibility properties are exposed by the native 4.4+ API.
/// Missing information rejects the edit rather than assuming an ordinary note.
function supported(plugin, chord) {
    if (!plugin.NoteType || chord.noteType !== plugin.NoteType.NORMAL
            || chord.tuplet !== null || !chord.graceNotes
            || chord.graceNotes.length !== 0) {
        return false;
    }
    var part = chord.staff ? chord.staff.part : null;
    if (!part || part.hasPitchedStaff !== true || part.hasDrumStaff !== false
            || part.hasTabStaff !== false) return false;
    for (var index = 0; index < chord.notes.length; index++) {
        var note = chord.notes[index];
        for (var peer = 0; peer < index; peer++) {
            if (chord.notes[peer].pitch === note.pitch) return false;
        }
        if (note.noteType !== plugin.NoteType.NORMAL || note.tieBack !== null
                || note.tieForward !== null || note.tuning !== 0
                || !conventionalAccidental(plugin, note)
                || !validTpc(note.tpc1) || !validTpc(note.tpc2)
                || !congruentPitch(note.pitch, note.tpc1)) return false;
    }
    return true;
}

function describe(plugin, resolved, params) {
    return Passage.describeEvent(plugin, resolved.chord, params.staff, params.voice,
                                 resolved.tick, resolved.measureTick);
}

function noteIndex(chord, selected) {
    for (var index = 0; index < chord.notes.length; index++) {
        if (chord.notes[index].is(selected)) return index;
    }
    return -1;
}

/// Native setters may reorder chord tones. Verify peers as a multiset while
/// checking the selected native object separately inside this same handler.
function matchesEdit(before, after, index, pitch, tpc, writtenTpc, displayedTpc) {
    if (eventKey(before, false) !== eventKey(after, false)) return false;
    var expectedNotes = before.notes.map(toneKey);
    expectedNotes[index] = [pitch, tpc, writtenTpc, displayedTpc,
                            Passage.concertName(pitch, tpc)];
    var actualNotes = after.notes.map(toneKey);
    expectedNotes = expectedNotes.map(function(note) { return JSON.stringify(note); }).sort();
    actualNotes = actualNotes.map(function(note) { return JSON.stringify(note); }).sort();
    return JSON.stringify(expectedNotes) === JSON.stringify(actualNotes);
}

function result(params, before, after, afterIndex, status) {
    return { result: {
        measure: params.measure, noteIndexBefore: params.noteIndex,
        noteIndexAfter: afterIndex, before: before, after: after,
        verified: true, mutationStatus: status, snapshotScope: "target_chord",
        limitations: snapshotLimitations.slice()
    } };
}

function setNotePitch(plugin, params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return failure(params, "target_unavailable", scoreError.error,
                                   "not_applied");
    if (!integer(params.measure) || params.measure < 1 || !integer(params.staff)
            || params.staff < 0 || params.staff >= plugin.curScore.nstaves
            || !integer(params.voice) || params.voice < 0
            || params.voice >= Constants.voicesPerStaff
            || !integer(params.noteIndex) || params.noteIndex < 0
            || !params.offset || !integer(params.offset.numerator)
            || !integer(params.offset.denominator) || params.offset.numerator < 0
            || params.offset.denominator <= 0 || !integer(params.pitch)
            || params.pitch < Constants.minMidiPitch || params.pitch > Constants.maxMidiPitch
            || !validTpc(params.tpc) || !congruentPitch(params.pitch, params.tpc)) {
        return failure(params, "invalid_target", "Invalid position, pitch or spelling",
                       "not_applied");
    }
    var expected = params.expected;
    if (!expected || expected.kind !== "chord" || !Array.isArray(expected.notes)
            || !expected.notes.length || expected.notes.some(function(note) {
                return !note || !validTpc(note.writtenTpc);
            })) {
        return failure(params, "invalid_snapshot",
                       "A complete fresh chord snapshot with written_tpc for every tone is required; update the plugin and read again",
                       "not_applied");
    }
    var resolved = resolve(plugin, params);
    if (resolved.error) {
        return failure(params, "target_unavailable", resolved.error, "not_applied");
    }
    var before = describe(plugin, resolved, params);
    if (eventKey(before, true) !== eventKey(expected, true)) {
        return failure(params, "stale_target", "Target chord differs from expected snapshot",
                       "not_applied", { actual: before });
    }
    var chord = resolved.chord;
    if (params.noteIndex >= chord.notes.length) {
        return failure(params, "invalid_target", "Chord-tone index out of range", "not_applied");
    }
    if (!supported(plugin, chord)) {
        return failure(params, "unsupported_target",
                       "Only ordinary pitched chords without ties, grace notes, tuplets, drum/tab staves, duplicate tones or microtonal tuning are supported",
                       "not_applied");
    }
    var selected = chord.notes[params.noteIndex];
    var writtenTpc = params.tpc + selected.tpc2 - selected.tpc1;
    if (!validTpc(writtenTpc)) {
        return failure(params, "unsupported_spelling",
                       "Requested spelling would put written TPC outside the native range",
                       "not_applied");
    }
    for (var peerIndex = 0; peerIndex < chord.notes.length; peerIndex++) {
        if (peerIndex !== params.noteIndex && chord.notes[peerIndex].pitch === params.pitch) {
            return failure(params, "unsupported_target",
                           "Duplicate sounding chord tones are not supported", "not_applied");
        }
    }
    var displayedTpc = selected.tpc === selected.tpc1 ? params.tpc : writtenTpc;
    if (selected.pitch === params.pitch && selected.tpc1 === params.tpc
            && selected.tpc2 === writtenTpc) {
        return result(params, before, describe(plugin, resolved, params), params.noteIndex,
                      "unchanged");
    }
    var score = plugin.curScore;
    var started = false;
    var committed = false;
    try {
        score.startCmd("setNotePitch");
        started = true;
        selected.pitch = params.pitch;
        selected.tpc1 = params.tpc;
        selected.tpc2 = writtenTpc;
        var after = describe(plugin, resolved, params);
        if (!matchesEdit(before, after, params.noteIndex, params.pitch, params.tpc,
                         writtenTpc, displayedTpc)) {
            throw new Error("Native setters did not produce the requested isolated pitch edit");
        }
        // Once commit is attempted its undo scope is no longer known to be open.
        // A commit exception must not cancel an unrelated command.
        started = false;
        score.endCmd(false);
        committed = true;
        // Verify again after the undo command closes, using native live data.
        after = describe(plugin, resolved, params);
        var afterIndex = noteIndex(chord, selected);
        if (afterIndex < 0 || selected.pitch !== params.pitch || selected.tpc1 !== params.tpc
                || selected.tpc2 !== writtenTpc
                || !matchesEdit(before, after, params.noteIndex, params.pitch, params.tpc,
                                writtenTpc, displayedTpc)) {
            return failure(params, "verification_failed",
                           "Pitch edit committed but final native verification failed; inspect before undoing or retrying",
                           "applied_unverified", { before: before, after: after });
        }
        return result(params, before, after, afterIndex, "applied");
    } catch (error) {
        var status = committed ? "applied_unverified" : "unknown";
        var restored = null;
        var cancelled = false;
        try {
            if (started) {
                started = false;
                score.endCmd(true);
                cancelled = true;
            }
            restored = describe(plugin, resolved, params);
            if (cancelled && eventKey(restored, true) === eventKey(before, true)) {
                status = "rolled_back";
            }
        } catch (rollbackError) {
            if (!committed) status = "unknown";
        }
        return failure(params, "verification_failed", String(error), status,
                       { before: before, actual: restored });
    }
}
