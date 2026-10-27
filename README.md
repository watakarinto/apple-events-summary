# Apple Events Summary

Apple Event の内容を日本語でまとめて公開するサイト。

**公開先：** [https://watakarinto.github.io/apple-events-summary/](https://watakarinto.github.io/apple-events-summary/)

---

## 使い方

### YouTube URL から生成

```bash
/apple-event https://www.youtube.com/watch?v=XXXXXX
```

字幕の取得、製品ごとの記事執筆、まとめスライド画像の抽出、ローカルコミットまでを自動で行う（目安 3〜5分）。**push はしない**。内容を確認してから手動で push する。

### 手元の字幕ファイルから生成

`inbox/transcript.txt` に字幕を置いてから：

```bash
/apple-event
```

1行目が YouTube URL なら通常の手順で生成する。URL が無い場合は画像なしの記事になる。

### 画像を追加で差し込む場合

`inbox/images/` にスクショを置いてから実行する。Claude が画像の内容を見て、該当セクションに挿入する。

---

## 記事の構成

- 冒頭：開催日・場所・テーマを1〜2文
- 製品ごとのセクションを発表順（動画の順）に並べる。YouTube のチャプターに複数製品があれば、製品ごとに分ける（例：AirPods 4／AirPods Max／AirPods Pro 2）
  - まとめスライド画像
  - **ハイライト**：変更点の箇条書き（太字ラベル。目次には出さない）
  - トピックごとの `###` 見出し：内容に合わせて表・箇条書き・短い段落を使い分ける
- 最後：全製品の価格・発売日の表（価格はここにだけ書く）。日本円の参考値は、発表日の為替レートで自動換算して列を足す（レートは [Frankfurter](https://frankfurter.dev/)（欧州中央銀行の参考レート、API キー不要）から取得。取得できなければ日本円の列なしで続行）

ページの目次は製品名だけが並び、読んでいる製品のトピックだけが展開される（Blowfish の smartTOC）。

書き方のルールは `.claude/skills/apple-event/writer_rules.md` にある。ここを直せば次の実行から反映される。

---

## 仕組み

```
/apple-event <URL>
  1. fetch     字幕（手動字幕を優先）と info.json を取得し、製品チャプターごとに字幕を分ける
  2. 執筆      チャプターごとのライター（Sonnet）を同時に起動。各自が字幕を読み、製品ごとのセクションをファイルに書く
  3. slides    240p SDR の映像を解析してカード状まとめスライドを検出し、1080p の静止画を取得
  4. 確認      候補を並べた確認シートを Claude が1回見て、違っていれば差し替える
  5. assemble  ヘッダ・各セクション・まとめを連結して content/posts/{日付-slug}/index.md を作る
  6. git commit（ローカル）→ inbox を空にする（動画・字幕は残さない）

手動で main に push → GitHub Actions が Hugo ビルド → GitHub Pages に公開
```

2 と 3〜4 は並行して進む。

### 画像を手で直す

```bash
P=.claude/skills/apple-event/pipeline.py
uv run $P pick <slug> c2              # 確認シートの2位候補に差し替え
uv run $P pick <slug> 1885            # 指定した秒の画面に差し替え
uv run $P pick <slug> c2 --extra      # 追加の画像として保存（1チャプターに複数製品があるとき）
uv run $P pick <slug> none            # 画像なし
uv run $P sheet <slug> [開始秒 終了秒]  # 2秒刻みの一覧シートを作る（秒数を探すとき）
uv run $P place <画像ファイル名> <見出し>  # 画像を製品セクションの先頭に置く（1チャプターに複数製品があるとき）
uv run $P yen <index.md> <発表日>     # 既存の記事の価格表に日本円の参考値を足す（例: 2024-09-09）
```

---

## ディレクトリ構成

```
.claude/skills/apple-event/SKILL.md          スキル本体（手順）
.claude/skills/apple-event/pipeline.py       字幕取得・スライド抽出・記事の組み立て・後片付け
.claude/skills/apple-event/writer_rules.md   製品セクションの書き方ルール
.claude/agents/apple-event-writer.md         製品セクションを書くサブエージェント（Sonnet）
.claude/settings.json                        記事パーツの書き込み許可など
content/posts/{日付-slug}/                    記事（index.md と画像をまとめて配置）
inbox/                                       一時置き場（gitignore 済み。実行後に自動で空にする）
.github/workflows/hugo.yml                   GitHub Actions（main への push で自動公開）
```

---

## セットアップ（初回のみ）

```bash
# Hugo は mise でバージョン固定（0.165.0 extended）
brew install mise yt-dlp ffmpeg uv
git clone --recurse-submodules https://github.com/watakarinto/apple-events-summary
cd apple-events-summary
mise install
hugo server -D  # http://localhost:1313 でプレビュー
```

`pipeline.py` が使う Python ライブラリ（numpy / Pillow / SciPy）は、`uv run` が初回に自動で入れる。
