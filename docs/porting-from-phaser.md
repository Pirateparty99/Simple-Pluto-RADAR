# Porting Jon Kraft's Phaser examples to a bare Pluto

Reference repo: https://github.com/jonkraft/PhaserRadarLabs

The Phaser examples cannot be run as-is without a CN0566. The blocker is not
the beamforming — it is that the Phaser and a bare Pluto generate the chirp in
completely different places.

## The core difference

In every Phaser example the **ADF4159 PLL on the CN0566 sweeps the LO** across
500 MHz at ~10 GHz. The Pluto transmits a *constant* 100 kHz CW tone:

```python
i = np.cos(2 * np.pi * t * fc) * 2**14   # fc = signal_freq = 100 kHz
q = np.sin(2 * np.pi * t * fc) * 2**14
my_sdr.tx([iq, iq])
```

Because the sweep happens in RF hardware, the receive mixer downconverts each
echo against the *currently swept* LO. The beat frequency therefore lands in
baseband for free, which is why their processing is a plain FFT:

```python
dist = (freq - signal_freq) * c / (2 * slope)
```

On a bare Pluto the LO is **fixed** and the chirp is synthesized in baseband.
An echo comes back as a *delayed chirp*, not as a beat tone. You have to
collapse it to a tone yourself:

```python
beat = rx * np.conj(reference)      # the step the Phaser gets in hardware
```

That is what `radar_functions/dechirp.py` does. Note there is no
`- signal_freq` term in the range axis, because there is no 100 kHz IF.

## The second problem: chirp alignment

The Phaser examples use the Pluto's TDD engine and a GPIO burst trigger to
start each capture at a known point in the ramp:

```python
my_phaser._gpios.gpio_burst = 0
my_phaser._gpios.gpio_burst = 1
my_phaser._gpios.gpio_burst = 0
data = my_sdr.rx()
```

Without that, a cyclic TX buffer free-runs against a free-running RX and every
captured buffer starts at an arbitrary point in the chirp. Dechirping against
a misaligned reference smears the beat tone across the entire spectrum.

`find_chirp_start()` solves this in software with a matched filter (FFT-based
circular correlation), which is why `RX_BUFFER_SIZE` must be at least `2 * N` —
a full chirp has to be guaranteed present regardless of where the buffer lands.

### What this costs you

The matched filter locks onto the **strongest** return. On a bench that is
direct TX→RX leakage, arriving at essentially zero delay, so it works as a
zero-range reference and every measured range is relative to it. That is the
normal and intended case.

But it means **ranges are relative, not absolute**. If a target ever outshines
the leakage, the profile silently re-references itself to that target and all
ranges shift. If you need absolute range, feed a coupled sample of TX into a
second RX channel and align against that instead — a good use for the second
channel if you do the 2R2T mod.

## What ports and what doesn't

| Their file | Portability |
| --- | --- |
| `target_detection_dbfs.py` (CFAR) | **Fully portable.** Pure numpy, takes a magnitude array. Feed it `magnitude_dbfs()` output. |
| `Range_Doppler_Processing.py` | Mostly portable. Offline script; the 2D FFT over a chirp stack is directly reusable. Build the stack yourself, fix the `dist` axis. |
| pyqtgraph waterfall GUI code | Portable — it is only plotting. |
| `my_phaser.*` ramp config, `set_beam_phase_diff`, gain/phase cal | Not portable. Replaced by the numpy chirp. |
| `*_ChirpSync.py` TDD blocks (`adi.tddn`) | Possibly portable. The TDD engine is a **Pluto firmware feature** (0.38+), not a Phaser one — `gpio_phaser_enable` is Phaser-specific, but the burst timing may be worth exploring as a better alternative to software alignment. |

## Capability differences

| | Phaser | Bare Pluto |
| --- | --- | --- |
| Sweep source | ADF4159 PLL, RF | numpy, baseband |
| Bandwidth | 500 MHz | ≤ sample rate; ~2–20 MHz practical |
| Range resolution | ~0.3 m | 9.4 m at the 16 MHz default |
| Carrier | 10 GHz | 2.45 GHz |
| Angle of arrival | 8-element beamforming | none, or 2-channel interferometry with the 2R2T mod |
| Chirp sync | TDD engine + GPIO burst | software matched filter |

Drone tracking at ~9 m resolution is marginal. This is the fundamental cost of
dropping the Phaser: bandwidth is capped by the Pluto's sample rate, and range
resolution is `c / 2B`.

## Range wrap from the cyclic buffer

With `tx_cyclic_buffer = True` the chirp repeats back-to-back. After aligning
to the leakage peak and taking `N` samples, a target at delay `tau` has its
last `tau` samples drawn from the *next* chirp, which folds into the profile.

For small `tau` relative to `T` this is a minor edge effect. Jon works around
the equivalent problem by discarding the first 10% of each ramp:

```python
begin_offset_time = 0.10 * ramp_time_s
```

Doing the same is worthwhile once you move past first light.

## Suggested order of work

1. **CW Doppler first.** Transmit a fixed tone, FFT the RX, look for a moving
   target. No dechirp, no alignment — it validates the whole RF chain (PA,
   LNAs, filters, antennas) with the fewest moving parts.
2. **FMCW range.** What `main.py` does now.
3. **Range-Doppler.** Stack many chirps into a 2D array, FFT both axes. Port
   `Range_Doppler_Processing.py` at this point.
4. **CFAR.** Drop in `target_detection_dbfs.py` unchanged.

## Two pyadi-iio settings that fail silently

Both of these are done correctly in the Phaser examples, and both cost real
debugging time when omitted.

**Transmit scaling.** `tx()` casts I and Q straight to `int16` with no
scaling, so a unit-amplitude waveform truncates to -1, 0 or 1 and effectively
nothing is transmitted. Jon's code scales explicitly:

```python
i = np.cos(2 * np.pi * t * fc) * 2 ** 14
```

**Gain control mode before gain.** `rx_hardwaregain_chan0`'s setter is a no-op
unless `gain_control_mode_chan0` is already `"manual"`, and the Pluto boots
into `slow_attack`. Jon sets them in the required order:

```python
my_sdr.gain_control_mode_chan0 = "manual"
my_sdr.rx_hardwaregain_chan0 = int(rx_gain)
```

Leaving the AGC on is particularly bad for FMCW: the receiver changes gain
between captures, so received levels stop tracking transmit power and the
correlation used for chirp alignment degrades.

## Hardware notes

These apply to the EVAL-CN0417-EBZ + 2× Nooelec LaNA WB + 2.4 GHz BPF setup.

- **The EVAL-CN0417-EBZ is a power amplifier, not an LNA.** It is an ADL5606,
  ~20 dB gain, P1dB ≈ +30.8 dBm. That makes it the right part for the transmit
  side, but with the Pluto's ~+7 dBm maximum output you are looking at roughly
  0.5 W radiated. `TX_GAIN` defaults to -40 dB for that reason. Raise it
  slowly.
- **Two RX LNAs implies two RX channels, and a stock ADALM-Pluto only brings
  out one RX SMA.** The Phaser's Pluto is modified: firmware set to
  `compatible=ad9361`, `mode=2r2t`, plus a U.FL soldered to the unpopulated
  RX2 pads. Without that mod one LaNA has nowhere to go. With it, the best use
  of the second channel is a coupled TX reference for absolute ranging.
- **TX/RX isolation is the biggest practical risk.** A 0.5 W PA transmitting
  continuously into LNAs with ~20 dB gain and ~+20 dBm maximum output, a short
  distance away, will saturate them and can damage them. Bench-test with the
  PA unpowered first, then add physical separation, cross-polarization, or
  absorber between the antennas.
- **The Pluto does not supply bias-tee power on RX.** Power the LaNA WBs over
  micro-USB or the DC barrel connector.
- **The bandpass filters are a good call.** They suppress Pluto LO leakage and
  images, and stop the PA amplifying out-of-band noise. An 83 MHz-wide ISM
  filter passes any sweep the Pluto can generate.
- **Check your transmit rights.** 2.45 GHz is shared ISM. Half a watt of FMCW
  is not a trivial emission, and Part 15 conditions may not cover a chirped
  radar waveform. The 2390–2450 MHz amateur allocation is the cleaner route if
  you are licensed.

## Sources

- [CN0417 Circuit Note](https://www.analog.com/en/resources/reference-designs/circuits-from-the-lab/cn0417.html)
- [EVAL-CN0417-EBZ product page](https://www.newark.com/analog-devices/eval-cn0417-ebz/evaluation-board-rf-power-amplifier/dp/50AK1390)
- [Nooelec LaNA WB](https://www.nooelec.com/store/featured-products/lana-wb.html)
- [CN0566 Quick Start Guide](https://wiki.analog.com/resources/eval/user-guides/circuits-from-the-lab/cn0566/quickstart)
- [PySDR: Hands-on with Phaser](https://pysdr.org/content/phaser.html)
