# Running LiteX-M2SDR on Raspberry Pi 5

Below is the [LiteX-M2SDR](../..) installed on a [Raspberry Pi 5](https://www.raspberrypi.com/products/raspberry-pi-5/).
Please follow the step-by-step tutorial below to get your SDR running.

![Connecting LiteX-M2SDR to the Raspberry Pi 5](https://github.com/user-attachments/assets/7205b4a2-814d-4db2-87c0-573993c92a3e)
![Raspberry Pi 5 Max from the CPU side](https://github.com/user-attachments/assets/4f821f79-f274-4270-8642-5e7e556e8577)

To connect the LiteX-M2SDR on the Raspberry Pi 5 board, you need an M.2 hat like the [Pineboards HatDrive BM1L](https://pineboards.io/products/hatdrive-bottom-2230-2242-2280-for-rpi5).

## Raspberry Pi OS

### Create MicroSD Card Image

This step requires **rpi-imager**. On Debian or similar OS, use this command to install the tool:
```bash
sudo apt install rpi-imager
```

Once installed, launch the tool with the following command (*root* user is required for write access to mass storage):
```bash
sudo rpi-imager
```

![rpi-imager main page](https://github.com/user-attachments/assets/beb10486-4150-470f-8172-45ab108e9808)

1. Click on **CHOOSE DEVICE** and select *Raspberry Pi 5*.
   ![Choose device](https://github.com/user-attachments/assets/54af5242-127c-4fe0-8d7b-01def2dc58a2)
2. Click on **CHOOSE OS** and select *Raspberry Pi OS (64-bit)*.
   ![Choose OS](https://github.com/user-attachments/assets/c9afdcec-8a2b-4794-8363-033dc3a2c4be)
3. Click on **CHOOSE STORAGE** and select the SD card (use `sudo fdisk -l` to list mass storage devices).
4. Click **NEXT**.

On the next page, *rpi-imager* allows OS customization:

![Image](https://github.com/user-attachments/assets/f81200fa-305a-4c13-b2a0-d0e958e3bca7)

If you plan to use SSH initially, click on **EDIT SETTINGS**,
go to the **GENERAL** pane to set the username and password:
![Image](https://github.com/user-attachments/assets/528a0460-79bf-48b9-ab02-f3cd50a2fce6)
and enable SSH in the **SERVICES** pane:
![Image](https://github.com/user-attachments/assets/9ed3a6b7-84dc-40a8-91b0-198ca10284c9)

After configuration, click **SAVE** and then **YES**.

The next page is the last point before erasing the device: double-check the selected mass storage.

Now, **rpi-imager** will download the image and write it to the SD card.

### Tweaking the System

Once *rpi-imager* has written the SD card, some modifications must be applied to use a PCIe device. Mount the
first SD card partition and edit *config.txt* and *cmdline.txt*. Both files are in the *firmware* directory.

**config.txt**
In the `[all]` section, please add:

```bash
kernel=kernel8.img
dtparam=pciex1=on
dtoverlay=pcie-32bit-dma-pi5
dtoverlay=pciex1-compat-pi5,l1ss=off,no-l0s=on,no-mip=off
# Run as fast as firmware/board allows
arm_boost=1
```

- `kernel8.img`: ensures a standard 4K PAGE SIZE instead of 16K.
- `dtparam=pciex1=on`: enables PCIe support.
- `dtoverlay=pcie-32bit-dma-pi5`: enables 32-bit DMA addressing instead of 64-bit.
- `dtoverlay=pciex1-compat-pi5,l1ss=off,no-l0s=on,no-mip=off`: improves support for PCIe devices that may not work well with the default
  configuration; `l1ss=off` disables a power-saving feature, `no-l0s=on` disables L0s (Active State Power Management - L0s state), and `no-mip=off`
  enables MIP (Maximum Payload Size Inference).

**cmdline.txt**
Please append the line with `pci=noaer pcie_aspm=off`:

- `pci=noaer`: disables PCIe advanced error reporting (`AER: Corrected error received`).
- `pcie_aspm=off`: ignores PCIe Active State Power Management. Leaves any configuration done by firmware unchanged.

If the board enumerates but DMA streaming does not produce samples, first make
sure the `m2sdr.ko` build matches this source tree (older builds mapped the
wrong pages for zero-copy DMA behind an active IOMMU/SMMU); with an older
driver, adding `iommu.passthrough=1` and rebooting is the legacy workaround.
Avoid `arm-smmu.disable=1` as a default setting; it disables the SMMU globally
and should only be used for controlled bring-up if passthrough mode is not
enough.

### Wiring Up and Running Your Raspberry Pi Board

Please plug in your Ethernet cable, HDMI monitor, USB keyboard, and the MicroSD card generated earlier.
You can power your Raspberry Pi now over the USB-C port using a 5V@4A power supply.

## First Login and System Update

Log in over SSH using the assigned IP and your password:
```bash
ssh toto@XXX.YYY.Z.A

# Update the system
sudo apt update
sudo apt dist-upgrade
sudo reboot
```

### Installing LiteX-M2SDR on Raspberry Pi

Insert the LiteX-M2SDR module before powering your Raspberry Pi.
You’ll need an M2x3mm screw to lock the SDR module in place.

![LiteX-M2SDR on Raspberry Pi 5 M.2 HAT](https://github.com/user-attachments/assets/a149be27-aaa7-42cd-9eca-9a02b3f91de8)

```bash
# Log in over SSH using your IP and password
ssh toto@XXX.YYY.Z.A

# Install missing dependencies
sudo apt install -y cmake soapysdr-tools libsoapysdr0.8 libsoapysdr-dev

# On the Raspberry Pi, clone the LiteX-M2SDR source tree
mkdir Code
cd Code
git clone https://github.com/enjoy-digital/litex_m2sdr

# Move to the software directory
cd litex_m2sdr/litex_m2sdr/software

# Compile the command line tools and the SoapySDR module for Litex-M2SDR
./build.py

# Build and Install the LiteX-M2SDR Linux kernel driver for arm64
cd kernel
make ARCH=arm64 clean all
sudo make ARCH=arm64 install
cd ..

# Verify the list of compiled tools
git status

# If you want to use LiteX-M2SDR Python tools later
sudo apt install -y python3-numpy
```

### Using LiteX-M2SDR on Raspberry Pi 5

```bash
user/m2sdr_util info
user/m2sdr_rf
```

For a short RX smoke test, tune to a local signal and record a bounded capture:

```bash
user/m2sdr_rf --sample-rate=5e6 --rx-freq=102.4e6 --rx-gain=30
user/m2sdr_record /tmp/m2sdr-rx.iq 67108864
```

The shared GNU Radio examples are under `litex_m2sdr/software/gnuradio/`.
After the SoapySDR module is installed, `test_fm_rx.grc` provides an FM
receiver flowgraph that can be used as a Raspberry Pi 5 end-to-end receive
test.

On subsequent boots, the LiteX-M2SDR driver is automatically loaded:
```bash
# Check whether it works
~/Code/litex_m2sdr/litex_m2sdr/software/user/m2sdr_util info
```

Example LiteX session:
![Running LiteX-M2SDR Tools on Raspberry Pi 5](https://github.com/user-attachments/assets/19198ebe-3898-4209-a208-2ebb1a2417c2)

## Troubleshooting (DistributedSpectrum fork additions)

The following issues were hit and root-caused while bringing up a Pi 5 + Pineboards HatDrive M.2 HAT from scratch. All three are config/driver issues on the host side, not board defects.

### Ubuntu Server doesn't ship the `pciex1-compat-pi5` overlay

This tutorial uses Raspberry Pi OS specifically because Ubuntu Server's arm64+raspi firmware package (as of 24.04.4) only ships `pcie-32bit-dma-pi5`/`pcie-32bit-dma`, not `pciex1-compat-pi5`. Without that overlay's `no-mip` option, driver probe fails outright:

```
m2sdr 0001:01:00.0: arch assigned 64-bit MSI address 0xffffffe000 but device only supports 32 bits
m2sdr 0001:01:00.0: Failed to enable MSI
m2sdr: probe of 0000:01:00.0 failed with error -5
```

The M2SDR's PCIe MSI capability is 32-bit-only, but BCM2712's PCIe root complex on the `pciex1` HAT slot defaults to targeting its 64-bit MSI-MIP peripheral. If you must use Ubuntu, fetch/compile `pciex1-compat-pi5-overlay.dts` from `raspberrypi/linux` yourself; otherwise use Raspberry Pi OS as above, which ships it natively.

**Important:** despite this doc's example (`dtoverlay=pciex1-compat-pi5,l1ss=off,no-l0s=on,no-mip=off`), you need **`no-mip=on`**, not `off`, to fix this specific symptom. `no-mip` means "stop targeting the MSI-MIP peripheral," so it must be explicitly turned on for a 32-bit-only-MSI device like the M2SDR. Use:

```
dtoverlay=pciex1-compat-pi5,l1ss=off,no-l0s=on,no-mip=on
```

### `kernel=kernel8.img` is required for zero-copy DMA, not just a page-size nicety

The `config.txt` snippet above lists `kernel=kernel8.img` with the note "ensures a standard 4K PAGE_SIZE instead of 16K" — this reads as cosmetic but is actually load-bearing. Raspberry Pi OS arm64 defaults to a 16K-page kernel (`+rpt-rpi-2712` flavor) unless this line forces the 4K-page `+rpt-rpi-v8` flavor instead. The kernel driver's zero-copy mmap path has its own sanity check:

```c
if (DMA_BUFFER_SIZE % PAGE_SIZE)
    return -EINVAL;
```

`DMA_BUFFER_SIZE` is a compile-time `8192` (`kernel/config.h`). On a 16K-page kernel, `8192 % 16384 != 0`, so this check trips immediately and `m2sdr_util --zero-copy dma-test` fails with `MMAP failed` / dmesg `mmap failed for buffer 0 (ret=-22)` before any DMA is attempted. Do not skip `kernel=kernel8.img` — confirm with `getconf PAGE_SIZE` (should read `4096`) before debugging anything else DMA-related. If you change this after already building the kernel module, rebuild it against the new kernel flavor (`uname -r` will show `+rpt-rpi-v8` instead of `+rpt-rpi-2712`).

### ARM/aarch64 zero-copy DMA needed a cache-coherency fix (included in this fork)

Independent of the two issues above, this fork's `kernel/main.c` includes a fix for a real, upstream-unaddressed bug: the ARM/aarch64 branch of `litepcie_mmap()` never received the `dev_is_dma_coherent()`/`pgprot_dmacoherent()` handling that the non-ARM path got upstream (see commits `8dd2a02`, `f0f62d7`, `422d630`, `e0404f1`). It mapped coherent DMA buffers with a plain cached `remap_pfn_range()`, relying on a comment telling userspace to "explicitly flush the CPU caches" — something neither the kernel driver nor any userspace tool actually does.

On a Pi 5, the PCIe host bridge has no `dma-coherent` devicetree property (check via `find /sys/firmware/devicetree/base/axi -iname 'pcie@*'` and inspect the node), meaning its DMA is genuinely not hardware-cache-coherent — exactly the condition the missing fix needs to handle. Without it, zero-copy DMA silently maps stale/incoherent memory into userspace. This fork's `litepcie_mmap()` unifies the ARM and non-ARM paths through the same coherency-aware helpers, using `remap_pfn_range()` (not `vm_insert_page()`, which fails with `-EINVAL` against the `PG_reserved`, zero-refcount pages that non-cache-coherent platforms' `dma_alloc_coherent()` typically returns).

With all three fixes applied, `m2sdr_util --duration 10 --zero-copy dma-test` sustains ~2.9-3.6 Gbps with 0 errors across 460,000+ buffers on a Pi 5.

### Dual M.2 HAT: the SDR and an NVMe share one PCIe uplink

With a two-slot HAT (tested: Seeed dual M.2) both devices sit behind an ASMedia ASM2806 switch whose upstream to the Pi is a single x1 link. The kernel says so at boot:

```
pci 0001:01:00.0: 7.876 Gb/s available PCIe bandwidth, limited by 8.0 GT/s PCIe x1 link at 0001:00:00.0
```

That is roughly **984 MB/s shared between the NVMe and the SDR**, in both directions combined. Streaming an IQ file from the NVMe to TX therefore spends the budget twice — once reading, once writing. At 30.72 MSPS 2T2R that is ~216 MB/s of PCIe plus ~108 MB/s of disk and fits comfortably; 61.44 MSPS 2T2R (~492 + ~246 MB/s) still fits; 122.88 MSPS 2T2R does not.

Config used, in the `[all]` section of `/boot/firmware/config.txt`:

```
kernel=kernel8.img                  # REQUIRED: driver hardcodes DMA_BUFFER_SIZE=8192, needs 4K pages
dtparam=pciex1
dtparam=pciex1_gen=3
dtoverlay=pcie-32bit-dma-pi5
dtoverlay=pciex1-compat-pi5,l1ss=off,no-l0s=on,no-mip,mmio-hi
dtoverlay=pcie-fix
```

plus `pci=pcie_bus_safe pci=noaer pcie_aspm=off pcie_port_pm=off` appended to `cmdline.txt`. `pcie-fix.dtbo` is not in Raspberry Pi OS; fetch it from the HAT vendor and drop it in `/boot/firmware/overlays/`.

Note `pciex1_gen=3` is what the HAT vendor specifies but is not a certified Pi 5 configuration. If the link becomes unreliable, `pciex1_gen=2` is the first thing to try.

### The whole HAT can latch off the bus, and only a power cycle clears it

Twice during bring-up the entire PCIe domain disappeared — SDR and NVMe both — with:

```
brcm-pcie 1000110000.pcie: link down
```

**Warm reboots do not recover this.** Power must actually be removed. A related but distinct failure is the SDR alone going missing while the switch is fine; there the downstream port reports `PresDet-` and `EqualizationComplete-`, which means the FPGA never finished configuring and so never presented receiver termination. That one usually is recoverable with a warm reboot, and is worth checking against the gateware build string afterwards — if it comes back reporting an older build, multiboot fell back to the golden image at flash offset 0 because the operational image at `0x800000` failed to load.

### Two apt traps when installing the toolchain

`raspberrypi-kernel-headers` does not exist on Debian 13 (trixie). apt aborts the entire transaction on an unknown package, so including it installs **nothing at all** while looking like a routine failure. The correct package is `linux-headers-rpi-v8`.

That package in turn **pulls in a newer kernel**. After the next reboot `uname -r` changes, and a module built before it is left in the wrong `/lib/modules/<version>/` directory. The symptom is confusing: the PCIe device enumerates normally, `lspci` shows it, and there is simply no `/dev/m2sdr0`. Rebuild the driver after rebooting onto the final kernel.

`scripts/m2sdr_provision_host.sh` handles all of this, including `--driver-only` for the post-reboot rebuild.

### Mounting an NVMe for capture storage — use `nofail`

```
UUID=<uuid>  /data  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2
```

`nofail` is not optional on this hardware. Given the HAT can drop off the bus entirely (above), a plain fstab entry leaves the Pi hanging at boot waiting for a device that is not on the bus. Use the UUID rather than `/dev/nvme0n1p1` so enumeration order cannot matter.

### Cloning gateware between two boards

To get two cards onto a byte-identical image, copy the flash rather than rebuilding — the operational image lives at `0x800000`:

```bash
# on the known-good board
m2sdr_util flash-read /tmp/op.bin 4194304 0x800000
# trim the trailing 0xFF erase pad, then on the target board
m2sdr_util -y flash-write op_clone.bin 0x800000
sudo reboot
```

Sanity-check the read before writing it anywhere: a Xilinx bitstream has the sync word `AA995566` within the first ~64 bytes. Confirm afterwards with `m2sdr_util info`, which reports the build timestamp.

Identify boards by **FPGA DNA** (`m2sdr_util info`), not by which host they are plugged into — cards get swapped between chassis and the DNA is the only thing that follows the card.

## Buildroot

*TBD*
