"""Parity tooling that compares the rebuild with the original on a real machine.

* :mod:`mlaser.parity.pcap`: pure-Python pcap / pcapng reader and writer (Ethernet, Linux
  SLL/SLL2, BSD loopback, raw IPv4; IPv4 fragments; UDP; TCP with stream reassembly).
* :mod:`mlaser.parity.capture`: card traffic in a capture (requests/replies, retransmissions,
  status reads, FIFO frames), job reconstruction, and the comparison with ``compile_job``.
* :mod:`mlaser.parity.report`: text and HTML reports (plots with matplotlib when installed).

CLI: ``tools/compare_capture.py``.  Procedure: docs/parity.md "Verifying against your machine".
"""
