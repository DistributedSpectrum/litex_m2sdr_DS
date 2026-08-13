# RX and TX Quickstart

Getting a LiteX-M2SDR receiving and transmitting from a host that already has the
software stack built. Every command here is a script in `scripts/`, run from the
repository root. If the stack is not built yet, start with
`scripts/m2sdr_provision_host.sh` (see also the per-host guides under `doc/hosts/`).

## Check the board is alive

```bash
m2sdr_util info          # build timestamp, FPGA DNA, PCIe link, SI5351/AD9361 presence
m2sdr_util clk-test 3 2  # all six measured clocks
SoapySDRUtil --find      # should list driver = LiteXM2SDR
```

`m2sdr_util info` reports **build-time** capability flags. `PCIe PTM: Enabled` means
the gateware was compiled with PTM support, not that PTM is working at runtime —
check `lspci -vv` for `PTMControl:` if you care. The SI5351 panel is similarly
optimistic: its "SYS_INIT: Ready / LOL: Locked / LOS: Valid Signal" line decodes
*active-high fault bits*, so a device returning all zeros reads as perfectly
healthy. See [Clock Investigation](clock-investigation.md).

## Receive

```bash
scripts/m2sdr_rx_capture.py /tmp/rx.iq --freq 800e6 --rate 30.72e6 --gain 40 --samples 4194304
scripts/m2sdr_spectrum.py --rate 30.72e6 peaks /tmp/rx.iq
```

Captures interleaved int16 (CS16). For sustained high-rate capture prefer the
native `m2sdr_record`; the script exists to exercise the same layer the TX tools
use. RX `--gain` is a real gain in dB, unlike TX (below).

Worth running once on any new board:

```bash
scripts/m2sdr_spectrum.py dup /tmp/rx.iq
```

Every figure should sit near 0 %. Anything near 50 % or 100 % means samples are
being repeated, which points at a channel-layout or interleaving fault rather
than anything in the RF chain.

## Transmit

**`--att` is attenuation, not gain.** `0` is full power (roughly +8 dBm) and `89`
is quietest. This is inverted from normal SoapySDR semantics and is easy to get
backwards; with a cabled connection into another receiver it is the difference
between a clean measurement and a damaged front end. Start high.

A tone, through the raw SoapySDR API:

```bash
scripts/m2sdr_tx_tone.py --freq 1e9 --tone 1e6 --att 50 --secs 20
```

The same thing through GNU Radio (gr-soapy), which is the path to use when you
want real modulation or a flowgraph:

```bash
scripts/m2sdr_tx_stream.py --channels 1 --tone 1e6 --freq 1e9 --att 50 --secs 20
```

To measure the LO itself, transmit DC — with `--tone 0` the carrier sits exactly
on the LO, so there is no sample-rate term in the measurement:

```bash
scripts/m2sdr_tx_tone.py --freq 1e9 --tone 0 --att 50 --secs 0   # runs until Ctrl-C
```

## Transmit from an IQ file

Generate a test file (sized so it loops seamlessly), then stream it:

```bash
scripts/m2sdr_tx_stream.py --make-file /data/tone.cs16 --size-gb 4 --tone 1e6 --rate 30.72e6
scripts/m2sdr_tx_stream.py --file /data/tone.cs16 --rate 30.72e6 --freq 1e9 --att 50
```

Playing a file once (without `--repeat`) prints what throughput was actually
achieved. Drop the page cache first (`sync; echo 3 > /proc/sys/vm/drop_caches`)
or you will measure RAM.

Measured on a Pi 5 with the file on an NVMe behind the same PCIe switch:

| mode | achieved | of real time | PCIe | disk |
|---|---|---|---|---|
| 1T1R @ 30.72 MSPS | 29.76 MSPS | 96.9 % | 119 MB/s | 119 MB/s |
| 2T2R @ 30.72 MSPS | 26.96 MSPS | 87.8 % | 216 MB/s | 108 MB/s |

Both runs printed GNU Radio underflow markers (`U`), so the shortfall is the host
failing to feed the SDR rather than the radio consuming slowly — the gap widens
as PCIe load doubles. On a shared x1 uplink the two directions compete; see
[Running LiteX-M2SDR on Raspberry Pi 5](hosts/raspberry-pi-5.md).

## Which control layer

- **Raw SoapySDR API** (`m2sdr_tx_tone.py`) — deterministic, no hidden
  resampling or gain stages between you and the DAC. Use it when you are
  measuring the radio itself.
- **gr-soapy** (`m2sdr_tx_stream.py`) — use `soapy.sink("driver=LiteXM2SDR", ...)`.
  The right choice once you want modulation or a flowgraph. IO type strings are
  lowercase `fc32` / `sc16` / `sc8`; `CF32` / `CS16` are rejected with
  "Invalid IO type".
- **gr-osmosdr** — works via `osmosdr.sink(args="soapy=0,driver=LiteXM2SDR")`,
  add `nchan=2,` for two channels. gr-soapy is the better-maintained path.
- **`m2sdr_gen` / `m2sdr_play`** — native, bypasses SoapySDR entirely. Useful as
  a cross-check: if a result differs between this and the Soapy path, that
  isolates which layer is at fault.

Opening the device from Python needs one non-obvious step:

```python
devices = SoapySDR.Device.enumerate()
matches = [d for d in devices if dict(d).get("driver") == "LiteXM2SDR"]
sdr     = SoapySDR.Device(matches[0])      # pass the enumerate entry verbatim
```

`SoapySDR.Device(dict(driver="LiteXM2SDR"))` raises `make() no match` even though
enumeration reports exactly that driver. Note `enumerate()` yields
`SoapySDRKwargs`, not dicts, hence the `dict()`.

## Verifying a signal is really yours

A single capture cannot distinguish a transmission from ambient RF, and at
800 MHz the ambient is strong enough to be convincing. Two checks:

```bash
# capture with TX on and off, then compare
scripts/m2sdr_spectrum.py --rate 30.72e6 diff /tmp/on.iq /tmp/off.iq
```

If several peaks move by similar amounts, that is ambient varying between
captures, not your signal.

The decisive test is to **change the tone offset and confirm the peak follows**.
Ambient does not move:

```bash
scripts/m2sdr_spectrum.py --rate 30.72e6 tone /tmp/rx.iq --expect 1e6
```

Given `--expect`, this also reports the implied sample-rate scale factor — a tone
that lands somewhere other than where it was requested tells you the rate is off
by that ratio.
