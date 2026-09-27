#!/usr/bin/env python3
"""
Apple Event まとめ記事セクション生成スクリプト

Usage:
    python3 generate_article.py

Reads:
    inbox/*.vtt           - 字幕ファイル
    inbox/*.info.json     - チャプター情報
    inbox/images/*.jpg    - 製品画像（あれば）

Outputs:
    inbox/sections_md/{slug}.md  - 製品ごとのMarkdownセクション
    stdout: {"sections": [{"title", "slug", "path"}, ...]} （チャプター順）

環境変数:
    ANTHROPIC_API_KEY が必要（Claude Code セッションでは自動的に設定済み）
"""

import sys
import json
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INBOX = PROJECT_ROOT / "inbox"
SECTIONS_OUT = INBOX / "sections_md"

SKIP_KEYWORDS = ["introduction", "intro", "opening", "conclusion", "outro", "credits"]
MODEL = "claude-sonnet-4-6"
MAX_WORKERS = 7


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


def generate_section(client, chapter, transcript, image_slug, event_title):
    """Anthropic API で製品セクションを生成して Markdown を返す"""
    title = chapter["title"]
    img_line = (
        f"![{title} 機能まとめ]({image_slug}-overview.jpg)\n*出典：Apple*\n\n"
        if image_slug else ""
    )

    prompt = f"""あなたは Apple Event まとめ記事の担当ライターです。
以下の製品セクションのみを日本語で生成してください。

イベント：{event_title}
製品名：{title}

担当製品の字幕（チャプター抜粋）：
{transcript}

**出力ルール（厳守）：**
- `## {title}` セクションのみ出力する（frontmatter・他製品・まとめは書かない）
- セクション末尾に `---` を付ける
- 画像がある場合は本文の先頭に以下を挿入する：
{img_line if img_line else "（画像なし）"}

粒度の方針（最重要）：
- 読者は **Apple信者・デベロッパ**。字幕で言及された機能・数値・デモ・API名をすべて拾って詳報として書く
- 「〇〇が改善された」で終わらせず「何がどう変わったか・具体的な数値・仕組み」まで書く
- プラットフォームごとに挙動が異なる場合は表で全プラットフォームを列挙する
- 複数のサブ機能があれば `###` 見出しで分けて段落ごとに説明する
- 価格・発売日は `### 価格・発売日` セクションに表形式でまとめる

表現の方針：
- 原文の言い回しを忠実に再現。誇張・評価の上乗せをしない
- 数値比較は字幕に根拠がある場合のみ書く"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def main():
    try:
        import anthropic
    except ImportError:
        print(
            "[error] anthropic パッケージが必要です: pip install anthropic --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

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

    event_title = info.get("title", "Apple Event")

    # 製品チャプターのみ
    product_chapters = [
        ch for ch in chapters
        if not any(kw in ch["title"].lower() for kw in SKIP_KEYWORDS)
    ]
    print(f"[ok] 製品チャプター {len(product_chapters)} 件: {[c['title'] for c in product_chapters]}", file=sys.stderr)

    images_dir = INBOX / "images"
    SECTIONS_OUT.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic()

    def process(ch):
        slug = ch["title"].lower().replace(" ", "_").replace("/", "_")
        transcript = extract_chapter_transcript(entries, ch["start_time"], ch["end_time"])
        img_slug = slug.replace("_", "-")
        has_image = (images_dir / f"{img_slug}-overview.jpg").exists()
        image_slug = img_slug if has_image else None
        print(f"[gen] {ch['title']} ({len(transcript)} chars, image={'yes' if has_image else 'no'})...", file=sys.stderr)
        text = generate_section(client, ch, transcript, image_slug, event_title)
        out_path = SECTIONS_OUT / f"{slug}.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"[ok] {ch['title']} → {out_path.name}", file=sys.stderr)
        return slug, str(out_path)

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process, ch): ch["title"] for ch in product_chapters}
        for future in as_completed(futures):
            title = futures[future]
            try:
                slug, path = future.result()
                results[slug] = path
            except Exception as e:
                print(f"[error] {title}: {e}", file=sys.stderr)

    # チャプター順にソート
    ordered = []
    for ch in product_chapters:
        slug = ch["title"].lower().replace(" ", "_").replace("/", "_")
        if slug in results:
            ordered.append({"title": ch["title"], "slug": slug, "path": results[slug]})

    print(json.dumps({"sections": ordered}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
