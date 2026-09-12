# Apple Events Summary

Apple Event の内容を日本語でまとめて公開するサイト。

**公開先：** [https://watakarinto.github.io/apple-events-summary/](https://watakarinto.github.io/apple-events-summary/)

---

## 使い方

### YouTube URL から生成

```bash
/apple-event https://www.youtube.com/watch?v=XXXXXX
```

### 手元の字幕ファイルから生成

```
inbox/transcript.txt  に字幕を置いてから：
```

```bash
/apple-event
```

### 画像を差し込む場合

```
inbox/images/ にスクショを置いてから実行する。
Claude が画像の内容を見て、該当セクションに自動で挿入する。
```

---

## 仕組み

```
字幕（yt-dlp or inbox/）
  → Claude が製品ごとに英語で要約
  → 日本語に翻訳
  → Markdown 生成（content/posts/{slug}/index.md）
  → git push
  → GitHub Actions が Hugo ビルド
  → GitHub Pages に公開
```

---

## ディレクトリ構成

```
.claude/commands/apple-event.md   スキル本体
content/posts/{slug}/             記事（index.md + 画像をまとめて配置）
inbox/                            手動入力の一時置き場（gitignore済み）
.github/workflows/hugo.yml        GitHub Actions（push で自動公開）
```

---



## セットアップ（初回のみ）

```bash
# Hugo は mise でバージョン固定（0.165.0 extended）
brew install mise yt-dlp
git clone --recurse-submodules https://github.com/watakarinto/apple-events-summary
cd apple-events-summary
mise install
hugo server -D --baseURL http://localhost:1313/  # http://localhost:1313 でプレビュー
```

