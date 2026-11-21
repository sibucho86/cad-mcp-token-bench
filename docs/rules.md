# そろえる条件

測定済みの4つの CAD と同じ表に並べるために、そろえる条件です。`tools/bench.py` は、この条件でコマンドを組み立てます。

## 固定するもの

| 項目 | 値 |
|---|---|
| 課題 | T1〜T5 の仕様書（`tasks/*/spec.md`）。文言を変えない |
| 課題文 | [prompts/template.md](../prompts/template.md)。CAD ごとに変わるのは下の3か所だけ |
| Claude Code | モデル `claude-opus-5-5`、effort `high` |
| Codex CLI | モデル `gpt-6.1-sol`、reasoning effort `high` |
| AI が使える道具 | その CAD の MCP サーバーのツールだけ。シェル、ファイル操作、Web 検索は使えない |
| 設定 | 空の設定フォルダーで動かす。ふだんの指示ファイル、スキル、ほかの MCP サーバーを読ませない |
| 回数 | 課題ごとに新しいセッションで1回 |
| 時間 | 1回 60 分まで |
| MCP の待ち時間 | サーバーの起動 120 秒、ツール1回 900 秒 |
| 集計 | 5課題の合計トークンの中央値 |

測定済みの回のバージョンは Claude Code 2.1.310 と Codex CLI 0.164.0 です。バージョンが違うと、道具の定義や指示の文が変わって、トークンも変わることがあります。結果にはバージョンを書きます（`summary` が記録から入れます）。

## 課題文で CAD ごとに変わるところ

| 場所 | 中身 | 設定の項目 |
|---|---|---|
| 操作手段の文 | 「MCP サーバー `<名前>` のツールを使って <CAD> を操作してください。」と、足す1文 | `mcp_server`、`cad_name`、`interface_note` |
| 完了条件の 2 | 保存形式の説明とファイル名 | `native_desc`、`native_ext` |
| 完了条件の 3 | STEP を書き出す行。書き出せない MCP サーバーでは、行ごと外す | `step_export` |

ほかに、作業フォルダーのパスが回ごとに変わります。

課題文には、測定していることが AI に伝わる語を入れません。課題の番号、`token`、`bench`、ユーザー名、ユーザーのフォルダーのパスが入っていると、`bench.py` は実行の前に止まります。ルールの中の「APIキー・トークン」の1行だけは例外で、測定済みの回と同じ文のまま残します。

## 実行のコマンド

Claude Code のコマンドです。

```
claude -p --model claude-opus-5-5 --effort high --output-format stream-json --verbose
  --tools ToolSearch --restricted --strict-mcp-config --mcp-config <mcp.json>
  --setting-sources local --permission-mode dontAsk --permission-prompts none
  --allowedTools mcp__<名前> ToolSearch --no-session-persistence
```

Codex CLI のコマンドです。

```
codex exec --json --skip-git-repo-check --ignore-user-config --ignore-rules -m gpt-6.1-sol
  -c model_reasoning_effort="high" -c web_search="disabled" -c approval_policy="never"
  -s danger-full-access
  --disable apps --disable plugins --disable multi_agent --disable browser_use
  --disable computer_use --disable image_generation --disable goals --disable memories
  -C <作業フォルダー> --disable shell_tool --disable unified_exec
  -c mcp_servers.<名前>.…（MCP サーバーの設定） -
```

- 課題文は標準入力に UTF-8 で渡します。
- Claude Code の組み込みの道具は Tool Search だけを残します。MCP のツールの定義を、必要なときに読むための道具です。
- Claude Code は `CLAUDE_CONFIG_DIR` を回ごとの空のフォルダーにし、自動メモリー、自動更新、テレメトリーを切ります。
- AI エージェントに渡す環境変数は、OS の基本の変数と認証の変数だけです。ほかのサービスのキーなどは渡しません。

## トークンの数え方

1回の実行で、AI を呼び出したすべての回の入力と出力を足します。

| エージェント | 合計の出し方 | 元の記録 |
|---|---|---|
| Claude Code | 入力 ＋ キャッシュの読み込み ＋ キャッシュの書き込み ＋ 出力。使った全モデルの分 | 標準出力の最後の `result` 行の `modelUsage` |
| Codex CLI | 入力（キャッシュの分を含む）＋ 出力（思考の分を含む） | 標準出力の最後の `turn.completed` の `usage` |

- 思考は出力に含まれます。課題を読んで手順を考える分も、合計に入ります。
- キャッシュの読み込みも1トークンを1として数えます。料金の重みは付けません。
- 呼び出しの回数は AI を呼び出した回数です。Claude Code は記録のメッセージ ID の種類の数、Codex CLI はセッション記録の `token_count` から数えます。
- 合計はおよそ「呼び出しの回数 × 1回あたりの文脈の長さ」になります。1回の呼び出しで多くの操作をまとめられる MCP サーバーほど、合計が小さくなります。

## 集計

| 項目 | 決まり |
|---|---|
| 中央値 | 課題ごとの合計を5つ並べた中央の値 |
| 最少との比 | 課題ごとに、いちばん少ない CAD との比を出し、その幾何平均を取る |
| 使う回 | 課題ごとに最後の回。やり直しの決まりは [procedure.md](procedure.md) の 5 |
| 成否 | トークンとは別に数える。失敗した回のトークンも中央値に入れる |

## 成否の判定

表の `judge` の列に、どの方法で確かめたかを書きます。

| `judge` | 確かめ方 | 見るもの |
|---|---|---|
| `full` | 測定済みの回で使った検証 | STEP と参照モデル、ネイティブファイルと STEP、干渉、ジョイント |
| `step` | `bench.py check` | STEP と参照モデル（体積、外形、重心）、余分なソリッド、干渉 |
| `simple` | 測定済みの ZW3D の回で使った簡易の判定 | 部品の数、外形の中心と大きさ、拘束の数 |
| `manual` | 目で確かめて `bench.py mark` | 確かめた人が書く |

`step` は `full` より見る範囲が狭い判定です。測定済みの 30 回の成果物に `step` の検査をかけたところ、27 回は `full` と同じ結果でした。残りの3回は `step` では合格でした。1回はネイティブファイルと STEP が食い違っていた回、2回は `full` で判定できなかった回です。

## 測定済みの回との違い

このキットは測定済みの回の仕組みから、測るのに要る部分だけを取り出したものです。次の点が違います。

| 項目 | 測定済みの回 | このキット |
|---|---|---|
| ホームのフォルダーと PATH | 回ごとに空のホームを作り、エージェントと MCP サーバーに渡す。PATH は OS と道具の場所だけにする | ふだんのホームと PATH のまま。設定フォルダーだけを空にする |
| Codex CLI の設定フォルダー | 回ごとに空のフォルダー | 測定用のフォルダーを使い回す |
| CAD の状態 | 回ごとに自動で新しい状態にする | 人が新しい状態にする |
| 記録の監査 | 身元の文字列や、決まり違反の操作がないかを自動で調べる | 道具の範囲だけを確かめる |
| 成否 | `full`（ZW3D は `simple`） | `step` か `manual` |

課題文、実行のコマンド、数え方は同じです。測定済みの記録で、次のことを確かめています。

| 確かめたもの | 結果 |
|---|---|
| 課題文 | 4つの CAD × 6課題の 24 通りが、測定済みの回の課題文と一字一句同じになる |
| コマンド | 測定済みの 42 回のコマンドと、MCP サーバーの起動の部分を除いて同じになる |
| 数え方 | 記録の残っている 94 回で、合計、内訳、呼び出しの回数が元の集計と同じになる |
| キットを通した実行 | Claude Code と FreeCAD で立方体の課題を1回実行した（2026-11-21）。合計 12,736 トークン、呼び出し 3 回。測定済みの回の同じ条件は 12,733 トークン、3 回 |

Codex CLI ではキットを通した実行をまだ行っていません。記録済みの出力を流す作りもののエージェントで、実行の流れだけを確かめています。

## 測定済みの4つの CAD の条件

| CAD | バージョン | MCP サーバー | 回ごとの状態 |
|---|---|---|---|
| Autodesk Fusion | 2706.1.14 | Fusion に入っている MCP サーバー | 回ごとに Fusion を起動し直して、新しいデザインを開く |
| FreeCAD | 1.1.3 | neka-nat/freecad-mcp v0.1.28（コミット 8babe99）。`--only-text-feedback` を付ける | 回ごとに FreeCAD を起動する |
| SolidCraft | 0.21.0 | `solidcraft-cli mcp` | MCP サーバーが回ごとに起動する |
| ZW3D | WuKong 2027 18.2 | 公式の `ZW3D_MCP.exe` | 回ごとに ZW3D を起動し直す |

ZW3D の MCP サーバーには STEP の書き出しがないので、課題文から完了条件の 3 を外しています。

## 結果を読むときの注意

- どのマスも1回の値です。同じ条件でも回によってトークンは変わるので、差が小さい CAD どうしの順位は入れ替わることがあります。
- Claude Code と Codex CLI は、モデルもトークンの区切り方も違います。同じエージェントの中で、CAD どうしを比べてください。
- 測っているのは、MCP サーバーを通した CAD の操作です。同じ CAD でも、MCP サーバーが変われば結果が変わります。
- モデルも MCP サーバーも更新されます。結果には測った日とバージョンを書いています。
