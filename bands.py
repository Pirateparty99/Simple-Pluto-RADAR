"""Band profiles and the licensing interlock.

Two kinds of profile:

  Part 15 (ISM)  -- unlicensed, no station identification required
  Part 97 (ham)  -- requires an amateur licence and periodic callsign ID

Selecting a Part 97 profile without a configured callsign is refused before
anything is transmitted. That is the point of this module: the frequency is
one constant, but forgetting to identify is a licence violation, so the code
enforces it rather than relying on memory.

NOT LEGAL ADVICE. Verify allocations against current FCC Part 97 and your
own licence privileges before transmitting. Amateur status is secondary in
parts of these ranges, and allocations in this region have been changing.
"""
import os

# Master transmit policy.
#
# While this is False, only Part 15 ISM profiles may be used, and selecting
# an amateur profile is refused outright -- before the radio is configured
# and before any carrier exists. The amateur profiles and the station
# identification machinery below are complete and tested, but unreachable
# until this is deliberately changed.
#
# Flipping this to True is a decision to transmit outside ISM. Do not do it
# incidentally: confirm your licence privileges cover the exact segment,
# that amateur status there is not secondary to a service you would
# interfere with, and that station identification is working.
ALLOW_NON_ISM_TRANSMIT = False

# Where the callsign is looked up, in order of precedence:
#   1. --callsign on the command line
#   2. RADAR_CALLSIGN in the environment
#   3. the file below
CALLSIGN_ENV = "RADAR_CALLSIGN"
CALLSIGN_FILE = os.path.expanduser("~/.config/simple-pluto-radar/callsign")

# Station identification interval. FCC 47 CFR 97.119(a) requires the callsign
# at least every 10 minutes during, and at the end of, a communication. Nine
# minutes leaves margin for a chirp that runs long between checks.
ID_INTERVAL_SECONDS = 9 * 60


class Band:
    """A band profile, including the gains measured for it.

    Gains are band-specific -- path loss, LNA gain and the ambient noise
    floor all change with frequency -- so they belong with the frequency
    rather than as one global pair of constants. `calibrated` records
    whether these came from a clean diagnose.py run or are placeholders.
    """

    def __init__(self, name, fc, low, high, authority, requires_licence,
                 tx_gain, rx_gain, calibrated=False, note=""):
        self.name = name
        self.fc = fc
        self.low = low
        self.high = high
        self.authority = authority
        self.requires_licence = requires_licence
        self.tx_gain = tx_gain
        self.rx_gain = rx_gain
        self.calibrated = calibrated
        self.note = note

    def contains(self, frequency):
        return self.low <= frequency <= self.high

    def describe(self):
        return "%-10s %7.3f GHz  %s (%.3f-%.3f GHz)  tx %+d rx %+d dB %s%s" % (
            self.name, self.fc / 1e9, self.authority,
            self.low / 1e9, self.high / 1e9,
            self.tx_gain, self.rx_gain,
            "[measured]" if self.calibrated else "[PROVISIONAL]",
            "  -- " + self.note if self.note else "")


BANDS = {
    # Measured 2026-08-02 with diagnose.py: lock 2344 median / 2442 best,
    # 0% clipping, noise floor -21.2 dBFS, floors agreeing within 0.3 dB.
    # RX is at the AD9361 minimum because powered LNAs sit ahead of it.
    "ism-2400": Band(
        "ism-2400", 2_450_000_000, 2_400_000_000, 2_483_500_000,
        "Part 15 ISM", False,
        tx_gain=-70, rx_gain=-3, calibrated=True,
        note="crowded: WiFi, Bluetooth, microwave ovens"),

    # Not yet measured. Expect roughly 7.5 dB more path loss than 2.4 GHz
    # and slightly less LNA gain, so tx starts higher; run diagnose.py.
    "ism-5800": Band(
        "ism-5800", 5_800_000_000, 5_725_000_000, 5_875_000_000,
        "Part 15 ISM", False,
        tx_gain=-50, rx_gain=-3, calibrated=False,
        note="overlaps U-NII-3 WiFi but usually far quieter than 2.4"),

    "ham-13cm": Band(
        "ham-13cm", 2_400_000_000, 2_390_000_000, 2_450_000_000,
        "Part 97 amateur", True,
        tx_gain=-70, rx_gain=-3, calibrated=False,
        note="13 cm; overlaps ISM above 2400"),

    "ham-5cm": Band(
        "ham-5cm", 5_690_000_000, 5_650_000_000, 5_925_000_000,
        "Part 97 amateur", True,
        tx_gain=-50, rx_gain=-3, calibrated=False,
        note="5 cm; 5650-5725 overlaps the U-NII-2C DFS range, where WiFi "
             "must detect radar and vacate"),
}

DEFAULT_BAND = "ism-5800"


class LicenceError(Exception):
    """Raised when a licensed band is selected without a callsign."""


class PolicyError(Exception):
    """Raised when a non-ISM band is selected while policy forbids it."""


def load_callsign(explicit=None):
    """Resolve the station callsign, or None if none is configured."""
    if explicit:
        return explicit.strip().upper()

    from_env = os.environ.get(CALLSIGN_ENV, "").strip()
    if from_env:
        return from_env.upper()

    try:
        with open(CALLSIGN_FILE) as handle:
            from_file = handle.read().strip()
    except OSError:
        return None

    return from_file.upper() if from_file else None


def select(name, callsign=None):
    """Return (band, callsign), refusing licensed bands without a callsign.

    Call this before configuring the radio, so an unlicensed configuration
    is rejected before any carrier is generated.
    """
    if name not in BANDS:
        raise KeyError("unknown band %r; choose from: %s"
                       % (name, ", ".join(sorted(BANDS))))

    band = BANDS[name]

    # Policy gate first: it is the harder constraint, and a refusal here
    # should not depend on whether a callsign happens to be configured.
    if band.requires_licence and not ALLOW_NON_ISM_TRANSMIT:
        raise PolicyError(
            "Band '%s' is outside the Part 15 ISM allocations and transmission\n"
            "there is disabled by policy (bands.ALLOW_NON_ISM_TRANSMIT is False).\n\n"
            "Use an ISM band:\n"
            "    %s\n\n"
            "Enabling non-ISM transmission is a deliberate edit to bands.py, not\n"
            "a command-line option, because it carries regulatory consequences."
            % (band.name,
               ", ".join(n for n, b in sorted(BANDS.items())
                         if not b.requires_licence)))

    resolved = load_callsign(callsign)

    if band.requires_licence and not resolved:
        raise LicenceError(
            "Band '%s' is an amateur allocation (%s) and requires a station\n"
            "callsign for identification under FCC 97.119.\n\n"
            "Set one of:\n"
            "    --callsign YOURCALL\n"
            "    export %s=YOURCALL\n"
            "    echo YOURCALL > %s\n\n"
            "If you do not hold an amateur licence, use an ISM band instead:\n"
            "    %s"
            % (band.name, band.authority, CALLSIGN_ENV, CALLSIGN_FILE,
               ", ".join(n for n, b in sorted(BANDS.items())
                         if not b.requires_licence)))

    return band, resolved


def listing():
    lines = ["Available bands:"]
    for name in sorted(BANDS):
        band = BANDS[name]
        blocked = band.requires_licence and not ALLOW_NON_ISM_TRANSMIT
        lines.append("  %s%s" % (band.describe(),
                                 "  [DISABLED by policy]" if blocked else ""))
    lines.append("")

    if ALLOW_NON_ISM_TRANSMIT:
        lines.append("Amateur bands require --callsign, %s, or %s"
                     % (CALLSIGN_ENV, CALLSIGN_FILE))
    else:
        lines.append("Non-ISM transmission is disabled "
                     "(bands.ALLOW_NON_ISM_TRANSMIT is False).")

    return "\n".join(lines)
