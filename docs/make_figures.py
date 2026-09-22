#!/usr/bin/env python3
"""Regenerate the figures used by README.md.

    python docs/make_figures.py

Everything is derived from bitfall.py itself, so the pictures cannot drift away
from what the tool actually produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import bitfall  # noqa: E402

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

IMAGE = ROOT / "umad.png"


def build(image=None, grid=None):
    """Resolve a config the way the CLI does, then modulate. No files written."""
    raw = {
        "image": dict({"path": str(IMAGE)}, **(image or {})),
        "grid": dict({"carriers": 256}, **(grid or {})),
        "output": {"path": "unused.cf32"},
        "__dir__": str(ROOT),
    }
    cfg = bitfall.resolve(raw)
    on, _ = bitfall.prepare_bitmap(cfg["image"], cfg["grid"]["carriers"])
    plan = bitfall.plan_grid(cfg["grid"])
    return on, plan, bitfall.modulate(on, plan)


def analyser(iq, fs, line_rate, n_fft=512, skip=0):
    """A spectrum analyser in FFT mode with a peak detector.

    One waterfall line per acquisition window of 1/line_rate. Every FFT that
    falls inside that window is folded into the line with a max, which is
    exactly why a window holding many OFDM symbols shows their union rather
    than any one of them.
    """
    line_len = int(round(fs / line_rate))
    n_lines = (len(iq) - skip) // line_len
    win = np.hanning(n_fft)
    out = np.empty((n_lines, n_fft), np.float32)
    for i in range(n_lines):
        seg = iq[skip + i * line_len: skip + (i + 1) * line_len]
        n_chunk = max(1, len(seg) // n_fft)
        chunks = seg[: n_chunk * n_fft].reshape(n_chunk, n_fft) * win
        spec = np.abs(np.fft.fftshift(np.fft.fft(chunks, axis=1), axes=1))
        out[i] = spec.max(axis=0)
    return 20 * np.log10(out + 1e-12)


def show(ax, S, plan, seconds, title, dyn=45):
    """Draw one simulated waterfall, cropped to the span you would dial in."""
    half = plan.n_car / plan.n_fft * 1.15  # occupied band plus a little margin
    lo = int(plan.n_fft * (0.5 - half / 2))
    hi = int(plan.n_fft * (0.5 + half / 2))
    edge = half * plan.fs / 2 / 1e6
    vmax = S.max()
    ax.imshow(S[:, lo:hi], aspect="auto", origin="upper", cmap="viridis",
              vmin=vmax - dyn, vmax=vmax, extent=[-edge, edge, seconds, 0])
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("frequency (MHz)")


def figure_line_time(path):
    """The one parameter that decides whether anything appears at all."""
    fps = 50.0  # a plausible analyser waterfall rate

    # Left: one symbol per image row, as the tool behaves with no line_time.
    # The waveform is only 19 ms long, so it is looped the way an ARB loops it.
    on_a, plan_a, iq_a = build(grid={"subcarrier_spacing": "10k"})
    reps = int(np.ceil(3.62 * plan_a.fs / len(iq_a)))
    iq_a = np.tile(iq_a, reps)
    rows_per_line = plan_a.fs / fps / plan_a.sym_len

    # Right: the same band, but each row held for 20 ms.
    on_b, plan_b, iq_b = build(grid={"subcarrier_spacing": "10k", "line_time": "20ms"})

    S_a = analyser(iq_a, plan_a.fs, fps)
    # Same waveform as the fixed case, but read by an analyser four times too
    # slow: the failure is gradual, not a cliff.
    S_c = analyser(iq_b, plan_b.fs, fps / 4)
    S_b = analyser(iq_b, plan_b.fs, fps)

    fig, axes = plt.subplots(1, 3, figsize=(15, 6.2), dpi=120)
    show(axes[0], S_a, plan_a, len(S_a) / fps,
         f"no line_time\n"
         f"{plan_a.scs / 1e3:.0f} kHz spacing, {rows_per_line:.0f} image rows per "
         f"analyser line\nevery line is the union of the whole picture")
    show(axes[2], S_c, plan_b, len(S_c) / (fps / 4),
         f'line_time "20ms", analyser at 12.5 lines/s\n'
         f"4 image rows per analyser line\n"
         f"detail merges away row by row")
    show(axes[1], S_b, plan_b, len(S_b) / fps,
         f'line_time "20ms", analyser at 50 lines/s\n'
         f"{plan_b.scs / 1e3:.2f} kHz spacing, {plan_b.repeat} symbols per row\n"
         f"one image row per analyser line")
    axes[0].set_ylabel("time (s)")
    fig.suptitle("What the analyser sees, simulated at 50 waterfall lines/s "
                 "(FFT mode, peak detector)", fontsize=12)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"wrote {path}")


def figure_carriers(path):
    """Carrier count sets detail in both axes, and the bandwidth you pay for it."""
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.8), dpi=120)
    for ax, n_car in zip(axes, (64, 256, 1024)):
        on, plan, _ = build(grid={"carriers": n_car, "subcarrier_spacing": "1k"})
        ax.imshow(np.where(on, 0, 255), cmap="gray", vmin=0, vmax=255)
        ax.set_anchor("N")
        ax.set_title(f"carriers {n_car}\n{on.shape[1]} x {on.shape[0]} pixels\n"
                     f"{plan.occupied_bw / 1e3:,.0f} kHz occupied, "
                     f"{plan.fs / 1e3:,.0f} kSa/s", fontsize=10)
        ax.set_xlabel("subcarrier")
    axes[0].set_ylabel("image row (symbol)")
    fig.suptitle("grid.carriers at a fixed 1 kHz spacing: detail in both axes, "
                 "paid for in bandwidth (each panel scaled to fit)", fontsize=12)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"wrote {path}")


def figure_stretch(path):
    """Row count is free to choose; column count is not."""
    fig, axes = plt.subplots(1, 3, figsize=(13, 7.9), dpi=120)
    for ax, s in zip(axes, (0.5, 1.0, 2.0)):
        on, plan, _ = build(image={"stretch": s},
                            grid={"subcarrier_spacing": "1k", "line_time": "20ms"})
        ax.imshow(np.where(on, 0, 255), cmap="gray", vmin=0, vmax=255)
        ax.set_anchor("N")
        ax.set_title(f"stretch {s}\n{on.shape[1]} carriers x {on.shape[0]} rows\n"
                     f"{on.shape[0] * 0.02:.2f} s per frame at 20 ms rows", fontsize=10)
        ax.set_xlabel("subcarrier")
    axes[0].set_ylabel("image row (symbol)")
    fig.suptitle("image.stretch: taller in time, never wider in frequency "
                 "(shown to scale)", fontsize=12)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"wrote {path}")


if __name__ == "__main__":
    figure_line_time(HERE / "line_time.png")
    figure_carriers(HERE / "carriers.png")
    figure_stretch(HERE / "stretch.png")
