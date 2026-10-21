#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Drive the SI5351 I2C master directly and decode what the bus actually does.

`m2sdr_util si5351-dump` prints register values but cannot tell you WHY they are
what they are: a device that is not answering and a device answering with zeros
look identical. This walks the LiteI2C master registers by hand and reports the
status bits at each step, so an unresponsive chip, a NACK and a successful-but-
zero read are distinguishable.

It also probes a deliberately-absent address as a control, and reads the same
4-byte burst from two different register pointers. If the chip ACKs, the control
NACKs, and both bursts come back identical, the register pointer never reached
the chip: payload bytes are going out wrong. That is the signature of the LiteX
code-generation bug that left the SI5351 unprogrammed (every I2C data byte sent
as 0xFF, so reads always start at register 0xFF: `00 11 F8 03`); see
doc/clock-investigation.md and scripts/check_verilog_comb_order.py.

The master's RX FIFO is drained before every transaction: a stale entry left by
an earlier transaction otherwise shifts every result by one.

Requires litex_server (the CSRs are reached via RemoteClient):
    litex_server --pcie --pcie-bar=/sys/bus/pci/devices/<BDF>/resource0
"""

import sys
import time
import argparse

# Constants ----------------------------------------------------------------------------------------

SI5351_I2C_ADDR = 0x60
BOGUS_I2C_ADDR  = 0x55      # nothing lives here; used as a NACK control

# LiteI2C master status bits.
STATUS_TX_READY = 0
STATUS_RX_READY = 1
STATUS_NACK     = 8

# Helpers ------------------------------------------------------------------------------------------

def decode_status(value):
    return ("status=0x%08x  TX_RDY=%d RX_RDY=%d NACK=%d"
            % (value, (value >> STATUS_TX_READY) & 1,
                      (value >> STATUS_RX_READY) & 1,
                      (value >> STATUS_NACK)     & 1))

class I2CMaster:
    def __init__(self, bus):
        self.bus = bus
        for name in ("active", "settings", "addr", "rxtx", "status"):
            if not hasattr(bus.regs, "si5351_i2c_master_" + name):
                sys.exit("si5351_i2c_master_%s not in the CSR map -- wrong gateware?" % name)
        r             = bus.regs
        self.active   = r.si5351_i2c_master_active
        self.settings = r.si5351_i2c_master_settings
        self.addr     = r.si5351_i2c_master_addr
        self.rxtx     = r.si5351_i2c_master_rxtx
        self.status   = r.si5351_i2c_master_status

    def reset(self):
        self.active.write(0)
        self.settings.write(0)

    def drain(self):
        """Pop stale RX FIFO entries so results belong to the next transaction."""
        for _ in range(64):
            if not (self.status.read() >> STATUS_RX_READY) & 1:
                return
            self.rxtx.read()

    def read_register(self, slave, reg, verbose=True, nbytes=1):
        """One address byte written, nbytes (1-4) data bytes read back (first byte in the MSBs)."""
        self.reset()
        self.drain()
        self.settings.write((nbytes << 8) | 1)  # 1 byte out, nbytes in
        self.addr.write(slave)
        self.active.write(1)
        if verbose:
            print("    after start        : %s" % decode_status(self.status.read()))
        self.rxtx.write(reg)
        time.sleep(0.05)
        st = self.status.read()
        if verbose:
            print("    after sending reg  : %s" % decode_status(st))
        data = self.rxtx.read() & ((1 << (8 * nbytes)) - 1)
        self.active.write(0)
        return data, st

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description     = "Probe the SI5351 I2C bus and decode the transaction.",
        formatter_class = argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--reg",   default=0x00, type=lambda x: int(x, 0), help="Register to read.")
    parser.add_argument("--addr",  default=SI5351_I2C_ADDR, type=lambda x: int(x, 0), help="Slave address.")
    parser.add_argument("--sweep", action="store_true",
                        help="Read a spread of registers that must be non-zero on a configured part.")
    args = parser.parse_args()

    from litex import RemoteClient   # imported late: --help/--list need no litex
    bus = RemoteClient()
    bus.open()
    try:
        i2c = I2CMaster(bus)

        print("=== target 0x%02x, register 0x%02x ===" % (args.addr, args.reg))
        data, status = i2c.read_register(args.addr, args.reg)
        print("    data               : 0x%02x" % data)

        print("=== control: bogus address 0x%02x (must NACK) ===" % BOGUS_I2C_ADDR)
        _, bogus_status = i2c.read_register(BOGUS_I2C_ADDR, args.reg)

        print("=== pointer test: 4-byte bursts from registers 0x00 and 0x10 ===")
        burst0, _ = i2c.read_register(args.addr, 0x00, verbose=False, nbytes=4)
        burst1, _ = i2c.read_register(args.addr, 0x10, verbose=False, nbytes=4)
        print("    0x00: %08x   0x10: %08x" % (burst0, burst1))

        acked = not ((status       >> STATUS_NACK) & 1)
        ctrl  =      (bogus_status >> STATUS_NACK) & 1
        print("\n=== verdict ===")
        if not ctrl:
            print("  Control address did NOT NACK -- NACK detection is not trustworthy;")
            print("  nothing below can be concluded about the bus.")
        elif acked and burst0 == burst1:
            print("  Chip ACKs at 0x%02x and the control NACKs, but bursts from two different" % args.addr)
            print("  registers are identical: the register pointer never reaches the chip, so")
            print("  payload bytes are going out wrong (writes are lost too). Check the gateware")
            print("  Verilog with scripts/check_verilog_comb_order.py (LiteX >= fd69861c6 needed).")
        elif acked and data == 0x00:
            print("  Chip ACKs at 0x%02x and returned 0x00 for register 0x%02x; the pointer test" % (args.addr, args.reg))
            print("  passed, so this may simply be the register's value.")
        elif acked:
            print("  Chip ACKs and returned 0x%02x: the bus is healthy." % data)
        else:
            print("  Chip did not ACK at 0x%02x: not present, unpowered, or held off the bus." % args.addr)

        if args.sweep:
            print("\n=== registers that are non-zero on a configured SI5351 ===")
            for reg, expect in ((0x02, "int mask"), (0x03, "output enable"), (0x10, "CLK0 ctrl"),
                                (0x22, "PLLB fb"),  (0x2D, "MS0 P1"),        (0xB7, "XTAL_CL")):
                value, _ = i2c.read_register(args.addr, reg, verbose=False)
                print("    reg 0x%02x (%-13s) = 0x%02x%s"
                      % (reg, expect, value, "" if value else "   <-- zero"))
    finally:
        bus.close()

if __name__ == "__main__":
    main()
