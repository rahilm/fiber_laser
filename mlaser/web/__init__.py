"""LAN monitor: an optional, read-only status page for phones and tablets on the local network.

Off by default.  Standard library only (``http.server``), no vendor cloud, no remote control:
the server answers ``GET`` / ``HEAD`` for four read-only resources and refuses every other method
(405) and every request without the access token (403).  See :mod:`.server` and
docs/user/lan_monitor.md.
"""
from .server import MonitorServer, new_token, local_addresses  # noqa: F401
from .snapshot import sheet_svg  # noqa: F401
