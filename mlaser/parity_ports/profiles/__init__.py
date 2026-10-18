"""Optional ParityProfiles derived from ``ORIGINAL`` (one module each, registered on import).

A profile here only *selects* other implementations for a slot; the targets are
``"module:attribute"`` strings resolved on first use, so importing a profile imports nothing
outside the parity fence.  The default profile stays ``"original"``.
"""
