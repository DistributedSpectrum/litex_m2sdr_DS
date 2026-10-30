#!/usr/bin/env python3
#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.sim import passive
import random

from litex.gen import LiteXModule
from litex.gen.sim import run_simulation

from litex.soc.interconnect import stream

from litex_m2sdr.gateware.ad9361 import core
from litex_m2sdr.gateware.ad9361.bitmode import (
    AD9361RXBitMode,
    AD9361TXBitMode,
    BFP8_HEADER_MAGIC,
    BFP8_PAYLOAD_WORDS,
)
from litex_m2sdr.gateware.ad9361.phy import phy_layout
from litex_m2sdr.gateware.ad9361.prbs import AD9361PRBSChecker, AD9361PRBSGenerator

# AD9361 BitMode Tests ----------------------------------------------------------------------------


def test_ad9361_tx_bitmode_16bit_passthrough():
    """Verify TX bitmode forwards 64-bit samples unchanged in 16-bit mode."""
    dut = AD9361TXBitMode()
    out = []

    def gen():
        yield dut.mode.eq(0)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(0x1122334455667788)
        yield
        yield dut.sink.valid.eq(0)
        yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [0x1122334455667788]


def test_ad9361_rx_bitmode_16bit_passthrough():
    """Verify RX bitmode forwards 64-bit samples unchanged in 16-bit mode."""
    dut = AD9361RXBitMode()
    out = []

    def gen():
        yield dut.mode.eq(0)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(0x99AABBCCDDEEFF00)
        yield
        yield dut.sink.valid.eq(0)
        yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [0x99AABBCCDDEEFF00]


def test_ad9361_tx_bitmode_8bit_sign_extension():
    """Verify TX 8-bit mode repacks and sign-extends lane samples as expected."""
    dut = AD9361TXBitMode()
    out = []

    def gen():
        yield dut.mode.eq(1)
        yield dut.source.ready.eq(1)
        # Keep valid high for two cycles to emit both 32-bit halves.
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(0x807F00FF11223344)
        yield
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(4):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert 0x0110022003300440 in out
    assert 0xF80007F00000FFF0 in out


def test_ad9361_rx_bitmode_8bit_packing():
    """Verify RX 8-bit mode compacts two 64-bit beats into one packed output beat."""
    dut = AD9361RXBitMode()
    out = []

    def gen():
        yield dut.mode.eq(1)
        yield dut.source.ready.eq(1)
        # Two 64-bit beats are required to emit one packed 64-bit beat.
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(0)
        yield dut.sink.data.eq((0x00C << 4) | ((0x00D << 4) << 16) | ((0x00E << 4) << 32) | ((0x00F << 4) << 48))
        yield
        yield dut.sink.first.eq(0)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq((0x008 << 4) | ((0x009 << 4) << 16) | ((0x00A << 4) << 32) | ((0x00B << 4) << 48))
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(4):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [0x0B0A09080F0E0D0C]


def test_ad9361_rx_bitmode_8bit_rounding_and_saturation():
    """Verify RX 8-bit mode rounds 12-bit samples and clamps positive overflow."""
    dut = AD9361RXBitMode()
    out = []

    def pack_sample12(value):
        value &= 0xfff
        return value | (0xf000 if value & 0x800 else 0)

    def pack_word(samples):
        word = 0
        for i, sample in enumerate(samples):
            word |= pack_sample12(sample) << (16 * i)
        return word

    def gen():
        yield dut.mode.eq(1)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(0)
        yield dut.sink.data.eq(pack_word([0x007, 0x008, 0xff9, 0xff8]))
        yield
        yield dut.sink.first.eq(0)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(pack_word([0x7f7, 0x7f8, 0xff7, 0x800]))
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(4):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [0x80FF7F7FFF000100]


def test_ad9361_rx_bitmode_bfp8_block_header_and_payload():
    """Verify RX BFP8 emits a block header and rounded mantissa payload."""
    dut = AD9361RXBitMode(bfp8_payload_words=1)
    out = []

    def pack_sample12(value):
        value &= 0xfff
        return value | (0xf000 if value & 0x800 else 0)

    def pack_word(samples):
        word = 0
        for i, sample in enumerate(samples):
            word |= pack_sample12(sample) << (16 * i)
        return word

    def gen():
        yield dut.mode.eq(2)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(0)
        yield dut.sink.data.eq(pack_word([0x007, 0x008, 0xff9, 0xff8]))
        yield
        yield dut.sink.first.eq(0)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(pack_word([0x7f7, 0x7f8, 0xff7, 0x800]))
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(12):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.first), (yield dut.source.last)))
            yield

    run_simulation(dut, [gen(), mon()])

    expected_header = BFP8_HEADER_MAGIC | (4 << 32) | (1 << 40) | (1 << 56)
    assert out == [
        (expected_header, 1, 0),
        (0x80FF7F7FFF000100, 0, 1),
    ]


def test_ad9361_rx_bitmode_bfp8_uses_low_exponent_for_small_blocks():
    """Verify RX BFP8 preserves small samples by lowering the block exponent."""
    dut = AD9361RXBitMode(bfp8_payload_words=1)
    out = []

    def pack_sample12(value):
        value &= 0xfff
        return value | (0xf000 if value & 0x800 else 0)

    def pack_word(samples):
        word = 0
        for i, sample in enumerate(samples):
            word |= pack_sample12(sample) << (16 * i)
        return word

    def gen():
        yield dut.mode.eq(2)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.data.eq(pack_word([1, -1, 64, -64]))
        yield
        yield dut.sink.data.eq(pack_word([127, -127, 0, 7]))
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(12):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])

    expected_header = BFP8_HEADER_MAGIC | (0 << 32) | (1 << 40) | (1 << 56)
    assert out == [
        expected_header,
        0x0700817FC040FF01,
    ]


def test_ad9361_tx_bitmode_bfp8_header_and_payload_expansion():
    """Verify TX BFP8 consumes a header and expands one payload word to two RFIC beats."""
    dut = AD9361TXBitMode(bfp8_payload_words=1)
    out = []

    header = BFP8_HEADER_MAGIC | (4 << 32) | (1 << 40) | (1 << 56)
    payload = 0x80FF7F7FFF000100

    def gen():
        yield dut.mode.eq(2)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(0)
        yield dut.sink.data.eq(header)
        yield
        yield dut.sink.first.eq(0)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(payload)
        yield
        yield dut.sink.valid.eq(0)
        for _ in range(8):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.last)))
            yield

    run_simulation(dut, [gen(), mon()])

    assert out == [
        (0x0FF0000000100000, 0),
        (0x08000FF007F007F0, 1),
    ]

# AD9361 PRBS Tests -------------------------------------------------------------------------------


def test_ad9361_prbs_checker_sync_and_loss():
    """Verify PRBS checker reaches sync then drops sync on injected bad samples."""
    gen = AD9361PRBSGenerator(seed=0x0A54)
    chk = AD9361PRBSChecker(seed=0x0A54)
    dut = Module()
    dut.submodules += gen, chk
    synced_trace = []

    def stimulus():
        for _ in range(1200):
            yield chk.i.eq((yield gen.o) & 0xFFF)
            synced_trace.append((yield chk.synced))
            yield
        # Inject wrong samples to force loss of sync.
        for _ in range(4):
            yield chk.i.eq(0x000)
            synced_trace.append((yield chk.synced))
            yield

    run_simulation(dut, stimulus())
    assert any(synced_trace[1100:])
    assert synced_trace[-1] == 0


def test_ad9361_prbs_generator_ce_hold():
    """Verify PRBS generator output holds constant when clock-enable is deasserted."""
    dut = AD9361PRBSGenerator(seed=0x0A54)
    values = []

    def gen():
        for _ in range(4):
            values.append((yield dut.o))
            yield
        yield dut.ce.eq(0)
        for _ in range(4):
            values.append((yield dut.o))
            yield

    run_simulation(dut, gen())
    assert len(set(values[-3:])) == 1


def test_ad9361_tx_bitmode_mode_switch_mid_stream():
    """Verify TX bitmode mode switch mid-stream preserves valid converted outputs."""
    random.seed(0x55AA)
    dut = AD9361TXBitMode()
    out = []

    def gen():
        yield dut.source.ready.eq(1)
        # Start in 16-bit mode.
        yield dut.mode.eq(0)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(0x0123456789ABCDEF)
        yield
        # Switch to 8-bit mode and keep valid for two cycles to emit both halves.
        yield dut.mode.eq(1)
        yield dut.sink.data.eq(0x807F00FF11223344)
        yield
        yield
        # Random ready stalls while data is idle.
        yield dut.sink.valid.eq(0)
        for _ in range(8):
            yield dut.source.ready.eq(0 if random.random() < 0.3 else 1)
            yield
        yield dut.source.ready.eq(1)
        for _ in range(4):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])

    # Must contain one 16-bit passthrough beat and 8-bit converted beats.
    assert 0x0123456789ABCDEF in out
    assert 0x0110022003300440 in out
    assert 0xF80007F00000FFF0 in out


def test_ad9361_prbs_checker_stays_unsynced_after_error_burst():
    """Verify checker remains unsynced after burst error with free-running source."""
    gen = AD9361PRBSGenerator(seed=0x0A54)
    chk = AD9361PRBSChecker(seed=0x0A54)
    dut = Module()
    dut.submodules += gen, chk
    synced = []

    def stimulus():
        # Initial lock.
        for _ in range(1200):
            yield chk.i.eq((yield gen.o) & 0xFFF)
            synced.append((yield chk.synced))
            yield
        # Error burst.
        for _ in range(12):
            yield chk.i.eq(0x000)
            synced.append((yield chk.synced))
            yield
        # Keep feeding generator output after error burst.
        for _ in range(1300):
            yield chk.i.eq((yield gen.o) & 0xFFF)
            synced.append((yield chk.synced))
            yield

    run_simulation(dut, stimulus())
    assert any(synced[1100:1200])  # initially locked
    assert synced[1210] == 0        # lost lock after error burst
    assert not any(synced[-200:])   # remains out-of-lock with free-running source

# AD9361 BitMode (128-bit) Tests ----------------------------------------------------------------------


def test_ad9361_tx_bitmode_128b_16bit_passthrough():
    """Verify the 128-bit TX bitmode forwards 128-bit words (two sample groups) unchanged in
    16-bit mode."""
    dut = AD9361TXBitMode(data_width=128)
    out = []
    word = (0x99AABBCCDDEEFF00 << 64) | 0x1122334455667788

    def gen():
        yield dut.mode.eq(0)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(word)
        yield
        yield dut.sink.valid.eq(0)
        yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [word]


def test_ad9361_tx_bitmode_128b_8bit_sign_extension():
    """Verify the 128-bit TX bitmode splits a 128-bit word of sc8 samples into two 128-bit words
    of sign-extended 16-bit samples (sc8 value v -> v << 4, sign-extended)."""
    dut = AD9361TXBitMode(data_width=128)
    out = []
    # 16 sc8 bytes; low 8 bytes become the first output word, high 8 the second.
    sc8 = [0x01, 0xFF, 0x7F, 0x80, 0x10, 0xF0, 0x00, 0x55,
           0x02, 0xFE, 0x40, 0xC0, 0x20, 0xE0, 0x7E, 0xAA]
    word = 0
    for i, b in enumerate(sc8):
        word |= b << (8 * i)

    def expand(b):
        v = (b << 4) & 0xFFF           # sc8 -> 12-bit left-aligned.
        if v & 0x800:                  # sign-extend 12 -> 16.
            v |= 0xF000
        return v

    def gen():
        yield dut.mode.eq(1)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(word)
        yield
        # The 128->64 converter acks the input on its LAST chunk: hold valid until then.
        while not (yield dut.sink.ready):
            yield
        yield dut.sink.valid.eq(0)
        for _ in range(8):
            yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    expected = []
    for half in (sc8[0:8], sc8[8:16]):
        w = 0
        for i, b in enumerate(half):
            w |= expand(b) << (16 * i)
        expected.append(w)
    assert out == expected, [hex(w) for w in out]


def test_ad9361_rx_bitmode_128b_16bit_passthrough():
    """Verify the 128-bit RX bitmode forwards 128-bit words unchanged in 16-bit mode."""
    dut = AD9361RXBitMode(data_width=128)
    out = []
    word = (0x1122334455667788 << 64) | 0x99AABBCCDDEEFF00

    def gen():
        yield dut.mode.eq(0)
        yield dut.source.ready.eq(1)
        yield dut.sink.valid.eq(1)
        yield dut.sink.first.eq(1)
        yield dut.sink.last.eq(1)
        yield dut.sink.data.eq(word)
        yield
        yield dut.sink.valid.eq(0)
        yield

    @passive
    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, [gen(), mon()])
    assert out == [word]

# AD9361 RX Timestamp Tests -----------------------------------------------------------------------

def _rx_bitmode_timestamp_run(mode, words, bfp8_payload_words=BFP8_PAYLOAD_WORDS, stall=False):
    """Feed (data, timestamp) words into a timestamped RX bitmode; return (data, timestamp) out."""
    dut = AD9361RXBitMode(bfp8_payload_words=bfp8_payload_words, with_timestamp=True)
    out = []
    prng = random.Random(7)

    def gen():
        yield dut.mode.eq(mode)
        for data, ts in words:
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(data)
            yield dut.sink.timestamp.eq(ts)
            yield
            while not (yield dut.sink.ready):
                yield
        yield dut.sink.valid.eq(0)
        for _ in range(64):
            yield

    @passive
    def mon():
        while True:
            yield dut.source.ready.eq(prng.randrange(3) != 0 if stall else 1)
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.timestamp)))

    run_simulation(dut, [gen(), mon()])
    return out


def test_ad9361_rx_bitmode_16bit_forwards_timestamp():
    """Verify 16-bit mode forwards each word's timestamp with it."""
    words = [(0x1000 + i, 1000 + 4*i) for i in range(8)]
    out = _rx_bitmode_timestamp_run(0, words, stall=True)
    assert out == words


def test_ad9361_rx_bitmode_8bit_timestamp_is_first_word():
    """Verify 8-bit mode stamps each packed word with its first input word's timestamp."""
    words = [(0, 1000 + 4*i) for i in range(16)]
    out = _rx_bitmode_timestamp_run(1, words, stall=True)
    assert [ts for _, ts in out] == [1000 + 8*i for i in range(8)]


def test_ad9361_rx_bitmode_bfp8_timestamp_is_block_start():
    """Verify BFP8 stamps a block's header and payload with its first input word's timestamp."""
    words = [(0, 1000 + 4*i) for i in range(8)]
    out = _rx_bitmode_timestamp_run(2, words, bfp8_payload_words=2, stall=True)
    # Two blocks of 1 header + 2 payload words, each packing 4 input words.
    assert [ts for _, ts in out] == [1000]*3 + [1016]*3


class _SimRXPHY(LiteXModule):
    """Sim stand-in for AD9361PHY: one RX word every `period` (a power of 2) rfic cycles, ia = word index."""
    def __init__(self, rfic_pads, period=4):
        self.sink   = stream.Endpoint(phy_layout())
        self.source = stream.Endpoint(phy_layout())
        self.cycle  = cycle = Signal(64)
        self.index  = index = Signal(12)

        # # #

        self.comb += self.sink.ready.eq(1)
        self.sync.rfic += [
            cycle.eq(cycle + 1),
            self.source.valid.eq((cycle & (period - 1)) == period - 1),
            If(self.source.valid, index.eq(index + 1)),
        ]
        self.comb += self.source.ia.eq(index)


def test_ad9361_rfic_rx_timestamp_counts_rfic_cycles_across_cdc(monkeypatch):
    """Verify every RX word reaches sys stamped with the rfic cycle it entered the RX FIFO,
    despite sys-side stalls (rfic and sys clocked at the 245.76/125 MHz ratio)."""
    period = 4
    monkeypatch.setattr(core, "AD9361PHY", lambda pads: _SimRXPHY(pads, period=period))
    rfic_pads = Record([("rst_n", 1), ("enable", 1), ("txnrx", 1), ("en_agc", 1),
                        ("ctrl", 4), ("stat", 8)])
    spi_pads  = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1)])
    rfic = core.AD9361RFIC(rfic_pads, spi_pads, sys_clk_freq=125e6,
        with_rx_fifo=True, rx_fifo_depth=64, with_rx_timestamp=True)
    # The RX CDC uses ResetSignal("sys"): give the sim a sys domain with a reset.
    dut = LiteXModule()
    dut.cd_sys = ClockDomain()
    dut.rfic   = rfic
    out  = []
    prng = random.Random(3)

    @passive
    def mon():
        while True:
            # Accept 3 of 4 cycles: ~94 Mwords/s against the 61.44 Mwords/s input.
            yield rfic.source.ready.eq(prng.randrange(4) != 0)
            yield
            if (yield rfic.source.valid) and (yield rfic.source.ready):
                out.append(((yield rfic.source.data) & 0xfff, (yield rfic.source.timestamp)))

    def run():
        for _ in range(600):
            yield

    # 245.76 / 125 MHz ~= 1.97. The RX CDC's common reset re-derives its two clock domains
    # combinationally, which the simulator does not clock: drive them like their parents.
    clocks = {"sys": 26, "rfic": 13}
    cdc_from, cdc_to = [cd.name for cd in rfic.rx_cdc._fragment.clock_domains]
    clocks.update({cdc_from: clocks["rfic"], cdc_to: clocks["sys"]})
    run_simulation(dut, {"sys": [mon(), run()]}, clocks=clocks)

    assert len(out) > 100
    indices = [i for i, _ in out]
    assert indices == [i & 0xfff for i in range(indices[0], indices[0] + len(out))]
    # PHY valid is registered: word i is presented, and written, at rfic cycle period*(i+1).
    for i, ts in out:
        assert ts == period*(i + 1), (i, ts)
