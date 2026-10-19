"""Peripheral devices of the Mlaser rebuild (one module per device; each has a client and a
small simulator).  Evidence and addresses: docs/re/devices.md.

* :mod:`.codecs`      IModbus codec family (RTU slave 1/2, Std, FTC61 tunnel, raw)
* :mod:`.transport`   UDP/TCP/serial transports with the HAL retry loop, ipAdd.ini settings
* :mod:`.modbus_rtu`  RTU-over-UDP register client + simulator (AF, EC)
* :mod:`.follower`    capacitive height follower (on-board via the card; network variant)
* :mod:`.laser`       Raycus / IPG command channel
* :mod:`.autofocus`   AF / AdvAF heads
* :mod:`.ethercat`    EC / AdvEC / EC3710 extension card
* :mod:`.pendant`     XHC wireless pendant over HID (hidapi, optional)
"""
