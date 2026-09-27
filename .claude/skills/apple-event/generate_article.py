#!/usr/bin/env python3
"""
Apple Event 字幕チャプター抽出スクリプト（API不要）

Usage:
    python3 generate_article.py

Reads:
    inbox/*.vtt           - 字幕ファイル
    inbox/*.info.json     - チャプター情報
    inbox/images/*.jpg    - 製品画像（存在確認のみ）

Outputs:
    inbox/chapter_transcripts/{slug}.txt  - 製品ごとの字幕
    stdout: {"chapters": [{title, slug, transcript_path, image_slug},...]}
"""

import sys
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INBOX = PROJECT_ROOT / "inbox"
TRANSCRIPTS_OUT = INBOX / "chapter_transcripts"

SKIP_KEYWORDS = ["introduction", "intro", "opening", "conclusion", "outro", "credits"]


def parse_vtt(vtt_path):
    """VTTをパースして [(seconds, text), ...] を返す（重複除去済み）"""
    entries = []
    seen_texts = set()
    with open(vtt_path, encoding="utf-8") as f:
        content = f.read()

    for block in re.split(r"\n{2,}", content):
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        ts_line = next((l for l in lines if "-->" in l), None)
        if not ts_line:
            continue
        start_str = ts_line.split("-->")[0].strip().split()[0]
        parts = start_str.replace(",", ".").split(":")
        try:
            if len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                seconds = int(parts[0]) * 60 + float(parts[1])
            else:
                continue
        except ValueError:
            continue
        text_lines = [
            l for l in lines
            if l and "-->" not in l and not l.isdigit() and not l.startswith("WEBVTT")
        ]
        text = " ".join(text_lines).strip()
        if text and text not in seen_texts:
            entries.append((int(seconds), text))
            seen_texts.add(text)

    return entries


def extract_chapter_transcript(entries, start_time, end_time):
    """チャプター時間範囲の字幕テキストを結合して返す"""
    texts = [text for ts, text in entries if start_time <= ts <= end_time]
    return " ".join(texts)


def main():
    # info.json
    info_files = list(INBOX.glob("*.info.json"))
    if not info_files:
        print("[error] inbox/*.info.json がありません", file=sys.stderr)
        sys.exit(1)
    with open(info_files[0]) as f:
        info = json.load(f)

    # VTT
    vtt_files = list(INBOX.glob("*.vtt"))
    if not vtt_files:
        print("[error] inbox/*.vtt がありません", file=sys.stderr)
        sys.exit(1)
    entries = parse_vtt(vtt_files[0])
    print(f"[ok] 字幕 {len(entries)} エントリ読み込み完了", file=sys.stderr)

    # チャプター情報を補完
    chapters = info.get("chapters", [])
    duration = info.get("duration", 0)
    if not chapters:
        print("[warn] チャプター情報がありません。動画全体を1チャプターとして扱います", file=sys.stderr)
        chapters = [{"title": info.get("title", "Full Event"), "start_time": 0, "end_time": duration}]
    else:
        for i, ch in enumerate(chapters):
            if "end_time" not in ch:
                ch["end_time"] = chapters[i + 1]["start_time"] if i + 1 < len(chapters) else duration

    # 製品チャプターのみ
    product_chapters = [
        ch for ch in chapters
        if not any(kw in ch["title"].lower() for kw in SKIP_KEYWORDS)
    ]

    images_dir = INBOX / "images"
    TRANSCRIPTS_OUT.mkdir(parents=True, exist_ok=True)

    result_chapters = []
    for ch in product_chapters:
        slug = ch["title"].lower().replace(" ", "_").replace("/", "_")
        transcript = extract_chapter_transcript(entries, ch["start_time"], ch["end_time"])
        out_path = TRANSCRIPTS_OUT / f"{slug}.txt"
        out_path.write_text(transcript, encoding="utf-8")

        img_slug = slug.replace("_", "-")
        has_image = (images_dir / f"{img_slug}-overview.jpg").exists()

        result_chapters.append({
            "title": ch["title"],
            "slug": slug,
            "transcript_path": str(out_path),
            "image_slug": img_slug if has_image else None,
            "chars": len(transcript),
        })
        print(f"[ok] {ch['title']}: {len(transcript)} chars → {out_path.name}", file=sys.stderr)

    print(json.dumps({"chapters": result_chapters}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
