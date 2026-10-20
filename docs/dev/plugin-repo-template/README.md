# mlaser plugin repository template

Copy this folder into a new repository to publish an mlaser plugin from your own GitHub
repository (the standard: `docs/dev/PLUGIN_STANDARD.md` of mlaser).

1. Rename `mlaser_example` / `mlaser-example` / the id `example` everywhere, and edit
   `mlaser_example/mlaser-plugin.toml` (summary, homepage, **permissions**).
2. Write the plugin in `mlaser_example/__init__.py` (`register(ctx)`, `PLUGIN_API = 2`).
3. Keep `version` equal in `pyproject.toml` and the manifest.
4. Tag a release: `git tag v0.1.0 && git push --tags`. The workflow
   `.github/workflows/release.yml` builds the wheel, checks it exactly as the plugin manager
   will (RECORD hashes, pure wheel, manifest), and attaches the wheel, `SHA256SUMS` and
   `mlaser-plugin-index.json` to the GitHub release.

Shops then install it in mlaser with **Options+ > Plugins > Repositories > Add**
`github:OWNER/REPO` (or `mlaser plugin repo add github:OWNER/REPO`), then Browse > Install, and
a service engineer enables it. Every later release is offered as an update.
