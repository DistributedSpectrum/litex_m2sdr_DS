#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Fast full-range RX sweep through SoapySDR.

Configures the stream once (default 122.88 MSPS wide-bandwidth, 16-bit, 1T1R),
then retunes with setFrequency() and keeps the centre --keep span of each chunk,
stepping by --keep minus --overlap. Stitch the result with m2sdr_sweep_stitch.py.

- The stream is stopped around each retune so every chunk starts from an empty
  DMA ring: Python reads one ~2044-sample buffer per readStream() call at about
  the rate the card produces them, so it can never drain a backlog.
- RX start is gated to the next PPS edge by the kernel (it re-arms the PCIe DMA
  synchronizer on every start), costing up to 1 s per chunk; the synchronizer
  bypass CSR is written right after each activateStream() to start immediately.
- Samples are kept only from hardware timestamps after the retune settled, and
  each chunk is checked to be contiguous.

Measured on a Jetson Orin Nano, Gen2 x4: 70 MHz-6 GHz in 79 chunks takes 0.96 s
with a fixed gain and 2.1-2.5 s with slow AGC and a 20 ms wait, 0 overflows/gaps.
Raw chunks plus per-step timing and gains are written to an .npz.
"""

import argparse, ctypes, os, re, time
import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16, SOAPY_SDR_OVERFLOW

SOFTWARE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "litex_m2sdr", "software")

def csr_addr(name, csr_h=os.path.join(SOFTWARE_DIR, "kernel", "csr.h")):
    m = re.search(r"#define\s+" + name + r"\s+(0x[0-9a-fA-F]+)", open(csr_h).read())
    if not m:
        raise SystemExit(f"{name} not found in {csr_h}")
    return int(m.group(1), 16)

p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
p.add_argument("--start", type=float, default=70e6)
p.add_argument("--stop", type=float, default=6000e6)
p.add_argument("--rate", type=float, default=122.88e6)
p.add_argument("--keep", type=float, default=80e6, help="usable span kept per chunk")
p.add_argument("--overlap", type=float, default=5e6)
p.add_argument("--nsamp", type=int, default=65536, help="samples kept per chunk")
p.add_argument("--settle-us", type=float, default=200.0)
p.add_argument("--gain", type=float, default=50.0, help="manual RX gain (ignored with --agc)")
p.add_argument("--agc", action="store_true", help="slow-attack AGC instead of manual gain")
p.add_argument("--agc-wait-ms", type=float, default=20.0, help="AGC settle time after each retune")
p.add_argument("--out", default="/dev/shm/m2sdr_sweep.npz")
p.add_argument("--repeat", type=int, default=1, help="back-to-back sweeps on one stream setup")
p.add_argument("--lib", default=os.path.join(SOFTWARE_DIR, "user", "libm2sdr", "libm2sdr.so.1"),
               help="libm2sdr used for the synchronizer-bypass CSR write")
p.add_argument("--device", default="pcie:/dev/m2sdr0")
a = p.parse_args()

half = a.keep / 2
step = a.keep - a.overlap
fcs = list(np.arange(a.start + half, a.stop - half, step))
if fcs[-1] < a.stop - half:
    fcs.append(a.stop - half)

SYNC_BYPASS = csr_addr("CSR_PCIE_DMA0_SYNCHRONIZER_BYPASS_ADDR")  # re-armed to PPS on every RX start
lib = ctypes.CDLL(a.lib)
csr = ctypes.c_void_p()
if lib.m2sdr_open(ctypes.byref(csr), a.device.encode()) != 0:
    raise SystemExit(f"m2sdr_open({a.device}) failed")

sdr = SoapySDR.Device({"driver": "LiteXM2SDR", "bitmode": "16"})
st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0])  # 1 channel -> 1T1R before the wide bring-up
sdr.setSampleRate(SOAPY_SDR_RX, 0, a.rate)
sdr.setGainMode(SOAPY_SDR_RX, 0, a.agc)  # True = slow-attack AGC (driver default AGC mode)
if not a.agc:
    sdr.setGain(SOAPY_SDR_RX, 0, a.gain)
mtu = sdr.getStreamMTU(st)
nbuf = -(-a.nsamp // mtu) + 1
raw = np.empty(2 * mtu * nbuf, np.int16)

ns_per_samp = 1e9 / a.rate
GAP_TOL_NS = 100  # board time ticks at 10 ns with a few ticks of jitter; a lost buffer is ~16.6 us
chunks = np.empty((len(fcs), a.nsamp), np.complex64)

def read(off=0):
    sr = sdr.readStream(st, [raw[2 * off:]], mtu, timeoutUs=200000)
    return sr.ret, sr.timeNs

ms = lambda v: f"mean {1e3*np.mean(v):.2f} / max {1e3*np.max(v):.2f} ms"
sweep_times = []
for rep_i in range(a.repeat):
    t_tune, t_flush, t_cap, n_disc, n_ovf, n_restart = [], [], [], [], [], []
    t_agc, g_pre, g_post = [], [], []
    t_sweep0 = time.perf_counter()
    for i, fc in enumerate(fcs):
        t0 = time.perf_counter()
        sdr.deactivateStream(st)
        sdr.setFrequency(SOAPY_SDR_RX, 0, fc)
        ta = time.perf_counter()
        if a.agc:
            time.sleep(a.agc_wait_ms / 1e3)  # AGC keeps running with the DMA stopped
        t_agc.append(time.perf_counter() - ta)
        g_pre.append(sdr.getGain(SOAPY_SDR_RX, 0))
        sdr.activateStream(st)
        lib.m2sdr_reg_write(csr, SYNC_BYPASS, 1)
        t1 = time.perf_counter()
        t_ready = sdr.getHardwareTime() + int(a.settle_us * 1e3)
        disc = ovf = restart = 0
        # Drop everything captured before the retune settled.
        while True:
            ret, tns = read()
            if ret == SOAPY_SDR_OVERFLOW:
                ovf += 1; continue
            if ret <= 0:
                continue
            if tns >= t_ready:
                break
            disc += ret
        t2 = time.perf_counter()
        # Collect nsamp contiguous samples straight into raw[] (timestamp-checked);
        # the block that ended the discard loop is already at raw[0].
        got, expect = ret, tns + int(round(ret * ns_per_samp))
        while got < a.nsamp:
            r, t = read(got)
            if r == SOAPY_SDR_OVERFLOW:
                ovf += 1; restart += 1
                got, expect = 0, None
                continue
            if r <= 0:
                continue
            if expect is not None and abs(t - expect) > GAP_TOL_NS:
                # Gap: restart with this block as the first one.
                restart += 1
                raw[:2 * r] = raw[2 * got:2 * (got + r)]
                got = 0
            elif expect is None:
                raw[:2 * r] = raw[2 * got:2 * (got + r)]
                got = 0
            got += r; expect = t + int(round(r * ns_per_samp))
        iq = raw[:2 * a.nsamp].astype(np.float32) / 2048.0
        chunks[i] = iq[0::2] + 1j * iq[1::2]
        t3 = time.perf_counter()
        g_post.append(sdr.getGain(SOAPY_SDR_RX, 0))
        t_tune.append(t1 - t0); t_flush.append(t2 - t1); t_cap.append(t3 - t2)
        n_disc.append(disc); n_ovf.append(ovf); n_restart.append(restart)
    t_sweep = time.perf_counter() - t_sweep0
    sweep_times.append(t_sweep)
    out = a.out if a.repeat == 1 else a.out.replace(".npz", f"_{rep_i:02d}.npz")
    np.savez(out, fcs=np.array(fcs), chunks=chunks, rate=a.rate, keep=a.keep, overlap=a.overlap,
             gain=a.gain, agc=a.agc, g_pre=g_pre, g_post=g_post, t_agc=t_agc, t_tune=t_tune, t_flush=t_flush,
             t_cap=t_cap, n_disc=n_disc, n_ovf=n_ovf, n_restart=n_restart, t_sweep=t_sweep)
    gp, gq = np.array(g_pre), np.array(g_post)
    print(f"sweep {rep_i + 1}/{a.repeat}: {t_sweep:.3f} s ({t_sweep/len(fcs)*1e3:.1f} ms/chunk), retune {ms(t_tune)}, "
          f"capture {ms(t_cap)}, gain {gp.min():.0f}..{gp.max():.0f} dB ({int(np.sum(gp != gq))} chunks changed mid-capture), "
          f"overflows {sum(n_ovf)}, restarts {sum(n_restart)}", flush=True)

sdr.deactivateStream(st); sdr.closeStream(st); lib.m2sdr_close(csr)
st_ = np.array(sweep_times)
print(f"{a.repeat} sweep(s) of {len(fcs)} chunks {fcs[0]/1e6:.0f}..{fcs[-1]/1e6:.0f} MHz LO (step {step/1e6:.0f} MHz, "
      f"{a.nsamp} samples, {'slow AGC ' + str(a.agc_wait_ms) + ' ms wait' if a.agc else 'gain ' + str(a.gain) + ' dB'}): "
      f"total {st_.sum():.3f} s, per sweep mean {st_.mean():.3f} / min {st_.min():.3f} / max {st_.max():.3f} s")
