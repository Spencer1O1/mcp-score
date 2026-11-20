// Fixed CI fault only in the marked installed copy, never production dispatch.
.pragma library
function arm(plugin,params) {
    if (Object.keys(params).length || !plugin.curScore || ["Guarded pitch fixture",
            "Guarded note rest fixture","Guarded note rest concert fixture"].indexOf(plugin.curScore.title)<0) {
        return {error:"Fixed lyric fault requires the owned fixture and no parameters"};
    }
    plugin.lyricTextFault={armed:true,observed:null};
    return read(plugin,{});
}
function read(plugin,params) {
    if (Object.keys(params).length) return {error:"No fault parameters accepted"};
    return {result:plugin.lyricTextFault || {armed:false,observed:null}};
}
function capture(plugin,lyric) {
    if (!plugin.lyricTextFault || !plugin.lyricTextFault.armed) return;
    plugin.lyricTextFault={armed:false,observed:{xmlText:lyric.text,plainText:lyric.plainText,
        verse:lyric.verse,syllabic:lyric.syllabic,placement:lyric.placement,
        lyricTicks:{numerator:lyric.lyricTicks.numerator,denominator:lyric.lyricTicks.denominator}}};
    throw Error("CI fixed fault after native lyric TEXT write");
}
