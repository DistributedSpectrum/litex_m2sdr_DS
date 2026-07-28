# Running LiteX-M2SDR on Hardkernel Odroid M1

Below is the [LiteX-M2SDR](../..) installed in the M.2 slot of a [Hardkernel Odroid M1](https://www.hardkernel.com/shop/odroid-m1/) (Rockchip RK3568, ARM64). This board is more involved to bring up than the Raspberry Pi 5 or OrangePi 5 Max: the M.2 slot's PCIe controller has host-side quirks that need explicit workarounds, and one gateware-side regression window is called out below. Read the whole Troubleshooting section before assuming a fresh symptom is something new.

Two kernels are viable:
- **Hardkernel's vendor BSP** (Ubuntu 24.04 image, kernel `6.1.0-rockchip-odroid-arm64`, `rk-pcie` driver).
- **Armbian mainline** (`*-current-rockchip64`, `rockchip-dw-pcie` driver).

Both work once the fixes below are applied; the vendor BSP is the path of least resistance.

## Flashing the eMMC/SD image

Use Hardkernel's official image (or Armbian's, per your preference) and flash it with `dd`/`balenaEtcher` as usual. No special partitioning is required beyond what the vendor image already provides.

## Required boot fix: remove `pci=nomsi`

Both Hardkernel's vendor BSP and Armbian mainline images ship with `pci=nomsi` in the default kernel command line (set via `flash-kernel`'s `ubootenv.d` fragments, typically `/usr/share/flash-kernel/ubootenv.d/{vendor,upstream}/90-misc`). This unconditionally disables MSI for all PCIe devices system-wide, and the M2SDR kernel driver has no legacy INTx fallback — with `pci=nomsi` present, driver probe fails outright:

```
m2sdr: Failed to enable MSI
m2sdr: probe of ... failed with error -22
```

Remove `pci=nomsi` from the relevant fragment(s), regenerate the boot script (`sudo u-boot-update` or `sudo update-bootscript`, depending on image), and reboot. This is required on **both** kernels — it is not Armbian- or vendor-BSP-specific.

## Required gateware fix: `--with-pcie-reset-workaround`

Stock factory-flashed bitstreams (as of the `2026_05_15` GitHub release) do not enable `PCIeLinkResetWorkaround` (`litex_m2sdr/gateware/pcie.py`). Without it, PCIe link training can fail to recover from a single failed LTSSM attempt on this host's root complex — symptomatic as `Phy link never came up` on Armbian mainline (`rockchip-dw-pcie`), persisting even with an artificially extended `PCIE_LINK_WAIT_MAX_RETRIES` kernel patch (confirmed not a boot-timing race).

Root cause is gateware-side, not host/kernel-side: a different PCIe endpoint (a WD SN750 NVMe SSD) trains perfectly in the same M.2 slot, proving the RC/PHY/slot are healthy. Build with the reset-workaround enabled:

```bash
./litex_m2sdr.py --variant=m2 --with-pcie --with-pcie-reset-workaround --build
```

Flash the resulting `_fallback.bin`/`_operational.bin` pair to offsets `0x0`/`0x800000` via `m2sdr_util flash-write` (PCIe-based, once any working link is available — even a JTAG-loaded volatile SRAM configuration is enough to get one working boot for the initial flash). This fixes PCIe link training on **both** Armbian mainline and the vendor BSP, and was also observed to make MSI interrupts fire reliably where they previously allocated but never fired (0 interrupts forever) on the vendor kernel — though see the note below on card-to-card variance for that specific symptom.

## Known issue: 2-lane (x2) builds do not reliably link

The Odroid M1's M.2 slot is documented by Hardkernel as PCIe Gen3 x2 capable, and `--pcie-lanes=2` builds do complete successfully, but link training behavior varies by kernel and even by build:
- On the vendor kernel (`rk-pcie`), an x2 build has been observed to either negotiate down to Gen2 x1 (root-port-limited, functional) or fail to link entirely (`PCIe Link Fail, LTSSM is 0x3, hw_retries=0/1`, `dw-pcie: probe ... failed with error -22`) depending on the exact gateware commit.
- On Armbian mainline (`rockchip-dw-pcie`), x2 builds have not been observed to link at all.

This is not resolved. If you need 2-lane operation on this host, expect to debug further or fall back to `--pcie-lanes=1`, which is fully reliable on both kernels once the above two fixes are applied.

## Known issue: MSI-never-fires symptom traced to a specific physical card, not the platform

Early in bring-up, one physical M2SDR unit showed MSI vectors allocated (`1 MSI IRQs allocated`) but exactly 0 interrupts ever firing (`/proc/interrupts` staying at 0 indefinitely, hanging any DMA test) on the vendor kernel. This looked like a Rockchip-PCIe-MSI-routing limitation and consumed significant investigation before being resolved.

**It was not a platform bug.** A second, physically different M2SDR card — running its own unmodified factory bitstream, on the exact same host/kernel/cmdline configuration that gave the first card 0 interrupts forever — produced real interrupts immediately (70,000+ firing during a DMA test). Whether the first card's issue was a marginal hardware defect, ESD from extensive JTAG probe handling, or something else was never conclusively identified. If you hit "MSI allocated but never fires" on this platform, don't assume it's unfixable in software — try a different physical card, and don't skip the `pci=nomsi`/reset-workaround fixes above regardless (both are required and independently confirmed to matter).

## Known issue: always use `--zero-copy` for any throughput or correctness testing on this host

The default (non-zero-copy) DMA path is dramatically slower on this SoC than on more powerful hosts — expect ~0.03-0.07 Gbps, one to two orders of magnitude below what `m2sdr_util`'s own `autotest.py` expects (`DMA_LOOPBACK_SPEED_THRESHOLD`), and below what other users report on the same non-zero-copy path on different (e.g. x86) hosts. At these speeds, the DMA test's producer/consumer ring buffers fall out of sync with the PN-sequence data checker, manifesting as a large, suspiciously consistent "data error" count (~430-450k) that is **not** real bit-level corruption — it's a timing/synchronization artifact of the slow path, not a hardware or gateware defect.

Always pass `--zero-copy` (`-z`) when testing DMA throughput or correctness on this host:

```bash
m2sdr_util --zero-copy dma-test
```

This resolves both symptoms simultaneously: throughput jumps to 3+ Gbps and the spurious error count disappears (0 errors across hundreds of thousands of buffers). Note that the project's own `autotest.py` does **not** pass `--zero-copy` by default — its results on this host should not be trusted for throughput/error conclusions without that flag. See `litex_m2sdr/software/autotest.py`'s `verify_pcie_speed()` for an unrelated PCIe-domain-path bug also fixed in this fork (affects any host where `lspci` reports a non-default PCIe domain, e.g. this Rockchip SoC's `0002:21:00.0`).

## Known issue (regression window identified, not yet bisected further): AD9361 SPI broken between two known points

`AD9361 Presence: No` (SPI reads returning stuck/garbage patterns) has been observed on custom builds from recent git history, while the exact same physical hardware works (`AD9361 Presence: Yes, Product ID: 000a`) when flashed with either of these two known-good points:
- The `2026_05_15` GitHub release (commit `eabdeb5b931d802d9b0b719e5e7758c307030611`, plain build, no reset-workaround/no oversampling).
- Commit `7b051df` (2026-06-10, includes the reset-workaround and RFIC-oversampling auto-detection), confirmed working via a Raspberry Pi 5 host (see [raspberry-pi-5.md](raspberry-pi-5.md)) since this host's own x2-lane link issues prevented testing directly here.

This rules out hardware damage (confirmed via clean single-variable swaps on the same physical card) and confirms a genuine, bisectable gateware/software regression somewhere between `7b051df` and current `HEAD`. If you hit `AD9361 Presence: No` on a recent build, don't assume board damage — check whether your build predates or postdates this window first.

## Recovering a board with a non-linking bitstream (JTAG)

If a bad flash leaves the board's PCIe endpoint unable to link at all (so `m2sdr_util flash-write` over PCIe isn't an option), JTAG via Vivado Hardware Manager is the fallback — the M2SDR's JTAG header is independent of the M.2 slot's PCIe link and can be powered/probed standalone (e.g. via a USB-C-to-PCIe-power adapter) without needing the Odroid at all. A genuine Xilinx Platform Cable (not a generic FTDI-compatible probe) is required; `openFPGALoader` and the project's own `create_programmer()` do not support it, so drive Vivado's Hardware Manager directly (`open_hw_manager`/`connect_hw_server`/`open_hw_target`/`program_hw_devices` for a volatile SRAM load, or `create_hw_cfgmem`/`program_hw_cfgmem` for a permanent flash write). Note that direct JTAG SPI-flash programming may fail with "Failure to set flash parameters" across every standard SPI part name tried — this is a known, unresolved JEDEC-ID-matching problem specific to this board's SPI wiring/Vivado's generic bridge bitstream; a volatile SRAM load followed by a PCIe-based `flash-write` (once the SRAM-loaded bitstream gives you a working link) is the reliable path to a permanent fix.
