"""OpenAPI 3.1 description of the automation API (served at ``/api/v1/openapi.json``; the
reference with examples is docs/dev/API.md)."""
from __future__ import annotations

from typing import Any

VERSION = "1.0.0"

_ERROR = {"type": "object", "required": ["error"], "properties": {"error": {
    "type": "object", "required": ["code", "message"],
    "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}

_EVENT = {"type": "object", "required": ["seq", "t", "type"], "properties": {
    "seq": {"type": "integer", "description": "increasing per server run"},
    "t": {"type": "number", "description": "Unix time"},
    "type": {"type": "string", "enum": [
        "job_started", "job_finished", "alarm", "alarm_cleared", "state", "armed", "disarmed", "breakpoint",
        "queue_changed", "sheet_done", "queue_waiting", "intake_submitted", "intake_approved",
        "intake_rejected", "remnant_saved"]},
    "source": {"type": "string", "enum": ["real", "simulator", "dry_run", "practice"],
               "description": "job events: where the job ran"}},
    "additionalProperties": True}

_PENDING = {"type": "object", "properties": {
    "id": {"type": "string"}, "name": {"type": "string"}, "file": {"type": "string"},
    "quantity": {"type": "integer"}, "material": {"type": "string"}, "thickness_mm": {"type": "number"},
    "sheet": {"type": "string"}, "note": {"type": "string"}, "reference": {"type": "string"},
    "source": {"type": "string"}, "client": {"type": "string"}, "submitted": {"type": "number"},
    "status": {"type": "string", "const": "pending_approval"}}}

_SUBMIT = {"type": "object", "additionalProperties": False, "properties": {
    "path": {"type": "string", "description": "a .chf / .dxf / .svg file inside one of the API's job folders"},
    "filename": {"type": "string", "description": "with content_base64: the uploaded drawing's name (.chf/.dxf/.svg)"},
    "content_base64": {"type": "string", "contentEncoding": "base64"},
    "name": {"type": "string", "maxLength": 120}, "quantity": {"type": "integer", "minimum": 1, "maximum": 9999},
    "material": {"type": "string", "maxLength": 80}, "thickness_mm": {"type": "number", "minimum": 0, "maximum": 200},
    "sheet": {"type": "string", "maxLength": 80}, "note": {"type": "string", "maxLength": 500},
    "reference": {"type": "string", "maxLength": 120}}}


def _resp(desc: str, schema: Any = None) -> dict:
    r: dict = {"description": desc}
    if schema is not None:
        r["content"] = {"application/json": {"schema": schema}}
    return r


def openapi(queue_intake: bool = True) -> dict:
    """The description; ``queue_intake`` False documents that POST /queue answers 403."""
    err = {"401": _resp("missing or wrong token", _ERROR), "403": _resp("refused (see the code)", _ERROR),
           "429": _resp("rate limit", None)}
    paths: dict = {
        "/health": {"get": {"summary": "Reachability (no token, no data)", "security": [],
                            "responses": {"200": _resp("up", {"type": "object"})}}},
        "/openapi.json": {"get": {"summary": "This description", "security": [],
                                  "responses": {"200": _resp("OpenAPI 3.1", {"type": "object"})}}},
        "/status": {"get": {"summary": "Machine, job and queue status (read-only)",
                            "description": "What the program published last (about once a second). "
                                           "`remote_control` is always false.",
                            "responses": {"200": _resp("status", {"type": "object"}), **err}}},
        "/queue": {
            "get": {"summary": "Queue entries and jobs waiting for approval",
                    "responses": {"200": _resp("queue", {"type": "object", "properties": {
                        "entries": {"type": "array", "items": {"type": "object"}},
                        "pending_approval": {"type": "array", "items": _PENDING},
                        "queue_intake": {"type": "boolean"}}}), **err}},
            "post": {"summary": "Submit a job; it waits for the operator's approval at the machine",
                     "description": "Never starts anything. The operator approves it on the Run screen "
                                    "(Queue > Approve); even then it runs only after 'Sheet loaded: start'. "
                                    "A submission carries no cutting parameters. "
                                    + ("" if queue_intake else "Queue intake is switched off on this machine: "
                                       "403 queue_intake_off."),
                     "requestBody": {"required": True, "content": {"application/json": {"schema": _SUBMIT}}},
                     "responses": {"202": _resp("pending approval", _PENDING),
                                   "400": _resp("invalid submission", _ERROR), "404": _resp("file not found", _ERROR),
                                   "411": _resp("Content-Length required", _ERROR),
                                   "413": _resp("too large", _ERROR), "415": _resp("not JSON", _ERROR), **err}}},
        "/events": {"get": {"summary": "Events after a sequence number (long poll)",
                            "parameters": [
                                {"name": "after", "in": "query", "schema": {"type": "integer", "default": 0}},
                                {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100,
                                                                            "maximum": 500}},
                                {"name": "wait", "in": "query", "schema": {"type": "number", "default": 0,
                                                                           "maximum": 20}}],
                            "responses": {"200": _resp("events", {"type": "object", "properties": {
                                "events": {"type": "array", "items": _EVENT}, "next": {"type": "integer"},
                                "lost": {"type": "integer"}}}), **err}}},
        "/ws": {"get": {"summary": "WebSocket event stream (read-only)",
                        "description": "Upgrade to a WebSocket (RFC 6455). The first message is "
                                       "{type: hello, seq, status}; then one JSON event per text frame. "
                                       "Client data frames are ignored. Token: Authorization header or "
                                       "?token=. At most 4 streams; closed after an hour (reconnect).",
                        "parameters": [{"name": "token", "in": "query", "schema": {"type": "string"}}],
                        "responses": {"101": _resp("switching protocols"), "426": _resp("not a WebSocket", _ERROR),
                                      "503": _resp("too many streams", _ERROR), **err}}},
    }
    return {
        "openapi": "3.1.0",
        "info": {"title": "mlaser automation API", "version": VERSION, "license": {"name": "MIT"},
                 "description": "Read-only status and events of one mlaser machine PC, plus queue intake that "
                                "needs the operator's approval. There is no remote start, stop, hold, arm, "
                                "move or approval; see docs/dev/API.md."},
        "servers": [{"url": "/api/v1"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
                       "schemas": {"Error": _ERROR, "Event": _EVENT, "PendingJob": _PENDING, "Submission": _SUBMIT}},
        "security": [{"bearer": []}],
        "paths": paths,
    }


__all__ = ["openapi", "VERSION"]
