#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Transmit through GNU Radio (gr-soapy), from a tone or an IQ file.

Streams an IQ file straight from disk to the SDR and reports what throughput was
actually achieved, which is the useful measurement when the file lives on fast
storage and you want to know whether the host can keep up.

Also generates the test file (--make-file), sized so it loops seamlessly.

Note on hosts where the NVMe and the SDR sit behind the same PCIe switch: both
directions share that uplink. On a Pi 5 dual-M.2 HAT the switch upstream is x1
Gen3 (~984 MB/s for reads plus writes combined), so a 61.44 MSPS 2T2R stream
fed from the same drive is close to the ceiling.
"""

import os
import sys
import json
import time
import argparse

import numpy as np

# IQ file generation -------------------------------------------------------------------------------

def make_file(path, size_gb, fmt, rate, tone, amplitude):
    """Write a test IQ file containing a whole number of tone cycles per block."""
    total      = int(size_gb * (1 << 30))
    block      = 1 << 22
    cycles     = max(1, round(tone * block / rate))
    phase      = 2 * np.pi * cycles * np.arange(block) / block
    if fmt == "cs16":
        data           = np.empty(2 * block, dtype=np.int16)
        data[0::2]     = np.round(amplitude * 32767 * np.cos(phase))
        data[1::2]     = np.round(amplitude * 32767 * np.sin(phase))
    else:
        data = (amplitude * np.exp(1j * phase)).astype(np.complex64)

    print("writing %s: %.2f GiB %s, tone %.6f MHz" % (path, size_gb, fmt, cycles * rate / block / 1e6))
    t0, written = time.time(), 0
    with open(path, "wb", buffering=1 << 24) as f:
        while written < total:
            f.write(data.tobytes())
            written += data.nbytes
        f.flush()
        os.fsync(f.fileno())
    dt = time.time() - t0
    print("  wrote %.2f GiB in %.1f s = %.0f MB/s" % (written / (1 << 30), dt, written / dt / 1e6))


# SigMF ---------------------------------------------------------------------------------------------

# SigMF core:datatype -> our --format. Only the complex types the SDR can send.
SIGMF_DATATYPES = {
    "cf32_le": "cf32", "cf32": "cf32",
    "ci16_le": "cs16", "ci16": "cs16",
}

def read_sigmf(path):
    """Return the .sigmf-meta global block for a data file, or None.

    SigMF pairs <base>.sigmf-data with <base>.sigmf-meta. Reading it removes a
    whole class of mistake: passing --format cs16 for a cf32 recording produces
    a plausible-looking but completely wrong signal rather than an error.
    """
    meta = path[:-len(".sigmf-data")] + ".sigmf-meta" if path.endswith(".sigmf-data") else path + ".sigmf-meta"
    if not os.path.exists(meta):
        return None
    try:
        with open(meta) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print("warning: could not read %s (%s)" % (meta, e))
        return None
    g = doc.get("global", {})
    caps = doc.get("captures") or [{}]
    return {
        "path":      meta,
        "datatype":  g.get("core:datatype"),
        "rate":      g.get("core:sample_rate"),
        "freq":      caps[0].get("core:frequency"),
        "desc":      g.get("core:description"),
    }

# Flowgraph ----------------------------------------------------------------------------------------

def build_flowgraph(args):
    from gnuradio import gr, blocks, analog, soapy

    class TxFlow(gr.top_block):
        def __init__(self):
            gr.top_block.__init__(self, "m2sdr-tx")
            n = args.channels
            # gr-soapy IO types are lowercase fc32/sc16/sc8; "CF32"/"CS16" are rejected.
            # The sink is always fc32: the driver's native format, and gr-soapy's sc16
            # path has its own issues. CS16 files are converted in-flowgraph instead,
            # which also halves the disk read for a given sample rate.
            self.sink = soapy.sink("driver=LiteXM2SDR", "fc32", n, "", "", [""] * n, [""] * n)
            for ch in range(n):
                self.sink.set_sample_rate(ch, args.rate)
                self.sink.set_frequency  (ch, args.freq)
                self.sink.set_bandwidth  (ch, args.bandwidth)
                self.sink.set_antenna    (ch, "A")
                self.sink.set_gain       (ch, args.att)   # attenuation on this driver

            if args.file:
                self.nbytes = os.path.getsize(args.file)
                if args.format == "cs16":
                    src = blocks.file_source(gr.sizeof_short, args.file, args.repeat)
                    cvt = blocks.interleaved_short_to_complex()
                    scl = blocks.multiply_const_cc(1.0 / 32768.0)
                    self.connect(src, cvt, scl)
                    self._keep, tail = (src, cvt, scl), scl
                else:
                    tail = blocks.file_source(gr.sizeof_gr_complex, args.file, args.repeat)
                    src, self._keep = tail, (tail,)
                # GNU Radio's default buffer is small enough that at high rates it
                # drains in well under a millisecond, so any scheduling jitter
                # starves the sink and shows up as a stream of 'U's. Deepen it.
                if args.buffer_mb > 0:
                    src.set_min_output_buffer(int(args.buffer_mb * (1 << 20)))
                for ch in range(n):
                    self.connect(tail, (self.sink, ch))     # fan the file out to every channel
            else:
                self.nbytes = None
                self._keep = []
                for ch in range(n):
                    src = analog.sig_source_c(args.rate, analog.GR_COS_WAVE, args.tone, args.amplitude)
                    self._keep.append(src)
                    self.connect(src, (self.sink, ch))

    return TxFlow()

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "TX via gr-soapy, from a tone or an IQ file, with a throughput report.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--file",                            help="IQ file to stream. Omit for an internal tone.")
    parser.add_argument("--make-file",                       help="Generate a test IQ file at this path and exit.")
    parser.add_argument("--size-gb",   default=4.0,  type=float, help="Size for --make-file (GiB).")
    parser.add_argument("--format",    default=None, choices=["cs16", "cf32"],
                        help="IQ file sample format. Default: from a SigMF .sigmf-meta sidecar "
                             "when present, otherwise cs16.")
    parser.add_argument("--channels",  default=1,    type=int, choices=[1, 2], help="1 = 1T1R, 2 = 2T2R.")
    parser.add_argument("--freq",      default=1e9,  type=float, help="TX LO frequency (Hz).")
    parser.add_argument("--tone",      default=1e6,  type=float, help="Tone offset (Hz) when not streaming a file.")
    parser.add_argument("--rate",      default=None, type=float,
                        help="Sample rate (SPS). Default: from the SigMF sidecar when present, "
                             "otherwise 30.72e6.")
    parser.add_argument("--bandwidth", default=20e6, type=float, help="TX filter bandwidth (Hz).")
    parser.add_argument("--att",       default=50.0, type=float,
                        help="TX ATTENUATION in dB, 0..89. Attenuation, not gain: 0 is FULL POWER.")
    parser.add_argument("--secs",      default=20.0, type=float,
                        help="How long to transmit in tone or --repeat mode. NOTE this bounds\n"
                             "--repeat: the file loops, but the run still stops after --secs.\n"
                             "Use 0 to transmit until Ctrl-C. Ignored when playing a file once.")
    parser.add_argument("--repeat",    action="store_true", help="Loop the file instead of playing it once.")
    parser.add_argument("--amplitude", default=0.5,  type=float, help="Amplitude, 0..1.")
    parser.add_argument("--buffer-mb",  default=16.0, type=float,
                        help="Deepen the GNU Radio source buffer (MiB). Raise this if you see "
                             "a stream of 'U' underflow markers at high rates; 0 disables.")
    args = parser.parse_args()

    # Resolve --format / --rate: an explicit flag wins, then a SigMF sidecar,
    # then the historical defaults.
    meta = read_sigmf(args.file) if args.file else None
    if meta:
        print("SigMF sidecar: %s" % meta["path"])
        if meta["desc"]:
            print("  description : %s" % meta["desc"])
        if meta["datatype"]:
            mapped = SIGMF_DATATYPES.get(meta["datatype"])
            if mapped is None:
                sys.exit("SigMF core:datatype %r is not a complex type this tool can transmit "
                         "(known: %s)" % (meta["datatype"], ", ".join(sorted(SIGMF_DATATYPES))))
            note = "" if args.format is None else "   (overridden by --format %s)" % args.format
            print("  datatype    : %s -> %s%s" % (meta["datatype"], mapped, note))
            if args.format is None:
                args.format = mapped
        if meta["rate"]:
            note = "" if args.rate is None else "   (overridden by --rate)"
            print("  sample_rate : %.6f MSPS%s" % (meta["rate"] / 1e6, note))
            if args.rate is None:
                args.rate = float(meta["rate"])
        if meta["freq"] is not None:
            print("  recorded at : %.6f MHz" % (meta["freq"] / 1e6))
            if abs(meta["freq"] - args.freq) > 1e3:
                print("  NOTE: transmitting at %.6f MHz, %.3f MHz from where this was recorded."
                      % (args.freq / 1e6, (args.freq - meta["freq"]) / 1e6))
    elif args.file and args.file.endswith(".sigmf-data") and (args.format is None or args.rate is None):
        # A SigMF recording whose sidecar did not come along. Falling back to the
        # generic defaults here is dangerous: sending a cf32 recording through the
        # cs16 path produces a plausible-looking but completely wrong signal.
        print("WARNING: %s has no .sigmf-meta sidecar next to it." % args.file)
        print("         Falling back to --format cs16 --rate 30.72e6, which is very likely")
        print("         wrong for this file. Copy the sidecar, or pass --format/--rate.")
    if args.format is None:
        args.format = "cs16"
    if args.rate is None:
        args.rate = 30.72e6

    if args.make_file:
        make_file(args.make_file, args.size_gb, args.format, args.rate, args.tone, args.amplitude)
        return
    if not 0.0 <= args.att <= 89.0:
        sys.exit("--att is attenuation in dB and must be 0..89 (0 = full power)")

    tb  = build_flowgraph(args)
    bps = 4 if args.format == "cs16" else 8

    print("%dch  rate %.3f MSPS  LO %.3f MHz  att %.1f dB"
          % (args.channels, args.rate / 1e6, args.freq / 1e6, args.att))
    print("PCIe write ~%.1f MB/s   disk read ~%.1f MB/s"
          % (args.rate * 4 * args.channels / 1e6, (args.rate * bps / 1e6) if args.file else 0.0))
    if args.file:
        print("file %s  %.2f GiB  repeat=%s" % (args.file, tb.nbytes / (1 << 30), args.repeat))

    t0, completed = time.time(), False
    tb.start()
    try:
        if args.file and not args.repeat:
            tb.wait()
            completed = True
        else:
            # --secs 0 means run until Ctrl-C, matching m2sdr_tx_tone.py.
            if args.secs == 0:
                print("transmitting until Ctrl-C ...")
                while True:
                    time.sleep(0.5)
            time.sleep(args.secs)
            tb.stop()
            tb.wait()
    except KeyboardInterrupt:
        print("\ninterrupted")
        tb.stop()
        tb.wait()
    dt = time.time() - t0

    if args.file and not args.repeat and not completed:
        print("stopped after %.2f s before the file finished -- no throughput figure, "
              "the amount actually streamed is not known." % dt)
    elif args.file and not args.repeat:
        achieved = tb.nbytes / bps / dt
        print("streamed %.2f GiB in %.2f s" % (tb.nbytes / (1 << 30), dt))
        print("ACHIEVED  disk %.1f MB/s | %.3f MSPS | PCIe %.1f MB/s"
              % (tb.nbytes / dt / 1e6, achieved / 1e6, achieved * 4 * args.channels / 1e6))
        print("= %.1f%% of the requested real-time rate" % (100 * achieved / args.rate))
        print("(GNU Radio prints 'U' on underflow; a shortfall with U's means the host "
              "could not feed the SDR, not that the radio ran slow)")
    else:
        print("ran %.1f s" % dt)

if __name__ == "__main__":
    main()
