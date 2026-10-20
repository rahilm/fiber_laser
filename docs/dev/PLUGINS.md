# Writing an mlaser plugin

A plugin adds commands, panels and settings to the application through one object, the
`PluginContext` (`mlaser/services/plugins.py`).  The eight built-in feature modules
(`mlaser/ui/ext_*.py`) are plugins written against the same API, so they are the largest
examples; `examples/mlaser_hello/` is the smallest.

## 1. Shape of a plugin

A plugin is a module, or any object, with `register(ctx)`:

```python
from mlaser.services.config import Setting

PLUGIN_API = 1                     # the API version the plugin was written for (optional)

def register(ctx):
    s = ctx.settings_schema([Setting("size", "float", 10.0, unit="mm", min=0.1, max=1000.0)])
    ctx.register_command("hello.square", lambda: add_square(ctx, s["size"]), label="Square")
    ctx.add_ribbon_tab("Hello", ["hello.square"])
    return None                    # anything; the app keeps it as window.extension("hello")
```

`register` runs once while the main window starts.  If it raises, everything it registered is
removed again and the reason is shown in the log panel (nothing is silently skipped).

## 2. Making it discoverable

Declare an entry point in the group `mlaser.plugins` of your package:

```toml
[project.entry-points."mlaser.plugins"]
hello = "mlaser_hello:plugin"      # module, or module:object with register(ctx)
```

`pip install -e examples/mlaser_hello` installs the example.  The application loads its
built-in plugins (`mlaser.ui.mainwindow.EXTENSIONS`) first.  Installed entry points are
**listed but not loaded** until a service engineer enables them in **Options+ > Plugins**
(Service mode; see [§6](#6-trust-model)); the enabled ones load at the next start, sorted by
name, after the safety UI exists (`MainWindow(..., entry_points=True)`).  An entry point whose
name is already taken is ignored with a log line.

## 3. The context

| Call | What it does |
|---|---|
| `ctx.register_command(id, handler, label="", checkable=False, min_mode="", workspace="")` | binds a command id.  `min_mode` (`"operator"` / `"engineer"` / `"service"`) is the lowest user mode that may run it (default: the policy table of `mlaser.services.usermodes.default_min_mode`); `workspace` the workspace it belongs to in the palette and the shortcut sheet.  A ribbon command keeps its button; a new id gets an action labelled `label` (a lang key or text).  A checkable command's handler receives the checked state.  **A second registration of an id raises `DuplicateCommandError`**; there is no override.  A handler that raises is logged and recorded (`tools/command_coverage.py` reports it); the app keeps running |
| `ctx.edit(fn, label, parts=None)` | runs `fn(document)` as one undoable transaction and refreshes the views.  `parts` is the scope: the parts `fn` changes in place (default all; `()` when `fn` only adds, removes or reorders parts or changes `layers` / `extra`).  The smaller the scope, the cheaper the undo step.  If `fn` raises, the scope is restored and nothing is recorded |
| `ctx.replace_document(doc, label)` | makes `doc` the open document (undoable) |
| `ctx.on_controller_changed(fn)` | `fn(controller)` after the machine controller was replaced (re-attach signals, hooks) |
| `ctx.on_mode_changed(fn)` | `fn(mode)` when the canvas mode changes (view.* commands, Esc) |
| `ctx.on_document_changed(fn)` | `fn(kind)` after an edit, undo, redo or a new document |
| `ctx.on("document_imported", fn)` | `fn(path, document)` after File > Open or Import read a drawing (DXF, SVG, G-code, chf); the built-in Orders plugin runs the import review from it ([import.md](../user/import.md)) |
| `ctx.on_plan(fn)` | `fn(planned_job)` when a new PlannedJob is ready (preview = what Start cuts) |
| `ctx.on_close(fn)` | before the window closes (save state, close devices) |
| `ctx.provide(name, fn)` | a single-slot provider; e.g. `"nudge"` takes over the arrow keys.  A second provider raises.  Used today: `dxf.mode` (ext_options), `machine.monitor_host` (ext_machine: `fn(key, factory)` shows a device monitor as a panel of the Machine workspace); read when present: `user_mode` (`fn() -> "operator" / "engineer" / "service"`), `workspace.show` (`fn(name)`, the shell's switcher) |
| `ctx.add_dock(widget, title, area="right")` | a dock widget (`left` / `right` / `bottom` / `top`) |
| `ctx.add_ribbon_tab(title, command_ids)` | a ribbon tab with one button per command |
| `ctx.add_workspace(name, widget, title, **options)` | a task workspace in the shell's rail, or the content of a built-in slot (§5); kept in `window.workspaces` too |
| `ctx.show_workspace(id)`, `ctx.user_mode` | switch workspace (`"run"`, `"<plugin>.<name>"`); the current user mode |
| `ctx.on_workspace_changed(fn)`, `ctx.on_user_mode_changed(fn)` | `fn(workspace_id)` / `fn(mode)` after the shell switched |
| `ctx.settings_schema([Setting, ...])` | typed, validated, persisted settings stored as `plugin.<name>.<key>` in the ConfigService; returns a dict-like `PluginSettings` |
| `ctx.document`, `ctx.docs`, `ctx.config`, `ctx.jobs`, `ctx.commands`, `ctx.controller`, `ctx.tr`, `ctx.log(msg)` | the services; `ctx.window` is the Qt parent for dialogs |

Data that belongs to the job (a technology assignment, queue entries, a plugin's own state) is
saved in the `.mlz` project and its autosave when the plugin registers a section:
`window.extension("ext_project").register_section(name, get, set)`; see
[project-format.md](project-format.md#plugin-sections).  Keep typed data in the model's typed
fields, not in new `extra` keys (`extra` is for opaque round-trip data, `model.EXTRA_KEYS`).

Rules:

* Change the document only inside `ctx.edit` / `ctx.replace_document`.  Never call the window's
  private methods (`_snapshot`, `_refresh`, `_edit`) and never replace window methods.
* Keep the logic Qt-free in a module of its own (as `mlaser.ops.cam` for ext_cam) and test it
  without a display; the plugin module holds the dialogs.
* Long work belongs in a worker; use `ctx.jobs` for anything that compiles a job, and run a cut
  only through `window.run_job(mode)` so it is the planned job.

**Trust.** A plugin is trusted code with machine authority: it runs in the application process
and Python cannot sandbox it ([SAFETY.md §3a](../SAFETY.md#3a-plugin-trust-boundary)).
`ctx.controller` is a read-only view of the controller (the `MachineAPI` members only): no
supervisor, no transport, no writable safety setting. `ctx.controller.arm(reason)`, `reset_fault()`,
`operator_action()` and `configure_safety()` are audited under `plugin:<name>` and are refused
unless the operator's plugin policy allows them. `ctx.replace_document` is refused while a job is
active (running, paused or kept at a breakpoint).

## 4. Testing

```python
from mlaser.services.plugins import PluginInfo, register_plugin

def test_hello(fake_host):          # see tests/test_services_plugins.py: FakeHost is Qt-free
    ctx, ext = register_plugin(fake_host, PluginInfo("hello", "mlaser_hello:plugin"))
    fake_host.commands.run("hello.grid")
    assert fake_host.docs.document.parts
    fake_host.docs.undo()
```

For the real application wiring, `tests/wiring.py:real_window()` builds the main window with
every built-in plugin, as the application does.  `python tools/command_coverage.py` triggers
every command headlessly and must report 0 unimplemented / 0 raising / 0 duplicates.

## 5. Workspaces (Wave 3 shell)

The shell (`mlaser/ui/shell/`, user docs: [docs/user/shell.md](../user/shell.md)) has a rail
with six built-in **slots**: `design` (Ctrl+1, the canvas, default), `process` (Ctrl+2),
`nest` (Ctrl+3), `run` (Ctrl+4), `report` (Ctrl+5) and `machine` (Ctrl+6).  Until a plugin fills
a slot it shows a placeholder with the task's classic commands.

```python
def register(ctx):
    panel = ProcessPanel(ctx)                      # any QWidget
    ctx.add_workspace("process", panel, "Process", # fills the Process slot (slot rule below)
                      canvas=True,                 # show the shared canvas, panel to its right
                      toolbar=["craft.lead", "craft.kerf", "-", "sort.run"],
                      docks=["lp0", "mf124"])      # docks shown while it is active
    ctx.add_workspace("techdb", TechDb(ctx), "Technology", icon="database-import",
                      order=25, min_mode="engineer", description="Material x thickness x gas")
```

Options (all optional; `mlaser.services.workspaces.WorkspaceSpec`):

| option | meaning |
|---|---|
| `icon` | Tabler icon name (`mlaser/ui/resources/icons/tabler/*.svg`) |
| `order` | rail position (slots 10..60; default 100) |
| `shortcut` | key sequence; plugins get the first free of Ctrl+7..9 |
| `min_mode` | lowest user mode that sees it (`operator`, `engineer` (default), `service`) |
| `canvas` | `True`: the page shows the shared canvas; `widget` goes into a side panel |
| `toolbar` | command ids of the slim workspace toolbar (`"-"` = separator), at most ~15 |
| `docks` | dock object names shown while active (`lp0` layers/parts, `mf141` run control, `mf124` log, `mf460` nest; your own `ctx.add_dock(..., key=...)`); a dock that some workspace lists but the active one does not is hidden, docks no workspace lists are left alone; `()` = no docks |
| `description` | one line for the rail tooltip and the placeholder |
| `slot` | the slot to fill, when `name`/`title` do not say it |

**Slot rule.**  A workspace whose `slot`, `name` or `title` equals a slot id (case-insensitive:
`"process"`, `"Run"`) fills that slot: the placeholder goes, the rail keeps one button.  The
first contribution decides the page (canvas or not, toolbar, docks; options it leaves out keep
the slot's); a second contribution to the same slot becomes a tab of the page.  Every other
workspace gets a rail button of its own.  The id is always `"<plugin>.<name>"`, and the
three-argument call of Wave 2 (`ctx.add_workspace(name, widget, title)`) keeps working: a host
without the shell (the Qt-free `FakeHost` of the tests) receives `host_add_workspace(id, widget,
title)` and the options are dropped.

**User modes.**  `ctx.user_mode` is `operator`, `engineer` or `service`.  Commands are disabled
(and hidden from the palette) below their `min_mode`; workspaces are hidden from the rail.  A run
UI for operators should use only commands whose minimum mode is `operator` (`run.*`,
`machine.home*`, `file.open`, `card.*` alarms/resume ...) or declare its own commands with
`min_mode="operator"`.  Never use the user mode as a safety interlock: arming, pre-flight and
the supervisor are independent of it (docs/SAFETY.md).

**Run controls.**  The shell registers `run.start` (F5), `run.frame` (F6), `run.dry_run`
(Shift+F6), `run.hold`, `run.stop` (F12), `run.arm`, `run.simulate`; they call the safety UI
(`window.safety_ui.start()` ...), so a Run workspace can put them on its own buttons with
`QToolButton.setDefaultAction(window.ribbon.actions["run.start"])` and get the pre-flight check,
the arming dialog and the keys for free.  The persistent run bar stays at the bottom of every
workspace; do not build a second one.

**Providers.**  `window.hooks.provider("workspace.show")(id)` and
`window.show_workspace(id)` switch (ids as in `window.workspaces`: `"run.run"`,
`"ext_machine.machine"` ... resolve to their slot); `window.hooks.provider("user_mode")()`
returns the mode.  A contributed page sits in a scroll area, so its minimum size never makes
the main window larger than 1280 × 800.

**Testing.**  `tests/wiring.py:real_window(user_mode="service")` (the default) enables every
command; pass `user_mode="operator"` to test an operator screen.  `window.show_workspace(id)`
switches; `window.shell.host.pages[id]` is the page.

## 6. Trust model

**A plugin is trusted code.**  It runs inside the mlaser process with the user's operating-system
privileges and the machine's authority: it can read and write the user's files, open network
connections, start processes, and call everything the application can call, including the
motion controller.  Nothing sandboxes it, and Python cannot: a plugin can import any module,
replace functions, or reach objects through the garbage collector.  Install and enable a plugin
only when you would run its author's program on the machine's PC.

**What the application does about it:**

| Protection | What it gives | What it does not |
|---|---|---|
| Opt-in loading | Entry-point plugins are discovered and listed (Options+ > Plugins, with their entry point, package and status) but imported only when the allowlist `plugins.allowlist` in `config.json` names them **with the same target** (`module:attr`).  Enabling needs Service mode and a confirmation, is audited (`plugin_allowlist`), and applies at the next start.  A package that points its name at other code shows as `changed` and is not loaded until it is enabled again. | No signature or hash check: anyone who can write to the Python environment or to `config.json` can change what runs. |
| Load order | Enabled plugins load after the safety UI (state banner, run bar, Stop / Hold keys, pre-flight dialog) exists; every load is audited (`plugin_loaded`). | A plugin can still change the window afterwards. |
| Reserved commands | A plugin that is not built in cannot register `run.*`, `shell.*` or `workspace.*` command ids (`ReservedCommandError`): it can neither take over Stop / Hold / Arm nor make start-up fail by pre-claiming them.  A failing plugin is rolled back and logged. | Other commands are first come, first served (a duplicate raises). |
| The safety guard | Every request to the card goes through the supervisor's command policy (docs/SAFETY.md §3): emission while disarmed is refused, halts always pass, the interlocks and the watchdog work as for the core. | It prevents **accidents** (a plugin that forgets to arm, a bug that writes a laser word while disarmed); it does not stop a **hostile** plugin, which can reach the transport or the guard's configuration through Python. |
| Plugin view and policy | A third-party plugin's `ctx.controller` is `mlaser.controller.plugin_view.plugin_view(ctl, name)` (no guard internals; its `arm` is audited as `source="plugin:<name>"`).  The controller asks its plugin policy before a plugin arms, resets a fault, holds the heartbeat or changes the safety configuration; the application's policy (`services.plugins.plugin_policy`) refuses unless the plugin is enabled **and** the service engineer granted it **machine authority** in Options+ > Plugins (`plugins.authority`). | A view narrows the documented API; it is not a security boundary. |
| User modes | Commands declare a minimum mode (Operator / Engineer / Service). | A user mode is not a safety interlock and not an access control for code. |

**Rules for plugin authors** (they keep a well-meaning plugin inside the safety design):

* talk to the machine only through `ctx.controller`'s `MachineAPI` methods (`mlaser.machine.api`)
  and run cuts only through `window.run_job(mode)` (pre-flight, the planned job, arming);
* never arm on the operator's behalf, never clear a fault or the halt latch, never change a
  `safety.*` value, the guard, the supervisor or its tunables (deadlines, buffer, heartbeat);
* never replace the document while a job runs or a breakpoint is pending (`ctx.replace_document`
  raises `DocumentLockedError` and the operator sees why);
* never register a command in `run.*`, `shell.*`, `workspace.*`; keep your settings under
  `ctx.settings_schema` (`plugin.<name>.*`);
* treat files and network data as untrusted input (sizes, depth, expansion; see
  [project-format.md](project-format.md) for the limits the core applies).

Known gaps (review 2): `ctx.config` is the whole ConfigService, so a plugin can change any value,
including `safety.*` and the LAN monitor's settings; `ctx.window` is the main window itself; and
a plugin can replace the `process` profile or providers such as `project.open` (all by design for
trusted code).  Report a plugin or a path around the guard that a well-meaning plugin could hit
by accident privately, as [SECURITY.md](../../SECURITY.md) describes.
