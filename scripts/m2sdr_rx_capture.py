#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Capture RX samples to a file through the raw SoapySDR API.

Writes interleaved int16 (CS16), the format the rest of the tooling expects --
feed the result to m2sdr_spectrum.py. For sustained high-rate capture prefer the
native m2sdr_record utility; this exists to verify the RX path end to end from
the same layer the TX tools use.
"""

import sys
import time
import argparse

import numpy as np

import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16

# Device -------------------------------------------------------------------------------------------

def open_device():
    """Open the LiteX-M2SDR (see m2sdr_tx_tone.py for why enumerate() is used)."""
    devices = SoapySDR.Device.enumerate()
    matches = [d for d in devices if dict(d).get("driver") == "LiteXM2SDR"]
    if not matches:
        sys.exit("no LiteX-M2SDR found (checked %d Soapy device(s))" % len(devices))
    return SoapySDR.Device(matches[0])

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Capture RX to an interleaved int16 (CS16) file.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("output",                            help="Output file path.")
    parser.add_argument("--freq",      default=800e6,   type=float, help="RX LO frequency (Hz).")
    parser.add_argument("--rate",      default=30.72e6, type=float, help="Sample rate (SPS).")
    parser.add_argument("--bandwidth", default=20e6,    type=float, help="RX filter bandwidth (Hz).")
    parser.add_argument("--gain",      default=40.0,    type=float, help="RX gain in dB (real gain, unlike TX).")
    parser.add_argument("--channel",   default=0,       type=int, choices=[0, 1], help="0 = RX1, 1 = RX2.")
    parser.add_argument("--samples",   default=4 << 20, type=int, help="Number of complex samples to capture.")
    args = parser.parse_args()

    sdr = open_device()
    ch  = args.channel
    sdr.setSampleRate(SOAPY_SDR_RX, ch, args.rate)
    sdr.setFrequency (SOAPY_SDR_RX, ch, "RF", args.freq)
    sdr.setBandwidth (SOAPY_SDR_RX, ch, args.bandwidth)
    sdr.setGain      (SOAPY_SDR_RX, ch, args.gain)

    print("rate    : %.3f MSPS" % (sdr.getSampleRate(SOAPY_SDR_RX, ch) / 1e6))
    print("LO      : %.3f MHz"  % (sdr.getFrequency (SOAPY_SDR_RX, ch) / 1e6))
    print("gain    : %.1f dB"   % sdr.getGain(SOAPY_SDR_RX, ch))
    try:
        print("lo_locked: %s" % sdr.readSensor(SOAPY_SDR_RX, ch, "lo_locked"))
    except Exception:
        pass

    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [ch])
    mtu    = sdr.getStreamMTU(stream)
    sdr.activateStream(stream)

    buf, got, overflows = np.empty(2 * mtu, dtype=np.int16), 0, 0
    t0 = time.time()
    with open(args.output, "wb", buffering=1 << 24) as f:
        while got < args.samples:
            r = sdr.readStream(stream, [buf], mtu, timeoutUs=1000000)
            if r.ret > 0:
                f.write(buf[:2 * r.ret].tobytes())
                got += r.ret
            elif r.ret == -4:      # SOAPY_SDR_OVERFLOW
                overflows += 1
            elif r.ret < 0:
                print("readStream error %d, stopping" % r.ret)
                break
    dt = time.time() - t0
    sdr.deactivateStream(stream)
    sdr.closeStream(stream)

    print("captured %d samples (%.2f MiB) in %.2f s, %d overflows"
          % (got, got * 4 / (1 << 20), dt, overflows))
    if dt > 2.0:
        print("sustained %.3f MSPS" % (got / dt / 1e6))
    else:
        print("(too short to time meaningfully -- a brief capture mostly drains buffers "
              "already filled by the driver; use --samples for a longer run to measure rate)")
    print("wrote %s -- analyse with: scripts/m2sdr_spectrum.py peaks %s --rate %g"
          % (args.output, args.output, args.rate))

if __name__ == "__main__":
    main()
