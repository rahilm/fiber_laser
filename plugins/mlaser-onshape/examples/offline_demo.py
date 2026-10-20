"""Offline demo: the whole Onshape -> DXF -> order lines path against the fake Onshape server
of the tests (127.0.0.1 only, no account needed).

    python plugins/mlaser-onshape/examples/offline_demo.py [output folder]

It starts the fake server, reads the example robot's bill of materials, keeps the parts whose
Process is "Laser" or "Fiber laser" (2 robots, 1 spare each), writes their DXFs and prints the
order lines and the material x thickness groups.  The DXFs land in the output folder
(default: a new temporary folder).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))

import fake_onshape as F  # noqa: E402
from stubctx import StubCtx  # noqa: E402

from mlaser_onshape.bom import MachineConfig  # noqa: E402
from mlaser_onshape.service import OnshapeService  # noqa: E402


def main(out: Path) -> int:
    with F.FakeOnshape() as fake:
        ctx = StubCtx(out)
        svc = OnshapeService(ctx)
        svc.set("server", fake.url)
        svc.save_keys(*fake.world.auth)
        svc.machine = MachineConfig(values=["Laser", "Fiber laser"], slots=["Process", "Process 2"], robots=2, spares=1)
        for label, tech in (("Aluminum - 6061", "Aluminium AlMg3"), ('Aluminum 5052 1/8"', "Aluminium AlMg3"),
                            ("Aluminum 2mm", "Aluminium AlMg3"), ("Steel 4mm", "Mild steel S235"),
                            ("Mild steel", "Mild steel S235"), ("Steel 2mm", "Mild steel S235")):
            svc.map_material(label, tech)
        print("connected as", svc.test_connection())
        bom = svc.load_bom(F.DID, "w", F.WID, F.AID, "Robot", "Robot 2026")
        plans = svc.prepare(bom.parts)
        for p in plans:
            t = f"{p.thickness:.2f} mm" if p.thickness else ""
            print(f"{p.part.name:14} {p.status:18} cut {p.to_cut:3}  {p.sheet_material:18} {t:9} {p.face_label}")
        added, _ = svc.create_orders(plans)
        print(f"\n{added} order lines; DXFs in {ctx.data_dir / 'dxf'}")
        for g in svc.groups(plans):
            print(f"  {g.title:26} {g.parts} parts, {g.quantity} pieces, ~{g.sheets} sheet(s) "
                  f"of {svc.get('sheet_w'):g} x {svc.get('sheet_h'):g} mm")
        print(f"API calls: {svc.calls}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="onshape-demo-"))))
