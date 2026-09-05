#!/usr/bin/env python3
r"""
convert_preview_fast_preserve.py

編集途中の一時確認用動画を、速度優先で MP4 (HEVC / 8-bit) に変換する。

方針:
  - NVIDIA: hevc_nvenc / preset p1 / 画質補助を極力OFF
  - 対応FFmpeg + 対応GPUでは Multi-NVENC Split Frame Encoding を2分割で使用
  - 複数ファイルを渡した場合、Intel QSV が実際に使える環境なら
    NVIDIA と Intel iGPU で別ファイルを並列変換
  - 元動画の解像度・フレームレート・時間長を維持
  - 音声は MP4 互換性優先で AAC 128kbps

単一ファイルについて:
  NVIDIA NVENC と Intel QSV を1本の同じ動画に混ぜてエンコードはしない。
  NVIDIA側で Split Frame Encoding が使える場合は RTX 5080 の複数NVENCを利用する。

必要:
  ffmpeg / ffprobe
  Python 3.9+

例:
  python convert_preview_fast_preserve.py "master.mov"
  python convert_preview_fast_preserve.py "a.mov" "b.mov" "c.mov"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


PRINT_LOCK = threading.Lock()
DEFAULT_FPS = Fraction(30, 1)


@dataclass
class VideoInfo:
    fps: Fraction
    width: int
    height: int
    has_audio: bool


def log(text: str = "") -> None:
    with PRINT_LOCK:
        print(text, flush=True)


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def check_basic_tools() -> str | None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            return f"{tool} が見つかりません。PATH を確認してください。"

    p = run_capture(["ffmpeg", "-hide_banner", "-encoders"])
    if "hevc_nvenc" not in ((p.stdout or "") + (p.stderr or "")):
        return (
            "この ffmpeg は hevc_nvenc に対応していません。\n"
            "NVENC 対応ビルドの ffmpeg を用意してください。"
        )
    return None


def nvenc_split_supported() -> bool:
    p = run_capture(["ffmpeg", "-hide_banner", "-h", "encoder=hevc_nvenc"])
    text = (p.stdout or "") + (p.stderr or "")
    return "split_encode_mode" in text


def qsv_encoder_present() -> bool:
    p = run_capture(["ffmpeg", "-hide_banner", "-encoders"])
    text = (p.stdout or "") + (p.stderr or "")
    return "hevc_qsv" in text


def qsv_usable() -> bool:
    """FFmpegにQSVがあるだけでなく、実際にiGPUで初期化できるか1フレーム試す。"""
    if not qsv_encoder_present():
        return False

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", "color=size=64x64:rate=30:duration=0.1",
        "-frames:v", "1",
        "-c:v", "hevc_qsv",
        "-preset", "veryfast",
        "-global_quality", "30",
        "-f", "null",
        "-",
    ]
    return subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _fps(value: object) -> Fraction | None:
    if not isinstance(value, str):
        return None
    num, sep, den = value.partition("/")
    try:
        f = Fraction(int(num), int(den if sep else 1))
    except (ValueError, ZeroDivisionError):
        return None
    return f if 0 < f <= 480 else None


def probe(path: Path) -> VideoInfo:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_entries",
        "stream=codec_type,width,height,r_frame_rate,avg_frame_rate",
        str(path),
    ]
    p = run_capture(cmd)
    if p.returncode != 0:
        lines = (p.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "ffprobe が失敗しました")

    streams = json.loads(p.stdout or "{}").get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("映像ストリームがありません")

    fps = (
        _fps(video.get("avg_frame_rate"))
        or _fps(video.get("r_frame_rate"))
        or DEFAULT_FPS
    )
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("映像サイズを取得できません")

    return VideoInfo(
        fps=fps,
        width=width,
        height=height,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def common_input_output(
    path: Path,
    out: Path,
    info: VideoInfo,
) -> list[str]:
    # 解像度・フレームレート・時間長を維持するため、
    # scale / fps / trim 系フィルタや -r は指定しない。
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-stats",
        "-y",
        "-i", str(path),
        "-map", "0:v:0",
    ]
    if info.has_audio:
        cmd += ["-map", "0:a:0"]

    # 元ファイルのメタデータも可能な範囲で引き継ぐ。
    cmd += ["-map_metadata", "0"]
    return cmd

def gop_for(info: VideoInfo) -> int:
    # 一時確認用なので2秒ごと。シークもしやすく、負荷も十分軽い。
    return max(1, round(float(info.fps) * 2))


def build_nvenc_command(
    args: argparse.Namespace,
    path: Path,
    out: Path,
    info: VideoInfo,
    use_split: bool,
) -> list[str]:
    cmd = common_input_output(path, out, info)
    cmd += [
        "-c:v", "hevc_nvenc",
        "-preset", "p1",
        "-tune", "ll",
        "-profile:v", "main",
        "-pix_fmt", "nv12",

        # 速度優先。lookahead/AQ/B-frame/multipass は使わない。
        "-rc", "constqp",
        "-qp", str(args.qp),
        "-bf", "0",
        "-rc-lookahead", "0",
        "-spatial-aq", "0",
        "-temporal-aq", "0",
        "-g", str(gop_for(info)),
        "-tag:v", "hvc1",
    ]
    if use_split:
        # 対応GPUでは1フレームを2分割し、複数NVENCで処理する。
        cmd += ["-split_encode_mode", "2"]

    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def build_qsv_command(
    args: argparse.Namespace,
    path: Path,
    out: Path,
    info: VideoInfo,
) -> list[str]:
    cmd = common_input_output(path, out, info)
    cmd += [
        "-c:v", "hevc_qsv",
        "-preset", "veryfast",
        "-profile:v", "main",
        "-pix_fmt", "nv12",
        "-global_quality", str(args.qp),
        "-bf", "0",
        "-g", str(gop_for(info)),
        "-tag:v", "hvc1",
    ]
    if info.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]

    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def execute(cmd: list[str], dry_run: bool) -> int:
    if dry_run:
        log(subprocess.list2cmdline(cmd))
        return 0
    return subprocess.run(cmd).returncode


def convert_nvenc(
    args: argparse.Namespace,
    path: Path,
    split_available: bool,
) -> bool:
    out = path.with_name(f"{path.stem}{args.suffix}.mp4")
    if out.exists() and not args.overwrite:
        log(f"[SKIP] {out.name} は既に存在します")
        return True

    try:
        info = probe(path)
    except Exception as exc:
        log(f"[ERROR] {path.name}: {exc}")
        return False

    split = split_available and not args.no_split_nvenc

    log("=" * 68)
    log(
        f"{path.name} -> {out.name}\n"
        f"  NVIDIA NVENC / p1 / QP {args.qp}"
        f"{' / Split NVENC x2' if split else ''}\n"
        f"  preserve: {info.width}x{info.height} / {float(info.fps):.3f} fps"
    )
    log("=" * 68)

    cmd = build_nvenc_command(args, path, out, info, split)
    rc = execute(cmd, args.dry_run)

    # FFmpeg側にはoptionがあっても、GPU/driver/条件によってSFEが拒否される場合にフォールバック。
    if rc != 0 and split and not args.dry_run:
        log("[WARN] Split Frame Encoding が使えなかったため、通常NVENCで再試行します。")
        cmd = build_nvenc_command(args, path, out, info, False)
        rc = execute(cmd, False)

    if rc != 0:
        log(f"[ERROR] {path.name} のNVENC変換に失敗しました。")
        return False
    return True


def convert_qsv(
    args: argparse.Namespace,
    path: Path,
    split_available: bool,
) -> bool:
    out = path.with_name(f"{path.stem}{args.suffix}.mp4")
    if out.exists() and not args.overwrite:
        log(f"[SKIP] {out.name} は既に存在します")
        return True

    try:
        info = probe(path)
    except Exception as exc:
        log(f"[ERROR] {path.name}: {exc}")
        return False

    log("=" * 68)
    log(
        f"{path.name} -> {out.name}\n"
        f"  Intel QSV / veryfast / quality {args.qp}\n"
        f"  preserve: {info.width}x{info.height} / {float(info.fps):.3f} fps"
    )
    log("=" * 68)

    cmd = build_qsv_command(args, path, out, info)
    rc = execute(cmd, args.dry_run)
    if rc == 0:
        return True

    if args.dry_run:
        return False

    # QSVが途中で使えなかった場合もファイルを失敗扱いにせずNVENCへ回す。
    log("[WARN] QSV変換に失敗したため、このファイルをNVENCで再試行します。")
    return convert_nvenc(args, path, split_available)


def worker(
    engine: str,
    files: list[Path],
    args: argparse.Namespace,
    split_available: bool,
    results: dict[Path, bool],
) -> None:
    for path in files:
        if engine == "qsv":
            ok = convert_qsv(args, path, split_available)
        else:
            ok = convert_nvenc(args, path, split_available)
        results[path] = ok
        log()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="編集途中の一時確認用MP4を速度優先で作成する"
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="動画ファイル")
    parser.add_argument(
        "--qp",
        type=int,
        default=30,
        help="一時確認用の量子化値。小さいほど高画質・大容量 (既定: 30)",
    )
    parser.add_argument(
        "--suffix",
        default="_preview",
        help="出力ファイル名の接尾辞 (既定: _preview)",
    )
    parser.add_argument(
        "--no-split-nvenc",
        action="store_true",
        help="Multi-NVENC Split Frame Encodingを使わない",
    )
    parser.add_argument(
        "--no-qsv",
        action="store_true",
        help="複数入力時のIntel QSV並列処理を使わない",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="出力先が既にあっても上書きする",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="FFmpegコマンドを表示するだけ",
    )
    args = parser.parse_args(argv)

    if not (0 <= args.qp <= 51):
        parser.error("--qp は 0～51 で指定してください")

    error = check_basic_tools()
    if error:
        print(error, file=sys.stderr)
        return 1

    files: list[Path] = []
    for path in args.inputs:
        if not path.is_file():
            print(f"[ERROR] ファイルが見つかりません: {path}", file=sys.stderr)
            return 1
        files.append(path.resolve())

    split_available = nvenc_split_supported()
    qsv_available = False if args.no_qsv else qsv_usable()

    log("=== Fast Preview configuration ===")
    log(f"NVENC Split Frame option : {'available' if split_available else 'not available'}")
    log(f"Intel QSV                : {'available' if qsv_available else 'not available'}")
    log("Resolution / FPS / time  : preserve source")
    log(f"QP                       : {args.qp}")
    log()

    results: dict[Path, bool] = {}

    # 1ファイルではNVENCを使用。
    # 2ファイル以上 + QSV可なら、ファイル単位でNVENC/QSVに分けて同時実行する。
    if len(files) >= 2 and qsv_available and not args.dry_run:
        nv_files = files[0::2]
        qsv_files = files[1::2]

        log(
            f"[PARALLEL] NVIDIA: {len(nv_files)} file(s) / "
            f"Intel QSV: {len(qsv_files)} file(s)"
        )
        log()

        t_nv = threading.Thread(
            target=worker,
            args=("nvenc", nv_files, args, split_available, results),
            daemon=False,
        )
        t_qsv = threading.Thread(
            target=worker,
            args=("qsv", qsv_files, args, split_available, results),
            daemon=False,
        )
        t_nv.start()
        t_qsv.start()
        t_nv.join()
        t_qsv.join()
    else:
        # dry-runはコマンドが順番に見やすいよう並列化しない。
        for path in files:
            results[path] = convert_nvenc(args, path, split_available)
            log()

    failed = [p for p in files if not results.get(p, False)]
    if failed:
        log("一部のファイルでエラーが発生しました:")
        for p in failed:
            log(f"  {p.name}")
        return 1

    log("すべて完了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
