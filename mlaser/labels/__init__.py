"""Part labels, QR job tickets, remnant labels (PDF) and part-ID etching (Qt-free).

* :mod:`.pdf`: a small PDF writer (standard fonts, vector drawing, QR modules);
* :mod:`.labels`: label sizes, label / ticket / remnant data and their PDF rendering;
* :mod:`.marking`: a single-stroke font and the placement of part ids inside parts.

User guide: docs/user/labels.md.  The application plugin is ``mlaser.ui.ext_labels``.
"""
from .labels import (DEFAULT_SIZE, SIZES, LabelSize, PartLabel, RemnantLabel, TicketData, label_size,  # noqa: F401
                     part_labels, remnant_label, render_part_labels, render_remnant_labels, render_tickets,
                     ticket_from_entry)
from .marking import EtchResult, etch_document, place_text, remove_marking  # noqa: F401
