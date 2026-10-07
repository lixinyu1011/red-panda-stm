"""把扫描保存的 txt 画成图。改下面文件名后直接运行即可。"""

from __future__ import annotations

from pathlib import Path

# 改这里：ADC 的 txt 文件名。同时间戳的 scan_dacz_*.txt 会自动一起画。
ADC_FILE = "scan_adc_20260930_224948.txt"
# 扫描 X（图上竖轴）插值倍数，2 或 4 会更密。Y 一般不用动。
X_ZOOM = 2
Y_ZOOM = 2

import numpy as np
import matplotlib.pyplot as plt

ADC_MIN, ADC_MAX = -32768, 32767
Z_MIN, Z_MAX = 0, 65535


def default_data_dir() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "data",
        here.parents[2] / "data",
        here.parent / "data",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def load_matrix(path: Path) -> np.ndarray:
    rows = []
    width = 0
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        row = [int(x) for x in parts if x.lstrip("-").isdigit()]
        if not row:
            continue
        rows.append(row)
        width = max(width, len(row))
    image = np.full((len(rows), width), np.nan, dtype=np.float64)
    for i, row in enumerate(rows):
        image[i, :len(row)] = row
    return image


def mask_range(image: np.ndarray, lo: float, hi: float) -> np.ndarray:
    out = image.astype(np.float64, copy=True)
    out[(out < lo) | (out > hi)] = np.nan
    return out


def upsample(image: np.ndarray, zx: int, zy: int) -> np.ndarray:
    out = np.repeat(image, max(int(zx), 1), axis=0)
    return np.repeat(out, max(int(zy), 1), axis=1)


def flatten_plane(image: np.ndarray) -> np.ndarray:
    valid = np.isfinite(image)
    if valid.sum() < 3 or np.nanstd(image) < 1.0:
        return image
    yy, xx = np.indices(image.shape)
    A = np.column_stack((np.ones(valid.sum()), xx[valid], yy[valid]))
    coef = np.linalg.lstsq(A, image[valid], rcond=None)[0]
    out = image.copy()
    out[valid] = image[valid] - (coef[0] + coef[1] * xx[valid] + coef[2] * yy[valid])
    return out


def adc_to_nA(adc: np.ndarray) -> np.ndarray:
    return adc / 32768.0 * 10.24 / 100e6 * 1e9


def color_limits(image: np.ndarray):
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return -1.0, 1.0
    lo, hi = np.percentile(finite, (2.0, 98.0))
    if lo == hi:
        lo, hi = float(finite.min()), float(finite.max())
    if lo == hi:
        lo, hi = lo - 1.0, hi + 1.0
    return lo, hi


def show_panel(ax, image, title, unit):
    vmin, vmax = color_limits(image)
    im = ax.imshow(image, origin="lower", cmap="afmhot", aspect="equal",
                   interpolation="bilinear", vmin=vmin, vmax=vmax)
    ax.set_title(title)
    ax.set_xlabel("Y")
    ax.set_ylabel("X")
    plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label=unit)


def plot_pair(adc_path: Path, z_path: Path | None, out_path: Path):
    adc = upsample(mask_range(load_matrix(adc_path), ADC_MIN, ADC_MAX),
                   X_ZOOM, Y_ZOOM)
    adc_flat = flatten_plane(adc)
    panels = [
        (adc, f"ADC raw  {adc_path.name}", "ADC"),
        (adc_to_nA(adc_flat), "ADC flattened (nA)", "nA"),
    ]
    if z_path is not None and z_path.is_file():
        z = upsample(mask_range(load_matrix(z_path), Z_MIN, Z_MAX),
                     X_ZOOM, Y_ZOOM)
        panels.append((flatten_plane(z), f"DAC Z  {z_path.name}", "DAC Z"))

    fig, axes = plt.subplots(1, len(panels), figsize=(5.2 * len(panels), 4.8))
    if len(panels) == 1:
        axes = [axes]
    for ax, (image, title, unit) in zip(axes, panels):
        show_panel(ax, image, title, unit)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    print(f"saved {out_path}  shape={adc.shape}  "
          f"adc {np.nanmin(adc):.0f}..{np.nanmax(adc):.0f}")


def plot_current_time(adc_path: Path, out_path: Path):
    adc = mask_range(load_matrix(adc_path), ADC_MIN, ADC_MAX)
    current = adc_to_nA(adc.ravel())
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(current, lw=0.5, color="C0")
    ax.set_xlabel("pixel (scan order)")
    ax.set_ylabel("current (nA)")
    ax.set_title(adc_path.name)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)
    finite = current[np.isfinite(current)]
    print(f"saved {out_path}  n={finite.size}  "
          f"I {finite.min():.3f}..{finite.max():.3f} nA")


if __name__ == "__main__":
    adc_path = Path(ADC_FILE)
    if not adc_path.is_file():
        adc_path = default_data_dir() / ADC_FILE
    stamp = adc_path.name.replace("scan_adc_", "", 1)
    z_path = adc_path.with_name(f"scan_dacz_{stamp}")
    plot_pair(adc_path, z_path, adc_path.with_suffix(".png"))
    plot_current_time(adc_path, adc_path.with_name(
        adc_path.stem + "_current.png"))
