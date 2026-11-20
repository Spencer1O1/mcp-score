const { LyricText, Receipts, LyricTextFixture } = modules;
current = chord(1920);
let text = "keep chord";
const lyricControls = { ignore: false, fail: false, style: false, peer: false };
const lyric = {
    type: 101,
    verse: 0,
    syllabic: 0,
    placement: 1,
    lyricTicks: frac(0, 1),
    isMelisma: false,
    separator: null,
    fontFace: "Edwin",
    fontSize: 11.25,
    lineSpacing: 1,
    offsetX: 0,
    offsetY: 0,
    subStyle: 11,
    fontStyle: 0,
    align: [0, 0],
    textScriptAlign: 0,
    position: 0,
    frameType: 0,
    visible: true,
    autoplace: true,
    sizeSpatiumDependent: true,
    avoidBarLines: true,
    textLinkedToMaster: true,
    appearanceLinkedToMaster: true,
    positionLinkedToMaster: true,
    excludeFromParts: false,
    color: "#000000",
    get text() {
        return text;
    },
    set text(value) {
        if (lyricControls.fail) throw Error("text setter failed");
        if (lyricControls.ignore) return;
        text = value;
        if (lyricControls.style) this.fontSize++;
        if (lyricControls.peer) peer.notes[0].pitch++;
    },
    get plainText() {
        return text
            .replace(/&lt;/g, "<")
            .replace(/&gt;/g, ">")
            .replace(/&quot;/g, '"')
            .replace(/&amp;/g, "&");
    },
};
current.lyrics = [lyric];
rebuild();
plugin.Element.LYRICS = 101;
plugin.Lyrics = { SINGLE: 0 };
plugin.Placement = { ABOVE: 0, BELOW: 1 };
score.excerpts = [];
const nativeStart = score.startCmd,
    nativeEnd = score.endCmd;
let savedLyric;
score.startCmd = function () {
    nativeStart.call(this);
    savedLyric = { text, fontSize: lyric.fontSize };
};
score.endCmd = function (cancel) {
    nativeEnd.call(this, cancel);
    if (cancel) {
        text = savedLyric.text;
        lyric.fontSize = savedLyric.fontSize;
    }
};
params.lyricIndex = 0;
params.plainText = 'Sing & <b>"café"</b> \'🙂';
delete params.duration;
params.expected = snapshot()[0];
plugin.commands = { setLyricText: LyricText.setLyricText };
plugin.receiptState = Receipts.newState();
plugin.receiptState.sessionId = "test-session";
function runLyric() {
    return {
        reply: LyricText.setLyricText(plugin, params),
        starts,
        cancels,
        history,
        cursor: [plugin.cursorMeasure, plugin.cursorStaff, plugin.cursorVoice],
        text,
    };
}
