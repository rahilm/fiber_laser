"""Optional profiles derived from ``ORIGINAL`` (docs/dev/ARCHITECTURE.md §3, "Adding a modern
algorithm").  Importing a module here registers its profile; none of them is the default.

* :mod:`.reference`: ``"original-reference"``, the statement-level reference implementations
  of kernels that ``ORIGINAL`` runs as fast drop-ins (same output, bit for bit).
"""
