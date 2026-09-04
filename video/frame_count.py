#!/usr/bin/env python3
r"""
frame_count.py

動画ファイルの映像フレーム数を数えて、テキストファイルに書き出す。

数え方:
  packets (既定) : 映像ストリームのパケット数を数える (-count_packets)。
                   デコードしないので高速。通常の動画では 1パケット = 1フレーム。
  decode         : 実際にデコードして数える (-count_frames)。低速だが確実。
  meta           : コンテナに書かれている nb_frames を読むだけ。一瞬だが、
                   値が無い/嘘のことがある (その場合は packets にフォールバック)。

出力はタブ区切りなので、そのまま表計算ソフトに貼り付けられる。

必要:
    ffprobe
    Python 3.9+

例:
    python frame_count.py "a.mp4" "b.mov"
    python frame_count.py --method decode --output frames.txt "a.mp4"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

METHOD_LABEL = {
    "packets": "パケット数 (-count_packets)",
    "decode": "デコード実測 (-count_frames)",
    "meta": "コンテナのメタデータ (nb_frames)",
}


def ffprobe_json(path: Path, method: str) -> dict:
    count = {"packets": ["-count_packets"], "decode": ["-count_frames"]}.get(method, [])
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        *count,
        "-print_format",
        "json",
        "-show_entries",
        "stream=nb_frames,nb_read_packets,nb_read_frames,r_frame_rate,"
        "avg_frame_rate,duration,width,height:format=duration",
        str(path),
    ]

    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        message = (p.stderr or "").strip().splitlines()
        raise RuntimeError(message[-1] if message else f"ffprobe が失敗しました (exit {p.returncode})")
    return json.loads(p.stdout or "{}")


def _int(value) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _fps(value) -> Fraction | None:
    if not isinstance(value, str):
        return None
    num, _, den = value.partition("/")
    try:
        fps = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        return None
    return fps if fps > 0 else None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(seconds)
    ms = round((seconds - total) * 1000)
    if ms == 1000:
        total, ms = total + 1, 0
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}.{ms:03d}"


class Result:
    def __init__(self, path: Path):
        self.path = path
        self.frames: int | None = None
        self.fps: Fraction | None = None
        self.size: str = "-"
        self.duration: float | None = None
        self.error: str | None = None

    @property
    def frames_text(self) -> str:
        return str(self.frames) if self.frames is not None else "-"

    @property
    def fps_text(self) -> str:
        if self.fps is None:
            return "-"
        return f"{float(self.fps):.3f}".rstrip("0").rstrip(".")

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)


def inspect(path: Path, method: str) -> Result:
    result = Result(path)
    try:
        info = ffprobe_json(path, method)
    except Exception as exc:  # ffprobe 失敗・JSON 破損はファイル単位で報告する
        result.error = str(exc)
        return result

    streams = info.get("streams") or []
    if not streams:
        result.error = "映像ストリームがありません"
        return result
    stream = streams[0]

    if method == "decode":
        result.frames = _int(stream.get("nb_read_frames"))
    elif method == "packets":
        result.frames = _int(stream.get("nb_read_packets"))
    else:
        result.frames = _int(stream.get("nb_frames"))
        if result.frames is None:
            # nb_frames を持たないコンテナ(mkv 等)向けのフォールバック
            try:
                fallback = ffprobe_json(path, "packets")
            except Exception as exc:
                result.error = str(exc)
                return result
            fallback_streams = fallback.get("streams") or []
            if fallback_streams:
                result.frames = _int(fallback_streams[0].get("nb_read_packets"))

    result.fps = _fps(stream.get("avg_frame_rate")) or _fps(stream.get("r_frame_rate"))
    width, height = stream.get("width"), stream.get("height")
    if width and height:
        result.size = f"{width}x{height}"

    result.duration = _float(stream.get("duration")) or _float(
        (info.get("format") or {}).get("duration")
    )
    if result.duration is None and result.frames and result.fps:
        result.duration = result.frames / float(result.fps)

    if result.frames is None:
        result.error = "フレーム数を取得できませんでした"
    return result


def build_lines(results: list[Result], method: str, full_path: bool) -> list[str]:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# frame_count.py  {stamp}",
        f"# 数え方: {METHOD_LABEL[method]}",
        "frames\tfps\tduration\tsize\tfile",
    ]
    for r in results:
        name = str(r.path) if full_path else r.path.name
        if r.error:
            lines.append(f"ERROR\t-\t-\t-\t{name}\t{r.error}")
        else:
            lines.append(
                f"{r.frames_text}\t{r.fps_text}\t{r.duration_text}\t{r.size}\t{name}"
            )

    total = sum(r.frames for r in results if r.frames is not None)
    if len(results) > 1:
        lines.append(f"{total}\t-\t-\t-\t合計 ({len(results)} ファイル)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="動画のフレーム数を数えてテキストファイルに出力する"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="動画ファイル")
    parser.add_argument(
        "--method",
        choices=("packets", "decode", "meta"),
        default="packets",
        help="フレーム数の数え方 (既定: packets)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="出力先テキストファイル (既定: 最初の動画と同じ場所の frame_count.txt)",
    )
    parser.add_argument("--append", action="store_true", help="出力先に追記する")
    parser.add_argument(
        "--full-path", action="store_true", help="ファイル名をフルパスで書く"
    )
    args = parser.parse_args(argv)

    files: list[Path] = []
    for path in args.inputs:
        if not path.is_file():
            print(f"[ERROR] ファイルが見つかりません: {path}", file=sys.stderr)
            return 1
        files.append(path.resolve())

    output = args.output or files[0].with_name("frame_count.txt")
    output = output.resolve()

    results = []
    for path in files:
        print(f"調査中: {path.name}")
        results.append(inspect(path, args.method))

    lines = build_lines(results, args.method, args.full_path)

    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    with output.open(mode, encoding="utf-8-sig", newline="\r\n") as f:
        if args.append and output.stat().st_size > 0:
            f.write("\n")
        f.write("\n".join(lines) + "\n")

    print()
    print("\n".join(lines))
    print()
    print(f"出力しました: {output}")

    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
