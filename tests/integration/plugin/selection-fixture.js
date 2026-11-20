// Disposable native test setup, never bundled with the production plugin.
.pragma library

function integer(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value;
}

function observe(plugin) {
    var selection = plugin.curScore.selection;
    var elements = [];
    for (var index = 0; index < selection.elements.length; index++) {
        var element = selection.elements[index];
        elements.push({type: element.type, track: element.track,
            pitch: element.type === plugin.Element.NOTE ? element.pitch : null});
    }
    return {result: {isRange: selection.isRange, elements: elements,
        startTick: selection.isRange ? selection.startSegment.tick : null,
        endTick: selection.isRange && selection.endSegment ? selection.endSegment.tick : null,
        startStaff: selection.isRange ? selection.startStaff : null,
        endStaffExclusive: selection.isRange ? selection.endStaff : null,
        cursor: [plugin.cursorMeasure, plugin.cursorStaff, plugin.cursorVoice],
        dirty: null, dirtyStateAvailable: false}};
}

function setup(plugin, params) {
    if (!plugin.curScore) return {error: "No test score"};
    if (params.mode === "observe") return observe(plugin);
    var selection = plugin.curScore.selection;
    if (params.mode === "clear") {
        if (!selection.clear()) return {error: "Native clear rejected"};
    } else if (params.mode === "range") {
        if (![params.startTick, params.endTick, params.startStaff, params.endStaffExclusive]
                .every(integer) || params.startTick < 0 || params.endTick > 10000
                || params.endTick <= params.startTick
                || params.startStaff < 0 || params.endStaffExclusive > plugin.curScore.nstaves
                || params.endStaffExclusive <= params.startStaff) return {error: "Invalid test range"};
        // This follows the existing selection setup tool's native update boundary.
        plugin.curScore.startCmd("integrationSelectionSetup");
        try {
            if (!selection.selectRange(params.startTick, params.endTick,
                    params.startStaff, params.endStaffExclusive)) throw Error("Native range rejected");
        } finally { plugin.curScore.endCmd(); }
    } else if (params.mode === "notes") {
        if (!Array.isArray(params.notes) || !params.notes.length || params.notes.length > 4) {
            return {error: "Invalid test note list"};
        }
        var notes = [];
        for (var index = 0; index < params.notes.length; index++) {
            var address = params.notes[index];
            if (![address.measure, address.track, address.tick, address.noteIndex].every(integer)
                    || address.measure < 1 || address.measure > 2 || address.track < 0
                    || address.track >= plugin.curScore.nstaves * 4 || address.tick < 0
                    || address.tick > 3840
                    || address.noteIndex < 0) return {error: "Invalid test note address"};
            var measure = plugin.curScore.firstMeasure;
            for (var ordinal = 1; measure && ordinal < address.measure; ordinal++) {
                measure = measure.nextMeasure;
            }
            var segment = measure ? measure.firstSegment : null;
            while (segment && segment.tick !== address.tick) segment = segment.nextInMeasure;
            var chord = segment ? segment.elementAt(address.track) : null;
            if (!chord || chord.type !== plugin.Element.CHORD
                    || address.noteIndex >= chord.notes.length) return {error: "Test note not found"};
            notes.push(chord.notes[address.noteIndex]);
        }
        if (!selection.clear()) return {error: "Native clear rejected"};
        for (var noteIndex = 0; noteIndex < notes.length; noteIndex++) {
            if (!selection.select(notes[noteIndex], noteIndex > 0)) return {error: "Native note rejected"};
        }
    } else return {error: "Unknown fixed test setup mode"};
    return observe(plugin);
}
