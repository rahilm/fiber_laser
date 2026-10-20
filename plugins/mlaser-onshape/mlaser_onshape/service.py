"""The plugin's state and actions without Qt: settings, the client, browse, prepare, orders.

Everything here may run on a worker thread (``ctx.run_in_background``) except
:meth:`OnshapeService.load_into_nest`, which runs the Orders command on the GUI thread.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Optional

from mlaser.services.config import Setting

from . import pipeline as PL
from .bom import DEFAULT_PROCESS_PROPS, BomPart, BomResult, MachineConfig, parse_bom
from .client import DEFAULT_SERVER, OnshapeClient, OnshapeError, check_server
from .materials import MaterialMap

SECRET_ACCESS = "access_key"
SECRET_SECRET = "secret_key"

SETTINGS = [
    Setting("server", "str", DEFAULT_SERVER, label="Onshape server"),
    Setting("process_props", "json", list(DEFAULT_PROCESS_PROPS), label="Process properties (in step order)"),
    Setting("machine", "json", MachineConfig().to_json(), label="Machine type"),
    Setting("material_map", "json", {}, label="Material mapping"),
    Setting("face_overrides", "json", {}, label="Saved DXF faces"),
    Setting("made", "json", {}, label="Already made per part"),
    Setting("thickness_tol", "float", 0.1, unit="mm", min=0.0, max=5.0, label="Thickness tolerance"),
    Setting("cross_check_volume", "bool", True, label="Cross-check thickness with mass properties"),
    Setting("sheet_w", "float", 2500.0, unit="mm", min=10.0, max=20000.0, label="Sheet length"),
    Setting("sheet_h", "float", 1250.0, unit="mm", min=10.0, max=20000.0, label="Sheet width"),
    Setting("utilisation", "float", 0.7, min=0.1, max=1.0, label="Expected sheet use"),
    Setting("customer", "str", "", label="Customer / team (empty: the document name)"),
    Setting("recent", "json", [], label="Recent documents"),
]


class OnshapeService:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.settings = ctx.settings_schema(SETTINGS)
        self._client: Optional[OnshapeClient] = None
        self._client_key: tuple = ()
        self.lock = threading.Lock()
        self.bom: Optional[BomResult] = None
        self.plans: dict[str, PL.PartPlan] = {}
        self.cancel = threading.Event()

    # ------------------------------------------------------------------ settings
    def get(self, key: str) -> Any:
        return self.settings[key]

    def set(self, key: str, value: Any) -> None:
        self.settings[key] = value

    @property
    def machine(self) -> MachineConfig:
        return MachineConfig.from_json(self.get("machine"))

    @machine.setter
    def machine(self, cfg: MachineConfig) -> None:
        self.set("machine", cfg.to_json())

    @property
    def process_props(self) -> list[str]:
        v = self.get("process_props")
        return [str(x) for x in v if str(x).strip()] if isinstance(v, list) and v else list(DEFAULT_PROCESS_PROPS)

    @property
    def material_map(self) -> MaterialMap:
        return MaterialMap.from_json(self.get("material_map"))

    def map_material(self, label: str, material: str) -> None:
        m = self.material_map
        m.set(label, material)
        self.set("material_map", m.to_json())
        opts = self.options()
        for p in self.plans.values():
            if p.label and MaterialMap.norm(p.label) == MaterialMap.norm(label):
                PL.resolve_material(p, opts)

    def set_face(self, part_key: str, face_id: str) -> None:
        d = dict(self.get("face_overrides") or {})
        if face_id:
            d[part_key] = face_id
        else:
            d.pop(part_key, None)
        self.set("face_overrides", d)

    def set_made(self, part_key: str, n: int) -> None:
        d = dict(self.get("made") or {})
        if n > 0:
            d[part_key] = int(n)
        else:
            d.pop(part_key, None)
        self.set("made", d)

    def tech_names(self) -> list[str]:
        tech = getattr(getattr(self.ctx, "services", None), "technology", None)
        try:
            return list(tech.materials()) if tech is not None else []
        except Exception:  # noqa: BLE001 - the database is optional
            return []

    # ------------------------------------------------------------------ connection
    def keys(self) -> Optional[tuple[str, str]]:
        s = getattr(self.ctx, "secrets", None)
        if s is None:
            return None
        a, b = s.get(SECRET_ACCESS), s.get(SECRET_SECRET)
        return (a, b) if a and b else None

    def save_keys(self, access: str, secret: str) -> None:
        access, secret = access.strip(), secret.strip()
        if access:
            self.ctx.secrets.set(SECRET_ACCESS, access)
        if secret:
            self.ctx.secrets.set(SECRET_SECRET, secret)
        self._client = None

    def forget_keys(self) -> None:
        for k in (SECRET_ACCESS, SECRET_SECRET):
            self.ctx.secrets.delete(k)
        self._client = None

    def set_server(self, url: str) -> str:
        norm = check_server(url)
        self.set("server", norm)
        self._client = None
        return norm

    def client(self) -> OnshapeClient:
        keys = self.keys()
        if keys is None:
            raise OnshapeError("no Onshape API keys yet: enter the access and secret key and save them")
        key = (self.get("server"), keys)
        with self.lock:
            if self._client is None or self._client_key != key:
                self._client = OnshapeClient(getattr(self.ctx, "http", None), self.get("server"), keys,
                                             cache_dir=self.ctx.data_dir / "cache")
                self._client_key = key
            return self._client

    @property
    def calls(self) -> int:
        return self._client.calls if self._client is not None else 0

    def test_connection(self) -> str:
        info = self.client().session()
        return str(info.get("name") or info.get("email") or "connected")

    # ------------------------------------------------------------------ browse
    def documents(self, query: str = "", filter: Optional[int] = None, parent: str = "", project: str = "",
                  offset: int = 0) -> dict:
        return self.client().documents(query, filter, parent, project, offset)

    def remember(self, did: str, name: str) -> None:
        rec = [r for r in (self.get("recent") or []) if isinstance(r, dict) and r.get("id") != did]
        self.set("recent", ([{"id": did, "name": name}] + rec)[:10])

    def workspace_choices(self, did: str, default_wid: str = "") -> list[tuple[str, str, str]]:
        """(label, wvm, id): the workspaces, then the versions (newest first)."""
        c = self.client()
        out = []
        try:
            for w in c.workspaces(did):
                out.append((f"Workspace: {w.get('name', w.get('id'))}", "w", str(w.get("id"))))
        except OnshapeError:
            if default_wid:
                out.append(("Workspace: Main", "w", default_wid))
        if default_wid:
            out.sort(key=lambda x: x[2] != default_wid)
        try:
            for v in reversed(c.versions(did) or []):
                out.append((f"Version: {v.get('name', v.get('id'))}", "v", str(v.get("id"))))
        except OnshapeError:
            pass
        return out

    def assemblies(self, did: str, wvm: str, wvmid: str) -> list[dict]:
        return [e for e in self.client().elements(did, wvm, wvmid, "ASSEMBLY") or []
                if str(e.get("elementType", "ASSEMBLY")).upper() == "ASSEMBLY"]

    def load_bom(self, did: str, wvm: str, wvmid: str, eid: str, assembly: str = "", document: str = "",
                 configuration: str = "") -> BomResult:
        c = self.client()
        mv = c.microversion(did, wvm, wvmid, refresh=True)
        bom = c.bom(did, wvm, wvmid, eid, configuration, mv=mv)
        res = parse_bom(bom, self.process_props, assembly_did=did, assembly=assembly, document=document)
        self.bom = res
        self.plans = {}
        return res

    # ------------------------------------------------------------------ prepare
    def options(self) -> PL.Options:
        return PL.Options(out_dir=self.ctx.data_dir / "dxf", machine=self.machine, process_props=self.process_props,
                          material_map=self.material_map, tech_names=self.tech_names(),
                          face_overrides=dict(self.get("face_overrides") or {}), made=dict(self.get("made") or {}),
                          thickness_tol_mm=float(self.get("thickness_tol")),
                          cross_check_volume=bool(self.get("cross_check_volume")))

    def classify(self, parts: Iterable[BomPart]) -> list[PL.PartPlan]:
        """The machine filter and quantities only: no API call."""
        opts = self.options()
        return [PL.plan_part(None, p, opts) for p in parts]

    def prepare(self, parts: Iterable[BomPart], progress: Callable[[int, int, str], None] = lambda i, n, m: None,
                cancel: Optional[threading.Event] = None) -> list[PL.PartPlan]:
        self.cancel = cancel or threading.Event()
        plans = PL.run(self.client(), parts, self.options(), progress, self.cancel)
        for p in plans:
            self.plans[p.part.key] = p
        try:
            self.client().prune_cache()
        except OSError:
            pass
        return plans

    # ------------------------------------------------------------------ orders
    def create_orders(self, plans: Iterable[PL.PartPlan]) -> tuple[int, int]:
        book = getattr(getattr(self.ctx, "services", None), "orders", None)
        if book is None:
            raise OnshapeError("the Orders service is not available (is the Orders plugin loaded?)")
        lines = PL.order_lines(plans, customer=str(self.get("customer") or ""))
        return PL.add_to_book(book, lines)

    def groups(self, plans: Optional[Iterable[PL.PartPlan]] = None) -> list[PL.GroupSummary]:
        ps = list(plans) if plans is not None else list(self.plans.values())
        return PL.group_summaries(ps, float(self.get("sheet_w")), float(self.get("sheet_h")),
                                  float(self.get("utilisation")))

    def load_into_nest(self, key: tuple[str, float]) -> Any:
        """Load one material x thickness group of the Orders book into Nest (the Orders
        workspace's own command, so the technology is picked the same way)."""
        cmds = getattr(self.ctx, "commands", None)
        if cmds is None:
            raise OnshapeError("no command registry")
        return cmds.run("orders.to_nest", tuple(key))
