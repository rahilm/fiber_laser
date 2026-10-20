"""Notifications: sheet done, queue waiting for the next sheet, alarm / fault, job finished,
maintenance due, sent through ntfy, e-mail (SMTP) or a generic webhook.

Qt-free and send-only: nothing can be controlled through a notification, and the package holds
no route to the machine (import-linter contract "Automation API and notifications cannot reach
the machine").  The application wires it in :mod:`mlaser.ui.ext_notify`; see
docs/user/notifications.md.

* :class:`~.core.Notifier`: filters, rate-limits and queues; one worker thread sends;
* :class:`~.wiring.EventWiring`: controller events, the queue runner and the maintenance check
  -> notifications;
* :class:`~.secrets.SecretStore`: the channel secrets (keyring, else a 0600 file);
* :mod:`.backends`: ntfy, e-mail, webhook.
"""
from .backends import CHANNELS, ChannelError, signature  # noqa: F401
from .core import EVENTS, SECRETS, SETTING_DEFAULTS, TEST, Notification, Notifier, Result, make  # noqa: F401
from .secrets import SecretStore  # noqa: F401
from .wiring import EventWiring  # noqa: F401

__all__ = ["CHANNELS", "ChannelError", "signature", "EVENTS", "SECRETS", "SETTING_DEFAULTS", "TEST",
           "Notification", "Notifier", "Result", "make", "SecretStore", "EventWiring"]
