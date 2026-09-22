#!/usr/bin/env python3
"""
bitfall.py - encode a 1-bit image into a complex-baseband OFDM waveform.

Each row of the image becomes one OFDM symbol. Each column of the image is
mapped to one subcarrier. A black pixel switches that subcarrier ON, a white
pixel leaves it OFF. Symbols are built with a complex IFFT, given a cyclic
prefix, windowed and overlap-added, exactly like an OFDM transmitter.

Transmit the result and the image appears in the waterfall.

A spectrum analyser paints tens of waterfall lines per second, so one image
row has to last tens of milliseconds or every line integrates the whole
picture into a flat block. Set grid.line_time ("20ms") and each row is held
over as many symbols as it takes, with the subcarrier spacing nudged so the
row lands on that time exactly.

The waterfall axes are rarely to scale, so the picture usually lands squashed
or stretched. image.stretch scales the row count to square it back up.

image.silence appends blank rows after the picture. The waterfall then shows
a gap of dead air, which separates one copy of the image from the next on a
looping ARB and gives the eye something to measure the line rate against.

All configuration is JSON:

    python bitfall.py config.json
    python bitfall.py --write-template config.json
    python bitfall.py config.json --print-config

Output is complex baseband IQ:
    cf32  float32 I,Q     GNU Radio .cfile, SoapySDR, UHD
    ci16  int16   I,Q     PlutoSDR, USRP sc16, bladeRF
    ci8   int8    I,Q     HackRF
    csv   text              one "i,q" line per sample, no header
    wv    tagged int16      Rohde & Schwarz ARB waveform
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

# ==========================================================================
# configuration schema
# ==========================================================================
#
# (default, kind) per key.  kind drives coercion and validation:
#   "path"      str or null, resolved relative to the config file
#   "freq"      number or SI string ("10k", "2.5M", "2.45G")
#   "time"      seconds, or a string with a unit ("20ms")   "time?"  or null
#   "int"       integer            "int?"    integer or null
#   "pow2"      integer, power of two
#   "frac"      float in [0, 1]    "float?"  float or null
#   "bool"      boolean            "str"     string, checked against CHOICES

SCHEMA: dict[str, dict[str, tuple]] = {
    "image": {
        "path":        (None,  "path"),
        "dither":      (True,  "bool"),
        "threshold":   (None,  "int?"),
        "invert":      (False, "bool"),
        "transpose":   (False, "bool"),
        "flip_time":   (False, "bool"),
        "stretch":     (1.0,   "float"),
        "max_symbols": (None,  "int?"),
        "silence":     (0,     "int"),
    },
    "grid": {
        "carriers":           (256,    "pow2"),
        "fft_size":           (None,   "int?"),   # default: 2 * carriers
        "subcarrier_spacing": (10e3,   "freq"),
        "cyclic_prefix":      (0.0625, "frac"),
        "window":             (0.03,   "frac"),
        "null_dc":            (True,   "bool"),
        "phase_seed":         (1234,   "int"),
        "line_time":          (None,   "time?"),
        "symbol_repeat":      (None,   "int?"),
    },
    "output": {
        "path":             (None,   "path"),
        "format":           ("cf32", "str"),
        "peak":             (0.95,   "frac"),
        "csv_precision":    (6,      "int"),
        "clip_papr_db":     (None,   "float?"),
        "centre_frequency": (0.0,    "freq"),
        "sigmf":            (True,   "bool"),
        "marker":           (False,  "bool"),
    },
    "diagnostics": {
        "plot":               (None,  "path"),
        "plot_dpi":           (130,   "int"),
        "plot_dynamic_range": (60.0,  "float"),
        "bitmap":             (None,  "path"),
        "verify":             (False, "bool"),
    },
}

CHOICES = {("output", "format"): ("cf32", "ci16", "ci8", "csv", "wv")}

# format -> (sample dtype, full scale, SigMF datatype)
# A SigMF datatype of None marks a container that is not a bare IQ stream, so
# a .sigmf-meta sidecar would describe something the file does not contain.
FORMATS = {
    "cf32": (np.float32, 1.0, "cf32_le"),
    "ci16": (np.int16, 32767.0, "ci16_le"),
    "ci8": (np.int8, 127.0, "ci8"),
    "csv": (None, 1.0, None),
    "wv": (np.int16, 32768.0, None),
}

SI = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "": 1.0}

# Longest suffix first, so "ms" is matched before "s".
TIME_UNITS = (("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0))


class ConfigError(Exception):
    pass


def parse_si(value, where: str) -> float:
    """Accept 10000, 1e4, '10k', '2.5M', '2.45G'."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected a frequency, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        for tail in ("Hz", "hz"):
            if s.endswith(tail):
                s = s[: -len(tail)].strip()
        suffix = s[-1] if s and s[-1] in SI else ""
        try:
            return float(s[:-1] if suffix else s) * SI[suffix]
        except (ValueError, KeyError):
            pass
    raise ConfigError(f"{where}: cannot read {value!r} as a frequency")


def parse_time(value, where: str) -> float:
    """Accept 0.02, 2e-2, '20ms', '20 ms', '50us'. A bare number means seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected a time, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        for tail, scale in TIME_UNITS:
            if s.endswith(tail):
                s = s[: -len(tail)].strip()
                break
        else:
            scale = 1.0
        try:
            return float(s) * scale
        except ValueError:
            pass
    raise ConfigError(f"{where}: cannot read {value!r} as a time (try 0.02 or '20ms')")


def is_pow2(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def next_pow2(n: int) -> int:
    return 1 << max(0, int(n) - 1).bit_length()


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_raw(path: Path, _seen: set | None = None) -> dict:
    """Read a config, following "extends" and deep-merging the child over it."""
    _seen = set() if _seen is None else _seen
    path = path.expanduser().resolve()
    if path in _seen:
        raise ConfigError(f"circular extends chain at {path}")
    _seen.add(path)

    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON at line {e.lineno} col {e.colno}: {e.msg}")

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")

    parent = raw.pop("extends", None)
    if parent is None:
        raw["__dir__"] = str(path.parent)
        return raw

    base = load_raw(path.parent / parent, _seen)
    base.pop("__dir__", None)
    merged = deep_merge(base, raw)
    merged["__dir__"] = str(path.parent)  # paths resolve against the child
    return merged


def coerce(section: str, key: str, value, kind: str, base_dir: Path):
    where = f"{section}.{key}"
    if value is None:
        if kind.endswith("?") or kind == "path":
            return None
        raise ConfigError(f"{where}: must not be null")

    if kind == "path":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a path string")
        p = Path(value).expanduser()
        return p if p.is_absolute() else (base_dir / p)

    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false, got {value!r}")
        return value

    if kind == "freq":
        return parse_si(value, where)

    if kind in ("time", "time?"):
        return parse_time(value, where)

    if kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string")
        allowed = CHOICES.get((section, key))
        if allowed and value not in allowed:
            raise ConfigError(f"{where}: must be one of {', '.join(allowed)}, got {value!r}")
        return value

    if kind in ("int", "int?", "pow2"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ConfigError(f"{where}: expected an integer, got {value!r}")
        value = int(value)
        if kind == "pow2" and not is_pow2(value):
            raise ConfigError(f"{where}: must be a power of two (try {next_pow2(value)})")
        return value

    if kind in ("float", "float?", "frac"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        value = float(value)
        if kind == "frac" and not 0.0 <= value <= 1.0:
            raise ConfigError(f"{where}: must be between 0 and 1, got {value}")
        return value

    raise ConfigError(f"{where}: unhandled kind {kind}")


def resolve(raw: dict) -> dict:
    """Validate, coerce and fill defaults. Unknown keys are an error."""
    base_dir = Path(raw.pop("__dir__", "."))

    unknown = set(raw) - set(SCHEMA)
    if unknown:
        raise ConfigError(
            f"unknown section(s): {', '.join(sorted(unknown))}. "
            f"expected: {', '.join(SCHEMA)}"
        )

    cfg: dict[str, dict] = {}
    for section, keys in SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            raise ConfigError(f"{section}: must be an object")
        bad = set(given) - set(keys)
        if bad:
            # A silently ignored typo would produce a plausible but wrong
            # waveform, so refuse rather than fall back to the default.
            raise ConfigError(
                f"unknown key(s) in {section}: {', '.join(sorted(bad))}. "
                f"valid keys: {', '.join(sorted(keys))}"
            )
        cfg[section] = {
            k: coerce(section, k, given.get(k, default), kind, base_dir)
            for k, (default, kind) in keys.items()
        }

    if cfg["image"]["path"] is None:
        raise ConfigError("image.path is required")
    if not cfg["image"]["path"].exists():
        raise ConfigError(f"image.path does not exist: {cfg['image']['path']}")
    if cfg["image"]["threshold"] is not None and not 0 <= cfg["image"]["threshold"] <= 255:
        raise ConfigError("image.threshold must be between 0 and 255")
    if cfg["image"]["stretch"] <= 0:
        raise ConfigError("image.stretch must be positive")
    if cfg["image"]["silence"] < 0:
        raise ConfigError("image.silence must not be negative")

    g = cfg["grid"]
    if g["fft_size"] is None:
        g["fft_size"] = 2 * g["carriers"]
    if not is_pow2(g["fft_size"]):
        raise ConfigError(
            f"grid.fft_size must be a power of two (try {next_pow2(g['fft_size'])})")
    if g["fft_size"] < g["carriers"]:
        raise ConfigError(
            f"grid.fft_size ({g['fft_size']}) must be >= grid.carriers ({g['carriers']})")
    if g["null_dc"] and g["fft_size"] < g["carriers"] + 1:
        raise ConfigError(
            f"grid.null_dc needs a spare bin: set grid.fft_size to "
            f"{next_pow2(g['carriers'] + 1)}, or grid.null_dc to false")
    if g["subcarrier_spacing"] <= 0:
        raise ConfigError("grid.subcarrier_spacing must be positive")
    if g["line_time"] is not None and g["symbol_repeat"] is not None:
        raise ConfigError(
            "grid.line_time and grid.symbol_repeat are two ways to say the same thing; "
            "set one or the other")
    if g["line_time"] is not None and g["line_time"] <= 0:
        raise ConfigError("grid.line_time must be positive")
    if g["symbol_repeat"] is not None and g["symbol_repeat"] < 1:
        raise ConfigError("grid.symbol_repeat must be at least 1")

    if not 1 <= cfg["output"]["csv_precision"] <= 17:
        raise ConfigError("output.csv_precision must be between 1 and 17")

    if cfg["output"]["path"] is None:
        cfg["output"]["path"] = cfg["image"]["path"].with_suffix("." + cfg["output"]["format"])

    return cfg


TEMPLATE = {
    "image": {"path": "input.png", "dither": True},
    "grid": {
        "carriers": 256,
        "fft_size": 512,
        "subcarrier_spacing": "10k",
        "line_time": None,
        "cyclic_prefix": 0.0625,
        "window": 0.03,
        "null_dc": True,
        "phase_seed": 1234,
    },
    "output": {
        "path": "out.cf32",
        "format": "cf32",
        "peak": 0.95,
        "clip_papr_db": 8.0,
        "centre_frequency": "2.45G",
        "sigmf": True,
    },
    "diagnostics": {"plot": "out_plot.png", "verify": True},
}


# ==========================================================================
# image -> boolean matrix  (True = subcarrier ON)
# ==========================================================================


def prepare_bitmap(cfg: dict, carriers: int) -> tuple[np.ndarray, Image.Image]:
    """Return (on[n_symbols, carriers], the grayscale image as loaded)."""
    img = Image.open(cfg["path"])

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img.convert("RGBA"))
    original = img.convert("L")

    work = original
    if cfg["transpose"]:
        # For displays that scroll time sideways instead of downwards.
        work = work.rotate(-90, expand=True)

    # The column count is fixed by the carrier count, so the row count is what
    # the aspect ratio asks for. `stretch` scales that: the picture gets taller
    # in time without getting wider in frequency, which is how you square up a
    # waterfall whose axes are not to scale. Above 1 it resamples from the
    # source, so detail survives up to the source height.
    w, h = work.size
    height = max(1, round(h * carriers / w * cfg["stretch"]))
    if cfg["max_symbols"] is not None and height > cfg["max_symbols"]:
        height = cfg["max_symbols"]
    work = work.resize((carriers, height), Image.LANCZOS)

    if cfg["dither"]:
        bw = work.convert("1")  # Floyd-Steinberg
    else:
        thr = 128 if cfg["threshold"] is None else cfg["threshold"]
        bw = work.point(lambda p: 255 if p > thr else 0).convert("1")

    on = ~np.array(bw, dtype=bool)  # black pixel -> ON
    if cfg["invert"]:
        on = ~on
    if cfg["flip_time"]:
        on = on[::-1]

    # Blank rows after the picture: every subcarrier OFF, so the waterfall
    # shows an empty band for that long. On a looping ARB this is the gap
    # between one copy of the image and the next, which is what makes the
    # spacing - and the line rate - readable off the display.
    if cfg["silence"]:
        on = np.vstack([on, np.zeros((cfg["silence"], carriers), dtype=bool)])
    return on, original


# ==========================================================================
# resource grid
# ==========================================================================


class Plan:
    """Subcarrier allocation and timing. Shared by modulator and demodulator."""

    def __init__(self, n_car, n_fft, scs, cp_len, win_len, bins, phases, repeat=1):
        self.n_car = n_car
        self.n_fft = n_fft
        self.scs = scs
        self.cp_len = cp_len
        self.win_len = win_len
        self.bins = bins
        self.phases = phases
        self.repeat = repeat  # symbols per image row

    @property
    def fs(self) -> float:
        return self.n_fft * self.scs

    @property
    def sym_len(self) -> int:
        return self.n_fft + self.cp_len

    @property
    def line_len(self) -> int:
        """Samples per image row: one symbol, or `repeat` identical ones."""
        return self.repeat * self.sym_len

    @property
    def occupied_bw(self) -> float:
        return self.n_car * self.scs

    def describe(self) -> str:
        t_s = self.sym_len / self.fs
        return (
            f"  active carriers  : {self.n_car}\n"
            f"  IFFT size        : {self.n_fft}"
            f"   (guard {100 * (1 - self.n_car / self.n_fft):.1f}% of Nyquist)\n"
            f"  subcarrier space : {self.scs:,.1f} Hz\n"
            f"  sample rate      : {self.fs:,.1f} Sa/s\n"
            f"  occupied BW      : {self.occupied_bw:,.1f} Hz"
            f"  ({-self.occupied_bw / 2:+,.0f} .. {self.occupied_bw / 2:+,.0f} Hz)\n"
            f"  useful sym time  : {1e6 / self.scs:,.1f} us\n"
            f"  cyclic prefix    : {self.cp_len} samples"
            f"  ({self.cp_len / self.fs * 1e6:,.1f} us)\n"
            f"  total sym time   : {t_s * 1e6:,.1f} us  -> {1 / t_s:,.1f} symbols/s\n"
            f"  symbols per row  : {self.repeat}\n"
            f"  row (line) time  : {t_s * self.repeat * 1e3:,.3f} ms"
            f"  -> {self.fs / self.line_len:,.2f} lines/s\n"
            f"  tx window ramp   : {self.win_len} samples"
        )


def plan_grid(g: dict) -> Plan:
    n_car, n_fft = g["carriers"], g["fft_size"]

    # Frequency offsets, in subcarrier units, for image column 0 .. n_car-1.
    if g["null_dc"]:
        # Skip offset 0 so LO leakage / DC offset does not sit on a pixel.
        lo = -(n_car // 2)
        offsets = np.concatenate([np.arange(lo, 0), np.arange(1, n_car - abs(lo) + 1)])
    else:
        offsets = np.arange(n_car) - n_car // 2

    if offsets.max() >= n_fft // 2 or offsets.min() < -(n_fft // 2):
        raise ConfigError("carrier allocation does not fit inside the IFFT; raise grid.fft_size")

    bins = offsets.astype(int) % n_fft  # negative offsets wrap to the top half

    cp_len = int(round(g["cyclic_prefix"] * n_fft))
    win_len = max(0, min(int(round(g["window"] * n_fft)), cp_len))  # ramp lives in the CP

    # Fixed random phase per subcarrier:
    #   random -> crest factor of a few dB instead of an impulse
    #   fixed  -> every carrier is phase-continuous from symbol to symbol
    rng = np.random.default_rng(g["phase_seed"])
    phases = rng.uniform(0.0, 2.0 * np.pi, n_car)

    scs, repeat = g["subcarrier_spacing"], g["symbol_repeat"] or 1
    if g["line_time"] is not None:
        # An analyser needs a dwell of at least 1/scs to tell the subcarriers
        # apart, and no more than one row to tell the rows apart. At one symbol
        # per row those two bounds are the same number, so no analyser setting
        # works. Holding each row over `repeat` symbols opens the gap.
        hop = n_fft + cp_len
        repeat = max(1, round(g["line_time"] * n_fft * scs / hop))
        # Nudge the spacing so `repeat` symbols last exactly line_time. The move
        # is under half a symbol in `repeat`, so the occupied bandwidth stays
        # within about 1/(2*repeat) of what was asked for.
        scs = repeat * hop / (n_fft * g["line_time"])

    return Plan(n_car, n_fft, scs, cp_len, win_len, bins, phases, repeat)


# ==========================================================================
# modulation
# ==========================================================================


def modulate(on: np.ndarray, plan: Plan) -> np.ndarray:
    if plan.repeat > 1:
        on = np.repeat(on, plan.repeat, axis=0)  # hold each image row
    n_sym = on.shape[0]
    L, cp, w = plan.n_fft, plan.cp_len, plan.win_len
    hop = L + cp

    tones = np.exp(1j * plan.phases)
    grid = np.zeros(L, dtype=np.complex128)

    # Each symbol is [cyclic prefix | IFFT output | cyclic suffix of w samples].
    # The suffix of symbol t overlaps the prefix of symbol t+1; a
    # power-complementary raised cosine crossfades them, which is what keeps
    # spectral regrowth down at the symbol boundaries.
    if w > 0:
        t = (np.arange(w) + 0.5) / w
        ramp_up = np.sin(0.5 * np.pi * t) ** 2
        ramp_dn = 1.0 - ramp_up

    out = np.zeros(n_sym * hop + w, dtype=np.complex128)

    for i in range(n_sym):
        row = on[i]
        grid[:] = 0.0
        if row.any():
            grid[plan.bins[row]] = tones[row]
        x = np.fft.ifft(grid) * L  # unit-amplitude carriers

        block = np.concatenate([x[L - cp:], x, x[:w]]) if w else np.concatenate([x[L - cp:], x])
        if w > 0:
            block[:w] *= ramp_up
            block[-w:] *= ramp_dn

        start = i * hop
        out[start:start + len(block)] += block

    return out.astype(np.complex64)


def clip_papr(iq: np.ndarray, plan: Plan, target_db: float, iters: int = 4) -> np.ndarray:
    """Iterative clip-and-filter: trade EVM for crest factor."""
    x = iq.astype(np.complex128).copy()
    keep = np.zeros(plan.n_fft, dtype=bool)
    keep[plan.bins] = True
    L, cp, hop = plan.n_fft, plan.cp_len, plan.n_fft + plan.cp_len
    n_sym = max(0, (len(x) - plan.win_len) // hop)
    for _ in range(iters):
        rms = np.sqrt(np.mean(np.abs(x) ** 2))
        thr = rms * 10 ** (target_db / 20.0)
        mag = np.abs(x)
        over = mag > thr
        if not over.any():
            break
        x[over] *= thr / mag[over]
        for i in range(n_sym):  # re-null out-of-band, per symbol
            s = i * hop + cp
            seg = x[s:s + L]
            if len(seg) < L:
                continue
            S = np.fft.fft(seg)
            S[~keep] = 0.0
            x[s:s + L] = np.fft.ifft(S)
    return x.astype(np.complex64)


def papr_db(iq: np.ndarray) -> float:
    p = np.abs(iq.astype(np.complex128)) ** 2
    return 10.0 * np.log10(p.max() / p.mean())


def demodulate(iq: np.ndarray, plan: Plan, n_sym: int) -> np.ndarray:
    L, cp, hop = plan.n_fft, plan.cp_len, plan.n_fft + plan.cp_len
    mags = np.zeros((n_sym, plan.n_car))
    for i in range(n_sym):
        s = i * hop + cp
        seg = iq[s:s + L]
        if len(seg) < L:
            seg = np.pad(seg, (0, L - len(seg)))
        mags[i] = np.abs(np.fft.fft(seg)[plan.bins])
    ref = mags.max() if mags.size else 1.0
    return mags > 0.5 * ref


# ==========================================================================
# output
# ==========================================================================


def interleave(x: np.ndarray) -> np.ndarray:
    inter = np.empty(2 * len(x), dtype=np.float64)
    inter[0::2] = x.real
    inter[1::2] = x.imag
    return inter


def write_iq(path: Path, iq: np.ndarray, out_cfg: dict, plan: Plan) -> None:
    fmt = out_cfg["format"]
    x = iq.astype(np.complex64)
    m = np.max(np.abs(x))
    if m > 0:
        x = x * (out_cfg["peak"] / m)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "csv":
        write_csv(path, x, out_cfg["csv_precision"])
        return
    if fmt == "wv":
        write_wv(path, x, plan, out_cfg["marker"])
        return

    dtype, full_scale, _ = FORMATS[fmt]
    inter = interleave(x)
    if dtype is np.float32:
        inter.astype("<f4").tofile(path)
    else:
        np.clip(np.round(inter * full_scale), -full_scale, full_scale).astype(dtype).tofile(path)


def write_csv(path: Path, x: np.ndarray, precision: int) -> None:
    """One sample per line as "i,q", no header, normalised floats."""
    with path.open("w", newline="\n") as fp:
        np.savetxt(fp, np.column_stack([x.real, x.imag]),
                   fmt=f"%.{precision}g", delimiter=",")


def write_wv(path: Path, x: np.ndarray, plan: Plan, marker: bool = False) -> None:
    """Rohde & Schwarz ARB waveform: ASCII tags, then one binary blob.

    Tag names and order follow the R&S reference writer. The checksum field of
    the TYPE tag is 0, which tells the instrument not to verify it.

    With `marker`, the file also carries a control list whose marker 1 goes high
    for the first image row. Feed the generator's marker output to the
    analyser's external trigger and every acquisition starts at the top of the
    picture, so the waterfall stops rolling.
    """
    dtype, full_scale, _ = FORMATS["wv"]
    info = np.iinfo(dtype)
    samples = np.clip(np.round(interleave(x) * full_scale),
                      info.min, info.max).astype("<i2")
    data = samples.tobytes()

    # LEVEL OFFS states how far rms and peak sit *below* digital full scale,
    # as positive dB, so the generator can set absolute output power.
    mag = np.abs(x.astype(np.complex128))
    rms = float(np.sqrt(np.mean(mag ** 2))) if mag.size else 0.0
    pk = float(mag.max()) if mag.size else 0.0
    rms_offs = -20.0 * np.log10(rms) if rms > 0 else 0.0
    peak_offs = -20.0 * np.log10(pk) if pk > 0 else 0.0

    # R&S parsers accept only word characters, spaces and dots in a COMMENT.
    comment = f"bitfall.py {plan.n_car} carriers {plan.scs:.0f} Hz spacing"
    # Omitted entirely unless asked for, so an unmarked file stays byte for
    # byte what earlier versions wrote.
    mark = ""
    if marker and len(x):
        pulse = min(plan.line_len, len(x))
        mark = f"{{CONTROL LENGTH: {len(x)}}}{{MARKER LIST 1: 0:1;{pulse}:0}}"

    head = (
        "{TYPE: SMU-WV,0}"
        f"{{COMMENT: {comment}}}"
        f"{{DATE: {datetime.now().strftime('%Y-%m-%d;%H:%M:%S')}}}"
        f"{{CLOCK: {plan.fs:.6f}}}"
        f"{{LEVEL OFFS: {rms_offs:.6f},{peak_offs:.6f}}}"
        f"{{SAMPLES: {len(x)}}}"
        f"{mark}"
        f"{{WAVEFORM-{len(data) + 1}:#"
    ).encode("ascii")

    with path.open("wb") as fp:
        fp.write(head)
        fp.write(data)
        fp.write(b"}")


def write_sigmf(path: Path, plan: Plan, fmt: str, n_sym: int, centre: float) -> None:
    meta = {
        "global": {
            "core:datatype": FORMATS[fmt][2],
            "core:sample_rate": plan.fs,
            "core:version": "1.0.0",
            "core:description": (
                f"OFDM image transmission, {plan.n_car} subcarriers, "
                f"{n_sym * plan.repeat} symbols, OOK per subcarrier (black pixel = ON)"
                + (f", {plan.repeat} symbols per image row" if plan.repeat > 1 else "")
            ),
            "core:recorder": "img2ofdm_iq.py",
        },
        "captures": [
            {
                "core:sample_start": 0,
                "core:frequency": centre,
                "core:datetime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        ],
        "annotations": [],
    }
    path.write_text(json.dumps(meta, indent=2))


def save_bitmap(path: Path, on: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.where(on, 0, 255).astype(np.uint8), mode="L").save(path)


# ==========================================================================
# plotting
# ==========================================================================


def compute_waterfall(iq: np.ndarray, plan: Plan) -> np.ndarray:
    """Unsynchronised STFT: what a receiver that knows nothing would see."""
    L = plan.n_fft
    hop = max(1, L // 2)
    win = np.hanning(L)
    n = max(1, (len(iq) - L) // hop)
    S = np.empty((n, L))
    for i in range(n):
        seg = iq[i * hop:i * hop + L]
        if len(seg) < L:
            seg = np.pad(seg, (0, L - len(seg)))
        S[i] = np.abs(np.fft.fftshift(np.fft.fft(seg * win)))
    return 20 * np.log10(S + 1e-12)


def _freq_scale(fs: float) -> tuple[float, str]:
    if fs >= 1e6:
        return 1e6, "MHz"
    if fs >= 1e3:
        return 1e3, "kHz"
    return 1.0, "Hz"


def save_plot(path, original, on, iq, plan, dpi, dyn_range) -> bool:
    """Original image, transmitted grid and waterfall, side by side."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed - skipping plot", file=sys.stderr)
        return False

    S = compute_waterfall(iq, plan)
    vmax = S.max()
    fscale, funit = _freq_scale(plan.fs)
    f_edge = plan.fs / 2 / fscale
    t_end = len(iq) / plan.fs * 1e3  # ms

    fig, axes = plt.subplots(
        1, 3, figsize=(15, 6.5), dpi=dpi,
        gridspec_kw={"width_ratios": [1, 1, 1.35]},
    )

    ax = axes[0]
    ax.imshow(np.asarray(original), cmap="gray", vmin=0, vmax=255, aspect="auto")
    ax.set_title(f"original\n{original.size[0]} x {original.size[1]} px")
    ax.set_xlabel("column")
    ax.set_ylabel("row")

    ax = axes[1]
    ax.imshow(np.where(on, 0, 255), cmap="gray", vmin=0, vmax=255, aspect="auto")
    ax.set_title(
        f"transmitted grid\n{on.shape[1]} carriers x {on.shape[0]} symbols "
        f"({on.mean() * 100:.0f}% ON)"
    )
    ax.set_xlabel("subcarrier")
    ax.set_ylabel("symbol")

    ax = axes[2]
    im = ax.imshow(
        S, aspect="auto", origin="upper", cmap="viridis",
        vmin=vmax - dyn_range, vmax=vmax,
        extent=[-f_edge, f_edge, t_end, 0],
    )
    for sign in (-1, 1):  # edges of the occupied band
        ax.axvline(sign * plan.occupied_bw / 2 / fscale, color="w", lw=0.6, ls="--", alpha=0.5)
    ax.set_title(
        f"waterfall of the generated IQ\n"
        f"{plan.fs / 1e6:.3f} MS/s, {plan.occupied_bw / 1e6:.3f} MHz occupied, "
        f"PAPR {papr_db(iq):.1f} dB"
    )
    ax.set_xlabel(f"baseband frequency ({funit})")
    ax.set_ylabel("time (ms)")
    fig.colorbar(im, ax=ax, label="dB rel. peak", pad=0.02)

    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return True


# ==========================================================================


def run(cfg: dict) -> int:
    on, original = prepare_bitmap(cfg["image"], cfg["grid"]["carriers"])
    n_sym = on.shape[0]
    plan = plan_grid(cfg["grid"])

    if plan.repeat > 64:
        # The repeat multiplies the file. Spacing buys the same line rate for
        # free; it just costs occupied bandwidth, so the caller has to choose.
        want = plan.scs / plan.repeat
        print(f"warning: {plan.repeat} symbols per row multiplies the waveform "
              f"{plan.repeat}x. grid.subcarrier_spacing near {want:,.0f} Hz would hold "
              f"this line rate with a repeat of about 1, in a "
              f"{plan.occupied_bw / plan.repeat:,.0f} Hz band instead of "
              f"{plan.occupied_bw:,.0f} Hz.", file=sys.stderr)

    iq = modulate(on, plan)
    raw_papr = papr_db(iq)
    out_cfg = cfg["output"]
    if out_cfg["clip_papr_db"] is not None:
        iq = clip_papr(iq, plan, out_cfg["clip_papr_db"])

    out = out_cfg["path"]
    write_iq(out, iq, out_cfg, plan)

    print(f"{cfg['image']['path']}  ->  {out}")
    silence = cfg["image"]["silence"]
    print(f"  bitmap           : {plan.n_car} carriers x {n_sym} lines "
          f"({on.mean() * 100:.1f}% ON)"
          + (f", {silence} of them blank" if silence else ""))
    print(plan.describe())
    print(f"  samples          : {len(iq):,}  ({len(iq) / plan.fs * 1e3:,.1f} ms, "
          f"{out.stat().st_size / 1e6:.2f} MB {out_cfg['format']})")
    print(f"  PAPR             : {raw_papr:.2f} dB"
          + (f"  -> {papr_db(iq):.2f} dB after clipping"
             if out_cfg["clip_papr_db"] is not None else ""))

    if out_cfg["sigmf"]:
        if FORMATS[out_cfg["format"]][2] is None:
            print(f"  metadata         : skipped, {out_cfg['format']} is not a raw IQ stream")
        else:
            meta = out.with_suffix(out.suffix + ".sigmf-meta")
            write_sigmf(meta, plan, out_cfg["format"], n_sym, out_cfg["centre_frequency"])
            print(f"  metadata         : {meta}")

    d = cfg["diagnostics"]
    if d["bitmap"]:
        save_bitmap(d["bitmap"], on)
        print(f"  bitmap image     : {d['bitmap']}")
    if d["plot"]:
        if save_plot(d["plot"], original, on, iq.astype(np.complex128), plan,
                     d["plot_dpi"], d["plot_dynamic_range"]):
            print(f"  plot             : {d['plot']}")
    if d["verify"]:
        ref = np.repeat(on, plan.repeat, axis=0) if plan.repeat > 1 else on
        rec = demodulate(iq.astype(np.complex128), plan, ref.shape[0])
        print(f"  round-trip BER   : {np.mean(rec != ref) * 100:.4f}%")

    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Encode an image as a complex-baseband OFDM waveform. "
                    "Configuration is JSON.")
    p.add_argument("config", type=Path, nargs="?", help="JSON configuration file")
    p.add_argument("--write-template", type=Path, metavar="FILE",
                   help="write a starter config and exit")
    p.add_argument("--print-config", action="store_true",
                   help="print the fully resolved configuration and exit")
    a = p.parse_args(argv)

    if a.write_template:
        if a.write_template.exists():
            print(f"refusing to overwrite {a.write_template}", file=sys.stderr)
            return 1
        a.write_template.write_text(json.dumps(TEMPLATE, indent=2) + "\n")
        print(f"wrote {a.write_template}")
        return 0

    if not a.config:
        p.error("a config file is required (or use --write-template)")

    try:
        raw = load_raw(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    # "jobs": [...] is a batch; each entry is merged over the shared settings.
    base_dir = raw.pop("__dir__", ".")
    jobs = raw.pop("jobs", None)
    if jobs is not None:
        if not isinstance(jobs, list):
            print("config error: jobs must be an array of objects", file=sys.stderr)
            return 1
        entries = [deep_merge(raw, j) for j in jobs]
    else:
        entries = [raw]
    for e in entries:
        e["__dir__"] = base_dir

    rc = 0
    for i, entry in enumerate(entries):
        try:
            cfg = resolve(entry)
        except ConfigError as e:
            print(f"config error (job {i + 1}): {e}", file=sys.stderr)
            rc = 1
            continue
        if a.print_config:
            printable = {s: {k: (str(v) if isinstance(v, Path) else v) for k, v in kv.items()}
                         for s, kv in cfg.items()}
            print(json.dumps(printable, indent=2))
            continue
        if len(entries) > 1:
            print(f"--- job {i + 1}/{len(entries)} ---")
        rc |= run(cfg)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
