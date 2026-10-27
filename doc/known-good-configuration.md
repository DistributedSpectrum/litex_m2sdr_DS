# Known-Good Configuration

## Current: Jetson Orin Nano, own gateware build (2026-10-20)

Host `litexjackson` (Jetson Orin Nano developer kit, L4T kernel `5.15.148-tegra`),
card FPGA DNA `0x0024c4825710485c`, gateware built from this branch with:

| | |
|---|---|
| LiteX | `8e0e71c60` (needs >= `fd69861c6`, see [Clock Investigation](clock-investigation.md)) |
| litepcie / litei2c / migen | `586d7a4` / `81cf75d` / `4c2ae8d` |
| build | `./litex_m2sdr.py --variant=m2 --with-pcie --pcie-gen=2 --pcie-lanes=4 --build` |
| identifier | `LiteX-M2SDR SoC / m2 variant / built on 2026-10-20 15:09:23` |
| flash | fallback `0x0`, operational `0x800000`, JTAG-HS2, read back and compared |

Verified: SI5351 status `0x11` and both PLLs locked, `AD9361 Ref Clk 38.40` and
`Time Ref Clk 100.00` MHz, board time advancing, `DATA_CLK` exactly `2 x Fs` in
1T1R (61.44 MHz at 30.72 MSPS, 245.76 MHz at 122.88 MSPS wide-bandwidth), DMA
running without the synchronizer bypass, 122.88 MSPS sustained in sc8 and sc16
with 0 overflows, 735 MHz LTE PSDs matching a bladeRF 2.0 at 30.72 and 61.44
MSPS, and a 60 s / 14.75 GB 122.88 MSPS sc8 NVMe recording with 0 overflows.
Host-side details are in [Jetson Orin](hosts/jetson-orin.md).

Confirm a board is on a healthy build with
`./scripts/check_verilog_comb_order.py` on its Verilog (0 hits) and
`m2sdr_util clk-test` (38.40 / 100.00 MHz).

## Historical: vendor gateware on Raspberry Pi 5 (2026-08-13)

Kept for reference. At the time our own builds were broken by the LiteX
code-generation bug above, so the vendor image was the only good gateware. The
debug-gating and tag/branch references below describe that older software
state, not this branch.

Date: 2026-08-13. Branch: `known-good-may-gateware`, tag `known-good-2026-08-13`.
A verified-working combination of gateware, host software and SDR stack, recorded
so it can be returned to. Source of truth for this snapshot was **litexpi2**
(FPGA DNA `0x0060544e4730485c`) with the same combination confirmed on litexpi1.

### What was verified

This command ran correctly and produced the expected output on a spectrum
analyzer:

```bash
python m2sdr_tx_stream.py --file /data/iq_tmp/SOI_E_msc_oobWIFI_large.sigmf-data \
    --freq 980e6 --rate 46079980 --bandwidth 20e6 --att 0 --repeat \
    --format cf32 --channels 1
```

An 802.11 g/n 20 MHz capture (`cf32_le`, 46.079980 MSPS, recorded at 1.4 GHz)
replayed at a 980 MHz LO, single channel.

Note `--att 0` is **full power** (~+8 dBm at the SMA). That is fine over the air
or into an attenuator, but check the path before repeating it into another
receiver's front end.

### Gateware

| | |
|---|---|
| build | `LiteX-M2SDR SoC / m2 variant / built on 2026-05-15 16:58:50` |
| variant | M.2, PCIe Gen2 x1 |
| PCIe PTM | Disabled |
| source | vendor original release, `litex_m2sdr_m2_pcie_x1_operational.bin` |
| md5 | `e1421af69a39bee9bdd983e2f88c68be` |
| size | 2999592 bytes |
| flashed to | operational slot, `0x800000` |

This is the vendor image, **not** a local build. The 2026-08-07 build that
preceded it produced wrong sample rates and LO frequencies -- see
[Clock Investigation](clock-investigation.md).

To restore it:

```bash
m2sdr_util -y flash-write litex_m2sdr_m2_pcie_x1_operational.bin 0x800000
sudo reboot
m2sdr_util info | grep "built on"     # expect 2026-05-15 16:58:50
```

Flash the **operational** file, never the `_fallback` one, into `0x800000`. The
two differ in 4 bytes at offset 62 -- the `NEXT_CONFIG_ADDR` / IPROG jump -- and
the fallback placed at `0x800000` jumps to itself forever, leaving the board
absent from PCIe entirely. The fallback slot at `0x0` was deliberately left
untouched as the recovery path.

### Host

| | litexpi1 | litexpi2 |
|---|---|---|
| OS | Debian 13 (trixie), Pi OS Lite arm64 ref 2026-06-18 | same |
| kernel | 6.18.34+rpt-rpi-v8 | 6.18.39+rpt-rpi-v8 |
| page size | 4096 (required) | 4096 |
| GNU Radio | 3.10.12.0-1 | 3.10.12.0-1 |
| SoapySDR | 0.8.1-5+b2 | 0.8.1-5+b2 |
| gr-osmosdr | 0.2.6-4 | 0.2.6-4 |

Kernels differ between the two units; both are 4K-page `-v8` builds and each has
its own `m2sdr.ko`. That difference is not believed to matter, but it is what was
tested.

### Software

Tag `known-good-2026-08-13` on branch `known-good-may-gateware`. Relative to
upstream this carries:

- **`soapysdr:`** the `interleave*()` destination-offset fix, without which any
  GNU Radio TX in 1T1R segfaults.
- **`libm2sdr:`** `xo_disable_use_ext_refclk_enable` at the stock **0**. It was
  briefly set to 1; on this gateware it makes no measurable difference and has
  been reverted.
- **`ad9361:` / `libm2sdr:`** `[CALDBG]`, `[CLKDBG]` and `[BYPASSDBG]` gated
  behind `M2SDR_CAL_DEBUG` / `M2SDR_CLK_DEBUG` / `M2SDR_STREAM_DEBUG`, silent by
  default.
- **`scripts/`** the RX/TX tools, including SigMF-driven format and rate
  selection in `m2sdr_tx_stream.py`.

The SoapySDR module must sit in `/usr/lib/<arch>-linux-gnu/SoapySDR/modules0.8/`.
`make install` writes somewhere else that is not on the search path.

### Confirming you are back on it

```bash
m2sdr_util info | grep -E "built on|PTM"      # 2026-05-15 16:58:50, PTM Disabled
m2sdr_util ad9361-read 0x009                  # 0x0007 (stock; 0x0017 means use_extclk is set)
scripts/m2sdr_rate_scan.py                    # ratio vs 2xFs must be 1.0000 at every rate
```

The rate scan is the sharpest check. On this gateware `DATA_CLK` is exactly
2x Fs in 1T1R -- 7.680, 15.360, 30.720, 61.440, 122.880 MHz -- exact, not
approximate. Anything else means the wrong gateware is loaded.

A clean TX run prints no `[CALDBG]` / `[BYPASSDBG]` lines at all. If it does, the
build predates the debug gating.
