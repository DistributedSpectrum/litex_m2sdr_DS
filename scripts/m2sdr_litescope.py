#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Capture from a LiteScope analyzer built into the gateware.

litescope_cli does not work against this build: its export_csv() emits 5-field
`enum,` rows that its own get_signals() cannot parse, and the RemoteClient import
path differs between litex releases. This drives the analyzer CSRs directly and
decodes the capture from the same CSV, so it works regardless.

Two things it gets right that are easy to get wrong by hand:

  - After disabling the trigger it polls trigger_done TWICE before loading terms.
    The gateware flushes stale terms on the falling edge of enable; write the new
    terms too early and they are flushed away, leaving an empty term memory whose
    hit output is a constant level. The capture then fires instantly and silently
    records idle -- which looks exactly like a successful capture of nothing.
  - Storage arms on the RISING edge of storage_enable, so it is cleared first.

A single-cycle strobe is missed by storage when subsampling > 1 even though the
trigger still sees it; capture at subsampling 1 when the event is one cycle wide.

Build gateware with a probe, e.g. --with-si5351-i2c-probe, then:
    litex_server --pcie --pcie-bar=/sys/bus/pci/devices/<BDF>/resource0
"""

import sys
import time
import pickle
import argparse

# CSV ----------------------------------------------------------------------------------------------

def load_layout(path):
    """Parse an analyzer CSV into (config, [(name, width)]).

    `enum,` rows are skipped: they carry FSM state labels and are what breaks
    litescope_cli's own parser.
    """
    config, signals = {}, []
    with open(path) as f:
        for line in f:
            parts = line.strip().split(",")
            if not parts or not parts[0]:
                continue
            if parts[0] == "config":
                config[parts[2]] = int(parts[3])
            elif parts[0] == "signal":
                signals.append((parts[2], int(parts[3])))
    if not signals:
        sys.exit("%s: no signal rows" % path)
    return config, signals

def bit_offsets(signals):
    """Signals are packed LSB-first in CSV order."""
    offsets, bit = {}, 0
    for name, width in signals:
        offsets[name] = (bit, width)
        bit += width
    return offsets

# Analyzer -----------------------------------------------------------------------------------------

class Analyzer:
    def __init__(self, bus, config):
        self.bus    = bus
        self.width  = config["storage_width"]
        self.depth  = config["depth"]
        r = bus.regs
        try:
            self.trigger_enable   = r.analyzer_trigger_enable
            self.trigger_done     = r.analyzer_trigger_done
            self.trigger_mem_mask = r.analyzer_trigger_mem_mask
            self.trigger_mem_value= r.analyzer_trigger_mem_value
            self.trigger_mem_write= r.analyzer_trigger_mem_write
            self.subsampler       = r.analyzer_subsampler_value
            self.storage_enable   = r.analyzer_storage_enable
            self.storage_done     = r.analyzer_storage_done
            self.storage_length   = r.analyzer_storage_length
            self.storage_offset   = r.analyzer_storage_offset
            self.storage_mem_level= r.analyzer_storage_mem_level
            self.storage_mem_data = r.analyzer_storage_mem_data
            self.mux              = r.analyzer_mux_value
        except AttributeError as e:
            sys.exit("analyzer CSRs missing (%s) -- was the gateware built with a probe?" % e)

    def arm(self, mask, value, length, subsampling, offset=0):
        self.trigger_enable.write(0)
        deadline = time.time() + 2.0
        for _ in range(2):                       # let the gateware flush stale terms
            while not self.trigger_done.read():
                if time.time() > deadline:
                    sys.exit("timed out waiting for the trigger memory to flush")
        self.mux.write(0)
        self.trigger_mem_mask.write(mask)
        self.trigger_mem_value.write(value)
        self.trigger_mem_write.write(1)
        self.subsampler.write(subsampling - 1)
        self.storage_offset.write(offset)
        self.storage_length.write(length)
        self.storage_enable.write(0)             # storage arms on the rising edge
        self.storage_enable.write(1)
        self.trigger_enable.write(1)

    def wait(self, timeout):
        t0 = time.time()
        while not self.storage_done.read():
            if time.time() - t0 > timeout:
                return False
            time.sleep(0.05)
        return True

    def upload(self):
        count    = self.storage_mem_level.read()
        subwords = (self.width + 31) // 32
        samples  = []
        for _ in range(count):
            value = 0
            for _ in range(subwords):
                value = (value << 32) | self.storage_mem_data.read()
            samples.append(value)
        return samples

# Reporting ----------------------------------------------------------------------------------------

def report_activity(samples, offsets):
    changing, constant = [], []
    for name, (bit, width) in offsets.items():
        mask   = (1 << width) - 1
        values = {(s >> bit) & mask for s in samples}
        (changing if len(values) > 1 else constant).append((name, sorted(values)[:4], len(values)))
    print("\n=== CHANGING (%d) ===" % len(changing))
    for name, sample, n in changing:
        print("  %-52s %d distinct, e.g. %s" % (name, n, sample))
    print("=== CONSTANT (%d) ===" % len(constant))
    for name, sample, _ in constant:
        print("  %-52s = %s" % (name, sample[0]))
    if not changing:
        print("\nNothing changed in the whole capture. Either the trigger fired on a stale "
              "term (see the note in this file's docstring) or the event is outside the window.")

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Capture from a LiteScope analyzer and decode it.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--csv",         default="test/analyzer.csv", help="Analyzer CSV from the gateware build.")
    parser.add_argument("--trigger",     default=None, help="Signal name to trigger on (rising to 1).")
    parser.add_argument("--length",      default=1024, type=int,   help="Samples to capture.")
    parser.add_argument("--subsampling", default=1,    type=int,   help="Keep 1 sample in N (widens the window).")
    parser.add_argument("--offset",      default=0,    type=int,   help="Pre-trigger samples to retain.")
    parser.add_argument("--timeout",     default=15.0, type=float, help="Seconds to wait for the trigger.")
    parser.add_argument("--list",        action="store_true", help="List signal names and exit.")
    parser.add_argument("--dump",        default=None, help="Write raw samples + layout to this pickle.")
    args = parser.parse_args()

    config, signals = load_layout(args.csv)
    offsets         = bit_offsets(signals)

    if args.list:
        print("%d signals, %d bits total, depth %d" % (len(signals), config["storage_width"], config["depth"]))
        for name, (bit, width) in offsets.items():
            print("  bit %4d  width %3d  %s" % (bit, width, name))
        return

    from litex import RemoteClient   # imported late: --help/--list need no litex
    bus = RemoteClient()
    bus.open()
    try:
        analyzer = Analyzer(bus, config)
        if args.trigger:
            if args.trigger not in offsets:
                sys.exit("unknown signal %r (use --list)" % args.trigger)
            bit, _ = offsets[args.trigger]
            mask = value = 1 << bit
            print("trigger on %s (bit %d)" % (args.trigger, bit))
        else:
            mask = value = 0                     # match anything: fire immediately
            print("no trigger: capturing immediately")

        window_us = args.length * args.subsampling / 125e6 * 1e6
        analyzer.arm(mask, value, args.length, args.subsampling, args.offset)
        print("armed: length %d, subsampling %d -> %.0f us window at 125 MHz"
              % (args.length, args.subsampling, window_us))

        if not analyzer.wait(args.timeout):
            print("TIMED OUT waiting for the trigger -- the event did not occur")
            return
        samples = analyzer.upload()
        print("captured %d samples" % len(samples))
        report_activity(samples, offsets)

        if args.dump:
            with open(args.dump, "wb") as f:
                pickle.dump({"samples": samples, "offsets": offsets, "config": config}, f)
            print("\nwrote %s" % args.dump)
    finally:
        bus.close()

if __name__ == "__main__":
    main()
