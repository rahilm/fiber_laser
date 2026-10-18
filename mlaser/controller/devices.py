"""Peripheral devices next to the motion card (compatibility facade).

The implementations live in :mod:`mlaser.devices` (one module per device, each with a client
and a simulator); this module keeps the phase-1 names importable.  See docs/devices.md.

Endpoints (File/ipAdd.ini, read in FUN_10058b70 / FUN_1004ed60):
    ZF     10.1.1.169:502   CStdModbus - codec never referenced in this build (unused)
    OnBZF  10.1.1.168:999 and motion-card registers 10000.. / 11000.. (the live follower path)
    Laser  10.1.1.170:10001 (IPG, laser type 2), OnBLaser 10.1.1.168:888 (Raycus, type 1)
    AF 10.1.1.168:888, AdvAF :666  (Modbus RTU slave 1 over UDP)
    EC 10.1.1.168:888, AdvEC :666, EC3710 10.1.1.170:502 (Modbus RTU slave 2 over UDP)
"""
from __future__ import annotations

from mlaser.devices.autofocus import AutoFocus, CardAutoFocus
from mlaser.devices.codecs import encode_std_modbus
from mlaser.devices.ethercat import EtherCATBridge
from mlaser.devices.follower import NetworkFollower, OnBoardFollower
from mlaser.devices.laser import IpgLaser, RaycusLaser

LaserSource = IpgLaser

__all__ = ["OnBoardFollower", "NetworkFollower", "encode_std_modbus", "LaserSource", "IpgLaser",
           "RaycusLaser", "AutoFocus", "CardAutoFocus", "EtherCATBridge"]
