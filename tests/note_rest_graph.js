const vm = require("node:vm"),
    modules = {};
for (const name of names)
    modules[name] = vm.runInNewContext(sources[name] + "\nthis;", {
        ...modules,
    });
const { Passage, Rhythm, NoteRestFixture } = modules;
const frac = (numerator, denominator) => ({
    numerator,
    denominator,
    ticks: (1920 * numerator) / denominator,
});
const controls = {
    ignore: false,
    damage: false,
    badCursor: false,
    failWrite: false,
    postCommit: null,
};
let starts = 0,
    cancels = 0,
    writes = 0;
const history = ["prior"];
const part = {
    hasPitchedStaff: true,
    hasDrumStaff: false,
    hasTabStaff: false,
    instrumentAtTick: () => ({
        instrumentId: "clarinet",
        musicXmlId: "wind.reed.clarinet",
        longName: "Clarinet",
        shortName: "Cl.",
    }),
};
const interval = { chromatic: -2, diatonic: -1 };
const staff = {
    part,
    transpose: () => interval,
    timeStretch: () => frac(1, 1),
    clefType: () => 0,
    key: () => 0,
    timeSig: () => ({ timesig: frac(4, 4) }),
};
const native = {
    track: 0,
    tuplet: null,
    topTuplet: null,
    staffMove: 0,
    beam: null,
    gap: false,
    visible: true,
    generated: false,
    elements: [],
    lyrics: [],
    isFullMeasureRest: false,
    staff,
    is(other) {
        return this === other;
    },
};
function tone(pitch = 60, tpc1 = 14, tpc2 = 16) {
    return {
        pitch,
        tpc1,
        tpc2,
        get tpc() {
            return concert ? this.tpc1 : this.tpc2;
        },
        noteType: 0,
        tieBack: null,
        tieForward: null,
        tuning: 0,
        accidentalType: 0,
        accidental: null,
        elements: [],
        spannerForward: [],
        spannerBack: [],
        isTrillCueNote: false,
    };
}
function rest(length, full = false) {
    return {
        ...native,
        type: 25,
        duration: frac(length, 1920),
        actualDuration: frac(length, 1920),
        isFullMeasureRest: full,
    };
}
function chord(length, pitch = 60, tpc1 = 14, tpc2 = 16) {
    return {
        ...native,
        type: 93,
        duration: frac(length, 1920),
        actualDuration: frac(length, 1920),
        notes: [tone(pitch, tpc1, tpc2)],
        noteType: 0,
        graceNotes: [],
        arpeggio: null,
        spanArpeggio: null,
        tremoloSingleChord: null,
        tremoloTwoChord: null,
        articulations: [],
        isTrillCueNote: false,
    };
}
let concert = false;
let current = rest(1920, true),
    suffix = [];
const peer = chord(1920, 64, 18, 20);
peer.track = 4;
const nextPeer = chord(1920, 67, 15, 17);
let segments = [];
const nextMeasure = {
    firstSegment: null,
    nextMeasure: null,
    tick: frac(1, 1),
    timesigActual: frac(1, 1),
    timesigNominal: frac(4, 4),
};
const measure = {
    firstSegment: null,
    nextMeasure: nextMeasure,
    tick: frac(0, 1),
    timesigActual: frac(1, 1),
    timesigNominal: frac(4, 4),
};
function segment(tick, element, second = null) {
    return {
        tick,
        fraction: frac(tick, 1920),
        annotations: [],
        nextInMeasure: null,
        elementAt: (track) =>
            track === 0 ? element : track === 4 ? second : null,
        is(other) {
            return this === other;
        },
    };
}
nextMeasure.firstSegment = segment(1920, nextPeer);
function rebuild() {
    segments = [
        segment(0, current, peer),
        ...suffix.map((s) => segment(s.tick, s.element)),
    ];
    segments.forEach((s, i) => (s.nextInMeasure = segments[i + 1] || null));
    measure.firstSegment = segments[0];
}
rebuild();
let saved;
const score = {
    firstMeasure: measure,
    nstaves: 2,
    nmeasures: 2,
    spanners: [],
    staves: [staff, staff],
    style: { value: () => concert },
    startCmd() {
        starts++;
        saved = { current, suffix, pitch: peer.notes[0].pitch };
    },
    endCmd(cancel) {
        if (cancel) {
            cancels++;
            current = saved.current;
            suffix = saved.suffix;
            peer.notes[0].pitch = saved.pitch;
            rebuild();
        } else {
            history.push("edit");
            if (controls.postCommit) controls.postCommit();
        }
    },
    newCursor() {
        return {
            inputStateMode: 0,
            filter: 0,
            track: 0,
            get staffIdx() {
                return Math.floor(this.track / 4);
            },
            get voice() {
                return this.track % 4;
            },
            rewindToFraction(f) {
                this.tick = f.ticks;
                this.segment = segments.find((s) => s.tick === f.ticks);
                this.element = this.segment?.elementAt(this.track);
                if (controls.badCursor) this.track = 4;
            },
            setDuration(n, d) {
                this.length = (1920 * n) / d;
            },
            addNote(pitch) {
                writes++;
                if (controls.failWrite) throw new Error("write failed");
                if (controls.ignore) return;
                const old = 1920;
                current = chord(this.length, pitch);
                suffix = [];
                if (this.length < old)
                    suffix = [
                        { tick: this.length, element: rest(old - this.length) },
                    ];
                if (controls.damage) peer.notes[0].pitch++;
                rebuild();
            },
            addRest() {
                writes++;
                if (controls.failWrite) throw new Error("write failed");
                if (controls.ignore) return;
                current = rest(this.length, this.length === 1920);
                suffix = [];
                if (controls.damage) peer.notes[0].pitch++;
                rebuild();
            },
        };
    },
};
const plugin = {
    curScore: score,
    Element: { CHORD: 93, REST: 25 },
    Segment: { ChordRest: 512 },
    NoteType: { NORMAL: 0 },
    Accidental: { NONE: 0 },
    mscoreMajorVersion: 4,
    mscoreMinorVersion: 7,
    fraction: frac,
    cursorMeasure: 2,
    cursorStaff: 1,
    cursorVoice: 3,
};
function snapshot() {
    return Passage.readPassage(plugin, {
        startMeasure: 1,
        endMeasure: 1,
    }).result.measures[0].events.filter((e) => e.staff === 0 && e.voice === 0);
}
const params = {
    measure: 1,
    staff: 0,
    voice: 0,
    offset: frac(0, 1),
    duration: frac(1, 4),
    pitch: 61,
    tpc: 21,
    expected: snapshot(),
};
let fill = true;
function run() {
    const reply = fill
        ? Rhythm.fillRestWithNote(plugin, params)
        : Rhythm.replaceNoteWithRest(plugin, params);
    return {
        reply,
        starts,
        cancels,
        writes,
        history,
        cursor: [plugin.cursorMeasure, plugin.cursorStaff, plugin.cursorVoice],
    };
}
