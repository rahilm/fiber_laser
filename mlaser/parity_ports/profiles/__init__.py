"""Optional, non-default parity profiles (new behaviour, no parity constraint).

Each module here defines and registers one :class:`~mlaser.parity_ports.profile.ParityProfile`
derived from ``ORIGINAL``; :func:`~mlaser.parity_ports.profile.get_profile` imports this package
when it is asked for a name it does not know, so ``use_profile("<name>")`` works without an
explicit import.  Importing a profile module must stay cheap (optional libraries are imported on
first use).

=================  ==========================================================================
``modern_motion``  ``"modern-ruckig"``: own look-ahead + Ruckig Community per segment (motion
                   slot ``plan_velocity``).  NOT validated on hardware.
=================  ==========================================================================
"""
from . import modern_motion  # noqa: F401  (registers "modern-ruckig")
