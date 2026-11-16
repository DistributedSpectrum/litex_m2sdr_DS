#
# This file is part of LiteX-M2SDR.
#
# SPDX-License-Identifier: BSD-2-Clause

# GPS time input: UBX-NAV-TIMEUTC over a UART + a qualified PPS -> board time in UTC.
#
# The M.2 breakout carries a u-blox ZED-F9P (Gateworks GW16143) to the SDR: its UART1 TX on
# Synchro_GPIO2 (M.2 pin 30, FPGA A18), its RX on Synchro_GPIO3 (pin 32, A21), and its timepulse
# on PPS_IN (pin 22, K18). This core is the M2SDR counterpart of the bladeRF_DS gps_uart block:
#
# - Decode UBX-NAV-TIMEUTC (class 0x01, id 0x21, 20-byte payload), checked by its Fletcher
#   checksum and the message's validUTC flag, into a UTC epoch (seconds since 1970) in hardware.
#   The message for second S arrives tens of ms after the PPS that starts S.
# - Qualify the PPS: an edge counts only after pps_filter_cycles consecutive high samples, and the
#   input is ignored for pps_refractory_s after an accepted edge (on bladeRF, UART crosstalk on the
#   PPS line produced bursts of false edges ~40 ms after each real one).
# - On each qualified PPS that follows a fresh valid decode of second S, pulse the TimeGenerator's
#   time sync with seconds = S: board time becomes (S + 1) * 1e9 + sync_offset ns at the edge
#   that starts second S + 1. Every RX header timestamp is then GPS UTC in ns.
#   Sync modes: off (default), every PPS, or once (armed by a CSR pulse).
# - PPS bypass (control.pps_bypass, host-set, off at reset): ignore the PPS pin. No PPS is
#   qualified, latched or counted, so nothing syncs and the board time free-runs; UBX decoding
#   carries on, so utc_valid/epoch still show what the receiver says. For a PPS line that is
#   missing, noisy or not trusted (indoors, a floating return, crosstalk). PPSInput (pps_in CSRs)
#   still watches the pin, so software can see whether edges arrive while bypassed. The bladeRF_DS
#   gps_uart image has the same switch (NIOS 8x32 target GPS_PPS_BYPASS).
# - Latch the board time on each qualified PPS (before any sync), so software can measure the
#   board clock against GPS (consecutive latches while free-running, or the step size while
#   syncing every second) and trim it (m2sdr_set_refclk_ppm disciplines the SI5351, which clocks
#   both the AD9361 and the time generator).
#
# The F9P must output NAV-TIMEUTC on UART1 at the configured baud (provisioned host-side over its
# USB port, as for bladeRF), with the timepulse on the UTC top of the second.

import datetime

from migen import *
from migen.genlib.cdc import MultiReg, PulseSynchronizer

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.cores.uart import RS232PHYRX

# Constants ----------------------------------------------------------------------------------------

UBX_SYNC1          = 0xb5
UBX_SYNC2          = 0x62
UBX_CLASS_NAV      = 0x01
UBX_ID_TIMEUTC     = 0x21
UBX_TIMEUTC_LEN    = 20
UBX_VALID_UTC_BIT  = 2

GPS_EPOCH_YEAR_MIN = 1970
GPS_EPOCH_YEAR_MAX = 2105

def _days_before_year(year):
    return (datetime.date(year, 1, 1) - datetime.date(1970, 1, 1)).days

_DAYS_BEFORE_MONTH = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]

def ubx_timeutc_frame(year, month, day, hour, minute, second, nano=0, valid=0x07, t_acc=20,
                      itow=0):
    """Build a UBX-NAV-TIMEUTC frame (bytes), for tests and bench tools."""
    payload  = itow.to_bytes(4, "little")
    payload += t_acc.to_bytes(4, "little")
    payload += nano.to_bytes(4, "little", signed=True)
    payload += year.to_bytes(2, "little")
    payload += bytes([month, day, hour, minute, second, valid])
    body = bytes([UBX_CLASS_NAV, UBX_ID_TIMEUTC]) + len(payload).to_bytes(2, "little") + payload
    ck_a = ck_b = 0
    for b in body:
        ck_a = (ck_a + b) & 0xff
        ck_b = (ck_b + ck_a) & 0xff
    return bytes([UBX_SYNC1, UBX_SYNC2]) + body + bytes([ck_a, ck_b])

def utc_epoch(year, month, day, hour, minute, second):
    """Software model of the gateware epoch (seconds since 1970-01-01 UTC)."""
    dt = datetime.datetime(year, month, day, hour, minute, 0, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp()) + second

# UBX NAV-TIMEUTC Decoder --------------------------------------------------------------------------

class UBXTimeUTCDecoder(LiteXModule):
    """Byte-stream UBX-NAV-TIMEUTC decoder.

    sink: 8-bit bytes (valid/data). On a frame with a good checksum, `decoded` pulses (a few
    cycles after the last byte) with `epoch` (UTC seconds, rounded to the nearest second with the
    message's nano field), `utc_valid` (validUTC and in-range fields), `t_acc` (ns) and `nano`.
    `ck_error` pulses on a NAV-TIMEUTC frame with a bad checksum.
    """
    def __init__(self):
        self.sink_valid = Signal()
        self.sink_data  = Signal(8)

        self.decoded    = Signal()
        self.ck_error   = Signal()
        self.epoch      = Signal(40)
        self.utc_valid  = Signal()
        self.t_acc      = Signal(32)
        self.nano       = Signal((32, True))

        # # #

        byte  = self.sink_data
        ck_a  = Signal(8)
        ck_b  = Signal(8)
        index = Signal(5)

        # Captured payload fields.
        p_tacc  = Signal(32)
        p_nano  = Signal(32)
        p_year  = Signal(16)
        p_month = Signal(8)
        p_day   = Signal(8)
        p_hour  = Signal(8)
        p_min   = Signal(8)
        p_sec   = Signal(8)
        p_valid = Signal(8)

        def ck_update():
            return [
                NextValue(ck_a, ck_a + byte),
                NextValue(ck_b, ck_b + ck_a + byte),
            ]

        frame_ok = Signal()

        self.fsm = fsm = FSM(reset_state="SYNC1")
        fsm.act("SYNC1",
            If(self.sink_valid & (byte == UBX_SYNC1),
                NextState("SYNC2"),
            )
        )
        fsm.act("SYNC2",
            If(self.sink_valid,
                If(byte == UBX_SYNC2,
                    NextValue(ck_a, 0),
                    NextValue(ck_b, 0),
                    NextState("CLASS"),
                ).Elif(byte != UBX_SYNC1,
                    NextState("SYNC1"),
                )
            )
        )
        fsm.act("CLASS",
            If(self.sink_valid,
                *ck_update(),
                If(byte == UBX_CLASS_NAV, NextState("ID")).Else(NextState("SYNC1")),
            )
        )
        fsm.act("ID",
            If(self.sink_valid,
                *ck_update(),
                If(byte == UBX_ID_TIMEUTC, NextState("LEN_L")).Else(NextState("SYNC1")),
            )
        )
        fsm.act("LEN_L",
            If(self.sink_valid,
                *ck_update(),
                If(byte == UBX_TIMEUTC_LEN, NextState("LEN_H")).Else(NextState("SYNC1")),
            )
        )
        fsm.act("LEN_H",
            If(self.sink_valid,
                *ck_update(),
                NextValue(index, 0),
                If(byte == 0, NextState("PAYLOAD")).Else(NextState("SYNC1")),
            )
        )
        captures = {
            4: p_tacc[0:8],  5: p_tacc[8:16],  6: p_tacc[16:24],  7: p_tacc[24:32],
            8: p_nano[0:8],  9: p_nano[8:16], 10: p_nano[16:24], 11: p_nano[24:32],
            12: p_year[0:8], 13: p_year[8:16],
            14: p_month, 15: p_day, 16: p_hour, 17: p_min, 18: p_sec, 19: p_valid,
        }
        fsm.act("PAYLOAD",
            If(self.sink_valid,
                *ck_update(),
                Case(index, {i: NextValue(field, byte) for i, field in captures.items()}),
                NextValue(index, index + 1),
                If(index == (UBX_TIMEUTC_LEN - 1), NextState("CK_A")),
            )
        )
        fsm.act("CK_A",
            If(self.sink_valid,
                If(byte == ck_a,
                    NextState("CK_B"),
                ).Else(
                    self.ck_error.eq(1),
                    NextState("SYNC1"),
                )
            )
        )
        fsm.act("CK_B",
            If(self.sink_valid,
                If(byte == ck_b,
                    frame_ok.eq(1),
                ).Else(
                    self.ck_error.eq(1),
                ),
                NextState("SYNC1"),
            )
        )

        # Epoch (pipelined after a good frame) -----------------------------------------------------
        year_offset = Signal(8)
        in_range    = Signal()
        leap        = Signal()
        dby         = Array(C(_days_before_year(y), 16) for y in range(GPS_EPOCH_YEAR_MIN, GPS_EPOCH_YEAR_MAX + 1))
        dbm         = Array(C(d, 9) for d in _DAYS_BEFORE_MONTH + [0]*4)
        self.comb += [
            year_offset.eq(p_year - GPS_EPOCH_YEAR_MIN),
            in_range.eq(
                (p_year >= GPS_EPOCH_YEAR_MIN) & (p_year <= GPS_EPOCH_YEAR_MAX) &
                (p_month >= 1) & (p_month <= 12) &
                (p_day   >= 1) & (p_day   <= 31) &
                (p_hour  < 24) & (p_min   < 60) & (p_sec <= 60)
            ),
            # Gregorian leap years inside 1970..2105: every 4th year except 2100.
            leap.eq((p_year[0:2] == 0) & (p_year != 2100)),
        ]

        # Stage 1: days since 1970, seconds of the day, rounding from nano.
        s1_valid = Signal()
        s1_days  = Signal(17)
        s1_sod   = Signal(17)
        s1_round = Signal((2, True))
        s1_utc   = Signal()
        nano_s   = Signal((32, True))
        self.comb += nano_s.eq(p_nano)
        self.sync += [
            s1_valid.eq(frame_ok),
            If(frame_ok,
                s1_days.eq(dby[year_offset] + dbm[p_month - 1] +
                    ((p_month > 2) & leap) + p_day - 1),
                s1_sod.eq(p_hour*3600 + p_min*60 + p_sec),
                s1_round.eq(Mux(nano_s >= 500_000_000, 1, Mux(nano_s <= -500_000_000, -1, 0))),
                s1_utc.eq(in_range & p_valid[UBX_VALID_UTC_BIT]),
            )
        ]

        # Stage 2: epoch.
        self.sync += [
            self.decoded.eq(s1_valid),
            If(s1_valid,
                self.epoch.eq(s1_days*86400 + s1_sod + s1_round),
                self.utc_valid.eq(s1_utc),
                self.t_acc.eq(p_tacc),
                self.nano.eq(p_nano),
            )
        ]

# PPS Qualifier ------------------------------------------------------------------------------------

class PPSQualifier(LiteXModule):
    """Accept a PPS rising edge only after `filter_cycles` consecutive high samples, then ignore
    the input for `refractory_cycles`. `pulse` is one sys cycle, `filter_cycles + 3` cycles after
    the pin (2-flop synchronizer, filter, pulse register)."""
    def __init__(self, pad, filter_cycles=8, refractory_cycles=int(0.9*125e6)):
        self.pulse    = Signal()
        self.rejected = Signal() # Pulses on a rising edge seen during the refractory window.
        self.latency_cycles = filter_cycles + 3

        # # #

        level    = Signal()
        previous = Signal()
        self.specials += MultiReg(pad, level)
        high_cnt = Signal(max=filter_cycles + 1)
        refr_cnt = Signal(max=refractory_cycles + 1)
        armed    = Signal(reset=1) # Ready for the next edge once the input has gone low.
        self.sync += [
            previous.eq(level),
            self.pulse.eq(0),
            self.rejected.eq(0),
            If(refr_cnt != 0,
                refr_cnt.eq(refr_cnt - 1),
                If(level & ~previous, self.rejected.eq(1)),
            ),
            If(~level,
                high_cnt.eq(0),
                armed.eq(1),
            ).Elif(armed & (refr_cnt == 0),
                If(high_cnt == (filter_cycles - 1),
                    self.pulse.eq(1),
                    armed.eq(0),
                    high_cnt.eq(0),
                    refr_cnt.eq(refractory_cycles),
                ).Else(
                    high_cnt.eq(high_cnt + 1),
                )
            )
        ]

# GPS Time -----------------------------------------------------------------------------------------

class GPSTime(LiteXModule):
    """GPS UART + PPS time input driving a TimeGenerator's PPS sync (see the file header)."""
    def __init__(self, uart_pads, pps_pad, time_gen, sys_clk_freq,
        baudrate          = 115200,
        pps_filter_cycles = 8,
        pps_refractory_s  = 0.9,
        pps_alive_s       = 1.5,
        utc_fresh_s       = 1.5,
        time_clk_freq     = 100e6):
        sys_clk_freq = int(sys_clk_freq)

        # UART -------------------------------------------------------------------------------------
        self.tuning_word = Signal(32)
        self.uart_rx = uart_rx = RS232PHYRX(uart_pads, self.tuning_word)
        self.comb += uart_rx.source.ready.eq(1)
        # FPGA -> GPS RX: idle high (nothing is sent; the F9P is provisioned over USB).
        if hasattr(uart_pads, "tx"):
            self.comb += uart_pads.tx.eq(1)

        # Decoder / PPS ----------------------------------------------------------------------------
        self.decoder = decoder = UBXTimeUTCDecoder()
        self.comb += [
            decoder.sink_valid.eq(uart_rx.source.valid),
            decoder.sink_data.eq(uart_rx.source.data),
        ]
        self.pps = pps = PPSQualifier(pps_pad,
            filter_cycles     = pps_filter_cycles,
            refractory_cycles = int(pps_refractory_s*sys_clk_freq),
        )

        # CSRs -------------------------------------------------------------------------------------
        default_tuning_word = int(round((baudrate/sys_clk_freq)*2**32))
        # Pin -> time write: the qualifier (latency_cycles), the time_sync register and the
        # PulseSynchronizer toggle flop (sys), then two flops + edge register + sync_pulse_time
        # register + the TimeGenerator write (time). About +-1 cycle of sampling in each domain.
        default_sync_offset = int(round((pps.latency_cycles + 2)*1e9/sys_clk_freq + 5*1e9/time_clk_freq))
        self.default_sync_offset = default_sync_offset
        self._control = CSRStorage(fields=[
            CSRField("enable",     size=1, offset=0, reset=1, description="Decode UBX and qualify PPS."),
            CSRField("sync_every", size=1, offset=1, reset=0,
                description="Sync the board time to GPS UTC on every qualified PPS that follows a fresh valid decode."),
            CSRField("sync_once",  size=1, offset=2, pulse=True,
                description="Arm one sync on the next such PPS."),
            CSRField("clear",      size=1, offset=3, pulse=True, description="Clear the counters."),
            CSRField("pps_bypass", size=1, offset=4, reset=0,
                description="Ignore the PPS pin: no PPS is qualified, latched, counted or used to sync."),
        ], description="GPS time control. The TimeGenerator's control.sync_enable must also be set for syncs to apply.")
        self._tuning_word = CSRStorage(32, reset=default_tuning_word,
            description=f"UART tuning word: round(baud / sys_clk_freq * 2**32) ({baudrate} baud at reset).")
        self._sync_offset = CSRStorage(32, reset=default_sync_offset,
            description="ns added to (S + 1) * 1e9 on a sync: the PPS pin -> time write latency.")
        self._sys_clk_freq = CSRStatus(32, reset=sys_clk_freq, description="sys clock (Hz), for the tuning word.")
        self._status = CSRStatus(fields=[
            CSRField("utc_valid", size=1, offset=0, description="Last good frame had validUTC and in-range fields."),
            CSRField("utc_fresh", size=1, offset=1, description="A valid decode within utc_fresh_s."),
            CSRField("pps_alive", size=1, offset=2, description="A qualified PPS within pps_alive_s."),
            CSRField("pending",   size=1, offset=3, description="A valid decode is waiting for the next PPS."),
            CSRField("synced",    size=1, offset=4, description="The last qualified PPS applied a sync."),
            CSRField("once_armed", size=1, offset=5, description="A one-shot sync is armed."),
            CSRField("pps_bypass", size=1, offset=6, description="The PPS pin is being ignored (control.pps_bypass)."),
        ])
        self._epoch     = CSRStatus(64, description="UTC seconds of the last valid NAV-TIMEUTC decode.")
        self._t_acc     = CSRStatus(32, description="tAcc (ns) of the last good frame.")
        self._nano      = CSRStatus(32, description="nano (ns, signed) of the last good frame.")
        self._pps_time  = CSRStatus(64, description="Board time (ns) latched on the last qualified PPS, before any sync.")
        self._sync_epoch = CSRStatus(64, description="Seconds S of the last sync (board time set to (S + 1) * 1e9 + sync_offset).")
        self._msg_count      = CSRStatus(32, description="Good NAV-TIMEUTC frames.")
        self._ck_errors      = CSRStatus(32, description="NAV-TIMEUTC frames with a bad checksum.")
        self._pps_count      = CSRStatus(32, description="Qualified PPS edges.")
        self._pps_rejected   = CSRStatus(32, description="PPS edges ignored inside the refractory window.")
        self._sync_count     = CSRStatus(32, description="Syncs applied.")
        self._pps_no_utc     = CSRStatus(32, description="Qualified PPS edges with no fresh valid decode pending.")

        # # #

        enable = self._control.fields.enable
        clear  = self._control.fields.clear
        # Qualified PPS after the bypass: every consumer below uses these, never pps.pulse.
        pps_bypass   = self._control.fields.pps_bypass
        pps_pulse    = Signal()
        pps_rejected = Signal()
        self.comb += [
            pps_pulse.eq(pps.pulse & ~pps_bypass),
            pps_rejected.eq(pps.rejected & ~pps_bypass),
        ]
        # One-shot arm: the CSR pulse, or this input for other gateware.
        self.sync_once = Signal()
        sync_once = Signal()
        self.comb += sync_once.eq(self._control.fields.sync_once | self.sync_once)
        self.comb += self.tuning_word.eq(self._tuning_word.storage)

        # Ages (saturating counters since the last event).
        pps_alive_cycles = int(pps_alive_s*sys_clk_freq)
        utc_fresh_cycles = int(utc_fresh_s*sys_clk_freq)
        pps_age = Signal(max=pps_alive_cycles + 1, reset=pps_alive_cycles)
        utc_age = Signal(max=utc_fresh_cycles + 1, reset=utc_fresh_cycles)
        self.sync += [
            If(pps_pulse & enable, pps_age.eq(0)).Elif(pps_age != pps_alive_cycles, pps_age.eq(pps_age + 1)),
            If(decoder.decoded & decoder.utc_valid & enable, utc_age.eq(0)).Elif(utc_age != utc_fresh_cycles, utc_age.eq(utc_age + 1)),
        ]

        # Decode bookkeeping.
        epoch     = Signal(40)
        utc_valid = Signal()
        pending   = Signal()
        self.sync += [
            If(decoder.decoded & enable,
                utc_valid.eq(decoder.utc_valid),
                self._t_acc.status.eq(decoder.t_acc),
                self._nano.status.eq(decoder.nano),
                If(decoder.utc_valid,
                    epoch.eq(decoder.epoch),
                ),
            )
        ]

        # PPS: latch, consume the pending decode, sync.
        self.time_sync    = time_sync    = Signal() # sys-domain pulse.
        self.sync_seconds = sync_seconds = Signal(40)
        once_armed = Signal()
        synced     = Signal()
        pps_time   = Signal(64)
        sync_epoch = Signal(40)
        do_sync    = Signal()
        # The seconds bus follows the last valid decode, which lands tens of ms after a PPS and
        # then holds until well past the next one: it is settled in the time domain long before
        # a sync pulse arrives there.
        self.comb += sync_seconds.eq(epoch)
        self.comb += do_sync.eq(enable & pps_pulse & pending &
            (self._control.fields.sync_every | once_armed))
        self.sync += [
            time_sync.eq(do_sync),
            If(sync_once, once_armed.eq(1)),
            If(enable & pps_pulse,
                pps_time.eq(time_gen.time),
                synced.eq(do_sync),
                If(do_sync,
                    sync_epoch.eq(epoch),
                    once_armed.eq(0),
                ),
            ),
            # A valid decode arms the next PPS; that PPS consumes it (synced or not). A decode on the
            # same cycle as the PPS belongs to the second that PPS starts, so it stays pending.
            If(decoder.decoded & enable & decoder.utc_valid,
                pending.eq(1),
            ).Elif(pps_pulse & enable,
                pending.eq(0),
            ),
        ]

        # Counters.
        def counter(csr, event):
            self.sync += If(clear, csr.status.eq(0)).Elif(event, csr.status.eq(csr.status + 1))
        counter(self._msg_count,    enable & decoder.decoded)
        counter(self._ck_errors,    enable & decoder.ck_error)
        counter(self._pps_count,    enable & pps_pulse)
        counter(self._pps_rejected, enable & pps_rejected)
        counter(self._sync_count,   do_sync)
        counter(self._pps_no_utc,   enable & pps_pulse & ~pending)

        self.comb += [
            self._status.fields.utc_valid.eq(utc_valid),
            self._status.fields.utc_fresh.eq(utc_age != utc_fresh_cycles),
            self._status.fields.pps_alive.eq(pps_age != pps_alive_cycles),
            self._status.fields.pending.eq(pending),
            self._status.fields.synced.eq(synced),
            self._status.fields.once_armed.eq(once_armed),
            self._status.fields.pps_bypass.eq(pps_bypass),
            self._epoch.status.eq(epoch),
            self._pps_time.status.eq(pps_time),
            self._sync_epoch.status.eq(sync_epoch),
        ]

        # TimeGenerator (time domain) --------------------------------------------------------------
        # sync_seconds / sync_offset are quasi-static (see above / CSR), so plain MultiRegs carry
        # them; the pulse crosses with a PulseSynchronizer.
        self.time_sync_ps = time_sync_ps = PulseSynchronizer("sys", "time")
        self.comb += time_sync_ps.i.eq(time_sync)
        seconds_time = Signal(40)
        offset_time  = Signal(32)
        sync_pulse_time = Signal()
        self.specials += [
            MultiReg(sync_seconds, seconds_time, odomain="time"),
            MultiReg(self._sync_offset.storage, offset_time, odomain="time"),
        ]
        self.sync.time += sync_pulse_time.eq(time_sync_ps.o)
        self.comb += [
            time_gen.time_sync.eq(sync_pulse_time),
            time_gen.time_seconds.eq(seconds_time),
            time_gen.time_sync_offset.eq(offset_time),
        ]
