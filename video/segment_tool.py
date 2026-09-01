#!/usr/bin/env python3
r"""
segment_tool.py

長尺動画を分割し、外部ツール(Topaz等)で処理した後にフレーム単位で再結合するためのツール。

カット位置はまず近傍のシーンチェンジに寄せ、次に最寄りのキーフレームにスナップされる。
さらに前後に handle 秒分の余分なフレーム(参照用ののりしろ)を付けて切る。時間軸を見るAI
モデルはクリップ先頭で参照フレームが不足し画質が安定しないため、採用区間に入る前に
モデルを温める。のりしろは join 時にフレーム番号で切り落とす。

映像ストリームのみを扱う(音声・字幕は破棄)。

必要:
    ffmpeg
    ffprobe
    Python 3.9+

例:
    # 15分ごとに自動分割(シーン整列 + 前後5秒ののりしろ)
    python segment_tool.py cut --source "The Mask.mp4" --output-dir ".\cut"

    # TopazでProRes出力した後、のりしろを除いてロスレス結合
    python segment_tool.py join ^
      --manifest ".\cut\segment_plan.json" ^
      --processed-dir ".\upscaled" ^
      --output ".\final.mov" --copy
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from bisect import bisect_left, bisect_right
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".m2ts"}

DEFAULT_ENCODE_ARGS = "-c:v libx264 -preset slow -crf 14 -pix_fmt yuv420p"


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


def probe_frames(source: Path) -> tuple[list[float], list[int]]:
    """映像フレームの表示順ptsと、キーフレームの表示順indexを返す。"""
    out = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags",
        "-of", "csv=print_section=0",
        str(source),
    ], capture=True)

    items: list[tuple[float, bool]] = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        pts_text, flags = parts[0], parts[1]
        if pts_text in ("", "N/A"):
            continue
        try:
            items.append((float(pts_text), "K" in flags))
        except ValueError:
            continue

    if not items:
        raise RuntimeError(f"映像フレームを取得できませんでした: {source}")

    items.sort(key=lambda x: x[0])
    pts = [x[0] for x in items]
    key_idx = [i for i, x in enumerate(items) if x[1]]

    if not key_idx:
        raise RuntimeError(f"キーフレームを検出できませんでした: {source}")
    return pts, key_idx


def frame_duration(pts: list[float]) -> float:
    if len(pts) < 2:
        return 1.0 / 30.0
    diffs = sorted(b - a for a, b in zip(pts, pts[1:]))
    return diffs[len(diffs) // 2]


def count_frames(path: Path) -> int:
    out = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0",
        str(path),
    ], capture=True)
    return int(out.strip().rstrip(","))


def get_frame_rate(path: Path) -> Fraction:
    out = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "csv=p=0",
        str(path),
    ], capture=True).strip().rstrip(",")
    num, _, den = out.partition("/")
    fps = Fraction(int(num), int(den or 1))
    if fps <= 0:
        raise RuntimeError(f"フレームレートを取得できませんでした: {path}")
    return fps


def first_frame_hash(path: Path, start: float | None = None) -> str:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "framemd5", "-"]
    out = run(cmd, capture=True)
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.split(",")[-1].strip().lower()
    raise RuntimeError(f"フレームハッシュを取得できませんでした: {path}")


def snap_boundary(
    t: float,
    kf_times: list[float],
    key_idx: list[int],
    mode: str,
) -> int:
    """時刻 t を最寄りのキーフレームに合わせ、その表示順indexを返す。"""
    j = bisect_left(kf_times, t)

    before = j - 1 if j > 0 else None
    after = j if j < len(kf_times) else None

    if mode == "before":
        if before is None:
            raise RuntimeError(f"{fmt_time(t)} より前にキーフレームがありません。")
        return key_idx[before]

    if mode == "after":
        if after is None:
            raise RuntimeError(f"{fmt_time(t)} より後にキーフレームがありません。")
        return key_idx[after]

    if before is None:
        assert after is not None
        return key_idx[after]
    if after is None:
        return key_idx[before]
    if (t - kf_times[before]) <= (kf_times[after] - t):
        return key_idx[before]
    return key_idx[after]


def handle_start_index(
    keep_start: int,
    pts: list[float],
    kf_times: list[float],
    key_idx: list[int],
    handle: float,
) -> int:
    """keep_start の手前に handle 秒ぶんの参照フレームを足した開始index(キーフレーム)。"""
    if keep_start == 0 or handle <= 0:
        return keep_start
    target = pts[keep_start] - handle
    j = bisect_right(kf_times, target) - 1
    return key_idx[max(0, j)]


def handle_end_index(keep_end: int, pts: list[float], handle: float) -> int:
    """keep_end の後ろに handle 秒ぶんの参照フレームを足した終了index(排他)。"""
    n = len(pts)
    if keep_end >= n or handle <= 0:
        return min(n, keep_end)
    target = pts[keep_end - 1] + handle
    return min(n, max(keep_end, bisect_right(pts, target)))


def natural_key(path: Path):
    return [
        int(x) if x.isdigit() else x
        for x in re.split(r"(\d+)", path.name.lower())
    ]


def fmt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    seconds -= h * 3600
    m = int(seconds // 60)
    seconds -= m * 60
    return f"{h:02d}.{m:02d}.{seconds:06.3f}"


def detect_scene_changes(source: Path, threshold: float) -> list[float]:
    """シーンチェンジと判定されたフレームのpts一覧を秒で返す(全フレームdecodeが走る)。"""
    out = run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
        "-i", str(source),
        "-an", "-sn",
        "-filter:v", f"select='gt(scene,{threshold})',metadata=print:file=-",
        "-f", "null", "-",
    ], capture=True)

    times = [float(m) for m in re.findall(r"pts_time:(\d+(?:\.\d+)?)", out)]
    times.sort()
    return times


def nearest_scene(t: float, scenes: list[float], window: float) -> float | None:
    lo = bisect_left(scenes, t - window)
    hi = bisect_right(scenes, t + window)
    if lo >= hi:
        return None
    return min(scenes[lo:hi], key=lambda s: abs(s - t))


def build_cuts(
    duration: float,
    cuts_text: str | None,
    chunk: float,
    parts: int | None,
) -> list[float]:
    if cuts_text:
        return sorted(parse_time(x) for x in cuts_text.split(",") if x.strip())

    if parts:
        if parts < 2:
            raise RuntimeError("--parts は 2 以上にしてください。")
        step = duration / parts
        return [step * i for i in range(1, parts)]

    if chunk <= 0:
        raise RuntimeError("--chunk は 0 より大きくしてください。")

    times: list[float] = []
    t = chunk
    while t < duration:
        times.append(t)
        t += chunk

    # 末尾が短すぎると変換キューが無駄に増えるので手前のセグメントに吸収させる
    if times and duration - times[-1] < chunk * 0.5:
        times.pop()

    return times


def do_cut(
    source: Path,
    output_dir: Path,
    cuts_text: str | None = None,
    chunk: float = 900.0,
    parts: int | None = None,
    snap: str = "nearest",
    snap_tolerance: float = 0.0,
    handle: float = 5.0,
    align: str = "scene",
    scene_threshold: float = 0.35,
    scene_window: float = 30.0,
    verify: bool = True,
) -> Path:
    require_tools()

    source = source.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=== probe ===")
    pts, key_idx = probe_frames(source)
    n = len(pts)
    fdur = frame_duration(pts)
    duration = pts[-1] + fdur
    kf_times = [pts[i] for i in key_idx]
    print(
        f"{source.name}  {fmt_time(duration)}  "
        f"{n} frames  {len(key_idx)} keyframes\n"
    )

    cut_times = build_cuts(duration, cuts_text, chunk, parts)
    if not cut_times:
        raise RuntimeError(
            f"分割位置がありません。動画長({fmt_time(duration)})が"
            f"チャンク長より短い可能性があります。"
        )
    if any(t <= 0 or t >= duration for t in cut_times):
        raise RuntimeError(
            f"cut位置は 0秒より後、動画長({duration:.3f}秒)より前にしてください。"
        )

    if align == "scene":
        print(f"=== scene detection (threshold {scene_threshold}) ===")
        scenes = detect_scene_changes(source, scene_threshold)
        print(f"scene changes: {len(scenes)}")

        aligned: list[float] = []
        for t in cut_times:
            s = nearest_scene(t, scenes, scene_window)
            if s is None:
                print(f"  {fmt_time(t)}: ±{scene_window:.0f}s 内に候補なし -> そのまま")
                aligned.append(t)
            else:
                print(f"  {fmt_time(t)} -> {fmt_time(s)}  ({s - t:+.3f}s)")
                aligned.append(s)
        cut_times = aligned
        print()

    print("=== snap to keyframe ===")
    snapped: list[int] = []
    for t in cut_times:
        idx = snap_boundary(t, kf_times, key_idx, snap)
        shift = pts[idx] - t
        if snap_tolerance > 0 and abs(shift) > snap_tolerance:
            raise RuntimeError(
                f"{fmt_time(t)} の最寄りキーフレームが {shift:+.3f}秒 離れており、"
                f"許容値 {snap_tolerance:.3f}秒 を超えています。"
            )
        print(f"  {fmt_time(t)} -> {fmt_time(pts[idx])}  frame {idx}  ({shift:+.3f}s)")
        snapped.append(idx)

    # スナップ後に重複した境界は空セグメントになるため除去
    boundaries = [0] + sorted({i for i in snapped if 0 < i < n}) + [n]
    if len(boundaries) < 3:
        raise RuntimeError("スナップ後に有効なカット位置が残りませんでした。")

    stem = source.stem
    ext = source.suffix

    print(f"\n=== cut (video only, handle {handle:.2f}s) ===")

    segments: list[dict] = []
    for i in range(len(boundaries) - 1):
        keep_start = boundaries[i]
        keep_end = boundaries[i + 1]
        cut_start = handle_start_index(keep_start, pts, kf_times, key_idx, handle)
        cut_end = handle_end_index(keep_end, pts, handle)

        head_trim = keep_start - cut_start
        tail_trim = cut_end - keep_end
        seg_frames = cut_end - cut_start
        keep_count = keep_end - keep_start

        name = f"{stem}-seg{i + 1:02d}-{fmt_time(pts[keep_start])}{ext}"
        dst = output_dir / name

        # backward seekが確実に cut_start のキーフレームに着地するよう半フレーム後ろを指す
        ss = pts[cut_start] + fdur * 0.5
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-noaccurate_seek",
            "-ss", f"{ss:.6f}",
            "-i", str(source),
            "-map", "0:v:0",
            "-c", "copy",
            "-frames:v", str(seg_frames),
            "-avoid_negative_ts", "make_zero",
            "-y", str(dst),
        ])

        actual = count_frames(dst)
        if actual != seg_frames:
            raise RuntimeError(
                f"{name}: フレーム数が想定と違います "
                f"(expected {seg_frames}, got {actual})"
            )

        if verify and first_frame_hash(dst) != first_frame_hash(source, pts[cut_start]):
            raise RuntimeError(
                f"{name}: 先頭フレームが source frame {cut_start} と一致しません。"
            )

        segments.append({
            "index": i + 1,
            "file": name,
            "seg_frames": seg_frames,
            "head_trim": head_trim,
            "keep_count": keep_count,
            "tail_trim": tail_trim,
            "source_start": keep_start,
            "source_end": keep_end - 1,
        })

        print(
            f"  seg{i + 1:02d}  {seg_frames:>7} frames  "
            f"keep {keep_start}..{keep_end - 1}  "
            f"(head {head_trim}, tail {tail_trim})  {name}"
        )

    manifest = {
        "source": str(source),
        "source_frames": n,
        "frame_duration": fdur,
        "handle_seconds": handle,
        "align": align,
        "segments": segments,
    }
    manifest_path = output_dir / "segment_plan.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n分割完了: {len(segments)} segments -> {output_dir}")
    print(f"manifest: {manifest_path}")
    return manifest_path


def do_join(
    manifest_path: Path,
    processed_dir: Path,
    output: Path,
    encode_args: list[str],
    copy_mode: bool = False,
) -> None:
    require_tools()

    plan = json.loads(manifest_path.read_text(encoding="utf-8"))
    segs = plan["segments"]

    files = [
        p for p in processed_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    files.sort(key=natural_key)

    if len(files) != len(segs):
        raise RuntimeError(
            f"ファイル数が manifest と一致しません "
            f"(expected {len(segs)}, got {len(files)}): {[p.name for p in files]}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)

    print("=== verify processed segments ===")

    if copy_mode:
        entries: list[str] = []
        for seg, f in zip(segs, files):
            pts, key_idx = probe_frames(f)
            if len(pts) != seg["seg_frames"]:
                raise RuntimeError(
                    f"{f.name}: フレーム数が {len(pts)} で、分割時の {seg['seg_frames']} と"
                    "一致しません。フレーム補間やfps変更が有効になっていないか確認してください。"
                )
            if len(key_idx) != len(pts):
                raise RuntimeError(
                    f"{f.name}: 全イントラではありません "
                    f"(keyframes {len(key_idx)} / frames {len(pts)})。"
                    "--copy には FFV1 や ProRes など全フレームがキーフレームの形式が必要です。"
                )

            fdur = frame_duration(pts)
            head = seg["head_trim"]
            keep = seg["keep_count"]
            # 全フレームがキーフレームなので、半フレームずらせばseekは常に狙ったフレームに着地する
            inpoint = pts[head] + fdur * 0.5
            outpoint = pts[head + keep - 1] + fdur * 0.5
            # durationを省くとconcat側のオフセット計算が丸まり、継ぎ目でtimestampが衝突する
            keep_duration = float(Fraction(keep) / get_frame_rate(f))

            entries.append(
                f"file '{str(f.resolve())}'\n"
                f"inpoint {inpoint:.6f}\n"
                f"outpoint {outpoint:.6f}\n"
                f"duration {keep_duration:.6f}"
            )
            print(f"  seg{seg['index']:02d}  {len(pts):>7} frames -> keep {keep}  {f.name}")

        list_path = output.parent / f"{output.stem}_concat.txt"
        list_path.write_text("\n".join(entries) + "\n", encoding="utf-8")

        run([
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
            "-f", "concat", "-safe", "0",
            "-segment_time_metadata", "1",
            "-i", str(list_path),
            "-map", "0:v:0",
            "-c", "copy",
            "-y", str(output),
        ])
    else:
        filters: list[str] = []
        for i, (seg, f) in enumerate(zip(segs, files)):
            actual = count_frames(f)
            if actual != seg["seg_frames"]:
                raise RuntimeError(
                    f"{f.name}: フレーム数が {actual} で、分割時の {seg['seg_frames']} と"
                    "一致しません。フレーム補間やfps変更が有効になっていないか確認してください。"
                )
            head = seg["head_trim"]
            keep = seg["keep_count"]
            filters.append(
                f"[{i}:v]trim=start_frame={head}:end_frame={head + keep},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            print(f"  seg{seg['index']:02d}  {actual:>7} frames -> keep {keep}  {f.name}")

        graph = (
            ";".join(filters)
            + ";"
            + "".join(f"[v{i}]" for i in range(len(segs)))
            + f"concat=n={len(segs)}:v=1:a=0[out]"
        )

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats"]
        for f in files:
            cmd += ["-i", str(f)]
        cmd += ["-filter_complex", graph, "-map", "[out]"]
        cmd += encode_args
        cmd += ["-y", str(output)]
        run(cmd)

    expected = sum(s["keep_count"] for s in segs)
    out_pts, _ = probe_frames(output)
    actual = len(out_pts)
    print(f"\n結合完了: {output}")
    print(f"expected frames: {expected} / actual: {actual}")
    if expected != actual:
        raise RuntimeError("結合後のフレーム数が期待値と一致しません。")
    if len(set(out_pts)) != actual:
        raise RuntimeError(
            "結合後のタイムスタンプに重複があります。"
            "後段のツールでフレームが欠落する可能性があります。"
        )
    if expected != plan["source_frames"]:
        print(
            f"注意: 元動画は {plan['source_frames']} frames です "
            f"(差分 {expected - plan['source_frames']})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="長尺動画をキーフレーム境界で分割し、処理後にフレーム単位で再結合する"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cut = sub.add_parser("cut", help="stream copy で分割し manifest を出力")
    p_cut.add_argument("--source", required=True, type=Path)
    p_cut.add_argument("--output-dir", required=True, type=Path)

    mode = p_cut.add_mutually_exclusive_group()
    mode.add_argument("--chunk", type=parse_time, default=900.0,
                      help="1セグメントの長さ。例: 15:00 (default: 15:00)")
    mode.add_argument("--parts", type=int, default=None,
                      help="指定数で等分割する")
    mode.add_argument("--cuts", default=None,
                      help='手動でカット位置を指定。例: "00:01:41.768,00:06:57.250"')

    p_cut.add_argument("--handle", type=float, default=5.0,
                       help="前後に付ける参照用余分フレームの秒数。0で無効 (default: 5.0)")
    p_cut.add_argument("--align", choices=["scene", "none"], default="scene",
                       help="カット位置を近傍のシーンチェンジに寄せる (default: scene)")
    p_cut.add_argument("--scene-threshold", type=float, default=0.35,
                       help="シーン検出のしきい値 0.0-1.0 (default: 0.35)")
    p_cut.add_argument("--scene-window", type=float, default=30.0,
                       help="シーンを探す前後の秒数 (default: 30.0)")
    p_cut.add_argument("--snap", choices=["nearest", "before", "after"],
                       default="nearest",
                       help="カット位置を近傍キーフレームに合わせる (default: nearest)")
    p_cut.add_argument("--snap-tolerance", type=float, default=0.0,
                       help="許容するズレ秒数。超えたらエラー。0で無制限 (default: 0)")
    p_cut.add_argument("--no-verify", action="store_true",
                       help="先頭フレームの照合をスキップする")

    p_join = sub.add_parser("join", help="処理済みセグメントから余分なフレームを除いて結合")
    p_join.add_argument("--manifest", required=True, type=Path,
                        help="cut が出力した segment_plan.json")
    p_join.add_argument("--processed-dir", required=True, type=Path,
                        help="Topaz等で処理したセグメントが入ったフォルダ")
    p_join.add_argument("--output", required=True, type=Path)
    p_join.add_argument("--copy", action="store_true",
                        help="全イントラ(FFV1/ProRes等)前提で、エンコードせずに結合する")
    p_join.add_argument("--encode-args", default=DEFAULT_ENCODE_ARGS,
                        help=f"--copy 未指定時の出力エンコード引数 (default: {DEFAULT_ENCODE_ARGS})")

    args = parser.parse_args()

    try:
        if args.command == "cut":
            do_cut(
                args.source,
                args.output_dir,
                cuts_text=args.cuts,
                chunk=args.chunk,
                parts=args.parts,
                snap=args.snap,
                snap_tolerance=args.snap_tolerance,
                handle=args.handle,
                align=args.align,
                scene_threshold=args.scene_threshold,
                scene_window=args.scene_window,
                verify=not args.no_verify,
            )
        else:
            do_join(
                args.manifest,
                args.processed_dir,
                args.output,
                shlex.split(args.encode_args),
                copy_mode=args.copy,
            )
        return 0

    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
