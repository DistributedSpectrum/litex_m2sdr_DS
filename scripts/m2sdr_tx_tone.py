#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Transmit a CW tone through the raw SoapySDR API.

The simplest path to a known signal on TX1: no GNU Radio, no flowgraph, just
writeStream() in a loop. Useful as a reference when something further up the
stack misbehaves, and for parking a carrier while measuring on a spectrum
analyzer.

Set --tone 0 to emit the bare LO (a DC baseband puts the carrier exactly on the
LO), which is how you measure LO accuracy without a sample-rate term in the way.
"""

import sys
import time
import argparse

import numpy as np

import SoapySDR
from SoapySDR import SOAPY_SDR_TX, SOAPY_SDR_CF32

# Device -------------------------------------------------------------------------------------------

def open_device():
    """Open the LiteX-M2SDR.

    Device(dict(driver="LiteXM2SDR")) raises "make() no match" even though
    enumerate() reports exactly that driver, so hand make() the enumerate entry
    verbatim. enumerate() yields SoapySDRKwargs, not dicts, hence the dict().
    """
    devices = SoapySDR.Device.enumerate()
    matches = [d for d in devices if dict(d).get("driver") == "LiteXM2SDR"]
    if not matches:
        sys.exit("no LiteX-M2SDR found (checked %d Soapy device(s))" % len(devices))
    return SoapySDR.Device(matches[0])

# Tone ---------------------------------------------------------------------------------------------

def make_tone(rate, tone, amplitude, n=65536):
    """Build a buffer holding a whole number of cycles so it loops seamlessly.

    tone == 0 gives a DC buffer, which upconverts to the bare LO -- that is the
    point of --tone 0, so it must not be rounded up to one cycle.
    """
    cycles = round(tone * n / rate)
    if tone != 0 and cycles == 0:
        cycles = 1                  # too small to represent in one buffer; nearest usable
    buf = (amplitude * np.exp(2j * np.pi * cycles * np.arange(n) / n)).astype(np.complex64)
    return buf, cycles * rate / n

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Transmit a CW tone on TX1 via the raw SoapySDR API.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--freq",      default=1e9,      type=float, help="TX LO frequency (Hz).")
    parser.add_argument("--tone",      default=1e6,      type=float, help="Baseband tone offset (Hz); 0 emits the bare LO.")
    parser.add_argument("--rate",      default=30.72e6,  type=float, help="Sample rate (SPS).")
    parser.add_argument("--bandwidth", default=20e6,     type=float, help="TX filter bandwidth (Hz).")
    parser.add_argument("--att",       default=50.0,     type=float,
                        help="TX ATTENUATION in dB, 0..89. This is attenuation, not gain: "
                             "0 is FULL POWER (~+8 dBm) and 89 is quietest.")
    parser.add_argument("--channel",   default=0,        type=int,   choices=[0, 1], help="0 = TX1, 1 = TX2.")
    parser.add_argument("--secs",      default=20.0,     type=float, help="Duration (s); 0 runs until Ctrl-C.")
    parser.add_argument("--amplitude", default=0.5,      type=float, help="Baseband amplitude, 0..1.")
    args = parser.parse_args()

    if not 0.0 <= args.att <= 89.0:
        sys.exit("--att is attenuation in dB and must be 0..89 (0 = full power)")

    sdr = open_device()
    ch  = args.channel
    sdr.setSampleRate(SOAPY_SDR_TX, ch, args.rate)
    sdr.setFrequency (SOAPY_SDR_TX, ch, "RF", args.freq)
    sdr.setBandwidth (SOAPY_SDR_TX, ch, args.bandwidth)
    sdr.setAntenna   (SOAPY_SDR_TX, ch, "A")
    sdr.setGain      (SOAPY_SDR_TX, ch, args.att)   # attenuation on this driver

    print("rate      : %.3f MSPS"  % (sdr.getSampleRate(SOAPY_SDR_TX, ch) / 1e6))
    print("LO        : %.3f MHz"   % (sdr.getFrequency (SOAPY_SDR_TX, ch) / 1e6))
    print("att       : %.1f dB"    % sdr.getGain(SOAPY_SDR_TX, ch))
    try:
        print("lo_locked : %s" % sdr.readSensor(SOAPY_SDR_TX, ch, "lo_locked"))
    except Exception as e:
        print("lo_locked : unavailable (%s)" % e)

    buf, actual_tone = make_tone(args.rate, args.tone, args.amplitude)
    print("tone      : %.6f MHz requested, %.6f MHz emitted (whole cycles per buffer)"
          % (args.tone / 1e6, actual_tone / 1e6))
    print("EXPECT CARRIER AT %.6f MHz" % ((args.freq + actual_tone) / 1e6))

    stream = sdr.setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32, [ch])
    sdr.activateStream(stream)
    t0, sent, errors = time.time(), 0, 0
    try:
        while args.secs == 0 or (time.time() - t0) < args.secs:
            r = sdr.writeStream(stream, [buf], len(buf), timeoutUs=1000000)
            if r.ret > 0:
                sent += r.ret
            else:
                errors += 1
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        elapsed = time.time() - t0
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)
    print("transmitted %d samples (%.1f s of signal in %.1f s wall), %d write errors"
          % (sent, sent / args.rate, elapsed, errors))

if __name__ == "__main__":
    main()
