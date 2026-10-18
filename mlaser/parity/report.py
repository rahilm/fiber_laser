"""Text and HTML reports for :func:`mlaser.parity.capture.run`.

The HTML report embeds its plots as PNG (matplotlib, Agg backend) when matplotlib is installed:
cumulative path overlay (original vs rebuild, laser-on drawn thicker, first divergence marked),
deviation along the path (time aligned and spatial), duty and frequency per step, and a zoom on
the first step divergence.  Without matplotlib the HTML report has the tables only.
"""
from __future__ import annotations

import base64
import html
import io
from typing import Optional

import numpy as np

from .capture import FAIL, INFO, PASS, WARN, Result

CAPTURE_COLOR = "#2a78d6"     # categorical slot 1 (original / capture)
REBUILD_COLOR = "#eb6834"     # categorical slot 2 (rebuild)
MAX_PLOT_POINTS = 20000


# =============================================================================== text
def _steps_table(rows: list[dict]) -> list[str]:
    out = [f"    {'step':>7} {'wire':>7} {'dx':>5} {'dy':>5} {'duty':>5} {'freq':>6} {'power':>6} {'x':>9} {'y':>9}"]
    for r in rows:
        out.append(f"    {r['step']:>7} {r['wire']:>7} {r['dx']:>5} {r['dy']:>5} {r['duty']:>5} {r['freq']:>6} "
                   f"{r['power']:>6} {r['x']:>9} {r['y']:>9}")
    return out


def text_report(res: Result, capture_path: str = "", drawing: str = "") -> str:
    an, job, cmp = res.analysis, res.job, res.comparison
    tr = an.traffic
    L: list[str] = []
    L += ["Mlaser capture comparison (original card traffic vs rebuild compile_job)",
          "=" * 72]
    rs = tr.read_stats
    if capture_path:
        L.append(f"capture : {capture_path}" + (f" ({rs.packets} packets)" if rs else ""))
    if drawing:
        L.append(f"drawing : {drawing}")
    L.append(f"card    : {tr.card[0]}:{tr.card[1]} over {tr.transport.upper()}, {len(tr.exchanges)} requests, "
             f"{tr.retransmissions} retransmissions, {tr.unanswered} unanswered, {tr.orphan_replies} orphan replies"
             + (f", TCP: {rs.tcp_retransmitted_segments} retransmitted segments" if rs and rs.tcp_segments else ""))
    L.append(f"status  : {len(an.ro_samples)} RO reads, {len(an.positions)} position reads, "
             f"fast map {'from the capture' if an.fast_map_from_capture else 'default (not in the capture)'}")
    L.append("jobs in the capture:")
    for j in an.jobs:
        L.append(("  * " if j is job else "    ") + j.summary())
    L.append(f"compared: job {job.index}, {'dry run' if res.dry_run else 'laser on'}, start "
             f"({res.start_mm[0]:.4f}, {res.start_mm[1]:.4f}) mm, placement offset "
             f"({res.offset_mm[0]:.4f}, {res.offset_mm[1]:.4f}) mm")
    for n in res.notes:
        L.append(f"note    : {n}")
    L += ["", f"VERDICT: {cmp.verdict}", "", "Checks (core = part of the verdict):"]
    for c in cmp.checks:
        L.append(f"  [{c.status:4}] {'*' if c.core else ' '} {c.name}: {c.detail}")
    fd = cmp.first_divergence
    if fd:
        L += ["", f"First divergence (item level): wire item {fd['item']} "
                  f"(after {fd['capture_steps_before']} capture / {fd['rebuild_steps_before']} rebuild steps)"
                  + (f"; it lies in a run of identical items starting at item {fd['run_start']}"
                     if fd.get("run_start", fd["item"]) < fd["item"] else "")]
        L.append("  capture:")
        L += [f"    {'>' if r['index'] == fd['item'] else ' '} {r['index']:>7}  {r['text']}" for r in fd["capture"]]
        L.append("  rebuild:")
        L += [f"    {'>' if r['index'] == fd['item'] else ' '} {r['index']:>7}  {r['text']}" for r in fd["rebuild"]]
    sd = cmp.step_divergence
    if sd:
        L += ["", f"First step divergence: step {sd['step']} (capture wire item {sd['capture_wire']}, rebuild "
                  f"{sd['rebuild_wire']}), position before it: capture {tuple(sd['capture_pos'])}, rebuild "
                  f"{tuple(sd['rebuild_pos'])} pulses"
                  + (f"; in a run of identical steps starting at step {sd['run_start']}"
                     if sd.get("run_start", sd["step"]) < sd["step"] else "")]
        for side in ("capture", "rebuild"):
            p = sd.get(f"nearest_process_{side}")
            if p:
                L.append(f"  nearest {side} process item: wire {p['wire']} after step {p['after_steps']}: {p['text']}")
        L.append("  capture:")
        L += _steps_table(sd["capture"])
        L.append("  rebuild:")
        L += _steps_table(sd["rebuild"])
    for name, d in cmp.laser_divergence.items():
        L += ["", f"First {name} difference: step {d['step']}: capture {d['capture']}, rebuild {d['rebuild']}"]
        L.append("  capture:")
        L += _steps_table(d["capture_context"])
        L.append("  rebuild:")
        L += _steps_table(d["rebuild_context"])
    if cmp.process_diff:
        L += ["", "Process item differences (difflib opcodes on (op, params)):"]
        for ent in cmp.process_diff[:20]:
            L.append(f"  {ent['tag']}: capture items {ent['capture_range']}, rebuild items {ent['rebuild_range']}")
            for r in ent["capture"][:8]:
                L.append(f"    - capture #{r['n']} (wire {r['wire']}, after step {r['after_steps']}): {r['text']}")
            for r in ent["rebuild"][:8]:
                L.append(f"    + rebuild #{r['n']} (wire {r['wire']}, after step {r['after_steps']}): {r['text']}")
        if len(cmp.process_diff) > 20:
            L.append(f"  ... {len(cmp.process_diff) - 20} more blocks")
    i = cmp.info
    L += ["", "Statistics:",
          f"  steps            capture {i['capture_steps']}, rebuild {i['rebuild_steps']}",
          f"  items            capture {i['capture_items']}, rebuild {i['rebuild_items']} "
          f"(process {i['capture_process_items']} / {i['rebuild_process_items']})",
          f"  laser-on steps   capture {i['capture_laser_steps']} in {i['capture_laser_runs']} runs, rebuild "
          f"{i['rebuild_laser_steps']} in {i['rebuild_laser_runs']} runs",
          f"  deviation        time aligned max {i['max_deviation_pulses']:.1f} / mean "
          f"{i['mean_deviation_pulses']:.2f} pulses; path Hausdorff {i['path_hausdorff']:.1f} pulses "
          f"({i['path_hausdorff_mm']:.4f} mm)",
          f"  step scale       {i['ppm'][0]:g} / {i['ppm'][1]:g} pulses/mm, period {i['period_ms']:g} ms, "
          f"position {i.get('position_ppm', 0):g} counts/mm",
          f"  time             steps {i['capture_step_time_s']:.3f} / {i['rebuild_step_time_s']:.3f} s, timed waits "
          f"{i['capture_wait_ms']} / {i['rebuild_wait_ms']} ms, rebuild estimate {i['rebuild_total_time_s']:.2f} s"
          + (f", capture wall {i['capture_wall_s']:.2f} s" if i.get("capture_wall_s") is not None else "")]
    if cmp.frame_diff:
        f = cmp.frame_diff
        L.append(f"  frames           capture {f['capture_frames']}, rebuild {f['rebuild_frames']}, first different "
                 f"{f['first_different']}")
    return "\n".join(L) + "\n"


# =============================================================================== plots
def _thin(a: np.ndarray, n: int = MAX_PLOT_POINTS) -> np.ndarray:
    if len(a) <= n:
        return np.arange(len(a))
    return np.unique(np.linspace(0, len(a) - 1, n).astype(int))


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _style(ax):
    ax.grid(True, color="#e4e3df", linewidth=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#9b9a94")
    ax.tick_params(colors="#52514e", labelsize=8)


def make_plots(res: Result) -> dict[str, str]:
    """name -> base64 PNG.  Empty when matplotlib is not installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {}
    a = res.comparison.arrays
    ppm = a["ppm"]
    cc, rc = a["cap_cum"] / np.asarray(ppm), a["reb_cum"] / np.asarray(ppm)
    cd, rd = a["cap_duty"], a["reb_duty"]
    out: dict[str, str] = {}
    sd = res.comparison.step_divergence

    # path overlay
    fig, ax = plt.subplots(figsize=(8, 6))
    for pts, duty, col, lab in ((cc, cd, CAPTURE_COLOR, "original (capture)"), (rc, rd, REBUILD_COLOR, "rebuild")):
        idx = _thin(pts)
        ax.plot(pts[idx, 0], pts[idx, 1], color=col, linewidth=0.8, alpha=0.55, label=f"{lab}, all moves")
        on = np.concatenate([[False], duty > 0])
        p2 = pts.copy()
        p2[~on] = np.nan
        ax.plot(p2[idx, 0], p2[idx, 1], color=col, linewidth=2.0, label=f"{lab}, laser on")
    if sd:
        k = sd["step"]
        ax.plot(*cc[min(k, len(cc) - 1)], marker="o", markersize=9, markerfacecolor="none",
                markeredgecolor="#0b0b0b", markeredgewidth=1.5, linestyle="none", label=f"first divergence (step {k})")
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("X (mm)", color="#52514e")
    ax.set_ylabel("Y (mm)", color="#52514e")
    ax.set_title("Cumulative path from the step deltas", loc="left", fontsize=11, color="#0b0b0b")
    ax.legend(fontsize=8, frameon=False, loc="best")
    _style(ax)
    out["path"] = _png(fig)

    # deviation along the path
    fig, ax = plt.subplots(figsize=(9, 3.4))
    dev, pdv = a["dev"], a["path_dev"]
    i1, i2 = _thin(dev), _thin(pdv)
    ax.plot(i1, dev[i1], color=CAPTURE_COLOR, linewidth=1.5, label="time aligned |P_orig(k) − P_rebuild(k)|")
    ax.plot(i2, pdv[i2], color=REBUILD_COLOR, linewidth=1.5, label="spatial: distance to the rebuild path")
    if sd:
        ax.axvline(sd["step"], color="#52514e", linewidth=1, linestyle="--")
        ax.annotate(f"first divergence, step {sd['step']}", (sd["step"], ax.get_ylim()[1]), fontsize=8,
                    color="#52514e", xytext=(6, -12), textcoords="offset points")
    ax.set_xlabel("step index (servo periods)", color="#52514e")
    ax.set_ylabel("deviation (pulses)", color="#52514e")
    ax.set_title("Deviation along the path", loc="left", fontsize=11, color="#0b0b0b")
    ax.legend(fontsize=8, frameon=False)
    _style(ax)
    out["deviation"] = _png(fig)

    # duty / frequency per step: two charts, one measure each
    for key, ylab, title in (("duty", "duty byte", "Duty per step"), ("freq", "frequency field", "Frequency per step")):
        fig, ax = plt.subplots(figsize=(9, 2.8))
        for arr, col, lab, lw in ((a[f"cap_{key}"], CAPTURE_COLOR, "original", 2.2), (a[f"reb_{key}"], REBUILD_COLOR, "rebuild", 1.2)):
            idx = _thin(arr)
            ax.step(idx, arr[idx], where="post", color=col, linewidth=lw, label=lab)
        ax.set_xlabel("step index", color="#52514e")
        ax.set_ylabel(ylab, color="#52514e")
        ax.set_title(title, loc="left", fontsize=11, color="#0b0b0b")
        ax.legend(fontsize=8, frameon=False)
        _style(ax)
        out[key] = _png(fig)

    # zoom on the first divergence
    if sd:
        k = sd["step"]
        lo, hi = max(0, k - 200), k + 200
        fig, ax = plt.subplots(figsize=(6, 5))
        for pts, col, lab, lw in ((cc, CAPTURE_COLOR, "original", 2.4), (rc, REBUILD_COLOR, "rebuild", 1.2)):
            seg = pts[lo:min(hi, len(pts))]
            ax.plot(seg[:, 0], seg[:, 1], color=col, linewidth=lw, marker="o", markersize=2, label=lab)
        ax.plot(*cc[min(k, len(cc) - 1)], marker="o", markersize=10, markerfacecolor="none",
                markeredgecolor="#0b0b0b", markeredgewidth=1.5, linestyle="none", label=f"step {k}")
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_title(f"Steps {lo}..{hi} around the first divergence", loc="left", fontsize=11, color="#0b0b0b")
        ax.set_xlabel("X (mm)", color="#52514e")
        ax.set_ylabel("Y (mm)", color="#52514e")
        ax.legend(fontsize=8, frameon=False)
        _style(ax)
        out["zoom"] = _png(fig)
    return out


# =============================================================================== HTML
_CSS = """
:root{--surface:#fcfcfb;--panel:#f3f2ef;--text:#0b0b0b;--text2:#52514e;--line:#e4e3df;
--pass:#1f7a3a;--fail:#b3261e;--warn:#8a5a00;--info:#52514e;--orig:#2a78d6;--reb:#eb6834;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--panel:#242422;
--text:#f0efec;--text2:#c3c2b7;--line:#3a3a37;--pass:#5cc27a;--fail:#ff8a80;--warn:#e0b44c;--info:#c3c2b7;color-scheme:dark}}
:root[data-theme="dark"]{--surface:#1a1a19;--panel:#242422;--text:#f0efec;--text2:#c3c2b7;--line:#3a3a37;
--pass:#5cc27a;--fail:#ff8a80;--warn:#e0b44c;--info:#c3c2b7;color-scheme:dark}
body{background:var(--surface);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
margin:0;padding:24px 16px;}
main{max-width:1100px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.muted{color:var(--text2)}
.verdict{padding:12px 16px;border-radius:8px;background:var(--panel);font-weight:600;margin:16px 0;border-left:5px solid var(--info)}
.verdict.ok{border-left-color:var(--pass)}.verdict.bad{border-left-color:var(--fail)}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--text2);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.s{font-weight:700;white-space:nowrap}.s.PASS{color:var(--pass)}.s.FAIL{color:var(--fail)}.s.WARN{color:var(--warn)}.s.INFO{color:var(--info)}
tr.hl td{background:var(--panel);font-weight:600}
code,pre{font:12px/1.4 ui-monospace,Consolas,monospace}
pre{background:var(--panel);padding:10px;border-radius:6px;overflow-x:auto}
figure{margin:12px 0}figure img{max-width:100%;height:auto;background:#fcfcfb;border-radius:6px}
figcaption{color:var(--text2);font-size:12px}
.scroll{overflow-x:auto}
"""


def _h(s) -> str:
    return html.escape(str(s))


def _steps_html(rows: list[dict], mark: Optional[int]) -> str:
    cols = ("step", "wire", "dx", "dy", "duty", "freq", "power", "x", "y")
    head = "".join(f'<th class="num">{c}</th>' for c in cols)
    body = "".join(f'<tr class="{"hl" if r["step"] == mark else ""}">'
                   + "".join(f'<td class="num">{r[c]}</td>' for c in cols) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><tr>{head}</tr>{body}</table></div>'


def html_report(res: Result, capture_path: str = "", drawing: str = "", plots: bool = True) -> str:
    an, job, cmp = res.analysis, res.job, res.comparison
    tr = an.traffic
    figs = make_plots(res) if plots else {}
    good = cmp.verdict.startswith(("IDENTICAL", "EQUIVALENT"))
    P: list[str] = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
                    '<meta name=viewport content="width=device-width,initial-scale=1">',
                    "<title>Capture comparison</title>", f"<style>{_CSS}</style></head><body><main>",
                    "<h1>Original vs rebuild: cutting-job stream</h1>",
                    f'<div class="muted">capture {_h(capture_path)} · drawing {_h(drawing)} · card '
                    f'{_h(tr.card[0])}:{tr.card[1]} ({tr.transport.upper()})</div>',
                    f'<div class="verdict {"ok" if good else "bad"}">{_h(cmp.verdict)}</div>']
    P.append("<h2>Checks</h2><table><tr><th>status</th><th>check</th><th>core</th><th>detail</th></tr>")
    for c in cmp.checks:
        P.append(f'<tr><td class="s {c.status}">{c.status}</td><td>{_h(c.name)}</td>'
                 f'<td>{"yes" if c.core else ""}</td><td>{_h(c.detail)}</td></tr>')
    P.append("</table>")
    P.append("<h2>Inputs</h2><ul>")
    for n in res.notes:
        P.append(f"<li>{_h(n)}</li>")
    P.append(f"<li>compared job {job.index}: {_h(job.summary())}</li></ul>")
    if len(an.jobs) > 1:
        P.append("<p class=muted>Other jobs in the capture:</p><ul>" +
                 "".join(f"<li>{_h(j.summary())}</li>" for j in an.jobs if j is not job) + "</ul>")
    for key, cap in (("path", "Cumulative path (step deltas integrated from the start position). Thick = laser on."),
                     ("deviation", "Deviation in step pulses, per step index."),
                     ("zoom", "Steps around the first step divergence (markers = one servo period each)."),
                     ("duty", "Duty byte of every step."), ("freq", "Frequency field of every step.")):
        if key in figs:
            P.append(f'<figure><img alt="{_h(cap)}" src="data:image/png;base64,{figs[key]}">'
                     f"<figcaption>{_h(cap)}</figcaption></figure>")
    if not figs and plots:
        P.append("<p class=muted>matplotlib is not installed: no plots (pip install matplotlib).</p>")
    fd = cmp.first_divergence
    if fd:
        P.append(f"<h2>First divergence: wire item {fd['item']}</h2>"
                 f"<p class=muted>after {fd['capture_steps_before']} capture / {fd['rebuild_steps_before']} "
                 "rebuild steps</p><table><tr><th>#</th><th>original</th><th>rebuild</th></tr>")
        rmap = {r["index"]: r["text"] for r in fd["rebuild"]}
        for r in fd["capture"]:
            P.append(f'<tr class="{"hl" if r["index"] == fd["item"] else ""}"><td class=num>{r["index"]}</td>'
                     f'<td><code>{_h(r["text"])}</code></td><td><code>{_h(rmap.get(r["index"], ""))}</code></td></tr>')
        P.append("</table>")
    sd = cmp.step_divergence
    if sd:
        P.append(f"<h2>First step divergence: step {sd['step']}</h2><p class=muted>position before it: original "
                 f"{tuple(sd['capture_pos'])}, rebuild {tuple(sd['rebuild_pos'])} pulses</p>")
        for side in ("capture", "rebuild"):
            p = sd.get(f"nearest_process_{side}")
            if p:
                P.append(f"<p class=muted>nearest {side} process item: wire {p['wire']} after step "
                         f"{p['after_steps']}: <code>{_h(p['text'])}</code></p>")
        P.append("<h3>original</h3>" + _steps_html(sd["capture"], sd["step"]))
        P.append("<h3>rebuild</h3>" + _steps_html(sd["rebuild"], sd["step"]))
    for name, d in cmp.laser_divergence.items():
        P.append(f"<h2>First {name} difference: step {d['step']}</h2>"
                 f"<p>original {d['capture']}, rebuild {d['rebuild']}</p>"
                 + _steps_html(d["capture_context"], d["step"]) + _steps_html(d["rebuild_context"], d["step"]))
    if cmp.process_diff:
        P.append("<h2>Process item differences</h2><table><tr><th>op</th><th>original</th><th>rebuild</th></tr>")
        for ent in cmp.process_diff[:50]:
            c = "<br>".join(f"#{r['n']} after step {r['after_steps']}: <code>{_h(r['text'])}</code>" for r in ent["capture"])
            r_ = "<br>".join(f"#{r['n']} after step {r['after_steps']}: <code>{_h(r['text'])}</code>" for r in ent["rebuild"])
            P.append(f"<tr><td>{_h(ent['tag'])}</td><td>{c}</td><td>{r_}</td></tr>")
        P.append("</table>")
    P.append("<h2>Text report</h2><pre>" + _h(text_report(res, capture_path, drawing)) + "</pre>")
    P.append("</main></body></html>")
    return "\n".join(P)


__all__ = ["text_report", "html_report", "make_plots", "PASS", "FAIL", "WARN", "INFO"]
