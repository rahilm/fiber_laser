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

## Limits

The monitor runs inside the program that controls the machine, so it protects that program from
busy or misbehaving clients on the network:

| Limit | Value | What happens |
|---|---|---|
| idle connection | 10 s | a connection that sends nothing (or an unfinished request) is closed |
| connection lifetime | 30 s | a connection is closed after 30 s, however slowly the client sends; a browser simply reconnects |
| connections at once | 16, at most 4 per client address | more connections are answered *503* and closed at once |
| requests per client address | 5 per second (bursts of 20) | more requests are answered *429* (*Retry-After: 1*) |

One phone or tablet with the page open uses well under these limits (one status request every
2 seconds, the sheet picture only when it changes). The program never prepares a page for a
request: it publishes the status twice a second, and the sheet picture is redrawn only when the
document, the cut parts or the current part change. The log records the requested path only,
never the access token.
