"""A fake Onshape REST server on 127.0.0.1 (threaded ``http.server``) for the tests and the
offline example.  It answers the endpoints the plugin uses with JSON shaped like Onshape's
OpenAPI schemas and records every request (path, query, headers)."""
from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import models as MD

API = "/api/v17"
DID, WID, AID, PSID = "d0c0000000000000000000a1", "w0rk0000000000000000000a", "a55e0000000000000000000a", "p5d0000000000000000000a1"
LINKED_DID, LINKED_VID = "11nked00000000000000000a", "ver0000000000000000000a1"

HEADERS = [
    {"id": "57f3fb8efa3416c06701d60d", "name": "Name", "propertyName": "name", "visible": True},
    {"id": "5ace84d3c046ad611c65a0dd", "name": "Quantity", "propertyName": "quantity", "visible": True},
    {"id": "57f3fb8efa3416c06701d60f", "name": "Part number", "propertyName": "partNumber", "visible": True},
    {"id": "57f3fb8efa3416c06701d615", "name": "Material", "propertyName": "material", "visible": True},
    {"id": "cp000000000000000000proc", "name": "Process", "propertyName": "Process", "visible": False},
    {"id": "cp00000000000000000proc2", "name": "Process 2", "propertyName": "Process 2", "visible": False},
    {"id": "cp000000000000000000prep", "name": "Pre Process", "propertyName": "Pre Process", "visible": False},
]
H = {h["name"]: h["id"] for h in HEADERS}


def _row(name, qty, pn, material, pid, process="", process2="", pre="", cfg="", did=DID, wvm="w", wvmid=WID,
         eid=PSID, standard=False):
    vals = {H["Name"]: name, H["Quantity"]: qty, H["Part number"]: pn,
            H["Material"]: {"displayName": material, "id": material, "libName": "Onshape Material Library"} if material else None,
            H["Process"]: process, H["Process 2"]: process2, H["Pre Process"]: pre}
    return {"id": f"row-{pid}-{cfg}", "rowId": f"row-{pid}-{cfg}", "indentLevel": 0, "headerIdToValue": vals,
            "itemSource": {"documentId": did, "wvmType": wvm, "wvmId": wvmid, "elementId": eid, "partId": pid,
                           "configuration": cfg, "fullConfiguration": cfg, "isStandardContent": standard}}


class World:
    """The fake account: documents, one assembly and its parts."""

    def __init__(self):
        self.microversion = "mv0001"
        self.parts = {}            # (pid, configuration) -> Builder
        b, *_ = MD.plate_with_holes()
        self.parts[("JHD", "")] = b
        b, *_ = MD.plate_with_slot()
        self.parts[("JSD", "")] = b
        b, *_ = MD.pocketed_plate()
        self.parts[("JPD", "")] = b
        b, *_ = MD.side_plate()
        self.parts[("JXD", "")] = b
        b, *_ = MD.spline_plate()
        self.parts[("JBD", "")] = b
        b, *_ = MD.ellipse_and_other_plate()
        self.parts[("JED", "")] = b
        self.parts[("JCD", "Length=100+mm")] = MD.configured_plate(100.0)
        self.parts[("JCD", "Length=150+mm")] = MD.configured_plate(150.0)
        self.parts[("JRD", "")] = MD.round_bar()
        self.parts[("JLD", "")] = MD.plate_with_holes(t=2.0, w=50, h=50, r=3, material="Steel 2mm")[0]
        self.rows = [
            _row("Side plate", 2, "2026-01-001", "Aluminum - 6061", "JHD", process="Laser"),
            _row("Slot plate", 1, "", "Aluminum 5052 1/8\"", "JSD", process="laser"),
            _row("Pocket plate", 4, "2026-01-003", "6061-T6 1/4\"", "JPD", process="CNC mill"),
            _row("Bracket", 2, "2026-01-004", "Steel 4mm", "JXD", process="Fiber laser"),
            _row("Quarter", 1, "", "Aluminum 2mm", "JBD", process="Laser"),
            _row("Cam plate", 1, "", "Mild steel", "JED", process="Laser"),
            _row("Spacer 100", 3, "SP-100", "Aluminum - 6061", "JCD", process="Laser", cfg="Length=100+mm"),
            _row("Spacer 150", 1, "SP-150", "Aluminum - 6061", "JCD", process="Laser", cfg="Length=150+mm"),
            _row("Shaft", 2, "", "Steel", "JRD", process="Laser"),
            _row("Gusset", 6, "", "Steel 2mm", "JLD", process="Lathe", process2="Laser"),
            _row("Bearing", 4, "", "", "JBR", standard=True),
            _row("Linked plate", 1, "", "Steel 2mm", "JLD", process="Laser", did=LINKED_DID, wvm="v",
                 wvmid=LINKED_VID),
        ]
        self.requests = []          # (method, path, query dict, headers dict)
        self.fail_429 = 0           # answer the next n requests with 429
        self.retry_after = "0"
        self.lock = threading.Lock()
        self.auth = ("on_testaccesskey", "secret-key-value")

    def bom(self):
        return {"btType": "BTBillOfMaterialsInfo", "formatVersion": "1.0", "name": "Robot BOM", "headers": HEADERS,
                "rows": self.rows, "bomSource": {"document": {"id": DID, "name": "Robot 2026"},
                                                 "element": {"id": AID, "name": "Robot"}}}


class Handler(BaseHTTPRequestHandler):
    world: World = None  # type: ignore[assignment]

    def log_message(self, *a):           # quiet
        pass

    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server API
        w = self.world
        u = urlsplit(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        with w.lock:
            w.requests.append(("GET", u.path, q, dict(self.headers)))
            if w.fail_429 > 0:
                w.fail_429 -= 1
                return self._send(429, {"message": "Too many requests", "status": 429},
                                  {"Retry-After": w.retry_after})
        auth = self.headers.get("Authorization", "")
        want = "Basic " + base64.b64encode(f"{w.auth[0]}:{w.auth[1]}".encode()).decode()
        if auth != want:
            return self._send(401, {"message": "Unauthenticated API request", "status": 401})
        if not u.path.startswith(API):
            return self._send(404, {"message": "no such API"})
        p = u.path[len(API):]
        cfg = (q.get("configuration") or [""])[0]
        m = re.fullmatch(r"/parts/d/([^/]+)/([wvm])/([^/]+)/e/([^/]+)/partid/([^/]+)/(\w+)", p)
        if m:
            pid, what = m.group(5), m.group(6)
            key = (pid, cfg.replace(" ", "+"))
            b = w.parts.get(key) or w.parts.get((pid, ""))
            if b is None:
                return self._send(404, {"message": "part not found"})
            if what == "bodydetails":
                return self._send(200, b.response(pid))
            if what == "massproperties":
                return self._send(200, b.mass(pid))
            if what == "tessellatededges":
                return self._send(200, b.tessellation(pid, q.get("edgeId")))
            if what == "shadedviews":
                png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
                return self._send(200, {"images": [png]})
            return self._send(404, {"message": what})
        if p == "/users/sessioninfo":
            return self._send(200, {"id": "u1", "name": "Test Team", "email": "team@example.org"})
        if p == "/documents":
            items = [{"jsonType": "folder", "id": "f0lder", "name": "Robots", "isContainer": True},
                     {"jsonType": "document-summary", "id": DID, "name": "Robot 2026", "isContainer": False,
                      "defaultWorkspace": {"id": WID, "name": "Main"}, "modifiedAt": "2026-10-01T10:00:00Z"}]
            if q.get("parentId", [""])[0] == "f0lder":
                items = [{"jsonType": "document-summary", "id": DID, "name": "Robot 2026 (in folder)",
                          "defaultWorkspace": {"id": WID, "name": "Main"}}]
            qq = (q.get("q") or [""])[0].lower()
            if qq:
                items = [i for i in items if qq in i["name"].lower()]
            return self._send(200, {"items": items, "next": None, "href": self.path})
        if re.fullmatch(r"/documents/[^/]+", p):
            return self._send(200, {"id": p.split("/")[-1], "name": "Robot 2026", "defaultWorkspace": {"id": WID}})
        if re.fullmatch(r"/documents/d/[^/]+/workspaces", p):
            return self._send(200, [{"id": WID, "name": "Main", "microversion": w.microversion}])
        if re.fullmatch(r"/documents/d/[^/]+/versions", p):
            return self._send(200, [{"id": "v1", "name": "V1 competition", "microversion": "mvV1"}])
        if re.fullmatch(r"/documents/d/[^/]+/[wv]/[^/]+/currentmicroversion", p):
            return self._send(200, {"microversion": w.microversion})
        if re.fullmatch(r"/documents/d/[^/]+/[wvm]/[^/]+/elements", p):
            return self._send(200, [{"id": AID, "name": "Robot", "elementType": "ASSEMBLY", "type": "Assembly"},
                                    {"id": "a55e2", "name": "Intake", "elementType": "ASSEMBLY", "type": "Assembly"}])
        if re.fullmatch(r"/assemblies/d/[^/]+/[wvm]/[^/]+/e/[^/]+/bom", p):
            return self._send(200, w.bom())
        return self._send(404, {"message": f"unknown {p}"})


class FakeOnshape:
    def __init__(self):
        self.world = World()
        handler = type("H", (Handler,), {"world": self.world})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self, contains=""):
        return [r[1] for r in self.world.requests if contains in r[1]]
