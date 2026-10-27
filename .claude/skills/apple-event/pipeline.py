#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["numpy", "pillow", "scipy"]
# ///
"""
Apple Event まとめ記事パイプライン（/apple-event スキル用）

プロジェクトルートで実行する:
    uv run .claude/skills/apple-event/pipeline.py fetch <YouTube URL>
        字幕＋info.json を取得し、製品チャプターごとの字幕を書き出す
    uv run .claude/skills/apple-event/pipeline.py slides [--debug]
        240p SDR 映像を解析してカード状まとめスライドを検出し、
        1位候補の 1080p SDR 静止画と確認シート（inbox/review_sheet.jpg）を作る
    uv run .claude/skills/apple-event/pipeline.py pick <slug> c2|<秒>|none
        画像を別候補・指定秒の静止画に差し替える（none で画像なし）
    uv run .claude/skills/apple-event/pipeline.py sheet <slug> [start end]
        低解像度映像から一覧シートを作る（手動で探すとき用）
    uv run .claude/skills/apple-event/pipeline.py place <画像ファイル名> <見出し>
        画像を記事パーツの `## 見出し`（前方一致）の直後に置く（1チャプターに複数製品があるとき）
    uv run .claude/skills/apple-event/pipeline.py assemble <YYYY-MM-DD-slug>
        inbox/article_parts/ を連結して content/posts/<dir>/index.md を作る
    uv run .claude/skills/apple-event/pipeline.py clean
        inbox の中間ファイルを削除する

stdout には結果 JSON だけを出し、進捗と所要時間は stderr に出す。
"""

import concurrent.futures
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INBOX = PROJECT_ROOT / "inbox"
INFO_JSON = INBOX / "source.info.json"
MANIFEST = INBOX / "manifest.json"
SLIDES_JSON = INBOX / "slides.json"
TRANSCRIPTS = INBOX / "chapter_transcripts"
PARTS = INBOX / "article_parts"
IMAGES = INBOX / "images"
CANDIDATES = INBOX / "candidates"
SHEETS = INBOX / "sheets"
DEBUG_DIR = INBOX / "debug"
REVIEW_SHEET = INBOX / "review_sheet.jpg"
POSTS = PROJECT_ROOT / "content" / "posts"

# yt-dlp の既定は HDR 優先。HDR(PQ) をトーンマップせず JPEG にすると白っぽく褪せるため、必ず SDR を選ぶ。
LOWRES_FORMAT = (
    "bv[height<=240][dynamic_range=SDR][protocol=https][vcodec^=avc1]"
    "/bv[height<=240][dynamic_range=SDR][protocol=https]"
    "/bv[height<=360][dynamic_range=SDR]"
)
SKIP_PATTERN = re.compile(r"\b(introduction|intro|opening|conclusion|outro|closing|credits)\b")
JST = timezone(timedelta(hours=9))

# スライド検出の解析解像度とパラメータ
AW, AH = 320, 180
RING = 4                # 背景色を推定する外周の幅(px)
TOL = 10                # 背景色とみなす最大差（RGB 各チャンネル）
RING_MIN = 0.45         # 外周一致率の下限（全カードが余白付きで揃っている。余白が細いスライドは 0.5 前後）
BG_MIN, BG_MAX = 0.04, 0.45
MIN_CARDS = 5
COV_MIN = 0.5
CARD_MIN_AREA = AW * AH * 0.003
CARD_MIN_FILL = 0.7
TAIL = 15               # チャプター終端の後ろも探索する秒数
TOP_K = 3

T0 = time.monotonic()


def log(msg):
    print(f"[{time.monotonic() - T0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def fail(msg):
    print(f"[error] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def rel(path):
    try:
        return str(Path(path).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def run(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-6:])
        raise RuntimeError(f"{Path(cmd[0]).name} 失敗 (code={r.returncode}): {tail[:1500]}")
    return r


def slugify(text, sep="_"):
    return re.sub(r"[^a-z0-9]+", sep, text.lower()).strip(sep) or "chapter"


def fmt_ts(t):
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def load_json(path, what):
    if not path.exists():
        fail(f"{rel(path)} がありません。先に {what} を実行してください")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- fetch


def parse_vtt(vtt_path):
    """VTT を [(秒, テキスト), ...] にする。

    YouTube 自動字幕の「前の行を繰り返すロールアップ形式」と HTML タグを取り除く。
    """
    entries = []
    recent = []
    content = vtt_path.read_text(encoding="utf-8")
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip() for line in block.strip().splitlines()]
        ts_idx = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_idx is None:
            continue
        parts = lines[ts_idx].split("-->")[0].strip().split()[0].replace(",", ".").split(":")
        try:
            seconds = sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))
        except ValueError:
            continue
        new = []
        for line in lines[ts_idx + 1:]:
            text = re.sub(r"<[^>]+>", "", line)
            text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&").replace("&nbsp;", " ")
            text = re.sub(r"\[[^\]]*\]", "", text).replace("♪", "").strip()
            if not text or text in recent:
                continue
            recent = (recent + [text])[-4:]
            new.append(text)
        if new:
            entries.append((seconds, " ".join(new)))
    return entries


def load_chapters(info):
    duration = float(info.get("duration") or 0)
    raw = info.get("chapters") or [{"title": info.get("title", "Full Event"), "start_time": 0, "end_time": duration}]
    chapters = []
    for i, ch in enumerate(raw):
        end = ch.get("end_time")
        if end is None:
            end = raw[i + 1]["start_time"] if i + 1 < len(raw) else duration
        chapters.append({"title": ch["title"].strip(), "start": float(ch["start_time"]), "end": float(end)})
    return chapters


def event_slug(title):
    base = re.split(r"[:|｜]", title)[0]
    tokens = [t for t in re.findall(r"[a-z0-9]+", base.lower()) if not (t.isdigit() and len(t) <= 2)]
    return "-".join(tokens) or "apple-event"


def event_date(info):
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")
    d = info.get("upload_date")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "1970-01-01"


def cmd_fetch(url):
    INBOX.mkdir(exist_ok=True)
    for p in INBOX.glob("source.*"):
        p.unlink()
    for d in (TRANSCRIPTS, PARTS, CANDIDATES, SHEETS, DEBUG_DIR):
        shutil.rmtree(d, ignore_errors=True)
    for p in (MANIFEST, SLIDES_JSON, REVIEW_SHEET):
        p.unlink(missing_ok=True)

    log("yt-dlp: 字幕と info.json を取得")
    run([
        "yt-dlp", "--no-progress", "--skip-download", "--write-info-json",
        "--write-subs", "--write-auto-subs", "--sub-langs", "en", "--sub-format", "vtt",
        "-o", str(INBOX / "source.%(ext)s"), url,
    ])
    info = json.loads(INFO_JSON.read_text(encoding="utf-8"))
    vtt = next(INBOX.glob("source*.vtt"), None)
    if vtt is None:
        fail("英語字幕を取得できませんでした")
    entries = parse_vtt(vtt)
    subtitles = "manual" if "en" in (info.get("subtitles") or {}) else "auto"
    log(f"字幕 {len(entries)} 行（{subtitles}）")

    chapters = load_chapters(info)
    products = [ch for ch in chapters if not SKIP_PATTERN.search(ch["title"].lower())] or chapters
    others = [ch for ch in chapters if ch not in products]

    TRANSCRIPTS.mkdir(parents=True)
    PARTS.mkdir(parents=True)
    used = set()
    result = []
    for n, ch in enumerate(products, 1):
        slug = slugify(ch["title"])
        while slug in used:
            slug += "_2"
        used.add(slug)
        lines = [t for s, t in entries if ch["start"] <= s < ch["end"]]
        transcript = TRANSCRIPTS / f"{n:02d}_{slug}.txt"
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result.append({
            "n": n,
            "title": ch["title"],
            "slug": slug,
            "image_slug": slug.replace("_", "-"),
            "start": ch["start"],
            "end": ch["end"],
            "transcript": rel(transcript),
            "part": rel(PARTS / f"{n:02d}_{slug}.md"),
            "chars": sum(len(line) for line in lines),
        })

    intro = None
    intro_lines = [t for s, t in entries if any(o["start"] <= s < o["end"] for o in others)]
    if intro_lines:
        intro = TRANSCRIPTS / "00_intro.txt"
        intro.write_text("\n".join(intro_lines) + "\n", encoding="utf-8")

    date = event_date(info)
    manifest = {
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "date": date,
        "post_dir": f"{date}-{event_slug(info.get('title') or '')}",
        "duration": info.get("duration"),
        "subtitles": subtitles,
        "chapters": result,
        "intro_transcript": rel(intro) if intro else None,
        "skipped_chapters": [o["title"] for o in others],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log("完了")
    emit(manifest)


# ---------------------------------------------------------------- slides: 解析


def frame_features(rgb):
    """1フレーム（AH×AW×3, uint8）の「カード状まとめスライドらしさ」を測る。"""
    img = rgb.astype(np.int16)
    ring = np.concatenate([
        img[:RING].reshape(-1, 3), img[-RING:].reshape(-1, 3),
        img[RING:-RING, :RING].reshape(-1, 3), img[RING:-RING, -RING:].reshape(-1, 3),
    ])
    bg = np.median(ring, axis=0)
    ring_match = float((np.abs(ring - bg).max(axis=1) <= TOL).mean())
    bgmask = np.abs(img - bg).max(axis=2) <= TOL
    bg_frac = float(bgmask.mean())
    feat = {"ring": round(ring_match, 3), "bg": round(bg_frac, 3), "cards": 0, "cov": 0.0, "score": 0.0}
    if ring_match < RING_MIN * 0.75 or not (BG_MIN * 0.5 <= bg_frac <= BG_MAX * 1.3):
        return feat

    # 背景以外を1px収縮して細い橋渡しを切り、内側の穴（カード内の背景色部分）は埋めてから連結成分を取る
    fg = ndimage.binary_fill_holes(ndimage.binary_erosion(~bgmask))
    labels, n = ndimage.label(fg)
    if n == 0:
        return feat
    areas = np.bincount(labels.ravel())[1:]
    cards, cov = 0, 0
    for i, sl in enumerate(ndimage.find_objects(labels)):
        area = areas[i]
        if area < CARD_MIN_AREA:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if area / (h * w) >= CARD_MIN_FILL:
            cards += 1
            cov += area
    cov /= AW * AH
    feat["cards"], feat["cov"] = cards, round(cov, 3)
    if (ring_match >= RING_MIN and BG_MIN <= bg_frac <= BG_MAX and cards >= MIN_CARDS and cov >= COV_MIN):
        feat["score"] = round(cards * cov, 3)
    return feat


def decode_frames(video, start, duration, fps=1.0, size=(AW, AH)):
    """ローカル映像を rawvideo で読み、(秒, フレーム) を順に返す。"""
    w, h = size
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
         "-i", str(video), "-vf", f"fps={fps},scale={w}:{h}:flags=area",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    frame_bytes = w * h * 3
    k = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield start + k / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            k += 1
    finally:  # 途中で読むのをやめた場合も ffmpeg を残さない
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def analyze_chapter(video, ch, debug=False):
    start = ch["start"]
    end = ch["end"] + TAIL
    frames, thumbs = [], {}
    prev = None
    for t, rgb in decode_frames(video, start, end - start):
        feat = frame_features(rgb)
        thumb = rgb[::10, ::10].mean(axis=2)
        feat["motion"] = round(float(np.abs(thumb - prev).mean()), 2) if prev is not None else 99.0
        prev = thumb
        feat["t"] = round(t, 1)
        thumbs[feat["t"]] = thumb
        frames.append(feat)

    # 条件を満たすフレームの連続区間（2フレームまでの途切れは許容）を候補にする
    runs, cur, gap = [], [], 0
    for f in frames:
        if f["score"] > 0:
            cur.append(f)
            gap = 0
        elif cur:
            gap += 1
            if gap > 2:
                runs.append(cur)
                cur, gap = [], 0
    if cur:
        runs.append(cur)

    span = max(1.0, ch["end"] - ch["start"])
    candidates = []
    for r in runs:
        peak = max(f["score"] for f in r)
        good = [f for f in r if f["score"] >= peak * 0.95]
        still = [f for f in good if f["motion"] < 3] or good
        best = still[-1]  # 完成状態（アニメーションが終わった後）を優先して最も遅いフレーム
        pos = min(1.0, max(0.0, (best["t"] - ch["start"]) / span))
        rank = peak * (1 + 0.05 * min(len(r), 10)) * (1 + 0.15 * pos)
        candidates.append({**best, "rank": round(rank, 3), "run": [r[0]["t"], r[-1]["t"]]})
    candidates.sort(key=lambda c: -c["rank"])

    # 同じスライドの別の瞬間（組み上がり途中・切り替え演出で区間が分かれたもの）は1つにまとめる
    def same_slide(a, b):
        if abs(a["t"] - b["t"]) <= 20:
            return True
        return similarity(thumbs[a["t"]].astype(np.uint8), thumbs[b["t"]].astype(np.uint8)) > 0.9

    unique = []
    for c in candidates:
        if not any(same_slide(c, u) for u in unique):
            unique.append(c)
    candidates = unique
    low_confidence = not candidates
    if low_confidence:
        # 条件を満たすフレームが無い: 外周一致率×カード被覆で緩く順位付けする
        loose = sorted(frames, key=lambda f: -(f["ring"] * (f["cards"] + 1) * (f["cov"] + 0.05)))
        for f in loose:
            if all(abs(f["t"] - c["t"]) >= 20 for c in candidates):
                candidates.append({**f, "rank": 0.0, "run": [f["t"], f["t"]]})
            if len(candidates) >= TOP_K:
                break

    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        keys = ["t", "score", "ring", "bg", "cards", "cov", "motion"]
        lines = [",".join(keys)] + [",".join(str(f[k]) for k in keys) for f in frames]
        (DEBUG_DIR / f"{ch['slug']}.csv").write_text("\n".join(lines) + "\n")
    return {"slug": ch["slug"], "frames": len(frames), "low_confidence": low_confidence,
            "candidates": candidates[:TOP_K]}


def grab_lowres(video, t, size=(AW, AH)):
    """ローカル低解像度映像から t 秒のフレームを1枚取り出す。"""
    for _, rgb in decode_frames(video, max(0.0, t), 1.0, fps=1.0, size=size):
        return rgb.copy()
    return np.zeros((size[1], size[0], 3), np.uint8)


# ---------------------------------------------------------------- slides: 高解像度静止画


def sdr_format(info, max_height, prefer_avc=False):
    """info.json から max_height 以下で最も高画質な SDR(https) 映像フォーマットを選ぶ。"""
    fmts = [
        f for f in info.get("formats", [])
        if f.get("vcodec") not in (None, "none") and f.get("url")
        and f.get("dynamic_range") == "SDR" and f.get("protocol") == "https"
        and (f.get("height") or 0) <= max_height
    ]
    if not fmts:
        raise RuntimeError(f"info.json に {max_height}p 以下の SDR(https) 映像フォーマットがありません")
    return max(fmts, key=lambda f: ((f.get("height") or 0),
                                    prefer_avc and str(f.get("vcodec", "")).startswith("avc1"),
                                    f.get("tbr") or 0))


def hires_format(info):
    return sdr_format(info, 1080)


def refresh_format(manifest, format_id):
    log(f"URL を再取得します（format {format_id}）")
    r = run(["yt-dlp", "--no-progress", "-j", "-f", format_id, manifest["url"]], timeout=120)
    data = json.loads(r.stdout)
    if data.get("dynamic_range") not in (None, "SDR"):
        raise RuntimeError(f"format {format_id} が SDR ではありません: {data.get('dynamic_range')}")
    return {"format_id": format_id, "url": data["url"], "http_headers": data.get("http_headers") or {}}


def ffmpeg_remote(fmt, args, timeout=90):
    headers = "".join(f"{k}: {v}\r\n" for k, v in (fmt.get("http_headers") or {}).items())
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    if headers:
        cmd += ["-headers", headers]
    cmd += args[:args.index("-i") + 1] + [fmt["url"]] + args[args.index("-i") + 1:]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def similarity(a, b):
    """2枚の画像（PIL / ndarray）の類似度（縮小グレースケールの正規化相関）。"""
    def norm(x):
        im = x if isinstance(x, Image.Image) else Image.fromarray(x)
        v = np.asarray(im.convert("L").resize((160, 90), Image.BILINEAR), np.float32).ravel()
        v -= v.mean()
        return v / (np.linalg.norm(v) + 1e-6)
    return float(norm(a) @ norm(b))


class Hires:
    """1080p SDR の静止画取得。失敗時はリトライし、それでも駄目なら URL を取り直す。"""

    def __init__(self, manifest, info):
        self.manifest = manifest
        fmt = hires_format(info)
        self.fmt = {"format_id": fmt["format_id"], "url": fmt["url"], "http_headers": fmt.get("http_headers") or {}}
        self.refreshed = False

    def _try(self, args, out_check):
        for attempt in range(3):
            try:
                r = ffmpeg_remote(self.fmt, args)
                if r.returncode == 0 and out_check():
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1.5 * (attempt + 1))
        return False

    def run(self, args, out_check):
        if self._try(args, out_check):
            return True
        if not self.refreshed:
            self.refreshed = True
            self.fmt = refresh_format(self.manifest, self.fmt["format_id"])
            return self._try(args, out_check)
        return False

    def still(self, t, out, reference=None):
        """t 秒の静止画を out に保存する。reference（低解像度フレーム）と食い違えば前後3秒から一致するものを選ぶ。"""
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        ok = self.run(["-ss", f"{t:.2f}", "-i", "-frames:v", "1", "-q:v", "2", str(out)],
                      lambda: out.exists() and out.stat().st_size > 0)
        if not ok:
            raise RuntimeError(f"{fmt_ts(t)} の静止画取得に失敗しました")
        if reference is None:
            return t, None
        with Image.open(out) as im:
            score = similarity(im, reference)
        if score >= 0.9:
            return t, round(score, 3)
        burst = out.parent / f"{out.stem}_burst"
        shutil.rmtree(burst, ignore_errors=True)
        burst.mkdir()
        t0 = max(0.0, t - 3)
        self.run(["-ss", f"{t0:.2f}", "-i", "-t", "6", "-vf", "fps=2", "-q:v", "2", str(burst / "f_%02d.jpg")],
                 lambda: any(burst.glob("f_*.jpg")))
        best_t, best_score, best_path = t, score, None
        for p in sorted(burst.glob("f_*.jpg")):
            with Image.open(p) as im:
                s = similarity(im, reference)
            if s > best_score:
                best_t, best_score, best_path = t0 + (int(p.stem[2:]) - 1) / 2, s, p
        if best_path is not None:
            shutil.copy(best_path, out)
        shutil.rmtree(burst, ignore_errors=True)
        return best_t, round(best_score, 3)


def content_length(fmt):
    if fmt.get("filesize"):
        return int(fmt["filesize"])
    clen = urllib.parse.parse_qs(urllib.parse.urlparse(fmt["url"]).query).get("clen")
    if clen:
        return int(clen[0])
    req = urllib.request.Request(fmt["url"], headers={**(fmt.get("http_headers") or {}), "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers["Content-Range"].rsplit("/", 1)[1])


def download_parallel(fmt, dest, workers=16, chunk=1 << 20):
    """Range リクエストを並列に投げて1ファイルを取得する。

    yt-dlp の逐次チャンク取得はこの用途だと 1/8 程度の速度しか出ないため（53MB で約100秒 → 約13秒）。
    """
    size = content_length(fmt)
    headers = fmt.get("http_headers") or {}
    tmp = dest.with_name(dest.name + ".part")
    fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.ftruncate(fd, size)

        def get(offset):
            end = min(offset + chunk, size) - 1
            err = None
            for attempt in range(4):
                try:
                    req = urllib.request.Request(fmt["url"], headers={**headers, "Range": f"bytes={offset}-{end}"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = r.read()
                    if len(data) != end - offset + 1:
                        raise OSError(f"受信サイズ不一致 {len(data)}/{end - offset + 1}")
                    os.pwrite(fd, data, offset)
                    return
                except Exception as e:  # noqa: BLE001
                    err = e
                    time.sleep(1 + attempt)
            raise RuntimeError(f"bytes {offset}-{end} の取得に失敗: {err}")

        with concurrent.futures.ThreadPoolExecutor(workers) as ex:
            list(ex.map(get, range(0, size, chunk)))
    finally:
        os.close(fd)
    tmp.rename(dest)


def ensure_lowres(manifest):
    existing = [p for p in INBOX.glob("source.lowres.*") if not p.name.endswith((".part", ".ytdl"))]
    if existing:
        return existing[0]
    info = load_json(INFO_JSON, "fetch")
    try:
        fmt = sdr_format(info, 240, prefer_avc=True)
        log(f"240p SDR 映像を並列取得（format {fmt['format_id']}）")
        download_parallel(fmt, INBOX / f"source.lowres.{fmt.get('ext') or 'mp4'}")
    except Exception as e:  # noqa: BLE001  URL 期限切れなどは yt-dlp で取り直す
        log(f"並列取得に失敗したので yt-dlp で取り直します: {e}")
        for p in INBOX.glob("source.lowres.*"):
            p.unlink()
        run(["yt-dlp", "--no-progress", "-f", LOWRES_FORMAT, "--retries", "10",
             "-o", str(INBOX / "source.lowres.%(ext)s"), manifest["url"]], timeout=900)
    files = [p for p in INBOX.glob("source.lowres.*") if not p.name.endswith((".part", ".ytdl"))]
    if not files:
        fail("低解像度映像を取得できませんでした")
    probe = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height,codec_name,color_transfer", "-of", "json", str(files[0])])
    stream = json.loads(probe.stdout)["streams"][0]
    if stream.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        fail(f"低解像度映像が HDR です（{stream.get('color_transfer')}）")
    log(f"映像: {stream.get('codec_name')} {stream.get('width')}x{stream.get('height')}, "
        f"{files[0].stat().st_size / 1e6:.0f}MB")
    return files[0]


# ---------------------------------------------------------------- slides: 確認シート


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_review_sheet(products, video):
    big, small, pad, label_h = (480, 270), (320, 180), 10, 26
    width = pad + big[0] + pad + 2 * (small[0] + pad)
    row_h = label_h + big[1] + pad
    sheet = Image.new("RGB", (width, pad + row_h * len(products)), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    f_title, f_label = font(18), font(15)
    for i, p in enumerate(products):
        y = pad + i * row_h
        title = f"{p['n']:02d} {p['slug']}"
        if p["low_confidence"]:
            title += "   [LOW CONFIDENCE]"
        draw.text((pad, y), title, fill=(255, 120, 120) if p["low_confidence"] else (255, 255, 255), font=f_title)
        x = pad
        for k, c in enumerate(p["candidates"]):
            size = big if k == 0 else small
            if k == 0 and p.get("image"):
                with Image.open(PROJECT_ROOT / p["image"]) as im:
                    thumb = im.convert("RGB").resize(size, Image.LANCZOS)
            else:
                thumb = Image.fromarray(grab_lowres(video, c["t"])).resize(size, Image.LANCZOS)
            sheet.paste(thumb, (x, y + label_h))
            if k == 0:
                draw.rectangle([x - 2, y + label_h - 2, x + size[0] + 1, y + label_h + size[1] + 1],
                               outline=(80, 220, 120), width=2)
            text = f"c{k + 1} {fmt_ts(c['t'])} ({c['t']:.0f}s) cards={c['cards']}"
            draw.text((x + 4, y + label_h + size[1] - 20), text, fill=(255, 255, 0), font=f_label,
                      stroke_width=2, stroke_fill=(0, 0, 0))
            x += size[0] + pad
    sheet.save(REVIEW_SHEET, quality=85)


def cmd_slides(debug=False):
    manifest = load_json(MANIFEST, "fetch")
    info = load_json(INFO_JSON, "fetch")
    video = ensure_lowres(manifest)
    chapters = manifest["chapters"]

    log(f"解析: {len(chapters)} チャプター")
    workers = max(1, min(len(chapters), os.cpu_count() or 4))
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        analyses = list(ex.map(analyze_chapter, [video] * len(chapters), chapters, [debug] * len(chapters)))

    IMAGES.mkdir(parents=True, exist_ok=True)
    for p in IMAGES.iterdir():  # 前回の自動抽出画像（手動画像は残す）
        if re.fullmatch(r".+-overview(-\d+)?\.jpg", p.name):
            p.unlink()
    shutil.rmtree(CANDIDATES, ignore_errors=True)

    log("1080p SDR 静止画を取得")
    hires = Hires(manifest, info)
    products = []
    for ch, a in zip(chapters, analyses, strict=True):
        products.append({"n": ch["n"], "slug": ch["slug"], "title": ch["title"], "image_slug": ch["image_slug"],
                         "low_confidence": a["low_confidence"], "candidates": a["candidates"], "image": None})

    def fetch_still(p):
        if not p["candidates"]:
            return
        c = p["candidates"][0]
        dest = IMAGES / f"{p['image_slug']}-overview.jpg"
        t, match = hires.still(c["t"], dest, reference=grab_lowres(video, c["t"]))
        p.update(image=rel(dest), chosen="c1", t=t, match=match)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for p, fut in [(p, ex.submit(fetch_still, p)) for p in products]:
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001  1製品の失敗で全体を止めない
                p["error"] = str(e)
                log(f"[warn] {p['slug']}: {e}")

    build_review_sheet(products, video)
    SLIDES_JSON.write_text(json.dumps({"format_id": hires.fmt["format_id"], "products": products},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    log("完了")
    emit({
        "review_sheet": rel(REVIEW_SHEET),
        "products": [
            {"slug": p["slug"], "image": p["image"], "t": p.get("t"), "match": p.get("match"),
             "low_confidence": p["low_confidence"], "error": p.get("error"),
             "candidates": [{"c": f"c{k + 1}", "t": c["t"], "cards": c["cards"], "rank": c["rank"]}
                            for k, c in enumerate(p["candidates"])]}
            for p in products
        ],
    })


# ---------------------------------------------------------------- pick / sheet


def find_product(items, key):
    key_n = key.replace("-", "_")
    for item in items:
        if key_n in (item["slug"], f"{item['n']:02d}", str(item["n"])):
            return item
    matches = [item for item in items if item["slug"].startswith(key_n)]
    if len(matches) == 1:
        return matches[0]
    fail(f"製品 {key!r} が見つかりません（候補: {', '.join(i['slug'] for i in items)}）")


def parse_seconds(text):
    parts = text.split(":")
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def cmd_pick(key, choice, extra=False):
    manifest = load_json(MANIFEST, "fetch")
    slides = load_json(SLIDES_JSON, "slides")
    info = load_json(INFO_JSON, "fetch")
    p = find_product(slides["products"], key)
    dest = IMAGES / f"{p['image_slug']}-overview.jpg"
    if extra and choice == "none":
        fail("--extra と none は同時に指定できません")
    if extra:
        n = 2
        while (IMAGES / f"{p['image_slug']}-overview-{n}.jpg").exists():
            n += 1
        dest = IMAGES / f"{p['image_slug']}-overview-{n}.jpg"
    if choice == "none":
        dest.unlink(missing_ok=True)
        p.update(image=None, chosen=None, t=None, match=None)
        result = {"slug": p["slug"], "image": None}
    else:
        if re.fullmatch(r"c\d+", choice):
            k = int(choice[1:]) - 1
            if not 0 <= k < len(p["candidates"]):
                fail(f"{choice} はありません（候補は {len(p['candidates'])} 個）")
            t = p["candidates"][k]["t"]
        else:
            t = parse_seconds(choice)
        video = ensure_lowres(manifest)
        t, match = Hires(manifest, info).still(t, dest, reference=grab_lowres(video, t))
        result = {"slug": p["slug"], "image": rel(dest), "t": t, "match": match}
        if extra:
            p.setdefault("extras", []).append(result)
        else:
            p.update(image=rel(dest), chosen=choice, t=t, match=match)
    SLIDES_JSON.write_text(json.dumps(slides, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


def cmd_sheet(key, start=None, end=None):
    manifest = load_json(MANIFEST, "fetch")
    ch = find_product(manifest["chapters"], key)
    video = ensure_lowres(manifest)
    s = parse_seconds(start) if start else ch["start"]
    e = parse_seconds(end) if end else ch["end"] + TAIL
    step = max(2, math.ceil((e - s) / 120))
    tw, th, cols, label_h = 192, 108, 8, 18
    frames = list(decode_frames(video, s, e - s, fps=1 / step, size=(tw, th)))
    rows = math.ceil(len(frames) / cols)
    sheet = Image.new("RGB", (cols * tw, rows * (th + label_h)), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    f_label = font(13)
    for i, (t, rgb) in enumerate(frames):
        x, y = i % cols * tw, i // cols * (th + label_h)
        sheet.paste(Image.fromarray(rgb), (x, y))
        draw.text((x + 3, y + th + 1), f"{t:.0f}s ({fmt_ts(t)})", fill=(230, 230, 120), font=f_label)
    SHEETS.mkdir(exist_ok=True)
    out = SHEETS / f"{ch['slug']}.jpg"
    sheet.save(out, quality=85)
    emit({"sheet": rel(out), "start": s, "end": e, "step": step, "frames": len(frames)})


# ---------------------------------------------------------------- assemble / clean


def strip_rules(md, leading=True):
    lines = md.strip().splitlines()
    while lines and lines[-1].strip() == "---":
        lines = lines[:-1]
        while lines and not lines[-1].strip():
            lines.pop()
    while leading and lines and lines[0].strip() == "---":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def image_block(heading, image_name):
    return [f"![{heading} の機能まとめ]({image_name})", "*出典：Apple*"]


def insert_image(md, title, image_name):
    """最初の `## ` 見出しの直後に画像を入れる。キャプションはその見出しから作る。"""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return "\n".join(lines[:i + 1] + [""] + image_block(line[3:].strip(), image_name) + lines[i + 1:])
    return "\n".join([f"## {title}", ""] + image_block(title, image_name) + ["", md])


def split_sections(md):
    """パートを `## ` 見出しごとに分ける（1つのチャプターに複数製品がある場合）。"""
    blocks, cur = [], []
    for line in md.splitlines():
        if line.startswith("## ") and any(c.strip() for c in cur):
            blocks.append(strip_rules("\n".join(cur)))
            cur = []
        cur.append(line)
    if any(c.strip() for c in cur):
        blocks.append(strip_rules("\n".join(cur)))
    return [b for b in blocks if b]


def product_parts():
    return sorted(p for p in PARTS.glob("[0-9][0-9]_*.md") if p.name not in ("00_header.md", "99_summary.md"))


def cmd_place(image_name, heading):
    """画像を、指定した `## ` 見出し（前方一致）の直後に置く。別の場所に置いてあれば移動する。"""
    image_name = Path(image_name).name
    if not (IMAGES / image_name).exists():
        fail(f"inbox/images/{image_name} がありません")
    found = []
    for part in product_parts():
        for line in part.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                found.append((part, line[3:].strip()))
    exact = [f for f in found if f[1] == heading]
    prefix = [f for f in found if f[1].startswith(heading)]
    target = exact or prefix
    if len(target) != 1:
        fail(f"見出し {heading!r} を1つに特定できません（候補: {' / '.join(h for _, h in found)}）")
    target_part, target_heading = target[0]

    for part in product_parts():  # 既存の配置を消す（移動）
        lines, out, skip_credit = part.read_text(encoding="utf-8").splitlines(), [], False
        for line in lines:
            if line.startswith("![") and line.endswith(f"]({image_name})"):
                skip_credit = True
                continue
            if skip_credit and line.strip() == "*出典：Apple*":
                skip_credit = False
                continue
            skip_credit = False
            out.append(line)
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
        if part == target_part:
            lines = text.splitlines()
            i = next(k for k, line in enumerate(lines) if line.startswith("## ") and line[3:].strip() == target_heading)
            text = "\n".join(lines[:i + 1] + [""] + image_block(target_heading, image_name) + lines[i + 1:])
        part.write_text(text.rstrip() + "\n", encoding="utf-8")
    emit({"image": image_name, "part": rel(target_part), "heading": target_heading})


def cmd_assemble(post_dir, posts_root=None):
    manifest = load_json(MANIFEST, "fetch")
    header, summary = PARTS / "00_header.md", PARTS / "99_summary.md"
    parts = [PROJECT_ROOT / ch["part"] for ch in manifest["chapters"]]
    missing = [rel(p) for p in [header, *parts, summary] if not p.exists()]
    if missing:
        fail(f"記事パーツが足りません: {', '.join(missing)}")

    out_dir = Path(posts_root) / post_dir if posts_root else POSTS / post_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = [strip_rules(p.read_text(encoding="utf-8")) for p in parts]
    placed = "\n".join(texts)
    chunks = [strip_rules(header.read_text(encoding="utf-8"), leading=False)]
    for ch, md in zip(manifest["chapters"], texts, strict=True):
        image = IMAGES / f"{ch['image_slug']}-overview.jpg"
        if image.exists() and f"]({image.name})" not in placed:  # place 済みなら自動挿入しない
            md = insert_image(md, ch["title"], image.name)
        chunks.extend(split_sections(md))
    chunks.append(strip_rules(summary.read_text(encoding="utf-8")))
    index = out_dir / "index.md"
    index.write_text("\n\n---\n\n".join(chunks) + "\n", encoding="utf-8")

    copied = []
    if IMAGES.exists():
        for p in sorted(IMAGES.iterdir()):
            if p.is_file() and p.name != ".gitkeep" and not p.name.startswith("."):
                shutil.copy2(p, out_dir / p.name)
                copied.append(p.name)
    emit({"index": rel(index), "chars": len(index.read_text(encoding="utf-8")), "images": copied})


def cmd_clean():
    for pattern in ("source.*", "*.vtt", "*.json", "*.mhtml"):
        for p in INBOX.glob(pattern):
            p.unlink()
    for d in (TRANSCRIPTS, PARTS, CANDIDATES, SHEETS, DEBUG_DIR,
              INBOX / "storyboard_frames", INBOX / "chapter_grids", INBOX / "sections", INBOX / "section_frames"):
        shutil.rmtree(d, ignore_errors=True)
    REVIEW_SHEET.unlink(missing_ok=True)
    transcript = INBOX / "transcript.txt"
    if transcript.exists():
        transcript.write_text("")
    if IMAGES.exists():
        for p in IMAGES.iterdir():
            if p.name != ".gitkeep":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
    emit({"cleaned": rel(INBOX)})


# ---------------------------------------------------------------- main


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "fetch" and len(args) == 1:
            cmd_fetch(args[0])
        elif cmd == "slides":
            cmd_slides(debug="--debug" in args)
        elif cmd == "pick" and len([a for a in args if a != "--extra"]) == 2:
            cmd_pick(*[a for a in args if a != "--extra"], extra="--extra" in args)
        elif cmd == "sheet" and len(args) in (1, 3):
            cmd_sheet(*args)
        elif cmd == "place" and len(args) == 2:
            cmd_place(*args)
        elif cmd == "assemble" and len(args) in (1, 2):
            cmd_assemble(*args)
        elif cmd == "clean":
            cmd_clean()
        else:
            print(__doc__, file=sys.stderr)
            sys.exit(1)
    except RuntimeError as e:
        fail(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
