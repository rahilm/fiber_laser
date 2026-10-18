"""MCC100 motion-card protocol (frames), client, simulator and peripheral stubs.

See docs/protocol.md.
"""
from .client import FifoStreamer, MCStatus, MotionController, TransportConfig  # noqa: F401
from .simulator import CardSimulator  # noqa: F401
