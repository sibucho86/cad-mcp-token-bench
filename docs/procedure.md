# 手順

CAD の MCP サーバーを1つ測るための手順です。そろえる条件の理由と数え方は [rules.md](rules.md) にあります。

コマンドはリポジトリのフォルダーで、ふつうのターミナルから実行します。Claude Code や Codex の中からは実行しないでください。

## 1. 準備するもの

| もの | 確かめたバージョン | メモ |
|---|---|---|
| Python | 3.12 | 3.9 以上。標準ライブラリだけを使う |
| Claude Code | 2.1.310 | `claude --version` で確かめる |
| Codex CLI | 0.164.0 | Codex でも測るときだけ |
| CAD と MCP サーバー | — | AI なしで MCP サーバーが動くところまで済ませておく |
| FreeCAD | 1.1.3 | できたモデルを STEP で検査するときだけ |

Windows では、Claude Code と Codex CLI は `.exe` を使います。`codex.cmd` のようなバッチファイルは、引数の引用符が壊れるので使えません。npm で入れた Codex CLI は、`bench.py` が中の `codex.exe` を探します。見つからないときは `--agent-bin` で場所を指定します。

## 2. 認証

AI エージェントを動かすときは、ふだんの設定を読まない空の設定フォルダーを使います。ふだんの CLAUDE.md、スキル、ほかの MCP サーバーが文脈に入ると、トークンが増えて比べられなくなるためです。空のフォルダーにはログインの記録がないので、認証を別に用意します。

### Claude Code

`claude setup-token` で長期トークンを作り、ターミナルの環境変数に入れます。

PowerShell では次のように入れます。

```powershell
$env:CLAUDE_CODE_OAUTH_TOKEN = "（作った値）"
```

bash では次のように入れます。

```bash
export CLAUDE_CODE_OAUTH_TOKEN="（作った値）"
```

- 値はファイルに書かず、そのターミナルの中だけで使います。`bench.py` は値を表示も記録もしません。
- `ANTHROPIC_API_KEY` でも動きます。その場合は API の料金がかかります。

### Codex CLI

測定用の空のフォルダーを `CODEX_HOME` にして、ログインします。PowerShell の例です。

```powershell
mkdir C:/cadwork/codex-home
$env:CODEX_HOME = "C:/cadwork/codex-home"
codex login
Remove-Item Env:CODEX_HOME
```

実行のときに `--codex-home C:/cadwork/codex-home` を付けます。このフォルダーには AGENTS.md や config.toml を置かないでください。置いてあると、`bench.py` が注意を出します。

## 3. CAD の設定ファイル

[cads/example.json](../cads/example.json) を写して、`cads/<id>.json` を作ります。測定済みの4つの設定（`cads/fusion.json` など）も参考になります。

| 項目 | 書くこと | 例 |
|---|---|---|
| `id` | 結果の表に使う短い名前。英小文字と数字 | `mycad` |
| `label` | 表に出す名前 | `MyCAD 2026` |
| `cad_name` | 課題文に書く CAD の名前 | `MyCAD` |
| `mcp_server` | MCP サーバーの名前。ツール名の `mcp__<名前>__…` の部分 | `mycad` |
| `interface_note` | 操作手段の文に足す1文。なければ空 | `MyCAD は起動済みです。` |
| `native_desc` | 保存形式の説明 | `MyCAD のアセンブリファイル .myasm` |
| `native_ext` | 保存するファイルの拡張子 | `.myasm` |
| `step_export` | MCP サーバーで STEP を書き出せるなら `true` | `true` |
| `mcp` | MCP サーバーの起動のしかた | 下を参照 |
| `versions` | CAD と MCP サーバーのバージョン | 結果の表に入る |

`mcp` の書き方は2通りです。1つ目はコマンドで起動して標準入出力でつなぐ MCP サーバーです。

```json
"mcp": {"command": "C:/path/to/mycad-mcp.exe", "args": ["--allow-path", "{work}"]}
```

2つ目は HTTP でつなぐ MCP サーバーです。

```json
"mcp": {"type": "http", "url": "http://127.0.0.1:12345/mcp"}
```

- `args` と `env` の中の `{work}` はその回の作業フォルダーのパスに置き換わります。
- 立方体1個の課題で部品用の別の形式に保存する CAD は、`native_desc_part` と `native_ext_part` も書きます（`cads/zw3d.json` を参照）。
- MCP サーバーがライセンスなどの環境変数を使うなら、`mcp` の `env` に書くか、実行のときに `--pass-env 変数名` を付けます。AI エージェントには OS の基本の変数と認証の変数だけを渡しています。

課題文を先に見るには、次のコマンドを使います。

```
python tools/bench.py prompt --cad cads/mycad.json --task T1 --work C:/cadwork/runs/r000000
```

## 4. 接続の確認

CAD を起動してから、立方体1個の課題を実行します。MCP サーバーが開いているドキュメントを操作する種類なら、先に空の新しいドキュメントを開いておいてください。

```
python tools/bench.py run --cad cads/mycad.json --agent claude --task plumbing
```

Codex CLI では `--codex-home` を付けます。

```
python tools/bench.py run --cad cads/mycad.json --agent codex --task plumbing --codex-home C:/cadwork/codex-home
```

終わると、合計トークン、呼び出しの回数、時間、成果物があるかが出ます。

| もの | 場所 |
|---|---|
| 作業フォルダー | `C:/cadwork/runs/<ID>/`（Windows 以外は `/tmp/cadwork/runs/<ID>/`）。`--work-root` で変えられる |
| 記録 | `results/raw/<id>/<エージェント>-<課題>-<ID>/`。課題文、コマンド、標準出力（`stream.jsonl`）、結果（`result.json`） |

- 作業フォルダーのパスは課題文に入ります。ユーザー名、`token`、`bench` を含む場所は使えません。含んでいると、実行の前に止まります。
- `--dry-run` を付けると、実行せずにコマンドを表示します。

立方体が保存されていて、注意が出ていなければ、次へ進みます。

## 5. 5課題の実行

課題ごとに1回ずつ実行します。

```
python tools/bench.py run --cad cads/mycad.json --agent claude --task T1
python tools/bench.py run --cad cads/mycad.json --agent claude --task T2
python tools/bench.py run --cad cads/mycad.json --agent claude --task T3
python tools/bench.py run --cad cads/mycad.json --agent claude --task T4
python tools/bench.py run --cad cads/mycad.json --agent claude --task T5
```

実行の前と途中で守ることは2つです。

- 毎回、CAD を起動し直してから始めます。測定済みの回もそうしています（[rules.md](rules.md) の表）。起動に時間がかかる CAD では、少なくとも前の回のドキュメントを閉じて、新しいドキュメントから始めます。
- 実行中は CAD とその PC をさわりません。1回は 60 分で打ち切ります。

どの回を結果に使うかは次のように決めます。

| 回の終わり方 | 扱い |
|---|---|
| AI が自分で作業を終えた | 数える。モデルが間違っていても数え、成否は別の列に書く |
| CAD や MCP サーバーが落ちた、接続が切れた、利用上限や API のエラーで止まった | 同じ課題をやり直す。集計は課題ごとに最後の回を使う。やり直した回数は表の `attempts` に出る |
| 60 分で打ち切られた | 合計を空欄にして、メモに「時間切れ」と書く（`summary` がそうする）。中央値には入らない |

## 6. できたモデルの確認

成否はトークンとは別の列に書きます。確かめ方は2通りです。

### STEP を書き出せる CAD

FreeCAD の `freecadcmd` で、STEP を参照モデルと比べます。

```
python tools/bench.py check results/raw/mycad/claude-T1-r1a2b3c --freecadcmd "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe"
```

- 部品ごとの体積（±0.5%）、外形と重心（±0.2 mm）、余分なソリッド、部品どうしの干渉を見ます。
- ジョイントとネイティブファイルの中身は見ません。ジョイントは CAD で動かして `tasks/<課題>/joints.json` と見比べてください。
- 検査は一時フォルダーを設定の置き場所にして動くので、ふだんの FreeCAD の設定は変わりません。

### STEP を書き出せない CAD

CAD でモデルを開き、部品の数と各部品の外形を `tasks/<課題>/reference.json` と見比べて、結果を記録します。

```
python tools/bench.py mark results/raw/mycad/claude-T1-r1a2b3c --ok yes --note "部品の数と外形を確かめた"
```

## 7. 集計

```
python tools/bench.py summary --cad cads/mycad.json
```

`results/submitted/<id>.csv` と `results/submitted/<id>.md` ができます。`.md` は、測定済みの4つの CAD と並べた表です。

## 8. 結果の送り方

Pull Request で `cads/<id>.json` と `results/submitted/<id>.csv` を送ってください。

- 設定ファイルの中の PC に固有のパスは `C:/path/to/...` に書き換えます。
- `results/raw/` は送りません。Claude Code の起動の記録や MCP サーバーの出力に、PC のユーザー名を含むパスが入っていることがあります。このフォルダーは `.gitignore` に入れてあります。

## 知っておくこと

- MCP サーバーの出力はそのまま AI の提供元に送られます。測定済みの CAD のうち2つ（Fusion と ZW3D）では、出力に PC のユーザー名を含むパスが入っていました。
- Codex CLI はサンドボックスを切って動かします（`danger-full-access`）。シェルとファイル操作の道具を外しているので、AI が使えるのは MCP サーバーのツールだけです。
- モデルは `--model` で変えられますが、変えた結果は測定済みの値と比べられません。
- 記録から合計を出し直すには、`python tools/bench.py count --agent claude results/raw/…/stream.jsonl` を使います。
