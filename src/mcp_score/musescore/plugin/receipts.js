// State belongs to a plugin root instance, never this shared library module.
.pragma library

var receiptVersion = 1;
var capacity = 64;
var maxPayloadUnits = 65536;
var maxReplyUnits = 262144;
var maxDepth = 32;
var maxNodes = 16384;
var maxExactInteger = 9007199254740991;

function newState() {
    // Qt QV4 Math.random uses QRandomGenerator::global(). Not an auth token.
    var random = "";
    for (var index = 0; index < 8; index++) {
        var word = Math.floor(Math.random() * 65536).toString(16);
        random += ("0000" + word).slice(-4);
    }
    return {sessionId: Date.now().toString(16) + "-" + random,
        entries: Object.create(null), size: 0};
}

function capabilities(plugin) {
    return {protocolVersion: receiptVersion, pluginSession: plugin.receiptState.sessionId,
        supportedMutations: ["setNotePitch"], capacity: capacity,
        maxPayloadUnits: maxPayloadUnits, maxReplyUnits: maxReplyUnits,
        maxDepth: maxDepth, maxNodes: maxNodes, retention: "plugin_session_non_evicting"};
}

function failure(params, code, message) {
    return {error: message, code: code,
        operation_id: params.operationId, expected_plugin_session: params.expectedPluginSession,
        this_attempt_status: "not_executed", prior_outcome: "not_established"};
}

function identityError(plugin, params) {
    if (params.receiptVersion !== receiptVersion) {
        return failure(params, "receipt_version_mismatch", "Receipt protocol version 1 is required; upgrade the server/plugin together");
    }
    var idMatch = typeof params.operationId === "string"
        ? params.operationId.match(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/) : null;
    if (!idMatch || idMatch[0] !== params.operationId || params.operationId.length > 128) {
        return failure(params, "invalid_operation_id", "Supply and retain a valid operation ID before the first send");
    }
    if (!plugin.receiptState || params.expectedPluginSession !== plugin.receiptState.sessionId) {
        return failure(params, "stale_plugin_session", "Plugin session changed or is unavailable; prior operation outcome cannot be established here");
    }
    return null;
}

function reason(prefix, error) {
    var message = "unavailable cause";
    try { message = String(error); } catch (unprintable) {}
    return prefix + ": " + message.slice(0, 256);
}

// Canonicalize JSON without hashes or silent number/undefined coercion. Bounds
// apply while constructing the string, before reservation or native execution.
function canonical(value, limit) {
    var parts = [];
    var units = 0;
    var nodes = 0;
    function append(text) {
        units += text.length;
        if (units > limit) throw Error("JSON size limit exceeded");
        parts.push(text);
    }
    function visit(item, depth) {
        if (++nodes > maxNodes || depth > maxDepth) throw Error("JSON structure limit exceeded");
        if (item === null) { append("null"); return; }
        var type = typeof item;
        if (type === "string" || type === "boolean") { append(JSON.stringify(item)); return; }
        if (type === "number") {
            if (!isFinite(item) || Math.floor(item) !== item || Math.abs(item) > maxExactInteger) {
                throw Error("Only finite safe-integer JSON numbers are supported");
            }
            append(JSON.stringify(item)); return;
        }
        if (type !== "object") throw Error("Non-JSON value");
        if (Array.isArray(item)) {
            append("[");
            for (var index = 0; index < item.length; index++) {
                if (index) append(",");
                visit(item[index], depth + 1);
            }
            append("]");
        } else {
            var keys = Object.keys(item).sort();
            if (keys.length > maxNodes) throw Error("JSON structure limit exceeded");
            append("{");
            for (var keyIndex = 0; keyIndex < keys.length; keyIndex++) {
                if (keyIndex) append(",");
                append(JSON.stringify(keys[keyIndex])); append(":");
                visit(item[keys[keyIndex]], depth + 1);
            }
            append("}");
        }
    }
    visit(value, 0);
    return parts.join("");
}

function result(plugin, operationId, entry, cached) {
    return {result: {pluginSession: plugin.receiptState.sessionId,
        operationId: operationId, status: entry ? entry.status : "not_found",
        command: entry ? "setNotePitch" : null, cached: cached, historical: true,
        originalReply: entry && entry.replyJson !== null ? JSON.parse(entry.replyJson) : null,
        reason: entry ? entry.reason : null}};
}

function readMutationReceipt(plugin, params) {
    var error = identityError(plugin, params);
    if (error) return error;
    var entries = plugin.receiptState.entries;
    var entry = Object.prototype.hasOwnProperty.call(entries, params.operationId)
        ? entries[params.operationId] : null;
    return result(plugin, params.operationId, entry, true);
}

function executeMutationOnce(plugin, params) {
    var error = identityError(plugin, params);
    if (error) return error;
    var mutation = params.mutation;
    if (!mutation || typeof mutation !== "object" || Array.isArray(mutation)
            || Object.keys(mutation).sort().join(",") !== "kind,request"
            || mutation.kind !== "set_note_pitch"
            || !mutation.request || typeof mutation.request !== "object"
            || Array.isArray(mutation.request)
            || !Object.prototype.hasOwnProperty.call(plugin.commands, "setNotePitch")) {
        return failure(params, "unsupported_receipted_mutation", "Only an explicit guarded set_note_pitch request is supported");
    }
    var payload;
    try { payload = canonical({command: "setNotePitch", params: mutation.request}, maxPayloadUnits); }
    catch (invalid) { return failure(params, "invalid_receipt_payload", "Payload is not bounded, safe-integer JSON"); }
    var state = plugin.receiptState;
    if (Object.prototype.hasOwnProperty.call(state.entries, params.operationId)) {
        var previous = state.entries[params.operationId];
        if (previous.payload !== payload) {
            return failure(params, "operation_id_conflict", "Operation ID already belongs to a different payload; the original receipt is retained");
        }
        return result(plugin, params.operationId, previous, true);
    }
    if (state.size >= capacity) {
        return failure(params, "receipt_ledger_full", "Receipt ledger is full; existing receipts remain available, new operations cannot execute");
    }
    // Reservation precedes every handler call. Entries are never evicted.
    var entry = {payload: payload, status: "pending", replyJson: null, reason: null};
    state.entries[params.operationId] = entry;
    state.size++;
    var reply;
    try { reply = plugin.commands.setNotePitch(plugin, mutation.request); }
    catch (thrown) {
        entry.status = "unknown";
        entry.reason = reason("handler_threw", thrown);
        return result(plugin, params.operationId, entry, false);
    }
    try {
        if (!reply || typeof reply !== "object" || Array.isArray(reply)) throw Error("Invalid reply");
        canonical(reply, maxReplyUnits);
        var serialized = JSON.stringify(reply);
        if (typeof serialized !== "string" || serialized.length > maxReplyUnits) throw Error("Invalid reply");
        entry.replyJson = serialized;
        entry.status = "completed";
    } catch (serializationFailure) {
        entry.status = "unknown";
        entry.reason = reason("reply_not_recordable", serializationFailure);
    }
    return result(plugin, params.operationId, entry, false);
}
