"""Notification back ends: ntfy, e-mail (SMTP) and a generic webhook.

Each sender takes the :class:`~mlaser.notify.core.Notification`, the channel configuration
(a dict built from the settings) and the channel's secret, and raises on failure.  They run on
the notifier's worker thread only, with short timeouts; they open outgoing connections and
receive nothing that could act on the machine.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable

USER_AGENT = "mlaser-notify/1"
TIMEOUT_S = 8.0
SIGNATURE_HEADER = "X-Mlaser-Signature"


class ChannelError(RuntimeError):
    """A channel is not configured or the server refused the notification."""


def _check_url(url: str) -> str:
    u = urllib.parse.urlsplit(str(url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ChannelError(f"not an http(s) URL: {url!r}")
    return urllib.parse.urlunsplit(u)


def _header_text(s: str) -> str:
    """An HTTP header value: plain when it is ASCII, else RFC 2047 (ntfy decodes it)."""
    s = " ".join(str(s).split())
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


def _post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - http(s) checked
            resp.read(4096)
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        raise ChannelError(f"HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ChannelError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc


def send_ntfy(n: Any, cfg: dict, secret: str = "", timeout: float = TIMEOUT_S) -> str:
    server = _check_url(cfg.get("server") or "https://ntfy.sh")
    topic = str(cfg.get("topic") or "").strip()
    if not topic:
        raise ChannelError("ntfy: no topic set")
    url = server.rstrip("/") + "/" + urllib.parse.quote(topic, safe="")
    headers = {"Title": _header_text(n.title), "Priority": str(max(1, min(5, int(n.priority)))),
               "Content-Type": "text/plain; charset=utf-8"}
    if n.tags:
        headers["Tags"] = _header_text(",".join(n.tags))
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    code = _post(url, n.message.encode("utf-8"), headers, timeout)
    return f"ntfy {code}"


def webhook_body(n: Any, machine: str = "") -> bytes:
    return json.dumps({"event": n.kind, "title": n.title, "message": n.message, "priority": n.priority,
                       "tags": list(n.tags), "t": n.t, "machine": machine, "data": n.data},
                      ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def signature(body: bytes, secret: str) -> str:
    """``sha256=<hex>`` of the HMAC-SHA256 of the request body with the shared secret."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def send_webhook(n: Any, cfg: dict, secret: str = "", timeout: float = TIMEOUT_S) -> str:
    url = _check_url(cfg.get("url") or "")
    body = webhook_body(n, str(cfg.get("machine") or ""))
    headers = {"Content-Type": "application/json; charset=utf-8", "X-Mlaser-Event": n.kind}
    if secret:
        headers[SIGNATURE_HEADER] = signature(body, secret)
    code = _post(url, body, headers, timeout)
    return f"webhook {code}"


def _local_name() -> str:
    """The name this host gives in EHLO, without a DNS lookup.  smtplib's default (and
    ``make_msgid``'s) is ``socket.getfqdn()``, a reverse lookup that no socket timeout covers:
    on macOS it took tens of seconds per call (CI: a mail to a local server took > 60 s)."""
    try:
        name = socket.gethostname()
    except OSError:
        name = ""
    name = name.strip().rstrip(".")
    return name if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})", name) else "localhost"


def send_email(n: Any, cfg: dict, secret: str = "", timeout: float = TIMEOUT_S) -> str:
    host = str(cfg.get("host") or "").strip()
    to = [a.strip() for a in str(cfg.get("to") or "").replace(";", ",").split(",") if a.strip()]
    sender = str(cfg.get("from") or "").strip()
    if not host or not to or not sender:
        raise ChannelError("e-mail: SMTP server, sender and recipient are required")
    port = int(cfg.get("port") or 0)
    security = str(cfg.get("security") or "starttls")
    msg = EmailMessage()
    msg["Subject"] = n.title
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    local = _local_name()
    domain = sender.rpartition("@")[2].strip(" <>") or local
    msg["Message-ID"] = make_msgid("mlaser", domain=domain)
    msg["X-Mlaser-Event"] = n.kind
    msg.set_content(n.message)
    ctx = ssl.create_default_context()
    smtp: smtplib.SMTP
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port or 465, local_hostname=local, timeout=timeout, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port or (587 if security == "starttls" else 25), local_hostname=local,
                            timeout=timeout)
    try:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=ctx)
            smtp.ehlo()
        user = str(cfg.get("user") or "")
        if user:
            smtp.login(user, secret)
        smtp.send_message(msg, sender, to)
    except smtplib.SMTPException as exc:
        raise ChannelError(f"e-mail: {exc}") from exc
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 - the server already closed
            pass
    return f"e-mail to {len(to)}"


#: channel name -> sender
SENDERS: dict[str, Callable[..., str]] = {"ntfy": send_ntfy, "email": send_email, "webhook": send_webhook}
CHANNELS = tuple(SENDERS)

__all__ = ["send_ntfy", "send_email", "send_webhook", "signature", "webhook_body", "ChannelError", "SENDERS",
           "CHANNELS", "SIGNATURE_HEADER"]
