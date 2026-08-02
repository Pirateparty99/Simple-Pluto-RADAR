import numpy as np

C = 299_792_458.0   # speed of light, m/s


def find_chirp_start(rx, reference):
    """Locate the start of the transmitted chirp inside an RX buffer.

    rx        -- received samples, longer than one chirp
    reference -- the transmitted chirp

    The Pluto's cyclic TX buffer free-runs against a free-running RX, so a
    captured buffer starts at an arbitrary point in the chirp. Dechirping
    against a misaligned reference smears the beat tone across the whole
    spectrum, so the offset has to be found first.

    Returns (start, quality):
      start   -- index in rx where the chirp begins
      quality -- correlation peak divided by the median correlation level

    Capture at least twice the chirp length so a full chirp is guaranteed to
    be present.

    quality is the honest answer to "did this actually lock?". Against pure
    noise the matched filter still returns an argmax, and dechirping there
    produces a confident-looking range profile that is entirely meaningless.
    A real chirp gives a sharp peak, typically well above 10. Values near 1
    mean there is no chirp in the buffer -- check that TX is actually
    running and that some leakage reaches RX.

    IMPORTANT: this locks onto the *strongest* return, so it gives you a
    zero-range reference rather than an absolute one. In a normal bench
    setup that is direct TX->RX leakage, which arrives at ~zero delay, and
    every range you measure afterwards is relative to it -- which is what
    you want. But if a target ever outshines the leakage, the profile
    silently re-references itself to that target and all ranges shift. If
    you need absolute range, feed a coupled sample of TX into a second RX
    channel and align against that instead.

    This is a matched filter done as a circular correlation via FFT --
    O(n log n) rather than the O(n*m) of a direct np.correlate, which
    matters when this runs on every buffer.
    """
    n = len(rx)
    if n <= len(reference):
        raise ValueError(
            "rx must be longer than reference (got %d <= %d); "
            "set RX_BUFFER_SIZE to at least 2*N" % (n, len(reference))
        )

    corr = np.fft.ifft(np.fft.fft(rx, n) * np.conj(np.fft.fft(reference, n)))

    # Only lags that leave a whole chirp inside the buffer are real
    # candidates; beyond that the circular correlation has wrapped.
    last_valid = n - len(reference)
    magnitude = np.abs(corr[:last_valid + 1])

    start = int(np.argmax(magnitude))
    peak = magnitude[start]

    if peak <= 0:
        # Nothing at all in the buffer -- no signal is not a perfect lock.
        return start, 0.0

    baseline = np.median(magnitude)
    quality = float(peak / baseline) if baseline > 0 else float("inf")

    return start, quality


def dechirp(rx, reference):
    """Mix received samples against the conjugate of the transmitted chirp.

    On a Pluto the LO is fixed and the sweep lives in baseband, so an echo
    comes back as a delayed chirp rather than as a beat tone. Mixing it with
    the conjugate reference collapses it to a single tone whose frequency is
    proportional to target range.

    rx and reference must be the same length and already aligned -- see
    find_chirp_start().

    For a target at delay tau the resulting tone sits at -k*tau, i.e. at
    negative frequency for positive range. range_fft() accounts for that.

    The reference is normalised to unit amplitude first. The same waveform
    is used for transmit, where it has to be scaled up for the DAC, and
    mixing against it unnormalised would multiply that scale factor into
    every result and inflate the dBFS numbers.
    """
    if len(rx) != len(reference):
        raise ValueError(
            "rx and reference must be the same length (got %d and %d)"
            % (len(rx), len(reference))
        )

    # Constant-envelope waveform, so the RMS magnitude is its amplitude.
    amplitude = np.sqrt(np.mean(np.abs(reference) ** 2))
    if amplitude == 0:
        raise ValueError("reference is all zeros")

    return rx * np.conj(reference) / amplitude


def range_fft(beat, fs, k, n_fft=None, window=True):
    """Turn dechirped samples into a range profile.

    beat  -- output of dechirp()
    fs    -- sample rate in Hz
    k     -- chirp sweep rate in Hz/s, B / T
    n_fft -- FFT length; defaults to len(beat). Zero-padding interpolates
             the profile but does not improve resolution.
    window -- apply a Hann window to suppress sidelobes from strong returns,
             at the cost of a slightly wider main lobe

    Returns (ranges, spectrum) with ranges in metres, ascending, covering
    positive range only.
    """
    n = len(beat)
    if n_fft is None:
        n_fft = n

    taper = np.hanning(n) if window else np.ones(n)

    # Normalising by the window sum keeps the amplitude of a full-scale tone
    # equal to its input amplitude, whatever window is used. That is what
    # makes the dBFS conversion below meaningful.
    spectrum = np.fft.fftshift(np.fft.fft(beat * taper, n_fft)) / np.sum(taper)
    freqs = np.fft.fftshift(np.fft.fftfreq(n_fft, 1 / fs))

    # A target at delay tau produces a tone at -k*tau, so negative beat
    # frequency maps to positive range.
    ranges = -freqs * C / (2 * k)

    # ranges now runs descending; flip so both arrays ascend in range.
    ranges = ranges[::-1]
    spectrum = spectrum[::-1]

    keep = ranges >= 0

    return ranges[keep], spectrum[keep]


def range_profile(rx, reference, fs, k, full_scale, window=True):
    """Run a raw RX buffer through the whole chain.

    rx         -- received samples, longer than one chirp
    reference  -- the transmitted chirp
    fs         -- sample rate in Hz
    k          -- chirp sweep rate in Hz/s
    full_scale -- ADC full-scale amplitude, for the dBFS conversion

    Returns (ranges, profile_dbfs, lock_quality). Check lock_quality before
    trusting the profile -- see find_chirp_start().

    This exists so that everything displaying a range profile runs exactly
    the same steps in the same order.
    """
    start, quality = find_chirp_start(rx, reference)

    beat = dechirp(rx[start:start + len(reference)], reference)
    ranges, spectrum = range_fft(beat, fs, k, window=window)

    return ranges, magnitude_dbfs(spectrum, full_scale), quality


def magnitude_dbfs(spectrum, full_scale):
    """Convert a range-profile spectrum to dBFS, ready for CFAR.

    spectrum   -- output of range_fft(), already normalised by the window sum
    full_scale -- ADC full-scale amplitude. The Pluto's AD9361 is a 12-bit
                  converter and pyadi-iio hands back right-aligned samples,
                  so this is 2**11.

    Results should be at or below 0 dBFS. Positive values mean full_scale is
    wrong for your hardware, not that you found a very strong target.
    """
    magnitude = np.abs(spectrum) / full_scale

    return 20 * np.log10(magnitude + 1e-20)
