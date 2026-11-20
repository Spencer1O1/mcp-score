// TEXT-only mutation. Native float appearance values never enter receipt JSON.
.pragma library
.import "pitch.js" as Pitch
.import "passage.js" as Passage
.import "chord-tone.js" as ChordTone
.import "duration.js" as Duration
.import "score.js" as Score

var limitations=[
    "expected snapshot covers exposed target-chord fields; native appearance is captured freshly inside the handler",
    "no global score identity/revision; identical contents in another score cannot be distinguished",
    "master excerpts must be known empty; unexposed links and every engraving property cannot be certified",
    "single existing canonical plain SINGLE lyric only; no lyric creation/deletion/formatting/syllabification",
    "all-track measure events, selected native lyric metadata and exposed score-spanner metadata are verified",
    "UI selection/layout are not preserved; logical bridge position is untouched"
];
var styleNumbers=["fontSize","lineSpacing","offsetX","offsetY"];
var styleIntegers=["subStyle","fontStyle","textScriptAlign","position","frameType"];
var styleBooleans=["visible","autoplace","sizeSpatiumDependent","avoidBarLines",
    "textLinkedToMaster","appearanceLinkedToMaster","positionLinkedToMaster","excludeFromParts"];
var verifiedFields=["verse","syllabic","placement","lyricTicks","isMelisma","separator",
    "fontFace","color","align"].concat(styleNumbers,styleIntegers,styleBooleans);
function validText(text,limit) {
    limit=limit || 512;
    if (typeof text!=="string" || !text.length || text.length>limit*2) return false;
    var count=0;
    for (var index=0;index<text.length;index++) {
        var code=text.charCodeAt(index);
        if (code>=0xd800 && code<=0xdbff) {
            var low=text.charCodeAt(++index);
            if (!(low>=0xdc00 && low<=0xdfff)) return false;
            code=0x10000+(code-0xd800)*1024+low-0xdc00;
        } else if (code>=0xdc00 && code<=0xdfff) return false;
        if (code<0x20 || (code>=0x7f && code<=0x9f) || (code>=0xfdd0 && code<=0xfdef)
                || (code & 0xffff)>=0xfffe || code===0x2028 || code===0x2029 || ++count>limit) return false;
    }
    return true;
}
// Matches String::toXmlEscaped and TextBase::createBlocks' supported entities.
// Apostrophe is literal: createBlocks does not decode &apos; or numeric entities.
function encode(text) {
    return text.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function failure(params,code,message,status,before,actual) {
    return {error:message,code:code,mutation_status:status,verified:false,
        rollback_verified:status==="rolled_back",snapshot_scope:"target_chord",
        target:{measure:params.measure,staff:params.staff,voice:params.voice,offset:params.offset,
            lyric_index:params.lyricIndex},before:before,actual:actual,
        selection_status:status==="not_applied" ? "unchanged" : "not_preserved",limitations:limitations.slice()};
}
function noExcerpts(plugin) {
    var excerpts=plugin.curScore.excerpts;
    if (!excerpts || !Pitch.integer(excerpts.length) || excerpts.length!==0) {
        throw Error("Known empty master excerpt list is required; linked parts are unsupported");
    }
}
function spans(plugin) {
    var list=plugin.curScore.spanners;
    if (!list || !Pitch.integer(list.length) || list.length<0) throw Error("Unknown native spanner list");
    return Duration.spanners(plugin);
}
function requireField(valid,name) {
    if (!valid) throw Error("Unsupported native lyric field: "+name);
}
function knownLyricType(plugin,lyric) {
    return plugin.Element && Pitch.integer(plugin.Element.LYRICS) && plugin.Element.LYRICS>0
        && lyric && Pitch.integer(lyric.type) && lyric.type===plugin.Element.LYRICS;
}
function capture(plugin,lyric) {
    requireField(knownLyricType(plugin,lyric),"type");
    // Validate native UTF-16 before its parser, which assumes paired surrogates.
    var xml=lyric.text;
    requireField(validText(xml,3072),"text (XML scalars/size)");
    var plain=lyric.plainText;
    requireField(validText(plain),"plainText (scalars/size)");
    requireField(xml===encode(plain),"text (noncanonical/rich XML)");
    requireField(lyric.syllabic===plugin.Lyrics.SINGLE,"syllabic (requires SINGLE)");
    requireField(Pitch.integer(lyric.verse) && lyric.verse>=0,"verse");
    requireField([plugin.Placement.ABOVE,plugin.Placement.BELOW].indexOf(lyric.placement)>=0,"placement");
    requireField(lyric.isMelisma===false,"isMelisma");
    requireField(lyric.separator===null,"separator");
    requireField(lyric.lyricTicks && Pitch.integer(lyric.lyricTicks.numerator)
        && Pitch.integer(lyric.lyricTicks.denominator) && lyric.lyricTicks.numerator===0
        && lyric.lyricTicks.denominator>0,"lyricTicks (requires zero exact fraction)");
    var style={};
    if (typeof lyric.fontFace!=="string" || !lyric.fontFace.length) throw Error("Unknown native lyric font");
    style.fontFace=lyric.fontFace;
    styleNumbers.forEach(function(name) {
        var value=lyric[name];
        if (typeof value!=="number" || !isFinite(value)) throw Error("Unknown native lyric appearance: "+name);
        style[name]=value;
    });
    requireField(style.fontSize>0,"fontSize");
    requireField(style.lineSpacing>0,"lineSpacing");
    styleIntegers.forEach(function(name) {
        if (!Pitch.integer(lyric[name])) throw Error("Unknown native lyric appearance: "+name);
        style[name]=lyric[name];
    });
    // PropertyValue::toQVariant exposes native ALIGN as [horizontal, vertical],
    // not the older combined PluginAPI Align flag used by its setter.
    var alignment=lyric.align;
    if (!alignment || alignment.length!==2 || !Pitch.integer(alignment[0])
            || !Pitch.integer(alignment[1])) throw Error("Unknown native lyric alignment pair");
    style.align=[alignment[0],alignment[1]];
    if (style.frameType!==0) throw Error("Framed lyrics are unsupported");
    styleBooleans.forEach(function(name) {
        if (typeof lyric[name]!=="boolean") throw Error("Unknown native lyric appearance: "+name);
        style[name]=lyric[name];
    });
    // QColor is a QML value type, whose native string conversion is hex color.
    var color=String(lyric.color);
    if (!/^#[0-9a-f]{6}([0-9a-f]{2})?$/i.test(color)) throw Error("Unknown native lyric color");
    style.color=color;
    return {core:{xmlText:xml,plainText:plain,verse:lyric.verse,
        syllabic:lyric.syllabic,placement:lyric.placement,lyricTicks:Passage.duration(lyric.lyricTicks)},style:style};
}
function read(plugin,params) {
    var current=ChordTone.read(plugin,params);
    var list=current.resolved.chord.lyrics;
    if (!list || !Pitch.integer(list.length) || list.length<=params.lyricIndex) throw Error("Lyric index unavailable");
    var identities={};
    for (var index=0;index<list.length;index++) {
        var lyric=list[index];
        requireField(knownLyricType(plugin,lyric),"lyrics["+index+"].type");
        if (!Pitch.integer(lyric.verse) || !Pitch.integer(lyric.placement)) throw Error("Unknown lyric identity");
        var key=lyric.verse+":"+lyric.placement;
        if (Object.prototype.hasOwnProperty.call(identities,key)) throw Error("Duplicate verse/placement lyric identity");
        identities[key]=true;
    }
    current.lyric=list[params.lyricIndex];
    current.native=capture(plugin,current.lyric);
    current.measure=Duration.read(plugin,params);
    current.spans=spans(plugin);
    noExcerpts(plugin);
    return current;
}
function verify(before,after,params) {
    var expected=JSON.parse(JSON.stringify(before.event));
    expected.lyrics[params.lyricIndex].text=encode(params.plainText);
    if (ChordTone.snapshotKey(expected,true)!==ChordTone.snapshotKey(after.event,true)) return false;
    var expectedMeasure=JSON.parse(JSON.stringify(before.measure));
    var replaced=false;
    for (var index=0;index<expectedMeasure.events.length;index++) {
        if (ChordTone.snapshotKey(expectedMeasure.events[index],true)===ChordTone.snapshotKey(before.event,true)) {
            if (replaced) return false;
            expectedMeasure.events[index]=expected;replaced=true;
        }
    }
    var native=JSON.parse(JSON.stringify(before.native));
    native.core.xmlText=encode(params.plainText);native.core.plainText=params.plainText;
    return replaced && Duration.measureKey(expectedMeasure)===Duration.measureKey(after.measure)
        && ChordTone.stable(native)===ChordTone.stable(after.native) && before.spans===after.spans;
}
function success(params,before,after,status) {
    return {result:{measure:params.measure,lyricIndex:params.lyricIndex,before:before.event,after:after.event,
        selectedBefore:before.native.core,selectedAfter:after.native.core,verified:true,mutationStatus:status,
        snapshotScope:"target_chord",selectionStatus:status==="unchanged" ? "unchanged" : "not_preserved",
        verifiedNativeFields:verifiedFields.slice(),limitations:limitations.slice()}};
}
function setLyricText(plugin,params) {
    var scoreError=Score.requireScore(plugin);
    if (scoreError) return failure(params,"target_unavailable",scoreError.error,"not_applied",null,null);
    if (plugin.mscoreMajorVersion!==4 || !Pitch.integer(plugin.mscoreMinorVersion) || plugin.mscoreMinorVersion<7
            || !plugin.Element || !Pitch.integer(plugin.Element.LYRICS) || plugin.Element.LYRICS<=0
            || !plugin.Lyrics || !Pitch.integer(plugin.Lyrics.SINGLE) || !plugin.Placement
            || !Pitch.integer(plugin.Placement.ABOVE) || !Pitch.integer(plugin.Placement.BELOW)) {
        return failure(params,"unsupported_native_api","Requires MuseScore4.7+ dedicated lyric metadata","not_applied",null,null);
    }
    if (!Pitch.integer(params.measure) || params.measure<1 || !Pitch.integer(params.staff)
            || params.staff<0 || params.staff>=plugin.curScore.nstaves || !Pitch.integer(params.voice)
            || params.voice<0 || params.voice>3 || !params.offset || Duration.ticks(params.offset)===null
            || !Pitch.integer(params.lyricIndex) || params.lyricIndex<0 || !validText(params.plainText)) {
        return failure(params,"invalid_target","Invalid explicit target or single-line Unicode plain text","not_applied",null,null);
    }
    var before;
    try {
        before=read(plugin,params);
        var expected=params.expected;
        if (!expected || !Array.isArray(expected.notes) || expected.text===undefined
                || expected.lyrics===undefined || expected.articulations===undefined
                || ChordTone.snapshotKey(expected,true)!==ChordTone.snapshotKey(before.event,true)) {
            return failure(params,"stale_target","Complete target chord differs from expected snapshot","not_applied",before.event,before.event);
        }
        if (!Pitch.supported(plugin,before.resolved.chord)) throw Error("Only ordinary pitched chords are supported");
        if (before.native.core.plainText===params.plainText) {
            var unchanged=read(plugin,params);
            if (!verify(before,unchanged,params)) throw Error("Native no-op read-back changed");
            return success(params,before,unchanged,"unchanged");
        }
    } catch (error) {return failure(params,"unsupported_target",String(error),"not_applied",before ? before.event : null,null);}
    var score=plugin.curScore,started=false,committed=false;
    try {
        score.startCmd("setLyricText");started=true;
        before.lyric.text=encode(params.plainText);
        var after=read(plugin,params);
        if (!verify(before,after,params)) throw Error("Native lyric setter failed isolated text/appearance/measure verification");
        started=false;score.endCmd(false);committed=true;
        after=read(plugin,params);
        if (!verify(before,after,params)) return failure(params,"verification_failed","Committed lyric edit failed final verification","applied_unverified",before.event,after.event);
        return success(params,before,after,"applied");
    } catch (error) {
        var status=committed ? "applied_unverified" : "unknown",actual=null,cancelled=false;
        try {
            if (started) {started=false;score.endCmd(true);cancelled=true;}
            actual=read(plugin,params);
            if (cancelled && Duration.measureKey(before.measure)===Duration.measureKey(actual.measure)
                    && ChordTone.stable(before.native)===ChordTone.stable(actual.native)
                    && before.spans===actual.spans) status="rolled_back";
        } catch (rollbackError) {if (!committed) status="unknown";}
        return failure(params,"verification_failed",String(error),status,before.event,actual ? actual.event : null);
    }
}
