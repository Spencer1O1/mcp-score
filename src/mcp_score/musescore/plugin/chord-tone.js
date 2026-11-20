// Native topology changes with fresh reads; no removed wrappers survive a call.
.pragma library
.import "pitch.js" as Pitch
.import "passage.js" as Passage
.import "score.js" as Score

var limitations = [
    "exposed target-chord contents only; no score identity, global revision or non-target verification",
    "written octave and unexposed note children/engraving properties are not represented",
    "native indices belong only to the returned snapshots",
    "UI selection and layout are not preserved; logical cursor is untouched",
    "removal requires native note-child/spanner lists, unavailable before MuseScore 4.6"
];

// Object key order is irrelevant; attachment and snapshot-note order is not.
function stable(value) {
    if (value === null || typeof value !== "object") return JSON.stringify(value);
    if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
    return "{" + Object.keys(value).sort().map(function(key) {
        return JSON.stringify(key) + ":" + stable(value[key]);
    }).join(",") + "}";
}
function snapshotKey(event, notes) {
    return stable([Pitch.eventKey(event, notes),event.text,event.lyrics,event.articulations]);
}
function failure(params, operation, code, message, status, before, actual) {
    return {error:message,code:code,operation:operation,mutation_status:status,
        verified:false,target:{measure:params.measure,staff:params.staff,voice:params.voice,
            offset:params.offset},snapshot_scope:"target_chord",
        selection_status:status === "not_applied" ? "unchanged" : "not_preserved",
        rollback_verified:status === "rolled_back",before:before,actual:actual,
        limitations:limitations.slice()};
}
function empty(list) { return list && Pitch.integer(list.length) && list.length === 0; }
function read(plugin, params) {
    var resolved = Pitch.resolve(plugin, params);
    if (resolved.error) throw Error(resolved.error);
    return {resolved:resolved,event:Pitch.describe(plugin,resolved,params)};
}
function matchingNotes(expected, actual) {
    return stable(expected.map(stable).sort()) === stable(actual.map(stable).sort());
}
function verify(before, after, expectedNotes) {
    return snapshotKey(before,false) === snapshotKey(after,false)
        && matchingNotes(expectedNotes,after.notes);
}
function success(params, operation, before, after) {
    var index = null;
    if (operation === "add") {
        for (var i=0;i<after.notes.length;i++) if (after.notes[i].pitch === params.pitch) index=i;
        if (index === null) throw Error("Added tone is not present");
    }
    return {result:{operation:operation,measure:params.measure,
        noteIndexBefore:operation === "remove" ? params.noteIndex : null,
        noteIndexAfter:index,before:before,after:after,verified:true,
        mutationStatus:"applied",snapshotScope:"target_chord",selectionStatus:"not_preserved",
        limitations:limitations.slice()}};
}
function edit(plugin, params, operation) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return failure(params,operation,"target_unavailable",scoreError.error,"not_applied",null,null);
    var before = null;
    var resolved;
    var expectedNotes;
    var writtenTpc;
    try {
        if (!Pitch.integer(params.measure) || params.measure < 1 || !Pitch.integer(params.staff)
                || params.staff < 0 || params.staff >= plugin.curScore.nstaves
                || !Pitch.integer(params.voice) || params.voice < 0 || params.voice > 3
                || !params.offset || !Pitch.integer(params.offset.numerator)
                || params.offset.numerator < 0 || !Pitch.integer(params.offset.denominator)
                || params.offset.denominator <= 0) throw Error("Invalid exact target coordinates");
        var expected = params.expected;
        if (!expected || expected.kind !== "chord" || !Array.isArray(expected.notes)
                || !expected.notes.length || expected.notes.some(function(note) {
                    return !note || !Pitch.validTpc(note.writtenTpc);
                }) || expected.text === undefined || expected.lyrics === undefined
                || expected.articulations === undefined) throw Error("A complete fresh chord snapshot is required");
        var current = read(plugin,params);
        resolved = current.resolved;
        before = current.event;
        if (snapshotKey(before,true) !== snapshotKey(expected,true)) {
            return failure(params,operation,"stale_target","Target chord differs from expected snapshot","not_applied",before,before);
        }
        if (!Pitch.supported(plugin,resolved.chord)) throw Error("Only ordinary pitched chords are supported");
        expectedNotes = JSON.parse(JSON.stringify(before.notes));
        if (operation === "add") {
            if (typeof plugin.newElement !== "function" || typeof resolved.chord.add !== "function"
                    || !Pitch.integer(plugin.Element.NOTE)) throw Error("Native note creation API is unavailable");
            if (!Pitch.integer(params.pitch) || params.pitch < 0 || params.pitch > 127
                    || !Pitch.validTpc(params.tpc) || !Pitch.congruentPitch(params.pitch,params.tpc)) {
                throw Error("Invalid concert pitch or spelling");
            }
            var delta = resolved.chord.notes[0].tpc2 - resolved.chord.notes[0].tpc1;
            for (var peer=0;peer<resolved.chord.notes.length;peer++) {
                if (resolved.chord.notes[peer].pitch === params.pitch
                        || resolved.chord.notes[peer].tpc2 - resolved.chord.notes[peer].tpc1 !== delta) {
                    throw Error("Duplicate tone or inconsistent native transposition spelling");
                }
            }
            writtenTpc = params.tpc + delta;
            if (!Pitch.validTpc(writtenTpc)) throw Error("Written spelling is outside native TPC range");
            var displayedTpc = resolved.chord.notes[0].tpc === resolved.chord.notes[0].tpc1
                ? params.tpc : writtenTpc;
            expectedNotes.push({pitch:params.pitch,tpc:params.tpc,writtenTpc:writtenTpc,
                name:Passage.concertName(params.pitch,params.tpc),displayedTpc:displayedTpc});
        } else {
            if (!Pitch.integer(params.noteIndex) || params.noteIndex < 0
                    || params.noteIndex >= resolved.chord.notes.length) throw Error("Tone index out of range");
            if (resolved.chord.notes.length < 2) throw Error("Removing the final chord tone is unsupported");
            var selected = resolved.chord.notes[params.noteIndex];
            var removable = typeof resolved.chord.remove === "function" && empty(selected.elements)
                && empty(selected.spannerForward) && empty(selected.spannerBack);
            selected = null;
            if (!removable) throw Error("Removal requires empty native child/spanner lists (MuseScore 4.6+)");
            expectedNotes.splice(params.noteIndex,1);
        }
    } catch (preflightError) {
        return failure(params,operation,"unsupported_target",String(preflightError),"not_applied",before,null);
    }
    var score = plugin.curScore;
    var started = false;
    var committed = false;
    var added = null;
    try {
        score.startCmd(operation === "add" ? "addChordTone" : "removeChordTone");
        started = true;
        if (operation === "add") {
            added = plugin.newElement(plugin.Element.NOTE);
            if (!added) throw Error("Native note factory returned no note");
            added.pitch = params.pitch;
            resolved.chord.add(added);
            if (Pitch.noteIndex(resolved.chord,added) < 0) throw Error("Native adoption did not add the note");
            // Adoption recalculates TPC from native key/transposition, so spell afterward.
            added.tpc1 = params.tpc;
            added.tpc2 = writtenTpc;
            added = null;
        } else {
            resolved.chord.remove(resolved.chord.notes[params.noteIndex]);
        }
        // Topology may invalidate wrappers. Only coordinates and pure snapshots survive.
        resolved = null;
        var after = read(plugin,params).event;
        if (!verify(before,after,expectedNotes)) throw Error("Native chord-tone verification failed");
        started = false;
        score.endCmd(false);
        committed = true;
        after = read(plugin,params).event;
        if (!verify(before,after,expectedNotes)) throw Error("Committed chord-tone verification failed");
        return success(params,operation,before,after);
    } catch (error) {
        added = null;
        resolved = null;
        var status = committed ? "applied_unverified" : "unknown";
        var actual = null;
        var cancelled = false;
        try {
            if (started) { started = false; score.endCmd(true); cancelled = true; }
            actual = read(plugin,params).event;
            if (cancelled && stable(actual) === stable(before)) status="rolled_back";
        } catch (restoreError) { if (!committed) status="unknown"; }
        return failure(params,operation,"verification_failed",String(error),status,before,actual);
    }
}
function addChordTone(plugin, params) { return edit(plugin,params,"add"); }
function removeChordTone(plugin, params) { return edit(plugin,params,"remove"); }
