# AD9361 Clock Scaling Investigation

Date: 2026-08-13. Branch: `rx-tx-working`.
Hardware: two LiteX-M2SDR M.2 cards (FPGA DNA `0x0024c4825710485c`,
`0x0060544e4730485c`) in Raspberry Pi 5 hosts, gateware built 2026-08-07.
Reference instrument: bladeRF 2.0 micro (same AD9361 RFIC, same nominal
38.4 MHz reference) plus a bench spectrum analyzer.

## Question

The board does not produce the sample rate or the LO frequency it is asked for.
Requested 30.72 MSPS came out near 27 MSPS, and requested rates below 2.5 MSPS
behaved differently again. Is this the driver programming the clock chain wrongly,
or is the AD9361 running from a reference that is not 38.4 MHz?

## Answer

**Partly resolved.** One real fault was found and fixed: the AD9361 was left
running its internal crystal-oscillator amplifier against a driven clock, because
`xo_disable_use_ext_refclk_enable` was 0. Setting it to 1 (commit `da53bdb`) turned
a rate-dependent error into a clean constant scale factor.

**The residual error is unexplained.** After the fix the sample rate is a steady
0.915 of nominal across a 16x range of rates. The LO is off by a *different*
constant, and the two do not reconcile to any single reference frequency. Do not
assume the reference is simply 38.4 x 0.915 — the measurements below say otherwise.

## Measurements

### The clock chain is programmed correctly

Diffing our AD9361 registers against a bladeRF 2.0 micro at matched rates, every
BBPLL register is byte-identical, at every rate:

| register | bladeRF | M2SDR |
|---|---|---|
| `0x03F`, `0x041`–`0x047` | `01`,`13`,`20`,`00`,`19`,`00`,`04`,`00` | identical |
| `0x02A` / `0x02B` (ref divide) | `00` / `00` | identical |
| `0x00A` divider @ 3.84/7.68/15.36/30.72/61.44 MSPS | 5/4/3/2/1 | identical |

Decoded, that is integer 25 plus fractional `0x132000`/2088960 = **BBPLL =
refclk x 25.6**, which is exactly right for 30.72 MSPS from 38.4 MHz. Both parts
hold the BBPLL fixed and vary only the output divider.

This rules out a whole class of hypotheses — divider selection, FIR table choice,
`--refclk-freq` arithmetic. The programming was never wrong.

### One register differed: `0x009`

bladeRF `0x17`, M2SDR `0x07`. Bit 4 is `XO_BYPASS`, and the two codebases set the
same init parameter to opposite values — bladeRF's `ad936x_params.c` carries the
comment "Expect external clock into XTALN". Our board drives that pin too, so the
bladeRF value is the correct one. Fixed in `da53bdb`.

Effect, measured with `scripts/m2sdr_rate_scan.py`:

| requested | before (`use_extclk=0`) | after (`use_extclk=1`) |
|---|---|---|
| 3.84 MSPS | 0.823 | **0.9154** |
| 7.68 MSPS | 0.883 | **0.9154** |
| 15.36 MSPS | 0.877 | **0.9154** |
| 30.72 MSPS | 0.894 | **0.9150** |
| 61.44 MSPS | 0.894 | **0.9140** |

A wandering, rate-dependent error became a constant one. That is the signature of
a pure reference-frequency scale error rather than a broken divider chain.

### The two ratios do not agree

Measured on a spectrum analyzer with `--tone 0`, so the carrier sits exactly on
the LO and no sample-rate term enters:

- requested 1000 MHz → **693.455 MHz**
- requested 2000 MHz → **1387.08 MHz**

A constant ratio of **0.6937** (a fixed offset would have put the second at
1693 MHz, so that is ruled out).

Sample rate, by two independent methods that agree with each other:

- DAC alias spacing on the analyzer: **27.26 MHz**
- `DATA_CLK / 4` read from the FPGA: **27.39 MHz**

giving **~0.884** — and incidentally establishing that **DATA_CLK = 4 x Fs**.

**0.6937 and 0.884 cannot both come from one reference frequency.** This is the
open question. One hypothesis, unverified: the BBPLL is being driven below its
~715 MHz VCO minimum and behaving non-linearly, while the GHz-range RFPLL scales
cleanly. Nobody has tested that.

### The SI5351 is not the AD9361's reference

Writing `0x03 = 0xFF` (disable **all** SI5351 outputs) left DATA_CLK completely
unchanged: 109.691 → 109.642 MHz, within noise. Whatever clocks the AD9361, it is
not coming through the SI5351 outputs.

The FPGA's own clock measurement agrees — `m2sdr_util clk-test` reports
**"AD9361 Ref Clk" = 0.00 MHz** and **"Time Ref Clk" = 0.00 MHz**, while Sys, PCIe
and FPGA 10M all read correctly.

### The SI5351 I2C read path is broken

Every register reads back `0x00`, and a write/readback test fails (wrote `0x5A`
to `0xA5`, read `0x00`). But the chip is present and answering: driving the I2C
master by hand, address `0x60` **ACKs** (`RX_RDY=1, NACK=0`) while a deliberately
bogus `0x55` correctly **NACKs**. So the bus, the controller and NACK detection
all work — only the read data path returns zeros.

Consequently **writes are unproven**. The usual write/readback check proves
nothing here, and side effects must be used instead. Reproduce with
`scripts/m2sdr_si5351_probe.py`.

A LiteScope capture on the I2C lines (gateware built `--with-si5351-i2c-probe`,
read out with `scripts/m2sdr_litescope.py`) showed the master accepting
transactions — `source_payload_data` changing — while `scl_oe`, `sda_o`, `sda_oe`
and `sda_i` stayed constant 0 across the whole window at 8 ns, 128 ns and 4 µs
resolution out to 8.4 ms. `sda_i` reading 0 while nothing drives it is itself
wrong for a pulled-up open-drain bus. This is **not** reconciled with the ACK/NACK
result above; both cannot be true, so either NACK is synthesised from internal
state rather than the wire, or the probed nets are not the ones reaching the pads.
Unresolved.

The I2C pins are FPGA balls AA20/AB21 with `PULLUP=TRUE` — the design relies on
the FPGA's **internal** pull-ups, not external resistors. The constraint does
reach the bitstream (confirmed in the generated XDC, no Vivado warnings).

### PTM is non-functional

`PTMControl: Enabled-` in config space, and neither the BCM2712 root port nor the
ASM2806 switch advertises PTM at all, so there is no PTM root in the path. The
requester sits permanently `BUSY=1, VALID=0` with MASTER_TIME, LINK_DELAY, T1 and
T4 all zero.

Even if the dialog worked, `TimeGenerator` is clocked from `si5351_clk1` — the
dead "Time Ref Clk" above — so board time is frozen. Reading `TIME_GEN READ_TIME`
four times a second apart returns `0x00000000` every time.

## Two status panels that lie

Both cost real debugging time and are worth fixing:

1. **`m2sdr_util info` SI5351 panel** reports "SYS_INIT: Ready, LOL_A/B: Locked,
   LOS: Valid Signal". These are *active-high fault bits*, so a device returning
   `0x00` — including one on a dead read path — decodes as everything-perfect. It
   is not evidence the SI5351 is alive.
2. **`PCIe PTM: Enabled`** is the build-time capability flag
   (`CSRField("ptm", reset=pcie_ptm)` in `gateware/capability.py`), reported
   regardless of runtime state. Actual state is `PTMControl: Enabled-`.

## Dead ends, recorded so they are not repeated

- **`--refclk-freq` cannot work around this.** It only changes the AD9361
  driver's divider arithmetic; the SI5351 table selection branches solely on
  `== 40000000`, so it never reprograms hardware. Sweeping it produces
  non-monotonic DATA_CLK because the driver picks different integer divider and
  FIR combinations per assumed reference, not because the hardware changed.
- **`--refclk-ppm` is capped at 100 ppm** (`M2SDR_REFCLK_PPM_MAX`), nowhere near
  enough for an 8 % error.
- **AD9361 internal loopback cannot measure rate accuracy.** It duplicates every
  sample (confirmed in the raw stream), so a tone comes back at half the expected
  frequency with an Fs/2 image. Real RX shows no duplication (0.06 %), so this is
  a property of the loopback path only.

## What to do next

1. Measure the actual frequency on the AD9361's reference pin. Everything above
   is inference; one instrument reading would settle it.
2. Check whether the BBPLL is being driven below its VCO minimum, which is the
   leading explanation for why the LO and sample-rate ratios differ.
3. Fix the two misleading status panels.
