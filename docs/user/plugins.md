# Plugins

A plugin adds features to mlaser: a workspace, commands, an importer (for example the
**Onshape** plugin, which turns the parts of an Onshape assembly into order lines). Plugins
come from **repositories**; mlaser checks every plugin before it installs it, and nothing
installed runs until a service engineer enables it.

> **A plugin is program code.** It runs inside mlaser with your user account's rights. mlaser
> shows what a plugin declares it will use (its *permissions*) and holds it to that on its own
> interfaces, but this is not a sandbox. Install only plugins from authors you trust.

### What the permissions protect, and what they do not

The permissions and the machine-authority grant are a **record of what you agreed to** and a
**guard against mistakes** in a plugin you trust. They are **not a lock against a hostile
plugin**.

* What they do: a plugin that uses mlaser's plugin interface as documented cannot contact a
  server it did not declare, read another plugin's credentials, change the drawing, the orders
  or the queue without the permission, or touch the machine controller without the machine
  authority you granted. It can change only its own settings: not the plugin grants, not the
  safety settings, not another plugin's settings. Even with machine authority, everything it
  sends to the machine passes the same safety checks as your own commands (pre-flight, the
  start gate, the guard that refuses laser output while disarmed).
* What they do not: a plugin is Python code inside mlaser. Written on purpose to get round
  these checks, it can (for example by importing Python modules directly or reaching into the
  main window). The checks cannot stop that. The protection against a hostile plugin is not
  installing it. The machine's own hardware safety (E-stop, door and interlock wiring, the
  watchdog relay) does not depend on any software.

Open the Plugin manager with **Tools › Plugins** (macOS: the **mlaser** menu › **Plugins…**), from
the *Plugins* page of Settings, or from the command palette (Ctrl+K, "Plugins"). Engineer mode can look; **installing, enabling, updating and removing need Service
mode**.

## Installed

Every plugin found at start-up, with its version, where it comes from (the plugin manager, or
installed into the Python environment with pip), its **status** and its permissions:

| status | meaning |
|---|---|
| loaded | enabled and running |
| failed | enabled but it failed to load: the reason is in the log panel |
| enabled (restart) | enabled now, it loads at the next start |
| disabled | installed, not enabled |
| changed | it was enabled, but its entry point or its permissions changed since: enable it again |
| legacy | an old-style plugin without a manifest: no declared permissions |

* **Enable...** shows the permissions and the warning and asks for confirmation. A plugin that
  asks for the **machine** controller asks a second question: grant it *machine authority*.
  With it the plugin may use the machine controller: move the machine, fire the laser once the
  machine is armed, arm it, reset faults, hold the "PC alive" heartbeat and change the safety
  settings, each through mlaser's checked and audited interface. Without it the plugin gets no
  controller at all. Answer no unless you know why the plugin needs it. Only you grant it,
  here (or in **Tools › Enabled third-party plugins**); a plugin cannot grant it to itself through its interface,
  but this is a safeguard for honest plugins, not a lock (see above).
* **Disable** stops it at the next start.
* **Update** appears after *Check for updates* finds a newer compatible version. If the new
  version asks for different permissions it is disabled until you enable it again.
* **Uninstall...** removes exactly the plugin's files; its settings and data folder stay.
* **Network hosts...** lets a plugin contact extra servers besides the ones it declares, for
  example your company's own Onshape Enterprise server (`acme.onshape.com`). Exact host names
  only.
* **Homepage** opens the author's page.

## Browse

The plugins of your repositories. Type to search; select one to see its description, the
version that fits this mlaser, its permissions and what it requires. **Install...** downloads
it, checks it, shows what it declares and its checksum, and installs it after you confirm.
Installing does **not** enable it.

## Repositories

A repository is a list of plugins (an *index*) that someone publishes. mlaser comes with the
project's own repository. **Add** one of:

* `github:owner/repo`: the latest release of a GitHub repository that publishes mlaser plugins
  (also `https://github.com/owner/repo`);
* an index URL: `https://.../mlaser-plugin-index.json`.

Only https addresses are accepted.

## Install from file or URL

* **Install from file...**: a plugin wheel (`.whl`) you were given.
* **Install from URL...**: `https://.../name.whl#sha256=...` (the author publishes the
  checksum); without `#sha256=` mlaser shows the checksum of what it downloaded and you confirm
  that it is the author's. `github:owner/repo` works here too.

## What mlaser checks before installing

The download is the one the repository lists (sha256 checksum and size); the package is
complete and untampered (every file's checksum), pure Python, declares a valid manifest that
matches the repository; it was made for this version of mlaser; everything it needs is already
in mlaser; and none of its files would replace part of mlaser or another plugin. If anything is
wrong it is refused with the reason, and nothing is written. Every install, update, removal,
refusal and enable is written to the audit log.

**Restart to apply.** Plugins are loaded when mlaser starts: after a change the Plugin manager
shows *Restart mlaser to apply the plugin changes*.

## Command line

The same with `mlaser plugin ...` (details: `mlaser plugin --help`):

```text
mlaser plugin list                         installed plugins and their status
mlaser plugin search onshape               search the repositories
mlaser plugin info onshape                 versions, permissions, requirements
mlaser plugin install onshape              or github:owner/repo, https://.../x.whl#sha256=..., x.whl
mlaser plugin update --check               newer versions (mlaser plugin update [ID] installs them)
mlaser plugin uninstall onshape
mlaser plugin enable onshape --yes         enable (never grants machine authority)
mlaser plugin disable onshape
mlaser plugin hosts onshape --add acme.onshape.com --yes
mlaser plugin repo add github:owner/repo   | repo remove URL | repo list
```

`--json` prints one JSON object. Exit codes: 0 ok, 2 usage, 3 not found (plugin, file,
repository), 4 failed (network, configuration not writable), 6 refused (a check failed, http
instead of https, or `enable` / `hosts --add` without `--yes`). Installing never enables;
`enable` prints the permissions and needs `--yes`, because it is the command-line form of the
Service-mode decision. Machine authority can only be granted in the Plugin manager, at the
machine. Close mlaser before changing plugins from the command line: they apply at the next
start.

## See also

* For plugin authors: [Writing a plugin](../dev/PLUGINS.md) and the
  [plugin standard](../dev/PLUGIN_STANDARD.md).
* [Safety](safety.md): what software can and cannot protect against.
