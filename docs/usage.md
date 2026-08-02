# Usage

How to set up the environment, run the radar, and change the waveform.

## 1. Prerequisites

### Hardware

- ADALM-Pluto SDR (PlutoSDR)
- Two 2.4 GHz antennas (one TX, one RX)
- Optional but recommended: attenuators or a coax jumper between TX and RX for
  bench testing, so you are not blasting a live antenna into a nearby receiver

**The transmit antenna must be on TX1, the SMA connector.** This code
transmits on channel 0 only, and `adi.Pluto` exposes exactly one complex
transmit channel — its `_tx_channel_names` is `["voltage0", "voltage1"]`,
which is I and Q of channel 0. It therefore cannot drive the TX2 pads exposed
by the 2R2T modification, regardless of what the firmware mode reports. An
antenna on TX2 receives nothing, and anything reaching the receiver in that
configuration is stray leakage from the TX1 connector. Reaching TX2 would
require `adi.ad9361` instead. The same applies on receive: the code listens on
channel 0, the RX SMA, so an antenna on the RX2 pads sits idle.

### Native libiio

This is the one dependency pip cannot install for you. `pyadi-iio` depends on
`pylibiio`, which is a pure-Python `ctypes` wrapper — it loads the real
`libiio` shared library at import time. Without that library on the system,
`import adi` fails no matter what is in the venv.

| Platform | Install |
| --- | --- |
| Debian / Ubuntu | `sudo apt install libiio0 libiio-utils` |
| Fedora | `sudo dnf install libiio libiio-utils` |
| Arch | `sudo pacman -S libiio` |
| macOS | `brew install libiio` |
| Windows | [Analog Devices libiio installer](https://github.com/analogdevicesinc/libiio/releases) |

Confirm it works and that the Pluto is reachable:

```bash
iio_info -u ip:pluto.local
```

If `pluto.local` does not resolve, use the IP the Pluto presents over USB —
usually `ip:192.168.2.1`.

## 2. Set up the Python environment

The setup script builds a self-contained venv in `.venv/` next to the repo, so
the project stays portable — clone it anywhere and re-run the script.

On Debian/Ubuntu, `setup-dependencies.sh` wraps both steps — it apt-installs
libiio, then calls `setup_env.sh`. Arguments pass straight through:

```bash
./setup-dependencies.sh
```

Otherwise, install libiio per the table above and run the venv setup directly.

Linux / macOS:

```bash
./setup_env.sh
```

Windows (PowerShell):

```powershell
.\setup_env.ps1
```

Useful flags (both scripts): `--recreate` / `-Recreate` to wipe and rebuild,
`--python` / `-Python` to pick a specific interpreter, and
`--system-site-packages` / `-SystemSitePackages` if you installed the libiio
Python bindings from your distro (`python3-libiio`) and want the venv to see
them instead of the pip copy.

The script warns — but does not fail — if native libiio is missing, since the
venv itself is still valid.

Activate it:

```bash
source .venv/bin/activate
```

## 3. Run

```bash
python main.py
```

`main.py` prints the derived radar parameters, connects to the Pluto,
generates one FMCW chirp, loads it into the cyclic TX buffer so it repeats
continuously, then loops: capture, align, dechirp, range-FFT, and print the
strongest return.

```
chirp duration  : 204.8 us
sweep rate      : 78.12 GHz/s
range resolution: 9.37 m
unambiguous to  : 19187 m
target:    899.4 m   -31.0 dBFS  (+69.3 dB over noise, lock 4117, resolution 9.4 m)
no target  (best bin -89.3 dBFS is only 10.8 dB over noise -100.1 dBFS, lock 4118)
```

`no target` is the correct and expected output for an empty room — see
"Detection: a peak is not a target" below.

Ctrl-C stops it and tears down the TX buffer so the Pluto stops transmitting.

## 4. Configuration

Every parameter lives at the top of [`main.py`](../main.py). The radar
functions in `radar_functions/` take these as arguments and hold no config of
their own, so you can change the waveform without touching them.

| Constant | Default | Meaning |
| --- | --- | --- |
| `URI` | `ip:pluto.local` | Pluto address; try `ip:192.168.2.1` over USB |
| `FS` | 20 MHz | Sample rate — sets the baseband width and the RX rate |
| `FC` | 2.45 GHz | Carrier frequency for both TX and RX LOs. Sits mid-WiFi; see the interference note in troubleshooting |
| `N` | 4096 | Samples per chirp; with `FS` this sets the chirp duration |
| `B` | 16 MHz | Swept bandwidth — sets range resolution |
| `RX_BUFFER_SIZE` | 8192 | Samples per RX buffer; **must be > `N`** so a whole chirp is always captured |
| `TX_GAIN` | -40 dB | TX hardware gain (negative is attenuation; start low, especially behind a PA) |
| `RX_GAIN` | 30 dB | RX hardware gain |
| `TX_SCALE` | 16384 | Peak I/Q value of the transmitted waveform — see below |
| `ADC_FULL_SCALE` | 2048 | ADC full scale, for dBFS. The AD9361 is 12-bit |
| `MIN_RANGE_CELLS` | 5 | Range cells to skip at zero range, where TX leakage dominates |
| `MIN_LOCK_QUALITY` | 200 | Reject buffers where the matched filter did not find a chirp |
| `DETECTION_MARGIN_DB` | 15 | dB a peak must clear the profile median to count as a target |

### `TX_SCALE` is not optional

`pyadi-iio`'s `tx()` casts I and Q straight to `int16` with **no scaling**:

```python
data[indx::stride] = i.astype(self._tx_data_type)   # _tx_data_type is int16
```

A unit-amplitude waveform therefore truncates to -1, 0 or 1 out of a ±32767
range — roughly 90 dB below full scale, which is indistinguishable from not
transmitting. The waveform must be pre-scaled; `2**14` is the conventional
choice, leaving headroom below the `2**15` clipping point.

This is why `chirp()` takes `amplitude` as a required argument rather than
defaulting it: getting it wrong is silent, and the symptom (random ranges)
looks nothing like the cause.

### What the defaults mean

With `N = 4096`, `FS = 20 MHz`, `B = 16 MHz`:

- Chirp duration `T = N / FS` = **204.8 µs**
- Sweep rate `k = B / T` = **78.1 GHz/s**
- Range resolution `c / 2B` = **9.37 m**

Range resolution depends only on bandwidth. To resolve targets more finely you
need more `B`, which means raising `FS` alongside it, since `B` cannot usefully
exceed the sample rate.

### A note on `B` vs `FS`

`B` is set to `0.8 * FS`. Pushing `B` all the way to `FS` puts the sweep at the
edge of Nyquist, where the Pluto's anti-alias filtering rolls off the ends of
the sweep. Keep some margin.

### Why `RX_BUFFER_SIZE` must exceed `N`

The cyclic TX buffer free-runs against a free-running RX, so a captured buffer
starts at an arbitrary point in the chirp. `find_chirp_start()` locates the
chirp with a matched filter, but that only works if a complete chirp is
present in the buffer — hence `2 * N`. Setting `RX_BUFFER_SIZE = N` raises a
`ValueError`.

## 5. How the processing works

```
chirp(N, B, FS, TX_SCALE)  generate the reference, transmit it cyclically
    |
sdr.rx()                   capture 2*N samples at an arbitrary chirp phase
    |
find_chirp_start()         matched filter -> (start index, lock quality)
    |                      quality below MIN_LOCK_QUALITY -> discard buffer
dechirp()                  mix against conj(reference) -> beat tone per target
    |
range_fft()                FFT -> range profile (ranges in metres, ascending)
    |
magnitude_dbfs()           magnitudes in dBFS, ready for CFAR
```

### Lock quality

`find_chirp_start()` returns the correlation peak divided by the median
correlation level alongside the index. This matters because **the matched
filter always returns an argmax, even against pure noise**, and dechirping
there produces a confident-looking range profile that is entirely meaningless.

A real chirp gives a peak in the thousands. Noise gives about 4. `main.py`
discards anything below `MIN_LOCK_QUALITY` and says so rather than printing a
fabricated range.

The threshold is 200 because of the band in between. Measured buffers scoring
12 to 140 cleared an earlier threshold of 10 and still produced uniformly
random ranges — partial correlation on weak leakage is not good enough to
align against.

### Detection: a peak is not a target

Even with perfect alignment, `argmax` over a target-free range profile returns
the loudest noise bin, and with ~2000 bins that already sits 10–12 dB above
the median. Reported as a range it is indistinguishable from a real target and
scatters uniformly across the unambiguous span.

`main.py` therefore requires a peak to clear the profile median by
`DETECTION_MARGIN_DB` before calling it a target, and prints `no target` with
the actual margin otherwise. This is a crude stand-in for CFAR;
`target_detection_dbfs.py` from the PhaserRadarLabs repo drops in unmodified
and does it properly — see [porting-from-phaser.md](porting-from-phaser.md).

A bare Pluto has a fixed LO and synthesizes the chirp in baseband, so echoes
return as *delayed chirps* rather than beat tones. The dechirp step is what
collapses them into tones — hardware FMCW radars get this for free from a
swept LO. See [porting-from-phaser.md](porting-from-phaser.md) for the full
explanation.

### Ranges are relative, not absolute

`find_chirp_start()` locks onto the strongest return. On a bench that is
direct TX→RX leakage at essentially zero delay, so it acts as a zero-range
reference and everything is measured relative to it — which is what you want.

If a target ever outshines the leakage, the profile silently re-references
itself to that target and all ranges shift. For absolute range you need a
coupled sample of TX on a second RX channel.

## 6. Visualization

```bash
python visualize.py
```

Two panels: the current range profile with the detection threshold drawn on
it, and a waterfall of recent profiles with time running downward. The
waterfall is the useful one — a moving target draws a diagonal track that is
obvious long before it is convincing in any single profile.

Useful flags:

```bash
python visualize.py --demo             # simulated data, no hardware needed
python visualize.py --max-range 500    # zoom the range axis
```

`--demo` synthesises leakage at zero range plus a target moving between 200
and 1600 m. Use it to confirm the display works and to see what a healthy
setup looks like before trusting a live run.

Buffers failing `MIN_LOCK_QUALITY` are skipped and counted rather than
plotted, so interference shows as a stalled display and a rising skip count,
not as garbage on screen. The status text reports lock quality, noise level,
and the plotted/skipped tally.

This needs matplotlib, which `requirements.txt` installs. `main.py` and
`diagnose.py` do not.

## 7. Transmitting responsibly

2.45 GHz is a shared ISM band. Keep `TX_GAIN` low, prefer a wired or heavily
attenuated path while developing, and do not leave the cyclic buffer
transmitting when you are not actively testing. Ctrl-C handles that; killing
the process another way may leave the Pluto transmitting until it is
re-initialized or power-cycled.

## 8. Troubleshooting

**`ModuleNotFoundError: No module named 'adi'`**
The venv is not activated, or `setup_env.sh` did not finish. Re-run it.

**`OSError` / `TypeError` on `import adi`, mentioning `find_library` or a
`NoneType` handle**
Native libiio is missing — see section 1. Check with:

```bash
python -c "from ctypes.util import find_library; print(find_library('iio'))"
```

`None` means the library is not installed or not on the loader path.

**Cannot connect / `Unable to create context`**
Confirm the Pluto enumerates with `iio_info -u ip:pluto.local`. Over USB the
Pluto shows up as a network adapter; if `pluto.local` does not resolve, use
`ip:192.168.2.1`.

**`no chirp lock (quality N, need 10.0)`**
The matched filter cannot find the chirp in the received buffer, so TX is not
reaching RX. Run the diagnostic, which sweeps TX gain and reports where the
chirp first becomes visible:

```bash
python diagnose.py
```

It runs three stages in order, each of which invalidates the next if it fails:

1. **Ambient survey** — is the band bursty? (see the interference note below)
2. **Receive gain sweep** — is the converter clipping? Nothing measured
   afterwards means anything if it is.
3. **Transmit gain sweep** — at what `TX_GAIN` does the chirp become visible?

It stops at the first solid lock rather than continuing to raise power, and
recommends a `TX_GAIN` and `RX_GAIN` pair. If it locks at no gain at all, work
through: both antennas connected and RX on the RX SMA; bandpass filters
passing 2.45 GHz; LNAs actually powered (the Pluto does not supply bias-tee
power on RX). A wired loopback through attenuators takes the antennas out of
the question — and also takes the interference out of it, which makes it the
fastest way to separate an RF-environment problem from a wiring one.

**Peaks jump to a different random range every buffer**
The classic symptom of dechirping noise. Historically this was caused by an
unscaled transmit waveform — see `TX_SCALE` above. `MIN_LOCK_QUALITY` now
catches it, but if you have changed `chirp()` or `TX_SCALE`, check there
first.

**dBFS values are positive**
`ADC_FULL_SCALE` does not match your hardware. Magnitudes should never exceed
0 dBFS.

**Readings jump around between runs; the gain sweep is not monotonic**
Received level cannot rise as gain falls, so a non-monotonic sweep is not a
measurement of anything. `diagnose.py` checks this and refuses to stand behind
its recommendation when it trips.

The usual cause is not the software but the band. 2.4 GHz is shared with WiFi,
Bluetooth and microwave ovens, all of which transmit in **packets**. A single
RX buffer is only ~0.4 ms, so consecutive captures can differ by 100× purely
on whether they caught a packet. `diagnose.py` opens with an ambient survey
that measures exactly this and reports a burst ratio; anything above about 10×
means the environment is bursty rather than merely noisy.

What to do, in order of effort:

- Move `FC` to a quieter corner of the band — 2.400 or 2.483 GHz, both still
  inside a 2.4 GHz bandpass filter — and re-run.
- Measure somewhere with less 2.4 GHz traffic, or turn off nearby WiFi.
- Accept it. `main.py` discards buffers that fail `MIN_LOCK_QUALITY`, so
  interference costs throughput, not correctness. You get fewer usable range
  profiles per second, not wrong ones.

Because of this, `diagnose.py` reports statistics that survive bursts: the
noise floor as a 10th percentile (the channel *between* packets), clipping as
the worst case across buffers, and lock quality as both median and best. Chirp
lock is judged on the best buffer — the question is whether the chirp is
findable when the channel is clear.

**Receiver saturated — RX rms near 2048 even with the transmitter off**
Powered LNAs plus `RX_GAIN` is more gain than the Pluto needs, and ambient
2.4 GHz traffic alone can rail the converter. A clipped ADC cannot see a
target below the leakage, and clipping products appear as fake peaks spread
across the range profile, so this must be fixed before any range output means
anything.

`diagnose.py` sweeps receive gain with the transmitter quiet, reports the
clipped-sample percentage at each setting, and recommends the highest
`RX_GAIN` that keeps the noise floor around -20 dBFS. Expect to end up far
below 30 dB with LNAs in line. Lowering `RX_GAIN` costs sensitivity, so
`TX_GAIN` usually has to come up to compensate — the diagnostic reports both
together.

**RX level does not rise with TX power, or drops below the TX-off floor**
The receive AGC is running and changing gain between captures, so no level
reading is comparable to any other. `pyadi-iio`'s gain setter is:

```python
if self.gain_control_mode_chan0 == "manual":
    self._set_iio_attr_float(...)
```

It **silently does nothing** while the Pluto is in its default `slow_attack`
mode, so `sdr.rx_hardwaregain_chan0 = 30` is a no-op unless the mode was set
to `manual` first. `configure_sdr()` sets them in that order and
`verify_config()` reads them back, so a mismatch is reported rather than
silently ignored.

## Repo layout

```
main.py                     Config + hardware setup + the run loop
diagnose.py                 Band survey, gain sweeps: is the chirp reaching RX?
visualize.py                Live range profile + waterfall (--demo works offline)
radar_functions/
    chirp.py                FMCW chirp generation
    dechirp.py              Alignment, dechirp, range FFT, dBFS conversion
requirements.txt            Python dependencies
setup-dependencies.sh       libiio + venv in one step (Debian/Ubuntu)
setup_env.sh                venv setup for Linux/macOS
setup_env.ps1               venv setup for Windows
docs/usage.md               This file
docs/porting-from-phaser.md Adapting Jon Kraft's PhaserRadarLabs examples
```

Everything in `radar_functions/` is pure numpy — no hardware access and no
configuration of its own. Every parameter is passed in from `main.py`, so the
functions can be tested against simulated data without a Pluto attached.
