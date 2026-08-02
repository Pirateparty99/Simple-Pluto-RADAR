"""Live range profile and waterfall display.

    python visualize.py              # live, from the Pluto
    python visualize.py --demo       # simulated data, no hardware needed
    python visualize.py --max-range 2000

The top panel is the current range profile with the detection threshold
drawn on it. The bottom panel is a waterfall of recent profiles, time
running downward, which makes a moving target far easier to see than any
single profile does.

Buffers that fail MIN_LOCK_QUALITY are skipped and counted rather than
plotted, so interference shows up as a stalled display, not as garbage.
"""
import argparse

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

import main as cfg
from radar_functions.chirp import chirp
from radar_functions.dechirp import C, range_profile

WATERFALL_ROWS = 120


class PlutoSource:
    """Live buffers from the radio."""

    def __init__(self, reference):
        self.sdr = cfg.configure_sdr()
        self.sdr.tx(reference)

    def read(self):
        return self.sdr.rx()

    def close(self):
        self.sdr.tx_destroy_buffer()


class DemoSource:
    """Simulated buffers: leakage at zero range plus a target closing in.

    Lets the display be exercised without a Pluto, and gives a known-good
    reference for what a working setup should look like.
    """

    def __init__(self, reference, k):
        self.reference = reference
        self.k = k
        self.range_m = 1200.0
        self.velocity = -35.0
        self.step = 0

    def read(self):
        n = cfg.RX_BUFFER_SIZE
        T = cfg.N / cfg.FS
        offset = np.random.randint(0, cfg.N)

        self.range_m += self.velocity
        if not 200.0 < self.range_m < 1600.0:
            self.velocity = -self.velocity

        idx = np.arange(n)
        buf = np.zeros(n, dtype=np.complex128)

        for target_range, amplitude_db in [(0.0, 0.0), (self.range_m, -28.0)]:
            tau = 2 * target_range / C
            t = (idx - offset) / cfg.FS - tau
            active = (t >= 0) & (t < T)
            buf[active] += 10 ** (amplitude_db / 20.0) * cfg.TX_SCALE * np.exp(
                1j * 2 * np.pi * (0.5 * self.k * t[active] ** 2
                                  - cfg.B / 2 * t[active]))

        buf *= cfg.ADC_FULL_SCALE * 0.4 / cfg.TX_SCALE
        buf += 0.6 * (np.random.randn(n) + 1j * np.random.randn(n))

        self.step += 1

        return buf.real.astype(np.int16) + 1j * buf.imag.astype(np.int16)

    def close(self):
        pass


def build_figure(ranges, max_range):
    visible = ranges <= max_range

    fig, (ax_profile, ax_fall) = plt.subplots(
        2, 1, figsize=(10, 8), height_ratios=[1, 1.4])
    fig.suptitle("Simple Pluto RADAR")

    x = ranges[visible]

    line, = ax_profile.plot(x, np.full(x.size, -120.0), lw=1.0)
    threshold_line = ax_profile.axhline(-120.0, color="tab:orange", ls="--",
                                        lw=1.0, label="detection threshold")
    marker, = ax_profile.plot([], [], "v", color="tab:red", ms=9,
                              label="detection")

    ax_profile.set_xlim(0, max_range)
    ax_profile.set_ylim(-120, 0)
    ax_profile.set_xlabel("range (m)")
    ax_profile.set_ylabel("dBFS")
    ax_profile.grid(alpha=0.3)
    ax_profile.legend(loc="upper right", fontsize=8)

    status = ax_profile.text(0.01, 0.95, "", transform=ax_profile.transAxes,
                             va="top", fontsize=9, family="monospace")

    history = np.full((WATERFALL_ROWS, x.size), -120.0)
    image = ax_fall.imshow(
        history, aspect="auto", origin="upper", cmap="viridis",
        extent=[0, max_range, WATERFALL_ROWS, 0], vmin=-100, vmax=-20)

    ax_fall.set_xlabel("range (m)")
    ax_fall.set_ylabel("buffers ago")
    fig.colorbar(image, ax=ax_fall, label="dBFS")

    fig.tight_layout()

    return fig, line, threshold_line, marker, status, image, history, visible


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true",
                        help="simulated data instead of hardware")
    parser.add_argument("--max-range", type=float, default=2000.0,
                        help="range axis limit in metres (default 2000)")
    args = parser.parse_args()

    T = cfg.N / cfg.FS
    k = cfg.B / T

    reference = chirp(cfg.N, cfg.B, cfg.FS, cfg.TX_SCALE)

    if args.demo:
        print("Demo mode: simulated data, no hardware in use.")
        source = DemoSource(reference, k)
    else:
        print("Connecting to %s ..." % cfg.URI)
        source = PlutoSource(reference)

    # One buffer to establish the range axis.
    ranges, _, _ = range_profile(source.read(), reference, cfg.FS, k,
                                 cfg.ADC_FULL_SCALE)

    (fig, line, threshold_line, marker, status, image, history,
     visible) = build_figure(ranges, args.max_range)

    counts = {"plotted": 0, "skipped": 0}

    def update(_frame):
        rx = source.read()
        _, profile, quality = range_profile(rx, reference, cfg.FS, k,
                                            cfg.ADC_FULL_SCALE)

        if quality < cfg.MIN_LOCK_QUALITY:
            counts["skipped"] += 1
            status.set_text("NO LOCK  quality %6.0f (need %.0f)\n"
                            "plotted %d  skipped %d"
                            % (quality, cfg.MIN_LOCK_QUALITY,
                               counts["plotted"], counts["skipped"]))
            status.set_color("tab:red")
            return line, threshold_line, marker, status, image

        counts["plotted"] += 1

        shown = profile[visible]
        line.set_ydata(shown)

        history[1:] = history[:-1]
        history[0] = shown
        image.set_data(history)

        searched = profile[cfg.MIN_RANGE_CELLS:]
        noise = float(np.median(searched))
        threshold = noise + cfg.DETECTION_MARGIN_DB
        threshold_line.set_ydata([threshold, threshold])

        peak = cfg.MIN_RANGE_CELLS + int(np.argmax(searched))
        detected = profile[peak] >= threshold

        if detected:
            marker.set_data([ranges[peak]], [profile[peak] + 3])
            detail = "TARGET %8.1f m  %6.1f dBFS" % (ranges[peak],
                                                     profile[peak])
        else:
            marker.set_data([], [])
            detail = "no target above threshold"

        status.set_text("%s\nlock %6.0f  noise %6.1f dBFS\n"
                        "plotted %d  skipped %d"
                        % (detail, quality, noise,
                           counts["plotted"], counts["skipped"]))
        status.set_color("tab:green" if detected else "0.35")

        return line, threshold_line, marker, status, image

    animation = FuncAnimation(fig, update, interval=100, blit=False,
                              cache_frame_data=False)

    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
        print("plotted %d buffers, skipped %d"
              % (counts["plotted"], counts["skipped"]))

    return animation


if __name__ == "__main__":
    main()
