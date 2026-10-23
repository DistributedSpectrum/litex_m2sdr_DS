#!/usr/bin/env python3
#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import LiteXModule
from litex.gen.sim import run_simulation

from litex_m2sdr.gateware.pps import PPSGenerator, PPSInput

# PPS Tests ---------------------------------------------------------------------------------------


def test_pps_generator_pulse_and_count():
    """Check PPS pulse generation and coarse counter increment when time advances."""
    time = Signal(64)
    dut = PPSGenerator(clk_freq=100e6, time=time, offset=0, reset=0)
    pulses = []
    counts = []

    def gen():
        # Simulate time in ns jumping each cycle.
        value = 1
        for _ in range(80):
            value += 50_000_000
            yield time.eq(value)
            pulses.append((yield dut.pps_pulse))
            counts.append((yield dut.count))
            yield

    run_simulation(dut, gen())

    assert any(pulses)
    assert counts[-1] >= 1


def test_pps_generator_stays_idle_when_time_zero():
    """Check PPS remains idle and count stays zero when time input is stuck at zero."""
    time = Signal(64)
    dut = PPSGenerator(clk_freq=100e6, time=time, offset=0, reset=0)
    pulses = []
    counts = []

    def gen():
        for _ in range(32):
            yield time.eq(0)
            pulses.append((yield dut.pps_pulse))
            counts.append((yield dut.count))
            yield

    run_simulation(dut, gen())
    assert not any(pulses)
    assert counts[-1] == 0


# PPS Input Latch Tests ---------------------------------------------------------------------------

class _PPSInputLatchTop(LiteXModule):
    """PPSInput with a free-running sys time (8 ns per cycle) and an rfic-domain cycle counter."""
    def __init__(self, with_cycle_latch=False):
        self.pad  = Signal()
        self.time = Signal(64)
        self.sync += self.time.eq(self.time + 8)
        self.pps_in = PPSInput(self.pad, time=self.time)
        if with_cycle_latch:
            self.cd_rfic = ClockDomain()
            self.cycles  = Signal(64)
            self.sync.rfic += self.cycles.eq(self.cycles + 1)
            self.pps_in.add_cycle_latch(self.cycles, cd="rfic", name="rfic")


def test_pps_input_latches_time_on_rising_edges_only():
    """Check board time is latched 2 cycles after each rising edge, and held through the falling edge."""
    dut = _PPSInputLatchTop()
    set_times = []
    latched   = []

    def gen():
        for cycle in range(120):
            if cycle in (20, 70):
                yield dut.pad.eq(1)
                set_times.append((yield dut.time))
            elif cycle == 40:
                yield dut.pad.eq(0)
            elif cycle in (45, 100):
                latched.append(((yield dut.pps_in.time), (yield dut.pps_in.count)))
            yield

    run_simulation(dut, gen())

    # A generator write reaches the pin one cycle later (time + 8 ns); MultiReg then sees the edge
    # 2 cycles after the pin, so the latch is the pin-high time + 2 x 8 ns.
    pin_times = [t + 8 for t in set_times]
    assert latched == [(pin_times[0] + 16, 1), (pin_times[1] + 16, 2)]


def test_pps_input_cycle_latch_crosses_to_sys():
    """Check the rfic-domain counter is latched on each edge (2 rfic cycles after the pin) and
    reaches sys intact, with sys and rfic at the 125/245.76 MHz ratio."""
    dut = _PPSInputLatchTop(with_cycle_latch=True)
    set_cycles = []
    latched    = []
    period     = 300

    def rfic_gen():
        for cycle in range(1000):
            if cycle in (50, 50 + period):
                yield dut.pad.eq(1)
                set_cycles.append((yield dut.cycles))
            elif cycle in (100, 100 + period):
                yield dut.pad.eq(0)
            yield

    def sys_mon():
        last = None
        for _ in range(500):
            value = (yield dut.pps_in.rfic_cycles)
            if value != last:
                latched.append(value)
                last = value
            yield

    run_simulation(dut, {"rfic": rfic_gen(), "sys": sys_mon()}, clocks={"sys": 26, "rfic": 13})

    # Pin high one rfic cycle after the write, edge seen 2 rfic cycles after the pin.
    pin_cycles = [c + 1 for c in set_cycles]
    assert latched == [0, pin_cycles[0] + 2, pin_cycles[1] + 2]
    assert set_cycles[1] - set_cycles[0] == period
