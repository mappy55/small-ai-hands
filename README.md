# small-ai-hands — 小さなAIで手足をうごかす

RTX 3050 のノート PC で動く 4B クラスのローカル LLM（Ollama）に、Linux デスクトップのアプリを操作させる趣味の実験コードです。
画面はスクリーンショットではなく **アクセシビリティツリー（AT-SPI2）** で読み、操作も AT-SPI2 の Action で押します。押せない・書けないところだけキーボード（ydotool）で補います。

Zenn の連載「小さなAIで手足をうごかす」の実験に使っているものを、そのまま置いています。

- [#1 RTX 3050 のノートで Qwen3 4B に gedit を操作させたら、4手8.6秒だった](https://zenn.dev/mappy55/articles/small-ai-hands-01)
- [#2 小さくしても速くならず、考えさせると7倍遅い](https://zenn.dev/mappy55/articles/small-ai-hands-02)
- [#3 Firefox は読めるけど書けない。4B に目次を開かせて「歴史」を読ませるまで](https://zenn.dev/mappy55/articles/small-ai-hands-03)

## 構成

三層に分けています。

| 層 | 誰が | 何をする |
|---|---|---|
| 計画層 | Claude（いまは会話の中で手書き） | 依頼文を「1 行 1 サブ目標＋達成条件」の計画ファイルにする |
| 実行層 | qwen3:4b-instruct など小さなモデル | 今のサブ目標と番号付きの選択肢だけを見て、番号を答える |
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

`--model claude-...` を渡すと、実行層（番号を選ぶ側）も Anthropic API になります。同じ約束・同じ選択肢を渡し、tool_use で `{choice, text}` を返させます。ローカル LLM との比較用です。

```bash
export ANTHROPIC_API_KEY=sk-ant-...
bash scripts/run_batch.sh claude-sonnet-5-5 3 plans/plan_tokyo_history_by_claude.txt
```

run の最後に `anthropic tokens in=… out=…` と累計トークンが出ます。

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
