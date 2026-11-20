// CI installed-copy only: observe fresh primitives after replacement, then fail.
.pragma library

function arm(plugin,params,kind) {
    if (Object.keys(params).length || !plugin.curScore
            || ["Guarded note rest fixture","Guarded note rest concert fixture"].indexOf(plugin.curScore.title)<0) {
        return {error:"Fixed note/rest fault requires an owned disposable fixture and no parameters"};
    }
    plugin.noteRestFault={kind:kind,armed:true,observed:null};
    return read(plugin,{});
}
function armFill(plugin,params) {return arm(plugin,params,"fill");}
function armReplace(plugin,params) {return arm(plugin,params,"replace");}
function read(plugin,params) {
    if (Object.keys(params).length) return {error:"No fault parameters accepted"};
    return {result:plugin.noteRestFault || {kind:null,armed:false,observed:null}};
}
function capture(plugin,params,kind) {
    var fault=plugin.noteRestFault;
    if (!fault || !fault.armed || fault.kind!==kind) return;
    // Never inspect the removed wrapper: walk the live measure again.
    var measure=plugin.curScore.firstMeasure;
    for (var index=1;measure && index<params.measure;index++) measure=measure.nextMeasure;
    var observed=[];
    var track=params.staff*4+params.voice;
    for (var segment=measure.firstSegment;segment;segment=segment.nextInMeasure) {
        var element=segment.elementAt(track);
        if (!element || (element.type!==plugin.Element.CHORD && element.type!==plugin.Element.REST)) continue;
        var pitches=[];
        if (element.type===plugin.Element.CHORD) {
            for (var tone=0;tone<element.notes.length;tone++) pitches.push(element.notes[tone].pitch);
        }
        observed.push({tick:segment.tick,type:element.type,pitches:pitches,
            duration:{numerator:element.actualDuration.numerator,denominator:element.actualDuration.denominator}});
    }
    plugin.noteRestFault={kind:kind,armed:false,observed:observed};
    throw Error("CI fixed fault after native note/rest " + kind);
}
