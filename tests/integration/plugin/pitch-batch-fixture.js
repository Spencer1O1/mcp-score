// Fixed disposable test setup; never bundled with the production plugin.
.pragma library

function arm(plugin, params) {
    if (Object.keys(params).length || !plugin.curScore
            || plugin.curScore.title !== "Guarded pitch fixture") {
        return {error: "Fixed batch fault requires the disposable pitch fixture and no parameters"};
    }
    plugin.pitchBatchFault = {armed: true, observed: null};
    return read(plugin, {});
}

function read(plugin, params) {
    if (Object.keys(params).length) return {error: "No fault parameters are accepted"};
    return {result: plugin.pitchBatchFault || {armed: false, observed: null}};
}
