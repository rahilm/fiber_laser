"""Accessibility helpers: screen-reader announcements and accessible names (UI evaluation F7).

``announce(widget, text, assertive=False)`` tells assistive technology about a change the user
did not cause by focusing something: an alarm, a fault, a job that started or ended, a safety
state change. On Qt 6.8+ it is a ``QAccessibleAnnouncementEvent`` (VoiceOver, Narrator/UIA
notifications, AT-SPI announcements); ``assertive`` interrupts the current speech (alarms and
faults), otherwise it waits (polite). Older Qt gets an ``Alert`` event (or ``NameChanged``) on
the widget, whose accessible name/description carry the text.

The same text is never announced twice in a row for one widget (a refresh timer that sets the
same banner text every tick stays silent).

``name(widget, text, description="")`` sets an accessible name (and description) and is a
no-op when the text has not changed. ``last_announcements`` keeps the recent announcements for
diagnostics; ``mark()`` / ``since(mark)`` give the ones made after a point (the deque is
bounded, so its length is no position: once full it stays full).
"""
from __future__ import annotations

import collections
import weakref
from typing import Any, Optional

from PySide6.QtCore import QObject
from PySide6.QtGui import QAccessible, QAccessibleEvent

#: (text, assertive) of the latest announcements, newest last (tests, diagnostics)
last_announcements: collections.deque = collections.deque(maxlen=50)
#: announcements made since the start (never wraps, unlike the bounded deque's length)
_sent = [0]
_last_text: "weakref.WeakKeyDictionary[QObject, str]" = weakref.WeakKeyDictionary()


def announce(widget: Optional[QObject], text: str, assertive: bool = False, *, force: bool = False) -> bool:
    """Announce ``text`` for ``widget``. Returns True when an announcement was sent (False when
    the text is empty or the same as this widget's previous one; ``force`` repeats it)."""
    text = (text or "").strip()
    if widget is None or not text:
        return False
    try:
        if not force and _last_text.get(widget) == text:
            return False
        _last_text[widget] = text
    except TypeError:                       # not weak-referenceable: always announce
        pass
    last_announcements.append((text, assertive))
    _sent[0] += 1
    try:
        _send(widget, text, assertive)
    except RuntimeError:                    # the widget was deleted meanwhile
        return False
    return True


def mark() -> int:
    """A position in the announcement history (see :func:`since`)."""
    return _sent[0]


def since(position: int) -> list:
    """(text, assertive) of the announcements made after :func:`mark` returned ``position``,
    oldest first (at most the deque's length)."""
    n = _sent[0] - position
    return list(last_announcements)[-n:] if n > 0 else []


def reset(widget: QObject) -> None:
    """Forget ``widget``'s last announcement (the next one is spoken even if it repeats)."""
    try:
        _last_text.pop(widget, None)
    except TypeError:
        pass


def _send(widget: QObject, text: str, assertive: bool) -> None:
    if not QAccessible.isActive():
        return                              # no assistive technology listening: nothing to do
    try:
        from PySide6.QtGui import QAccessibleAnnouncementEvent    # Qt 6.8+
    except ImportError:
        QAccessibleAnnouncementEvent = None  # type: ignore[assignment,misc]  # noqa: N806
    if QAccessibleAnnouncementEvent is not None:
        ev: Any = QAccessibleAnnouncementEvent(widget, text)
        ev.setPoliteness(QAccessible.AnnouncementPoliteness.Assertive if assertive
                         else QAccessible.AnnouncementPoliteness.Polite)
        QAccessible.updateAccessibility(ev)
        return
    kind = QAccessible.Event.Alert if assertive else QAccessible.Event.NameChanged
    QAccessible.updateAccessibility(QAccessibleEvent(widget, kind))


def name(widget: Any, text: str, description: Optional[str] = None) -> None:
    """Set ``widget``'s accessible name (and description when given), only on a change; a
    change of an already named widget is reported to assistive technology."""
    if widget is None:
        return
    if widget.accessibleName() != text:
        widget.setAccessibleName(text)
    if description is not None and widget.accessibleDescription() != description:
        widget.setAccessibleDescription(description)
        if QAccessible.isActive():
            QAccessible.updateAccessibility(QAccessibleEvent(widget, QAccessible.Event.DescriptionChanged))


def translator_for(widget: Any) -> Any:
    """The translator of ``widget``'s main window (``tr_``), else the application's."""
    try:
        t = getattr(widget.window(), "tr_", None)
    except (AttributeError, RuntimeError):
        t = None
    if t is None:
        from .i18n import translator
        t = translator()
    return t


__all__ = ["announce", "name", "reset", "last_announcements", "mark", "since", "translator_for"]
