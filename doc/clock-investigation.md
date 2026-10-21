# AD9361 Clock Scaling Investigation

Date: 2026-08-13. Branch: `rx-tx-working`.
Hardware: two LiteX-M2SDR M.2 cards (FPGA DNA `0x0024c4825710485c`,
`0x0060544e4730485c`) in Raspberry Pi 5 hosts. Reference instrument: bladeRF 2.0
micro (same AD9361 RFIC, same nominal 38.4 MHz reference) plus a bench spectrum
analyzer.

## Resolution (2026-10-20): a LiteX code-generation bug, not the clock

The SI5351 was never being programmed on our builds. LiteX `5b641f517`
(2026-06-16, "gen/fhdl: unify combinatorial Verilog generation") made generated
`always @(*)` blocks use blocking `=` assignments while keeping source statement
order. LiteI2C's PHY reads `sr_out_en` (in `If(sr_out_en, sda_oe.eq(1), ...)`)
before the FSM statement that assigns it, in the same block, so in synthesis the
read sees the default `0`: SDA is never driven during payload bytes and **every
I2C data byte goes out as `0xFF`**. The address byte is driven from the FSM
directly, so the SI5351 still ACKs and nothing reports an error. The boot
sequencer's writes all became "register `0xFF` <- `0xFF`".

Evidence, on the Jetson Orin Nano board:

- Draining the I2C master's RX FIFO before each transaction, `0x60` ACKs and other
  addresses NACK, but multi-byte reads return `00 11 F8 03` **for any register
  pointer**: register `0xFF`, then a wrap to `0x00` (`0x11`) and `0x01` (`0xF8`,
  the power-up sticky bits, never cleared). The pointer write itself was `0xFF`.
- Migen simulation of the PHY is correct; it iterates comb logic to a fixpoint.
  Vivado xsim of the *generated* Verilog puts `11111111 11111111` on SDA for a
  `0x07, 0x01` write with LiteX `2ebe242f7`, and `00000111 00000001` with LiteX
  master.
- Rebuilt with LiteX `8e0e71c60`: clk-test `AD9361 Ref Clk 38.40`, `Time Ref Clk
  100.00`, SI5351 registers read back exactly the boot table, board time
  advances, `DATA_CLK` is exactly `2 x Fs`, DMA streams without the synchronizer
  bypass, and 735 MHz LTE PSDs line up with a bladeRF 2.0.

LiteX fixed this on 2026-07-07 (`fd69861c6`, `acb25fa66`). Our environment had
pinned `2ebe242f7`, inside the broken window, which is why the 2026-08-07 and
later builds were bad while the vendor's 2026-05-15 image (older LiteX) was fine.
`scripts/check_verilog_comb_order.py` detects the pattern in any build's Verilog.

The rest of this document is the August investigation, kept for the
measurements; the "SI5351 observations" below are explained by the above.

## Conclusion, corrected (2026-08-13)

**The clock is fine. The 2026-08-07 gateware was not.**

Everything below was measured on a gateware build dated **2026-08-07**, and the
sample-rate and LO errors it describes are properties of *that build*. On the
vendor's **2026-05-15** image the same boards produce, at every rate tested:

```
   requested   DATA_CLK MHz  DATA_CLK/Fs    vs 2xFs
      3.840M          7.680       2.0000     1.0000
      7.680M         15.360       2.0000     1.0000
     15.360M         30.720       2.0000     1.0000
     30.720M         61.440       2.0000     1.0000
     61.440M        122.880       2.0000     1.0000
```

Exact, not approximate. For 1T1R LVDS `DATA_CLK = 2 x Fs` is the correct
relationship, so there is **no reference-clock error at all** on that image.

Two mistakes in the original write-up, both corrected in the repo:

1. **The expected relationship was taken as `DATA_CLK = 4 x Fs`.** That holds for
   2T2R, not 1T1R. Measuring 1T1R against a 4x expectation manufactured a
   spurious "0.5x" and made a healthy board look broken.
   `scripts/m2sdr_rate_scan.py` now picks the multiplier from the layout.
2. **`xo_disable_use_ext_refclk_enable` was set to 1** to "fix" the scaling. On
   the 2026-05-15 image `DATA_CLK` is exactly right with the setting at either 0
   or 1 -- it changes nothing. It has been reverted to the stock value of 0.

If you are chasing a rate or frequency error, **check which gateware is loaded
first** (`m2sdr_util info`) before suspecting the reference.

## What the 2026-08-07 gateware did

Recorded because the effects were real and repeatable on that build, and because
the same symptoms would identify it again.

- Sample rate ran at roughly 0.88-0.92 of the requested value, varying with rate
  (0.823 at 3.84 MSPS through 0.894 at 61.44 MSPS).
- LO was off by a constant 0.6937: requested 1000 MHz measured 693.455 MHz,
  requested 2000 MHz measured 1387.08 MHz, on a spectrum analyzer with
  `--tone 0` so the carrier sits exactly on the LO.
- Those two ratios never reconciled to a single reference frequency, which in
  hindsight was the clue that no single reference was responsible.
- The FPGA<->AD9361 interface delay calibration failed on that build ("No valid
  RX Clk/Dat delay settings found"), consistent with the FPGA and the RFIC
  disagreeing about DATA_CLK.

## The clock chain is programmed correctly

Worth keeping, because it holds regardless of gateware. Diffing our AD9361
registers against a bladeRF 2.0 micro at matched rates, every BBPLL register is
byte-identical, at every rate:

| register | bladeRF | M2SDR |
|---|---|---|
| `0x03F`, `0x041`-`0x047` | `01`,`13`,`20`,`00`,`19`,`00`,`04`,`00` | identical |
| `0x02A` / `0x02B` (ref divide) | `00` / `00` | identical |
| `0x00A` divider @ 3.84/7.68/15.36/30.72/61.44 MSPS | 5/4/3/2/1 | identical |

Decoded: integer 25 plus fractional `0x132000`/2088960 = **BBPLL = refclk x 25.6**,
exactly right for 30.72 MSPS from 38.4 MHz. Both parts hold the BBPLL fixed and
vary only the output divider. The driver's arithmetic was never in question.

The one register that differed was `0x009` (bladeRF `0x17`, ours `0x07`,
bit 4 = `XO_BYPASS`). That difference is real but, as established above, has no
measurable effect here.

## SI5351 observations (explained 2026-10-20, see Resolution)

These were measured on both gateware builds and are not explained by the above.

- **I2C reads always return `0x00`**, yet the chip answers: driving the I2C
  master by hand, address `0x60` **ACKs** (`RX_RDY=1, NACK=0`) while a
  deliberately bogus `0x55` correctly **NACKs**. Bus, controller and NACK
  detection all work; only the read data path returns zeros. Writes are
  therefore unproven -- the usual write/readback check proves nothing here.
  Reproduce with `scripts/m2sdr_si5351_probe.py`.
- **Disabling every SI5351 output** (`0x03 = 0xFF`) left DATA_CLK unchanged
  (109.691 -> 109.642 MHz, noise). Whatever clocks the AD9361 does not come
  through the SI5351 outputs.
- `m2sdr_util clk-test` reports **"AD9361 Ref Clk" = 0.00** and **"Time Ref Clk"
  = 0.00** while Sys, PCIe and FPGA 10M read correctly.
- A LiteScope capture on the I2C lines showed the master accepting transactions
  while `scl_oe`, `sda_o`, `sda_oe`, `sda_i` stayed constant 0 out to 8.4 ms.
  `sda_i` reading 0 with nothing driving is wrong for a pulled-up open-drain bus,
  and it contradicts the ACK/NACK result -- both cannot be true. Unresolved.

The I2C pins are FPGA balls AA20/AB21 with `PULLUP=TRUE`, i.e. the design relies
on the FPGA's **internal** pull-ups. The constraint does reach the bitstream.

## PTM is non-functional

`PTMControl: Enabled-` in config space, and neither the BCM2712 root port nor the
ASM2806 switch advertises PTM, so there is no PTM root in the path. The requester
sits permanently `BUSY=1, VALID=0`. Even if the dialog worked, `TimeGenerator` is
clocked from `si5351_clk1` -- the dead "Time Ref Clk" above -- so board time is
frozen at zero.

## Two status panels that lie

1. **`m2sdr_util info` SI5351 panel** reports "SYS_INIT: Ready, LOL_A/B: Locked,
   LOS: Valid Signal". These are *active-high fault bits*, so a device returning
   `0x00` -- including one on a dead read path -- decodes as everything-perfect.
2. **`PCIe PTM: Enabled`** is the build-time capability flag
   (`CSRField("ptm", reset=pcie_ptm)`), reported regardless of runtime state.

## Dead ends, recorded so they are not repeated

- **`--refclk-freq` cannot reach the hardware.** It only changes the AD9361
  driver's divider arithmetic; the SI5351 table selection branches solely on
  `== 40000000`.
- **`--refclk-ppm` is capped at 100 ppm** (`M2SDR_REFCLK_PPM_MAX`).
- **AD9361 internal loopback cannot measure rate accuracy.** It duplicates every
  sample, so a tone returns at half frequency with an Fs/2 image. Real RX shows
  no duplication (0.06%), so this is a property of the loopback path only.
- **Do not copy the fallback image into the operational slot.** The vendor's
  `_operational.bin` and `_fallback.bin` differ in 4 bytes at offset 62 -- the
  `NEXT_CONFIG_ADDR` / IPROG jump. The fallback jumps to `0x800000`; placed *at*
  `0x800000` it jumps to itself forever and the board never enumerates.
