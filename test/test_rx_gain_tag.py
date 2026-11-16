#!/usr/bin/env python3
#
# This file is part of LiteX-M2SDR.
#
# SPDX-License-Identifier: BSD-2-Clause

# RX gain tag: the AD9361 CTRL_OUT sampler, the per-word "gain" side field through the RX bitmode,
# and the header inserter's frame store + tag word (gateware/header.py RXGainTagFramer).

import random

from migen import *
from migen.sim import passive

from litex.gen.sim import run_simulation

from litex_m2sdr.gateware.header import (
    HeaderInserterExtractor, rx_gain_tag_word, RX_GAIN_TAG_CHUNKS,
)
from litex_m2sdr.gateware.ad9361.bitmode import AD9361RXBitMode
from litex_m2sdr.gateware.ad9361.core import AD9361CtrlOutSampler

LEGACY_SYNC = 0x5aa5_5aa5_5aa5_5aa5

# Model --------------------------------------------------------------------------------------------

def gain_field(index, lock=0, valid=1):
    return (index & 0x7f) | (int(lock) << 7) | (int(valid) << 8)

def model_tag(gains):
    """Tag word for one frame from its per-word gain fields (the documented semantics)."""
    n      = len(gains)
    index  = [g & 0x7f for g in gains]
    base   = index[0]
    # Chunk c is words [ceil(c*n/4), ceil((c+1)*n/4)); a chunk with no words carries the end of
    # the chunk before it.
    bounds = [-(-c*n // RX_GAIN_TAG_CHUNKS) for c in range(RX_GAIN_TAG_CHUNKS + 1)]
    ends   = []
    for c in range(RX_GAIN_TAG_CHUNKS):
        lo, hi = bounds[c], bounds[c + 1]
        ends.append(index[hi - 1] if hi > lo else (ends[-1] if ends else base))
    return rx_gain_tag_word(
        valid   = all((g >> 8) & 1 for g in gains),
        lock    = (gains[0] >> 7) & 1,
        changed = any(idx != base for idx in index),
        base    = base,
        ends    = ends,
    )

# Harness ------------------------------------------------------------------------------------------

def run_inserter(frames, frame_cycles, tag_enable=True, fifo_depth=64,
                 sink_gaps=0.0, source_stalls=0.0, seed=1):
    """Stream frames (lists of (data, gain)) through a gain-tag inserter; return what came out.

    Returns (headers, stamps, payload_frames, first_accept_cycles): one (word, timestamp) per
    emitted header, the payload words of each frame, and the cycle each frame's first word was
    accepted on the sink. The inserter's timestamp input is the cycle counter.
    """
    dut = HeaderInserterExtractor(mode="inserter", data_width=64, with_csr=False,
        with_gain_tag=True, gain_tag_fifo_depth=fifo_depth)
    rng = random.Random(seed)
    words = [w for frame in frames for w in frame]
    out = []
    accepts = []
    cycle = [0]

    def clock():
        while True:
            yield dut.timestamp.eq(cycle[0])
            yield
            cycle[0] += 1
    clock = passive(clock)

    def gen():
        yield dut.enable.eq(1)
        yield dut.header_enable.eq(1)
        yield dut.gain_tag_enable.eq(int(tag_enable))
        yield dut.frame_cycles.eq(frame_cycles)
        yield dut.header.eq(LEGACY_SYNC)
        yield
        for i, (data, gain) in enumerate(words):
            while rng.random() < sink_gaps:
                yield dut.sink.valid.eq(0)
                yield
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(data)
            yield dut.sink.gain.eq(gain)
            yield
            while not (yield dut.sink.ready):
                yield
            if i % frame_cycles == 0:
                accepts.append(cycle[0] - 1)
        yield dut.sink.valid.eq(0)
        for _ in range(4*len(words) + 200):
            yield

    def sink_ready():
        while True:
            yield dut.source.ready.eq(int(rng.random() >= source_stalls))
            yield
    sink_ready = passive(sink_ready)

    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.first)))
            yield
    mon = passive(mon)

    run_simulation(dut, [gen(), clock(), sink_ready(), mon()])

    # Parse: each frame = sync word, timestamp word, frame_cycles payload words.
    headers, stamps, payloads = [], [], []
    i = 0
    while i + 2 + frame_cycles <= len(out):
        assert out[i][1] == 1, f"frame {len(headers)} header word lacks first"
        headers.append(out[i][0])
        stamps.append(out[i + 1][0])
        payloads.append([w for w, _ in out[i + 2:i + 2 + frame_cycles]])
        i += 2 + frame_cycles
    # The legacy path emits the next header before its frame's data arrives, so a run can end
    # with one header-only pair; the tagged path never does (no tag without a stored frame).
    trailing = len(out) - i
    assert trailing == 0 or (not tag_enable_effective(tag_enable, frame_cycles, fifo_depth)
                             and trailing == 2 and out[i][1] == 1), f"{trailing} trailing words"
    return headers, stamps, payloads, accepts

def tag_enable_effective(tag_enable, frame_cycles, fifo_depth):
    return tag_enable and frame_cycles <= fifo_depth

def make_frames(n_frames, frame_cycles, rng, invalid_at=None):
    frames = []
    index = 40
    for f in range(n_frames):
        frame = []
        for w in range(frame_cycles):
            # Slow-AGC-like steps of 1-2 indices, sometimes several in a frame.
            if rng.random() < 0.08:
                index = max(0, min(76, index + rng.choice([-2, -1, 1, 2])))
            valid = not (invalid_at == (f, w))
            frame.append(((f << 32) | w, gain_field(index, lock=(f % 2), valid=valid)))
        frames.append(frame)
    return frames

# Inserter / framer --------------------------------------------------------------------------------

def test_gain_tag_matches_model_with_backpressure():
    rng = random.Random(7)
    for frame_cycles in [16, 13, 1022 // 64 * 2 + 2, 6, 4, 3, 2, 1]:
        frames = make_frames(6, frame_cycles, rng)
        headers, stamps, payloads, accepts = run_inserter(frames, frame_cycles,
            sink_gaps=0.3, source_stalls=0.3, seed=frame_cycles)
        assert len(headers) == len(frames)
        for f, frame in enumerate(frames):
            assert headers[f] == model_tag([g for _, g in frame]), \
                f"frame_cycles={frame_cycles} frame {f}: {headers[f]:#018x}"
            assert payloads[f] == [d for d, _ in frame]

def test_gain_tag_timestamp_is_first_word_arrival():
    rng = random.Random(3)
    frames = make_frames(5, 8, rng)
    headers, stamps, payloads, accepts = run_inserter(frames, 8, sink_gaps=0.5, source_stalls=0.6)
    # The stamp is latched on the cycle the frame's first word is accepted (stamp_time, with
    # rx_offset = 0), however long the frame then waits in the frame FIFO.
    assert stamps == accepts

def test_gain_tag_invalid_word_clears_valid():
    rng = random.Random(5)
    frames = make_frames(3, 12, rng, invalid_at=(1, 7))
    headers, _, _, _ = run_inserter(frames, 12)
    assert (headers[0] >> 20) & 1 == 1
    assert (headers[1] >> 20) & 1 == 0
    assert (headers[2] >> 20) & 1 == 1
    assert headers[1] == model_tag([g for _, g in frames[1]])

def test_gain_tag_word_never_matches_legacy_sync():
    # The legacy word decodes as version 5 at bits [19:16]; a v1 tag can never equal it.
    assert (LEGACY_SYNC >> 16) & 0xf == 5
    tag = rx_gain_tag_word(1, 1, 1, 0x7f, [0x7f]*4)
    assert tag & 0xffff == LEGACY_SYNC & 0xffff
    assert (tag >> 16) & 0xf == 1

def test_gain_tag_disabled_emits_legacy_header():
    rng = random.Random(11)
    frames = make_frames(3, 10, rng)
    headers, _, payloads, _ = run_inserter(frames, 10, tag_enable=False, source_stalls=0.2)
    assert headers == [LEGACY_SYNC]*3
    assert payloads == [[d for d, _ in fr] for fr in frames]

def test_gain_tag_oversize_frame_falls_back_to_legacy():
    # A frame longer than the frame FIFO could never complete: the inserter stays untagged.
    rng = random.Random(13)
    frames = make_frames(2, 20, rng)
    headers, _, payloads, _ = run_inserter(frames, 20, fifo_depth=16)
    assert headers == [LEGACY_SYNC]*2
    assert payloads == [[d for d, _ in fr] for fr in frames]

def test_gain_tag_reset_flushes_partial_frame():
    """A reset mid-frame drops the stored words; framing restarts at the next word."""
    dut = HeaderInserterExtractor(mode="inserter", data_width=64, with_csr=False,
        with_gain_tag=True, gain_tag_fifo_depth=32)
    out = []

    def gen():
        yield dut.enable.eq(1)
        yield dut.header_enable.eq(1)
        yield dut.gain_tag_enable.eq(1)
        yield dut.frame_cycles.eq(4)
        yield dut.source.ready.eq(1)
        # Two words of a frame, then a reset (which drains), then two full frames.
        for w in [(0xdead, gain_field(9)), (0xbeef, gain_field(9))]:
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(w[0])
            yield dut.sink.gain.eq(w[1])
            yield
        yield dut.sink.valid.eq(0)
        yield dut.reset.eq(1)
        for _ in range(4):
            yield
        yield dut.reset.eq(0)
        yield
        for i in range(8):
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(i)
            yield dut.sink.gain.eq(gain_field(20 + i))
            yield
            while not (yield dut.sink.ready):
                yield
        yield dut.sink.valid.eq(0)
        for _ in range(40):
            yield

    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield
    mon = passive(mon)

    run_simulation(dut, [gen(), mon()])
    assert len(out) == 2*(2 + 4)
    assert out[0] == model_tag([gain_field(20 + i) for i in range(4)])
    assert out[2:6] == [0, 1, 2, 3]
    assert out[6] == model_tag([gain_field(24 + i) for i in range(4)])
    assert out[8:12] == [4, 5, 6, 7]

# RX bitmode: gain side field ----------------------------------------------------------------------

def _run_bitmode(mode, words):
    dut = AD9361RXBitMode(data_width=64, with_gain=True)
    out = []

    def gen():
        yield dut.mode.eq(mode)
        yield dut.source.ready.eq(1)
        for data, gain in words:
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(data)
            yield dut.sink.gain.eq(gain)
            yield
            while not (yield dut.sink.ready):
                yield
        yield dut.sink.valid.eq(0)
        for _ in range(8):
            yield

    def mon():
        while True:
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.gain)))
            yield
    mon = passive(mon)

    run_simulation(dut, [gen(), mon()])
    return out

def test_rx_bitmode_16bit_forwards_gain():
    words = [(i, gain_field(i)) for i in range(6)]
    out = _run_bitmode(0, words)
    assert [g for _, g in out] == [g for _, g in words]

def test_rx_bitmode_8bit_gain_is_first_word():
    words = [(i, gain_field(10 + i)) for i in range(8)]
    out = _run_bitmode(1, words)
    assert [g for _, g in out] == [gain_field(10 + 2*k) for k in range(4)]

# CTRL_OUT sampler ---------------------------------------------------------------------------------

def test_ctrl_out_sampler_settles_and_rejects_torn_bytes():
    class DUT(Module):
        def __init__(self):
            self.ctrl_out = Signal(8)
            self.submodules.sampler = AD9361CtrlOutSampler(self.ctrl_out)
    dut = DUT()
    seen = []

    def gen():
        yield dut.ctrl_out.eq(0x2a)
        for _ in range(6):
            yield
        seen.append((yield dut.sampler.gain))
        # A one-cycle torn byte must never be accepted.
        yield dut.ctrl_out.eq(0x55)
        yield
        yield dut.ctrl_out.eq(0x2a)
        for _ in range(6):
            yield
            seen.append((yield dut.sampler.gain))
        # A real change is accepted.
        yield dut.ctrl_out.eq(0xab)
        for _ in range(6):
            yield
        seen.append((yield dut.sampler.gain))

    run_simulation(dut, gen())
    assert seen[0] == (1 << 8) | 0x2a
    assert all(g == (1 << 8) | 0x2a for g in seen[1:-1])
    assert seen[-1] == (1 << 8) | 0xab


def test_gain_tag_chunk_ends_match_bladerf_sample_chunks():
    """1T1R SC16: a 1022-word frame is 2044 samples; chunk ends sit on the words holding samples
    510/511 (end of chunk 0 at sample 510), 1021, 1532 and 2043, i.e. 511 samples per chunk."""
    n = 1022
    bounds = [-(-c*n // RX_GAIN_TAG_CHUNKS) for c in range(RX_GAIN_TAG_CHUNKS + 1)]
    last_words = [b - 1 for b in bounds[1:]]
    assert last_words == [255, 510, 766, 1021]
    for c, w in enumerate(last_words):
        last_sample = (c + 1)*2044//4 - 1
        assert last_sample // 2 == w
