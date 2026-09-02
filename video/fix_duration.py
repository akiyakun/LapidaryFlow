#!/usr/bin/env python3
r"""
fix_duration.py

コンテナに記録された再生時間(duration)が実際の中身と食い違っている動画を修復する。

よくある原因:
  * 録画中のクラッシュ等でヘッダの duration が書き戻されていない
  * MP4 ヘッダ(mvhd/mdhd)だけ嘘の値で、VLC や Windows のプロパティが長時間と表示する
  * チャプターやタイムコードのトラック(mp4 では data/bin_data)だけが長い duration を持ち、
    それに引っ張られて全体尺が長く見える
  * タイムスタンプが飛んでいて、実尺よりはるかに長い/短い duration になっている

ffprobe の format.duration だけでなく、各ストリーム・タグ・MP4 ヘッダの値も読み、
実際にパケットを読み切った時刻(実測)と比べて、ひとつでもずれていれば修正対象とする。

方式:
  remux (既定) : 全パケットを読み直して duration を書き直す。無劣化・高速。
                 パケットのタイムスタンプ自体が正しい場合はこれで直る。
  retime       : 映像を生ストリームに取り出し、指定 fps の等間隔タイムスタンプを
                 振り直して入れ直す。タイムスタンプそのものが壊れている場合用。
                 H.264 / H.265 のみ対応。音声は元ファイルからコピーする。

必要:
    ffmpeg
    ffprobe
    Python 3.9+

例:
    # 判定だけ行う(書き込みなし)
    python fix_duration.py --check "broken.mp4"

    # duration を書き直して broken_fixed.mp4 を作る
    python fix_duration.py "broken.mp4"

    # タイムスタンプごと 29.97fps で振り直す
    python fix_duration.py --method retime --fps 30000/1001 "broken.mp4"
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

RAW_FORMATS = {"h264": "h264", "hevc": "hevc"}

OUT_TIME_RE = re.compile(r"^out_time=(\d+):(\d\d):(\d\d(?:\.\d+)?)$", re.MULTILINE)


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


def probe(path: Path) -> dict:
    out = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture=True,
    )
    return json.loads(out)


def video_stream(info: dict) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return None


def _to_seconds(value) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
        m = re.fullmatch(r"(\d+):(\d\d):(\d\d(?:\.\d+)?)", value.strip())
        if not m:
            return None
        seconds = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    return seconds if seconds > 0 else None


def _iter_boxes(f, end: int):
    while f.tell() < end:
        start = f.tell()
        header = f.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[0:4], "big")
        box_type = header[4:8]
        header_size = 8
        if size == 1:
            size = int.from_bytes(f.read(8), "big")
            header_size = 16
        elif size == 0:
            size = end - start
        if size < header_size or start + size > end:
            return
        yield box_type, start + header_size, start + size
        f.seek(start + size)


def _read_timescale_duration(f, body_start: int) -> tuple[int, int] | None:
    """mvhd から (timescale, duration) を読む。"""
    f.seek(body_start)
    head = f.read(4)
    if len(head) < 4:
        return None
    version = head[0]
    f.read(16 if version == 1 else 8)
    raw = f.read(12 if version == 1 else 8)
    if len(raw) < (12 if version == 1 else 8):
        return None
    timescale = int.from_bytes(raw[0:4], "big")
    duration = int.from_bytes(raw[4:], "big")
    if timescale <= 0 or duration in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return None
    return timescale, duration


def _read_tkhd_duration(f, body_start: int) -> int | None:
    """tkhd の duration(movie timescale 単位)。edit list 反映後の再生長。"""
    f.seek(body_start)
    head = f.read(4)
    if len(head) < 4:
        return None
    version = head[0]
    f.read((16 if version == 1 else 8) + 8)
    size = 8 if version == 1 else 4
    raw = f.read(size)
    if len(raw) < size:
        return None
    duration = int.from_bytes(raw, "big")
    if duration in (0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
        return None
    return duration


def mp4_header_durations(path: Path) -> list[tuple[str, float]]:
    """MP4/MOV のヘッダに書かれた duration。VLC やエクスプローラーはこちらを見る。"""
    found: list[tuple[str, float]] = []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            file_end = f.tell()
            f.seek(0)
            moov = None
            for box_type, body, box_end in _iter_boxes(f, file_end):
                if box_type == b"moov":
                    moov = (body, box_end)
                    break
            if moov is None:
                return found
            movie_timescale = None
            f.seek(moov[0])
            for box_type, body, _ in _iter_boxes(f, moov[1]):
                if box_type == b"mvhd":
                    value = _read_timescale_duration(f, body)
                    if value:
                        movie_timescale = value[0]
                        found.append(("mvhd (ヘッダ)", value[1] / value[0]))
                    break
            if movie_timescale is None:
                return found
            f.seek(moov[0])
            track = 0
            for box_type, body, box_end in _iter_boxes(f, moov[1]):
                if box_type != b"trak":
                    continue
                track += 1
                f.seek(body)
                for t2, b2, _ in _iter_boxes(f, box_end):
                    if t2 == b"tkhd":
                        duration = _read_tkhd_duration(f, b2)
                        if duration:
                            found.append(
                                (f"tkhd (track {track})", duration / movie_timescale)
                            )
                        break
    except OSError:
        return found
    return found


def declared_durations(path: Path, info: dict) -> list[tuple[str, float]]:
    """ファイルが自己申告している duration をすべて集める。"""
    found: list[tuple[str, float]] = []
    value = _to_seconds(info.get("format", {}).get("duration"))
    if value is not None:
        found.append(("format", value))
    for s in info.get("streams", []):
        codec = s.get("codec_name") or s.get("codec_tag_string") or "?"
        label = f"stream {s.get('index')} ({s.get('codec_type')}/{codec})"
        value = _to_seconds(s.get("duration"))
        if value is not None:
            found.append((label, value))
        value = _to_seconds((s.get("tags") or {}).get("DURATION"))
        if value is not None:
            found.append((f"{label} tag", value))
    found.extend(mp4_header_durations(path))
    return found


def measure_duration(path: Path) -> float:
    """全パケットを読み飛ばして、実際に再生される終端時刻を得る(デコードなし)。"""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-i",
        str(path),
        # 壊れたデータ/チャプタートラックは実尺の根拠にならないので除外
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map_chapters",
        "-1",
        "-c",
        "copy",
        "-f",
        "null",
        "-progress",
        "pipe:1",
        "-",
    ]
    print("+", subprocess.list2cmdline(cmd))
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit("実尺の測定に失敗しました。ファイルが壊れている可能性があります。")
    matches = OUT_TIME_RE.findall(p.stdout)
    if not matches:
        sys.stderr.write(p.stderr)
        raise SystemExit("実尺の測定に失敗しました (out_time を取得できません)。")
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def frame_rate(stream: dict) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        text = stream.get(key)
        if not text or text in ("0/0", "N/A"):
            continue
        try:
            value = Fraction(text)
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def hms(seconds: float) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def mux_opts(dst: Path) -> list[str]:
    if dst.suffix.lower() not in (".mp4", ".mov", ".m4v"):
        return []
    # -write_tmcd 0 : timecode メタデータから tmcd トラックを作らせない
    return ["-write_tmcd", "0", "-movflags", "+faststart"]


def remux(src: Path, dst: Path, keep_data: bool) -> None:
    maps = ["-map", "0:v", "-map", "0:a?", "-map", "0:s?"]
    if keep_data:
        maps += ["-map", "0:d?", "-map", "0:t?"]
    else:
        # mp4 のチャプターは bin_data トラックとして書かれ、壊れていると尺を引っ張る
        maps += ["-map_chapters", "-1"]
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-stats",
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(src),
            *maps,
            "-c",
            "copy",
            *mux_opts(dst),
            str(dst),
        ]
    )


def retime(src: Path, dst: Path, codec: str, fps: Fraction) -> None:
    raw_format = RAW_FORMATS.get(codec)
    if raw_format is None:
        raise SystemExit(
            f"retime は H.264 / H.265 のみ対応しています (このファイルは {codec})。"
        )
    raw = dst.with_name(dst.stem + f".raw.{raw_format}")
    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-stats",
                "-y",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                raw_format,
                str(raw),
            ]
        )
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-stats",
                "-y",
                "-fflags",
                "+genpts",
                "-r",
                str(fps),
                "-i",
                str(raw),
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-map_chapters",
                "-1",
                "-c",
                "copy",
                *mux_opts(dst),
                str(dst),
            ]
        )
    finally:
        raw.unlink(missing_ok=True)


def report(label: str, declared: list[tuple[str, float]], actual: float) -> float:
    """申告値を一覧表示し、実測との最大ズレを返す。"""
    print(f"  {label} 実測 : {hms(actual)}")
    worst = 0.0
    for name, value in declared:
        diff = value - actual
        worst = max(worst, abs(diff))
        print(f"    {name:<22} {hms(value)}  (差 {diff:+.3f} 秒)")
    if not declared:
        print("    申告 duration が見つかりません。")
    return worst


def process(src: Path, args: argparse.Namespace) -> bool:
    info = probe(src)
    stream = video_stream(info)
    actual = measure_duration(src)
    worst = report("修正前", declared_durations(src, info), actual)

    if not args.force and worst <= args.tolerance:
        print("  [OK] 許容範囲内です。修正しません。")
        return True

    if args.check:
        print("  [NG] duration がずれています (--check のため修正はしません)。")
        return True

    dst = src.with_name(src.stem + args.suffix + src.suffix)
    if dst.exists() and not args.overwrite:
        print(f"  [SKIP] 出力先が既に存在します: {dst.name}")
        return True

    if args.method == "remux":
        if not args.keep_data and any(
            s.get("codec_type") in ("data", "attachment") for s in info.get("streams", [])
        ):
            print("  データトラック(チャプター/タイムコード等)は破棄します。残すなら --keep-data")
        remux(src, dst, args.keep_data)
    else:
        fps = Fraction(args.fps) if args.fps else (frame_rate(stream) if stream else None)
        if fps is None or fps <= 0:
            raise SystemExit("fps を判定できません。--fps で明示してください。")
        codec = (stream or {}).get("codec_name", "")
        print(f"  振り直す fps  : {fps} ({float(fps):.3f})")
        retime(src, dst, codec, fps)

    print(f"  出力          : {dst.name}")
    fixed_actual = measure_duration(dst)
    worst = report("修正後", declared_durations(dst, probe(dst)), fixed_actual)
    if worst > args.tolerance:
        print("  [WARN] まだずれています。タイムスタンプ自体が壊れている可能性があります。")
        if args.method == "remux":
            print("         --method retime --fps <実fps> を試してください。")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="動画の duration を実際の長さに修正する")
    parser.add_argument("sources", nargs="+", help="対象の動画ファイル")
    parser.add_argument(
        "--method",
        choices=["remux", "retime"],
        default="remux",
        help="remux: duration の書き直しのみ / retime: タイムスタンプを等間隔に振り直す",
    )
    parser.add_argument("--fps", help="retime 用の fps (例: 30000/1001)。省略時は元の値")
    parser.add_argument(
        "--tolerance", type=float, default=1.0, help="この秒数以下の差は正常とみなす"
    )
    parser.add_argument("--suffix", default="_fixed", help="出力ファイル名に付ける接尾辞")
    parser.add_argument("--check", action="store_true", help="判定のみ行い、書き込まない")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="データ/チャプター/タイムコードを残す(既定は破棄。これが誤った duration の原因になりやすい)",
    )
    parser.add_argument("--force", action="store_true", help="差分が許容範囲内でも修正する")
    parser.add_argument("--overwrite", action="store_true", help="出力先が既にあっても上書きする")
    args = parser.parse_args()

    failed = False
    for name in args.sources:
        src = Path(name).expanduser().resolve()
        print("=" * 58)
        print(f" {src.name}")
        print("=" * 58)
        if not src.is_file():
            print("  [ERROR] ファイルが見つかりません。")
            failed = True
            continue
        try:
            if not process(src, args):
                failed = True
        except SystemExit as e:
            print(f"  [ERROR] {e}")
            failed = True
        print()

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
