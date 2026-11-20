// Live score traversal. No selections, undo steps, or logical cursor updates.
.pragma library
.import "constants.js" as Constants
.import "score.js" as Score

var maxMeasuresPerRead = 64;
var maxEventsPerRead = 10000;

// This first reader exposes rhythmic contents and opaque segment elements.
// Completeness of traversal is deliberately separate from notation fidelity.
var unsupportedNotation = [
    "written pitch and octave for transposing instruments (concert pitch is returned)",
    "grace-note contents",
    "tie and spanner endpoints",
    "chord articulations when the native list is unavailable (null, before MuseScore 4.6)",
    "note-attached symbols, fingerings and other non-articulation child elements",
    "lyric melisma endpoints, displayed verse labels and rich-text interpretation",
    "tuplet grouping (notated and elapsed durations are returned)",
    "effective instrument, clef, key and tempo context",
    "displayed measure numbering and persistent event identifiers",
    "measure-attached elements outside segments",
    "system annotations without a staff assignment"
];

function integer(value) {
    return typeof value === "number" && isFinite(value) && Math.floor(value) === value;
}

/// Reduced exact whole-note fraction, including a zero onset.
function fraction(numerator, denominator) {
    var left = Math.abs(numerator);
    var right = Math.abs(denominator);
    while (right) {
        var remainder = left % right;
        left = right;
        right = remainder;
    }
    var divisor = left || 1;
    return { numerator: numerator / divisor, denominator: denominator / divisor };
}

function duration(value) {
    if (!value || !integer(value.numerator) || !integer(value.denominator)
            || value.denominator <= 0) return null;
    return fraction(value.numerator, value.denominator);
}

/// MuseScore tpc1 uses the circle of fifths, with F natural at 13.
/// The octave follows the spelling, including Cb and B# across octave boundaries.
function concertName(pitch, tpc) {
    var letters = ["F", "C", "G", "D", "A", "E", "B"];
    var semitones = [5, 0, 7, 2, 9, 4, 11];
    var fifths = tpc - 13;
    var index = ((fifths % 7) + 7) % 7;
    var accidental = Math.floor(fifths / 7);
    var suffix = "";
    for (var i = 0; i < Math.abs(accidental); i++) {
        suffix += accidental > 0 ? "#" : "b";
    }
    var octave = (pitch - semitones[index] - accidental) / 12 - 1;
    return letters[index] + suffix + octave;
}

function enumName(values, names, value) {
    if (!values || !integer(value)) return null;
    for (var index = 0; index < names.length; index++) {
        if (values[names[index]] === value) return names[index];
    }
    return null;
}

// ChordRest.lyrics is a native read-only list on supported MuseScore versions.
// Missing metadata stays null; unknown enum values keep their native code.
function describeLyrics(plugin, element) {
    if (!element.lyrics || !integer(element.lyrics.length)) return null;
    var lyrics = [];
    for (var index = 0; index < element.lyrics.length; index++) {
        var lyric = element.lyrics[index];
        lyrics.push({
            text: typeof lyric.text === "string" ? lyric.text : null,
            verse: integer(lyric.verse) ? lyric.verse : null,
            syllabic: integer(lyric.syllabic) ? lyric.syllabic : null,
            syllabicName: enumName(plugin.Lyrics,
                                  ["SINGLE", "BEGIN", "END", "MIDDLE"], lyric.syllabic),
            placement: integer(lyric.placement) ? lyric.placement : null,
            placementName: enumName(plugin.Placement, ["ABOVE", "BELOW"], lyric.placement)
        });
    }
    return lyrics;
}

// Chord.articulations was added to the native wrapper in MuseScore 4.6.
function describeArticulations(plugin, element) {
    if (!element.articulations || !integer(element.articulations.length)) return null;
    var articulations = [];
    for (var index = 0; index < element.articulations.length; index++) {
        var articulation = element.articulations[index];
        articulations.push({
            symbol: integer(articulation.symbol) ? articulation.symbol : null,
            name: typeof articulation.subtypeName === "function"
                ? articulation.subtypeName() : null,
            placement: integer(articulation.placement) ? articulation.placement : null,
            placementName: enumName(plugin.Placement, ["ABOVE", "BELOW"],
                                    articulation.placement),
            anchor: integer(articulation.articulationAnchor)
                ? articulation.articulationAnchor : null,
            direction: integer(articulation.direction) ? articulation.direction : null
        });
    }
    return articulations;
}

function describeEvent(plugin, element, staff, voice, tick, measureTick) {
    var isChord = element.type === plugin.Element.CHORD;
    var isRest = element.type === plugin.Element.REST;
    var rhythmic = isChord || isRest;
    var notes = [];
    if (isChord) {
        for (var noteIndex = 0; noteIndex < element.notes.length; noteIndex++) {
            var note = element.notes[noteIndex];
            notes.push({
                pitch: note.pitch,
                tpc: note.tpc1,
                name: concertName(note.pitch, note.tpc1),
                displayedTpc: note.tpc
            });
        }
    }
    return {
        kind: isChord ? "chord" : (isRest ? "rest" : "other"),
        type: element.type,
        staff: staff,
        voice: voice,
        tick: tick,
        offset: fraction(tick - measureTick, Constants.ticksPerWholeNote),
        duration: rhythmic ? duration(element.duration) : null,
        actualDuration: rhythmic ? duration(element.actualDuration) : null,
        notes: notes,
        text: typeof element.text === "string" ? element.text : null,
        lyrics: rhythmic ? describeLyrics(plugin, element) : null,
        articulations: isChord ? describeArticulations(plugin, element) : null
    };
}

function indices(count, selected) {
    if (selected !== null && selected !== undefined) return [selected];
    var result = [];
    for (var index = 0; index < count; index++) result.push(index);
    return result;
}

function readPassage(plugin, params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return scoreError;
    var start = params.startMeasure;
    var end = params.endMeasure;
    if (!integer(start) || !integer(end) || start < 1 || end < start) {
        return { error: "Invalid inclusive measure range" };
    }
    if (end - start + 1 > maxMeasuresPerRead) {
        return { error: "Passage exceeds measure limit; request a smaller range" };
    }
    var score = plugin.curScore;
    var staff = params.staff;
    var voice = params.voice;
    if (staff !== null && staff !== undefined
            && (!integer(staff) || staff < 0 || staff >= score.nstaves)) {
        return { error: "Staff out of range" };
    }
    if (voice !== null && voice !== undefined
            && (!integer(voice) || voice < 0 || voice >= Constants.voicesPerStaff)) {
        return { error: "Voice out of range" };
    }
    var staves = indices(score.nstaves, staff);
    var voices = indices(Constants.voicesPerStaff, voice);
    var measure = score.firstMeasure;
    var measureNumber = 1;
    while (measure && measureNumber < start) {
        measure = measure.nextMeasure;
        measureNumber++;
    }
    var measures = [];
    var eventCount = 0;
    for (; measureNumber <= end; measureNumber++) {
        if (!measure) return { error: "Measure out of range: " + measureNumber };
        if (!measure.firstSegment) {
            return { error: "Measure has no readable segments: " + measureNumber };
        }
        var measureTick = measure.firstSegment.tick;
        var events = [];
        var segment = measure.firstSegment;
        while (segment) {
            for (var staffIndex = 0; staffIndex < staves.length; staffIndex++) {
                for (var voiceIndex = 0; voiceIndex < voices.length; voiceIndex++) {
                    var currentStaff = staves[staffIndex];
                    var currentVoice = voices[voiceIndex];
                    var track = currentStaff * Constants.voicesPerStaff + currentVoice;
                    var element = segment.elementAt(track);
                    if (element) {
                        events.push(describeEvent(plugin, element, currentStaff,
                                                  currentVoice, segment.tick, measureTick));
                    }
                }
            }
            for (var annotationIndex = 0; annotationIndex < segment.annotations.length;
                    annotationIndex++) {
                var annotation = segment.annotations[annotationIndex];
                var annotationStaff = Math.floor(annotation.track / Constants.voicesPerStaff);
                var annotationVoice = annotation.track % Constants.voicesPerStaff;
                if (staves.indexOf(annotationStaff) >= 0 && voices.indexOf(annotationVoice) >= 0) {
                    events.push(describeEvent(plugin, annotation, annotationStaff,
                                              annotationVoice, segment.tick, measureTick));
                }
            }
            if (eventCount + events.length > maxEventsPerRead) {
                return { error: "Passage exceeds event limit; request a smaller range" };
            }
            segment = segment.nextInMeasure;
        }
        eventCount += events.length;
        measures.push({
            measure: measureNumber,
            tick: measureTick,
            actualDuration: duration(measure.timesigActual),
            timeSignature: measure.timesigNominal ? {
                numerator: measure.timesigNominal.numerator,
                denominator: measure.timesigNominal.denominator
            } : null,
            events: events
        });
        measure = measure.nextMeasure;
    }
    return {
        result: {
            source: "live",
            startMeasure: start,
            endMeasure: end,
            staves: staves,
            voices: voices,
            measures: measures,
            rangeComplete: true,
            unsupported: unsupportedNotation.slice()
        }
    };
}
