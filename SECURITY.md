# Security and machine-safety policy

mlaser drives a multi-kilowatt fiber laser and a moving gantry. A software fault can hurt
people, not only data. This policy covers **two kinds of report**, and both are handled the
same way: privately, first.

## Report privately, never in a public issue

Use GitHub's **private vulnerability reporting**:

1. Open the repository on GitHub: <https://github.com/rahilm/fiber_laser>.
2. Go to **Security** > **Advisories** > **Report a vulnerability**
   (direct link: <https://github.com/rahilm/fiber_laser/security/advisories/new>).
3. Describe the problem (see "What to include" below) and submit. Only the maintainers see it.

Please do **not** open a public issue, discussion or pull request for a security or
machine-safety problem until a fix is released, and do not post exploit details, crafted files
or probe scripts publicly. If the private reporting form is not available to you, open a public
issue that says only "security / safety report: please contact me" (no details), and a
maintainer will set up a private channel.

## What counts as a machine-safety issue

Anything that could, without the operator intending it:

* **fire the laser**: emission while disarmed, in dry-run (`--laser-off`), during Frame or Dry
  run, after Stop, after a fault, or at a power / frequency the layer does not ask for;
* **move the axes**: motion after Stop / Hold / e-stop / a latched fault, a jog that does not
  stop on release, a job that starts from the wrong position or outside the travel limits,
  wrong scaling or direction;
* **defeat a guard or an interlock**: a card command that passes the command guard
  (docs/SAFETY.md §3) when it should be refused, a door / gas / laser-alarm / follower interlock
  that does not trip, the "PC alive" heartbeat that keeps toggling when the program is hung,
  a plugin or file that disables any of these;
* hide a fault: an alarm or a fault that is not shown, or a "Ready" state while one is latched;
* switch gas or other process outputs unexpectedly.

Also report findings in the simulator alone: most of the safety tests run against it, and a
simulator gap can hide a real-machine hazard.

If you have an **immediate hazard on a machine**, make it safe first (e-stop, laser key off,
power off) and do not rely on software to protect people. mlaser is not a certified machine
controller (README, "Safety warning").

## What counts as a security issue

* code execution, file writes outside the user's data directories, or resource exhaustion
  (memory, CPU, file descriptors) from an opened file (`.mlz`, `.chf`, `.dxf`, `.svg`, G-code,
  parameter / language imports);
* the LAN monitor (`mlaser/web`): authentication bypass, token leaks, injection, denial of
  service of the controlling process;
* anything that lets a network peer or a file change the machine configuration or the safety
  settings.

Plugins run with the full privileges of the user (docs/dev/PLUGINS.md); a malicious plugin
that the user installed and enabled is out of scope, but a way for a plugin to bypass the
safety guard through a *public* API, or to load without being enabled, is in scope. So are
holes in the plugin manager's verification (docs/dev/PLUGIN_STANDARD.md §7): a wheel that
installs although its hash, `RECORD`, manifest or index entry does not match, a file written
outside the plugin site directory, a top-level module that shadows a core module, a plugin that
reaches a host, the secrets or the controller its declared permissions do not allow through
the plugin API, or a repository index or download that exhausts memory or disk.

**Plugin authors**: report a vulnerability in a third-party plugin to its author; report one in
a plugin of this repository (`plugins/`) here.

## What to include

* the mlaser version and commit (the About dialog, the first line of `mlaser.log`), the OS and
  Python;
* simulator or real machine (card, laser source, follower), and the command line;
* steps to reproduce, a minimal file or a script (attach it to the private report);
* what happened, what you expected, and a **diagnostic bundle**: **Help › Save diagnostic
  bundle…** (also in the log viewer and the fault and crash-recovery dialogs) or
  `mlaser-cli diag-bundle out.zip`. It is a zip on your computer, never uploaded: versions, the
  settings with tokens, passwords, notification credentials, e-mail addresses and home-folder
  paths removed, the machine parameters, the safety transitions and halts, the last card messages,
  the alarm history and the logs (newest first, size-limited). The drawing is included only when
  you tick it. Read it before you attach it ([docs/user/settings.md](docs/user/settings.md) and
  the bundle's own `README.txt` list what it holds).

## The update check

The **update check** is off by default and never switched on automatically (Help › Check for
updates asks once; Settings › Network & API › Updates also asks at start). It makes one HTTPS `GET` of
`https://api.github.com/repos/rahilm/fiber_laser/releases?per_page=20` with `User-Agent:
mlaser-update-check`, `Accept: application/vnd.github+json` and `Accept-Encoding: identity`: no
version, no identifier, no cookie or token; it never downloads or installs anything
([docs/user/settings.md](docs/user/settings.md), "Update check: exactly what is sent").

## What happens next

* An acknowledgement within **7 days**; a first assessment (accepted, severity, plan) within
  **14 days**. This is a volunteer project: the times are targets, not guarantees.
* Machine-safety issues are handled before any other work. A fix comes with a regression test
  (`tests/safety/` for safety behaviour) and a note in [CHANGELOG.md](CHANGELOG.md).
* A GitHub security advisory is published with the fix. You are credited unless you prefer not
  to be.

## Supported versions

mlaser is pre-release software (0.1.0 release candidates). Only the latest release and the
`main` branch receive fixes; there are no backports to older release candidates.

| Version | Supported |
|---|---|
| `main` | yes |
| latest `0.1.0rcN` | yes |
| older release candidates | no: update |
