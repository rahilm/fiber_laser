"""Craft operations: leads, kerf, micro-joints (Qt-free; moved from ``mlaser.ui.docops`` and
``MainWindow.microjoint_auto`` in Wave 2).

The micro-joint dialog state used to be a loose dict on the main window (``mj_params``) and the
auto-joint loop existed twice (main window and ext_craft's one-key planning); both now use
:class:`MicroJointParams` and :func:`auto_microjoints`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from mlaser.model import Document, LeadParams, MicroJoint

from .document import contour_targets as targets  # raster parts excluded


def default_lead_params() -> tuple[LeadParams, LeadParams]:
    """Lead dialog defaults of a fresh install (O, parity harness ``app_lead``: the original's
    引线 dialog with no settings saved): lead-in **and** lead-out = line, 90°, 3 mm, r 2."""
    return (
        LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0),
        LeadParams(kind=1, angle_deg=90.0, length=3.0, radius=2.0),
    )


def apply_lead(doc: Document, lead_in, lead_out, closed_only: bool = True, start_mode: int = 0) -> int:
    """引线 (lead) on the selected parts (all parts when none is selected).

    O (parity harness ``app_lead``, the original MainApp on basic_r2000.dxf with the dialog
    defaults):

    * "只对封闭图形有效" (closed graphs only, on by default): open contours keep their lead;
      ``closed_only=False`` applies to every target (used to clear leads);
    * the start point is chosen and **stored** when the lead is applied (``start_param`` of
      the contour, ``Craft.start_param`` here), with the dialog's start mode
      (default 0, "自动选择起点位置(长边优先)", :func:`mlaser.cam.ordering.choose_start`);
      clearing leads (both kinds 0) leaves the start alone.

    Parts with several contours get per-contour crafts (:func:`mlaser.cam.manual.contour_craft`,
    honoured by the toolpath), so each contour keeps its own start.  Returns the number of
    parts changed.
    """
    from mlaser.cam.ordering import choose_start

    set_start = bool(lead_in.kind or lead_out.kind)
    n = 0
    for p in targets(doc):
        idx = [i for i, c in enumerate(p.contours) if c.closed or not closed_only]
        if not idx:
            continue
        if len(p.contours) == 1:
            crafts = [(0, p.craft)]
        else:
            from mlaser.cam.manual import contour_craft

            if len(idx) == len(p.contours):  # keep the part craft in step for the .chf writer
                p.craft.lead_in = copy.deepcopy(lead_in)
                p.craft.lead_out = copy.deepcopy(lead_out)
            crafts = [(i, contour_craft(p, i, create=True)) for i in idx]
        for i, cr in crafts:
            cr.lead_in = copy.deepcopy(lead_in)
            cr.lead_out = copy.deepcopy(lead_out)
            c = p.contours[i]
            L = c.length()
            if set_start and c.closed and L > 0:
                cr.start_param = (choose_start(c, start_mode, lead_in.length) % L) / L
        n += 1
    return n


def _crafts(p) -> list:
    """The part craft and every per-contour craft of ``p`` (``Contour.craft``, created by a
    lead, a start point or a manual edit on one contour; the toolpath uses it instead of the
    part craft for that contour)."""
    out = [p.craft]
    for c in p.contours:
        cr = getattr(c, "craft", None)
        if cr is not None and cr is not p.craft:
            out.append(cr)
    return out


def apply_kerf(doc: Document, kind: int, width: float) -> int:
    """Kerf compensation of the target parts, on the part craft **and** on every per-contour
    craft: a part whose contours got their own craft (leads on a part with holes) used to keep
    the old compensation on all of them, so the kerf set afterwards did nothing."""
    n = 0
    for p in targets(doc):
        for cr in _crafts(p):
            cr.kerf_comp = kind
            cr.kerf_width = width
        n += 1
    return n


def clear_joints(doc: Document) -> int:
    """mf103 clear: drop the micro-joints of the target parts (per-contour crafts included)."""
    n = 0
    for p in targets(doc):
        n += len(p.craft.micro_joints)
        for cr in _crafts(p):
            cr.micro_joints = []
    return n


@dataclass
class MicroJointParams:
    """The micro-joint dialog (mf103): ``kind`` 0 = by count, 1 = by distance."""

    kind: int = 0
    count: int = 4
    distance: float = 100.0
    length: float = 0.5
    min_size: float = 0.0

    def get(self, key, default=None):  # the old ``window.mj_params`` dict interface
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value) -> None:
        setattr(self, key, value)

    def update(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def auto_microjoints(doc: Document, prm: MicroJointParams) -> int:
    """mf103 auto micro-joints (:func:`mlaser.cam.microjoint.auto_joints`, CADModule 0x10071180)
    on the longest contour of every target part; stores the joint length in
    ``Craft.joint_width``.  Returns the number of joints."""
    from mlaser.cam.microjoint import auto_joints

    n = 0
    for p in targets(doc):
        if not p.contours:
            continue
        ci = max(range(len(p.contours)), key=lambda k: p.contours[k].length())
        c = p.contours[ci]
        js = auto_joints(
            c,
            count=prm.count if prm.kind == 0 else 0,
            spacing=prm.distance if prm.kind == 1 else 0.0,
            joint_len=prm.length,
            min_size=prm.min_size,
        )
        p.craft.micro_joints = [MicroJoint(float(a), float(w)) for a, w in js]
        p.craft.joint_width = prm.length
        ov = getattr(c, "craft", None)         # the contour's own craft is the one the toolpath uses
        if ov is not None and ov is not p.craft:
            ov.micro_joints = list(p.craft.micro_joints)
            ov.joint_width = prm.length
        n += len(js)
    return n


def one_key(
    doc: Document,
    *,
    mirror_h: bool = False,
    mirror_v: bool = False,
    optimize: bool = False,
    cleanup_params=None,
    lead: bool = False,
    lead_in=None,
    lead_out=None,
    micro_joint: bool = False,
    mj_params: "MicroJointParams | None" = None,
) -> None:
    """mf197 one-key planning (FUN_004be780 0x4a42): mirror, optimize, leads and micro-joints in
    one step (the ordering part is a sort mode, set by the caller)."""
    from .document import mirror

    if mirror_h:
        mirror(doc, True)
    if mirror_v:
        mirror(doc, False)
    if optimize:
        from mlaser.cam.cleanup import cleanup_document

        cleanup_document(doc, cleanup_params, only_selected=any(p.selected for p in doc.parts))
    if lead:
        li, lo = default_lead_params()
        apply_lead(doc, lead_in or li, lead_out or lo)
    if micro_joint:
        auto_microjoints(doc, mj_params or MicroJointParams())
