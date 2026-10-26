# LiteX-M2SDR on Jetson Orin / NVIDIA L4T

These notes cover the host-side pieces that are specific to Jetson Orin
systems running NVIDIA L4T.

## Kernel Module Build

Install the NVIDIA kernel headers that match the running L4T kernel:

```bash
sudo apt install nvidia-l4t-kernel-headers
```

Then build the LitePCIe kernel module from the repository:

```bash
cd litex_m2sdr/software/kernel
make
sudo make install
sudo modprobe litepcie
```

L4T installs headers under `/usr/src/linux-headers-$(uname -r)-ubuntu20.04_aarch64`
or a nearby NVIDIA-specific path depending on the release. If the module build
cannot find them automatically, pass the matching kernel build directory through
the `KERNEL_PATH=...` make variable.

## IOMMU / SMMU

The SMMU can stay fully enabled: current `m2sdr.ko` builds stream correctly
with SMMU address translation active (verified on JetPack 7 / L4T R39 with the
stock kernel command line, no `iommu.passthrough=1`). Older kernel-module
builds mapped the wrong pages for zero-copy DMA behind an IOMMU/SMMU, which
made SoapySDR RX return mostly/all-zero buffers and made IOMMU passthrough
look like a requirement on Jetson; that driver bug is fixed.

Do not add `arm-smmu.disable=1` as a default workaround; it disables the SMMU
globally and can break unrelated Jetson peripherals.

If you must run an older driver build and DMA streaming does not produce
samples even though PCIe enumeration works, IOMMU passthrough remains the
legacy workaround:

```bash
# /boot/extlinux/extlinux.conf
APPEND ... iommu.passthrough=1
```

Reboot after changing `extlinux.conf`. Keep a known-good boot entry or serial
console access when changing kernel command-line options.

## High-Rate Streaming

For sustained RX on Jetson:

- Configure the stream once with `m2sdr_sync_config()`.
- Use larger `m2sdr_sync_rx()` requests, for example about 2 ms of samples, to
  amortize syscall and scheduling overhead.
- Run the RX pump in its own thread and avoid blocking it on downstream
  processing or other slow operations.
- Hand completed buffers to a second thread or queue when additional work is
  needed.
- Watch `m2sdr_get_stream_stats()` or the utility diagnostics for RX overflow
  counters and ring high-water marks.

`m2sdr_sync_config()` still takes the fixed DMA descriptor size in samples. Use
larger per-call `m2sdr_sync_rx()` sizes for application batching instead of
changing the descriptor size.

## Linking Applications

The development install exports the public libm2sdr headers, the generated
kernel CSR headers, `pkg-config` metadata, and CMake package files:

```bash
pkg-config --cflags --libs m2sdr
```

or:

```cmake
find_package(m2sdr REQUIRED)
target_link_libraries(app PRIVATE m2sdr::m2sdr)
```

## Validated Setup: Jetson Orin Nano (DistributedSpectrum fork)

The Jetson Orin Nano developer kit (`litexjackson`, L4T kernel `5.15.148-tegra`,
15 W power mode) is the fork's main target. Everything below was measured on it
on 2026-10-20 with the M.2 card in the x4 Key-M slot.

### Build Gateware With LiteX >= fd69861c6

LiteX between `5b641f517` (2026-06-16) and `fd69861c6` (2026-07-07) generates
Verilog that synthesizes the LiteI2C PHY wrongly: every I2C data byte goes out
as `0xFF`, so the SI5351 is never programmed. Symptoms are indistinguishable
from a hardware clock fault: `m2sdr_util clk-test` reports `AD9361 Ref Clk` and
`Time Ref Clk` as `0.00`, the sample rate runs 1.17-1.36x fast and drifts, LO is
wrong, delay calibration fails, board time stays at 1970 and DMA never starts
without the synchronizer bypass. See [Clock Investigation](../clock-investigation.md).

After every build, check the generated Verilog:

```bash
./scripts/check_verilog_comb_order.py build/<name>/gateware/<name>.v   # must report 0
```

### x1 or x4

```bash
./litex_m2sdr.py --variant=m2 --with-pcie --pcie-gen=2 --pcie-lanes=4 --build              # recommended
./litex_m2sdr.py --variant=m2 --with-pcie --pcie-gen=2 --pcie-lanes=1 --with-pcie-ptm --build
```

The slot trains at Gen2 x4 (5 GT/s, Width x4). PTM is only supported on Gen2 x1,
and on this host the PTM PHC never registered, so x4 loses nothing: board time
runs from the SI5351 time reference either way.

| 122.88 MSPS 1T1R, wide-bandwidth | Gen2 x1 | Gen2 x4 |
|---|---|---|
| sc8 (1.97 Gbps needed) | 1.97 Gbps, 0 overflows | 1.97 Gbps, 0 overflows |
| sc16 (3.93 Gbps needed) | capped at ~3.32 Gbps, drops | 3.93 Gbps, 0 overflows |

The x4 build changes the generated kernel headers (no PTM CSRs), so rebuild and
reinstall `m2sdr.ko`, the user tools and the SoapySDR module after switching.
`make install` of the SoapySDR module writes `/usr/local/lib/SoapySDR/...`, but
SoapySDR on L4T loads modules from `/usr/lib/aarch64-linux-gnu/SoapySDR/modules0.8/`.

### Flashing and Power Cycling

With a Digilent JTAG-HS2 on the card's JTAG header (flash both images, then
read them back; a successful write is not proof):

```bash
openFPGALoader --cable digilent_hs2 --fpga-part xc7a200tsbg484 --write-flash --offset 0x0      <name>_fallback.bin
openFPGALoader --cable digilent_hs2 --fpga-part xc7a200tsbg484 --write-flash --offset 0x800000 <name>_operational.bin
openFPGALoader --cable digilent_hs2 --fpga-part xc7a200tsbg484 --dump-flash  --offset 0x800000 --file-size $(stat -c %s <name>_operational.bin) readback.bin
```

The card must be configured before the Jetson enumerates PCIe, so a new image
needs a full power cycle, not a reboot. Run `sync` first: a power cycle right
after rebuilding the user tools left `m2sdr_util`, `m2sdr_rf` and
`m2sdr_record` as 0-byte files that silently print nothing.

Healthy board checks:

```bash
m2sdr_util info        # SI5351 Device Status 0x11, PLLs locked; PCIe Lanes x4
m2sdr_util clk-test    # AD9361 Ref Clk 38.40, Time Ref Clk 100.00 (MHz)
m2sdr_rf --channel-layout 1t1r --sample-rate 30.72e6 && m2sdr_util clk-test   # Dat Clk 61.44
```

At 735 MHz (LTE) the PSDs at 30.72 and 61.44 MSPS line up with a bladeRF 2.0 on
the same antenna, carrier edge for carrier edge. 122.88 MSPS wide-bandwidth mode
aligns on the first try (one BBF tune per direction, DATA_CLK 245.76 MHz).

### Recording 122.88 MSPS to NVMe Without Overflows

`/dev/null` runs are clean at twice the sc8 rate, yet writing to the NVMe drops
buffers: the driver ring is 256 x 8 KB (~8.5 ms at 245.76 MB/s) and page-cache
writeback bursts stall the reader. Over 60 s at 122.88 MSPS sc8:

| setup | new overflows |
|---|---|
| plain `m2sdr_record FILE` | 476 (~4.7% of buffers lost) |
| `--host-buffers 65536` (512 MB writer queue) | 26 |
| + `sudo chrt -f 50` | 6 |
| + small dirty-page thresholds (below) | **0** |

```bash
sudo sysctl vm.dirty_background_bytes=67108864 vm.dirty_bytes=268435456   # runtime only, redo after boot
m2sdr_rf --channel-layout 1t1r --sample-rate 122.88e6 --rx-freq 735e6 --rx-gain-mode slow --format sc8
sudo chrt -f 50 m2sdr_record --host-buffers 65536 --format sc8 --sigmf \
    --sample-rate 122880000 --center-freq 735000000 --nchannels 1 capture 14745600000
mv capture capture.sigmf-data   # the .sigmf-meta already names it so
```

`m2sdr_record`'s overflow counters are cumulative since boot; compare against a
reading taken before the capture. `jetson_clocks` makes no difference in the
15 W mode (the CPU is already at its 1.42 GHz cap).

#### 16-bit (sc16, 491.5 MB/s): `m2sdr_record --direct`

The default path copies every sample several times (ring -> recorder -> page cache -> disk). In the
15 W mode that is CPU-bound at sc16, even though the NVMe sustains 1.2 GB/s of direct writes. Over
30 s at 122.88 MSPS sc16, with a 2048-buffer ring:

| setup | overflow events |
|---|---|
| plain `m2sdr_record FILE` | 879 (~1/3 of the data lost) |
| + the sc8 tuning above | 808 |
| `m2sdr_record - \| dd bs=8M oflag=direct` | 930 |
| `m2sdr_record --direct FILE` | **0** (max write 7.4 ms, peak backlog ~730 buffers) |

`--direct` (`m2sdr_rx_stream_to_fd()` in libm2sdr) takes buffers straight from the zero-copy DMA ring,
copies runs of 256 into a 256 MiB RAM queue (the one copy; O_DIRECT cannot read the PFN-mapped ring)
and a writer thread writes them with O_DIRECT. Disk stalls fill the queue instead of the DMA ring:
on a KingSpec XG7000 1 TB (MAXIO MAP1602, Gen4 x1 through the E-key slot), 100 GB had 89 overflows
before the queue (write stalls up to 38 ms) and 0 with it (stalls up to 36.5 ms, queue peak 18 MB).
A 350 GiB fio run on the same drive (2 MiB O_DIRECT writes, one at a time) averaged 963 MB/s with no
write-cache cliff and never fell below 690 MB/s in any second, but single writes stalled for up to
141 ms (p99.99 95 ms): more than the DMA ring can hold, well within the queue.
The original PNY CS2142 cannot sustain the rate past its ~38 GB write cache (~85 MB/s after). It needs no sysctl, `chrt` or `--host-buffers`. A run with `--enable-header` checked
all 1.8 M buffer timestamps: no gaps or repeats. Its peak backlog is past the old 256-buffer ring's
128-buffer overflow budget, so build the gateware with `--pcie-dma-table-depth=2048`.

```bash
m2sdr_rf --channel-layout 1t1r --sample-rate 122.88e6 --rx-freq 735e6 --rx-gain-mode slow --format sc16
m2sdr_record --direct --format sc16 capture.bin 14745600000   # 30 s
```

### Full-Range Sweeps

`scripts/m2sdr_sweep.py` covers 70 MHz-6 GHz at 122.88 MSPS 16-bit in 79
chunks (80 MHz kept, 5 MHz overlap): 0.96 s at a fixed gain, about 2.1-2.5 s with
slow AGC (`--agc --agc-wait-ms 20`), 0 overflows and gaps. Stitch with
`scripts/m2sdr_sweep_stitch.py`. The script's docstring explains why each chunk
restarts the stream and bypasses the PPS start gate.
