// The handshake is independent of whether a score is open.
.pragma library
.import "sequence.js" as Sequence

var protocolVersion = 1;

function getCapabilities(plugin, params) {
    if (params.protocolVersion !== protocolVersion) {
        return { error: "Unsupported bridge protocol version: " + params.protocolVersion
            + "; plugin requires " + protocolVersion };
    }
    return {
        result: {
            protocolVersion: protocolVersion,
            pluginVersion: plugin.version,
            commands: Object.keys(plugin.commands).sort(),
            sequenceCommands: Object.keys(Sequence.steps).sort()
        }
    };
}
