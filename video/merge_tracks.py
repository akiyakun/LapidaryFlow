#!/usr/bin/env python3
r"""
merge_tracks.py

動画B の映像ストリームと、動画A の映像以外のストリーム(音声・字幕・添付フォント・
チャプター・メタデータ)を結合して動画C を作る。すべて stream copy なので無劣化。

segment_cut / segment_join で映像だけを取り出して加工した後、分割前の元動画が持って
いた音声や字幕を加工後の映像に戻すためのツール。

  A = 分割前の元動画   (音声・字幕などの供給元。映像は捨てる)
  B = 加工後の結合動画 (映像の供給元。音声などは持っていないことが多い)
  C = 出力

必要:
    ffmpeg
    ffprobe
    Python 3.9+

例:
    # 役割を明示して結合
    python merge_tracks.py --video "joined.mov" --tracks "original.mkv"

    # 2ファイルを渡して自動判定(映像しか持たない方を映像側とみなす)
    python merge_tracks.py "original.mkv" "joined.mov" -o "final.mkv"
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# mov/mp4 系コンテナに stream copy で入れられる字幕コーデック
MOV_TEXT_CODECS = {"mov_text", "ttxt"}
MOV_LIKE_EXTS = {".mov", ".mp4", ".m4v"}


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
        if capture and p.stderr:
            sys.stderr.write(p.stderr)
        raise SystemExit(f"コマンドが失敗しました (exit {p.returncode}): {cmd[0]}")
    return p.stdout or ""


def require_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if shutil.which(name) is None:
            raise SystemExit(f"{name} が PATH に見つかりません。")


def probe(path: Path) -> dict:
    out = run([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        str(path),
    ], capture=True)
    return json.loads(out)


def streams(info: dict, kind: str) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def non_video_streams(info: dict) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") != "video"]


def duration(info: dict) -> float | None:
    try:
        value = float(info.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def describe(path: Path, info: dict) -> str:
    lines = [f"  {path.name}"]
    for s in info.get("streams", []):
        tags = s.get("tags") or {}
        label = tags.get("title") or tags.get("language") or ""
        lines.append(
            f"    #{s.get('index')} {s.get('codec_type')}: "
            f"{s.get('codec_name')}{' [' + label + ']' if label else ''}"
        )
    chapters = len(info.get("chapters") or [])
    if chapters:
        lines.append(f"    chapters: {chapters}")
    secs = duration(info)
    if secs:
        lines.append(f"    duration: {secs:.3f}s")
    return "\n".join(lines)


def auto_assign(first: Path, second: Path, infos: dict[Path, dict]) -> tuple[Path, Path]:
    """映像しか持たない方を映像側(B)、それ以外を素材側(A)とみなす。"""
    bare = [p for p in (first, second) if not non_video_streams(infos[p])]
    if len(bare) != 1:
        raise SystemExit(
            "どちらが映像側か自動判定できませんでした。\n"
            "--video (映像を使う動画B) と --tracks (音声・字幕を使う動画A) を指定してください。"
        )
    video_src = bare[0]
    track_src = second if video_src is first else first
    return video_src, track_src


def needs_matroska(track_info: dict) -> bool:
    """mov/mp4 に stream copy できないストリームが素材側にあるか。"""
    for s in streams(track_info, "subtitle"):
        if s.get("codec_name") not in MOV_TEXT_CODECS:
            return True
    return bool(streams(track_info, "attachment"))


def default_output(video_src: Path, track_info: dict) -> Path:
    ext = video_src.suffix.lower()
    if ext not in MOV_LIKE_EXTS and ext != ".mkv":
        ext = ".mkv"
    if ext in MOV_LIKE_EXTS and needs_matroska(track_info):
        print("[NOTE] mov/mp4 に入らない字幕・添付があるため出力を .mkv にします。")
        ext = ".mkv"
    return video_src.with_name(video_src.stem + "_merged" + ext)


def build_cmd(args, video_src: Path, track_src: Path, output: Path) -> list[str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-y" if args.overwrite else "-n",
        "-i", str(video_src),
        "-i", str(track_src),
        "-map", "0:v",
        "-map", "1:a?",
        "-map", "1:s?",
        "-map", "1:t?",
    ]
    if args.include_data:
        cmd += ["-map", "1:d?"]
    cmd += [
        "-c", "copy",
        "-map_metadata", "1",
        "-map_chapters", "-1" if args.no_chapters else "1",
    ]
    if output.suffix.lower() in MOV_LIKE_EXTS:
        # タイムコードから tmcd トラックが生えて尺が狂うのを防ぐ
        cmd += ["-write_tmcd", "0"]
    cmd.append(str(output))
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="動画Bの映像 + 動画Aの映像以外(音声・字幕など)を無劣化で結合する。",
    )
    parser.add_argument("files", nargs="*", type=Path,
                        help="2ファイル渡すと役割を自動判定する")
    parser.add_argument("--video", type=Path, help="映像を使う動画B")
    parser.add_argument("--tracks", type=Path, help="音声・字幕などを使う動画A")
    parser.add_argument("-o", "--output", type=Path, help="出力先(省略時は <B>_merged.<ext>)")
    parser.add_argument("--include-data", action="store_true",
                        help="データストリーム(タイムコード等)も引き継ぐ")
    parser.add_argument("--no-chapters", action="store_true",
                        help="チャプターを引き継がない")
    parser.add_argument("--tolerance", type=float, default=1.0,
                        help="尺のズレを警告する閾値(秒)。既定 1.0")
    parser.add_argument("--overwrite", action="store_true", help="出力先を上書きする")
    args = parser.parse_args()

    require_tools()

    if args.video and args.tracks:
        if args.files:
            raise SystemExit("--video/--tracks と位置引数は同時に指定できません。")
        video_src, track_src = args.video, args.tracks
    elif args.video or args.tracks:
        raise SystemExit("--video と --tracks は両方指定してください。")
    elif len(args.files) == 2:
        video_src = track_src = None
    else:
        raise SystemExit("動画を2つ指定するか、--video と --tracks を指定してください。")

    candidates = [args.files[0], args.files[1]] if video_src is None else [video_src, track_src]
    for path in candidates:
        if not path.is_file():
            raise SystemExit(f"ファイルが見つかりません: {path}")

    infos = {path: probe(path) for path in candidates}
    if video_src is None:
        video_src, track_src = auto_assign(candidates[0], candidates[1], infos)

    video_info, track_info = infos[video_src], infos[track_src]
    if not streams(video_info, "video"):
        raise SystemExit(f"映像ストリームがありません: {video_src}")
    if not non_video_streams(track_info):
        raise SystemExit(f"引き継ぐ音声・字幕などがありません: {track_src}")

    output = args.output or default_output(video_src, track_info)
    if output.resolve() in (video_src.resolve(), track_src.resolve()):
        raise SystemExit("出力先が入力と同じです。")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"出力先が既にあります(--overwrite で上書き): {output}")

    print("=" * 58)
    print("映像 (B):")
    print(describe(video_src, video_info))
    print("音声・字幕など (A):")
    print(describe(track_src, track_info))
    print(f"出力 (C): {output}")
    print("=" * 58)

    v_dur, t_dur = duration(video_info), duration(track_info)
    if v_dur and t_dur and abs(v_dur - t_dur) > args.tolerance:
        print(f"[WARN] 尺が {abs(v_dur - t_dur):.3f}s ずれています "
              f"(B={v_dur:.3f}s / A={t_dur:.3f}s)。音ズレの可能性があります。")

    output.parent.mkdir(parents=True, exist_ok=True)
    run(build_cmd(args, video_src, track_src, output))
    print(f"完了しました: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
