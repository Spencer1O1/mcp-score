// Independent chord targets: complete preflight, one native command, no navigation.
.pragma library
.import "pitch.js" as Pitch
.import "score.js" as Score
.import "passage.js" as Passage

var maxTargets = 32;
var limitations = [
    "target-chord snapshots only; no whole-score identity, revision or non-target verification",
    "expected attachment metadata is informational; fresh exposed metadata is preserved during writes",
    "native indices and wrappers belong only to this synchronous handler",
    "written octave and unexposed notation/layout/undo-stack details are not verified"
];

function failure(params, code, message, status, index, before, actual) {
    return {error: message, code: code, mutation_status: status, verified: false,
        snapshot_scope: "target_chords", failed_index: index,
        targets: Array.isArray(params.edits) ? params.edits.slice(0, maxTargets).map(function(edit) {
            return edit && typeof edit === "object" ? Pitch.target(edit) : null;
        }) : [], before: before, actual: actual,
        rollback_verified: status === "rolled_back", limitations: limitations.slice()};
}

function metadataKey(event) {
    var metadata = {};
    Object.keys(event).forEach(function(key) {
        if (key !== "notes") metadata[key] = event[key];
    });
    return JSON.stringify(metadata);
}

// Preserve every exposed note field, except the intended selected pitch/spelling.
function matchesPlan(plan, after) {
    var selected = plan.selected;
    if (Pitch.noteIndex(plan.chord, selected) < 0
            || selected.pitch !== plan.params.pitch || selected.tpc1 !== plan.params.tpc
            || selected.tpc2 !== plan.writtenTpc
            || metadataKey(plan.before) !== metadataKey(after)
            || !Pitch.matchesEdit(plan.before, after, plan.params.noteIndex,
                                 plan.params.pitch, plan.params.tpc,
                                 plan.writtenTpc, plan.displayedTpc)) return false;
    var expected = JSON.parse(JSON.stringify(plan.before.notes));
    var note = expected[plan.params.noteIndex];
    note.pitch = plan.params.pitch;
    note.tpc = plan.params.tpc;
    note.writtenTpc = plan.writtenTpc;
    note.displayedTpc = plan.displayedTpc;
    note.name = Passage.concertName(plan.params.pitch, plan.params.tpc);
    var expectedKeys = expected.map(function(value) { return JSON.stringify(value); }).sort();
    var actualKeys = after.notes.map(function(value) { return JSON.stringify(value); }).sort();
    return JSON.stringify(expectedKeys) === JSON.stringify(actualKeys);
}

function readAll(plugin, plans) {
    return plans.map(function(plan) { return Pitch.describe(plugin, plan.resolved, plan.params); });
}

function verifiedResult(plugin, plans, after, status) {
    var edits = [];
    for (var index = 0; index < plans.length; index++) {
        var plan = plans[index];
        if (!matchesPlan(plan, after[index])) throw Error("Target pitch or exposed metadata verification failed at " + index);
        edits.push(Pitch.result(plan.params, plan.before, after[index],
            Pitch.noteIndex(plan.chord, plan.selected), plan.changed ? "applied" : "unchanged").result);
    }
    return {result: {edits: edits, verified: true, mutationStatus: status,
        snapshotScope: "target_chords", limitations: limitations.slice()}};
}

function setNotePitches(plugin, params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return failure(params, "target_unavailable", scoreError.error,
                                   "not_applied", null, [], null);
    if (!Array.isArray(params.edits) || !params.edits.length || params.edits.length > maxTargets) {
        return failure(params, "invalid_batch", "A batch requires 1 through 32 distinct chord targets",
                       "not_applied", null, [], null);
    }
    var plans = [];
    var before = [];
    var changed = false;
    var index = 0;
    try {
        for (; index < params.edits.length; index++) {
            var plan = Pitch.planPitchEdit(plugin, params.edits[index]);
            if (plan.error) return failure(params, plan.code, plan.error, "not_applied",
                                           index, before, plan.actual || null);
            for (var peer = 0; peer < plans.length; peer++) {
                if (plans[peer].chord.is(plan.chord)) {
                    return failure(params, "duplicate_chord", "Batch targets must belong to distinct native chords",
                                   "not_applied", index, before, null);
                }
            }
            plans.push(plan);
            before.push(plan.before);
            changed = changed || plan.changed;
        }
        var ready = readAll(plugin, plans);
        for (index = 0; index < plans.length; index++) {
            if (JSON.stringify(ready[index]) !== JSON.stringify(before[index])
                    || !Pitch.supported(plugin, plans[index].chord)
                    || Pitch.noteIndex(plans[index].chord, plans[index].selected)
                        !== plans[index].params.noteIndex) {
                return failure(params, "preflight_changed", "Target changed during batch preflight",
                               "not_applied", index, before, ready);
            }
        }
        if (!changed) return verifiedResult(plugin, plans, ready, "unchanged");
    } catch (preflightError) {
        return failure(params, "preflight_failed", String(preflightError), "not_applied",
                       index, before, null);
    }
    var score = plugin.curScore;
    var started = false;
    var committed = false;
    var after = null;
    try {
        score.startCmd("setNotePitches");
        started = true;
        for (index = 0; index < plans.length; index++) {
            if (plans[index].changed) {
                Pitch.applyPitchPlan(plans[index]);
            }
        }
        after = readAll(plugin, plans);
        verifiedResult(plugin, plans, after, "applied");
        // A commit attempt revokes rollback authority, even if it throws.
        started = false;
        score.endCmd(false);
        committed = true;
        after = readAll(plugin, plans);
        return verifiedResult(plugin, plans, after, "applied");
    } catch (error) {
        var status = committed ? "applied_unverified" : "unknown";
        var actual = null;
        var cancelled = false;
        try {
            if (started) {
                started = false;
                score.endCmd(true);
                cancelled = true;
            }
            actual = readAll(plugin, plans);
            if (cancelled && JSON.stringify(actual) === JSON.stringify(before)) status = "rolled_back";
        } catch (restoreError) {
            if (!committed) status = "unknown";
        }
        return failure(params, "verification_failed", String(error), status,
                       index < plans.length ? index : null, before, actual);
    }
}
