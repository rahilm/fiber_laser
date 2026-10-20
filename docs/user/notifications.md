# Notifications

mlaser can tell you about the machine while you do other work: **sheet done**, **the queue waits
for the next sheet**, **alarm or fault**, **job finished or stopped** and **maintenance due**.
Messages go to a phone or a mailbox through one or more channels:

* **ntfy** (the free [ntfy.sh](https://ntfy.sh) service or your own ntfy server; the ntfy app on
  the phone subscribes to a topic),
* **e-mail** through your SMTP server,
* a **webhook**: an HTTP POST with a JSON body to your own system (Home Assistant, a stack light
  controller, an ERP), optionally signed.

Notifications are **off by default**.

## Send-only: nothing can be controlled

A notification only informs. The program sends; it never receives anything through these
channels, so no reply, link or webhook answer can start, stop, arm or move the machine. An alarm
or a fault must always be handled at the machine (docs/SAFETY.md: the e-stop chain and the
operator are the safety, not a message). Do not rely on a notification arriving: the network,
the server or the phone can fail, and notifications are rate-limited.

## Set up

Open **Notification settings** (command `notify.settings`, Engineer mode). Tick **Send
notifications**, enable at least one channel, fill it in and press **Send test**. The result of
the last sends ("sent" or the error) is shown at the bottom of the dialog; **Save** keeps the
settings. The command `notify.test` sends a test message without opening the dialog.

| Channel | Fields |
|---|---|
| ntfy | server (default `https://ntfy.sh`), topic, access token (optional, for a protected topic or your own server) |
| E-mail | SMTP server and port, connection security (STARTTLS, SSL/TLS, or none for a local relay only), user and password (empty user: no login), sender, recipients (comma separated) |
| Webhook | URL, signing secret (optional) |

Pick a long, hard-to-guess ntfy topic: on the public ntfy.sh server anyone who knows the topic
can read it. Messages contain the job and sheet names, the alarm text and the machine name.

### Events

Each event has an on/off switch and its channels: `all` (every enabled channel) or a list such as
`ntfy, email`. For example alarms to ntfy and the webhook, sheet done by e-mail only.

| Event | When |
|---|---|
| Sheet done | a queued sheet was cut completely |
| Queue waits for the next sheet | after a sheet, the queue waits for the operator to load the next one and confirm |
| Alarm or fault | a card alarm is raised (once per alarm code until it clears) or a fault is latched |
| Job finished or stopped | a cutting job ended (frames, laser tests and pauses are not jobs) |
| Maintenance due | an item of the maintenance plan became due or overdue |

Simulator, dry-run and practice runs send nothing unless **Include simulator, dry-run and
practice runs** is ticked (see [report.md](report.md) for how runs are classified).

### Limits and quiet hours

* **Same notification at most every** (default 60 s): the same alarm, the same sheet, is sent at
  most once per interval.
* **At most per hour** (default 60, bursts of 10): over all notifications together; 0 switches
  the limit off.
* **Quiet from / until** (`HH:MM`, local time; empty = off): nothing is sent in between, except
  alarms when **Alarms also during quiet hours** is ticked (default).

Sending never slows the machine down: an event only puts the message on a short queue, and one
background thread sends it with short timeouts. When a server does not answer, messages that do
not fit the queue are dropped (and counted in the log), never waited for.

## Secrets

The ntfy token, the SMTP password and the webhook secret are **not** stored in `config.json`.
They go to the operating system's keyring when the optional Python package `keyring` is installed
and working (Windows Credential Manager, macOS Keychain, Secret Service on Linux); otherwise to
`secrets.json` next to `config.json`, readable by your user only (permissions 0600 on Linux and
macOS; on Windows the file is in your own profile folder). The dialog never shows a stored
secret: leave the field empty to keep it, type a new one to replace it.

## Webhook format

`POST <url>` with `Content-Type: application/json` and the header `X-Mlaser-Event: <event>`:

```json
{"event": "sheet_done", "title": "Laser 1: Sheet done", "message": "flange: sheet 1/3 cut.",
 "priority": 3, "tags": ["white_check_mark"], "t": 1792483200.0, "machine": "Laser 1",
 "data": {"entry": "a1b2c3d4e5f6", "done": 1, "quantity": 3}}
```

`event` is one of `sheet_done`, `queue_waiting`, `alarm`, `job_finished`, `maintenance_due`,
`test`. With a signing secret the request carries `X-Mlaser-Signature: sha256=<hex>`, the
HMAC-SHA256 of the raw request body with the secret; check it on the receiving side with a
constant-time comparison before trusting the message. The receiver's answer is ignored (only an
HTTP error status is logged as a failure).

## Settings

All settings are in the configuration as `plugin.ext_notify.*` (for example
`plugin.ext_notify.enabled`, `ntfy_topic`, `smtp_host`, `event_alarm`, `channels_alarm`,
`min_interval_s`, `quiet_start`).
