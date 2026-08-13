#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Analyse a CS16 IQ capture: peaks, tone search, A/B difference, duplication.

Subcommands:
  peaks   list the strongest peaks with SNR above the noise median
  tone    find the strongest tone and, given the expected offset, report the
          implied sample-rate scale factor
  diff    compare two captures and report what APPEARED between them -- the way
          to tell a real transmission from ambient RF, which does not move
  dup     check whether samples are duplicated in the stream (an interleaving or
          channel-layout fault shows up as sample[n] == sample[n+1])

Frequencies are baseband offsets from the LO unless --lo is given.
"""

import sys
import argparse

import numpy as np

# Loading ------------------------------------------------------------------------------------------

def load_iq(path, max_samples=1 << 21, middle=True):
    """Load interleaved int16 as complex64, taking a power-of-two chunk."""
    raw = np.fromfile(path, dtype=np.int16)
    n   = (len(raw) // 2) * 2
    iq  = raw[:n:2].astype(np.float32) + 1j * raw[1:n:2].astype(np.float32)
    if len(iq) == 0:
        sys.exit("%s: no samples" % path)
    if len(iq) > max_samples:
        # skip startup transients by taking from the middle
        start = (len(iq) - max_samples) // 2 if middle else 0
        iq    = iq[start:start + max_samples]
    else:
        size = 1 << int(np.floor(np.log2(len(iq))))
        iq   = iq[:size]
    return iq

def spectrum(iq, rate):
    """Return (freqs, dB) for a Hann-windowed FFT."""
    window = np.hanning(len(iq))
    mag    = np.fft.fftshift(np.abs(np.fft.fft(iq * window)))
    freqs  = np.fft.fftshift(np.fft.fftfreq(len(iq), 1 / rate))
    return freqs, 20 * np.log10(mag + 1e-9)

def fmt_freq(f, lo):
    return "%+10.4f MHz" % (f / 1e6) if lo is None else "%10.4f MHz" % ((lo + f) / 1e6)

# Subcommands --------------------------------------------------------------------------------------

def cmd_peaks(args):
    iq            = load_iq(args.file)
    freqs, db     = spectrum(iq, args.rate)
    median        = np.median(db)
    usable        = (np.abs(freqs) > args.dc_mask) & (np.abs(freqs) < args.edge)
    work          = np.where(usable, db, -1e9)
    guard         = max(1, int(0.25e6 / (args.rate / len(iq))))

    print("%s: %d samples, noise median %.1f dB" % (args.file, len(iq), median))
    found = 0
    while found < args.count:
        k = int(np.argmax(work))
        if db[k] - median < args.snr:
            break
        print("   %s  snr %5.1f dB" % (fmt_freq(freqs[k], args.lo), db[k] - median))
        work[max(0, k - guard):k + guard] = -1e9
        found += 1
    if found == 0:
        print("   no peak above %.0f dB SNR" % args.snr)

def cmd_tone(args):
    iq        = load_iq(args.file)
    freqs, db = spectrum(iq, args.rate)
    median    = np.median(db)
    usable    = np.abs(freqs) > args.dc_mask
    if args.near is not None:
        usable &= np.abs(freqs - args.near) < args.window
    k = int(np.argmax(np.where(usable, db, -1e9)))

    print("%s: %d samples, rms %.1f" % (args.file, len(iq), np.abs(iq).std()))
    print("peak at %s   SNR %.1f dB" % (fmt_freq(freqs[k], args.lo), db[k] - median))

    if args.expect is not None:
        print("expected %+.6f MHz" % (args.expect / 1e6))
        if abs(freqs[k]) > 1e3:
            # analysing at the nominal rate, a tone at true offset f appears at
            # f * (Fs_nominal / Fs_actual)
            ratio = args.expect / freqs[k]
            print("=> Fs_actual / Fs_nominal = %.4f  (actual %.3f MSPS vs nominal %.3f)"
                  % (ratio, args.rate * ratio / 1e6, args.rate / 1e6))
            if abs(ratio - 1) < 0.002:
                print("=> within 0.2% of nominal")

def cmd_diff(args):
    a, b   = load_iq(args.on), load_iq(args.off)
    n      = min(len(a), len(b))
    fa, da = spectrum(a[:n], args.rate)
    _,  db_= spectrum(b[:n], args.rate)
    delta  = da - db_
    usable = np.abs(fa) > args.dc_mask

    idx = np.argsort(np.where(usable, delta, -1e9))[::-1][:args.count]
    print("what appeared in %s that is not in %s:" % (args.on, args.off))
    for k in idx:
        print("   %s  %+6.1f dB  (on %.1f / off %.1f)" % (fmt_freq(fa[k], args.lo), delta[k], da[k], db_[k]))
    print("NOTE: several movers of similar size usually means ambient RF varying "
          "between captures, not your signal. A real transmission is one peak that "
          "moves when you change the tone offset.")

def cmd_dup(args):
    raw = np.fromfile(args.file, dtype=np.int16)
    n   = (len(raw) // 2) * 2
    iq  = (raw[:n:2].astype(np.int32) + 1j * raw[1:n:2].astype(np.int32))[:args.count]
    if len(iq) < 8:
        sys.exit("not enough samples")
    print("%s: first 8 IQ pairs: %s" % (args.file,
          ", ".join("(%d,%d)" % (int(v.real), int(v.imag)) for v in iq[:8])))
    for stride in (1, 2, 3, 4):
        frac = np.mean(iq[:-stride] == iq[stride:])
        print("   sample[n] == sample[n+%d] : %6.2f %%" % (stride, 100 * frac))
    pairs = np.mean(iq[0::2][:len(iq) // 2] == iq[1::2][:len(iq) // 2])
    print("   sample[2k] == sample[2k+1] : %6.2f %%   <-- 2x duplication test" % (100 * pairs))
    print("A healthy real-RX capture sits near 0 % on all of these; anything near "
          "50/100 % means samples are being repeated.")

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Analyse CS16 IQ captures from the LiteX-M2SDR.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--rate",    default=30.72e6, type=float, help="Sample rate the capture was taken at (SPS).")
    parser.add_argument("--lo",      default=None,    type=float, help="LO (Hz); report absolute instead of baseband.")
    parser.add_argument("--dc-mask", default=150e3,   type=float, help="Ignore +/- this around DC (LO leakage).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("peaks", help="list the strongest peaks")
    p.add_argument("file")
    p.add_argument("--count", default=8,    type=int,   help="Maximum peaks to list.")
    p.add_argument("--snr",   default=15.0, type=float, help="Minimum SNR over the noise median (dB).")
    p.add_argument("--edge",  default=1e12, type=float, help="Ignore beyond +/- this offset (band-edge rolloff).")
    p.set_defaults(func=cmd_peaks)

    p = sub.add_parser("tone", help="find a tone and derive the rate scale factor")
    p.add_argument("file")
    p.add_argument("--expect", default=None, type=float, help="Expected baseband offset (Hz).")
    p.add_argument("--near",   default=None, type=float, help="Search only near this offset (Hz).")
    p.add_argument("--window", default=1e6,  type=float, help="Half-width for --near (Hz).")
    p.set_defaults(func=cmd_tone)

    p = sub.add_parser("diff", help="what appeared between two captures")
    p.add_argument("on")
    p.add_argument("off")
    p.add_argument("--count", default=5, type=int, help="How many movers to list.")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("dup", help="check for duplicated samples")
    p.add_argument("file")
    p.add_argument("--count", default=400000, type=int, help="Samples to examine.")
    p.set_defaults(func=cmd_dup)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
