import adi
import numpy as np

from radar_functions.chirp import chirp
from radar_functions.dechirp import C, range_profile

# Radar configuration
URI = "ip:pluto.local"      # or your Pluto URI

# The transmit antenna must be on TX1, the SMA connector.
#
# This code transmits on channel 0 only. adi.Pluto exposes exactly one
# complex transmit channel -- its _tx_channel_names is ["voltage0",
# "voltage1"], which is I and Q of channel 0 -- so it cannot drive the TX2
# pads exposed by the 2R2T modification, whatever the firmware mode says.
# An antenna on TX2 receives nothing; anything that reaches RX in that
# configuration is stray leakage from the TX1 connector.
FS = 20_000_000             # sample rate, Hz
FC = 2_450_000_000          # carrier frequency, Hz
N = 4096                    # samples per chirp
B = 16_000_000              # chirp bandwidth, Hz -- keep below FS
RX_BUFFER_SIZE = 2 * N      # must exceed N so a whole chirp is always captured
# Measured with diagnose.py on a clean run: lock quality ~2100, no clipping,
# noise floor -21 dBFS. RX_GAIN is at the AD9361 minimum because two powered
# LNAs already supply ~40 dB ahead of the Pluto. Re-run diagnose.py after any
# change to the RF chain.
TX_GAIN = -70               # dB -- start low, especially behind a PA
RX_GAIN = -3                # dB -- AD9361 minimum; range is [-3, 71]

# AD9361 analog filter bandwidth. The Pluto defaults to 18 MHz, which only
# just contains a 16 MHz sweep -- the chirp edges land on the filter skirt
# and correlate poorly. Set it explicitly, comfortably wider than B.
RF_BANDWIDTH = 20_000_000   # Hz

# pyadi-iio casts I/Q straight to int16 with no scaling, so the waveform has
# to be pre-scaled into that range. 2**14 leaves headroom below clipping.
TX_SCALE = 2 ** 14

# The AD9361 is a 12-bit converter and samples come back right-aligned.
ADC_FULL_SCALE = 2 ** 11

# Ignore the first few range cells, which are dominated by TX leakage.
MIN_RANGE_CELLS = 5

# Correlation peak-to-median below this means the matched filter did not
# find a chirp, and any range it reports is noise.
#
# Calibrated against measurements, not guessed: pure noise scores about 4, a
# healthy lock on real leakage scores 2000+. The band in between is the
# dangerous part -- buffers scoring 12 to 140 passed the old threshold of 10
# and produced uniformly random ranges.
MIN_LOCK_QUALITY = 200.0

# A peak must exceed the median of the range profile by this much to count
# as a detection. Without it, argmax over a target-free profile just returns
# the loudest noise bin, which looks exactly like a target and is not one.
#
# The loudest of ~2000 noise bins already sits 10-12 dB above the median, so
# anything under about 15 dB is not evidence of a target. This is a crude
# stand-in for CFAR -- target_detection_dbfs.py in the PhaserRadarLabs repo
# drops in unmodified and does the job properly.
DETECTION_MARGIN_DB = 15.0


def configure_sdr():

    sdr = adi.Pluto(URI)

    sdr.sample_rate = FS
    sdr.tx_lo = FC
    sdr.rx_lo = FC

    sdr.rx_rf_bandwidth = RF_BANDWIDTH
    sdr.tx_rf_bandwidth = RF_BANDWIDTH

    sdr.rx_buffer_size = RX_BUFFER_SIZE
    sdr.tx_cyclic_buffer = True
    sdr.tx_hardwaregain_chan0 = TX_GAIN

    # This ordering is load-bearing. pyadi-iio's rx_hardwaregain setter is
    #
    #     if self.gain_control_mode_chan0 == "manual":
    #         self._set_iio_attr_float(...)
    #
    # so it silently does nothing while the Pluto is in its default
    # slow_attack AGC mode. Leaving the AGC running makes received levels
    # non-monotonic in transmit power and wrecks the correlation, because
    # the gain moves between captures.
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = RX_GAIN

    verify_config(sdr)

    return sdr


def verify_config(sdr):
    """Read settings back, since several of them can fail silently."""

    checks = [
        ("gain_control_mode_chan0", "manual", sdr.gain_control_mode_chan0),
        ("rx_hardwaregain_chan0", RX_GAIN, sdr.rx_hardwaregain_chan0),
        ("sample_rate", FS, sdr.sample_rate),
        ("rx_buffer_size", RX_BUFFER_SIZE, sdr.rx_buffer_size),
        ("rx_rf_bandwidth", RF_BANDWIDTH, sdr.rx_rf_bandwidth),
        ("tx_rf_bandwidth", RF_BANDWIDTH, sdr.tx_rf_bandwidth),
    ]

    for name, wanted, actual in checks:
        if isinstance(wanted, str):
            ok = actual == wanted
        else:
            ok = abs(float(actual) - float(wanted)) <= abs(float(wanted)) * 0.01

        if not ok:
            print("WARNING: %s is %r, expected %r" % (name, actual, wanted))

    if B > float(sdr.rx_rf_bandwidth) * 0.9:
        print("WARNING: chirp bandwidth %.1f MHz is close to the receive "
              "filter width %.1f MHz." % (B / 1e6, sdr.rx_rf_bandwidth / 1e6))
        print("         The sweep edges will be attenuated, which degrades "
              "the correlation used for chirp alignment.")


def print_config():

    T = N / FS
    k = B / T

    print("chirp duration  : %.1f us" % (T * 1e6))
    print("sweep rate      : %.2f GHz/s" % (k / 1e9))
    print("range resolution: %.2f m" % (C / (2 * B)))
    print("unambiguous to  : %.0f m" % ((FS / 2) * C / (2 * k)))


def main():

    T = N / FS
    k = B / T

    print_config()

    sdr = configure_sdr()

    reference = chirp(N, B, FS, TX_SCALE)
    sdr.tx(reference)

    resolution = C / (2 * B)
    unlocked = 0

    try:
        while True:
            rx = sdr.rx()

            # The cyclic TX buffer free-runs against a free-running RX, so
            # the chain locates the chirp before dechirping.
            ranges, profile, quality = range_profile(
                rx, reference, FS, k, ADC_FULL_SCALE)

            if quality < MIN_LOCK_QUALITY:
                unlocked += 1
                print("no chirp lock (quality %.1f, need %.1f) -- "
                      "RX rms %.0f. Is TX reaching RX?"
                      % (quality, MIN_LOCK_QUALITY, np.sqrt(np.mean(np.abs(rx) ** 2))))
                if unlocked == 5:
                    print("  See docs/usage.md troubleshooting: check TX_GAIN, "
                          "antenna coupling, and that TX_SCALE is applied.")
                continue

            unlocked = 0

            # Skip the leakage-dominated cells at zero range.
            first = MIN_RANGE_CELLS
            searched = profile[first:]
            peak = first + int(np.argmax(searched))

            # The median of the profile is the noise level. A peak that does
            # not stand clear of it is the loudest noise bin, not a target.
            noise = float(np.median(searched))
            margin = profile[peak] - noise

            if margin < DETECTION_MARGIN_DB:
                print("no target  (best bin %.1f dBFS is only %.1f dB over "
                      "noise %.1f dBFS, lock %.0f)"
                      % (profile[peak], margin, noise, quality))
                continue

            print("target: %8.1f m  %6.1f dBFS  (%+.1f dB over noise, "
                  "lock %.0f, resolution %.1f m)"
                  % (ranges[peak], profile[peak], margin, quality, resolution))

    except KeyboardInterrupt:
        sdr.tx_destroy_buffer()


if __name__ == "__main__":
    main()
