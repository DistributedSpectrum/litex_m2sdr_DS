#!/usr/bin/env bash
#
# Install the LiteX-M2SDR software stack on a freshly imaged host.
#
# Installs dependencies, builds and installs the kernel driver, the userspace
# utilities and the SoapySDR module, then verifies the device comes up.
# Written against Raspberry Pi OS (Debian 13 trixie) on a Pi 5; the build steps
# are generic, only the package list is Debian-specific.
#
# Three traps this handles, each of which fails quietly if you do it by hand:
#
#   1. raspberrypi-kernel-headers does not exist on trixie. apt aborts the WHOLE
#      transaction on an unknown package, so including it installs nothing at all
#      while looking like a normal failure.
#   2. linux-headers-rpi-v8 pulls in a kernel upgrade. After the next reboot
#      uname -r changes, and a module built before it sits in the wrong
#      /lib/modules/<version>/ directory -- the PCIe device still enumerates but
#      there is no /dev/m2sdr0. Hence --driver-only, to rebuild after rebooting.
#   3. `make install` puts the Soapy module in /usr/local/lib/SoapySDR/modules0.8/,
#      which is NOT on SoapySDR's search path, so the rebuild appears to do
#      nothing. It is copied to the multiarch path explicitly.
#
# Usage: sudo scripts/m2sdr_provision_host.sh [--skip-packages] [--driver-only]
#
#   --skip-packages   build only, do not touch apt
#   --driver-only     rebuild and reload just the kernel module (use after a
#                     kernel upgrade + reboot)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SW="$REPO/litex_m2sdr/software"
KERNEL="$(uname -r)"
SOAPY_MODULE_DIR="/usr/lib/$(uname -m)-linux-gnu/SoapySDR/modules0.8"

SKIP_PACKAGES=0
DRIVER_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --skip-packages) SKIP_PACKAGES=1 ;;
        --driver-only)   DRIVER_ONLY=1 ;;
        *) echo "unknown option: $arg"; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "must run as root (sudo)"; exit 1; }

echo "repo   : $REPO"
echo "kernel : $KERNEL  (page size $(getconf PAGESIZE))"
if [ "$(getconf PAGESIZE)" != "4096" ]; then
    echo
    echo "WARNING: page size is not 4096. The m2sdr driver hardcodes"
    echo "         DMA_BUFFER_SIZE=8192 and needs 4K pages. On a Pi 5 set"
    echo "         kernel=kernel8.img in /boot/firmware/config.txt and reboot."
    echo
fi

# Packages -----------------------------------------------------------------------------------------

if [ "$SKIP_PACKAGES" -eq 0 ] && [ "$DRIVER_ONLY" -eq 0 ]; then
    echo "### installing packages ###"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    # NOTE: no raspberrypi-kernel-headers here -- see the header comment.
    apt-get install -y -q \
        build-essential cmake git pkg-config \
        linux-headers-rpi-v8 \
        gnuradio gnuradio-dev gr-osmosdr gqrx-sdr \
        soapysdr-tools libsoapysdr-dev libsoapysdr0.8 python3-soapysdr \
        soapysdr0.8-module-all \
        python3-numpy
    if [ ! -e "/lib/modules/$KERNEL/build/Makefile" ]; then
        echo
        echo "NOTE: headers for the RUNNING kernel ($KERNEL) are not installed, but a"
        echo "      newer kernel was probably just pulled in. Reboot, then re-run:"
        echo "          sudo $0 --driver-only"
        exit 1
    fi
fi

# Kernel driver ------------------------------------------------------------------------------------

echo "### kernel driver ###"
cd "$SW/kernel"
make clean >/dev/null 2>&1 || true
make -j"$(nproc)"
mkdir -p "/lib/modules/$KERNEL/updates"
cp m2sdr.ko "/lib/modules/$KERNEL/updates/"
xz -f "/lib/modules/$KERNEL/updates/m2sdr.ko"
depmod -a
echo "m2sdr" > /etc/modules-load.d/m2sdr.conf
echo 'KERNEL=="m2sdr*", MODE="0666"' > /etc/udev/rules.d/99-m2sdr.rules
udevadm control --reload-rules 2>/dev/null || true
modprobe -r m2sdr 2>/dev/null || true
modprobe m2sdr
[ -e /dev/m2sdr0 ] && echo "  /dev/m2sdr0 present" || { echo "  ERROR: no /dev/m2sdr0"; exit 1; }

if [ "$DRIVER_ONLY" -eq 1 ]; then
    echo "driver rebuilt for $KERNEL; stopping here (--driver-only)"
    exit 0
fi

# Userspace ----------------------------------------------------------------------------------------

echo "### userspace utilities ###"
cd "$SW/user"
make clean >/dev/null 2>&1 || true
make -j"$(nproc)"
echo "  built $(ls m2sdr_* 2>/dev/null | grep -vcE '\.(c|h|o|d|py)$') tools"

# SoapySDR -----------------------------------------------------------------------------------------

echo "### SoapySDR module ###"
cd "$SW/soapysdr"
rm -rf build && mkdir -p build && cd build
cmake .. >/dev/null
make -j"$(nproc)" >/dev/null
mkdir -p "$SOAPY_MODULE_DIR"
cp libSoapyLiteXM2SDR.so "$SOAPY_MODULE_DIR/"   # NOT `make install` -- see header
ldconfig
echo "  installed to $SOAPY_MODULE_DIR"

# Verify -------------------------------------------------------------------------------------------

echo "### verification ###"
"$SW/user/m2sdr_util" info 2>/dev/null | grep -iE "built on|FPGA DNA" | sed 's/^/  /' || true
if SoapySDRUtil --find 2>/dev/null | grep -q LiteXM2SDR; then
    echo "  SoapySDR: device found"
else
    echo "  SoapySDR: NOT found -- check $SOAPY_MODULE_DIR"
fi
echo
echo "done. Try:"
echo "  scripts/m2sdr_tx_tone.py --freq 1e9 --tone 1e6 --att 60 --secs 10"
echo "  scripts/m2sdr_rx_capture.py /tmp/rx.iq --freq 800e6"
