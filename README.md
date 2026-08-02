# Simple-Pluto-RADAR

This project aims to build a "quick and dirty" RADAR using the Pluto SDR and a total cost of parts being less than $1,000 USD. 

This project takes inspiration from the example FMCW setups Jon Kraft from Analog Devices crested with their CN0566 phased array development board. 
This project is not intended to provide a similar level of fidelity to the original project. The primary goal of this project is to use a relatively cheap platform to learn simple RADAR theory.

Phased Array FMCW RADAR example repo:
https://github.com/jonkraft/PhaserRadarLabs

## Requirements

- ADALM-Pluto SDR and two 2.4 GHz antennas
- Python 3.8+
- Native `libiio` — this cannot be installed by pip. On Debian/Ubuntu:
  `sudo apt install libiio0 libiio-utils`

See [docs/usage.md](docs/usage.md) for the other platforms and the reason pip
alone is not enough.

## Quick start

On Debian/Ubuntu, one command installs libiio and builds the venv:

```bash
./setup-dependencies.sh
```

On other platforms, install libiio yourself (see the docs) and then:

```bash
./setup_env.sh
```

Either way, activate and run:

```bash
source .venv/bin/activate
python main.py
```

On Windows, run `.\setup_env.ps1` and activate with
`.\.venv\Scripts\Activate.ps1`.

The setup script creates a self-contained venv in `.venv/`, installs the
dependencies, and checks that `libiio` is actually present.

## Configuration

All radar parameters — Pluto URI, sample rate, carrier frequency, chirp length,
bandwidth, and gains — are constants at the top of [main.py](main.py). The
functions in `radar_functions/` take them as arguments and hold no config of
their own, so they can be tested against simulated data without a Pluto.

At the defaults (16 MHz sweep, 4096 samples, 20 MHz sample rate) the chirp is
204.8 µs long and the range resolution is 9.4 m.

## How it works

The Pluto has a fixed LO and synthesizes the chirp in baseband, so echoes come
back as *delayed chirps* rather than beat tones. Each RX buffer is aligned to
the chirp with a matched filter, dechirped against the reference, and
range-FFT'd:

```
chirp -> tx (cyclic) -> rx -> find_chirp_start -> dechirp -> range_fft -> dBFS
```

Ranges are measured relative to the strongest return, which on a bench is TX
leakage at zero range.

## Visualization

```bash
python visualize.py --demo
```

Live range profile plus a waterfall of recent profiles. `--demo` runs on
simulated data with no hardware attached, which is the quickest way to see
what a working setup should look like.

## Documentation

- [docs/usage.md](docs/usage.md) — setup, running, parameter reference, and
  troubleshooting
- [docs/porting-from-phaser.md](docs/porting-from-phaser.md) — how to adapt
  Jon Kraft's PhaserRadarLabs examples to a bare Pluto, and notes on PA/LNA
  hardware
