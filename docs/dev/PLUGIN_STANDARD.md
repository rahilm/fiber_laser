# mlaser plugin standard, version 1 (plugin API 2)

This is the **normative** standard for third-party mlaser plugins: how a plugin is packaged,
what its manifest declares, the plugin API it codes against, how it is published in a
repository, and what the application checks before it installs, enables and loads it. The
words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119. The guide to writing a plugin is
[PLUGINS.md](PLUGINS.md); the user's side is [docs/user/plugins.md](../user/plugins.md).

Reference implementation: `mlaser/services/plugin_manifest.py` (manifest, permissions),
`mlaser/services/plugins.py` (API 2), `mlaser/services/plugin_http.py` (`ctx.http`),
`mlaser/services/plugin_manager.py` (repositories, verification, install),
`mlaser/services/plugin_index.py` (the index builder). Examples:
[examples/mlaser_hello](../../examples/mlaser_hello/), the template
[plugin-repo-template](plugin-repo-template/README.md) and the project's own plugins in
`plugins/`.

## 1. Packaging

1. A plugin MUST be a Python distribution published as a **pure-Python wheel**: file name tag
   `py3-none-any`, `Root-Is-Purelib: true`, no `<dist>.data/` directory, no compiled module or
   shared library (`.so`, `.pyd`, `.dll`, `.dylib`, `.exe`), no `.pth` file, no top-level
   `sitecustomize` / `usercustomize`. An sdist or a git checkout MAY be used for development
   installs with pip; the plugin manager installs wheels only.
2. Every file of the wheel MUST be listed in its `RECORD` with a `sha256` (or `sha384` /
   `sha512`) hash, and `METADATA` `Name` / `Version` MUST match the file name and the
   `.dist-info` directory.
3. It MUST declare **exactly one** entry point in the group `mlaser.plugins`:
   `<id> = "<module>:<attr>"`. The object MUST have `register(ctx)` and SHOULD have
   `PLUGIN_API = 2`.
4. It MUST ship the manifest **`mlaser-plugin.toml` inside its top-level import package**
   (`<top>/mlaser-plugin.toml`, package data, listed in `RECORD`), so the application reads it
   from the installed distribution's file list **without importing the plugin**.
5. Its top-level import names MUST NOT collide with `mlaser`, the standard library, any module
   importable in the application, or another installed plugin.
6. Its `Requires-Dist` dependencies MUST already be satisfied by the application's environment
   (the frozen bundle ships ezdxf, numpy, shapely, segno, Pillow, fontTools, svgelements,
   pyclipper, uharfbuzz, platformdirs, PySide6). The plugin manager installs no dependency; a
   plugin that needs another library vendors it inside its own package (pure Python) or is not
   installable. `mlaser` itself MAY be listed (checked against the running application); it is
   not required.
7. The licence is the author's choice (declared in the manifest and `METADATA`).

## 2. The manifest `mlaser-plugin.toml`

```toml
[plugin]
id = "onshape"                     # REQUIRED  ^[a-z][a-z0-9_]{1,31}$ ; = the entry-point name
name = "Onshape"                   # REQUIRED  display name (<= 80 characters)
version = "0.1.0"                  # REQUIRED  PEP 440; = the distribution version
entry = "mlaser_onshape:plugin"    # REQUIRED  = the entry-point value
api = ">=2,<3"                     # REQUIRED  PEP 440 specifier against PLUGIN_API ("" = any)
mlaser = ">=0.1.0rc1"              # REQUIRED  PEP 440 specifier against the application ("" = any)
summary = "One line"               # <= 200 characters
description = "Longer text"        # markdown, <= 20000 characters
authors = ["Name <email>"]
license = "MIT"
homepage = "https://..."
source = "https://..."             # the repository
min_mode = "engineer"              # default lowest user mode of its commands: operator | engineer | service
icon = "icons/onshape.svg"         # an SVG (drawn with currentColor) or PNG inside the package

[permissions]                      # see section 5; missing keys are false / []
network = ["cad.onshape.com", "*.onshape.com"]
files = true
document = false
orders = true
queue = false
machine = false
secrets = true
```

* The `id` names the plugin everywhere: the allowlist, the settings prefix `plugin.<id>.*`, the
  data directory, the command namespace it SHOULD use (`<id>.*`).
* `id`, `entry` and `version` MUST equal the entry point and the distribution; a mismatch is
  an invalid manifest (the plugin is listed but never loaded).
* `network` entries are lower-case host names, IP addresses, or `*.example.com` (any subdomain,
  not the apex).
* Unknown keys are kept (in `ctx.manifest`), shown in the Plugin manager's details, and
  otherwise ignored (forward compatibility).
* The schema check: `python -m mlaser.services.plugin_index check mlaser-plugin.toml`
  (`tools/plugin_index.py check` in a checkout).

**No manifest.** An entry-point plugin without a manifest is a *legacy* plugin: it is listed as
"legacy (no manifest)", MAY be enabled by a service engineer after an extra warning, gets the
API-1 behaviour and none of the API-2 services (`http`, `secrets`, `data_dir`). New plugins
MUST ship a manifest.

## 3. Versioning and compatibility

* `PLUGIN_API` (in `mlaser.services.plugins`) is **2**. It changes in its major sense only when
  a member is removed or changes meaning; additions keep the number. A plugin SHOULD declare
  `api = ">=2,<3"`.
* `mlaser.__version__` is the application version (= `[project] version`). A plugin declares
  the versions it was tested with (`mlaser = ">=0.1.0rc1"`).
* Both ranges are checked **before the plugin is imported**, at install and at every start; a
  plugin outside them is refused with the reason in the log. The application's own
  pre-release counts by its order (`0.1.0rc1` satisfies `>=0.0.1`): it is a compatibility check
  of what is installed, not a selection.
* Selection from an index: the newest version whose ranges include this application; plugin
  pre-releases only when asked for (`--pre`) or named (`onshape==0.2.0rc1`).
* A plugin MUST NOT import private (`_`-prefixed) mlaser names. Public API for plugins: the
  `PluginContext`; `mlaser.model`; `mlaser.services.orders`, `.parts`, `.nesting`,
  `.config.Setting`; the `mlaser.io.*` loaders.

## 4. Plugin API 2

API 2 is additive to API 1 (the members of [PLUGINS.md §3](PLUGINS.md#3-the-context)). All
members exist on every context; a built-in plugin may use everything.

| member | contract |
|---|---|
| `ctx.manifest` | the parsed manifest (dict), or None (built-in, legacy) |
| `ctx.permissions` | a frozen `Permissions` (`.network` tuple, `.files`, `.document`, `.orders`, `.queue`, `.machine`, `.secrets` booleans; `.allows(name)`, `.allows_host(host)`); built-ins: everything |
| `ctx.icon` | absolute path of the manifest's `icon` ("" when none), found without importing; for `add_workspace(icon=ctx.icon)` and `register_command(..., icon=ctx.icon)` |
| `ctx.data_dir` | `pathlib.Path` `<data dir>/plugins/data/<id>/`, created on first access |
| `ctx.http` | `get(url, *, params=None, headers=None, timeout=20, max_bytes=50_000_000, auth=None)`, `post(url, *, json=None, data=None, headers=None, timeout=20, max_bytes=..., auth=None)` → `Response` (`.status`, `.headers` case-insensitive, `.content`, `.text`, `.json()`, `.url`, `.ok`). Rules: section 5.1 |
| `ctx.secrets` | `get(key) -> str | None`, `set(key, value)`, `delete(key)`; keys `[A-Za-z0-9_.-]{1,64}`; stored as `plugin.<id>.<key>` in the OS keyring when available, else a 0600 JSON file in the data dir; never logged. Needs `secrets` |
| `ctx.run_in_background(fn, *, on_done=None, on_error=None, on_progress=None) -> handle` | runs `fn` on a worker thread; `on_done(result)`, `on_error(exc)` and `on_progress(value)` run on the GUI thread. If `fn` has a parameter named `progress` (or `**kwargs`) it is called as `fn(progress=report)`, else `fn()`; `report(value)` may be called often: deliveries are coalesced (the latest value; at most one pending). `handle.cancel()` sets `handle.cancelled` (cooperative); `handle.wait(timeout)` |
| `ctx.controller` | API-1 rules (the Safety-core plugin view), plus: `PermissionError` unless `machine` is declared **and** the service engineer granted machine authority |
| `ctx.services` | `orders` (the `OrderBook`; needs `orders`), `orders_load_group` (`fn(key, replace=None)`: one material x thickness group into Nest; needs `orders`), `technology` (the technology database), `queue` and `intake` (need `queue`), `config` (the ConfigService, read-mostly); None when that part of the application is absent |
| `ctx.show_message(text, level="info")` | status-bar message, also in the log (`level`: `info`, `warning`, `error`) |
| `ctx.register_command(..., icon="")` | `icon`: a Tabler icon name or an icon file; the manifest's `min_mode` is the default `min_mode` |

`ctx.edit` / `ctx.replace_document` need `document`.

## 5. Permissions and the security model

Permissions are **declarations, not a sandbox**: Python cannot isolate code in the same process
([SAFETY.md §3a](../SAFETY.md#3a-plugin-trust-boundary)). The application shows them before
install and before enabling, records the accepted set, and **enforces them on its own APIs**, so
a well-meaning plugin cannot exceed them by accident:

| permission | enforced on |
|---|---|
| `network` | `ctx.http`: the host of every request and redirect (plus hosts granted in the Plugin manager, 5.2) |
| `files` | declarative (the plugin's own `data_dir` is always available) |
| `document` | `ctx.edit`, `ctx.replace_document` |
| `orders` | `ctx.services.orders`, `ctx.services.orders_load_group` |
| `queue` | `ctx.services.queue`, `ctx.services.intake` (the queue never starts a job by itself) |
| `machine` | `ctx.controller` (and the machine-authority grant); the controller's plugin policy still refuses arm / reset / safety changes without the grant |
| `secrets` | `ctx.secrets` |

A plugin with a manifest that uses an API it did not declare gets `PermissionError`.

### 5.1 `ctx.http`

* https only; plain http only to `localhost`, `127.0.0.1`, `::1`. Another scheme, credentials in
  the URL, or http to another host: `PermissionError` (`InsecureURLError`).
* The host MUST match `permissions.network` (or a granted host), else `PermissionError`
  (`HostNotAllowedError`).
* Redirects are followed by the client (at most 5), only to allowed hosts; `Authorization` and
  `Cookie` are dropped when the host changes.
* `params` values that are lists repeat the key (`{"edgeId": ["a", "b"]}` →
  `edgeId=a&edgeId=b`, `urlencode(doseq=True)`).
* `auth=("user", "secret")`: HTTP Basic. 429 / 502 / 503 / 504 are retried up to 3 times
  honouring `Retry-After` (seconds or HTTP date; else 1, 2, 4 s), within 30 s of waiting; then
  the last response is returned. Other statuses are returned, not raised.
* A body larger than `max_bytes`: `ValueError`. A network failure: `ConnectionError`.
* Never on a machine thread (`RuntimeError`); callers keep it off the GUI thread
  (`ctx.run_in_background`).

### 5.2 Granted hosts

A service engineer MAY grant a plugin extra **exact** hosts (no wildcard) in the Plugin manager
(Installed > Network hosts, Service mode) or with `mlaser plugin hosts <id> --add <host> --yes`,
e.g. a company's Onshape Enterprise server. They are stored in `plugins.network`, audited
(`plugin_network_grant`), apply at once and are removed with the plugin.

### 5.3 What is enforced at install, enable and load

* **Install** (section 7): verified wheel, nothing written until every check passed.
* **Enable**: Service mode and a confirmation that lists the permissions; `machine` asks for
  the machine authority separately; the enabled target (`plugins.allowlist`) and the accepted
  permissions (`plugins.accepted`) are recorded and audited (`plugin_allowlist`).
* **Load** (every start): only allowlisted plugins whose entry point is unchanged and whose
  manifest permissions equal the accepted ones (else `changed`: enable again); the manifest is
  valid and in range; they load after the safety UI exists; their commands cannot use the
  core's `run.*`, `shell.*`, `workspace.*` namespaces; every load is audited.

### 5.4 Limits

What the model does **not** do: it does not stop a hostile plugin. Plugin code can import any
module (`urllib`, `socket`, `subprocess`), read `ctx.window`, change any configuration value,
or open its own connection to the motion card; signatures are not checked (the sha256 proves
the download is the one the index lists, not who wrote it). The protections are against
accidents and tampering in transit, and they make every change visible and audited. Install
only plugins whose author you would let run a program on the machine's PC. The physical safety
chain (e-stop, interlocks, the watchdog relay) does not depend on any of this.

## 6. Repositories

A repository is an https URL of an **index** JSON (schema:
[plugin-index.schema.json](plugin-index.schema.json)):

```json
{"format": "mlaser-plugin-index", "version": 1, "name": "Example plugins", "homepage": "https://...",
 "plugins": [
  {"id": "onshape", "name": "Onshape", "summary": "...", "description": "...", "homepage": "...",
   "source": "...", "authors": ["..."], "license": "MIT",
   "versions": [
    {"version": "0.1.0", "url": "mlaser_onshape-0.1.0-py3-none-any.whl", "sha256": "<64 hex>",
     "size": 12345, "api": ">=2,<3", "mlaser": ">=0.1.0rc1", "entry": "mlaser_onshape:plugin",
     "requires": ["ezdxf>=1.1"], "permissions": {"network": ["cad.onshape.com"], "files": true},
     "published": "2026-10-20"}]}]}
```

* `url` MAY be relative: it resolves against the index URL (the natural form when the index and
  the wheels are assets of one release).
* `id`, `version`, `url` (a `.whl`) and `sha256` are required per version; `entry`,
  `permissions`, `api`, `mlaser` SHOULD be present (they MUST then equal the wheel's manifest).
* Build it with the tool, never by hand:
  `python -m mlaser.services.plugin_index build dist/*.whl -o mlaser-plugin-index.json --sha256sums SHA256SUMS`
  (`--update old.json` merges; `--base-url` for absolute URLs; `check` validates).
* Limits applied by the manager (untrusted input): index at most 5 MB, 1000 plugins, 200
  versions per plugin, 100 requirements; text fields truncated; invalid plugins or versions are
  dropped and reported, not fatal. Indexes are cached (`<data dir>/plugins/cache`, 6 hours);
  the cache is used, marked stale, when the network fails.
* Default repository: the project's own index,
  `https://github.com/rahilm/fiber_laser/releases/latest/download/mlaser-plugin-index.json`
  (removable).

### 6.1 The GitHub shortcut

`github:<owner>/<repo>` (also accepted: `https://github.com/<owner>/<repo>`) means the
**latest release** of that repository (`github:<owner>/<repo>@<tag>` for one release, as an
install source):

1. its `mlaser-plugin-index.json` asset, if present (the recommended form: the template's
   workflow attaches it);
2. else every `*.whl` asset, with the sha256 from a `SHA256SUMS` asset (`sha256sum` format) or
   GitHub's asset `digest` (`sha256:<hex>`); a wheel without a hash is skipped. Such entries
   carry no manifest data, so the permissions are shown from the wheel before installing.

Downloads from `github.com` / `api.github.com` may be redirected to GitHub's asset hosts
(`objects.githubusercontent.com`, `release-assets.githubusercontent.com`,
`github-releases.githubusercontent.com`); any other redirect must stay on the same host.

### 6.2 Publishing from your own repository

Copy [plugin-repo-template](plugin-repo-template/README.md): `pyproject.toml`, the manifest,
the plugin and `.github/workflows/release.yml`, which on a `v*` tag builds the wheel, checks it
exactly as the manager will, and attaches the wheel, `SHA256SUMS` and `mlaser-plugin-index.json`
to the release. Users add `github:<owner>/<repo>`.

## 7. Installation (what the manager checks and does)

Sources: a plugin id from the repositories (`onshape`, `onshape==0.2.0`), `github:<owner>/<repo>`,
an `https://.../<name>.whl` URL with `#sha256=<hex>` (without it the user MUST confirm the
downloaded hash shown to them; the CLI needs `--sha256` or `--accept-hash`), or a local `.whl`.

Before anything is written, refused with the reason (audited `plugin_refused`) unless:

1. https (http only to this computer); redirects as in 6.1; the download within the index's
   `size` and at most 100 MB;
2. the sha256 (and size) equal the index entry or the given hash;
3. a safe zip: no absolute or `..` paths, no backslashes, links, devices, encrypted entries or
   duplicates; at most 5000 files and 300 MB unpacked;
4. a pure wheel (section 1.1) whose `RECORD` hashes every file;
5. `METADATA` matches the file name; exactly one `mlaser.plugins` entry point;
6. the manifest is valid, equals the entry point and version, and its icon is in the package;
7. the manifest equals the index entry (id, version, entry, permissions, api, mlaser);
8. `api` / `mlaser` include this application; `Requires-Python` includes this Python;
9. no top-level collision (section 1.5), the id is not taken (another installed plugin, a
   pip-installed entry point; a built-in plugin's name in the GUI), the distribution is not installed under another id;
10. every applicable `Requires-Dist` is satisfied (in the frozen bundle, a library without
    metadata counts when its module is importable).

Then the wheel is unpacked (staged, then moved) into `<data dir>/plugins/site/` and recorded
with its exact file list in `<data dir>/plugins/installed.json` (audited `plugin_install`).
**Installing never enables.** At start-up the site directory is appended to the **end** of
`sys.path` before the entry-point discovery (core modules always win); the discovery is the
ordinary `mlaser.plugins` one. Changes apply at the next start.

* **Update**: no automatic updates; "Check for updates" lists newer compatible versions. An
  update keeps the plugin enabled only when its entry point and permissions are unchanged;
  otherwise it is disabled and must be enabled again (audited `plugin_update`).
* **Uninstall** removes exactly the recorded files (and directories they leave empty), the
  allowlist, authority, accepted and granted-host entries; the data directory is kept unless
  purged (audited `plugin_uninstall`).
* Repository changes are audited (`plugin_repo_add`, `plugin_repo_remove`).

## 8. Conformance checklist for authors

- [ ] pure wheel, one `mlaser.plugins` entry point, `PLUGIN_API = 2`
- [ ] `mlaser-plugin.toml` in the package, listed as package data; id / entry / version equal
- [ ] permissions: only what the plugin uses; network hosts exact or `*.domain`
- [ ] dependencies: only libraries the application ships
- [ ] commands in `<id>.*`, settings through `ctx.settings_schema`, network through `ctx.http`
      on a worker (`ctx.run_in_background`), credentials through `ctx.secrets`
- [ ] the rules for plugin authors of [PLUGINS.md §6](PLUGINS.md#6-trust-model)
- [ ] `python -m mlaser.services.plugin_index check` passes on the wheel and the index
