# /apple-event

Apple Event の字幕から製品ごとの日本語まとめ記事を生成して公開する。

## 使い方

```
/apple-event <YouTube URL>   # URLから字幕を自動取得
/apple-event                 # inbox/transcript.txt を使う
```

## 手順

### 1. 入力の判定

**URLが引数にある場合：**
```bash
yt-dlp --write-auto-sub --sub-lang en --skip-download \
  --write-info-json -o "inbox/%(title)s" <URL>
```
- `inbox/*.vtt` が字幕ファイル
- `inbox/*.info.json` の `chapters` フィールドをチャプター情報として使う（あれば）
- `inbox/*.info.json` の `upload_date`（例: `20260909`）を記事の `date` に使う

**URLがない場合：**
- `inbox/transcript.txt` を字幕として使う
- チャプター情報・日付はなしとして扱い、日付は `1970-01-01` を仮置きしてユーザーに確認する

### 2. 字幕の読み込み

- VTT形式の場合はタイムスタンプ行を除去してテキストのみ抽出
- 重複行（VTTの仕様で同じ行が続く）を除去して読みやすくする

### 3. 製品ごとに日本語まとめを生成

以下のプロンプトでClaudeが処理する：

```
以下はApple Eventの英語字幕です。

[チャプター情報がある場合]
チャプター：
{chapters}

字幕：
{transcript}

指示：
1. 字幕を読み、製品・トピックごとにセクションを分割する
   - チャプターがあればそれを優先ヒントにする
   - なければ "Now, let's talk about..." などの切り替え発話を手がかりにする
2. 各セクションを英語で箇条書きに要約する（数値・チップ名・価格は字幕の該当箇所を根拠に正確に書く）
3. その英語要約をそのまま日本語に翻訳する
4. 以下のMarkdown形式で出力する：

---
title: "{イベント名} まとめ"
date: {info.jsonのupload_dateをYYYY-MM-DD形式に変換した日付（例: 20260909 → 2026-09-09）}
draft: false
tags: [{製品名1}, {製品名2}, ...]
summary: "一言でイベントの概要"
---

## {製品名1}

- {日本語まとめ箇条書き}

## {製品名2}

- {日本語まとめ箇条書き}

...
```

### 4. inbox/images/ の振り分け（画像がある場合）

`inbox/images/` にファイルがあれば、各画像をVisionで確認して：
- どの製品セクションの画像かを判定
- `static/images/{slug}/` にコピー
- 該当セクションの末尾に挿入

```markdown
![{キャプション}](/images/{slug}/{filename})
*出典：Apple*
```

### 5. MDファイルの保存

- ファイル名：`content/posts/{YYYY-MM-DD}-{slug}.md`
- slug はイベント名から生成（例：`wwdc-2026`）

### 6. 公開

```bash
git add content/posts/ static/images/
git commit -m "Add: {イベント名} まとめ"
git push
```

push 後、数分で https://watakarinto.github.io/apple-events-summary/ に反映される。

## inbox/ の後片付け

公開確認後、必要に応じて手動で削除：
```bash
rm -rf inbox/*
```
字幕ファイルは著作権上、長期保存しない（PLANの法律面の整理より）。
