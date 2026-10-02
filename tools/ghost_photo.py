"""Measure ghosting on photos of the e-ink panel (ImageMagick backend).

The tool crops the same panel area in every photo and reports simple
statistics of the gray levels; a uniform canvas with visible ghosting has a
higher standard deviation. See `docs/ghosting-experiments.md` for how the
photos are taken. This tool only reads image files.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """Accept both `WxH+X+Y` (ImageMagick) and `x,y,w,h`."""

    try:
        if "," in text:
            parts = [int(part) for part in text.split(",")]
            if len(parts) != 4:
                raise ValueError
            x, y, width, height = parts
        else:
            geometry, x_text, y_text = text.split("+", 2)
            width_text, height_text = geometry.split("x", 1)
            width, height = int(width_text), int(height_text)
            x, y = int(x_text), int(y_text)
    except ValueError as exc:
        raise ValueError(
            "crop must be WxH+X+Y or x,y,w,h with integers"
        ) from exc
    if width <= 0 or height <= 0 or x < 0 or y < 0:
        raise ValueError("crop must have positive width and height")
    return (x, y, width, height)


def parse_metrics(text: str) -> dict[str, float]:
    """Parse `mean standard_deviation minima maxima` values in 0..1."""

    parts = text.split()
    if len(parts) != 4:
        raise ValueError(f"unexpected metrics output: {text!r}")
    mean, deviation, minimum, maximum = (float(part) for part in parts)
    return {
        "mean": round(mean, 4),
        "std": round(deviation, 4),
        "min": round(minimum, 4),
        "max": round(maximum, 4),
    }


def ghost_index(deviation: float) -> float:
    """Scale the standard deviation to a convenient 0..1000 index."""

    return round(deviation * 1000, 2)


def _backend() -> str:
    """Name of the available ImageMagick executable (v7 `magick` or v6)."""

    for name in ("magick", "convert"):
        if shutil.which(name):
            return name
    raise SystemExit(
        "ghost_photo: ImageMagick (magick or convert) is required"
    )


def measure(
    path: str, crop: tuple[int, int, int, int], backend: str | None = None
) -> dict[str, float]:
    """Crop one photo, convert it to gray and return its level statistics."""

    x, y, width, height = crop
    command = [
        backend or _backend(),
        path,
        "-crop",
        f"{width}x{height}+{x}+{y}",
        "+repage",
        "-colorspace",
        "Gray",
        "-format",
        "%[fx:mean] %[fx:standard_deviation] %[fx:minima] %[fx:maxima]",
        "info:",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or f"cannot read {path} with ImageMagick"
        )
    metrics = parse_metrics(result.stdout.strip())
    metrics["ghost_index"] = ghost_index(metrics["std"])
    return metrics


def reference_error(path: str, reference: str, crop, backend=None) -> float:
    """RMSE against a clean photo with identical framing and fixed exposure.

    The same uniform canvas must be displayed. This does not align photos or
    compensate for lighting changes; use repeat clean photos to measure noise.
    """
    x, y, width, height = crop
    command = [backend or _backend()]
    for filename in (reference, path):
        command += ["(", filename, "-crop", f"{width}x{height}+{x}+{y}",
                    "+repage", "-colorspace", "Gray", ")"]
    command += ["-compose", "Difference", "-composite", "-evaluate", "Pow", "2",
                "-format", "%[fx:sqrt(mean)]", "info:"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "reference comparison failed")
    return float(result.stdout)


def build_parser() -> argparse.ArgumentParser:
    """Parser for the photo measurement tool."""

    parser = argparse.ArgumentParser(
        prog="ghost_photo",
        description="compare ghosting across photos of the e-ink panel",
    )
    parser.add_argument(
        "--crop",
        required=True,
        help="panel area in photo pixels: WxH+X+Y or x,y,w,h",
    )
    parser.add_argument(
        "--csv", action="store_true", help="print a CSV table"
    )
    parser.add_argument(
        "--json", action="store_true", help="print one JSON object per line"
    )
    parser.add_argument("photos", nargs="+", help="photo files to analyze")
    parser.add_argument("--reference", help="clean reference photo: also report normalized RMSE")
    parser.add_argument("--noise-reference", help="second clean photo for noise-corrected RMSE")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Measure every photo and print the table in the selected format."""

    args = build_parser().parse_args(argv)
    if args.noise_reference and not args.reference:
        print("ghost_photo: --noise-reference requires --reference", file=sys.stderr)
        return 1
    try:
        crop = parse_crop(args.crop)
    except ValueError as exc:
        print(f"ghost_photo: error: {exc}", file=sys.stderr)
        return 1
    rows = []
    noise = 0.0
    if args.noise_reference:
        try:
            noise = reference_error(args.noise_reference, args.reference, crop)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ghost_photo: error: {exc}", file=sys.stderr)
            return 1
    for path in args.photos:
        try:
            metrics = measure(path, crop)
            if args.reference:
                metrics["reference_rmse"] = reference_error(path, args.reference, crop)
                # Subtract the noise floor measured on two clean photos, so
                # the corrected RMSE only reflects real ghosting.
                metrics["corrected_rmse"] = math.sqrt(max(
                    0, metrics["reference_rmse"]**2 - noise**2))
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ghost_photo: error: {exc}", file=sys.stderr)
            return 1
        rows.append({"photo": path, **metrics})
    if args.csv:
        suffix = ",reference_rmse,corrected_rmse" if args.reference else ""
        print("photo,mean,std,min,max,ghost_index" + suffix)
        for row in rows:
            print(
                f"{row['photo']},{row['mean']},{row['std']},"
                f"{row['min']},{row['max']},{row['ghost_index']}"
                + (f",{row['reference_rmse']},{row['corrected_rmse']}" if args.reference else "")
            )
    elif args.json:
        for row in rows:
            print(json.dumps(row))
    else:
        for row in rows:
            print(
                f"{row['photo']}: mean={row['mean']} std={row['std']} "
                f"min={row['min']} max={row['max']} "
                f"ghost_index={row['ghost_index']}"
                + (f" reference_rmse={row['reference_rmse']} corrected_rmse={row['corrected_rmse']}"
                   if args.reference else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
