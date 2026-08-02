import numpy as np


def chirp(N, B, fs, amplitude):
    """Generate one linear FMCW chirp at complex baseband.

    N         -- samples in the chirp
    B         -- swept bandwidth in Hz
    fs        -- sample rate in Hz
    amplitude -- peak I/Q value

    The sweep runs from -B/2 to +B/2 over T = N / fs seconds.
    Returns a complex64 array ready to hand to sdr.tx().

    amplitude is not optional because getting it wrong is silent and fatal.
    pyadi-iio's tx() casts I and Q straight to int16 with no scaling, so a
    unit-amplitude waveform truncates to -1, 0 or 1 out of a +/-32767 range
    and effectively nothing is transmitted. Scale to a fraction of full
    scale -- 2**14 is the usual choice, leaving headroom below the 2**15
    clipping point.
    """
    T = N / fs
    t = np.arange(N) / fs
    k = B / T
    waveform = amplitude * np.exp(1j * 2*np.pi * (0.5 * k * t**2 - B/2 * t))

    return waveform.astype(np.complex64)
