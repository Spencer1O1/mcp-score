// Direct native staff getters only: no cursor, selection, undo, or tempo reads.
.pragma library
.import "constants.js" as Constants
.import "passage.js" as Passage
.import "score.js" as Score

var maxMeasures = 64;
var maxMeasuresScanned = 10000;
var maxSegmentsScanned = 20000;
var maxQueries = 4096;
var maxExactInteger = 9007199254740991;
var limitations = [
    "sampling_complete means the bounded native start/segment samples were traversed, not full context fidelity",
    "changes without exposed native segment anchors are not enumerated",
    "effective tempo is not queried; cursor positioning can create anchors or change layout",
    "custom/atonal key notation and key mode are not serialized; native_key is the raw Staff.key value",
    "native_clef_type is the raw Staff.clefType value, not a guessed name or a written-pitch conversion",
    "full written pitch/octave, playback offsets and displayed measure labels remain unsupported",
    "no score-instance identity or revision token is established"
];
function integer(value) {
    return Passage.integer(value) && Math.abs(value) <= maxExactInteger;
}
function rational(value) {
    if (!value || !integer(value.numerator) || !integer(value.denominator)
            || value.denominator <= 0) throw new Error("Native fraction unavailable or invalid");
    return Passage.fraction(value.numerator,value.denominator);
}
function multiply(left,right) {
    var result = left * right;
    if (!integer(result)) throw new Error("Context fraction exceeds exact arithmetic bounds");
    return result;
}
function compare(left,right) {
    return multiply(left.numerator,right.denominator) - multiply(right.numerator,left.denominator);
}
function subtract(left,right) {
    var numerator = compare(left,right);
    if (!integer(numerator)) throw new Error("Context fraction exceeds exact arithmetic bounds");
    return Passage.fraction(numerator,multiply(left.denominator,right.denominator));
}
function add(left,right) {
    var numerator = multiply(left.numerator,right.denominator) + multiply(right.numerator,left.denominator);
    if (!integer(numerator)) throw new Error("Context fraction exceeds exact arithmetic bounds");
    return Passage.fraction(numerator,multiply(left.denominator,right.denominator));
}
function meter(value) {
    if (!value || !integer(value.numerator) || !integer(value.denominator)
            || value.numerator <= 0 || value.denominator <= 0) return null;
    // Meter spelling must not be reduced: 4/4 and 2/2 differ.
    return {numerator:value.numerator,denominator:value.denominator};
}
function unavailable(point,field,reason) {
    point.unavailableFields.push({field:field.replace(/[A-Z]/g,function(letter) {return "_" + letter.toLowerCase();}),reason:reason});
    point[field] = null;
}
function capture(plugin,candidate,staffIndex,exact) {
    var point = {
        measure:candidate.measure, tick:candidate.tick, absoluteTime:candidate.time,
        offset:subtract(candidate.time,candidate.start),
        measureNominalMeter:candidate.nominal, measureActualDuration:candidate.actual,
        instrument:null,nativeClefType:null,nativeKey:null,localMeter:null,
        timeStretch:null,nativeTransposition:null,unavailableFields:[]
    };
    var staff = plugin.curScore.staves ? plugin.curScore.staves[staffIndex] : null;
    var part = staff ? staff.part : null;
    if (!part) {
        var parts = plugin.curScore.parts || [];
        for (var index = 0; index < parts.length; index++) {
            if (parts[index].startTrack <= staffIndex * Constants.voicesPerStaff
                    && parts[index].endTrack > staffIndex * Constants.voicesPerStaff) {
                part = parts[index]; break;
            }
        }
    }
    try {
        if (!part || typeof part.instrumentAtTick !== "function") throw new Error("Native instrumentAtTick unavailable");
        var instrument = part.instrumentAtTick(exact ? candidate.nativeTime : candidate.tick);
        if (!instrument) throw new Error("Native instrument lookup returned null");
        point.instrument = {instrumentId:typeof instrument.instrumentId === "string" ? instrument.instrumentId : null,
            musicxmlId:typeof instrument.musicXmlId === "string" ? instrument.musicXmlId : null,
            longName:typeof instrument.longName === "string" ? instrument.longName : null,
            shortName:typeof instrument.shortName === "string" ? instrument.shortName : null};
        ["longName","shortName"].forEach(function(name) {
            if (point.instrument[name] === null) point.unavailableFields.push({
                field:"instrument." + name.replace(/[A-Z]/g,function(letter) {return "_" + letter.toLowerCase();}),
                reason:"Native instrument name unavailable"});
        });
        if (point.instrument.instrumentId === null) {
            point.unavailableFields.push({field:"instrument.instrument_id",reason:"Native instrument ID unavailable"});
        }
        if (point.instrument.musicxmlId === null) {
            point.unavailableFields.push({field:"instrument.musicxml_id",reason:"Native MusicXML ID unavailable on this wrapper"});
        }
    } catch (error) { unavailable(point,"instrument",String(error)); }
    var queries = [
        ["nativeClefType","clefType",function(value) {return integer(value) ? value : null;}],
        ["nativeKey","key",function(value) {return integer(value) ? value : null;}],
        ["localMeter","timeSig",function(value) {return value ? meter(value.timesig) : null;}],
        ["timeStretch","timeStretch",function(value) {return value ? rational(value) : null;}],
        ["nativeTransposition","transpose",function(value) {
            return value && integer(value.chromatic) && integer(value.diatonic)
                ? {chromatic:value.chromatic,diatonic:value.diatonic} : null;
        }]
    ];
    for (var queryIndex = 0; queryIndex < queries.length; queryIndex++) {
        var query = queries[queryIndex];
        try {
            if (!exact || !staff || typeof staff[query[1]] !== "function") {
                throw new Error("Native direct staff getter requires MuseScore 4.6+ and this wrapper");
            }
            var value = query[2](staff[query[1]](candidate.nativeTime));
            if (value === null) throw new Error("Native getter returned unavailable metadata");
            point[query[0]] = value;
        } catch (error) { unavailable(point,query[0],String(error)); }
    }
    return point;
}
function state(point) {
    return JSON.stringify([point.instrument,point.nativeClefType,point.nativeKey,
        point.localMeter,point.timeStretch,point.nativeTransposition,
        point.measureNominalMeter,point.measureActualDuration,point.unavailableFields]);
}
function readStaffContext(plugin,params) {
    var scoreError = Score.requireScore(plugin);
    if (scoreError) return scoreError;
    var start = params.startMeasure;
    var end = params.endMeasure;
    if (!integer(start) || !integer(end) || start < 1 || end < start || end - start + 1 > maxMeasures) {
        return {error:"Invalid inclusive context range (maximum " + maxMeasures + " measures)"};
    }
    var selected = params.staff;
    if (selected !== undefined && selected !== null && (!integer(selected) || selected < 0
            || selected >= plugin.curScore.nstaves)) return {error:"Staff out of range"};
    if (!integer(plugin.curScore.nstaves) || plugin.curScore.nstaves <= 0) {
        return {error:"Native staff count unavailable"};
    }
    if ((selected === null || selected === undefined) && plugin.curScore.nstaves > maxQueries) {
        return {error:"Context staff-by-point query limit exceeded; select fewer staves"};
    }
    var staves = Passage.indices(plugin.curScore.nstaves,selected);
    var exact = plugin.mscoreMajorVersion === 4 && plugin.mscoreMinorVersion >= 6;
    var candidates = [];
    var measure = plugin.curScore.firstMeasure;
    var scannedSegments = 0;
    try {
        for (var ordinal = 1; ordinal <= end; ordinal++) {
            if (!measure) throw new Error("Measure out of range");
            if (ordinal > maxMeasuresScanned) throw new Error("Context measure scan limit exceeded");
            if (ordinal >= start) {
                if (!measure.firstSegment) throw new Error("Measure has no native segments");
                var nativeStart = exact ? measure.tick : null;
                var startTime = exact ? rational(nativeStart)
                    : Passage.fraction(measure.firstSegment.tick,Constants.ticksPerWholeNote);
                var actual = rational(measure.timesigActual);
                var nominal = meter(measure.timesigNominal);
                if (!nominal) throw new Error("Native measure nominal meter unavailable");
                var endTime = add(startTime,actual);
                var seen = {};
                function include(nativeTime,tick,time) {
                    if (!integer(tick) || compare(time,startTime) < 0 || compare(time,endTime) >= 0) return;
                    var key = time.numerator + "/" + time.denominator;
                    if (seen[key]) return;
                    seen[key] = true;
                    candidates.push({measure:ordinal,nativeTime:nativeTime,time:time,tick:tick,
                        start:startTime,actual:actual,nominal:nominal});
                    if (candidates.length * staves.length > maxQueries) {
                        throw new Error("Context staff-by-point query limit exceeded; request a smaller range");
                    }
                }
                var startTick = exact ? nativeStart.ticks : measure.firstSegment.tick;
                if (!integer(startTick)) throw new Error("Native measure start tick unavailable");
                include(nativeStart,startTick,startTime);
                var segment = measure.firstSegment;
                while (segment) {
                    scannedSegments++;
                    if (scannedSegments > maxSegmentsScanned) throw new Error("Context segment scan limit exceeded");
                    var nativeTime = exact ? segment.fraction : null;
                    include(nativeTime,segment.tick,exact ? rational(nativeTime)
                        : Passage.fraction(segment.tick,Constants.ticksPerWholeNote));
                    segment = segment.nextInMeasure;
                }
            }
            measure = measure.nextMeasure;
        }
        if (!candidates.length || !staves.length) throw new Error("No context samples available");
        candidates.sort(function(left,right) {return compare(left.time,right.time);});
        // No native instrument/staff queries happen until the entire cost is validated.
        var timelines = [];
        for (var staffIndex = 0; staffIndex < staves.length; staffIndex++) {
            var first = capture(plugin,candidates[0],staves[staffIndex],exact);
            var changes = [];
            var previous = state(first);
            for (var pointIndex = 1; pointIndex < candidates.length; pointIndex++) {
                var point = capture(plugin,candidates[pointIndex],staves[staffIndex],exact);
                var current = state(point);
                if (current !== previous) {changes.push(point);previous = current;}
            }
            timelines.push({staff:staves[staffIndex],start:first,changes:changes});
        }
        return {result:{source:"live",startMeasure:start,endMeasure:end,staves:staves,
            nativeMajorVersion:plugin.mscoreMajorVersion,nativeMinorVersion:plugin.mscoreMinorVersion,
            positionPrecision:exact ? "native_fraction" : "native_integer_ticks",
            sampling:"passage_start_and_native_segment_changes",samplingComplete:true,tempoStatus:"not_queried",
            sampledPointCount:candidates.length,staffPointQueries:candidates.length * staves.length,
            timelines:timelines,limitations:limitations.slice()}};
    } catch (error) { return {error:String(error)}; }
}
