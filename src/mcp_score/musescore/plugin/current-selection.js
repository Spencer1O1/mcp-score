// Read only the application's native selected list; never reconstruct a range.
.pragma library
.import "constants.js" as Constants
.import "score.js" as Score
.import "passage.js" as Passage

var maxSelectedElements = 1000;
var maxMeasuresScanned = 10000;
var maxParentDepth = 32;

function same(left, right) {
    return left && right && typeof left.is === "function" && left.is(right);
}

function readCurrentSelection(plugin, params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return scoreError;
    var selection = plugin.curScore.selection;
    if (!selection || typeof selection.isRange !== "boolean"
            || !selection.elements) return {error: "Native selection is unavailable"};
    var nativeElements = selection.elements;
    if (!Passage.integer(nativeElements.length) || nativeElements.length < 0) {
        return {error: "Native selected-element list is unavailable"};
    }
    if (nativeElements.length > maxSelectedElements) {
        return {error: "Selection exceeds element limit; select fewer elements"};
    }
    var range = null;
    if (selection.isRange) {
        if (!selection.startSegment) return {error: "Native range start is unavailable"};
        range = {
            startTick: selection.startSegment.tick,
            endTick: selection.endSegment ? selection.endSegment.tick : null,
            startStaff: selection.startStaff,
            endStaffExclusive: selection.endStaff
        };
    }
    var elements = [];
    var locations = [];
    var voices = [];
    var unsupported = Passage.unsupportedNotation.slice();
    unsupported.push("native selection filter settings (selected list is used as exposed)");
    unsupported.push("opaque selected types and grace-note positions may lack content or addresses");
    for (var index = 0; index < nativeElements.length; index++) {
        var element = nativeElements[index];
        var isNote = element.type === plugin.Element.NOTE;
        var isChord = element.type === plugin.Element.CHORD;
        var isRest = element.type === plugin.Element.REST;
        var track = Passage.integer(element.track) && element.track >= 0
            && element.track < plugin.curScore.nstaves * Constants.voicesPerStaff
            ? element.track : null;
        if (track !== null && voices.indexOf(track % Constants.voicesPerStaff) < 0) {
            voices.push(track % Constants.voicesPerStaff);
        }
        var item = {
            kind: isNote ? "note" : (isChord ? "chord" : (isRest ? "rest" : "other")),
            type: element.type,
            name: typeof element._name === "function" ? element._name() : null,
            text: typeof element.text === "string" ? element.text : null,
            track: track, measure: null, eventContext: null, noteIndex: null, note: null
        };
        elements.push(item);
        var owner = isNote ? element.parent : element;
        var segment = owner;
        for (var depth = 0; segment && depth < maxParentDepth; depth++) {
            if (segment.type === plugin.Element.SEGMENT) break;
            segment = segment.parent;
        }
        if (!segment || segment.type !== plugin.Element.SEGMENT || track === null) continue;
        // Grace chords have a main chord parent; do not alias their onset to it.
        if ((isNote || isChord || isRest)
                && !same(segment.elementAt(track), owner)) continue;
        var noteIndex = null;
        if (isNote) {
            if (!owner || owner.type !== plugin.Element.CHORD) continue;
            for (var tone = 0; tone < owner.notes.length; tone++) {
                if (same(owner.notes[tone], element)) { noteIndex = tone; break; }
            }
            if (noteIndex === null) continue;
        }
        locations.push({item: item, owner: owner, segment: segment, noteIndex: noteIndex});
    }
    // Resolve ordinal measures using native parent identity, not title/path/tick guesses.
    var remaining = locations.length;
    var measure = remaining ? plugin.curScore.firstMeasure : null;
    var ordinal = 1;
    while (measure && remaining) {
        if (ordinal > maxMeasuresScanned) {
            return {error: "Selection exceeds measure scan limit; no partial result returned"};
        }
        for (var locationIndex = 0; locationIndex < locations.length; locationIndex++) {
            var location = locations[locationIndex];
            if (location.item.measure !== null || !same(measure, location.segment.parent)) continue;
            if (!measure.firstSegment) continue;
            var event = Passage.describeEvent(plugin, location.owner,
                Math.floor(location.item.track / Constants.voicesPerStaff),
                location.item.track % Constants.voicesPerStaff,
                location.segment.tick, measure.firstSegment.tick);
            location.item.measure = ordinal;
            location.item.eventContext = event;
            location.item.noteIndex = location.noteIndex;
            location.item.note = location.noteIndex === null ? null : event.notes[location.noteIndex];
            Passage.includeEventUnsupported(unsupported, event);
            remaining--;
        }
        measure = remaining ? measure.nextMeasure : null;
        ordinal++;
    }
    return {result: {
        source: "live", kind: selection.isRange ? "range" : (elements.length ? "list" : "none"),
        range: range, elements: elements, elementsComplete: true,
        observedVoices: voices.sort(), filterState: "not_exposed", unsupported: unsupported
    }};
}
