"""Optional ParityProfiles beside the default ``"original"`` (mlaser.parity_ports.profile).

Each module here builds a profile with :meth:`ParityProfile.derive` and registers it on import;
none of them edits a port.  Importing a profile module never requires its optional dependency:
the dependency is checked when the profile is used.
"""
