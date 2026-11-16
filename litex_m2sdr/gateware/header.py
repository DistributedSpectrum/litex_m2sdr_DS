#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2024-2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

from litepcie.common import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *

from litex_m2sdr.gateware.ad9361.bitmode import rx_layout
from litex_m2sdr.gateware.ad9361.bitmode import RX_GAIN_INDEX_BITS, RX_GAIN_LOCK_BIT, RX_GAIN_VALID_BIT

# RX Gain Tag --------------------------------------------------------------------------------------
#
# With the RX gain tag enabled, the 64-bit RX header word (the legacy constant sync word
# 0x5aa55aa55aa55aa5) carries the AD9361 RX1 gain-table index over the frame that follows, in the
# same spirit as bladeRF_DS's per-message gain tag (base + per-chunk end values):
#
#   bits [15: 0]  0x5aa5 sync (also the low 16 bits of the legacy word)
#   bits [19:16]  tag version, 1 (the legacy word has 5 here, so the two never collide)
#   bit  [20]     VALID   every word of the frame carried a settled CTRL_OUT byte
#   bit  [21]     LOCK    CTRL_OUT[7] (RX1 gain lock) at the first word of the frame
#   bit  [22]     CHANGED some word of the frame had a gain index different from BASE
#   bits [30:24]  BASE    gain index at the first word of the frame
#   bits [38:32]  END0    gain index at the last word of chunk 0
#   bits [46:40]  END1    gain index at the last word of chunk 1
#   bits [54:48]  END2    gain index at the last word of chunk 2
#   bits [62:56]  END3    gain index at the last word of chunk 3 (= last word of the frame)
#   bits 23, 31, 39, 47, 55, 63 are reserved (0).
#
# A frame is W = frame_cycles stream words. Chunk c is words [B(c), B(c+1)) with
# B(c) = ceil(c * W / 4), so B(0) = 0 and B(4) = W. The rounding puts each chunk end on the word
# that holds sample (c + 1) * S / 4 - 1 of a frame of S samples (S = 2044 in 1T1R SC16: chunk ends
# at samples 510, 1021, 1532, 2043), which is the bladeRF chunk definition (S / 4 samples each)
# whenever a word carries more than one sample. A chunk with no words (W < 4) reports the end of
# the chunk before it (BASE for chunk 0's predecessor).
#
# The header precedes its frame in the DMA buffer, so the frame is stored first: the framer writes
# each word into a frame FIFO while it updates the tag, then queues {tag, timestamp} once the
# frame's last word is in. The inserter emits a header only when a tag is queued, so the stream
# is delayed by one frame. The timestamp is latched when the frame's first word reaches the framer
# (not when the header leaves, as on the legacy path).

RX_GAIN_TAG_SYNC    = 0x5aa5
RX_GAIN_TAG_VERSION = 1
RX_GAIN_TAG_CHUNKS  = 4

def rx_gain_tag_word(valid, lock, changed, base, ends):
    """Software model of the tag word (used by the tests and as the reference for decoders)."""
    assert len(ends) == RX_GAIN_TAG_CHUNKS
    word  = RX_GAIN_TAG_SYNC
    word |= RX_GAIN_TAG_VERSION << 16
    word |= int(bool(valid))   << 20
    word |= int(bool(lock))    << 21
    word |= int(bool(changed)) << 22
    word |= (base & 0x7f)      << 24
    for c, end in enumerate(ends):
        word |= (end & 0x7f) << (32 + 8*c)
    return word

class RXGainTagFramer(LiteXModule):
    """Store-and-forward one frame of RX words and build its gain tag.

    sink:       RX words with the per-word "gain" side field (bitmode.rx_layout(with_gain)).
    source:     the same words, without the side field, one frame behind.
    tag_source: {tag, timestamp} per frame, queued once the frame's last word is stored.
    """
    def __init__(self, data_width=64, fifo_depth=2048, tag_depth=4):
        self.sink         = sink       = stream.Endpoint(rx_layout(data_width, with_gain=True))
        self.source       = source     = stream.Endpoint(dma_layout(data_width))
        self.tag_source   = tag_source = stream.Endpoint([("tag", 64), ("timestamp", 64)])
        self.frame_cycles = Signal(32) # i Frame length in words (>= 1, <= fifo_depth).
        self.timestamp    = Signal(64) # i Stamp latched at each frame's first word.
        self.flush        = Signal()   # i Drop stored words/tags and restart framing.
        self.drain        = Signal()   # i While flushing: accept (and drop) incoming words.
        self.fifo_depth   = fifo_depth

        # # #

        # FIFOs.
        # ------
        self.data_fifo = data_fifo = ResetInserter()(
            stream.SyncFIFO(dma_layout(data_width), depth=fifo_depth, buffered=True))
        self.tag_fifo  = tag_fifo  = ResetInserter()(
            stream.SyncFIFO([("tag", 64), ("timestamp", 64)], depth=tag_depth, buffered=False))
        self.comb += [
            data_fifo.reset.eq(self.flush),
            tag_fifo.reset.eq(self.flush),
            data_fifo.source.connect(source),
            tag_fifo.source.connect(tag_source),
        ]

        # Framing.
        # --------
        count = Signal(32) # Word index in the frame.
        chunk = Signal(2)  # Chunk of the current word.
        # Chunk boundaries B(1..3) = ceil(c * W / 4), registered: frame_cycles only changes while
        # the stream is stopped (and a change flushes the framer through the inserter reset).
        bounds = [Signal(34, name=f"chunk_bound{c}") for c in range(1, RX_GAIN_TAG_CHUNKS)]
        for c, bound in enumerate(bounds, start=1):
            self.sync += bound.eq((c*self.frame_cycles + (RX_GAIN_TAG_CHUNKS - 1)) >> 2)
        self.comb += chunk.eq(sum(count >= bound for bound in bounds))
        first = Signal()
        last  = Signal()
        self.comb += [
            first.eq(count == 0),
            last.eq(count == (self.frame_cycles - 1)),
        ]

        # Tag state: registered over the frame, with the "_n" values including the current word.
        gain_index = sink.gain[0:RX_GAIN_INDEX_BITS]
        gain_lock  = sink.gain[RX_GAIN_LOCK_BIT]
        gain_valid = sink.gain[RX_GAIN_VALID_BIT]
        base,   base_n    = Signal(RX_GAIN_INDEX_BITS), Signal(RX_GAIN_INDEX_BITS)
        lock,   lock_n    = Signal(), Signal()
        valid,  valid_n   = Signal(), Signal()
        changed, changed_n = Signal(), Signal()
        ts,     ts_n      = Signal(64), Signal(64)
        ends   = [Signal(RX_GAIN_INDEX_BITS, name=f"end{c}")   for c in range(RX_GAIN_TAG_CHUNKS)]
        ends_n = [Signal(RX_GAIN_INDEX_BITS, name=f"end{c}_n") for c in range(RX_GAIN_TAG_CHUNKS)]
        self.comb += [
            base_n.eq(   Mux(first, gain_index, base)),
            lock_n.eq(   Mux(first, gain_lock,  lock)),
            valid_n.eq(  Mux(first, gain_valid, valid & gain_valid)),
            changed_n.eq(Mux(first, 0,          changed | (gain_index != base))),
            ts_n.eq(     Mux(first, self.timestamp, ts)),
        ]
        for c in range(RX_GAIN_TAG_CHUNKS):
            # Last write wins: each chunk ends up holding the index at its last word. Every chunk
            # starts the frame at the first word's index, and the frame's last word also writes
            # the chunks after its own, so a chunk with no words reports the end of the chunk
            # before it.
            self.comb += ends_n[c].eq(Mux(first | (chunk == c) | (last & (chunk < c)),
                gain_index, ends[c]))

        # Datapath.
        # ---------
        accept = Signal()
        self.comb += [
            If(self.flush,
                sink.ready.eq(self.drain),
            ).Else(
                # The last word of a frame also needs room for its tag.
                sink.ready.eq(data_fifo.sink.ready & (~last | tag_fifo.sink.ready)),
                accept.eq(sink.valid & sink.ready),
                data_fifo.sink.valid.eq(accept),
                tag_fifo.sink.valid.eq(accept & last),
            ),
            data_fifo.sink.data.eq(sink.data),
            tag_fifo.sink.tag.eq(Cat(
                C(RX_GAIN_TAG_SYNC, 16), C(RX_GAIN_TAG_VERSION, 4),
                valid_n, lock_n, changed_n, C(0, 1),
                base_n, C(0, 1),
                *[Cat(ends_n[c], C(0, 1)) for c in range(RX_GAIN_TAG_CHUNKS)],
            )),
            tag_fifo.sink.timestamp.eq(ts_n),
        ]
        self.sync += [
            If(self.flush,
                count.eq(0),
            ).Elif(accept,
                base.eq(base_n),
                lock.eq(lock_n),
                valid.eq(valid_n),
                changed.eq(changed_n),
                ts.eq(ts_n),
                *[ends[c].eq(ends_n[c]) for c in range(RX_GAIN_TAG_CHUNKS)],
                If(last,
                    count.eq(0),
                ).Else(
                    count.eq(count + 1),
                )
            )
        ]

# Header Inserter/Extractor ------------------------------------------------------------------------

class HeaderInserterExtractor(LiteXModule):
    def __init__(self, mode="inserter", data_width=64, with_csr=True,
        with_gain_tag=False, gain_tag_fifo_depth=2048):
        # The DMA header is a fixed 16 bytes (sync word + ns timestamp). At 64-bit it occupies
        # two stream words (HEADER then TIMESTAMP states); at 128-bit it fits in one word
        # ([timestamp(64) | sync(64)], low half = sync = first buffer bytes), so the FSM emits/
        # captures it in a single HEADER state. 128-bit is used by the wide (Oversampling) path
        # so the sys-domain word rate halves (2T2R @ 122.88 MSPS fits the 64-bit @ 125MHz fabric).
        assert data_width in [64, 128]
        assert mode in ["inserter", "extractor"]
        # with_gain_tag (inserter, 64-bit only): the sink carries the per-word RX "gain" side
        # field and, when enabled at runtime (gain_tag_enable, with header_enable set), the header
        # word becomes the RX gain tag (see RXGainTagFramer above).
        assert not (with_gain_tag and mode != "inserter"), "The gain tag is an RX (inserter) feature."
        assert not (with_gain_tag and data_width != 64), "The gain tag needs the 64-bit datapath."
        self.data_width    = data_width
        self.with_gain_tag = with_gain_tag
        single_word = (data_width == 128)
        sink_description = rx_layout(data_width, with_gain=True) if with_gain_tag else dma_layout(data_width)
        self.sink   = sink   = stream.Endpoint(sink_description)       # i
        self.source = source = stream.Endpoint(dma_layout(data_width)) # o

        self.reset         = Signal() # i

        self.update        = Signal()   # o
        self.header        = Signal(64) # i (Inserter) / o (Extractor)
        self.timestamp     = Signal(64) # i (Inserter) / o (Extractor)

        self.enable        = Signal()   # i (CSR).
        self.header_enable = Signal()   # i (CSR).
        self.frame_cycles  = Signal(32) # i (CSR).

        # Timed-TX gate (extractor only). Hold each frame until the FPGA time (+ tx_offset,
        # the calibrated TX-pipeline latency) reaches the frame's air-time (header ns
        # timestamp), then release so the signal is ON THE AIR at exactly the timestamp.
        # A frame that arrives already past its air-time is dropped entirely (no tolerance
        # window) and counted as a TX underflow. The PHY airs zeros during any hold or drop
        # ("Clear to avoid spurs" in phy.py), so gaps are clean.
        self.mode           = mode
        self.time           = Signal(64) # i  FPGA time (ns).
        self.tx_offset      = Signal(64) # i  CSR: TX pipeline offset (ns).
        self.underflow      = Signal(32) # o  TX underflow count: timed frames that missed
                                         #    their air-time (dropped -> RFIC aired zeros).

        # RX timestamp plane correction (inserter only), the mirror of tx_offset: the inserted
        # timestamp marks the cycle the buffer starts forming (DMA plane), so rx_offset is
        # subtracted to refer it back to when the first sample was at the ANTENNA (fixed RX
        # pipeline latency: antenna -> ADC -> deserializer -> unpacker -> DMA). With both
        # calibrated, TX and RX timestamps share one reference plane. Reset 0 keeps the
        # current stamping bit-exact.
        self.rx_offset      = Signal(64) # i  CSR: RX pipeline offset (ns).

        # RX gain tag (inserter with with_gain_tag only).
        self.gain_tag_enable = Signal()   # i  CSR: tag the header word (needs header_enable).
        self.gain_tag_active = Signal()   # o  The tagged path is in use.
        # Header word / timestamp actually inserted by the last header (inserter).
        self.header_out     = Signal(64) # o
        self.stamp_out      = Signal(64) # o

        if with_csr:
            self.add_csr()

        # # #

        # Signals.
        # --------
        cycles = Signal(32)
        frame_cycles_eff = Signal(32)
        self.comb += frame_cycles_eff.eq(Mux(self.frame_cycles == 0, 1, self.frame_cycles))
        # Release reference: the frame airs at self.time + tx_offset, so this is what the FSM
        # compares against the requested air-time (self.timestamp).
        #
        # Registered on the sys clock (not combinational): tx_offset is a CSR register and this
        # 64-bit add feeds three 64-bit comparisons that select the FSM next-state. The sys clock
        # is the design's critical domain, so keeping the whole CSR -> 64-bit adder -> 64-bit
        # comparator -> FSM chain in one cycle costs timing slack; registering splits it into
        # (CSR -> adder -> flop) and (flop -> comparator -> FSM). The one added sys-clock cycle of
        # release latency is a fixed constant, absorbed by the tx_offset calibration.
        gate_now = Signal(64)
        self.sync += gate_now.eq(self.time + self.tx_offset)

        # Inserted stamp: timestamp input (the live FPGA time) referred back to the antenna
        # plane. Registered on the sys clock for the same timing reason as gate_now; the one
        # added cycle is a fixed constant absorbed by the rx_offset calibration. Clamped at 0
        # so a boot-time stamp (time still below rx_offset) cannot wrap to a huge bogus value.
        stamp_time = Signal(64)
        self.sync += stamp_time.eq(
            Mux(self.timestamp >= self.rx_offset, self.timestamp - self.rx_offset, 0))
        self.stamp_time = stamp_time

        # Header source (inserter).
        # -------------------------
        # fsm_sink is the stream the FSM frames. hdr_ready/hdr_word/hdr_ts are the header it
        # emits: on the legacy path the constant header input and the live stamp, always ready;
        # on the gain-tag path the framer's queued {tag, timestamp}, ready once a whole frame is
        # stored behind it.
        hdr_ready = Signal()
        hdr_word  = Signal(64)
        hdr_ts    = Signal(64)
        hdr_pop   = Signal()
        if with_gain_tag:
            fsm_sink = stream.Endpoint(dma_layout(data_width))
            self.framer = framer = RXGainTagFramer(data_width, fifo_depth=gain_tag_fifo_depth)
            tagged = self.gain_tag_active
            fsm_reset = Signal()
            self.comb += [
                # Frames longer than the frame FIFO could never complete: keep those untagged.
                tagged.eq(self.header_enable & self.gain_tag_enable &
                    (frame_cycles_eff <= gain_tag_fifo_depth)),
                fsm_reset.eq(self.reset | ~self.enable),
                framer.frame_cycles.eq(frame_cycles_eff),
                framer.timestamp.eq(stamp_time),
                framer.flush.eq(fsm_reset | ~tagged),
                framer.drain.eq(self.reset),
                If(tagged,
                    sink.connect(framer.sink),
                    framer.source.connect(fsm_sink),
                    hdr_ready.eq(framer.tag_source.valid),
                    hdr_word.eq(framer.tag_source.tag),
                    hdr_ts.eq(framer.tag_source.timestamp),
                    framer.tag_source.ready.eq(hdr_pop),
                ).Else(
                    sink.connect(fsm_sink, omit={"gain"}),
                    hdr_ready.eq(1),
                    hdr_word.eq(self.header),
                    hdr_ts.eq(stamp_time),
                ),
            ]
        else:
            fsm_sink = sink
            self.comb += [
                hdr_ready.eq(1),
                hdr_word.eq(self.header),
                hdr_ts.eq(stamp_time),
            ]
        # Default: the live values (what the legacy path inserts); latched below on each header.
        header_out_q = Signal(64)
        stamp_out_q  = Signal(64)
        self.comb += [
            self.header_out.eq(header_out_q),
            self.stamp_out.eq(stamp_out_q),
        ]

        # FSM.
        # ----
        self.fsm = fsm = ResetInserter()(FSM(reset_state="RESET"))
        self.comb += self.fsm.reset.eq(self.reset | ~self.enable)

        # Reset.
        reset_actions = [
            NextValue(cycles, 0),
            NextState("IDLE"),
        ]
        if mode == "inserter":
            # Drain the upstream while an explicit reset is asserted so the
            # producer does not stall against a held-in-reset inserter. (The
            # mode selection is an elaboration-time constant, hence the
            # Python conditional.)
            reset_actions.append(fsm_sink.ready.eq(self.reset))
        fsm.act("RESET", *reset_actions)

        # Idle.
        fsm.act("IDLE",
            NextValue(cycles, 0),
            If(self.header_enable,
                NextState("HEADER")
            ).Else(
                NextState("FRAME")
            )
        )

        # Inserter specific.
        if mode == "inserter":
            if single_word:
                # 128-bit: sync + timestamp (antenna-plane corrected, see stamp_time above) in
                # one word -> straight to FRAME.
                fsm.act("HEADER",
                    source.valid.eq(hdr_ready),
                    source.first.eq(1),
                    source.data[0:64].eq(hdr_word),
                    source.data[64:128].eq(hdr_ts),
                    If(source.valid & source.ready,
                        hdr_pop.eq(1),
                        NextValue(header_out_q, hdr_word),
                        NextValue(stamp_out_q, hdr_ts),
                        NextValue(self.update, 1),
                        NextState("FRAME"),
                    )
                )
            else:
                # 64-bit: sync word, then timestamp word.
                # The timestamp word goes out after the sync word. On the gain-tag path the tag
                # is popped with the first word, so its timestamp is held until the second; the
                # legacy path keeps stamping the live time in the TIMESTAMP state (bit-exact with
                # the pre-gain-tag gateware).
                hdr_ts_q = Signal(64)
                ts_word  = Signal(64)
                self.comb += ts_word.eq(Mux(self.gain_tag_active, hdr_ts_q, stamp_time))
                fsm.act("HEADER",
                    source.valid.eq(hdr_ready),
                    source.first.eq(1),
                    source.data[0:64].eq(hdr_word),
                    If(source.valid & source.ready,
                        hdr_pop.eq(1),
                        NextValue(header_out_q, hdr_word),
                        NextValue(hdr_ts_q, hdr_ts),
                        NextState("TIMESTAMP"),
                    )
                )
                # Timestamp (antenna-plane corrected, see stamp_time above).
                fsm.act("TIMESTAMP",
                    source.valid.eq(1),
                    source.data[0:64].eq(ts_word),
                    If(source.valid & source.ready,
                        NextValue(stamp_out_q, ts_word),
                        NextValue(self.update, 1),
                        NextState("FRAME"),
                    )
                )

        # Extractor specific.
        if mode == "extractor":
            if single_word:
                # 128-bit: capture sync + timestamp from one word, then enter the timed gate.
                fsm.act("HEADER",
                    sink.ready.eq(1),
                    If(sink.valid & sink.ready & sink.first,
                        NextValue(self.header, sink.data[0:64]),
                        NextValue(self.timestamp, sink.data[64:128]),
                        NextValue(self.update, 1),
                        NextState("GATE")
                    )
                )
            else:
                # 64-bit: sync word, then timestamp word. On timestamp capture, enter the timed
                # gate instead of the frame directly.
                fsm.act("HEADER",
                    sink.ready.eq(1),
                    If(sink.valid & sink.ready & sink.first,
                        NextValue(self.header, sink.data[0:64]),
                        NextState("TIMESTAMP")
                    )
                )
                fsm.act("TIMESTAMP",
                    sink.ready.eq(1),
                    If(sink.valid & sink.ready,
                        NextValue(self.timestamp, sink.data[0:64]),
                        NextValue(self.update, 1),
                        NextState("GATE")
                    )
                )

            # Timed gate. Decide on arrival (no tolerance window):
            #  - timestamp == 0  -> UNTIMED: transmit immediately (pass-through). Continuous
            #    streaming clients (and any writer that does not set a per-buffer air-time)
            #    leave the header timestamp at 0; they must stream with zero added latency, so
            #    0 bypasses the gate. 0 is never a legitimate air-time (the FPGA time counter is
            #    0-based at power-on, so air-time 0 is always in the past) -> safe sentinel.
            #  - air-time still ahead -> WAIT (hold); exactly now -> release; already passed
            #    (frame arrived too late to hit its air-time) -> DROP.
            # A held frame releases the exact cycle the FPGA time reaches its air-time
            # (deterministic to the time tick).
            fsm.act("GATE",
                If(self.timestamp == 0,
                    NextState("FRAME")
                ).Elif(gate_now < self.timestamp,
                    NextState("WAIT")
                ).Elif(gate_now == self.timestamp,
                    NextState("FRAME")
                ).Else(
                    NextState("DROP")
                )
            )
            fsm.act("WAIT",
                # Hold (RFIC airs zeros); release the cycle the FPGA time reaches the air-time.
                If(gate_now >= self.timestamp,
                    NextState("FRAME")
                )
            )
            fsm.act("DROP",
                # A late frame is dropped by airing ZEROS for its whole duration, PACED to the RFIC
                # sample rate exactly like FRAME (advance on source.valid & source.ready) -- NOT
                # drained at sink.ready=1 (fabric/DMA speed). Fast-draining empties the TX DMA ring
                # far faster than real time, so the LOOP-mode reader races ahead of the host
                # (reader_hw_count >> host submit) -> m2sdr_sync_tx -11 UNDERFLOW; and once ANY frame
                # is late every later frame is late too, each fast-dropped -> unrecoverable positive
                # feedback (the timed-TX storm). Pacing makes a dropped slot cost one frame air-time,
                # so the reader stays real-time-locked. source.data is 0 by default in non-FRAME
                # states, so the RFIC airs zeros; only the 1-bit valid/last/ready are added here.
                source.valid.eq(sink.valid),
                source.last.eq(cycles == (frame_cycles_eff - 1)),
                sink.ready.eq(source.ready),
                If(source.valid & source.ready,
                    NextValue(cycles, cycles + 1),
                    If(cycles == (frame_cycles_eff - 1),
                        NextValue(cycles, 0),
                        NextValue(self.underflow, self.underflow + 1),
                        NextState("HEADER")
                    )
                )
            )

        # Frame.
        fsm.act("FRAME",
            fsm_sink.connect(source, omit={"first"}),
            NextValue(self.update, 0),
            If(self.header_enable,
                source.first.eq((cycles == 0) & (mode == "extractor")),
                source.last.eq( cycles == (frame_cycles_eff - 1)),
                If(source.valid & source.ready,
                    NextValue(cycles, cycles + 1),
                    If(source.last,
                        NextValue(cycles, 0),
                        NextState("HEADER")
                    )
                )
            )
        )

    def add_csr(self, default_enable=1, default_header_enable=0, default_frame_cycles=None):
        # One DMA buffer (8192 B) minus the header words, in stream words of data_width/8 bytes:
        # 64-bit -> 8192/8 - 2 header words = 1022; 128-bit -> 8192/16 - 1 header word = 511.
        if default_frame_cycles is None:
            bytes_per_word = self.data_width // 8
            header_words   = 1 if self.data_width == 128 else 2
            default_frame_cycles = 8192 // bytes_per_word - header_words
        self._control = CSRStorage(fields=[
            CSRField("enable", size=1, offset=0, values=[
                ("``0b0``", "Module Disabled."),
                ("``0b1``", "Module Enabled."),
            ], reset=default_enable),
            CSRField("header_enable", size=1, offset=1, values=[
                ("``0b0``", "Header Inserter/Extractor Disabled."),
                ("``0b1``", "Header Inserter/Extractor Enabled."),
            ], reset=default_header_enable),
        ])
        self._frame_cycles = CSRStorage(32, description="Frame Cycles (stream words of data_width/8 bytes)", reset=int(default_frame_cycles))

        # # #

        self.comb += [
            self.enable.eq(self._control.fields.enable),
            self.header_enable.eq(self._control.fields.header_enable),
            self.frame_cycles.eq(self._frame_cycles.storage),
        ]

        # The timed-TX gate and timestamp-plane CSRs (tx_offset/tx_underflow/rx_offset) are
        # declared by TXRXHeader AFTER the pre-existing CSRs, so the HEADER CSR map stays
        # backward-compatible: software built against an older csr.h keeps addressing every
        # pre-gate register correctly on new gateware and vice versa.

# TX Header Extractor ------------------------------------------------------------------------------

class TXHeaderExtractor(HeaderInserterExtractor):
    def __init__(self, data_width=128, with_csr=True):
        HeaderInserterExtractor.__init__(self,
            mode       = "extractor",
            data_width = data_width,
            with_csr   = with_csr,
        )

# RX Header Inserter -------------------------------------------------------------------------------

class RXHeaderInserter(HeaderInserterExtractor):
    def __init__(self, data_width=128, with_csr=True, with_gain_tag=False):
        HeaderInserterExtractor.__init__(self,
            mode          = "inserter",
            data_width    = data_width,
            with_csr      = with_csr,
            with_gain_tag = with_gain_tag,
        )

# TX/RX Header -------------------------------------------------------------------------------------

class TXRXHeader(LiteXModule):
    def __init__(self, data_width, with_csr=True, with_rx_gain_tag=False):
        # TX.
        self.tx = TXHeaderExtractor(data_width, with_csr)

        # RX.
        self.rx = RXHeaderInserter(data_width, with_csr, with_gain_tag=with_rx_gain_tag)

        # CSR.
        if with_csr:
            self.last_tx_header    = CSRStatus(64, description="Last TX Header.")
            self.last_tx_timestamp = CSRStatus(64, description="Last TX Timestamp.")
            self.last_rx_header    = CSRStatus(64, description="Last RX Header.")
            self.last_rx_timestamp = CSRStatus(64, description="Last RX Timestamp.")
            # Timed-TX gate / timestamp-plane CSRs. Declared AFTER the pre-existing CSRs on
            # purpose: they are appended at the end of the HEADER CSR block, so every register
            # above keeps its pre-gate address and software built against either csr.h vintage
            # keeps addressing the common registers correctly (presence of the gate is
            # advertised through the capability features CSR, not probed by address).
            self._tx_offset    = CSRStorage(64, reset=0, description=
                "Timed-TX pipeline offset (ns): added to the FPGA time before comparing against the "
                "header air-time so the signal is on the air at exactly the timestamp (loopback-calibrated).")
            self._tx_underflow = CSRStatus(32, description=
                "TX underflow count: timed frames that missed their air-time (dropped whole; "
                "the RFIC aired zeros for them).")
            self._rx_offset    = CSRStorage(64, reset=0, description=
                "RX timestamp offset (ns): subtracted from the FPGA time before insertion into the "
                "RX header, referring the stamp back to the antenna plane (the mirror of tx_offset).")
            self.comb += [
                self.tx.tx_offset.eq(self._tx_offset.storage),
                self._tx_underflow.status.eq(self.tx.underflow),
                self.rx.rx_offset.eq(self._rx_offset.storage),
            ]
            # RX gain tag CSRs, appended after every pre-existing HEADER CSR for the same
            # address-compatibility reason (presence is advertised by the capability
            # features.rx_gain_tag bit).
            if with_rx_gain_tag:
                self._rx_gain_tag = CSRStorage(fields=[
                    CSRField("enable", size=1, offset=0, reset=0, values=[
                        ("``0b0``", "RX header word is the legacy sync word."),
                        ("``0b1``", "RX header word is the RX gain tag (needs header_enable)."),
                    ]),
                ], description="RX gain tag control. Change only while the RX DMA is stopped.")
                self._rx_gain_tag_status = CSRStatus(fields=[
                    CSRField("active", size=1, offset=0,
                        description="The gain-tag path is in use (enable, header_enable and a frame that fits the frame FIFO)."),
                    CSRField("fifo_depth", size=16, offset=16, reset=self.rx.framer.fifo_depth,
                        description="Frame FIFO depth in words: the longest frame that can be tagged."),
                ])
                self.comb += [
                    self.rx.gain_tag_enable.eq(self._rx_gain_tag.fields.enable),
                    self._rx_gain_tag_status.fields.active.eq(self.rx.gain_tag_active),
                ]
            self.sync += [
                # Reset.
                If(self.tx.reset,
                    self.last_tx_header.status.eq(0),
                    self.last_tx_timestamp.status.eq(0),
                ),
                If(self.rx.reset,
                    self.last_rx_header.status.eq(0),
                    self.last_rx_timestamp.status.eq(0),
                ),
                # TX Update.
                If(self.tx.update,
                    self.last_tx_header.status.eq(self.tx.header),
                    self.last_tx_timestamp.status.eq(self.tx.timestamp),
                ),
                # RX Update (header_out/stamp_out: the header word and antenna-plane stamp
                # actually inserted).
                If(self.rx.update,
                    self.last_rx_header.status.eq(self.rx.header_out),
                    self.last_rx_timestamp.status.eq(self.rx.stamp_out),
                )
            ]
