// Fixed structural-fault driver; installed only into the owned disposable copy.
.pragma library

function arm(plugin, params, kind) {
    if (Object.keys(params).length || !plugin.curScore
            || plugin.curScore.title !== "Guarded pitch fixture") {
        return {error:"Fixed chord-tone fault requires the disposable pitch fixture and no parameters"};
    }
    plugin.chordToneFault={kind:kind,armed:true,observed:null};
    return read(plugin,{});
}
function armAdd(plugin, params) { return arm(plugin,params,"add"); }
function armRemove(plugin, params) { return arm(plugin,params,"remove"); }
function read(plugin, params) {
    if (Object.keys(params).length) return {error:"No fault parameters are accepted"};
    return {result:plugin.chordToneFault || {kind:null,armed:false,observed:null}};
}
function capture(plugin, chord, kind, beforeCount) {
    var state=plugin.chordToneFault;
    if (!state || !state.armed || state.kind !== kind) return;
    var afterCount=chord.notes.length;
    if (afterCount !== beforeCount + (kind === "add" ? 1 : -1)) return;
    var pitches=[];
    for (var index=0;index<afterCount;index++) pitches.push(chord.notes[index].pitch);
    plugin.chordToneFault={kind:kind,armed:false,
        observed:{beforeCount:beforeCount,afterCount:afterCount,pitches:pitches}};
    throw Error("CI fixed fault after native chord-tone " + kind);
}
