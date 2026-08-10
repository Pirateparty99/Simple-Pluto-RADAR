import argparse
import time

import adi
import numpy as np

import bands
from radar_functions.chirp import chirp
from radar_functions.dechirp import C, range_profile
from radar_functions.morse import cw_tone, key_pattern

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

# Carrier frequency, from the selected band profile. apply_band() overwrites
# this; the value here is what an importer sees before any band is chosen.
FC = bands.BANDS[bands.DEFAULT_BAND].fc     # Hz

# Station identification, for amateur-band operation only.
ID_WPM = 15                 # keying speed
ID_TONE_HZ = 1000.0         # offset from the carrier, away from DC
ID_KEY_OFF_GAIN = -89       # AD9361 transmit attenuation floor, key-up
N = 4096                    # samples per chirp
B = 16_000_000              # chirp bandwidth, Hz -- keep below FS
RX_BUFFER_SIZE = 2 * N      # must exceed N so a whole chirp is always captured
# Gains are band-specific and live in bands.py alongside the frequency, since
# path loss, LNA gain and the noise floor all change with frequency. These
# are the DEFAULT_BAND values; apply_band() overwrites them when a band is
# selected. Re-run diagnose.py after any change to the RF chain.
TX_GAIN = bands.BANDS[bands.DEFAULT_BAND].tx_gain   # dB
RX_GAIN = bands.BANDS[bands.DEFAULT_BAND].rx_gain   # dB; AD9361 range [-3, 71]

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


def send_station_id(sdr, callsign, reference):
    """Interrupt the radar to transmit the callsign in CW, then resume.

    The tone is a small cyclic buffer and the keying is done by switching
    the transmit gain. Spelling the message out in samples would need about
    a hundred million of them at radar sample rates -- twelve times the
    Pluto's 2**23 buffer limit -- so gain keying is the practical route. It
    gives roughly 89 dB of on/off ratio, which is plainly readable.

    Buffers missed during identification are simply not captured, which the
    run loop already tolerates.
    """
    segments = key_pattern(callsign, ID_WPM)
    seconds = sum(s for _, s in segments)
    print("station ID: %s (%.1f s CW)" % (callsign, seconds))

    sdr.tx_destroy_buffer()

    tone = cw_tone(FS, TX_SCALE, tone_hz=ID_TONE_HZ)
    sdr.tx_cyclic_buffer = True
    sdr.tx(tone)

    try:
        for on, length in segments:
            sdr.tx_hardwaregain_chan0 = TX_GAIN if on else ID_KEY_OFF_GAIN
            time.sleep(length)
    finally:
        sdr.tx_hardwaregain_chan0 = TX_GAIN
        sdr.tx_destroy_buffer()
        sdr.tx(reference)


def apply_band(name, callsign=None):
    """Select a band and adopt its frequency and gains as configuration.

    Returns (band, callsign). Raises bands.PolicyError or bands.LicenceError
    before anything is applied, so a refused band never reaches the radio.

    diagnose.py and visualize.py call this too, so every entry point tunes
    and sets gains the same way.
    """
    global FC, TX_GAIN, RX_GAIN

    band, resolved = bands.select(name, callsign)

    FC = band.fc
    TX_GAIN = band.tx_gain
    RX_GAIN = band.rx_gain

    return band, resolved


def describe_band(band, callsign):
    print("band            : %s" % band.describe().strip())

    if not band.calibrated:
        print("                  gains are PROVISIONAL for this band -- "
              "run diagnose.py")
    if band.requires_licence:
        print("station         : %s, identifying every %d minutes (97.119)"
              % (callsign, bands.ID_INTERVAL_SECONDS // 60))


def add_band_arguments(parser):
    """Shared --band/--callsign options for every entry point."""
    parser.add_argument("--band", default=bands.DEFAULT_BAND,
                        choices=sorted(bands.BANDS),
                        help="band profile (default: %(default)s)")
    parser.add_argument("--callsign", default=None,
                        help="station callsign; required on amateur bands")

    return parser


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Simple Pluto RADAR",
        epilog=bands.listing(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_band_arguments(parser)
    parser.add_argument("--list-bands", action="store_true",
                        help="print the band table and exit")

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.list_bands:
        print(bands.listing())
        return

    # Resolve the band before touching the radio, so a refused configuration
    # never reaches the point of generating a carrier.
    try:
        band, callsign = apply_band(args.band, args.callsign)
    except (bands.PolicyError, bands.LicenceError) as exc:
        print("REFUSED: %s" % exc)
        raise SystemExit(2)

    describe_band(band, callsign)

    T = N / FS
    k = B / T

    print_config()

    sdr = configure_sdr()

    reference = chirp(N, B, FS, TX_SCALE)
    sdr.tx(reference)

    resolution = C / (2 * B)
    unlocked = 0

    # Identify at the start of transmission, then on the interval.
    if callsign and band.requires_licence:
        send_station_id(sdr, callsign, reference)
    last_id = time.monotonic()

    try:
        while True:
            if (callsign and band.requires_licence
                    and time.monotonic() - last_id >= bands.ID_INTERVAL_SECONDS):
                send_station_id(sdr, callsign, reference)
                last_id = time.monotonic()

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
        # 97.119 also requires identification at the end of a communication.
        if callsign and band.requires_licence:
            try:
                send_station_id(sdr, callsign, reference)
            except Exception as exc:
                print("WARNING: final station ID failed: %s" % exc)

        sdr.tx_destroy_buffer()


if __name__ == "__main__":
    main()
