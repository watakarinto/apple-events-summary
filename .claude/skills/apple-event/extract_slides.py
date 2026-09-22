#!/usr/bin/env python3
"""
Apple Event まとめスライド抽出スクリプト

Usage:
    python3 extract_slides.py <YouTube URL>
        → storyboard取得・フレーム抽出・チャプターグリッド作成

    python3 extract_slides.py download <URL> slug:ts slug:ts ...
        → スカウトDL（360p以下 ±120s）→ グリッド作成
        → グリッドでモザイクフレーム番号を確認後 finalize を実行

    python3 extract_slides.py finalize <URL> slug:frame_num slug:frame_num ...
        → スカウトで特定したフレームを 1080p ±10s で本番DL・グリッド作成
"""

import sys
import json
import email
import io
import shutil
import subprocess
import concurrent.futures
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INBOX = PROJECT_ROOT / "inbox"


def download_storyboard(url):
    """ストーリーボードとinfo.jsonを取得する"""
    info_files = list(INBOX.glob("*.info.json"))
    if not info_files:
        print(
            "[error] inbox/*.info.json が見つかりません。先に yt-dlp --write-info-json を実行してください。",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(info_files[0]) as f:
        info = json.load(f)

    sb_path = INBOX / "storyboard.mhtml"
    if not sb_path.exists():
        result = subprocess.run(
            ["yt-dlp", "-f", "sb0", "-o", str(sb_path), url],
            check=False,
        )
        if result.returncode != 0:
            print(f"[error] yt-dlp storyboard 取得失敗 (code={result.returncode})", file=sys.stderr)
            sys.exit(1)
    else:
        print("[skip] storyboard.mhtml は既に存在します", file=sys.stderr)

    return info, sb_path


def extract_frames(sb_path, info):
    """ストーリーボードからフレームを抽出する"""
    try:
        from PIL import Image
    except ImportError:
        print("[error] Pillow が必要です: python3 -m pip install Pillow --break-system-packages", file=sys.stderr)
        sys.exit(1)

    duration = info.get("duration", 0)
    out_dir = INBOX / "storyboard_frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(sb_path, "rb") as f:
        msg = email.message_from_bytes(f.read())

    grids_data = [
        part.get_payload(decode=True)
        for part in msg.walk()
        if part.get_content_type() == "image/webp"
    ]

    if not grids_data:
        print("[error] ストーリーボードにWebP画像が見つかりませんでした", file=sys.stderr)
        sys.exit(1)

    sb_fmt = next(
        (f for f in info.get("formats", []) if f.get("format_id") == "sb0"),
        None,
    )
    if sb_fmt:
        cols = sb_fmt.get("columns", 5)
        rows = sb_fmt.get("rows", 5)
    else:
        cols, rows = 5, 5

    tiles_per_grid = cols * rows
    total_frames = len(grids_data) * tiles_per_grid
    interval = duration / total_frames if total_frames else 10.0

    frame_count = 0
    for grid_data in grids_data:
        grid_img = Image.open(io.BytesIO(grid_data))
        w, h = grid_img.size
        fw, fh = w // cols, h // rows
        for row in range(rows):
            for col in range(cols):
                frame = grid_img.crop((col * fw, row * fh, (col + 1) * fw, (row + 1) * fh))
                ts = int(frame_count * interval)
                frame.save(out_dir / f"frame_{frame_count:04d}_t{ts:05d}s.jpg")
                frame_count += 1

    print(f"[ok] {frame_count}フレーム抽出完了 (間隔: {interval:.1f}秒)", file=sys.stderr)
    return interval


def make_chapter_grids(info, interval):
    """各チャプターの末尾30%フレームをグリッド画像化する"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit(1)

    chapters = info.get("chapters", [])
    duration = info.get("duration", 0)

    if not chapters:
        print("[warn] チャプター情報がありません。動画全体を1チャプターとして扱います", file=sys.stderr)
        chapters = [{"title": "Full Event", "start_time": 0, "end_time": duration}]
    else:
        for i, ch in enumerate(chapters):
            if "end_time" not in ch:
                ch["end_time"] = chapters[i + 1]["start_time"] if i + 1 < len(chapters) else duration

    frames = sorted((INBOX / "storyboard_frames").glob("frame_*.jpg"))
    out_dir = INBOX / "chapter_grids"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = {}
    for ch in chapters:
        slug = ch["title"].lower().replace(" ", "_").replace("/", "_")
        start, end = ch["start_time"], ch["end_time"]

        ch_frames = [
            f for f in frames
            if start <= int(f.stem.split("_t")[1].replace("s", "")) <= end
        ]
        if not ch_frames:
            print(f"[warn] {ch['title']}: フレームなし", file=sys.stderr)
            continue

        n = len(ch_frames)
        start_idx = max(0, min(int(n * 0.7), n - 15))
        target = ch_frames[start_idx:]

        cols = 5
        tw, th, lh = 160, 90, 14
        rows_count = (len(target) + cols - 1) // cols
        grid = Image.new("RGB", (cols * tw, rows_count * (th + lh)), (20, 20, 20))

        for i, fpath in enumerate(target):
            col, row = i % cols, i // cols
            img = Image.open(fpath).resize((tw, th), Image.LANCZOS)
            x, y = col * tw, row * (th + lh)
            grid.paste(img, (x, y))
            ts = fpath.stem.split("_t")[1].replace("s", "")
            ImageDraw.Draw(grid).text((x + 1, y + th + 1), f"t{ts}", fill=(200, 200, 100))

        grid_path = str(out_dir / f"{slug}.jpg")
        grid.save(grid_path, quality=85)
        recommended_ts = max(int(start), int(end) - 90)
        result[slug] = {
            "title": ch["title"],
            "start_time": start,
            "end_time": end,
            "recommended_ts": recommended_ts,
            "grid_path": grid_path,
            "frame_interval": interval,
        }
        print(f"[ok] {ch['title']}: グリッド作成 ({len(target)}フレーム)", file=sys.stderr)

    return result


def download_sections_scout(url, chapter_timestamps):
    """スカウトフェーズ：360p以下・±120s で並列DL

    モザイクスライドのフレーム番号特定用。低解像度で広いウィンドウを取得する。
    メタデータ（ts_start）を section_frames/{slug}_scout.json に保存する。
    戻り値のキーは "{slug}_scout" 形式で、extract_section_grids がそのまま使える。
    """
    out_dir = INBOX / "sections"
    meta_dir = INBOX / "section_frames"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    def download(slug, ts):
        ts_start = max(0, ts - 120)
        ts_end = ts + 120
        out = str(out_dir / f"{slug}_scout.mp4")
        result = subprocess.run(
            [
                "yt-dlp",
                "--download-sections", f"*{ts_start}-{ts_end}",
                "-f", "bestvideo[height<=360]",
                "--concurrent-fragments", "4",
                "--force-overwrite",
                "-o", out,
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[error] {slug}: スカウトDL失敗 (code={result.returncode})\n{result.stderr}", file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, "yt-dlp")
        meta = {"slug": slug, "ts_center": ts, "ts_start": ts_start, "fps": 0.5}
        (meta_dir / f"{slug}_scout.json").write_text(json.dumps(meta, ensure_ascii=False))
        print(f"[ok] {slug}: スカウトDL完了 ({ts_start}s-{ts_end}s, 360p以下)", file=sys.stderr)
        return f"{slug}_scout", out

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(download, slug, ts): slug for slug, ts in chapter_timestamps.items()}
        results = {}
        for future in concurrent.futures.as_completed(futures):
            try:
                key, path = future.result()
                results[key] = path
            except Exception as e:
                slug = futures[future]
                print(f"[error] {slug}: {e}", file=sys.stderr)
    return results


def finalize_sections(url, slug_frame_nums):
    """本番フェーズ：スカウトで特定したフレームを 1080p ±10s でDL

    slug_frame_nums: {slug: frame_num} — スカウトグリッドのフレーム番号
    戻り値のキーは "{slug}" 形式。
    """
    out_dir = INBOX / "sections"
    meta_dir = INBOX / "section_frames"

    def finalize(slug, frame_num):
        meta_path = meta_dir / f"{slug}_scout.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"スカウトメタデータが見つかりません: {meta_path}\n先に download を実行してください"
            )
        meta = json.loads(meta_path.read_text())
        scout_fps = meta.get("fps", 1)
        exact_ts = int(meta["ts_start"] + (frame_num - 1) / scout_fps)
        ts_start = max(0, exact_ts - 10)
        ts_end = exact_ts + 10
        out = str(out_dir / f"{slug}.mp4")
        result = subprocess.run(
            [
                "yt-dlp",
                "--download-sections", f"*{ts_start}-{ts_end}",
                "-f", "bestvideo[ext=mp4][height<=1080]/bestvideo[height<=1080]",
                "--concurrent-fragments", "4",
                "--force-overwrite",
                "-o", out,
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[error] {slug}: 本番DL失敗 (code={result.returncode})\n{result.stderr}", file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, "yt-dlp")
        print(f"[ok] {slug}: 本番DL完了 ({ts_start}s-{ts_end}s, 1080p, exact_ts={exact_ts})", file=sys.stderr)
        return slug, out

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(finalize, slug, n): slug for slug, n in slug_frame_nums.items()}
        results = {}
        for future in concurrent.futures.as_completed(futures):
            try:
                slug, path = future.result()
                results[slug] = path
            except Exception as e:
                slug = futures[future]
                print(f"[error] {slug}: {e}", file=sys.stderr)
    return results


def extract_section_grids(section_paths, fps=1):
    """各セクション動画からフレームを抽出してグリッド画像を作る（並列ffmpeg）"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit(1)

    out_dir = INBOX / "section_frames"
    out_dir.mkdir(parents=True, exist_ok=True)

    def extract_frames(slug, mp4_path):
        frames_dir = out_dir / slug
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir()
        result = subprocess.run(
            [
                "ffmpeg", "-i", mp4_path,
                "-vf", f"fps={fps}",
                "-q:v", "1",
                str(frames_dir / "frame_%03d.jpg"),
                "-y",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return slug, result, frames_dir

    # ffmpeg を並列実行
    extracted = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(extract_frames, slug, path): slug for slug, path in section_paths.items()}
        for future in concurrent.futures.as_completed(futures):
            slug_key = futures[future]
            try:
                slug, result, frames_dir = future.result()
                if result.returncode != 0:
                    print(f"[error] {slug}: ffmpeg 失敗 (code={result.returncode})\n{result.stderr}", file=sys.stderr)
                else:
                    extracted[slug] = frames_dir
            except Exception as e:
                print(f"[error] {slug_key}: {e}", file=sys.stderr)

    # グリッド作成
    cols = 10
    tw, th, lh = 64, 36, 10
    grids = {}
    for slug, frames_dir in extracted.items():
        frame_files = sorted(frames_dir.glob("frame_*.jpg"))
        if not frame_files:
            print(f"[warn] {slug}: フレーム抽出結果なし", file=sys.stderr)
            continue

        rows_count = (len(frame_files) + cols - 1) // cols
        grid = Image.new("RGB", (cols * tw, rows_count * (th + lh)), (20, 20, 20))
        for i, fpath in enumerate(frame_files):
            col, row = i % cols, i // cols
            img = Image.open(fpath).resize((tw, th), Image.LANCZOS)
            x, y = col * tw, row * (th + lh)
            grid.paste(img, (x, y))
            ImageDraw.Draw(grid).text((x + 1, y + th + 1), str(i + 1), fill=(200, 200, 100))

        grid_path = str(out_dir / f"{slug}_grid.jpg")
        grid.save(grid_path, quality=85)
        grids[slug] = {
            "grid_path": grid_path,
            "frames_dir": str(frames_dir),
            "frame_files": [str(f) for f in frame_files],
        }
        _sz = Image.open(frame_files[0]).size
        print(f"[ok] {slug}: {len(frame_files)}フレーム ({_sz[0]}x{_sz[1]}) → グリッド作成", file=sys.stderr)

    return grids


def _parse_slug_int_args(args):
    result = {}
    for arg in args:
        parts = arg.split(":", 1)
        if len(parts) != 2:
            print(f"[error] 引数の形式が不正です: {arg!r}  (期待形式: slug:数値)", file=sys.stderr)
            sys.exit(1)
        slug, val = parts
        try:
            result[slug] = int(val)
        except ValueError:
            print(f"[error] 数値ではありません: {val!r}", file=sys.stderr)
            sys.exit(1)
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 extract_slides.py <URL>")
        print("    → ストーリーボード取得・フレーム抽出・チャプターグリッド作成")
        print("  python3 extract_slides.py download <URL> slug1:ts1 slug2:ts2 ...")
        print("    → スカウトDL (360p ±120s) → グリッド作成")
        print("  python3 extract_slides.py finalize <URL> slug1:frame_num1 slug2:frame_num2 ...")
        print("    → 本番DL (1080p ±10s) → グリッド作成")
        sys.exit(1)

    if sys.argv[1] == "download":
        if len(sys.argv) < 4:
            print("[error] download モードには URL と slug:ts の指定が必要です", file=sys.stderr)
            sys.exit(1)
        url = sys.argv[2]
        chapter_timestamps = _parse_slug_int_args(sys.argv[3:])
        section_paths = download_sections_scout(url, chapter_timestamps)
        section_grids = extract_section_grids(section_paths, fps=0.5)
        print(json.dumps(section_grids, ensure_ascii=False, indent=2))

    elif sys.argv[1] == "finalize":
        if len(sys.argv) < 4:
            print("[error] finalize モードには URL と slug:frame_num の指定が必要です", file=sys.stderr)
            sys.exit(1)
        url = sys.argv[2]
        slug_frame_nums = _parse_slug_int_args(sys.argv[3:])
        section_paths = finalize_sections(url, slug_frame_nums)
        section_grids = extract_section_grids(section_paths)
        print(json.dumps(section_grids, ensure_ascii=False, indent=2))

    else:
        url = sys.argv[1]
        info, sb_path = download_storyboard(url)
        interval = extract_frames(sb_path, info)
        chapter_data = make_chapter_grids(info, interval)
        print(json.dumps(chapter_data, ensure_ascii=False, indent=2))
