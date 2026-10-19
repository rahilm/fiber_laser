"""``python -m mlaser._native [--require] [--json]``: is the C emitter kernel built, does it pass
its self-check, and does the default profile select it?  Exit status 1 with ``--require`` when
the default profile does not run the kernel (the wheel test of cibuildwheel, CI)."""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mlaser._native", description=__doc__.split("\n")[0])
    ap.add_argument("--require", action="store_true", help="exit 1 unless the C kernel is active")
    ap.add_argument("--json", action="store_true", help="print the status as JSON")
    a = ap.parse_args(argv)
    from mlaser.parity_ports import kernel_status

    st = kernel_status()
    print(json.dumps(st.to_dict(), indent=1) if a.json else st.text())
    return 1 if (a.require and not st.native) or st.active == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
