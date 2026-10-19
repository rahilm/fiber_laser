"""mlaser-native: optional C kernel of the per-sample emitter loop.

Used through the ``native-sampling`` ParityProfile (``mlaser.parity_ports.profiles.native``);
not meant to be called directly.  See docs/dev/NATIVE_KERNEL.md in the mlaser repository.
"""
from . import _emitter

KERNEL_VERSION = 1

__all__ = ["_emitter", "KERNEL_VERSION"]
