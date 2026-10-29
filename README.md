# small-ai-hands — 小さなAIで手足をうごかす

**English summary.** A hobby experiment in letting *small* models drive Linux desktop apps (GNOME / Wayland). The screen is read as the **AT-SPI2 accessibility tree**, not screenshots; actions go through AT-SPI2 `Action`, with a real keyboard (ydotool) only where the tree cannot write. Three layers: a **planner** (Claude via API) turns a request into a plan file of one-line sub-goals, each with a machine-checkable condition; an **executor** picks one numbered action from what is visible right now; the **loop** (`smallhands/loop.py`) performs the action and polls until the condition holds. The executor can be a local 4B model (qwen3:4b-instruct on a 6 GB RTX 3050 via Ollama), Claude Haiku/Sonnet, or **Jev** (TypeSafe AI's decision-only model, `--model jev-latest`, whose confidence score lets the loop cut a low-confidence WAIT from 12 s to 2 s). On the same 4-line plan, all executors succeed; a plain run takes 12–20 s, the local 4B costs only electricity, Sonnet ≈ ¥2, Haiku ≈ ¥0.1, Jev ≈ ¥0.04 per run. Timings and failure modes are logged per step and written up (in Japanese) in the Zenn series linked below. Similar ideas at production scale: Microsoft UFO2, UI-TARS; this repo is the minimal, reproducible, numbers-first version.

RTX 3050 のノート PC で動く 4B クラスのローカル LLM（Ollama）に、Linux デスクトップのアプリを操作させる趣味の実験コードです。
画面はスクリーンショットではなく **アクセシビリティツリー（AT-SPI2）** で読み、操作も AT-SPI2 の Action で押します。押せない・書けないところだけキーボード（ydotool）で補います。

Zenn の連載「小さなAIで手足をうごかす」の実験に使っているものを、そのまま置いています。

- [#1 RTX 3050 のノートで Qwen3 4B に gedit を操作させたら、4手8.6秒だった](https://zenn.dev/mappy55/articles/small-ai-hands-01)
- [#2 小さくしても速くならず、考えさせると7倍遅い](https://zenn.dev/mappy55/articles/small-ai-hands-02)
- [#3 Firefox は読めるけど書けない。4B に目次を開かせて「歴史」を読ませるまで](https://zenn.dev/mappy55/articles/small-ai-hands-03)
- [#4 依頼文一つで最後まで。計画は Claude、手は 4B、落ちたのは手と段取り](https://zenn.dev/mappy55/articles/small-ai-hands-04)
- [#5 手足も Claude にしたら。4B・Haiku・Sonnet を同じ計画で、秒数と成否と値段で比べる](https://zenn.dev/mappy55/articles/small-ai-hands-05)
- [#6 選ぶだけのモデル Jev を手足にしたら、同じ計画が 12 秒・0.04 円で通った](https://zenn.dev/mappy55/articles/small-ai-hands-06)

## 構成

三層に分けています。

| 層 | 誰が | 何をする |
|---|---|---|
| 計画層 | Claude（`smallhands/planner.py`、API） | 依頼文を「1 行 1 サブ目標＋達成条件」の計画ファイルにする |
| 実行層 | qwen3:4b-instruct など小さなモデル（Claude や Jev にも差し替え可） | 今のサブ目標と番号付きの選択肢だけを見て、番号を答える |
| 判定層 | `smallhands/loop.py` | AT-SPI2 か ydotool で実行し、達成条件が満たされるまで待つ |

計画ファイルはこういう形です（`plans/plan_tokyo_history.txt`）。

```text
launch firefox --profile $HOME/ffprof about:blank||window:mozilla firefox
Type ja.wikipedia.org/wiki/%E6%9D%B1%E4%BA%AC%E9%A7%85 into the address bar and press Enter to open the page||heading:東京駅
Click the link '歴史' (History) in the table of contents to jump to that section||url:#歴史
read@歴史 the heading '歴史' and the first paragraphs under it||doc:東京駅
```

`||` の左がモデルに見せる目標文、右がコードで判定する達成条件です。`launch` と `read` で始まる行はモデルに聞かず、コードが実行します。

達成条件の語彙: `window:` `nowindow:`（ウィンドウ名）、`title:`（メインウィンドウのタイトル）、`heading:`（本文の見出しが画面内にある）、`url:`（アドレスバーの中身）、`doc:`（表示中のページ名）、`text:` / `text@<ウィンドウ>:`（入力欄の中身）。

## 動かす

試した環境: Windows 11 ホストで Ollama、VirtualBox 上の Ubuntu 25.04（GNOME / Wayland）がゲスト。ゲストからホストの Ollama に `10.0.2.2:11434` で届きます。

ホスト側（Ollama）:

```powershell
$env:OLLAMA_HOST = "0.0.0.0"
ollama serve
ollama pull qwen3:4b-instruct
```

ゲスト側（Ubuntu）:

```bash
sudo apt install python3-pyatspi ydotool firefox
bash scripts/setup_ydotool.sh          # ydotoold をサービス化（root で動かす。一度だけ）
bash scripts/run_batch.sh qwen3:4b-instruct 3 plans/plan_tokyo_history.txt
```

ログは `logs/` に出ます（CSV と標準出力、各ステップのプロンプトと答え）。`logs/` は Git 管理外です。

知っておくこと:

- **ydotool は前面のウィンドウに打ちます。** 実行中に他のウィンドウをクリックすると、文字がそちらへ行きます。ループは対象アプリを自分で起動して、前面にある前提で進めます。
- `ydotool type` は ASCII だけが確実です。日本語を含む URL は %エンコードして計画ファイルに書きます。
- Firefox は専用プロファイル `$HOME/ffprof` で起動します（初回画面・翻訳パネル・セッション復元を `user.js` で切っています）。
- gedit のような GTK アプリは Collection インターフェースに対応していないので、木を歩く方式に自動で切り替わります。

## 依頼文一つで動かす（計画層を Claude に）

計画ファイルを Claude（Anthropic API）に書かせることもできます。API キーは環境変数で渡します（リポジトリには入れません）。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
bash scripts/run_request.sh "Firefox で Wikipedia の東京駅を調べて、歴史の節を読んで"
```

`smallhands/planner.py` が依頼文と条件の語彙、いま見えているウィンドウ名を Claude に渡し、`plans/generated_<日時>.txt` を書きます。そのまま `run_batch.sh` に渡して実行します。モデルは `ANTHROPIC_MODEL`（既定 `claude-sonnet-5-5`。`--list-models` で一覧）で変えられます。

## 実行層を Claude にする

`--model claude-...` を渡すと、実行層（番号を選ぶ側）も Anthropic API になります。同じ約束・同じ選択肢を渡し、返事の本文から `{choice, text}` の JSON を拾います（Claude 5 系は tool_choice の強制が使えないため）。ローカル LLM との比較用です。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
bash scripts/run_batch.sh claude-sonnet-5-5 3 plans/plan_tokyo_history_by_claude.txt
```

run の最後に `api tokens in=… out=…` と累計トークンが出ます。

`--model jev-latest` にすると、実行層が TypeSafe AI の判断専用モデル Jev になります（環境変数 `TYPESAFE_API_KEY`）。Jev は文章を書かないので、選択肢の番号を choice で選ばせ、打つ文字列はサブ目標の文中の引用符から拾います。確信度と上位の確率をログに出します。`--wait-conf 0.8`（または環境変数 `SMALLHANDS_WAIT_CONF=0.8`）を付けると、Jev が確信度 0.8 未満で WAIT を選んだときに 12 秒のタイムアウトを待たず、2 秒（`--wait-short`）で聞き直します。

`--shot marks`（または環境変数 `SMALLHANDS_SHOT=marks`）を付けると、各手でスクショを撮り、選択肢の部品の矩形に一覧と同じ番号の札を描いて（Set-of-Mark）、画像対応のモデル（Ollama の gemma3 / qwen3-vl など）に一覧と一緒に渡します。答えは今までどおり番号一つ。`--shot plain` は札なしの素のスクショ。スクショは GNOME の Shift+Print を ydotool で押して撮るので、run_batch.sh が通知バナーを切ります。

## ファイル

```
smallhands/loop.py        メインループ（ツリー取得・間引き・選択肢・LLM 呼び出し・実行・条件待ち）
smallhands/planner.py     計画層: 依頼文 → 計画ファイル（Anthropic API）
scripts/run_request.sh    依頼文 → 計画 → 実行 を一気に
plans/                    計画ファイル
scripts/run_batch.sh      N 回まわして logs/ に記録
scripts/setup_ydotool.sh  ydotoold の systemd ユニットを入れる（scripts/ydotoold.service）
scripts/atspi_dump.py     アプリのアクセシビリティツリーを JSON で見る（下見用）
```

## これから

- 計画層が書いた条件が実物の画面と合わない失敗を集めて、約束の文章を直す
- 実行層を Claude に差し替えて、小さなモデルと秒数・成否・費用を並べる
- `loop.py` を a11y / conditions / actions / backends に分ける

## ライセンス

MIT

（このプロジェクトの設計と実装は Claude と一緒に進めています）
