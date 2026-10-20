# Developer documentation

| Document | Content |
|---|---|
| [Architecture](ARCHITECTURE.md) | package layers, import rules, the parity fence, services |
| [Plugins](PLUGINS.md) | writing a plugin: `PluginContext`, workspaces, docks, commands, hooks |
| [Automation API](API.md) | the local REST + WebSocket API: resources, events, queue intake, why there is no remote control |
| [Performance](PERFORMANCE.md) | measurements, the optional native kernel ([NATIVE_KERNEL.md](NATIVE_KERNEL.md)) |
| [Testing](TESTING.md) | test suites and markers, goldens, the contract checked on every commit |
| [Parity](PARITY.md) | how behaviour is kept identical to the original, profiles, the Wine harness |
| [Native project format](project-format.md) | the `.mlz` project file |
| [Translations and fonts](i18n.md) | the string catalogue, Qt Linguist, the i18n check, the cutting-text font |
| [Release](RELEASE.md) | versioning, packaging, the release checklist |
| [Safety architecture](../SAFETY.md) | state machine, command policy, pre-flight, streaming, watchdog |
| [Bundled assets](../ASSETS.md) | provenance and licence of every bundled resource |
| [Roadmap](../ROADMAP.md) | plan of record |

The reverse-engineering notes are in [docs/re](../re/README.md); the user guide is in
[docs/user](../user/README.md). The architecture, UX and library reviews are in
[docs/review](../review/REVIEW.md).

## Documentation layout

* `docs/user/`: task guides for operators; shipped with the program and shown by **Help**.
* `docs/dev/`: how the code is built, tested and released.
* `docs/re/`: how the original program works (observed / inferred / unknown, with addresses);
  `docs/re/history/` keeps the original project brief.
* `docs/review/`: the Wave 2 reviews (not shipped).

`tests/test_docs_links.py` checks every relative link and anchor, and every `docs/...md` path
named in the code. The help browser ships `docs/user`, `docs/dev`, `docs/re` and the top-level
files (`mlaser/diag/helpdocs.py` `SHIPPED`).

Notation in the RE notes: **O** observed (an address or a file offset is cited), **I** inferred
(deduced, not confirmed in code), **U** unknown, **R** a rebuild choice.
