#!/usr/bin/env python3
r"""
stream_copy_cut.py

ffmpeg の stream copy (-c copy) で長尺動画を分割するツール。

必要:
    ffmpeg
    ffprobe
    Python 3.9+

例:
    python stream_copy_cut.py cut ^
      --source "The Mask.mp4" ^
      --cuts "00:01:41.768,00:06:57.250,00:22:53.873,00:46:08.099,01:10:23.386,01:24:48.417" ^
      --output-dir ".\cut_auto"
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], capture: bool = False) -> str:
    print("+", subprocess.list2cmdline(cmd))
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        if capture:
            if p.stdout:
                print(p.stdout)
            if p.stderr:
                print(p.stderr, file=sys.stderr)
        raise RuntimeError(f"Command failed ({p.returncode}): {cmd[0]}")
    return p.stdout if capture else ""


def require_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            raise RuntimeError(f"{name} が PATH に見つかりません。")


def parse_time(s: str) -> float:
    s = s.strip()
    if not s:
        raise ValueError("empty time")

    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
    except ValueError:
        pass
    raise ValueError(f"時刻形式が不正です: {s}")


def get_duration(source: Path) -> float:
    out = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ], capture=True).strip()
    return float(out)


def fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    seconds -= h * 3600
    m = int(seconds // 60)
    seconds -= m * 60
    return f"{h:02d}.{m:02d}.{seconds:06.3f}"


def do_cut(source: Path, cuts_text: str, output_dir: Path) -> None:
    require_tools()

    source = source.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = get_duration(source)
    cuts = sorted(parse_time(x) for x in cuts_text.split(",") if x.strip())

    if any(t <= 0 or t >= duration for t in cuts):
        raise RuntimeError(
            f"cut位置は 0秒より後、動画長({duration:.3f}秒)より前にしてください。"
        )

    boundaries = [0.0] + cuts + [duration]

    stem = source.stem
    ext = source.suffix

    print("=== ffmpeg stream-copy cut ===")
    print("注意: 非キーフレーム境界では余分なframeが付く可能性があります。\n")

    for i in range(len(boundaries) - 1):
        start = boundaries[i]
        end = boundaries[i + 1]
        length = end - start

        name = (
            f"{stem}-"
            f"{fmt_time(start)}-{fmt_time(end)}-"
            f"seg{i+1:02d}{ext}"
        )
        dst = output_dir / name

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-ss", f"{start:.6f}",
            "-i", str(source),
            "-t", f"{length:.6f}",
            "-map", "0",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-y", str(dst),
        ]
        run(cmd)

    print(f"\n分割完了: {output_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ffmpeg stream-copyで動画を分割"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cut = sub.add_parser("cut", help="ffmpeg stream-copyで自動分割")
    p_cut.add_argument("--source", required=True, type=Path)
    p_cut.add_argument("--cuts", required=True,
                       help='例: "00:01:41.768,00:06:57.250,00:22:53.873"')
    p_cut.add_argument("--output-dir", required=True, type=Path)

    args = parser.parse_args()

    try:
        do_cut(args.source, args.cuts, args.output_dir)
        return 0

    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
