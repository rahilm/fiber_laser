"""The simulator's processing status (RO[19]) never shows "executing" without "data".

The real card reports 0 -> 0x100 -> 0x301 -> 0x001 (protocol §15).  The simulator popped the
last FIFO item and kept executing it with the FIFO empty: 0x201 for one step period, which a
poll on a coarse clock (Windows CI) caught in test_wire_reference."""
from mlaser.controller.simulator import CardSimulator
from mlaser.protocol import frames as F


def test_the_last_item_executing_is_still_data():
    sim = CardSimulator(time_scale=1.0)
    st = sim.state
    for op, params in F.parse_fifo_items(F.encode_step(8, 3, 5000, 0)):
        st.fifo.append((op, params, 4 * (1 + len(params))))
    st.program_active = True
    st.fifo_running = True
    seen = set()
    for _ in range(40):                       # a step lasts one period: walk through it
        seen.add(sim._ro(F.RO_RUNNING))
        sim.advance(sim.period_ms * 1e-3 / 8)
    seen.add(sim._ro(F.RO_RUNNING))
    assert st.history and not st.fifo_running
    assert seen <= {0x301, 0x001}, sorted(hex(v) for v in seen)
    assert 0x301 in seen and 0x001 in seen
