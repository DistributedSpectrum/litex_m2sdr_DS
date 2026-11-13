#!/usr/bin/env python3
#
# This file is part of LiteX-M2SDR.
#
# SPDX-License-Identifier: BSD-2-Clause

# GPS time input (gateware/gps.py): UBX-NAV-TIMEUTC decode, PPS qualification, board time sync.

from migen import *
from migen.sim import passive

from litex.gen.sim import run_simulation

from litex_m2sdr.gateware.gps import (
    UBXTimeUTCDecoder, PPSQualifier, GPSTime, ubx_timeutc_frame, utc_epoch,
)

# Decoder ------------------------------------------------------------------------------------------

def run_decoder(stream):
    dut = UBXTimeUTCDecoder()
    decoded, ck_errors = [], [0]

    def gen():
        for b in stream:
            yield dut.sink_valid.eq(1)
            yield dut.sink_data.eq(b)
            yield
            yield dut.sink_valid.eq(0)
            yield  # Bytes arrive with gaps, as from a UART.
        for _ in range(8):
            yield

    def mon():
        while True:
            if (yield dut.decoded):
                decoded.append(((yield dut.epoch), (yield dut.utc_valid),
                                (yield dut.t_acc), (yield dut.nano)))
            if (yield dut.ck_error):
                ck_errors[0] += 1
            yield
    mon = passive(mon)

    run_simulation(dut, [gen(), mon()])
    return decoded, ck_errors[0]

def test_decoder_epochs_match_calendar():
    dates = [
        (2026, 11, 13, 21, 8, 49),
        (2024,  2, 29, 23, 59, 59),  # Leap day.
        (2024,  3,  1,  0,  0,  0),
        (2023,  3,  1,  0,  0,  0),  # Non-leap year.
        (2000, 12, 31, 12, 0, 0),    # Leap (div by 400).
        (2100,  3,  1,  0,  0, 0),   # 2100 is not a leap year.
        (1970,  1,  1,  0,  0, 0),
        (2105, 12, 31, 23, 59, 59),
    ]
    stream = b"".join(ubx_timeutc_frame(*d, t_acc=17) for d in dates)
    decoded, ck_errors = run_decoder(stream)
    assert ck_errors == 0
    assert [e for e, _, _, _ in decoded] == [utc_epoch(*d) for d in dates]
    assert all(v == 1 and t == 17 for _, v, t, _ in decoded)

def test_decoder_rounds_with_nano():
    d = (2026, 11, 13, 10, 0, 0)
    frames = [
        ubx_timeutc_frame(*d, nano=-40),            # Normal: a few ns off the second.
        ubx_timeutc_frame(*d, nano=600_000_000),    # Rounds up.
        ubx_timeutc_frame(*d, nano=-600_000_000),   # Rounds down.
    ]
    decoded, _ = run_decoder(b"".join(frames))
    base = utc_epoch(*d)
    assert [e for e, _, _, _ in decoded] == [base, base + 1, base - 1]
    assert decoded[0][3] == -40

def test_decoder_flags_invalid_utc_and_bad_fields():
    frames = [
        ubx_timeutc_frame(2026, 11, 13, 1, 2, 3, valid=0x03),  # validUTC clear.
        ubx_timeutc_frame(2026, 13, 13, 1, 2, 3),              # Month out of range.
        ubx_timeutc_frame(2026, 11, 13, 1, 2, 3),
    ]
    decoded, _ = run_decoder(b"".join(frames))
    assert [v for _, v, _, _ in decoded] == [0, 0, 1]

def test_decoder_rejects_bad_checksum_and_resyncs_through_garbage():
    good = ubx_timeutc_frame(2026, 11, 13, 1, 2, 3)
    bad  = bytearray(good)
    bad[10] ^= 0x01  # Corrupt a payload byte.
    other = bytes([0xb5, 0x62, 0x01, 0x07, 0x5c, 0x00]) + bytes(92) + bytes([0, 0])  # NAV-PVT.
    stream = b"$GPGGA,garbage*00\r\n" + bytes(bad) + other + b"\xb5\xb5" + good[1:] + good
    decoded, ck_errors = run_decoder(stream)
    assert ck_errors == 1
    assert [e for e, _, _, _ in decoded] == [utc_epoch(2026, 11, 13, 1, 2, 3)]*2

# PPS Qualifier ------------------------------------------------------------------------------------

def test_pps_qualifier_filters_glitches_and_refractory_bursts():
    class DUT(Module):
        def __init__(self):
            self.pad = Signal()
            self.submodules.q = PPSQualifier(self.pad, filter_cycles=8, refractory_cycles=200)
    dut = DUT()
    pulses, rejected, cycle = [], [0], [0]

    def drive(level, n):
        yield dut.pad.eq(level)
        for _ in range(n):
            yield

    def gen():
        yield from drive(0, 10)
        yield from drive(1, 5)    # Glitch shorter than the filter: rejected.
        yield from drive(0, 10)
        yield from drive(1, 30)   # Real PPS: accepted.
        yield from drive(0, 20)
        for _ in range(4):        # Crosstalk burst inside the refractory window.
            yield from drive(1, 12)
            yield from drive(0, 6)
        yield from drive(0, 250)
        yield from drive(1, 30)   # Next real PPS: accepted.
        yield from drive(0, 20)

    def mon():
        while True:
            if (yield dut.q.pulse):
                pulses.append(cycle[0])
            if (yield dut.q.rejected):
                rejected[0] += 1
            cycle[0] += 1
            yield
    mon = passive(mon)

    run_simulation(dut, [gen(), mon()])
    assert len(pulses) == 2
    # The edge is accepted latency_cycles after the pin went high (pin high at cycle 25).
    assert pulses[0] - 25 == dut.q.latency_cycles - 1 or pulses[0] - 25 == dut.q.latency_cycles
    assert rejected[0] == 4

# GPS Time -----------------------------------------------------------------------------------------

class FakeTimeGen(Module):
    """The TimeGenerator interface GPSTime drives (time domain inputs) plus a sys-domain time."""
    def __init__(self):
        self.time             = Signal(64)
        self.time_sync        = Signal()
        self.time_seconds     = Signal(40)
        self.time_sync_offset = Signal(32)

class GPSDUT(Module):
    def __init__(self, sys_clk_freq=1e6, baudrate=62500):
        self.clock_domains.cd_time = ClockDomain()
        self.pps  = Signal()
        self.uart = Record([("rx", 1), ("tx", 1)])
        self.submodules.tg  = FakeTimeGen()
        self.submodules.gps = GPSTime(self.uart, self.pps, self.tg, sys_clk_freq,
            baudrate          = baudrate,
            pps_refractory_s  = 300e-6,
            pps_alive_s       = 5e-3,
            utc_fresh_s       = 5e-3,
        )

def uart_byte(rx, b, bit):
    """Bit-bang one 8N1 byte on rx, bit sys cycles per bit."""
    yield rx.eq(0)
    for _ in range(bit):
        yield
    for i in range(8):
        yield rx.eq((b >> i) & 1)
        for _ in range(bit):
            yield
    yield rx.eq(1)
    for _ in range(bit):
        yield

def test_gps_time_end_to_end_uart_pps_sync():
    sys_clk_freq, baud = 1e6, 62500  # 16 sys cycles per bit.
    bit = int(sys_clk_freq // baud)
    dut = GPSDUT(sys_clk_freq, baud)
    seconds = [utc_epoch(2026, 11, 13, 21, 8, s) for s in (49, 50, 51)]
    syncs = []
    st = {}

    def pps_pulse():
        yield dut.pps.eq(1)
        for _ in range(40):
            yield
        yield dut.pps.eq(0)

    def gen():
        yield dut.uart.rx.eq(1)
        yield from dut.gps._control.write(0b011)  # enable + sync_every.
        for _ in range(50):
            yield
        # A PPS with nothing decoded yet: no sync.
        yield from pps_pulse()
        for i, s in enumerate(seconds):
            # The NAV-TIMEUTC of second s arrives after the PPS that starts s...
            for b in ubx_timeutc_frame(2026, 11, 13, 21, 8, 49 + i):
                yield from uart_byte(dut.uart.rx, b, bit)
            for _ in range(300):
                yield
            # ...and the next PPS starts s + 1.
            yield dut.tg.time.eq(1000 + i)
            yield from pps_pulse()
            for _ in range(400):
                yield
        st["msg"]   = (yield dut.gps._msg_count.status)
        st["pps"]   = (yield dut.gps._pps_count.status)
        st["sync"]  = (yield dut.gps._sync_count.status)
        st["noutc"] = (yield dut.gps._pps_no_utc.status)
        st["epoch"] = (yield dut.gps._epoch.status)
        st["ppst"]  = (yield dut.gps._pps_time.status)
        st["synced"] = (yield dut.gps._status.fields.synced)

    @passive
    def mon_time():
        while True:
            if (yield dut.tg.time_sync):
                syncs.append(((yield dut.tg.time_seconds), (yield dut.tg.time_sync_offset)))
            yield

    run_simulation(dut, {"sys": [gen()], "time": [mon_time()]}, clocks={"sys": 10, "time": 12})
    assert st["msg"] == 3 and st["pps"] == 4 and st["sync"] == 3 and st["noutc"] == 1
    assert [s for s, _ in syncs] == seconds
    assert all(o == dut.gps.default_sync_offset for _, o in syncs)
    assert st["epoch"] == seconds[-1]
    assert st["ppst"] == 1002
    assert st["synced"] == 1  # The last PPS applied a sync.

def test_gps_time_sync_once_and_off():
    dut = GPSDUT()
    syncs = []
    st = {}
    frame = ubx_timeutc_frame(2026, 1, 2, 3, 4, 5)

    def feed_decode():
        for b in frame:
            yield from uart_byte(dut.uart.rx, b, 16)  # GPSDUT default: 16 sys cycles per bit.
        for _ in range(20):
            yield

    def pps_pulse():
        yield dut.pps.eq(1)
        for _ in range(40):
            yield
        yield dut.pps.eq(0)
        for _ in range(400):
            yield

    def gen():
        yield dut.uart.rx.eq(1)
        # Sync off: decode + PPS consume the pending decode, no sync.
        yield from dut.gps._control.write(0b001)
        yield from feed_decode()
        yield from pps_pulse()
        # One-shot: armed, applied once, then off again.
        # (A CSR pulse field does not fire from a testbench write in migen's simulator, so arm
        # through the core's sync_once input, which is OR'd with the CSR pulse.)
        yield dut.gps.sync_once.eq(1)
        yield
        yield dut.gps.sync_once.eq(0)
        yield
        yield from feed_decode()
        st["armed"] = (yield dut.gps._status.fields.once_armed)
        yield from pps_pulse()
        yield from feed_decode()
        yield from pps_pulse()
        st["sync"] = (yield dut.gps._sync_count.status)

    @passive
    def mon_time():
        while True:
            if (yield dut.tg.time_sync):
                syncs.append((yield dut.tg.time_seconds))
            yield

    run_simulation(dut, {"sys": [gen()], "time": [mon_time()]}, clocks={"sys": 10, "time": 12})
    assert st["armed"] == 1
    assert st["sync"] == 1
    assert syncs == [utc_epoch(2026, 1, 2, 3, 4, 5)]
