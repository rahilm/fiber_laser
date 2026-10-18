"""Process statistics and the processing-report viewer (port of ``Report/report.exe``).

``records``   record format and the Report/File statistic files (TotalReport, LogReport, report.txt)
``recorder``  builds and stores a record per completed job (MainApp FUN_00598280 / FUN_004c5380)
``thumbnail`` job thumbnail JPG
``printing``  report page layout, print preview and PDF export
``window``    the viewer (ReportWindow) and the Count/Bill dialog
Evidence: docs/diag.md.
"""
from .records import JobRecord, ReportStore, cost, parse_line, read_records, totals  # noqa: F401
