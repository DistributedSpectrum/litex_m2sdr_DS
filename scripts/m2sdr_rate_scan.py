#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Sweep the requested sample rate and measure the resulting AD9361 DATA_CLK.

DATA_CLK should come out at 2x the sample rate in 1T1R and 4x in 2T2R (LVDS DDR,
one edge pair per channel). Comparing measured against expected across a range of
rates separates the two failure modes:

  - a ratio that stays CONSTANT across rates  -> a pure reference-frequency
    scale error; the divider chain is being selected correctly
  - a ratio that VARIES with rate             -> the driver is picking the wrong
    dividers or filters for some rates

Uses the m2sdr_rf / m2sdr_util binaries, so no litex_server is required.
See also `m2sdr_util clk-test` for a one-shot read of every clock, and
doc/clock-investigation.md for what this measured on real hardware.
"""

import os
import re
import sys
import shutil
import argparse
import subprocess

# Defaults -----------------------------------------------------------------------------------------

DEFAULT_RATES = [3.84e6, 7.68e6, 15.36e6, 30.72e6, 61.44e6]

# Helpers ------------------------------------------------------------------------------------------

def find_tool(name, explicit_dir):
    """Locate a m2sdr_* binary: explicit dir, then PATH, then the in-tree build."""
    if explicit_dir:
        p = os.path.join(explicit_dir, name)
        if os.path.exists(p):
            return p
    found = shutil.which(name)
    if found:
        return found
    here = os.path.dirname(os.path.abspath(__file__))
    p    = os.path.join(here, "..", "litex_m2sdr", "software", "user", name)
    if os.path.exists(p):
        return os.path.abspath(p)
    sys.exit("could not find %s (use --bin-dir)" % name)

def measure_data_clk(util, retries=2):
    """Return the AD9361 Dat Clk in MHz, parsed from `m2sdr_util clk-test`."""
    for _ in range(retries + 1):
        out = subprocess.run([util, "clk-test", "2", "2"], capture_output=True, text=True).stdout
        header, values = None, None
        for line in out.splitlines():
            if "Dat Clk" in line:
                header = re.sub(r"\x1b\[[0-9;]*m", "", line)
            elif header and re.match(r"^\s*\d+\s", line):
                values = line.split()
        if header and values:
            # Header columns are multi-word, so split on runs of spaces. Its first
            # field is "Meas.", which lines up with the row's measurement number,
            # so header index maps straight onto value index.
            names = [c.strip() for c in re.split(r"\s{2,}", header.strip()) if c.strip()]
            try:
                col = next(i for i, n in enumerate(names) if "Dat Clk" in n)
                return float(values[col])
            except (StopIteration, IndexError, ValueError):
                pass
    return float("nan")

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Sweep sample rates and measure AD9361 DATA_CLK.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--rates",    default=",".join("%g" % r for r in DEFAULT_RATES),
                        help="Comma-separated sample rates in SPS.")
    parser.add_argument("--layout",   default="1t1r", choices=["1t1r", "2t2r"], help="Channel layout.")
    parser.add_argument("--freq",     default=1e9,  type=float, help="LO for both TX and RX (Hz).")
    parser.add_argument("--att",      default=60.0, type=float, help="TX attenuation in dB (60 = quiet).")
    parser.add_argument("--bin-dir",  default=None, help="Directory holding m2sdr_rf / m2sdr_util.")
    args = parser.parse_args()

    rf, util = find_tool("m2sdr_rf", args.bin_dir), find_tool("m2sdr_util", args.bin_dir)
    rates    = [float(r) for r in args.rates.split(",")]

    # The interface delay calibration re-runs on every rate change and is slow;
    # it is not what we are measuring here.
    env = dict(os.environ, M2SDR_AUTO_DELAY_CAL="0")

    expect = 2 if args.layout == "1t1r" else 4
    print("layout %s, LO %.3f MHz -- DATA_CLK should be %dx the sample rate"
          % (args.layout, args.freq / 1e6, expect))
    print("%12s %14s %12s %10s" % ("requested", "DATA_CLK MHz", "DATA_CLK/Fs", "vs %dxFs" % expect))
    ratios = []
    for rate in rates:
        subprocess.run([rf, "--sample-rate=%d" % int(rate), "--channel-layout=%s" % args.layout,
                        "--bandwidth=5000000", "--tx-freq=%d" % int(args.freq),
                        "--rx-freq=%d" % int(args.freq), "--tx-att=%d" % int(args.att),
                        "--loopback=0"],
                       capture_output=True, text=True, env=env)
        clk = measure_data_clk(util)
        fs  = rate / 1e6
        if clk != clk:                       # NaN
            print("%11.3fM %14s" % (fs, "measure failed"))
            continue
        ratios.append(clk / (expect * fs))
        print("%11.3fM %14.3f %12.4f %10.4f" % (fs, clk, clk / fs, clk / (expect * fs)))

    if ratios and max(ratios) == 0.0:
        print("\nEvery measurement read 0 MHz. DATA_CLK is only present once the RFIC is"
              "\nclocked and configured -- check `m2sdr_util clk-test` directly, and that"
              "\nm2sdr_rf actually applied the rate.")
    elif len(ratios) >= 2:
        spread = max(ratios) - min(ratios)
        print("\nratio vs %dxFs: min %.4f max %.4f spread %.4f"
              % (expect, min(ratios), max(ratios), spread))
        if spread < 0.01:
            print("=> CONSTANT across rates: divider selection is consistent; a ratio far from "
                  "1.0 points at the reference frequency, not the chain.")
        else:
            print("=> VARIES with rate: the clock chain is being selected differently per rate.")

if __name__ == "__main__":
    main()
