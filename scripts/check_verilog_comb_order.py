#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Flag generated Verilog whose combinational blocks read a target before assigning it.

LiteX 5b641f517 (2026-06-16) emitted `always @(*)` blocks with blocking `=`
assignments but kept the statements in source order. A block that reads one of
its own targets before the statement assigning it then sees the reset default
in synthesis, while Migen's simulator (which iterates comb logic to a fixpoint)
still behaves. On the M2SDR this miscompiled the LiteI2C PHY: SDA was never
driven during payload bytes, every I2C data byte went out as 0xFF and the SI5351
was never programmed (no AD9361 reference, no time reference). Fixed in LiteX by
fd69861c6 / acb25fa66 (2026-07-07, dependency-ordered comb blocks).

Run it on a build's generated Verilog; it exits non-zero if any such read exists:

    ./scripts/check_verilog_comb_order.py build/litex_m2sdr_m2_pcie_x1/gateware/litex_m2sdr_m2_pcie_x1.v
"""

import re
import sys

IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
DEFAULT = re.compile(r"\s*([A-Za-z_]\w*)\s*<?=\s*[\d']")
ASSIGN = re.compile(r"\s*([A-Za-z_]\w*)(\[[^\]]*\])?\s*<?=(?!=)\s*(.*)")


def comb_blocks(lines):
    """Yield (first line number, body lines) for each `always @(*) begin ... end`."""
    i = 0
    while i < len(lines):
        if lines[i].strip() == "always @(*) begin":
            depth, j, body = 1, i + 1, []
            while depth and j < len(lines):
                s = lines[j].strip()
                depth += len(re.findall(r"\bbegin\b", s))
                depth -= len(re.findall(r"^end\b", s))
                body.append((j + 1, lines[j]))
                j += 1
            yield i + 1, body
            i = j
        else:
            i += 1


def forward_reads(lines):
    """Return {signal: (block line, read line)} for targets read before their first assignment."""
    found = {}
    for blk, body in comb_blocks(lines):
        # LiteX opens each block with one reset-default assignment per target.
        k, targets = 0, set()
        while k < len(body) and DEFAULT.match(body[k][1]):
            targets.add(DEFAULT.match(body[k][1]).group(1))
            k += 1
        assigned = set()
        for ln, line in body[k:]:
            m = ASSIGN.match(line)
            reads = m.group(3) if m else line
            for t in set(IDENT.findall(reads)) & targets:
                if t not in assigned:
                    found.setdefault(t, (blk, ln))
            if m and m.group(1) in targets:
                assigned.add(m.group(1))
    return found


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    lines = open(sys.argv[1]).read().split("\n")
    found = forward_reads(lines)
    for t, (blk, ln) in sorted(found.items(), key=lambda kv: kv[1]):
        print(f"{sys.argv[1]}:{ln}: '{t}' read before assignment in always @(*) at line {blk}")
    print(f"{len(found)} combinational signal(s) read before assignment")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
