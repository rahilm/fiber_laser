"""XHC pendant: report formats from PHBX.dll, key mapping from MainApp FUN_005ab2e0."""
from mlaser.devices import pendant as P


def test_checksum_and_decode():
    rep = bytes([4, 0x33, 0x13, 0, 0, P.make_checksum(0x33, 0x13, 18)])
    ev = P.decode_input(rep, day=18)
    assert ev and ev.key == 0x13 and ev.down
    assert P.decode_input(rep, day=19) is None or P.checksum_ok(rep, 19)
    assert P.decode_input(bytes([5]) + rep[1:], day=18) is None


def test_feature_reports():
    assert P.day_sync_report(7) == bytes.fromhex("06fefd07ffff0000")
    assert P.output_report(0x10, 0x01, day=7) == bytes.fromhex("06fefd0710010000")
    assert P.fid_report(0x1234, day=7) == bytes.fromhex("06fafb0734120000")
    assert P.raw_reports(bytes(range(9))) == [bytes([6, 0, 1, 2, 3, 4, 5, 6]), bytes([6, 7, 8, 0, 0, 0, 0, 0])]
    st = P.PendantStatus.parse(bytes([6, 0x68, 0x92, 0, 5, 0x60, 1, 0]))
    assert st.dev_id == 0x9268 and st.channel == 5 and st.poor_signal and st.fid == 1
    assert P.led_mask(1, 0x9268) == 0x01 and P.led_mask(1, 0x1000) == 0x10


class FakeCtl:
    def __init__(self):
        self.log = []

    def jog_start(self, axis, d, fast=False):
        self.log.append(("start", axis, d, fast))

    def jog_stop(self, axis=None):
        self.log.append(("stop", axis))


def test_pendant_to_jog_mapping():
    dev = P.FakeHidDevice(dev_id=0x1000, day=P.utc_day())
    ctl = FakeCtl()
    homes = []
    pend = P.Pendant(devices=(dev, dev))
    assert pend.open() and dev.features[0][:3] == bytes([6, 0xFE, 0xFD])
    leds = []
    mapper = P.PendantJogMapper(ctl, {"home": lambda: homes.append(1)}, leds=pend.set_leds,
                                dev_id=lambda: pend.status.dev_id)
    pend.on_event = mapper.handle
    dev.press(0x13)                     # X+
    dev.press(0)                        # release
    dev.press(0x0D)                     # toggle fast
    dev.press(0)
    dev.press(0x0E)                     # Z-
    dev.press(0x0B, 0x07)               # Fn + home
    dev.press(0x12, bad=True)           # bad checksum: ignored
    while dev.queue:
        pend.poll()
    assert ctl.log == [("start", "X", 1, False), ("stop", "X"), ("start", "Z", -1, True), ("stop", "Z")]
    assert homes == [1]
    assert pend.bad_reports == 1
    assert dev.features[1] == P.output_report(0x10 if False else P.led_mask(0x13, 0x1000), 1)


def test_busy_only_stop():
    ctl = FakeCtl()
    stops = []
    m = P.PendantJogMapper(ctl, {"stop": lambda: stops.append(1)}, busy=lambda: True)
    assert m.handle(P.PendantEvent(0x13, 0, 0)) is None and ctl.log == []
    m.handle(P.PendantEvent(0x0A, 0, 0))
    assert stops == [1]
