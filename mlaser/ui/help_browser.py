"""Help browser (``app.help``, mf23): the packaged Markdown documentation, offline.

* topic tree grouped into the sections of :data:`mlaser.diag.helpdocs.SECTIONS` (User guide,
  Developer, Reverse engineering), the overview first and About last;
* Markdown rendered by ``QTextBrowser.setMarkdown`` (palette colours: light and dark themes);
* relative links between documents, with ``#anchors`` (GitHub heading slugs), Back / Forward
  (Alt+Left / Alt+Right, the mouse back/forward buttons) and Contents (Alt+Home);
* ranked full-text search (:class:`mlaser.diag.helpdocs.HelpIndex`, Ctrl+F): a result opens
  the topic at the section and highlights the words; Esc returns to the topic tree;
* external links (http, mailto) are handed to the system browser only when clicked.

Where the documents come from: :func:`mlaser.diag.helpdocs.docs_root`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QSplitter, QStackedWidget, QTextBrowser, QTextEdit, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from mlaser.diag import helpdocs as H
from mlaser.ui import icons

ROLE_REL = Qt.UserRole
ABOUT = "::about"


def _translator(tr) -> Callable[..., str]:
    """``t(key, default, **values)`` through the window's Translator (English default)."""
    def t(key: str, default: str, **kw) -> str:
        text = default
        if tr is not None:
            try:
                if kw:
                    return tr.format(key, default=default, **kw)
                if tr.has(key):
                    text = tr.tr(key)
            except Exception:  # noqa: BLE001
                pass
        return text.format(**kw) if kw else text
    return t


@dataclass
class _Place:
    rel: str
    anchor: str = ""
    scroll: Optional[int] = None


class HelpBrowser(QDialog):
    """The help window.  ``docs_dir`` overrides :func:`helpdocs.docs_root` (tests)."""

    def __init__(self, parent: Optional[QWidget] = None, tr=None, docs_dir: Optional[str] = None,
                 about: Optional[Callable[[], str]] = None):
        super().__init__(parent)
        self.t = _translator(tr)
        self.root = docs_dir or H.docs_root() or ""
        self.about = about or H.about_text
        self.topics = H.topics(self.root) if self.root else []
        self.by_rel = {tp.rel: tp for tp in self.topics}
        self._index: Optional[H.HelpIndex] = None
        self.history: list[_Place] = []
        self.pos = -1
        self.current = ""
        self._anchors: dict[str, int] = {}
        self.setWindowTitle(self.t("help.title", "Help"))
        self.resize(1100, 720)
        self._build()
        self.home()

    # ------------------------------------------------------------------ layout
    def _button(self, icon_name: str, key: str, default: str, slot, shortcut) -> QToolButton:
        b = QToolButton()
        b.setIcon(icons.icon(icon_name))
        text = self.t(key, default)
        b.setToolTip(f"{text} ({QKeySequence(shortcut).toString(QKeySequence.NativeText)})")
        b.setAccessibleName(text)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        QShortcut(QKeySequence(shortcut), self, activated=slot)
        return b

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.back_btn = self._button("arrow-left", "help.back", "Back", self.back, QKeySequence.Back)
        self.fwd_btn = self._button("arrow-right", "help.forward", "Forward", self.forward,
                                    QKeySequence.Forward)
        self.home_btn = self._button("home", "help.contents", "Contents", self.home, "Alt+Home")
        for b in (self.back_btn, self.fwd_btn, self.home_btn):
            top.addWidget(b)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(self.t("help.search_placeholder", "Search the help (Ctrl+F)"))
        self.search.setAccessibleName(self.t("help.search", "Search"))
        self.search.returnPressed.connect(self.do_search)
        self.search.textChanged.connect(lambda s: s.strip() or self.show_tree())
        self.search.installEventFilter(self)
        QShortcut(QKeySequence.Find, self, activated=self._focus_search)
        top.addWidget(self.search, 1)
        lay.addLayout(top)

        sp = QSplitter()
        self.side = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName(self.t("help.topics", "Topics"))
        self.tree.currentItemChanged.connect(self._tree_changed)
        self._fill_tree()
        self.side.addWidget(self.tree)
        res = QWidget()
        rl = QVBoxLayout(res)
        rl.setContentsMargins(0, 0, 0, 0)
        self.results_label = QLabel()
        self.results_label.setWordWrap(True)
        self.results = QListWidget()
        self.results.setAccessibleName(self.t("help.results", "Search results"))
        self.results.setWordWrap(True)
        self.results.setSpacing(3)
        self.results.setAlternatingRowColors(True)
        self.results.itemActivated.connect(self._open_hit)
        self.results.itemClicked.connect(self._open_hit)
        self.results.installEventFilter(self)
        rl.addWidget(self.results_label)
        rl.addWidget(self.results, 1)
        self.side.addWidget(res)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.anchorClicked.connect(self._link)
        self.view.setAccessibleName(self.t("help.page", "Help page"))
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)
        sp.addWidget(self.side)
        sp.addWidget(self.view)
        sp.setStretchFactor(1, 1)
        sp.setSizes([300, 800])
        lay.addWidget(sp, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

    def _fill_tree(self) -> None:
        self.items: dict[str, QTreeWidgetItem] = {}
        groups: dict[str, QTreeWidgetItem] = {}
        for sec, key, default in H.SECTIONS:
            if any(tp.section == sec for tp in self.topics):
                g = QTreeWidgetItem([self.t(key, default)])
                index_rel = f"{sec}/README.md"           # the section's index page, if any
                g.setData(0, ROLE_REL, index_rel if index_rel in self.by_rel else "")
                if index_rel in self.by_rel:
                    self.items[index_rel] = g
                else:
                    g.setFlags(Qt.ItemIsEnabled)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                groups[sec] = g
        for tp in self.topics:
            if tp.rel in self.items:
                continue
            it = QTreeWidgetItem([tp.title])
            it.setData(0, ROLE_REL, tp.rel)
            it.setToolTip(0, tp.rel)
            self.items[tp.rel] = it
            if tp.section in groups:
                groups[tp.section].addChild(it)
            else:
                self.tree.addTopLevelItem(it)
        for sec, *_ in H.SECTIONS:
            if sec in groups:
                self.tree.addTopLevelItem(groups[sec])
                groups[sec].setExpanded(sec == "user")
        about = QTreeWidgetItem([self.t("mf24", "About")])
        about.setData(0, ROLE_REL, ABOUT)
        self.items[ABOUT] = about
        self.tree.addTopLevelItem(about)

    # ------------------------------------------------------------------ navigation
    def home(self) -> None:
        first = "README.md" if "README.md" in self.by_rel else (self.topics[0].rel if self.topics else ABOUT)
        self.open(first)

    def open(self, rel: str, anchor: str = "", push: bool = True, scroll: Optional[int] = None,
             highlight: Optional[list[str]] = None) -> bool:
        """Show topic ``rel`` (relative to the docs root) at ``anchor``; False when unknown."""
        if rel != ABOUT and rel not in self.by_rel:
            return False
        if push:
            self._remember()
            del self.history[self.pos + 1:]
            self.history.append(_Place(rel, anchor))
            self.pos = len(self.history) - 1
        if rel != self.current:
            self._render(rel)
        self.status.clear()
        it = self.items.get(rel)
        if it is not None and self.tree.currentItem() is not it:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(it)
            self.tree.blockSignals(False)
        self._highlight(highlight or [], anchor)
        if scroll is not None:
            self.view.verticalScrollBar().setValue(scroll)
        elif not highlight:
            self.scroll_to(anchor)
        self._update_buttons()
        return True

    def _render(self, rel: str) -> None:
        self.current = rel
        if rel == ABOUT:
            self.view.setPlainText(self.about())
            self._anchors = {}
            return
        tp = self.by_rel[rel]
        self.view.document().setBaseUrl(QUrl.fromLocalFile(tp.path))
        self.view.setSearchPaths([os.path.dirname(tp.path)])
        self.view.setMarkdown(H.read_topic(tp.path))
        self._anchors = {}
        seen: dict[str, int] = {}
        b = self.view.document().begin()
        while b.isValid():
            if b.blockFormat().headingLevel() > 0:
                base = H.slugify(b.text())
                n = seen.get(base, 0)
                seen[base] = n + 1
                self._anchors[base if n == 0 else f"{base}-{n}"] = b.position()
            b = b.next()

    def scroll_to(self, anchor: str) -> None:
        sb = self.view.verticalScrollBar()
        pos = self._anchors.get(anchor) if anchor else None
        if pos is None:
            sb.setValue(0)
            return
        block = self.view.document().findBlock(pos)
        self.view.setTextCursor(QTextCursor(block))
        top = self.view.document().documentLayout().blockBoundingRect(block).top()
        sb.setValue(int(top))

    def _highlight(self, words: list[str], anchor: str) -> None:
        doc = self.view.document()
        sels = []
        first: Optional[QTextCursor] = None
        start = 0
        if anchor in self._anchors:      # the first match in the section body, not its heading
            nb = doc.findBlock(self._anchors[anchor]).next()
            start = nb.position() if nb.isValid() else self._anchors[anchor]
        colour = self.palette().highlight()
        text_colour = self.palette().highlightedText()
        for w in words:
            c = doc.find(w, 0)
            while not c.isNull():
                s = QTextEdit.ExtraSelection()
                s.cursor = c
                s.format.setBackground(colour)
                s.format.setForeground(text_colour)
                sels.append(s)
                if c.selectionStart() >= start and (first is None or c.selectionStart() < first.selectionStart()):
                    first = QTextCursor(c)
                c = doc.find(w, c)
        self.view.setExtraSelections(sels)
        if first is None and sels:
            first = QTextCursor(sels[0].cursor)
        if first is not None:
            self.scroll_to(anchor)
            first.clearSelection()
            self.view.setTextCursor(first)
            self.view.ensureCursorVisible()

    def _remember(self) -> None:
        if 0 <= self.pos < len(self.history):
            self.history[self.pos].scroll = self.view.verticalScrollBar().value()

    def back(self) -> None:
        if self.pos > 0:
            self._remember()
            self.pos -= 1
            p = self.history[self.pos]
            self.open(p.rel, p.anchor, push=False, scroll=p.scroll)

    def forward(self) -> None:
        if self.pos + 1 < len(self.history):
            self._remember()
            self.pos += 1
            p = self.history[self.pos]
            self.open(p.rel, p.anchor, push=False, scroll=p.scroll)

    def _update_buttons(self) -> None:
        self.back_btn.setEnabled(self.pos > 0)
        self.fwd_btn.setEnabled(self.pos + 1 < len(self.history))

    def _tree_changed(self, it: Optional[QTreeWidgetItem], _prev=None) -> None:
        rel = it.data(0, ROLE_REL) if it is not None else ""
        if rel:
            self.open(rel)

    def _link(self, url: QUrl) -> None:
        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        if scheme == "file" or url.isLocalFile():
            path = os.path.relpath(url.toLocalFile(), self.root).replace(os.sep, "/")
            rel, anchor = path, url.fragment()
        else:
            rel, anchor = H.resolve_link(self.current if self.current != ABOUT else "", url.toString())
        if not self.open(rel, anchor):
            self.status.setText(self.t("help.not_packaged", "{target} is not part of the installed help.",
                                       target=url.toString()))

    # ------------------------------------------------------------------ search
    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def index(self) -> H.HelpIndex:
        if self._index is None:
            self._index = H.HelpIndex(self.topics)
        return self._index

    def do_search(self) -> list[H.SearchHit]:
        q = self.search.text().strip()
        if not q:
            self.show_tree()
            return []
        hits = self.index().search(q)
        self.results.clear()
        for h in hits:
            head = h.heading if h.heading != h.topic.title else ""
            it = QListWidgetItem(f"{h.topic.title}" + (f" › {head}" if head else "") + f"\n{h.snippet}")
            it.setToolTip(f"{h.topic.rel}:{h.line}")
            it.setData(ROLE_REL, h)
            self.results.addItem(it)
        if hits:
            self.results_label.setText(self.t("help.search_results", "{count} results for “{query}”",
                                              count=len(hits), query=q))
            self.results.setCurrentRow(0)
        else:
            self.results_label.setText(self.t("help.search_none", "No results for “{query}”", query=q))
        self.side.setCurrentIndex(1)
        return hits

    def _open_hit(self, it: QListWidgetItem) -> None:
        h = it.data(ROLE_REL)
        if isinstance(h, H.SearchHit):
            words = [w for w in self.search.text().split() if w]
            self.open(h.topic.rel, h.anchor, highlight=words)

    def show_tree(self) -> None:
        self.side.setCurrentIndex(0)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.KeyPress:
            if obj is self.search and ev.key() == Qt.Key_Down and self.side.currentIndex() == 1:
                self.results.setFocus()
                return True
            if obj in (self.search, self.results) and ev.key() == Qt.Key_Escape and self.side.currentIndex() == 1:
                self.search.clear()
                self.show_tree()
                self.tree.setFocus()
                return True
        if ev.type() == QEvent.MouseButtonRelease and obj is self.view.viewport():
            if ev.button() == Qt.BackButton:
                self.back()
                return True
            if ev.button() == Qt.ForwardButton:
                self.forward()
                return True
        return super().eventFilter(obj, ev)

    def find_text(self, text: str) -> bool:
        """Select the next occurrence of ``text`` in the page (tests, accessibility)."""
        return self.view.find(text, QTextDocument.FindFlag(0))
