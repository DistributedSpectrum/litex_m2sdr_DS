#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2026 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Stitch an m2sdr_sweep.py capture into one PSD.

Usage: m2sdr_sweep_stitch.py SWEEP.npz OUT.png

Welch PSD per chunk (4096-pt), keep +-keep/2 around each LO, linear-power
cross-fade across the overlaps. AGC sweeps are referred to the input
(dBFS - RX gain per chunk). Prints seam mismatch and clipping, saves the plot
plus OUT_stitched.npz (frequency grid + PSD).
"""

import sys, numpy as np, matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from scipy.signal import welch
d = np.load(sys.argv[1]); out = sys.argv[2]
fcs, chunks, fs, keep, ov = d["fcs"], d["chunks"], float(d["rate"]), float(d["keep"]), float(d["overlap"])
nfft = 4096; half = keep / 2
agc = bool(d["agc"]) if "agc" in d else False
# With AGC each chunk has its own gain: refer every chunk to the input (dBFS - gain).
gains = np.array(d["g_pre"], float) if agc else np.full(len(fcs), float(d["gain"]))
gchg = (np.array(d["g_pre"]) != np.array(d["g_post"])) if agc else np.zeros(len(fcs), bool)
grid_df = fs / nfft
f0, f1 = fcs[0] - half, fcs[-1] + half
grid = np.arange(f0, f1, grid_df)
acc = np.zeros_like(grid); wsum = np.zeros_like(grid)
clip = []; seams = []
prev = None
for fc, x, g in zip(fcs, chunks, gains):
    clip.append(np.mean(np.maximum(abs(x.real), abs(x.imag)) >= 2047/2048) * 100)
    f, p = welch(x, fs=fs, window="hann", nperseg=nfft, noverlap=0, return_onesided=False)
    f, p = np.fft.fftshift(f) + fc, np.fft.fftshift(p) / 10 ** (g / 10)
    m = np.abs(f - fc) <= half
    f, p = f[m], p[m]
    # Triangular-edge weight: 1 inside, ramps to 0 across the overlap at each edge.
    w = np.clip((half - np.abs(f - fc)) / ov, 0, 1)
    gi = np.clip(np.round((f - f0) / grid_df).astype(int), 0, len(grid) - 1)
    np.add.at(acc, gi, w * p); np.add.at(wsum, gi, w)
    if prev is not None:
        pf, pp = prev
        lo, hi = fc - half, pf[-1]
        a_ = np.median(10*np.log10(pp[(pf >= lo) & (pf <= hi)])); b_ = np.median(10*np.log10(p[(f >= lo) & (f <= hi)]))
        seams.append(b_ - a_)
    prev = (f, p)
psd = 10*np.log10(acc / np.maximum(wsum, 1e-12) + 1e-30)
seams = np.array(seams)
print(f"stitched {len(fcs)} chunks -> {grid[0]/1e6:.1f}..{grid[-1]/1e6:.1f} MHz, {len(grid)} bins of {grid_df/1e3:.1f} kHz")
print(f"seam level mismatch (next - prev chunk, median dB in overlap): median {np.median(seams):+.2f}, |max| {np.max(np.abs(seams)):.2f} dB at LO {fcs[1+np.argmax(np.abs(seams))]/1e6:.0f} MHz")
if agc:
    print(f"AGC gain per chunk {gains.min():.0f}..{gains.max():.0f} dB; changed during capture at LO (MHz): "
          + ", ".join(f"{fc/1e6:.0f}" for fc in fcs[gchg]))
print(f"clipping: max {max(clip):.3f}% (chunk LO {fcs[int(np.argmax(clip))]/1e6:.0f} MHz), chunks with any clipping: {sum(c > 0 for c in clip)}")
np.savez(out.replace(".png", "_stitched.npz"), f=grid, psd_db=psd)
fig, ax = plt.subplots(3, 1, figsize=(14, 11))
ax[0].plot(grid/1e9, psd, lw=0.3); ax[0].set_xlim(grid[0]/1e9, grid[-1]/1e9)
for fc in fcs: ax[0].axvline(fc/1e9, color="0.85", lw=0.3, zorder=0)
ylab = "dBFS/Hz - RX gain (dB)" if agc else "dBFS/Hz"
ax[0].set_title(f"LiteX-M2SDR sweep {grid[0]/1e6:.0f} MHz - {grid[-1]/1e9:.2f} GHz: {len(fcs)} x 80 MHz chunks @ 122.88 MSps (16-bit), 5 MHz overlap, "
                + (f"slow AGC ({gains.min():.0f}-{gains.max():.0f} dB, {1e3*np.mean(d['t_agc']):.0f} ms wait)" if agc else f"gain {float(d['gain']):.0f} dB") + f", sweep {float(d['t_sweep']):.2f} s", fontsize=10)
ax[0].set_xlabel("GHz"); ax[0].set_ylabel(ylab)
if agc:
    a2 = ax[0].twinx(); a2.step(fcs/1e9, gains, where="mid", color="tab:orange", lw=0.8); a2.set_ylabel("AGC gain (dB)", color="tab:orange")
for a, (lo, hi, t) in zip(ax[1:], [(600e6, 1000e6, "600 MHz - 1 GHz (LTE 700/850, ...)"), (1700e6, 2700e6, "1.7 - 2.7 GHz (AWS/PCS, 2.4 GHz ISM, ...)")]):
    m = (grid >= lo) & (grid <= hi); a.plot(grid[m]/1e6, psd[m], lw=0.4)
    for fc in fcs:
        if lo <= fc <= hi: a.axvline(fc/1e6, color="r", lw=0.3, alpha=0.4)
    a.set_title(t + "  (red = chunk LO centres)"); a.set_xlabel("MHz"); a.set_ylabel(ylab)
for a in ax: a.grid(alpha=0.3)
plt.tight_layout(); plt.savefig(out, dpi=100)
