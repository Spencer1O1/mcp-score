// processSequence: several commands in one undo step.
.pragma library
.import "score.js" as Score
.import "reading.js" as Reading
.import "editing.js" as Editing
.import "selection.js" as Selection

// What each step runs. Editing steps use the `apply*` form so the whole
// sequence shares the caller's undo step.
var steps = {
    "ping":                 Reading.ping,
    "goToMeasure":          Reading.goToMeasure,
    "goToStaff":            Reading.goToStaff,
    "addNote":              Editing.applyAddNote,
    "addRehearsalMark":     Editing.applyAddRehearsalMark,
    "setBarline":           Editing.applySetBarline,
    "setKeySignature":      Editing.applySetKeySignature,
    "setTimeSignature":     Editing.applySetTimeSignature,
    "setTempo":             Editing.applySetTempo,
    "addChordSymbol":       Editing.applyAddChordSymbol,
    "addDynamic":           Editing.applyAddDynamic,
    "appendMeasures":       Editing.applyAppendMeasures,
    "selectCurrentMeasure": Selection.applySelectCurrentMeasure,
    "selectCustomRange":    Selection.applySelectCustomRange,
    "transpose":            Selection.applyTranspose
};

/// Execute one step inside the caller's undo step.
function executeStep(plugin, action, params) {
    if (!Object.prototype.hasOwnProperty.call(steps, action)) {
        return { error: "Unknown action in sequence: " + action };
    }
    return steps[action](plugin, params);
}

/// Execute multiple actions in a single undo step.
/// Validate every step's structure before execution. Argument semantics may
/// depend on earlier steps (for example navigating to an appended measure).
/// Failures request native score rollback and restore the logical cursor.
///
/// Params: { sequence: [{ action, params }, ...] }
function processSequence(plugin, params) {
    var scoreErr = Score.requireScore(plugin);
    if (scoreErr) return scoreErr;

    if (!params || typeof params !== "object" || !Array.isArray(params.sequence)) {
        return validationFailure("Missing required parameter: sequence (array of {action, params})");
    }
    var sequence = params.sequence;
    if (sequence.length === 0) {
        return { result: { results: [], count: 0 } };
    }

    for (var index = 0; index < sequence.length; index++) {
        var candidate = sequence[index];
        var error = null;
        if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
            error = "must be an object";
        } else if (typeof candidate.action !== "string" || candidate.action.length === 0) {
            error = "must have a nonempty string 'action' field";
        } else if (!Object.prototype.hasOwnProperty.call(steps, candidate.action)) {
            error = "Unknown action in sequence: " + candidate.action;
        } else if (candidate.params !== undefined && (!candidate.params
                || typeof candidate.params !== "object" || Array.isArray(candidate.params))) {
            error = "'params' must be an object when supplied";
        }
        if (error !== null) {
            var rejected = validationFailure("Step " + index + " " + error);
            rejected.failedIndex = index;
            if (candidate && typeof candidate.action === "string") {
                rejected.failedAction = candidate.action;
            }
            return rejected;
        }
    }

    var savedMeasure = plugin.cursorMeasure;
    var savedStaff = plugin.cursorStaff;
    var savedVoice = plugin.cursorVoice;
    var committed = false;

    try {
        var response = Score.withUndoStep(plugin, "processSequence", function() {
            var results = [];
            for (var i = 0; i < sequence.length; i++) {
                var step = sequence[i];
                var action = step.action;
                var stepResult;
                try {
                    stepResult = executeStep(plugin, action, step.params === undefined ? {} : step.params);
                    if (!stepResult || typeof stepResult !== "object" || Array.isArray(stepResult)) {
                        throw new Error("Action returned an invalid response");
                    }
                } catch (e) {
                    stepResult = { error: e && e.message ? e.message : String(e) };
                }
                if (stepResult.error !== undefined) {
                    return {
                        error: "Step " + i + " (" + action + ") failed: " + stepResult.error,
                        failedAction: action,
                        failedIndex: i,
                        results: results
                    };
                }
                results.push(stepResult.result);
            }
            return { result: { results: results, count: results.length } };
        });

        committed = response.error === undefined;
        if (!committed) {
            // endCmd(true) returned, but no complete content read-back was done.
            response.mutationStatus = "unknown";
            response.rollbackStatus = "requested";
        }
        return response;
    } catch (exception) {
        // A transaction-boundary exception cannot confirm commit or rollback.
        return {
            error: exception && exception.message ? exception.message : String(exception),
            mutationStatus: "unknown",
            rollbackStatus: "unknown"
        };
    } finally {
        if (!committed) {
            plugin.cursorMeasure = savedMeasure;
            plugin.cursorStaff = savedStaff;
            plugin.cursorVoice = savedVoice;
        }
    }
}

function validationFailure(message) {
    return { error: message, mutationStatus: "not_applied", rollbackStatus: "not_needed" };
}
