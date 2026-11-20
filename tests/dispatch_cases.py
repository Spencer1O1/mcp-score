"""Malformed wire envelopes shared by dispatcher and real WebSocket probes."""

from __future__ import annotations

INVALID_ENVELOPES: list[tuple[str, str]] = [
    ("{", "Invalid JSON:"),
    ("null", "Invalid request:"),
    ("[]", "Invalid request:"),
    ('"ping"', "Invalid request:"),
    ("true", "Invalid request:"),
    ("0", "Invalid request:"),
    ("{}", "'command' field"),
    ('{"command":null}', "'command' field"),
    ('{"command":false}', "'command' field"),
    ('{"command":0}', "'command' field"),
    ('{"command":[]}', "'command' field"),
    ('{"command":["ping"]}', "'command' field"),
    ('{"command":{}}', "'command' field"),
    ('{"command":""}', "'command' field"),
    ('{"command":"toString"}', "Unknown command:"),
    ('{"command":"constructor"}', "Unknown command:"),
    ('{"command":"__proto__"}', "Unknown command:"),
    ('{"command":"hasOwnProperty"}', "Unknown command:"),
    ('{"command":"__defineGetter__"}', "Unknown command:"),
    ('{"command":"futureCommand"}', "Unknown command:"),
    ('{"command":"ping","params":null}', "'params' field"),
    ('{"command":"ping","params":[]}', "'params' field"),
    ('{"command":"ping","params":false}', "'params' field"),
    ('{"command":"ping","params":0}', "'params' field"),
    ('{"command":"ping","params":""}', "'params' field"),
    ('{"command":"ping","params":"bad"}', "'params' field"),
]
