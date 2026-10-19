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
built-in plugins (`mlaser.ui.mainwindow.EXTENSIONS`) first and then the entry points sorted by
name (`MainWindow(..., entry_points=True)`); an entry point whose name is already taken is
ignored with a log line.

## 3. The context

| Call | What it does |
|---|---|
| `ctx.register_command(id, handler, label="", checkable=False)` | binds a command id.  A ribbon command keeps its button; a new id gets an action labelled `label` (a lang key or text).  A checkable command's handler receives the checked state.  **A second registration of an id raises `DuplicateCommandError`**; there is no override.  A handler that raises is logged and recorded (`tools/command_coverage.py` reports it); the app keeps running |
| `ctx.edit(fn, label, parts=None)` | runs `fn(document)` as one undoable transaction and refreshes the views.  `parts` is the scope: the parts `fn` changes in place (default all; `()` when `fn` only adds, removes or reorders parts or changes `layers` / `extra`).  The smaller the scope, the cheaper the undo step.  If `fn` raises, the scope is restored and nothing is recorded |
| `ctx.replace_document(doc, label)` | makes `doc` the open document (undoable) |
| `ctx.on_controller_changed(fn)` | `fn(controller)` after the machine controller was replaced (re-attach signals, hooks) |
| `ctx.on_mode_changed(fn)` | `fn(mode)` when the canvas mode changes (view.* commands, Esc) |
| `ctx.on_document_changed(fn)` | `fn(kind)` after an edit, undo, redo or a new document |
| `ctx.on_plan(fn)` | `fn(planned_job)` when a new PlannedJob is ready (preview = what Start cuts) |
| `ctx.on_close(fn)` | before the window closes (save state, close devices) |
| `ctx.provide(name, fn)` | a single-slot provider; e.g. `"nudge"` takes over the arrow keys.  A second provider raises |
| `ctx.add_dock(widget, title, area="right")` | a dock widget (`left` / `right` / `bottom` / `top`) |
| `ctx.add_ribbon_tab(title, command_ids)` | a ribbon tab with one button per command |
| `ctx.add_workspace(name, widget, title)` | registers a task workspace (Wave 3 workspaces; kept in `window.workspaces`) |
| `ctx.settings_schema([Setting, ...])` | typed, validated, persisted settings stored as `plugin.<name>.<key>` in the ConfigService; returns a dict-like `PluginSettings` |
| `ctx.document`, `ctx.docs`, `ctx.config`, `ctx.jobs`, `ctx.commands`, `ctx.controller`, `ctx.tr`, `ctx.log(msg)` | the services; `ctx.window` is the Qt parent for dialogs |

Rules:

* Change the document only inside `ctx.edit` / `ctx.replace_document`.  Never call the window's
  private methods (`_snapshot`, `_refresh`, `_edit`) and never replace window methods.
* Keep the logic Qt-free in a module of its own (as `mlaser.ops.cam` for ext_cam) and test it
  without a display; the plugin module holds the dialogs.
* Long work belongs in a worker; use `ctx.jobs` for anything that compiles a job, and run a cut
  only through `window.run_job(mode)` so it is the planned job.

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
