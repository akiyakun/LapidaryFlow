#!/usr/bin/env python3
r"""
convert_view_mp4.py

結合済みの ProRes / FFV1 などのマスターを、視聴用の H.265 (HEVC / Main 10) mp4
に変換する。エンコードは NVIDIA NVENC (hevc_nvenc) を使う。

入力の fps を判定して、ビットレートとキーフレーム間隔を自動で調整する。
60fps は 30fps と同じ画質にするのに約1.4倍のビットレートが要る
(フレーム間が似ているので2倍は不要)。

モード:
  vbr (既定) : 30fps 換算のビットレートを指定する。狙ったファイルサイズにほぼ合う。
  cq         : 画質基準。fps が上がると画質は保たれる代わりにサイズが増える。

必要:
    ffmpeg / ffprobe (NVENC 対応ビルド)
    Python 3.9+

例:
    python convert_view_mp4.py "master.mov"
    python convert_view_mp4.py --mode cq --cq 22 "a.mov" "b.mov"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

# (fps の下限, ビットレート倍率)。上から順に最初に当てはまったものを使う。
FPS_SCALE = (
    (100, 1.80),
    (55, 1.40),
    (45, 1.25),
    (0, 1.00),
)

# 画質補助オプション。b_ref_mode / temporal-aq は Turing 世代以降が必要。
QUALITY_OPTS = [
    "-tune", "hq",
    "-rc-lookahead", "32",
    "-bf", "3",
    "-b_ref_mode", "middle",
    "-spatial-aq", "1",
    "-aq-strength", "8",
    "-temporal-aq", "1",
]

DEFAULT_FPS = 30


def check_tools() -> str | None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            return f"{tool} が見つかりません。PATH を確認してください。"

    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if "hevc_nvenc" not in (p.stdout or ""):
        return (
            "この ffmpeg は hevc_nvenc に対応していません。\n"
            "NVENC 対応ビルドの ffmpeg を用意してください。"
        )
    return None


def ffprobe_streams(path: Path) -> list[dict]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "stream=index,codec_type,r_frame_rate,avg_frame_rate",
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
        raise RuntimeError(
            message[-1] if message else f"ffprobe が失敗しました (exit {p.returncode})"
        )
    return (json.loads(p.stdout or "{}").get("streams") or [])


def _fps(value) -> Fraction | None:
    if not isinstance(value, str):
        return None
    num, _, den = value.partition("/")
    try:
        fps = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        return None
    return fps if fps > 0 else None


def detect_fps(streams: list[dict]) -> int:
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        fps = _fps(stream.get("r_frame_rate")) or _fps(stream.get("avg_frame_rate"))
        if fps is None:
            break
        rounded = round(float(fps))
        # 480fps 超は r_frame_rate が壊れている可能性が高いので既定値に倒す
        return rounded if 1 <= rounded <= 480 else DEFAULT_FPS
    return DEFAULT_FPS


def fps_scale(fps: int) -> float:
    for threshold, scale in FPS_SCALE:
        if fps >= threshold:
            return scale
    return 1.0


def rate_options(args, fps: int) -> tuple[list[str], str]:
    if args.mode == "cq":
        return ["-rc", "vbr", "-cq", str(args.cq), "-b:v", "0"], f"cq {args.cq}"

    bitrate = round(args.bitrate * fps_scale(fps))
    maxrate = bitrate * args.maxrate_pct // 100
    bufsize = bitrate * args.bufsize_pct // 100
    opts = [
        # multipass はエンコード自体は1回で、GPU 内部で2パス相当の解析を行う
        "-rc", "vbr",
        "-multipass", "fullres",
        "-cq", "0",
        "-b:v", f"{bitrate}k",
        "-maxrate", f"{maxrate}k",
        "-bufsize", f"{bufsize}k",
    ]
    return opts, f"vbr {bitrate}k"


def build_command(args, path: Path, out: Path, streams: list[dict], fps: int) -> list[str]:
    rate, _ = rate_options(args, fps)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-stats",
        "-y",
        "-i", str(path),
        "-map", "0:v:0",
    ]
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += [
        "-c:v", "hevc_nvenc",
        "-preset", args.preset,
        "-profile:v", args.profile,
        "-pix_fmt", args.pix_fmt,
        "-tag:v", "hvc1",
        *QUALITY_OPTS,
        "-g", str(fps),  # 1秒ごとのキーフレーム
        *rate,
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", args.audio_bitrate, "-ac", "2"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def convert(args, path: Path) -> bool:
    out = path.with_name(f"{path.stem}{args.suffix}.mp4")

    try:
        streams = ffprobe_streams(path)
    except Exception as exc:  # ffprobe 失敗・JSON 破損はファイル単位で報告する
        print(f"[ERROR] {path.name}: {exc}", file=sys.stderr)
        return False

    fps = detect_fps(streams)
    _, label = rate_options(args, fps)

    print("=" * 58)
    print(f" {path.name}  ->  {out.name}   (NVENC / {label} / {fps}fps)")
    print("=" * 58)

    if out.exists() and not args.overwrite:
        print(f"[SKIP] 出力先が既に存在します: {out}")
        return True

    cmd = build_command(args, path, out, streams, fps)
    if args.dry_run:
        print(subprocess.list2cmdline(cmd))
        return True

    if subprocess.run(cmd).returncode != 0:
        print(f"\n[ERROR] {path.name} の変換に失敗しました。", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="動画を視聴用の H.265 (NVENC) mp4 に変換する"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="動画ファイル")
    parser.add_argument(
        "--mode",
        choices=("vbr", "cq"),
        default="vbr",
        help="レート制御 (既定: vbr)",
    )
    parser.add_argument(
        "--cq", type=int, default=24, help="cq モードの品質。小さいほど高画質 (既定: 24)"
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=9000,
        help="vbr モードの 30fps 換算ビットレート kbps (既定: 9000)",
    )
    parser.add_argument(
        "--maxrate-pct", type=int, default=175, help="maxrate のビットレート比%% (既定: 175)"
    )
    parser.add_argument(
        "--bufsize-pct", type=int, default=350, help="bufsize のビットレート比%% (既定: 350)"
    )
    parser.add_argument(
        "--preset", default="p7", help="NVENC プリセット p1(最速)-p7(最高画質) (既定: p7)"
    )
    parser.add_argument("--profile", default="main10", help="H.265 プロファイル (既定: main10)")
    parser.add_argument("--pix-fmt", default="p010le", help="ピクセルフォーマット (既定: p010le)")
    parser.add_argument("--audio-bitrate", default="192k", help="AAC のビットレート (既定: 192k)")
    parser.add_argument("--suffix", default="_h265", help="出力ファイル名の接尾辞 (既定: _h265)")
    parser.add_argument("--overwrite", action="store_true", help="出力先が既にあっても上書きする")
    parser.add_argument("--dry-run", action="store_true", help="ffmpeg のコマンドを表示するだけ")
    args = parser.parse_args(argv)

    error = check_tools()
    if error:
        print(error, file=sys.stderr)
        return 1

    files: list[Path] = []
    for path in args.inputs:
        if not path.is_file():
            print(f"[ERROR] ファイルが見つかりません: {path}", file=sys.stderr)
            return 1
        files.append(path.resolve())

    failed = False
    for path in files:
        if not convert(args, path):
            failed = True
        print()

    if failed:
        print("一部のファイルでエラーが発生しました。上のログを確認してください。")
        return 1
    print("すべて完了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
