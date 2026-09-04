#!/usr/bin/env python3
r"""
propainter_tool.py

tools/mask_tool/index.html が書き出した mask_plan.json を読んで、
ロゴ消し (ProPainter) の一連の処理を回す。

サブコマンド:
  extract : パターンごとのマスク PNG と、区間ごとの切り出し連番 PNG を作る。
  run     : 区間ごとに ProPainter (inference_propainter.py) を走らせる。
  compose : ProPainter の結果を元動画に1パスで合成して完成品を1本出す。

区間 (range) は「ロゴ位置が同じフレームの連続」で、start/end は両端を含む
フレーム番号。パターン (pattern) は「消したい矩形 + 周囲余白」で、そこから
ProPainter に渡す crop 範囲が決まる。

合成の注意 (実測で確認した落とし穴):
  * overlay の format は既定が yuv420 なので、明示しないと 10bit 4:2:2 の
    マスターが全編 8bit 4:2:0 に落ちる。
  * setpts は切り捨てなので PTS+S/FR/TB だとパッチが 1 フレーム早くズレる。
    settb=1/1 でフレーム番号タイムベースにして整数演算にする。
  * overlay の X は 4:2:2/4:2:0 で偶数に丸められる (輝度ごと動く)。
    crop の原点が偶数であることを前提にしている。

必要:
    ffmpeg
    ffprobe
    Python 3.9+
    ProPainter (mask_plan.json の propainter.dir / propainter.python で指定)

例:
    python propainter_tool.py extract --plan mask_plan.json
    python propainter_tool.py run     --plan mask_plan.json
    python propainter_tool.py compose --plan mask_plan.json --preflight
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from fractions import Fraction
from pathlib import Path

PLAN_VERSION = 1

# ProPainter は 8bit で処理するので、10bit ソースから rgb48be を吐かせても意味がない
EXTRACT_PIX_FMT = "rgb24"

DEFAULT_VCODEC = [
    "-c:v", "prores_ks", "-profile:v", "3",
    "-pix_fmt", "yuv422p10le", "-vendor", "apl0",
]
DEFAULT_OVERLAY_FORMAT = "yuv422p10"

# ProPainter はマスクを膨張させてから塗るので、貼り戻す範囲は矩形より少し広く取る
DEFAULT_PATCH_MARGIN = 16

FRAME_NAME_RE = re.compile(r"^(\d+)\.png$", re.IGNORECASE)


def _safe_console() -> None:
    """コンソールに出せない文字でも落ちないようにする。

    bat は chcp 932 で動くので、CP932 に無い文字 (例: 「歲」U+6B72) を含む
    パスを print すると UnicodeEncodeError で止まってしまう。? に潰すと
    パスが読めなくなるので \\uXXXX 表記に落とす。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def run(cmd: list[str], capture: bool = False, cwd: Path | None = None) -> str:
    print("+", subprocess.list2cmdline(cmd))
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
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


def first_frame_hash(path: Path, start: float | None = None, crop: dict | None = None) -> str:
    """先頭 1 フレームの md5。crop を渡すと同じ切り出しをしてから取る。

    ソースが 10bit YUV で切り出し結果が 8bit RGB なので、比較できるように
    どちらも rgb24 に揃えてからハッシュする。
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(path), "-map", "0:v:0"]
    if crop:
        cmd += ["-vf", f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}"]
    cmd += ["-frames:v", "1", "-pix_fmt", EXTRACT_PIX_FMT, "-f", "framemd5", "-"]
    out = run(cmd, capture=True)
    for line in out.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.split(",")[-1].strip().lower()
    raise RuntimeError(f"フレームハッシュを取得できませんでした: {path}")


def probe_source(path: Path) -> dict:
    out = run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-print_format", "json",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,start_pts,pix_fmt,nb_frames",
        str(path),
    ], capture=True)
    streams = (json.loads(out or "{}").get("streams") or [])
    if not streams:
        raise RuntimeError(f"映像ストリームが見つかりません: {path}")
    return streams[0]


# ---- PNG -----------------------------------------------------------------

def write_mask_png(path: Path, width: int, height: int, box: tuple[int, int, int, int]) -> None:
    """黒地に白い矩形の 8bit グレースケール PNG を書く。

    ffmpeg (lavfi color + drawbox) 経由だと -update 1 が要る上に swscale の
    レンジ推定次第で黒が 16 になりうるので、自前で書いて 0/255 を保証する。
    """
    bx, by, bw, bh = box
    white = bytes([255]) * bw
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # フィルタタイプ None
        if by <= y < by + bh:
            row = bytearray(bytes(width))
            row[bx:bx + bw] = white
            raw += row
        else:
            raw += bytes(width)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError(f"PNG として読めません: {path}")
    return struct.unpack(">II", head[16:24])


def frame_files(folder: Path) -> list[Path]:
    files = [p for p in folder.iterdir() if p.is_file() and FRAME_NAME_RE.match(p.name)]
    files.sort(key=lambda p: int(FRAME_NAME_RE.match(p.name).group(1)))
    return files


def frame_pattern(folder: Path) -> str:
    """連番フォルダから ffmpeg の入力パターンを組み立てる。

    ProPainter の出力桁数はバージョンで変わるので、実ファイル名から拾う。
    """
    files = frame_files(folder)
    if not files:
        raise RuntimeError(f"連番 PNG が見つかりません: {folder}")
    digits = len(FRAME_NAME_RE.match(files[0].name).group(1))
    return str(folder / f"%0{digits}d.png")


# ---- plan ----------------------------------------------------------------

def clean_path(s) -> str:
    """前後の引用符と空白を落とす。

    Windows の「パスのコピー」は "C:\\..." と引用符付きで入るので、JSON を手で
    直したときにそのまま残っていることがある。マスクツール側でも落としているが、
    手書きの JSON も受けられるようにここでも落とす。
    """
    return str(s or "").strip().strip("\"'").strip()


class Plan:
    def __init__(self, data: dict, path: Path, source: Path | None, work: Path | None):
        self.data = data
        self.path = path
        self.patterns = {p["id"]: p for p in data.get("patterns", [])}
        self.ranges = sorted(data.get("ranges", []), key=lambda r: r["start"])
        self.fps = float(data.get("fps") or 0)
        self.total_frames = int(data.get("totalFrames") or 0)
        self.media_w = int(data.get("mediaW") or 0)
        self.media_h = int(data.get("mediaH") or 0)
        self.source = source or Path(clean_path(data.get("source")))
        self.work = work or path.parent / f"{path.stem}_work"
        pp = data.get("propainter") or {}
        self.pp_dir = Path(clean_path(pp.get("dir")))
        self.pp_python = clean_path(pp.get("python"))
        self.pp_args = [str(a) for a in (pp.get("args") or [])]

    def pattern_of(self, r: dict) -> dict:
        return self.patterns[r["patternId"]]

    def frames_dir(self, r: dict) -> Path:
        return self.work / "frames" / r["slug"]

    def mask_path(self, p: dict) -> Path:
        return self.work / "masks" / f"{p['slug']}_crop_mask.png"

    def results_dir(self) -> Path:
        return self.work / "results"


def load_plan(args) -> Plan:
    path = Path(args.plan).resolve()
    if not path.is_file():
        raise RuntimeError(f"JSON が見つかりません: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("v") != PLAN_VERSION:
        raise RuntimeError(f"対応していない JSON バージョンです (v={data.get('v')})")

    source = Path(args.source).resolve() if getattr(args, "source", None) else None
    work = Path(args.work_dir).resolve() if getattr(args, "work_dir", None) else None
    plan = Plan(data, path, source, work)

    if not plan.patterns:
        raise RuntimeError("パターンが1つもありません。")
    if not plan.ranges:
        raise RuntimeError("区間が1つもありません。")

    # 以下はどれも「黙って壊れる」種類なので必ず止める
    for p in plan.patterns.values():
        c, rect = p["crop"], p["rect"]
        name = p["name"]
        if c["w"] % 8 or c["h"] % 8:
            raise RuntimeError(
                f"パターン {name} の crop サイズが 8 の倍数ではありません "
                f"({c['w']}x{c['h']})。ProPainter が内部で縮小→拡大します。"
            )
        if c["x"] % 2 or c["y"] % 2:
            raise RuntimeError(
                f"パターン {name} の crop 原点が偶数ではありません ({c['x']},{c['y']})。"
                "overlay が X を偶数に丸めるためパッチが 1px ずれます。"
                "マスクツールで作り直してください。"
            )
        if plan.media_w and (c["x"] + c["w"] > plan.media_w or c["y"] + c["h"] > plan.media_h):
            raise RuntimeError(f"パターン {name} の crop が動画の外にはみ出しています。")
        if not (c["x"] <= rect["x"] and c["y"] <= rect["y"]
                and rect["x"] + rect["w"] <= c["x"] + c["w"]
                and rect["y"] + rect["h"] <= c["y"] + c["h"]):
            raise RuntimeError(f"パターン {name} の crop がマスク矩形を含んでいません。")

    prev = None
    for r in plan.ranges:
        if r["patternId"] not in plan.patterns:
            raise RuntimeError(f"区間 {r['name']} のパターンが存在しません。")
        if r["end"] < r["start"]:
            raise RuntimeError(f"区間 {r['name']} の終了フレームが開始より前です。")
        if prev is not None and r["start"] <= prev["end"]:
            # 重なっていると compose で後の overlay が無警告に上書きする
            raise RuntimeError(
                f"区間 {prev['name']} と {r['name']} が重複しています "
                f"({r['start']} ≦ {prev['end']})。"
            )
        prev = r

    if not plan.source or not plan.source.is_file():
        raise RuntimeError(
            f"元動画が見つかりません: {plan.source}\n"
            "JSON の source を直すか、bat に元動画を一緒にドロップしてください。"
        )
    return plan


def check_source(plan: Plan) -> Fraction:
    """元動画が合成の前提を満たしているか確かめ、実 fps を返す。"""
    st = probe_source(plan.source)
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if plan.media_w and (w, h) != (plan.media_w, plan.media_h):
        raise RuntimeError(
            f"元動画の解像度 {w}x{h} が JSON の {plan.media_w}x{plan.media_h} と違います。"
            "マスクツールで見ていた動画と別物ではありませんか。"
        )

    def _frac(v):
        num, _, den = str(v or "0/1").partition("/")
        try:
            return Fraction(int(num), int(den or 1))
        except (ValueError, ZeroDivisionError):
            return Fraction(0)

    r_fps, avg_fps = _frac(st.get("r_frame_rate")), _frac(st.get("avg_frame_rate"))
    if r_fps <= 0:
        raise RuntimeError("元動画のフレームレートを取得できませんでした。")
    if avg_fps > 0 and r_fps != avg_fps:
        # VFR だと between(n,..) のフレーム数え上げと overlay の PTS 照合が発散する
        raise RuntimeError(
            f"元動画が可変フレームレートです (r={r_fps}, avg={avg_fps})。"
            "video\\fix_duration.bat で固定フレームレートに直してから処理してください。"
        )
    start_pts = int(st.get("start_pts") or 0)
    if start_pts != 0:
        raise RuntimeError(
            f"元動画の映像 start_pts が 0 ではありません ({start_pts})。"
            "フレーム番号と時刻がずれるため、video\\fix_duration.bat で直してください。"
        )
    if plan.fps and abs(float(r_fps) - plan.fps) > 0.01:
        print(f"[WARN] JSON の fps {plan.fps} と元動画の {float(r_fps):.3f} が違います。"
              "マスクツールで指定したフレーム番号自体がずれている可能性があります。")

    # 区間が動画の外に出ていないか。ここで止めないと、何時間も切り出した末に
    # 「N 枚のはずが N-1 枚でした」で落ちることになる
    nb = int(st.get("nb_frames") or 0)
    if nb <= 0:
        nb = count_frames(plan.source)
    last = plan.ranges[-1]["end"]
    if last > nb - 1:
        raise RuntimeError(
            f"区間 {plan.ranges[-1]['name']} の終了フレーム {last} が"
            f"元動画の最終フレーム {nb - 1} を超えています。\n"
            f"元動画は {nb} フレームですが、フレーム番号は 0 から数えるので"
            f"最後のフレームは {nb - 1} です。\n"
            f"マスクツールで終了フレームを {nb - 1} にしてください"
            f"（総フレーム数をそのまま入れると 1 つ多くなります）。"
        )
    if plan.total_frames and plan.total_frames != nb:
        print(f"[WARN] JSON の総フレーム数 {plan.total_frames} と"
              f" 元動画の {nb} が違います。")
    return r_fps


# ---- extract -------------------------------------------------------------

def do_extract(plan: Plan, args) -> int:
    fps = check_source(plan)
    failed = 0

    print("=" * 58)
    print("マスク PNG")
    for p in plan.patterns.values():
        c, rect = p["crop"], p["rect"]
        dst = plan.mask_path(p)
        write_mask_png(dst, c["w"], c["h"],
                       (rect["x"] - c["x"], rect["y"] - c["y"], rect["w"], rect["h"]))
        print(f"  {dst.name}  {c['w']}x{c['h']} / 白 {rect['w']}x{rect['h']} "
              f"@ {rect['x'] - c['x']},{rect['y'] - c['y']}")

    print("=" * 58)
    print("連番 PNG")
    for r in plan.ranges:
        p = plan.pattern_of(r)
        c = p["crop"]
        n = r["end"] - r["start"] + 1
        dst_dir = plan.frames_dir(r)

        if not args.force and dst_dir.is_dir() and len(frame_files(dst_dir)) == n:
            print(f"[SKIP] {r['name']}: 既に {n} 枚あります")
            continue

        if dst_dir.is_dir():
            shutil.rmtree(dst_dir)
        dst_dir.mkdir(parents=True, exist_ok=True)

        # 入力側シーク。select フィルタは E で復号を止めないので数時間の素材で使えない。
        # 半フレーム手前に置くのは segment_tool.py の cut と同じ流儀
        ss = max(0.0, (r["start"] - 0.5) / float(fps))
        try:
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
                "-ss", f"{ss:.6f}",
                "-i", str(plan.source),
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", f"crop={c['w']}:{c['h']}:{c['x']}:{c['y']}",
                "-frames:v", str(n),
                "-fps_mode", "passthrough",
                "-pix_fmt", EXTRACT_PIX_FMT,
                "-start_number", "0",
                str(dst_dir / "%08d.png"),
            ])
        except RuntimeError as exc:
            print(f"[ERROR] {r['name']}: {exc}", file=sys.stderr)
            failed += 1
            continue

        got = len(frame_files(dst_dir))
        if got != n:
            print(f"[ERROR] {r['name']}: {n} 枚のはずが {got} 枚でした", file=sys.stderr)
            failed += 1
            continue
        w, h = png_size(dst_dir / "00000000.png")
        if (w, h) != (c["w"], c["h"]):
            print(f"[ERROR] {r['name']}: PNG が {w}x{h} で crop {c['w']}x{c['h']} と違います",
                  file=sys.stderr)
            failed += 1
            continue
        if not args.skip_verify:
            # 半フレーム手前シークが目的のフレームに着いたか、
            # フレーム中央を狙った別のシークと突き合わせて確かめる
            want = first_frame_hash(plan.source, r["start"] / float(fps), c)
            got_hash = first_frame_hash(dst_dir / "00000000.png")
            if want != got_hash:
                print(f"[ERROR] {r['name']}: 先頭フレームが frame {r['start']} と一致しません。"
                      "シーク位置がずれています。", file=sys.stderr)
                failed += 1
                continue
        print(f"[OK] {r['name']}: {got} 枚 ({c['w']}x{c['h']}) -> {dst_dir}")

    print("=" * 58)
    if failed:
        print(f"[ERROR] {failed} 区間で失敗しました。", file=sys.stderr)
        return 1
    print(f"完了しました。次は propainter_run.bat です。作業フォルダ: {plan.work}")
    return 0


# ---- run -----------------------------------------------------------------

def make_ascii_link(target: Path) -> Path | None:
    """target を指す ASCII のディレクトリジャンクションを作る。

    ProPainter は画像を cv2.imread で読むが、Windows の OpenCV は ANSI の
    ファイル API を使うため、非 ASCII のパスを開けない (パスが文字化けして
    「can't open/read file」になる)。フレームを ASCII の場所へコピーすると
    数十 GB の複製になるので、リンクだけ作って渡す。
    """
    if not target.is_dir():
        return None
    for base in (Path(tempfile.gettempdir()), Path(target.anchor)):
        if not str(base).isascii() or not base.is_dir():
            continue
        link = base / f"propainter_{uuid.uuid4().hex[:8]}"
        # ジャンクションは管理者権限もデベロッパーモードも要らない。
        # _winapi は Unicode のまま渡せるので cmd のコードページを経由しない
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        except Exception:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if link.is_dir():
            return link
        # mklink はリンク先が無くてもリンクだけ作ってしまうので、
        # 使えないものを残さず消す
        if link.exists() or os.path.lexists(link):
            try:
                os.rmdir(link)
            except OSError:
                pass
    return None


def remove_ascii_link(link: Path) -> None:
    """ジャンクションだけを消す (rmdir はリンク先の中身に触らない)。"""
    try:
        os.rmdir(link)
    except OSError as exc:
        print(f"[WARN] 作業リンクを削除できませんでした: {link} ({exc})\n"
              "       中身は消えていません。手で rmdir してください。")


def find_result_frames(plan: Plan, r: dict) -> Path | None:
    """ProPainter の出力フレームフォルダを探す (バージョンで階層が違う)。"""
    base = plan.results_dir()
    if not base.is_dir():
        return None
    for cand in (base / r["slug"] / "frames", base / r["slug"]):
        if cand.is_dir() and frame_files(cand):
            return cand
    for cand in sorted(base.glob(f"{r['slug']}/**/")):
        if frame_files(cand):
            return cand
    return None


def do_run(plan: Plan, args) -> int:
    if not plan.pp_python:
        raise RuntimeError("JSON に propainter.python がありません。マスクツールで入力してください。")
    if not plan.pp_dir.is_dir():
        raise RuntimeError(f"ProPainter フォルダが見つかりません: {plan.pp_dir}")
    script = plan.pp_dir / "inference_propainter.py"
    if not script.is_file():
        raise RuntimeError(f"inference_propainter.py が見つかりません: {script}")
    if not args.dry_run and not Path(plan.pp_python).is_file():
        raise RuntimeError(f"python が見つかりません: {plan.pp_python}")

    # ProPainter に渡すパスが非 ASCII だと cv2.imread が開けないので、
    # ASCII のジャンクションを一時的に作ってそちら越しに渡す
    link = None
    pp_work = plan.work
    if not str(plan.work).isascii():
        link = make_ascii_link(plan.work)
        if link is None:
            raise RuntimeError(
                f"作業フォルダのパスに ASCII 以外の文字が含まれています:\n  {plan.work}\n"
                "ProPainter は OpenCV で画像を読むため、Windows では非 ASCII のパスを"
                "開けません (パスが文字化けして can't open/read file になります)。\n"
                "回避用の ASCII リンクも作れませんでした。propainter_*.bat の設定で\n"
                'WORKDIR を ASCII のパス (例: set "WORKDIR=C:\\pp_work") にして'
                "切り出しからやり直してください。"
            )
        pp_work = link
        print(f"[NOTE] 作業フォルダのパスに ASCII 以外の文字があるため、"
              f"ProPainter には ASCII のリンク越しに渡します:\n"
              f"       {link} -> {plan.work}")

    try:
        return _run_ranges(plan, args, pp_work)
    finally:
        if link is not None:
            remove_ascii_link(link)


def _run_ranges(plan: Plan, args, pp_work: Path) -> int:
    script = plan.pp_dir / "inference_propainter.py"
    failed = 0
    for r in plan.ranges:
        p = plan.pattern_of(r)
        n = r["end"] - r["start"] + 1
        src_dir, mask = plan.frames_dir(r), plan.mask_path(p)
        # ProPainter へ渡す側のパス (pp_work が実体か ASCII リンクか)
        pp_frames = pp_work / "frames" / r["slug"]
        pp_mask = pp_work / "masks" / f"{p['slug']}_crop_mask.png"
        pp_out = pp_work / "results"

        print("=" * 58)
        print(f"{r['name']}  frame {r['start']}-{r['end']}  ({p['name']}, {n} 枚)")
        if not args.dry_run:
            if not src_dir.is_dir() or len(frame_files(src_dir)) != n:
                print(f"[ERROR] 連番 PNG が揃っていません: {src_dir}"
                      " (先に propainter_extract.bat を実行してください)", file=sys.stderr)
                failed += 1
                continue
            if not mask.is_file():
                print(f"[ERROR] マスクがありません: {mask}", file=sys.stderr)
                failed += 1
                continue
            if not args.force:
                done = find_result_frames(plan, r)
                if done and len(frame_files(done)) == n:
                    print(f"[SKIP] 既に処理済みです: {done}")
                    continue
        if n > args.max_range_frames:
            # inference_propainter.py は read_frame_from_videos で全フレームを
            # 先にメモリへ読み込むので、区間長がそのまま必要メモリになる
            c = p["crop"]
            gb = n * c["w"] * c["h"] * 3 / (1024 ** 3)
            print(f"[WARN] {n} フレームは長すぎます (--max-range-frames {args.max_range_frames})。"
                  f"\n       ProPainter は全フレームを先にメモリへ読み込むため、"
                  f"読み込みだけで約 {gb:.1f} GB 必要です (さらに処理用のメモリが要ります)。"
                  f"\n       マスクツールで区間を {args.max_range_frames} フレーム程度に"
                  "分割してから切り出し直してください。")

        cmd = [plan.pp_python, str(script),
               "--video", str(pp_frames),
               "--mask", str(pp_mask),
               "--output", str(pp_out),
               *plan.pp_args]
        if args.dry_run:
            print("+", subprocess.list2cmdline(cmd), f"   (cwd={plan.pp_dir})")
            continue
        try:
            # 重みを相対パスで読むので ProPainter のフォルダで動かす
            run(cmd, cwd=plan.pp_dir)
        except RuntimeError as exc:
            print(f"[ERROR] {r['name']}: {exc}", file=sys.stderr)
            failed += 1
            continue

        out_dir = find_result_frames(plan, r)
        if out_dir is None:
            tree = "\n".join(f"    {q}" for q in sorted(plan.results_dir().rglob("*"))[:40])
            print(f"[ERROR] {r['name']}: 出力フレームが見つかりません。"
                  f"--save_frames は付いていますか。\n{tree}", file=sys.stderr)
            failed += 1
            continue
        got = len(frame_files(out_dir))
        if got != n:
            print(f"[ERROR] {r['name']}: 出力が {got} 枚で {n} 枚と違います", file=sys.stderr)
            failed += 1
            continue
        print(f"[OK] {r['name']}: {got} 枚 -> {out_dir}")

    print("=" * 58)
    if failed:
        print(f"[ERROR] {failed} 区間で失敗しました。", file=sys.stderr)
        return 1
    print("完了しました。次は propainter_compose.bat です。")
    return 0


# ---- compose -------------------------------------------------------------

def patch_box(p: dict, margin: int) -> tuple[int, int, int, int]:
    """貼り戻す矩形を crop 内の絶対座標で返す (x, y, w, h)。

    ProPainter はマスク外を元画素のまま返すので、crop 全体を貼ると変わっていない
    画素まで 8bit RGB を往復して劣化する。マスク矩形 + マージンだけ貼る。
    overlay の X 偶数丸め対策で、原点は偶数・幅も偶数に揃える。
    """
    c, rect = p["crop"], p["rect"]
    x0 = max(c["x"], rect["x"] - margin)
    y0 = max(c["y"], rect["y"] - margin)
    x1 = min(c["x"] + c["w"], rect["x"] + rect["w"] + margin)
    y1 = min(c["y"] + c["h"], rect["y"] + rect["h"] + margin)
    x0 -= x0 % 2
    y0 -= y0 % 2
    if x0 < c["x"]:
        x0 = c["x"]
    if y0 < c["y"]:
        y0 = c["y"]
    w, h = x1 - x0, y1 - y0
    w -= w % 2
    h -= h % 2
    return x0, y0, max(2, w), max(2, h)


def build_compose(plan: Plan, args, fps: Fraction,
                  limit: tuple[int, int] | None = None, tail: str = ""):
    """compose の ffmpeg コマンドを組み立てる。limit があればその区間だけ使う。

    tail は出力直前に足すフィルタ (preflight の showinfo 用)。-filter_complex と
    -vf は併用できないので、グラフの中に入れる必要がある。
    """
    ranges = plan.ranges
    if limit is not None:
        lo, hi = limit
        ranges = [r for r in ranges if r["end"] >= lo and r["start"] <= hi]

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    if limit is not None:
        cmd += ["-ss", f"{limit[0] / float(fps):.6f}"]
    cmd += ["-i", str(plan.source)]

    # settb=1/1 でフレーム番号タイムベースにする。これで enable の n と
    # framesync の PTS 照合が構造的に一致し、setpts の切り捨ても効かなくなる
    parts = ["[0:v:0]settb=1/1,setpts=N[m]"]
    label = "m"
    for i, r in enumerate(ranges, start=1):
        p = plan.pattern_of(r)
        c = p["crop"]
        out_dir = find_result_frames(plan, r)
        if out_dir is None:
            raise RuntimeError(
                f"区間 {r['name']} の ProPainter 出力が見つかりません。"
                "先に propainter_run.bat を実行してください。"
            )
        got = len(frame_files(out_dir))
        want = r["end"] - r["start"] + 1
        if got != want:
            raise RuntimeError(
                f"区間 {r['name']} の出力が {got} 枚で、区間長 {want} と違います。"
                "足りないと末尾に元のロゴが残ります。"
            )
        pw, ph = png_size(frame_files(out_dir)[0])
        if (pw, ph) != (c["w"], c["h"]):
            raise RuntimeError(
                f"区間 {r['name']} の出力が {pw}x{ph} で crop {c['w']}x{c['h']} と違います。"
            )

        cmd += ["-framerate", f"{fps.numerator}/{fps.denominator}",
                "-start_number", "0", "-i", frame_pattern(out_dir)]

        start = r["start"] - (limit[0] if limit else 0)
        end = r["end"] - (limit[0] if limit else 0)
        if args.patch == "mask":
            ax, ay, aw, ah = patch_box(p, args.patch_margin)
            crop_expr = f"crop={aw}:{ah}:{ax - c['x']}:{ay - c['y']},"
        else:
            ax, ay = c["x"], c["y"]
            crop_expr = ""
        nxt = "out" if i == len(ranges) else f"t{i}"
        parts.append(f"[{i}:v]{crop_expr}settb=1/1,setpts=N+{start}[p{i}]")
        parts.append(
            f"[{label}][p{i}]overlay={ax}:{ay}:format={args.overlay_format}"
            f":eof_action=pass:enable='between(n,{start},{end})'[{nxt}]"
        )
        label = nxt

    if label == "m":
        raise RuntimeError("合成対象の区間がありません。")
    # 最後にフレーム番号から正確な CFR タイムスタンプを組み直す
    parts.append(f"[{label}]settb={fps.denominator}/{fps.numerator},setpts=N{tail}[out2]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[out2]"]
    return cmd, ranges


def do_compose(plan: Plan, args) -> int:
    fps = check_source(plan)

    if args.preflight:
        # パッチが乗る前と乗った後の両方が見える境界を選ぶ
        r = next((x for x in plan.ranges if x["start"] >= 50), plan.ranges[0])
        lo = max(0, r["start"] - 50)
        hi = r["start"] + 50
        print("=" * 58)
        print(f"[preflight] {r['name']} の開始 frame {r['start']} 付近 ({lo}-{hi}) を"
              " -f null で流します（エンコードはしません）")
        cmd, _ = build_compose(plan, args, fps, limit=(lo, hi), tail=",showinfo")
        # showinfo は info レベルで出るので loglevel を上げる
        cmd[cmd.index("-loglevel") + 1] = "info"
        cmd += ["-frames:v", str(hi - lo + 1), "-f", "null", "-"]
        run(cmd)
        print("[OK] preflight 完了。showinfo の fmt が "
              f"{args.overlay_format} 系のままか、pts が 1 ずつ増えているか確認してください。")
        return 0

    out = args.output
    if out is None:
        out = plan.source.with_name(f"{plan.source.stem}_inpainted{plan.source.suffix}")
    out = Path(out).resolve()
    if out == plan.source:
        raise RuntimeError("出力先が元動画と同じです。")

    cmd, ranges = build_compose(plan, args, fps)
    # -r が無いと最終フレームの尺が 0 になり、尺が 1 フレーム分短くなって
    # avg_frame_rate も狂う (後段が可変フレームレートと誤認する)
    cmd += ["-map", "0:a?", "-r", f"{fps.numerator}/{fps.denominator}",
            *args.vcodec, "-c:a", "copy", str(out)]

    print("=" * 58)
    print(f"合成: {len(ranges)} 区間 -> {out}")
    run(cmd)

    total = count_frames(out)
    src_total = count_frames(plan.source)
    print("=" * 58)
    if total != src_total:
        print(f"[WARN] フレーム数が元動画と違います (出力 {total} / 元 {src_total})")
    else:
        print(f"[OK] {total} フレーム。元動画と一致しています。")
    st = probe_source(out)
    if st.get("r_frame_rate") != st.get("avg_frame_rate"):
        print(f"[WARN] 出力の r_frame_rate {st.get('r_frame_rate')} と "
              f"avg_frame_rate {st.get('avg_frame_rate')} が違います。"
              "video\\fix_duration.bat を通してください。")
    print(f"出力: {out}")
    print("音声以外 (字幕・チャプター) を戻すときは video\\merge_tracks.bat を使ってください。")
    return 0


# ---- main ----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="mask_plan.json から ProPainter 処理を回す")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--plan", type=Path, required=True, help="mask_plan.json")
        p.add_argument("--source", type=Path, help="元動画 (既定: JSON の source)")
        p.add_argument("--work-dir", type=Path, help="作業フォルダ (既定: <JSON>_work)")

    pe = sub.add_parser("extract", help="マスク PNG と区間ごとの連番 PNG を作る")
    common(pe)
    pe.add_argument("--force", action="store_true", help="既存の連番 PNG があっても作り直す")
    pe.add_argument("--skip-verify", action="store_true", help="先頭フレームの照合を省く")

    pr = sub.add_parser("run", help="区間ごとに ProPainter を走らせる")
    common(pr)
    pr.add_argument("--force", action="store_true", help="処理済みでも作り直す")
    pr.add_argument("--dry-run", action="store_true", help="コマンドを表示するだけ")
    pr.add_argument("--max-range-frames", type=int, default=1000,
                    help="これを超える区間長で警告する (既定: 1000)")

    pc = sub.add_parser("compose", help="結果を元動画に1パスで合成する")
    common(pc)
    pc.add_argument("--output", type=Path, help="出力ファイル (既定: <元動画>_inpainted.<拡張子>)")
    pc.add_argument("--patch", choices=("mask", "crop"), default="mask",
                    help="貼り戻す範囲。mask=マスク矩形+マージン / crop=crop 全体 (既定: mask)")
    pc.add_argument("--patch-margin", type=int, default=DEFAULT_PATCH_MARGIN,
                    help=f"mask のときのマージン px (既定: {DEFAULT_PATCH_MARGIN})")
    pc.add_argument("--overlay-format", default=DEFAULT_OVERLAY_FORMAT,
                    help=f"overlay の format。省くと 8bit 4:2:0 に落ちる (既定: {DEFAULT_OVERLAY_FORMAT})")
    pc.add_argument("--vcodec", default=" ".join(DEFAULT_VCODEC),
                    help="出力の映像エンコード設定")
    pc.add_argument("--preflight", action="store_true",
                    help="最初の区間境界の前後だけを -f null で流して確認する")

    args = ap.parse_args()
    if args.command == "compose":
        args.vcodec = args.vcodec.split()

    _safe_console()
    try:
        require_tools()
        plan = load_plan(args)
        if args.command == "extract":
            return do_extract(plan, args)
        if args.command == "run":
            return do_run(plan, args)
        return do_compose(plan, args)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
