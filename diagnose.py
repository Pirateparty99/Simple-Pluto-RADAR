"""Check that TX actually reaches RX before trusting any range output.

The processing chain will happily report confident-looking ranges from pure
noise, so this script answers the prior question: is there a chirp in the
received buffer at all?

Run it with the antennas connected as you intend to use them:

    python diagnose.py
"""
import time

import numpy as np

import main as cfg
from radar_functions.chirp import chirp
from radar_functions.dechirp import find_chirp_start

# TX gains to try, weakest first. Stops early once lock is solid, so a PA
# is not driven harder than it needs to be.
GAIN_SWEEP = [-70, -60, -50, -40, -30, -20, -10, 0]
GOOD_ENOUGH = 100.0

# AD9361 transmit attenuation floor -- quieter than destroying the buffer,
# and leaves the TX path in a known state.
TX_OFF_GAIN = -89

SETTLE_BUFFERS = 5

# The first captures after device init and the first tx() are unreliable --
# the DDS has just been torn down and the AD9361 is still settling. The
# noise floor is the first thing measured, so it needs the most warm-up.
WARMUP_BUFFERS = 20


def rms(x):
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


REPEATS = 15

# Buffers to collect when characterising the ambient environment.
SURVEY_BUFFERS = 40

# Ratio of loudest to quietest buffer above which the environment is
# considered bursty rather than merely noisy.
BURST_RATIO_LIMIT = 10.0

# Where we want the noise floor to sit: low enough to leave headroom for
# strong returns, high enough to stay well clear of the quantisation floor.
TARGET_FLOOR_DBFS = -20.0

# AD9361 receive gain limits, from hardwaregain_available "[-3 1 71]".
RX_GAIN_MIN = -3
RX_GAIN_MAX = 71


# Seconds to wait after changing gain before capturing. The AD9361 runs
# DC-offset and quadrature tracking loops that re-converge after any gain
# change; capturing during that transient produces buffers that disagree
# with each other by 100x and a gain sweep that is not even monotonic.
SETTLE_SECONDS = 0.3

# A gain sweep must be monotonic. Tolerate this much non-monotonicity (dB)
# before declaring the sweep untrustworthy.
MONOTONIC_TOLERANCE_DB = 3.0


def restart_rx(sdr, pause=SETTLE_SECONDS, flush=SETTLE_BUFFERS):
    """Force a clean restart of the receive stream after a settings change.

    Flushing buffers alone is not enough: pyadi-iio holds a persistent
    receive buffer, so stale data keeps arriving after the hardware has
    changed underneath it. Dropping the buffer makes rx() build a new one.
    """
    sdr.rx_destroy_buffer()
    time.sleep(pause)
    for _ in range(flush):
        sdr.rx()


def clipping_fraction(x, full_scale):
    """Fraction of samples sitting at the converter rails.

    RMS understates saturation badly -- a hard-clipped signal still has a
    respectable RMS. This counts the rails directly.
    """
    at_rail = (np.abs(np.real(x)) >= full_scale - 1) | \
              (np.abs(np.imag(x)) >= full_scale - 1)

    return float(np.mean(at_rail))


def capture(sdr, settle=SETTLE_BUFFERS):
    """Discard settling buffers, then measure."""
    for _ in range(settle):
        sdr.rx()
    return sdr.rx()


def measure(sdr, reference, settle=SETTLE_BUFFERS, repeats=REPEATS):
    """Median level and lock quality over several buffers.

    Single buffers off this hardware vary by tens of dB, which made
    successive runs of this script disagree with each other. The median
    across several captures is stable enough to compare.
    """
    levels = []
    qualities = []
    clips = []

    restart_rx(sdr, flush=settle)

    for _ in range(repeats):
        rx = sdr.rx()
        levels.append(rms(rx))
        clips.append(clipping_fraction(rx, cfg.ADC_FULL_SCALE))
        if reference is not None:
            qualities.append(find_chirp_start(rx, reference)[1])

    level = float(np.median(levels))

    # In a bursty environment the median is dragged around by interference.
    # The 10th percentile represents the channel between bursts, which is
    # what the gain setting should be chosen against; clipping is judged on
    # the worst buffer, because a burst that clips ruins that capture.
    quiet = float(np.percentile(levels, 10))

    return {
        "level": level,
        "quiet": quiet,
        "dbfs": 20 * np.log10(level / cfg.ADC_FULL_SCALE + 1e-20),
        "dbfs_quiet": 20 * np.log10(quiet / cfg.ADC_FULL_SCALE + 1e-20),
        "quality": float(np.median(qualities)) if qualities else 0.0,
        "quality_best": float(np.max(qualities)) if qualities else 0.0,
        # Floor the denominator at one LSB: below that the signal is under
        # the quantisation limit and the ratio is meaningless, not enormous.
        "spread": float(np.max(levels) / max(np.min(levels), 1.0)),
        "clipping": float(np.max(clips)),
    }


def survey_environment(sdr):
    """Characterise ambient RF with the transmitter quiet.

    Distinguishes a broken setup from a hostile band. Steady noise gives
    buffers that agree with each other; bursty interference -- WiFi,
    Bluetooth, microwave ovens, all of which live in this band -- gives
    buffers that differ by orders of magnitude over milliseconds.
    """
    restart_rx(sdr)

    levels = np.array([rms(sdr.rx()) for _ in range(SURVEY_BUFFERS)])
    clips = np.array([0.0])

    p10, p50, p90 = np.percentile(levels, [10, 50, 90])
    burst_ratio = float(levels.max() / (levels.min() + 1e-20))

    print("\nAmbient survey (%d buffers, %.1f ms each, transmitter quiet):"
          % (SURVEY_BUFFERS,
             sdr.rx_buffer_size / sdr.sample_rate * 1e3))
    print("  quietest %.1f | 10%% %.1f | median %.1f | 90%% %.1f | loudest %.1f LSB"
          % (levels.min(), p10, p50, p90, levels.max()))
    print("  loudest / quietest = %.0fx" % burst_ratio)

    if burst_ratio > BURST_RATIO_LIMIT:
        print("\n  The band is BURSTY, not just noisy. Individual %.1f ms"
              % (sdr.rx_buffer_size / sdr.sample_rate * 1e3))
        print("  captures differ by %.0fx, which is interference arriving in"
              % burst_ratio)
        print("  packets -- WiFi, Bluetooth and microwave ovens all occupy")
        print("  2.4 GHz, and %.3f GHz sits in the middle of it."
              % (cfg.FC / 1e9))
        print("  This is an RF environment problem, not a settings problem.")
        print("  Options, roughly in order of effort:")
        print("    - Move FC to a quieter corner of the band (2.400 or")
        print("      2.483 GHz) and re-run; your filters still pass it.")
        print("    - Test somewhere with less 2.4 GHz traffic, or disable")
        print("      nearby WiFi while measuring.")
        print("    - Accept it: main.py already discards buffers that fail")
        print("      to lock, so bursts cost throughput, not correctness.")

    return burst_ratio


def measure_floor(sdr, reference, settle=SETTLE_BUFFERS):
    """Receive level with the transmitter attenuated as far as it goes.

    Attenuating fully is more reliable than destroying the buffer: it leaves
    the TX path in a known state rather than an unspecified one.
    """
    sdr.tx_destroy_buffer()
    sdr.tx_hardwaregain_chan0 = TX_OFF_GAIN
    sdr.tx(reference)

    return measure(sdr, None, settle)["level"]


def choose_rx_gain(sdr, reference):
    """Sweep receive gain with the transmitter quiet, and pick a setting.

    Run before anything else: every level measured afterwards is meaningless
    if the converter is clipping, and a saturated receiver cannot see a
    target below the leakage no matter how good the chirp lock looks.
    """
    sdr.tx_destroy_buffer()
    sdr.tx_hardwaregain_chan0 = TX_OFF_GAIN
    sdr.tx(reference)

    original = sdr.rx_hardwaregain_chan0

    print("\nReceive gain sweep, transmitter quiet:")
    print("%-9s %10s %10s %10s %8s"
          % ("RX gain", "quiet dBFS", "med dBFS", "worst clip", "spread"))
    print("-" * 52)

    best = None
    best_dbfs = None
    sweep = []
    for gain in range(RX_GAIN_MAX, RX_GAIN_MIN - 1, -10):
        sdr.rx_hardwaregain_chan0 = gain
        result = measure(sdr, None)
        sweep.append((gain, result))

        print("%-9d %10.1f %10.1f %9.2f%% %7.1fx"
              % (gain, result["dbfs_quiet"], result["dbfs"],
                 result["clipping"] * 100, result["spread"]))

        # Judge headroom against the channel between bursts, but reject any
        # gain where a burst clips -- a clipped burst ruins that buffer.
        if best is None and result["clipping"] < 0.001 \
                and result["dbfs_quiet"] <= TARGET_FLOOR_DBFS:
            best = gain
            best_dbfs = result["dbfs_quiet"]

    # A gain sweep that is not monotonic is not a measurement. Received
    # level can only fall as gain falls, so violations mean the captures
    # are still contaminated and the recommendation below is worthless.
    violations = []
    for (g_hi, r_hi), (g_lo, r_lo) in zip(sweep, sweep[1:]):
        if r_lo["dbfs_quiet"] > r_hi["dbfs_quiet"] + MONOTONIC_TOLERANCE_DB:
            violations.append(
                (g_hi, g_lo, r_lo["dbfs_quiet"] - r_hi["dbfs_quiet"]))

    unstable = max((r["spread"] for _, r in sweep), default=1.0)

    if violations or unstable > 3.0:
        print("\n  WARNING: this sweep is not trustworthy.")
        for g_hi, g_lo, delta in violations:
            print("    level ROSE %.1f dB going from %d dB to %d dB of gain"
                  % (delta, g_hi, g_lo))
        if unstable > 3.0:
            print("    worst buffer-to-buffer spread was %.0fx" % unstable)
        print("  Received level cannot rise as gain falls. If the ambient")
        print("  survey above reported a bursty band, that is the cause and")
        print("  no amount of settling will fix it -- move FC away from the")
        print("  WiFi channels. Otherwise re-run; if it persists, increase")
        print("  SETTLE_SECONDS (currently %.1f s)." % SETTLE_SECONDS)
        print("  Do not act on the recommendation below until a run comes")
        print("  back clean.")

    if best is None:
        best = RX_GAIN_MIN
        print("\n  Even at minimum gain the floor is too high. The LNAs are")
        print("  feeding too much into the Pluto -- consider removing one,")
        print("  or adding attenuation ahead of the RX input.")
    else:
        print("\n  Recommended RX_GAIN = %d, giving a floor of %.1f dBFS."
              % (best, best_dbfs))
        if best_dbfs < TARGET_FLOOR_DBFS - 15:
            print("  That is well below the %.0f dBFS target, but it is the"
                  % TARGET_FLOOR_DBFS)
            print("  highest gain available -- the receiver is not the limit.")
        if best != original:
            print("  Currently %.0f dB in main.py -- update it." % original)

    sdr.rx_hardwaregain_chan0 = best

    return best


def main():
    reference = chirp(cfg.N, cfg.B, cfg.FS, cfg.TX_SCALE)

    print("Connecting to %s ..." % cfg.URI)
    sdr = cfg.configure_sdr()
    print("  sample rate : %.1f MHz" % (sdr.sample_rate / 1e6))
    print("  rx_lo       : %.3f GHz" % (sdr.rx_lo / 1e9))
    print("  buffer      : %d samples" % sdr.rx_buffer_size)
    print("  gain mode   : %s" % sdr.gain_control_mode_chan0)
    print("  rx gain     : %.1f dB" % sdr.rx_hardwaregain_chan0)
    print("  rf bandwidth: %.1f MHz rx, %.1f MHz tx"
          % (sdr.rx_rf_bandwidth / 1e6, sdr.tx_rf_bandwidth / 1e6))

    # Which physical device did the hostname actually resolve to?
    try:
        attrs = sdr._ctx.attrs
        for key in ("hw_model", "hw_serial", "fw_version", "ip,ip-addr"):
            if key in attrs:
                print("  %-12s: %s" % (key, attrs[key]))
    except Exception as exc:
        print("  (could not read context attributes: %s)" % exc)

    if sdr.gain_control_mode_chan0 != "manual":
        print("\n  WARNING: receive AGC is active. Levels below will not be")
        print("  comparable, because the receiver changes its own gain")
        print("  between captures. Fix configure_sdr() before trusting this.")

    # --- what is the band doing? -------------------------------------------
    # Ask this first: a bursty band makes every later measurement noisy, and
    # it is worth knowing that before blaming the settings.
    sdr.tx_destroy_buffer()
    sdr.tx_hardwaregain_chan0 = TX_OFF_GAIN
    sdr.tx(reference)
    burst_ratio = survey_environment(sdr)

    # --- receive gain next -------------------------------------------------
    # Nothing measured later means anything if the converter is clipping.
    rx_gain = choose_rx_gain(sdr, reference)

    # --- noise floor, transmitter quiet -----------------------------------
    floor = measure_floor(sdr, reference, settle=WARMUP_BUFFERS)
    floor_dbfs = 20 * np.log10(floor / cfg.ADC_FULL_SCALE + 1e-20)

    print("\nTX at %d dB (effectively off), RX gain %d: rms = %.1f LSB (%.1f dBFS)"
          % (TX_OFF_GAIN, rx_gain, floor, floor_dbfs))

    # --- sweep transmit gain ----------------------------------------------
    print("\n%-9s %9s %10s %9s %9s %8s"
          % ("TX gain", "RX rms", "vs floor", "lock med", "lock best",
             "clipping"))
    print("-" * 60)

    best = None
    levels = []
    for gain in GAIN_SWEEP:
        sdr.tx_destroy_buffer()
        sdr.tx_hardwaregain_chan0 = gain
        sdr.tx(reference)

        result = measure(sdr, reference)
        level = result["level"]

        # In a bursty band the median lock is dragged down by buffers that
        # happened to catch interference. The best buffer answers the real
        # question -- can this chirp be found when the channel is clear --
        # and main.py discards the rest anyway.
        quality = result["quality_best"]
        rise = 20 * np.log10(level / floor) if floor > 0 else float("inf")
        levels.append(level)

        print("%-9d %9.1f %+9.1f dB %9.1f %9.1f %7.2f%%"
              % (gain, level, rise, result["quality"], quality,
                 result["clipping"] * 100))

        if result["clipping"] > 0.001:
            print("           ^ clipping: this reading and the lock quality")
            print("             above it are not trustworthy.")

        # Stop at the first solid lock. There is no reason to keep raising
        # transmit power once the chirp is clearly visible, and every step
        # up is real power into the PA.
        if quality >= GOOD_ENOUGH:
            best = gain
            break

    # --- re-measure the floor ---------------------------------------------
    # By now the device is thoroughly warmed up. If this disagrees with the
    # first reading, the first one was taken before the AD9361 settled and
    # every "vs floor" figure above is referenced to a bad baseline.
    floor_after = measure_floor(sdr, reference)
    sdr.tx_destroy_buffer()

    drift = 20 * np.log10((floor_after + 1e-20) / (floor + 1e-20))
    print("\nFloor re-measured after sweep: %.1f LSB (%+.1f dB vs first)"
          % (floor_after, drift))

    if abs(drift) > 6:
        print("  The two floor readings disagree, so the first one was taken")
        print("  before the receiver settled. Trust this one, and read the")
        print("  'vs floor' column above as unreliable.")

    # --- sanity check on the levels ---------------------------------------
    # A passive receiver at fixed gain cannot produce less output when the
    # transmitter produces more. If it appears to, the measurement is wrong,
    # not the physics.
    reference_floor = min(floor, floor_after)

    below_floor = [(g, lv) for g, lv in zip(GAIN_SWEEP, levels)
                   if lv < reference_floor * 0.9]

    if below_floor:
        print("\nWARNING: %d transmitting measurement(s) came out below the"
              % len(below_floor))
        print("  TX-off floor, which is not physically possible at fixed gain:")
        for g, lv in below_floor:
            print("    TX %d dB -> %.1f LSB, floor %.1f LSB"
                  % (g, lv, reference_floor))
        print("  Either the floor reading was unsettled, or receive gain is")
        print("  not actually fixed. Check the gain mode reported above.")

    if len(levels) > 2:
        drops = sum(1 for a, b in zip(levels, levels[1:]) if b < a * 0.9)
        if drops > 1:
            print("\nWARNING: RX level fell %d times as TX power rose." % drops)
            if sdr.gain_control_mode_chan0 != "manual":
                print("  Receive AGC is active -- that fully explains it.")
                print("  configure_sdr() must set gain_control_mode_chan0 to")
                print("  'manual' *before* rx_hardwaregain_chan0; pyadi-iio")
                print("  ignores the gain setting otherwise.")
            elif burst_ratio > BURST_RATIO_LIMIT:
                print("  Receive gain is fixed at %.1f dB, so this is not AGC."
                      % sdr.rx_hardwaregain_chan0)
                print("  The ambient survey found a bursty band (%.0fx), which"
                      % burst_ratio)
                print("  explains it: each row caught a different amount of")
                print("  interference. Move FC off the WiFi channels.")
            else:
                print("  Receive gain is fixed at %.1f dB, so this is not AGC,"
                      % sdr.rx_hardwaregain_chan0)
                print("  and the band looked quiet. Suspect the coupling path")
                print("  itself -- a loose SMA or a moving antenna.")

    # --- verdict -----------------------------------------------------------
    print()
    if best is None:
        print("No chirp lock at any gain. TX is not reaching RX.")
        print("Check, in order:")
        print("  1. Both antennas connected, and RX on the RX SMA (not TX).")
        print("  2. Bandpass filters pass %.3f GHz." % (cfg.FC / 1e9))
        print("  3. LNAs actually powered (the Pluto does not supply bias-tee).")
        print("  4. Try a wired loopback TX -> attenuator -> RX to isolate the")
        print("     antennas from the question.")
    else:
        print("Chirp lock achieved at TX_GAIN = %d." % best)
        print("Set TX_GAIN = %d and RX_GAIN = %d in main.py."
              % (best, rx_gain))
        print("Prefer the lowest gain that locks, especially behind a PA.")


if __name__ == "__main__":
    main()
