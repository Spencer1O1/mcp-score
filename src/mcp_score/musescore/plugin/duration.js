// Native changeCRlen only: shorten one ordinary chord and fill released time.
.pragma library
.import "constants.js" as Constants
.import "passage.js" as Passage
.import "pitch.js" as Pitch
.import "score.js" as Score

var limitations = [
    "whole target-voice measure snapshot; no score identity or global revision",
    "identical target contents in another score cannot be distinguished",
    "MuseScore 4.7+ in major version 4 only; earlier native metadata is insufficient",
    "shorten_fill_rest only; no extension, bar crossing or insertion fallback",
    "target attachments, beams, ties, tuplets, grace notes and intersecting/unknown spanners are unsupported",
    "UI selection and layout may change; logical cursor is untouched",
    "verification covers exposed measure events, not every engraving property"
];

function key(event) {
    return JSON.stringify([Pitch.eventKey(event, true), event.lyrics, event.articulations]);
}
function keys(events) { return events.map(key).sort().join("\n"); }
function measureKey(measure) {
    return JSON.stringify([measure.tick,measure.timeSignature,measure.actualDuration,
                           keys(measure.events)]);
}
function voiceEvents(events, params) {
    return events.filter(function(event) {
        return event.staff === params.staff && event.voice === params.voice;
    });
}
function read(plugin, params) {
    var response = Passage.readPassage(plugin, {startMeasure:params.measure,
                                               endMeasure:params.measure});
    if (response.error) throw new Error(response.error);
    return response.result.measures[0];
}
// SPANNER_TICKS is native m_ticks (length), despite the API header's end-tick wording.
// Conservatively include both endpoints, across all staves and system spanners.
function spanners(plugin, onset, end) {
    var state = [];
    var spans = plugin.curScore.spanners;
    for (var index = 0; index < spans.length; index++) {
        var span = spans[index];
        var start = ticks(span.spannerTick);
        var length = ticks(span.spannerTicks);
        if (start === null || length === null || !Pitch.integer(start + length)
                || !Pitch.integer(span.type) || !Pitch.integer(span.track)
                || !Pitch.integer(span.spannerTrack2) || !Pitch.integer(span.anchor)) {
            throw new Error("Unknown or unlocatable native spanner metadata");
        }
        if (onset !== undefined && start <= end && start + length >= onset) {
            throw new Error("A score spanner intersects the affected time interval");
        }
        state.push(JSON.stringify([span.type,start,length,span.track,span.spannerTrack2,span.anchor]));
    }
    return state.sort().join("\n");
}
function failure(params, code, message, status, extra) {
    var reply = {error:message, code:code, mutation_status:status, verified:false,
        target:{measure:params.measure,staff:params.staff,voice:params.voice,offset:params.offset},
        snapshot_scope:"target_voice_measure",
        selection_status:status === "not_applied" ? "unchanged" : "not_preserved",
        limitations:limitations.slice()};
    Object.keys(extra || {}).forEach(function(name) { reply[name] = extra[name]; });
    return reply;
}
function empty(list) { return list && list.length === 0; }
function ticks(fraction) {
    if (!fraction || !Pitch.integer(fraction.numerator) || !Pitch.integer(fraction.denominator)
            || fraction.numerator < 0 || fraction.denominator <= 0) return null;
    var scaled = fraction.numerator * Constants.ticksPerWholeNote;
    return Pitch.integer(scaled) && scaled % fraction.denominator === 0
        ? scaled / fraction.denominator : null;
}
// Single undotted binary values. Native decomposition must never create tied notes.
function canonical(value) {
    return [1920,960,480,240,120,60].indexOf(value) >= 0;
}
function eligible(plugin, chord) {
    if (!Pitch.supported(plugin, chord) || chord.staffMove !== 0 || chord.beam !== null
            || chord.arpeggio !== null || chord.spanArpeggio !== null
            || chord.tremoloSingleChord !== null || chord.tremoloTwoChord !== null
            || !empty(chord.elements) || !empty(chord.lyrics) || !empty(chord.articulations)
            || chord.isTrillCueNote !== false) return false;
    for (var index = 0; index < chord.notes.length; index++) {
        var note = chord.notes[index];
        if (!empty(note.elements) || !empty(note.spannerForward) || !empty(note.spannerBack)
                || note.isTrillCueNote !== false) return false;
    }
    return true;
}
function verify(before, after, params, onset, oldTicks, newTicks) {
    var targetBefore = before.events.filter(function(event) {
        return event.staff === params.staff && event.voice === params.voice
            && event.kind === "chord" && event.tick === onset;
    });
    var targetAfter = after.events.filter(function(event) {
        return event.staff === params.staff && event.voice === params.voice
            && event.kind === "chord" && event.tick === onset;
    });
    if (targetBefore.length !== 1 || targetAfter.length !== 1) return false;
    var expectedTarget = JSON.parse(JSON.stringify(targetBefore[0]));
    expectedTarget.duration = Passage.fraction(newTicks, Constants.ticksPerWholeNote);
    expectedTarget.actualDuration = expectedTarget.duration;
    if (key(expectedTarget) !== key(targetAfter[0])) return false;
    var fill = after.events.filter(function(event) {
        return event.staff === params.staff && event.voice === params.voice
            && event.tick >= onset + newTicks && event.tick < onset + oldTicks;
    });
    var covered = onset + newTicks;
    for (var index = 0; index < fill.length; index++) {
        var rest = fill[index];
        var length = ticks(rest.actualDuration);
        if (rest.kind !== "rest" || rest.tick !== covered || !length || ticks(rest.duration) !== length
                || !empty(rest.lyrics) || rest.notes.length !== 0) return false;
        covered += length;
    }
    if (covered !== onset + oldTicks) return false;
    var inside = voiceEvents(after.events,params).filter(function(event) {
        return event.kind !== "other" && event.tick >= onset && event.tick < onset + oldTicks;
    });
    if (inside.length !== fill.length + 1) return false;
    function outside(event) {
        return event.kind === "other" || !(event.staff === params.staff && event.voice === params.voice
            && event.tick >= onset && event.tick < onset + oldTicks);
    }
    return keys(before.events.filter(outside)) === keys(after.events.filter(outside))
        && JSON.stringify([before.tick,before.timeSignature,before.actualDuration])
            === JSON.stringify([after.tick,after.timeSignature,after.actualDuration]);
}
function success(params, before, after, status) {
    return {result:{measure:params.measure,staff:params.staff,voice:params.voice,
        before:before.events,after:after.events,verified:true,mutationStatus:status,
        snapshotScope:"target_voice_measure",
        selectionStatus:status === "unchanged" ? "unchanged" : "not_preserved",
        limitations:limitations.slice()}};
}
function setEventDuration(plugin, params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return failure(params,"target_unavailable",scoreError.error,"not_applied");
    if (plugin.mscoreMajorVersion !== 4 || !Pitch.integer(plugin.mscoreMinorVersion)
            || plugin.mscoreMinorVersion < 7 || typeof plugin.fraction !== "function"
            || !plugin.curScore.spanners || !Pitch.integer(plugin.curScore.spanners.length)) {
        return failure(params,"unsupported_native_api",
            "Requires MuseScore 4.7+ native duration setter and spanner metadata", "not_applied");
    }
    if (!Pitch.integer(params.measure) || params.measure < 1 || !Pitch.integer(params.staff)
            || params.staff < 0 || params.staff >= plugin.curScore.nstaves
            || !Pitch.integer(params.voice) || params.voice < 0 || params.voice > 3
            || params.policy !== "shorten_fill_rest" || !Array.isArray(params.expected)
            || params.expected.length === 0 || ticks(params.offset) === null
            || !canonical(ticks(params.duration))) {
        return failure(params,"invalid_target","Invalid position, duration, policy or snapshot","not_applied");
    }
    var before;
    var resolved;
    var beforeSpanners;
    try {
        before = read(plugin,params);
        if (keys(voiceEvents(before.events,params)) !== keys(params.expected)) {
            return failure(params,"stale_target","Target voice measure differs from expected snapshot",
                           "not_applied",{actual:before.events});
        }
        resolved = Pitch.resolve(plugin,params);
        if (resolved.error) return failure(params,"target_unavailable",resolved.error,"not_applied");
        var oldTicks = ticks(resolved.chord.actualDuration);
        var newTicks = ticks(params.duration);
        if (!canonical(oldTicks) || ticks(resolved.chord.duration) !== oldTicks
                || newTicks > oldTicks || resolved.tick + oldTicks > before.tick + ticks(before.actualDuration)
                || !eligible(plugin,resolved.chord)) {
            return failure(params,"unsupported_target",
                "Only unadorned ordinary unbeamed chords may shorten within a bar",
                "not_applied");
        }
        try {
            beforeSpanners = spanners(plugin,resolved.tick,resolved.tick + oldTicks);
        } catch (spanError) {
            return failure(params,"unsupported_spanner",String(spanError),"not_applied");
        }
        // No other event in the target voice may occupy the released interval.
        if (voiceEvents(before.events,params).some(function(event) {
            return event.tick > resolved.tick && event.tick < resolved.tick + oldTicks;
        })) return failure(params,"unsupported_target","Affected interval contains other events","not_applied");
        if (newTicks === oldTicks) return success(params,before,read(plugin,params),"unchanged");
    } catch (error) {
        return failure(params,"invalid_snapshot",String(error),"not_applied");
    }
    var score = plugin.curScore;
    var started = false;
    var committed = false;
    try {
        score.startCmd("setEventDuration");
        started = true;
        resolved.chord.duration = plugin.fraction(params.duration.numerator,params.duration.denominator);
        // Native rewriting can invalidate wrappers. Never reuse resolved.chord after this assignment.
        var after = read(plugin,params);
        if (!verify(before,after,params,resolved.tick,oldTicks,newTicks)
                || spanners(plugin) !== beforeSpanners) {
            throw new Error("Native duration edit failed complete measure verification");
        }
        started = false;
        score.endCmd(false);
        committed = true;
        after = read(plugin,params);
        if (!verify(before,after,params,resolved.tick,oldTicks,newTicks)
                || spanners(plugin) !== beforeSpanners) {
            return failure(params,"verification_failed","Committed duration edit failed final verification",
                           "applied_unverified",{before:before.events,actual:after.events});
        }
        return success(params,before,after,"applied");
    } catch (error) {
        var status = committed ? "applied_unverified" : "unknown";
        var actual = null;
        var cancelled = false;
        try {
            if (started) { started = false; score.endCmd(true); cancelled = true; }
            actual = read(plugin,params);
            if (cancelled && measureKey(before) === measureKey(actual)
                    && spanners(plugin) === beforeSpanners) status = "rolled_back";
        } catch (rollbackError) { if (!committed) status = "unknown"; }
        return failure(params,"verification_failed",String(error),status,
                       {before:before.events,actual:actual ? actual.events : null});
    }
}
