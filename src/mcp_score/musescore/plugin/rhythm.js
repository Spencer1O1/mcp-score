// Structural replacements through independent native input, never delete/reinsert.
.pragma library
.import "constants.js" as Constants
.import "passage.js" as Passage
.import "pitch.js" as Pitch
.import "duration.js" as Duration
.import "context.js" as Context
.import "score.js" as Score

var limitations = [
    "target voice measure snapshot; no score-instance identity or global revision",
    "identical contents in another score cannot be distinguished",
    "MuseScore 4.7+ in major version 4 only",
    "ordinary undecorated rest or single-tone unbeamed chord only; no bar crossing or time shifting",
    "full-measure rests require a regular 4/4 bar; pickups and irregular full-measure rests reject",
    "target attachments, nonunit time stretch and intersecting or unknown spanners reject",
    "verification covers exposed measure/context/adjacent-measure events and spanner metadata, not every engraving property",
    "UI selection and layout may change; logical bridge cursor is untouched",
    "no mutation receipts, sequence execution or automatic replay"
];
function failure(params, code, message, status, extra) {
    var reply = {error:message,code:code,mutation_status:status,verified:false,
        target:{measure:params.measure,staff:params.staff,voice:params.voice,offset:params.offset},
        rollback_verified:status === "rolled_back",
        snapshot_scope:"target_voice_measure",selection_status:status === "not_applied" ? "unchanged" : "not_preserved",
        limitations:limitations.slice()};
    Object.keys(extra || {}).forEach(function(name) {reply[name]=extra[name];});
    return reply;
}
function measureAt(plugin, ordinal) {
    var measure = plugin.curScore.firstMeasure;
    for (var index=1; measure && index<ordinal; index++) measure=measure.nextMeasure;
    return measure;
}
// There may be non-rhythmic segments at the same tick. Resolve exactly one
// ChordRest element and retain no wrappers across structural changes.
function resolve(plugin,params) {
    var measure=measureAt(plugin,params.measure);
    if (!measure || !measure.firstSegment) throw new Error("Native measure unavailable");
    var onset=measure.firstSegment.tick + Duration.ticks(params.offset);
    var track=params.staff*Constants.voicesPerStaff+params.voice;
    var matches=[];
    for (var segment=measure.firstSegment; segment; segment=segment.nextInMeasure) {
        var element=segment.elementAt(track);
        if (segment.tick===onset && element
                && (element.type===plugin.Element.CHORD || element.type===plugin.Element.REST)) {
            matches.push({element:element,segment:segment,tick:onset,measure:measure,track:track});
        }
    }
    if (matches.length!==1) throw new Error("Exactly one existing rhythmic onset is required");
    return matches[0];
}
function staffContext(plugin,params) {
    var reply=Context.readStaffContext(plugin,{startMeasure:params.measure,endMeasure:params.measure});
    if (reply.error) throw new Error(reply.error);
    return JSON.stringify(reply.result.timelines);
}
function adjacent(plugin,params) {
    var count=plugin.curScore.nmeasures;
    if (!Pitch.integer(count) || count<params.measure) throw new Error("Native measure count unavailable");
    var reply=Passage.readPassage(plugin,{startMeasure:Math.max(1,params.measure-1),endMeasure:Math.min(count,params.measure+1)});
    if (reply.error) throw new Error(reply.error);
    return JSON.stringify([count,reply.result.measures.filter(function(measure) {
        return measure.measure!==params.measure;
    }).map(Duration.measureKey)]);
}
function state(plugin,params) {
    return [staffContext(plugin,params),adjacent(plugin,params),Duration.spanners(plugin)];
}
function stateKey(value) {return JSON.stringify(value);}
function staffFacts(plugin,resolved,params) {
    var staff=plugin.curScore.staves ? plugin.curScore.staves[params.staff] : null;
    var part=staff ? staff.part : null;
    if (!part || part.hasPitchedStaff!==true || part.hasDrumStaff!==false || part.hasTabStaff!==false
            || typeof staff.transpose!=="function" || typeof staff.timeStretch!=="function") {
        throw new Error("A native pitched non-drum/non-tab staff is required");
    }
    var nativeTime=resolved.segment.fraction;
    if (Duration.ticks(nativeTime)!==resolved.tick) throw new Error("Exact native segment fraction unavailable");
    var stretch=staff.timeStretch(nativeTime);
    if (!stretch || stretch.numerator!==stretch.denominator || stretch.denominator<=0) {
        throw new Error("Nonunit or unknown native time stretch");
    }
    var interval=staff.transpose(nativeTime);
    if (!interval || !Pitch.integer(interval.chromatic) || !Pitch.integer(interval.diatonic)) {
        throw new Error("Exact native transposition unavailable");
    }
    var delta=7*interval.chromatic-12*interval.diatonic;
    if (!Pitch.integer(delta)) throw new Error("Native transposition exceeds exact bounds");
    var concert=plugin.curScore.style ? plugin.curScore.style.value("concertPitch") : null;
    if (typeof concert!=="boolean") throw new Error("Native concert-pitch display mode unavailable");
    return {delta:delta,chromatic:interval.chromatic,concert:concert};
}
function plainRest(plugin,rest,track,measure,onset,allowFull) {
    var length=Duration.ticks(rest.actualDuration);
    if (rest.type!==plugin.Element.REST || rest.track!==track || !length
            || rest.tuplet!==null || rest.topTuplet!==null || rest.beam!==null
            || rest.staffMove!==0 || rest.gap!==false || rest.visible!==true || rest.generated!==false
            || !Duration.empty(rest.elements) || !Duration.empty(rest.lyrics)) return false;
    if (rest.isFullMeasureRest===true) {
        return allowFull && length===Constants.ticksPerWholeNote
            && onset===measure.firstSegment.tick
            && Duration.ticks(measure.timesigActual)===length
            && measure.timesigNominal.numerator===4 && measure.timesigNominal.denominator===4;
    }
    return rest.isFullMeasureRest===false && Duration.ticks(rest.duration)===length;
}
function intervalClear(plugin,resolved,length) {
    var end=resolved.tick+length;
    for (var segment=resolved.measure.firstSegment; segment; segment=segment.nextInMeasure) {
        if (!Pitch.integer(segment.tick)) throw new Error("Unknown segment tick");
        if (segment.tick<resolved.tick || segment.tick>=end) continue;
        if (!segment.annotations || !Pitch.integer(segment.annotations.length)) throw new Error("Unknown annotations");
        for (var index=0; index<segment.annotations.length; index++) {
            var track=segment.annotations[index].track;
            if (!Pitch.integer(track) || track<0 || track===resolved.track) {
                throw new Error("Target interval contains an annotation");
            }
        }
        var element=segment.elementAt(resolved.track);
        if (element && (element.type===plugin.Element.CHORD || element.type===plugin.Element.REST)
                && !element.is(resolved.element)) throw new Error("Target interval overlaps another event");
        if (element && segment.tick>resolved.tick) throw new Error("Target interval contains native notation");
    }
}
function cursorAt(plugin,resolved,params) {
    var cursor=plugin.curScore.newCursor();
    // Explicitly reset even though the native default is independent input.
    cursor.inputStateMode=0; // Cursor.INPUT_STATE_INDEPENDENT
    cursor.filter=plugin.Segment.ChordRest;
    cursor.track=resolved.track;
    cursor.rewindToFraction(resolved.segment.fraction);
    if (cursor.inputStateMode!==0 || cursor.filter!==plugin.Segment.ChordRest
            || cursor.track!==resolved.track || cursor.staffIdx!==params.staff || cursor.voice!==params.voice
            || cursor.tick!==resolved.tick || !cursor.segment || !cursor.element
            || !cursor.segment.is(resolved.segment) || !cursor.element.is(resolved.element)) {
        throw new Error("Independent cursor did not resolve the exact existing track/segment/element");
    }
    return cursor;
}
function verify(plugin,params,before,after,onset,oldLength,newLength,fill,facts) {
    var end=onset+oldLength;
    function outside(event) {
        return event.kind==="other" || event.staff!==params.staff || event.voice!==params.voice
            || event.tick<onset || event.tick>=end;
    }
    if (Duration.keys(before.events.filter(outside))!==Duration.keys(after.events.filter(outside))
            || JSON.stringify([before.tick,before.timeSignature,before.actualDuration])
            !==JSON.stringify([after.tick,after.timeSignature,after.actualDuration])) return false;
    var inside=Duration.voiceEvents(after.events,params).filter(function(event) {
        return event.kind!=="other" && event.tick>=onset && event.tick<end;
    });
    if (!inside.length || inside[0].tick!==onset) return false;
    var current=resolve(plugin,params);
    if (fill) {
        if (inside[0].kind!=="chord" || current.element.notes.length!==1
                || !Duration.eligible(plugin,current.element)) return false;
        var note=current.element.notes[0];
        if (note.pitch!==params.pitch || note.tpc1!==params.tpc || note.tpc2!==params.tpc-facts.delta
                || note.tpc!==(facts.concert ? params.tpc : params.tpc-facts.delta)
                || Duration.ticks(inside[0].duration)!==newLength
                || Duration.ticks(inside[0].actualDuration)!==newLength) return false;
    }
    var covered=fill ? onset+newLength : onset;
    var restIndex=fill ? 1 : 0;
    for (; restIndex<inside.length; restIndex++) {
        var event=inside[restIndex];
        if (event.kind!=="rest" || event.tick!==covered) return false;
        var nativeParams={measure:params.measure,staff:params.staff,voice:params.voice,
            offset:event.offset};
        var native=resolve(plugin,nativeParams);
        var length=Duration.ticks(native.element.actualDuration);
        if (!plainRest(plugin,native.element,current.track,native.measure,event.tick,true)
                || event.notes.length!==0) return false;
        covered+=length;
    }
    return covered===end;
}
function edit(plugin,params,fill) {
    var scoreError=Score.requireScore(plugin);
    if (scoreError) return failure(params,"target_unavailable",scoreError.error,"not_applied");
    if (plugin.mscoreMajorVersion!==4 || !Pitch.integer(plugin.mscoreMinorVersion)
            || plugin.mscoreMinorVersion<7 || typeof plugin.fraction!=="function"
            || !plugin.curScore.spanners || !plugin.Segment || !Pitch.integer(plugin.Segment.ChordRest)) {
        return failure(params,"unsupported_native_api","Requires MuseScore 4.7+ native input and spanner metadata","not_applied");
    }
    if (!Pitch.integer(params.measure) || params.measure<1 || !Pitch.integer(params.staff)
            || params.staff<0 || params.staff>=plugin.curScore.nstaves || !Pitch.integer(params.voice)
            || params.voice<0 || params.voice>3 || Duration.ticks(params.offset)===null
            || !Array.isArray(params.expected) || !params.expected.length
            || (fill && (!Duration.canonical(Duration.ticks(params.duration))
                || !Pitch.integer(params.pitch) || params.pitch<0 || params.pitch>127
                || !Pitch.validTpc(params.tpc) || !Pitch.congruentPitch(params.pitch,params.tpc)))) {
        return failure(params,"invalid_target","Invalid exact position, duration, pitch, spelling or snapshot","not_applied");
    }
    var before,resolved,beforeState,facts,oldLength,newLength,cursor;
    try {
        before=Duration.read(plugin,params);
        if (Duration.keys(Duration.voiceEvents(before.events,params))!==Duration.keys(params.expected)) {
            return failure(params,"stale_target","Target voice measure differs from expected snapshot","not_applied",{actual:before.events});
        }
        resolved=resolve(plugin,params);
        oldLength=Duration.ticks(resolved.element.actualDuration);
        newLength=fill ? Duration.ticks(params.duration) : oldLength;
        if (!oldLength || !Duration.canonical(newLength) || newLength>oldLength
                || resolved.tick+oldLength>before.tick+Duration.ticks(before.actualDuration)) {
            throw new Error("Unsupported duration or bar-crossing target");
        }
        if (fill) {
            if (!plainRest(plugin,resolved.element,resolved.track,resolved.measure,resolved.tick,true)
                    || (resolved.element.isFullMeasureRest===false && !Duration.canonical(oldLength))) {
                throw new Error("Target must be an undecorated undotted rest or a regular 4/4 full-measure rest");
            }
        } else if (resolved.element.type!==plugin.Element.CHORD || resolved.element.notes.length!==1
                || !Duration.eligible(plugin,resolved.element) || Duration.ticks(resolved.element.duration)!==oldLength) {
            throw new Error("Target must be an ordinary undecorated undotted single-tone unbeamed chord");
        }
        facts=staffFacts(plugin,resolved,params);
        if (fill && (!Pitch.validTpc(params.tpc-facts.delta)
                || !Pitch.congruentPitch(params.pitch-facts.chromatic,params.tpc-facts.delta))) {
            throw new Error("Requested spelling cannot be represented in native written TPC");
        }
        intervalClear(plugin,resolved,oldLength);
        Duration.spanners(plugin,resolved.tick,resolved.tick+oldLength);
        beforeState=state(plugin,params);
        cursor=cursorAt(plugin,resolved,params);
        cursor.setDuration(fill ? params.duration.numerator : newLength,
                           fill ? params.duration.denominator : Constants.ticksPerWholeNote);
    } catch (error) {return failure(params,"unsupported_target",String(error),"not_applied");}
    var score=plugin.curScore;
    var started=false,committed=false;
    try {
        score.startCmd(fill ? "fillRestWithNote" : "replaceNoteWithRest");
        started=true;
        if (fill) {
            cursor.addNote(params.pitch,false);
            var fresh=resolve(plugin,params);
            if (fresh.element.type!==plugin.Element.CHORD || fresh.element.notes.length!==1) {
                throw new Error("Native input did not create one note");
            }
            fresh.element.notes[0].pitch=params.pitch;
            fresh.element.notes[0].tpc1=params.tpc;
            fresh.element.notes[0].tpc2=params.tpc-facts.delta;
        } else {
            cursor.addRest();
        }
        // No original element wrappers are used after native replacement.
        var after=Duration.read(plugin,params);
        if (!verify(plugin,params,before,after,resolved.tick,oldLength,newLength,fill,facts)
                || stateKey(beforeState)!==stateKey(state(plugin,params))) {
            throw new Error("Native replacement failed complete interval/measure/context verification");
        }
        started=false;
        score.endCmd(false);
        committed=true;
        after=Duration.read(plugin,params);
        if (!verify(plugin,params,before,after,resolved.tick,oldLength,newLength,fill,facts)
                || stateKey(beforeState)!==stateKey(state(plugin,params))) {
            return failure(params,"verification_failed","Committed replacement failed final verification",
                "applied_unverified",{before:before.events,actual:after.events});
        }
        return {result:{measure:params.measure,staff:params.staff,voice:params.voice,
            before:before.events,after:after.events,verified:true,mutationStatus:"applied",
            snapshotScope:"target_voice_measure",selectionStatus:"not_preserved",limitations:limitations.slice()}};
    } catch (error) {
        var status=committed ? "applied_unverified" : "unknown";
        var actual=null,cancelled=false;
        try {
            if (started) {started=false;score.endCmd(true);cancelled=true;}
            actual=Duration.read(plugin,params);
            var restored=resolve(plugin,params);
            var eligible=fill ? plainRest(plugin,restored.element,restored.track,restored.measure,restored.tick,true)
                : restored.element.type===plugin.Element.CHORD && Duration.eligible(plugin,restored.element);
            if (cancelled && eligible && Duration.measureKey(before)===Duration.measureKey(actual)
                    && stateKey(beforeState)===stateKey(state(plugin,params))) status="rolled_back";
        } catch (rollbackError) {if (!committed) status="unknown";}
        return failure(params,"verification_failed",String(error),status,
            {before:before.events,actual:actual ? actual.events : null});
    }
}
function fillRestWithNote(plugin,params) {return edit(plugin,params,true);}
function replaceNoteWithRest(plugin,params) {return edit(plugin,params,false);}
