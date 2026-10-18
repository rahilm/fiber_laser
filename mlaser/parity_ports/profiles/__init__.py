"""Optional, non-default parity profiles derived from ``ORIGINAL`` (docs/dev/ARCHITECTURE.md §3,
"Adding a modern algorithm").

Each module here defines and registers one :class:`~mlaser.parity_ports.profile.ParityProfile`;
:func:`~mlaser.parity_ports.profile.get_profile` imports this package when it is asked for a
name it does not know, so ``use_profile("<name>")`` works without an explicit import.  Importing
a profile module must stay cheap (optional libraries are imported on first use).

=================  ==========================================================================
``reference``      ``"original-reference"``: the statement-level reference implementations of
                   the kernels ``ORIGINAL`` runs as fast drop-ins (same output, bit for bit)
``modern_motion``  ``"modern-ruckig"``: own look-ahead + Ruckig Community per segment (motion
                   slot ``plan_velocity``).  NOT validated on hardware.
=================  ==========================================================================
"""
from . import modern_motion, reference  # noqa: F401  (register their profiles)
