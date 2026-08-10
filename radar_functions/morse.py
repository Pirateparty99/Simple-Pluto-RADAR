import numpy as np

# FCC 47 CFR 97.119(b)(1) accepts CW for station identification, which suits
# a radar with no voice or data path: key a tone on and off on the same
# carrier the radar is already using.

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.",
}


def dot_seconds(wpm):
    """Seconds per dot at a given speed, using the standard PARIS word.

    PARIS is 50 dot-lengths, so at W words per minute a dot is 60/(50*W).
    """
    return 1.2 / wpm


def key_pattern(text, wpm):
    """Expand text into (on, seconds) segments of on/off keying.

    Standard timing: dot = 1 unit, dash = 3, intra-character gap = 1,
    inter-character gap = 3, word gap = 7.
    """
    unit = dot_seconds(wpm)
    segments = []

    words = [w for w in text.upper().split() if w]

    for word_index, word in enumerate(words):
        if word_index:
            segments.append((False, 7 * unit))

        for char_index, char in enumerate(word):
            symbols = MORSE.get(char)
            if symbols is None:
                raise ValueError("no Morse encoding for %r in %r"
                                 % (char, text))

            if char_index:
                segments.append((False, 3 * unit))

            for symbol_index, symbol in enumerate(symbols):
                if symbol_index:
                    segments.append((False, unit))
                segments.append((True, (3 if symbol == "-" else 1) * unit))

    return segments


def duration(text, wpm):
    """Total seconds to send text at the given speed."""
    return sum(seconds for _, seconds in key_pattern(text, wpm))


def cw_tone(fs, amplitude, tone_hz=1000.0, min_samples=4096):
    """A short continuous tone sized to loop seamlessly in a cyclic buffer.

    Station identification keys this tone on and off with the transmit gain
    rather than by generating the whole message as samples. At radar sample
    rates a spelled-out callsign runs to a hundred million samples -- over
    a hundred times the Pluto's 2**23 buffer limit -- so sample-by-sample
    keying is not transmittable. Keying the gain needs one small buffer and
    gives roughly 89 dB of on/off ratio, which is plainly readable.

    The length is rounded to a whole number of cycles so the cyclic buffer
    has no phase discontinuity at the wrap.
    """
    period = fs / tone_hz
    cycles = max(1, int(np.ceil(min_samples / period)))
    n = int(round(cycles * period))

    t = np.arange(n) / fs
    tone = amplitude * np.exp(1j * 2 * np.pi * tone_hz * t)

    return tone.astype(np.complex64)


def cw_waveform(text, fs, amplitude, wpm=15, tone_hz=1000.0, ramp_ms=5.0):
    """Build a complete CW identification waveform, sample by sample.

    Correct but usually too large to transmit: at radar sample rates this
    runs to tens of millions of samples. Kept because it makes the Morse
    encoding verifiable end to end -- a test can demodulate it and check
    the callsign comes back. For transmission use cw_tone() with gain
    keying; see key_pattern().

    text      -- callsign to send
    fs        -- sample rate in Hz
    amplitude -- peak I/Q value, same scaling as the chirp
    wpm       -- keying speed
    tone_hz   -- offset from the carrier, so the ID is not at DC where the
                 receiver's DC-offset correction would fight it
    ramp_ms   -- rise and fall time of each element. Hard keying splatters
                 energy across the band; a short raised-cosine edge keeps
                 the ID inside the channel.

    Returns complex64, ready for sdr.tx().
    """
    segments = key_pattern(text, wpm)
    total = int(round(sum(s for _, s in segments) * fs))

    envelope = np.zeros(total, dtype=np.float64)
    ramp_len = max(1, int(round(ramp_ms * 1e-3 * fs)))

    cursor = 0
    for on, seconds in segments:
        length = int(round(seconds * fs))
        if on and length > 0:
            block = np.ones(length)
            edge = min(ramp_len, length // 2)
            if edge > 0:
                shape = 0.5 * (1 - np.cos(np.pi * np.arange(edge) / edge))
                block[:edge] = shape
                block[-edge:] = shape[::-1]
            envelope[cursor:cursor + length] = block[:max(0, total - cursor)]
        cursor += length

    t = np.arange(total) / fs
    waveform = amplitude * envelope * np.exp(1j * 2 * np.pi * tone_hz * t)

    return waveform.astype(np.complex64)
