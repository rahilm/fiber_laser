# LAN monitor (read-only)

A status page for a phone or tablet on the local network: machine state with the reason,
job and sheet, progress, elapsed time and ETA, speed / power / position, active alarms with
remedies, the queue, and a picture of the current sheet (cut parts grey, the current one orange).
It refreshes every 2 seconds.

**It is off by default.** It cannot control the machine: the server only answers `GET`
requests for the page, the status and the picture; every other request is refused, and it has no
connection to the controller (it only receives the status the program publishes). Nothing is
sent to any cloud.

## Enable

Command `monitor.toggle` (or the setting `plugin.run.monitor_enabled`). A warning explains who
can see the page before it starts; while it runs, the Run screen shows **LAN monitor ON** with
the address, and the log has the full link.

Settings (`plugin.run.*`):

| Setting | Default | |
|---|---|---|
| `monitor_host` | `127.0.0.1` | the interface address it listens on. Set the machine PC's LAN address (e.g. `192.168.1.20`) to reach it from a phone; `127.0.0.1` keeps it on this PC |
| `monitor_port` | `8765` | TCP port |
| `monitor_token` | random | the access token in the link (`http://<host>:<port>/?t=<token>`); clear it to make a new one (old links stop working) |

Requests without the token get *403*. Share the link only with people who may see production
data, and keep the monitor off on untrusted or guest networks. The page is plain HTTP (no TLS):
use it on the shop network only.
