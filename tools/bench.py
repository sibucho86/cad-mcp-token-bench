#!/usr/bin/env python3
"""CAD の MCP ごとのトークン消費量を測る道具。

  python tools/bench.py prompt  --cad cads/mycad.json --task T1 --work C:/cadwork/runs/r000000
  python tools/bench.py run     --cad cads/mycad.json --agent claude --task plumbing
  python tools/bench.py count   --agent claude results/raw/mycad/claude-T1-r1a2b3c/stream.jsonl
  python tools/bench.py check   --freecadcmd C:/path/to/freecadcmd.exe results/raw/mycad/claude-T1-r1a2b3c
  python tools/bench.py mark    results/raw/mycad/claude-T1-r1a2b3c --ok yes --note "部品の数と外形を目で確かめた"
  python tools/bench.py summary --cad cads/mycad.json

標準ライブラリだけで動く（Python 3.9 以上）。手順は docs/procedure.md、そろえる条件は docs/rules.md。
認証の値（トークンや API キー）は、表示も記録もしない。
"""
import argparse
import csv
import glob
import json
import math
import os
import re
import secrets
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "prompts", "template.md")
TASK_ROOT = os.path.join(ROOT, "tasks")
MEASURED = os.path.join(ROOT, "results", "measured.csv")
WINDOWS = os.name == "nt"

# ---------- そろえる条件（docs/rules.md） ----------
CLAUDE_MODEL, CLAUDE_EFFORT = "claude-opus-5-5", "high"
CODEX_MODEL, CODEX_EFFORT = "gpt-6.1-sol", "high"
TIMEOUT_MIN = 60              # 1回の上限
MCP_STARTUP_TIMEOUT_S = 120   # MCP サーバーの起動の待ち
MCP_TOOL_TIMEOUT_S = 900      # MCP のツール1回の待ち
CODEX_DISABLE = ["apps", "plugins", "multi_agent", "browser_use", "computer_use", "image_generation", "goals",
                 "memories", "shell_tool", "unified_exec"]

# 課題: ID → (フォルダー, ファイル名に使う名前, 課題文に書く名前, 種類)
TASKS = {
    "plumbing": ("plumbing", "block", "立方体のブロック", "モデル"),
    "T1": ("T1_vise", "vise", "マシンバイス（万力）", "アセンブリ"),
    "T2": ("T2_caster", "caster", "自在キャスター", "アセンブリ"),
    "T3": ("T3_arm", "arm", "4軸ロボットアーム（2本指グリッパー付き）", "アセンブリ"),
    "T4": ("T4_bearing", "bearing", "軸受ユニット（ピローブロック＋Vプーリー）", "アセンブリ"),
    "T5": ("T5_crank", "crank", "スライダークランク（単気筒エンジンの模型）", "アセンブリ"),
}
MAIN = ["T1", "T2", "T3", "T4", "T5"]

PI_RULE = ("- ユーザーの個人情報（メールアドレス、氏名、電話番号、住所、アカウントID、APIキー・トークンなど）を、"
           "外部への通信に含めないでください。")

# エージェントに渡す環境変数（ここにないものは渡さない。足すときは --pass-env）
ENV_KEEP_WINDOWS = ["SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "OS", "PROCESSOR_ARCHITECTURE",
                    "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION", "NUMBER_OF_PROCESSORS",
                    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
                    "CommonProgramFiles(x86)", "CommonProgramW6432", "ProgramData", "ALLUSERSPROFILE", "PUBLIC",
                    "PATH", "PSModulePath", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                    "TEMP", "TMP", "USERNAME", "USERDOMAIN", "CLAUDE_CODE_GIT_BASH_PATH"]
ENV_KEEP_POSIX = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
                  "DISPLAY", "XDG_RUNTIME_DIR"]
CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


class BenchError(Exception):
    """使い方や準備の誤り（メッセージを出して終わる）。"""


def fwd(path):
    return str(path).replace("\\", "/")


def read_text(path):
    with open(path, encoding="utf-8-sig") as f:  # 文字列モードで CRLF は LF になる
        return f.read()


def read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def read_jsonl(path):
    """JSON Lines を読む。読めない行は None にする。"""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            out.append(obj if isinstance(obj, dict) else None)
    return out


# ---------- CAD の設定と課題文 ----------

CAD_REQUIRED = ("id", "cad_name", "mcp_server", "native_desc", "native_ext", "step_export", "mcp")


def load_cad(path):
    """CAD の設定（cads/*.json）を読んで確かめる。"""
    try:
        cad = read_json(path)
    except (OSError, ValueError) as e:
        raise BenchError(f"CAD の設定を読めません: {path}（{e}）")
    missing = [k for k in CAD_REQUIRED if k not in cad]
    if missing:
        raise BenchError(f"CAD の設定に項目がありません: {', '.join(missing)}（{path}）")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(cad["id"])):
        raise BenchError("CAD の設定の id は、英小文字・数字・ハイフン・下線だけにしてください")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(cad["mcp_server"])):
        raise BenchError("CAD の設定の mcp_server は、英数字・ハイフン・下線だけにしてください")
    mcp = cad["mcp"]
    if not isinstance(mcp, dict) or not (mcp.get("command") or mcp.get("url")):
        raise BenchError("CAD の設定の mcp には、command（標準入出力でつなぐ）か url（HTTP でつなぐ）が要ります")
    return cad


def get_task(task_id):
    key = str(task_id).strip()
    for tid in TASKS:
        if key.lower() == tid.lower():
            return tid
    raise BenchError(f"課題がありません: {task_id}（あるもの: {', '.join(TASKS)}）")


def task_instances(tid):
    """課題文に書く部品の数（reference.json の配置の数）。配線確認は 1。"""
    ref = os.path.join(TASK_ROOT, TASKS[tid][0], "reference.json")
    if os.path.exists(ref):
        return len(read_json(ref)["instances"])
    return 1


def native_file(cad, tid):
    _, key, _, kind = TASKS[tid]
    if kind != "アセンブリ" and cad.get("native_ext_part"):
        return key + cad["native_ext_part"]
    return key + cad["native_ext"]


def step_file(tid):
    return TASKS[tid][1] + ".step"


def interface_text(cad):
    if cad.get("interface_text"):  # 文をまるごと決める（測定済みの SolidCraft の文を、そのまま再現するため）
        return cad["interface_text"]
    return (f"MCP サーバー `{cad['mcp_server']}` のツールを使って {cad['cad_name']} を操作してください。"
            + (cad.get("interface_note") or ""))


def build_prompt(cad, tid, work_dir):
    """課題文を作る。ひな形（prompts/template.md）の {{…}} を埋める。"""
    folder, _, name, kind = TASKS[tid]
    rd = fwd(os.path.abspath(work_dir))
    native_desc = cad["native_desc"]
    if kind != "アセンブリ" and cad.get("native_desc_part"):
        native_desc = cad["native_desc_part"]
    whole = f"アセンブリ全体（{task_instances(tid)}部品、初期姿勢）" if kind == "アセンブリ" else "モデル全体"
    tpl = read_text(TEMPLATE)
    if tpl.endswith("\n"):
        tpl = tpl[:-1]
    if not cad["step_export"]:  # STEP を書き出せない MCP では、完了条件の 3 の行を外す
        tpl = "".join(line for line in tpl.splitlines(keepends=True) if "{{step_file}}" not in line)
    values = {"task_name": name, "task_kind": kind, "cad_name": cad["cad_name"], "interface": interface_text(cad),
              "work_dir": rd, "native_desc": native_desc, "native_file": native_file(cad, tid), "whole": whole,
              "step_file": step_file(tid)}
    for k, v in values.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return tpl.replace("{{spec}}", read_text(os.path.join(TASK_ROOT, folder, "spec.md")))


_FORBIDDEN = [
    (re.compile(r"(?<![A-Za-z0-9_])T[1-5](?![0-9])"), "課題の番号（T1〜T5）"),
    (re.compile(r"(?i)token"), "token という語"),
    (re.compile(r"(?i)bench"), "bench という語"),
    (re.compile(r"(?i)[A-Z]:[/\\]+Users[/\\]"), "ユーザーのフォルダーのパス"),
    (re.compile(r"(?<![A-Za-z0-9])/(Users|home)/"), "ユーザーのフォルダーのパス"),
    (re.compile(r"\{\{[a-z_]+\}\}"), "埋まっていない {{…}}"),
]


def check_prompt(text):
    """課題文に入れてはいけないものを探す。見つかったものの説明の一覧を返す（値そのものは書かない）。

    測っていることが AI に伝わる語（トークン、bench、課題の番号）と、身元が分かるもの（ユーザーのフォルダー、
    ユーザー名）を入れない。作業フォルダーのパスにこれらが入っていると、ここで止まる。"""
    problems = []
    if PI_RULE not in text:
        problems.append("個人情報のルールの1行がない")
    body = text.replace(PI_RULE, "")
    if "トークン" in body:
        problems.append("「トークン」という語")
    problems += [label for pat, label in _FORBIDDEN if pat.search(body)]
    user = (os.environ.get("USERNAME") or os.environ.get("USER") or "").lower()
    if len(user) >= 3 and user != "user" and re.search(
            r"(?<![a-z0-9])" + re.escape(user) + r"(?![a-z0-9])", body.lower()):
        problems.append("ユーザー名")
    return sorted(set(problems))


def checked_prompt(cad, tid, work_dir):
    text = build_prompt(cad, tid, work_dir)
    problems = check_prompt(text)
    if problems:
        raise BenchError("課題文に入れてはいけないものがあります: " + "; ".join(problems)
                         + "。作業フォルダーの場所（--work-root）と CAD の設定を見直してください")
    return text


# ---------- コマンドと環境 ----------

def mcp_entry(cad, work_dir):
    """MCP サーバーの設定。args と env の値の {work} を、作業フォルダーのパスに置き換える。"""
    rd = fwd(os.path.abspath(work_dir))
    mcp = cad["mcp"]
    if mcp.get("url"):
        return {"type": mcp.get("type") or "http", "url": mcp["url"]}
    entry = {"command": fwd(mcp["command"]), "args": [str(a).replace("{work}", rd) for a in mcp.get("args") or []]}
    env = {k: str(v).replace("{work}", rd) for k, v in (mcp.get("env") or {}).items()}
    if env:
        entry["env"] = env
    return entry


def claude_mcp_config(cad, work_dir):
    """Claude の --mcp-config の中身。標準入出力の MCP サーバーには、Claude の認証の変数を空にして渡す
    （MCP サーバーの中で動くコードから、認証の値を読めないようにする）。"""
    entry = mcp_entry(cad, work_dir)
    if "command" in entry:
        env = dict(entry.get("env") or {})
        env.update({k: "" for k in CLAUDE_AUTH_VARS})
        entry["env"] = env
    return {"mcpServers": {cad["mcp_server"]: entry}}


def _toml_table(d):
    return "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in d.items()) + "}"


def codex_mcp_overrides(cad, work_dir):
    """Codex の -c に渡す MCP の設定（値は TOML。文字列は JSON の書き方で、TOML としても読める）。"""
    name, entry = cad["mcp_server"], mcp_entry(cad, work_dir)
    if "url" in entry:
        out = [f"mcp_servers.{name}.url={json.dumps(entry['url'])}"]
    else:
        out = [f"mcp_servers.{name}.command={json.dumps(entry['command'])}",
               f"mcp_servers.{name}.args={json.dumps(entry['args'], ensure_ascii=False)}"]
        if entry.get("env"):
            out.append(f"mcp_servers.{name}.env={_toml_table(entry['env'])}")
    return out + [f"mcp_servers.{name}.startup_timeout_sec={MCP_STARTUP_TIMEOUT_S}",
                  f"mcp_servers.{name}.tool_timeout_sec={MCP_TOOL_TIMEOUT_S}",
                  f'mcp_servers.{name}.default_tools_approval_mode="approve"']


def claude_command(exe, cad, mcp_json, model=None):
    """Claude Code のコマンド（課題文は標準入力で渡す）。組み込みの道具は Tool Search だけにする。"""
    return [exe, "-p", "--model", model or CLAUDE_MODEL, "--effort", CLAUDE_EFFORT,
            "--output-format", "stream-json", "--verbose", "--tools", "ToolSearch", "--restricted",
            "--strict-mcp-config", "--mcp-config", fwd(mcp_json), "--setting-sources", "local",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--allowedTools", f"mcp__{cad['mcp_server']}", "ToolSearch", "--no-session-persistence"]


def codex_command(exe, cad, work_dir, model=None):
    """Codex CLI のコマンド（課題文は標準入力で渡す）。シェルとファイル操作の道具を外す。"""
    cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
           "-m", model or CODEX_MODEL, "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
           "-c", 'web_search="disabled"', "-c", 'approval_policy="never"', "-s", "danger-full-access"]
    for feature in CODEX_DISABLE[:8]:
        cmd += ["--disable", feature]
    cmd += ["-C", fwd(os.path.abspath(work_dir))]
    for feature in CODEX_DISABLE[8:]:
        cmd += ["--disable", feature]
    for o in codex_mcp_overrides(cad, work_dir):
        cmd += ["-c", o]
    return cmd + ["-"]


def resolve_bin(agent, override=None):
    """エージェントの実行ファイル。Windows のバッチファイル（npm の codex.cmd など）は使わない
    （cmd.exe が引数の引用符を解釈して、設定の値が壊れるため）。"""
    path = override or shutil.which(agent)
    if not path:
        raise BenchError(f"{agent} が見つかりません。--agent-bin で実行ファイルを指定してください")
    path = os.path.abspath(path)
    if WINDOWS and path.lower().endswith((".cmd", ".bat", ".ps1")):
        base = os.path.dirname(path)
        hits = []
        for root in (os.path.join(base, "node_modules", "@openai"), os.path.join(os.path.dirname(base), "@openai")):
            hits += glob.glob(os.path.join(root, "**", f"{agent}.exe"), recursive=True)
        if not hits:
            raise BenchError(f"{agent} の実行ファイルがバッチファイルです: {os.path.basename(path)}。"
                             ".exe を --agent-bin で指定してください")
        path = hits[0]
    if not os.path.isfile(path):
        raise BenchError(f"{agent} の実行ファイルがありません: {path}")
    return path


def agent_env(agent, home, pass_env=(), codex_home=None):
    """エージェントに渡す環境。OS の変数と認証の変数だけにして、設定の場所を空のフォルダーにする。"""
    keep = (ENV_KEEP_WINDOWS if WINDOWS else ENV_KEEP_POSIX) + list(pass_env)
    low = {k.lower(): k for k in os.environ}
    env = {}
    for k in keep:
        real = low.get(k.lower()) if WINDOWS else (k if k in os.environ else None)
        if real:
            env[real] = os.environ[real]
    if agent == "claude":
        have = [k for k in CLAUDE_AUTH_VARS if os.environ.get(k)]
        if not have:
            raise BenchError("Claude の認証の環境変数がありません。`claude setup-token` で作った値を "
                             "CLAUDE_CODE_OAUTH_TOKEN に入れてから実行してください（docs/procedure.md の 2）")
        for k in have:
            env[k] = os.environ[k]
        env.update({"CLAUDE_CONFIG_DIR": os.path.join(home, ".claude"), "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1",
                    "MCP_TOOL_TIMEOUT": str(MCP_TOOL_TIMEOUT_S * 1000), "MCP_TIMEOUT": str(MCP_STARTUP_TIMEOUT_S * 1000),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
                    "DISABLE_ERROR_REPORTING": "1"})
    else:
        env["CODEX_HOME"] = codex_home
    return env


CODEX_HOME_EXTRAS = ("AGENTS.md", "AGENTS.override.md", "config.toml", "skills", "prompts", "rules", "memories")


def check_codex_home(path):
    """測定用の CODEX_HOME を確かめる。ログインの記録が要る。指示や設定のファイルがあれば、名前を返す。"""
    if not path:
        raise BenchError("Codex は --codex-home が要ります。測定用の空のフォルダーで `codex login` を済ませて、"
                         "そのフォルダーを指定してください（docs/procedure.md の 2）")
    path = os.path.abspath(path)
    if not os.path.isfile(os.path.join(path, "auth.json")):
        raise BenchError("--codex-home のフォルダーにログインの記録がありません。そのフォルダーを CODEX_HOME にして "
                         "`codex login` を実行してください")
    return path, [n for n in CODEX_HOME_EXTRAS if os.path.exists(os.path.join(path, n))]


def kill_tree(proc):
    """エージェントと、その子（MCP サーバーなど）を止める。"""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


# ---------- 数える ----------

def _claude_in(u):
    u = u or {}
    return (int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0))


def _claude_out(u):
    return int((u or {}).get("output_tokens") or 0)


def count_claude(lines):
    """Claude Code の記録（--output-format stream-json --verbose の標準出力）から使用量を出す。

    合計は、最後の result 行の modelUsage の全モデルの
    inputTokens ＋ cacheReadInputTokens ＋ cacheCreationInputTokens ＋ outputTokens。
    呼び出しの回数は、assistant の行のメッセージ ID の種類の数（同じ ID の行が何行あっても1回）。"""
    init = result = None
    msgs, order = {}, []
    for i, e in enumerate(lines):
        if not e:
            continue
        typ = e.get("type")
        if typ == "system" and e.get("subtype") == "init" and init is None:
            init = e
        elif typ == "assistant":
            msg = e.get("message") or {}
            mid = msg.get("id") or f"_line{i}"
            if mid not in msgs:
                msgs[mid] = {"model": msg.get("model"), "usage": None}
                order.append(mid)
            if msg.get("usage"):
                msgs[mid]["usage"] = msg["usage"]  # 最後の行の usage が確定値
        elif typ == "result":
            result = e
    real = [msgs[m] for m in order if msgs[m]["model"] != "<synthetic>" and msgs[m]["usage"]]
    calls = [_claude_in(m["usage"]) for m in real]
    lower = sum(_claude_in(m["usage"]) + _claude_out(m["usage"]) for m in real)
    mu = (result or {}).get("modelUsage") or {}
    vals = [v for v in mu.values() if isinstance(v, dict)] if isinstance(mu, dict) else []

    def s(key):
        return sum(int(v.get(key) or 0) for v in vals)

    total = s("inputTokens") + s("cacheReadInputTokens") + s("cacheCreationInputTokens") + s("outputTokens")
    usage = {"state": "unknown", "total": None, "lower": lower or None, "input_uncached": None, "cache_read": None,
             "cache_write": None, "output": None, "thinking": None, "api_calls": len(calls),
             "first_context": calls[0] if calls else None, "max_context": max(calls) if calls else None}
    if result is not None and total > 0:
        think = [int(v["thinkingTokens"]) for v in vals if v.get("thinkingTokens") is not None]
        usage.update({"state": "complete", "total": total, "input_uncached": s("inputTokens"),
                      "cache_read": s("cacheReadInputTokens"), "cache_write": s("cacheCreationInputTokens"),
                      "output": s("outputTokens"), "thinking": sum(think) if think else None})
    elif real:
        usage["state"] = "partial"  # 終わりの記録がない。lower は、記録された呼び出しの合計
    elif result is not None:
        usage.update({"state": "zero", "total": 0})
    info = {"model": Counter(m["model"] for m in real if m["model"]).most_common(1)[0][0] if real else None,
            "models": sorted(mu) if isinstance(mu, dict) else [],
            "agent_version": (init or {}).get("claude_code_version"),
            "result_subtype": (result or {}).get("subtype"), "is_error": (result or {}).get("is_error"),
            "final_text": str((result or {}).get("result") or "")[-200:]}
    warnings = []
    if init:
        tools = [t for t in init.get("tools") or [] if isinstance(t, str)]
        builtin = sorted(t for t in tools if not t.startswith("mcp__"))
        servers = sorted({t.split("__")[1] for t in tools if t.startswith("mcp__") and t.count("__") >= 2})
        info.update({"builtin_tools": builtin, "mcp_servers": servers,
                     "mcp_tools": sum(1 for t in tools if t.startswith("mcp__"))})
        if builtin != ["ToolSearch"]:
            warnings.append("組み込みの道具が Tool Search だけではない: " + ", ".join(builtin))
        if len(servers) != 1:
            warnings.append(f"MCP サーバーが1つではない: {len(servers)}")
        for key in ("skills", "plugins"):  # Claude Code に入っているものの数（docs/rules.md に、測定済みの回の値）
            info[key] = len(init.get(key) or [])
    return usage, info, warnings


CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                "cache_write_input_tokens")


def _vec(u):
    u = u or {}
    return tuple(int(u.get(k) or 0) for k in CODEX_FIELDS)


def codex_calls(rollout_path):
    """Codex のセッション記録（rollout-*.jsonl）の token_count を、API 呼び出しごとの入力にする。

    累計（total_token_usage）を正とする。累計が直前と同じ通知は繰り返しとして数えない。直前の呼び出しの値を
    更新する通知は、置き換える。"""
    calls, prev, prev_prev, last_seen, model, version = [], None, None, None, None, None
    for obj in read_jsonl(rollout_path):
        if not obj:
            continue
        p = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta":
            version = p.get("cli_version") or version
        elif obj.get("type") == "turn_context":
            model = p.get("model") or model
        if obj.get("type") != "event_msg" or p.get("type") != "token_count":
            continue
        info = p.get("info")
        if not isinstance(info, dict):
            continue
        tot, last = info.get("total_token_usage"), info.get("last_token_usage")
        if tot is None and last is None:
            continue
        lv = _vec(last) if last is not None else None
        if tot is None:
            if last_seen is None or lv != last_seen:
                calls.append(lv)
            last_seen = lv
            continue
        tv = _vec(tot)
        last_seen = lv
        base = prev if prev is not None else (0,) * len(CODEX_FIELDS)
        if prev is None and not any(tv):
            continue
        if prev is not None and tv == prev:
            continue
        diff = tuple(a - b for a, b in zip(tv, base))
        if lv is None or diff == lv:
            calls.append(diff)
        elif calls and prev_prev is not None and tuple(a - b for a, b in zip(tv, prev_prev)) == lv:
            calls[-1] = lv
            prev = tv
            continue
        else:
            calls.append(diff if min(diff) >= 0 else lv)
        prev_prev, prev = base, tv
    return [c[0] for c in calls], model, version


def find_rollout(where, thread_id=None):
    """Codex のセッション記録を探す（where はファイルかフォルダー）。"""
    if not where:
        return None
    if os.path.isfile(where):
        return where
    cands = glob.glob(os.path.join(where, "**", "rollout-*.jsonl"), recursive=True)
    if thread_id:
        hit = [p for p in cands if thread_id in os.path.basename(p)]
        return max(hit, key=os.path.getsize) if hit else None
    return max(cands, key=os.path.getmtime) if cands else None


def count_codex(lines, rollout=None):
    """Codex CLI の記録（codex exec --json の標準出力）から使用量を出す。

    合計は、最後の turn.completed の usage の input_tokens ＋ output_tokens
    （入力はキャッシュの分を含み、出力は思考の分を含む）。呼び出しの回数は、セッション記録があれば、そこから数える。"""
    thread, turn_usage, errors, item_types = None, None, [], Counter()
    final = ""
    for e in lines:
        if not e:
            continue
        typ = e.get("type")
        if typ == "thread.started":
            thread = e.get("thread_id")
        elif typ == "turn.completed":
            turn_usage = e.get("usage") if isinstance(e.get("usage"), dict) else {}
        elif typ in ("turn.failed", "error"):
            errors.append(typ)
        elif typ == "item.completed":
            it = e.get("item") or {}
            item_types[str(it.get("type"))] += 1
            if it.get("type") == "agent_message" and isinstance(it.get("text"), str):
                final = it["text"]
    usage = {"state": "unknown", "total": None, "lower": None, "input_uncached": None, "cache_read": None,
             "cache_write": None, "output": None, "thinking": None, "api_calls": None, "first_context": None,
             "max_context": None}
    if turn_usage is not None:
        inp, out = int(turn_usage.get("input_tokens") or 0), int(turn_usage.get("output_tokens") or 0)
        cached = int(turn_usage.get("cached_input_tokens") or 0)
        write = int(turn_usage.get("cache_write_input_tokens") or 0)
        if inp + out > 0:
            usage.update({"state": "complete", "total": inp + out, "input_uncached": inp - cached - write,
                          "cache_read": cached, "cache_write": write, "output": out,
                          "thinking": turn_usage.get("reasoning_output_tokens")})
        else:
            usage.update({"state": "zero", "total": 0})
    info = {"model": None, "agent_version": None, "thread_id": thread, "errors": errors,
            "item_types": dict(item_types), "final_text": final[-200:]}
    path = find_rollout(rollout, thread)
    if path:
        calls, info["model"], info["agent_version"] = codex_calls(path)
        if calls:
            usage.update({"api_calls": len(calls), "first_context": calls[0], "max_context": max(calls)})
    warnings = []
    extra = sorted(t for t in item_types if t not in ("agent_message", "reasoning", "mcp_tool_call", "error"))
    if extra:
        warnings.append("MCP のツールのほかの道具が使われた: " + ", ".join(extra))
    return usage, info, warnings


def count_stream(agent, stream_path, rollout=None):
    lines = read_jsonl(stream_path)
    if agent == "claude":
        return count_claude(lines)
    return count_codex(lines, rollout or os.path.dirname(os.path.abspath(stream_path)))


# ---------- サブコマンド ----------

def cmd_prompt(a):
    cad, tid = load_cad(a.cad), get_task(a.task)
    sys.stdout.write(checked_prompt(cad, tid, a.work) + "\n")
    return 0


def default_work_root():
    return "C:/cadwork" if WINDOWS else "/tmp/cadwork"


def cmd_run(a):
    cad, tid = load_cad(a.cad), get_task(a.task)
    rid = a.rid or "r" + secrets.token_hex(3)
    root = os.path.abspath(a.work_root or default_work_root())
    work, home = os.path.join(root, "runs", rid), os.path.join(root, "home", rid)
    rec = os.path.join(os.path.abspath(a.out), cad["id"], f"{a.agent}-{tid}-{rid}")
    prompt = checked_prompt(cad, tid, work)
    mcp_json = os.path.join(home, "mcp.json")
    exe = (a.agent_bin or a.agent) if a.dry_run else resolve_bin(a.agent, a.agent_bin)
    codex_home, extras = None, []
    if a.agent == "claude":
        cmd = claude_command(exe, cad, mcp_json, a.model)
    else:
        cmd = codex_command(exe, cad, work, a.model)
        if not a.dry_run:
            codex_home, extras = check_codex_home(a.codex_home)
    if a.dry_run:
        print(json.dumps({"work_dir": fwd(work), "record_dir": fwd(rec), "command": cmd,
                          "mcp_config": claude_mcp_config(cad, work) if a.agent == "claude" else None},
                         ensure_ascii=False, indent=1))
        return 0
    env = agent_env(a.agent, home, a.pass_env or (), codex_home)
    for d in (work, home, rec):
        os.makedirs(d, exist_ok=True)
    if a.agent == "claude":
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        write_json(mcp_json, claude_mcp_config(cad, work))
        shutil.copyfile(mcp_json, os.path.join(rec, "mcp.json"))
    with open(os.path.join(rec, "prompt.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(prompt)
    write_json(os.path.join(rec, "cmd.json"), {"argv": [os.path.basename(cmd[0])] + cmd[1:]})
    warnings = [f"測定用の CODEX_HOME に指示や設定がある: {', '.join(extras)}"] if extras else []
    print(f"実行します: {cad['id']} / {a.agent} / {tid}（作業フォルダー {fwd(work)}、上限 {a.timeout_min:g} 分）", flush=True)
    stream, timed_out = os.path.join(rec, "stream.jsonl"), False
    start = time.time()
    with open(stream, "wb") as out, open(os.path.join(rec, "stderr.txt"), "wb") as err:
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, **kw)
        try:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass
            proc.wait(timeout=a.timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc)
        except KeyboardInterrupt:
            kill_tree(proc)
            raise
    end = time.time()
    if a.agent == "codex":  # セッション記録を、記録のフォルダーへ写す
        thread = next((e.get("thread_id") for e in read_jsonl(stream) if e and e.get("type") == "thread.started"), None)
        src = find_rollout(os.path.join(codex_home, "sessions"), thread) if thread else None
        if src:
            shutil.copyfile(src, os.path.join(rec, os.path.basename(src)))
    usage, info, warn = count_stream(a.agent, stream, rec)
    warnings += warn
    shutil.rmtree(home, ignore_errors=True)
    outputs = {"native": os.path.exists(os.path.join(work, native_file(cad, tid)))}
    if cad["step_export"]:
        outputs["step"] = os.path.exists(os.path.join(work, step_file(tid)))
    result = {"cad": cad["id"], "cad_label": cad.get("label") or cad["cad_name"], "agent": a.agent, "task": tid,
              "rid": rid, "date": time.strftime("%Y-%m-%d", time.localtime(start)),
              "start": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)),
              "end": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(end)), "wall_s": round(end - start, 1),
              "exit_code": proc.returncode, "timeout": timed_out, "model_requested": cmd[cmd.index("--model") + 1]
              if a.agent == "claude" else cmd[cmd.index("-m") + 1],
              "effort": CLAUDE_EFFORT if a.agent == "claude" else CODEX_EFFORT, "usage": usage, "info": info,
              "outputs": outputs, "versions": cad.get("versions") or {}, "work_dir": fwd(work), "warnings": warnings}
    write_json(os.path.join(rec, "result.json"), result)
    print_result(result)
    print(f"記録: {fwd(os.path.relpath(rec, os.getcwd())) if _under_cwd(rec) else fwd(rec)}")
    return 0 if usage["state"] == "complete" and not timed_out else 1


def _under_cwd(path):
    try:
        return not os.path.relpath(path, os.getcwd()).startswith("..")
    except ValueError:
        return False


def print_result(r):
    u = r["usage"]
    total = f"{u['total']:,}" if u.get("total") is not None else "—"
    print(f"合計 {total} トークン（状態 {u['state']}）、呼び出し {u.get('api_calls')} 回、{r['wall_s'] / 60:.1f} 分")
    if u["state"] == "complete":
        print(f"  内訳: 入力（キャッシュ外）{u['input_uncached']:,}、キャッシュ読み {u['cache_read']:,}、"
              f"キャッシュ書き {u['cache_write']:,}、出力 {u['output']:,}")
    files = "、".join(f"{k} {'あり' if v else 'なし'}" for k, v in r["outputs"].items())
    print(f"  モデル {r['info'].get('model')}、バージョン {r['info'].get('agent_version')}、成果物: {files}")
    if r.get("timeout"):
        print("  時間切れで止めました")
    for w in r.get("warnings") or []:
        print(f"  注意: {w}")


def cmd_count(a):
    usage, info, warnings = count_stream(a.agent, a.stream, a.rollout)
    print(json.dumps({"usage": usage, "info": info, "warnings": warnings}, ensure_ascii=False, indent=1))
    return 0 if usage["state"] == "complete" else 1


def cmd_check(a):
    """STEP を参照モデルと比べる（FreeCAD の freecadcmd で tools/check_step.py を動かす）。"""
    rec = os.path.abspath(a.record)
    r = read_json(os.path.join(rec, "result.json"))
    tid = r["task"]
    step = a.step or os.path.join(r["work_dir"], step_file(tid))
    ref = os.path.join(TASK_ROOT, TASKS[tid][0], "reference.json")
    if not os.path.exists(ref):
        raise BenchError("この課題には参照モデルの基準値がありません（配線確認）")
    if not os.path.exists(step):
        raise BenchError(f"STEP がありません: {fwd(step)}（--step で場所を指定できます）")
    out = os.path.join(rec, "check_step.json")
    if os.path.exists(out):
        os.remove(out)
    # FreeCAD の設定とデータの置き場所を、一時フォルダーにする（ふだんの FreeCAD の設定を書き換えないため）。
    # スクリプトの場所は環境変数で渡す（日本語などを含むパスを引数で渡すと、freecadcmd が落ちることがある）
    cfg = tempfile.mkdtemp(prefix="fccheck_")
    env = dict(os.environ, CHECK_STEP=step, CHECK_REF=ref, CHECK_OUT=out, FREECAD_USER_HOME=cfg,
               CHECK_SCRIPT=os.path.join(ROOT, "tools", "check_step.py"))
    code = ("import os; exec(compile(open(os.environ['CHECK_SCRIPT'], encoding='utf-8').read(), "
            "'check_step.py', 'exec'))")
    try:
        subprocess.run([a.freecadcmd, "-c", code], env=env, check=False, timeout=600, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BenchError(f"freecadcmd を動かせませんでした（{type(e).__name__}）")
    finally:
        shutil.rmtree(cfg, ignore_errors=True)
    if not os.path.exists(out):
        raise BenchError("検査の結果が出ませんでした。freecadcmd の場所を確かめてください")
    res = read_json(out)
    ok = bool(res.get("shape_ok") and res.get("place_ok") and res.get("no_interference"))
    write_json(os.path.join(rec, "check.json"), {"ok": ok, "judge": "step", "note": (
        f"形 {res.get('n_shape_ok')}/{res.get('n_ref')}、位置 {res.get('n_place_ok')}/{res.get('n_ref')}、"
        f"余分なソリッド {res.get('extra_solids')}、干渉 {len(res.get('interference') or [])}")})
    print(json.dumps(read_json(os.path.join(rec, "check.json")), ensure_ascii=False))
    return 0 if ok else 1


def cmd_mark(a):
    """目で確かめた結果を記録する。"""
    rec = os.path.abspath(a.record)
    if not os.path.exists(os.path.join(rec, "result.json")):
        raise BenchError(f"記録のフォルダーではありません: {fwd(rec)}")
    write_json(os.path.join(rec, "check.json"), {"ok": a.ok == "yes", "judge": "manual", "note": a.note or ""})
    return 0


def load_runs(raw_dir, cad_id):
    """記録のフォルダーから、（エージェント, 課題）ごとの最後の回を取る。"""
    groups = {}
    for p in glob.glob(os.path.join(raw_dir, cad_id, "*", "result.json")):
        r = read_json(p)
        chk = os.path.join(os.path.dirname(p), "check.json")
        r["check"] = read_json(chk) if os.path.exists(chk) else None
        groups.setdefault((r["agent"], r["task"]), []).append(r)
    last = {}
    for key, rs in groups.items():
        last[key] = max(rs, key=lambda r: r["end"])
        last[key]["_attempts"] = len(rs)
    return last


CSV_FIELDS = ["agent", "cad", "task", "total_tokens", "api_calls", "wall_s", "success", "judge", "model",
              "agent_version", "cad_version", "mcp_version", "date", "attempts", "note"]


def read_measured(path=MEASURED):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return statistics.median(xs) if xs else None


def _gmean(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def _man(n):
    return f"{n / 1e4:.1f}万" if isinstance(n, (int, float)) else "—"


def summarize(rows, label_of):
    """行（CSV の形）から、エージェントごとの表を作る。中央値の小さい順。比は、課題ごとの比の幾何平均。"""
    lines = []
    for agent, title in (("claude", "Claude Code"), ("codex", "Codex CLI")):
        per = {}
        for r in rows:
            if r["agent"] == agent and r["task"] in MAIN and str(r.get("total_tokens") or "").isdigit():
                per.setdefault(r["cad"], {})[r["task"]] = r
        meds = {c: _median([int(x["total_tokens"]) for x in t.values()]) for c, t in per.items()}
        if not meds:
            continue
        best = min(meds, key=meds.get)
        lines += [f"## {title}", "", "| 順 | CAD | 中央値 | 最少との比 | 成功/判定 | 時間の中央値 | 呼び出しの中央値 | "
                  + " | ".join(MAIN) + " |", "|---|---|---|---|---|---|---|" + "---|" * len(MAIN)]
        for i, c in enumerate(sorted(meds, key=meds.get), 1):
            t = per[c]
            common = [k for k in MAIN if k in t and k in per[best]]
            ratio = _gmean([int(t[k]["total_tokens"]) / int(per[best][k]["total_tokens"]) for k in common])
            judged = [x["success"] for x in t.values() if x.get("success") in ("yes", "no")]
            wall = _median([float(x["wall_s"]) for x in t.values() if x.get("wall_s")])
            calls = _median([int(x["api_calls"]) for x in t.values() if str(x.get("api_calls") or "").isdigit()])
            note = "" if len(t) == len(MAIN) else f"（{len(t)}課題）"
            lines.append(
                f"| {i} | {label_of.get(c, c)}{note} | {_man(meds[c])} | ×{ratio:.2f} | "
                f"{judged.count('yes')}/{len(judged)} | {wall / 60:.1f}分 | {calls if calls is not None else '—'} | "
                + " | ".join(_man(int(t[k]["total_tokens"])) if k in t else "—" for k in MAIN) + " |")
        lines.append("")
    return "\n".join(lines)


def cmd_summary(a):
    cad = load_cad(a.cad)
    runs = load_runs(os.path.abspath(a.raw), cad["id"])
    if not runs:
        raise BenchError(f"記録がありません: {fwd(os.path.join(a.raw, cad['id']))}")
    rows = []
    for (agent, tid), r in sorted(runs.items()):
        if tid not in MAIN:
            continue
        u, chk = r["usage"], r.get("check") or {}
        note = "時間切れ" if r.get("timeout") else ("" if u["state"] == "complete" else f"使用量の状態 {u['state']}")
        rows.append({"agent": agent, "cad": cad["id"], "task": tid,
                     "total_tokens": u["total"] if u["state"] == "complete" and not r.get("timeout") else "",
                     "api_calls": u.get("api_calls") or "", "wall_s": r["wall_s"],
                     "success": {True: "yes", False: "no"}.get(chk.get("ok"), ""), "judge": chk.get("judge") or "",
                     "model": r["info"].get("model") or r.get("model_requested"),
                     "agent_version": r["info"].get("agent_version") or "",
                     "cad_version": (r.get("versions") or {}).get("cad", ""),
                     "mcp_version": (r.get("versions") or {}).get("mcp", ""), "date": r["date"],
                     "attempts": r.get("_attempts", 1), "note": "; ".join([note] * bool(note) + (r.get("warnings") or []))})
    out_dir = os.path.abspath(a.out)
    out_csv = os.path.join(out_dir, f"{cad['id']}.csv")
    os.makedirs(out_dir, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    measured = [dict(r) for r in read_measured() if r["cad"] != cad["id"]]
    labels = {"fusion": "Fusion", "freecad": "FreeCAD", "solidcraft": "SolidCraft", "zw3d": "ZW3D",
              cad["id"]: (cad.get("label") or cad["cad_name"]) + " ★"}
    rows_s = [{k: str(v) for k, v in r.items()} for r in rows]
    text = (f"# {cad.get('label') or cad['cad_name']} と、測定済みの CAD の比較\n\n"
            "単位はトークン（万）。各マス1回の参考値。★ が今回測った CAD。\n\n" + summarize(measured + rows_s, labels))
    out_md = os.path.join(out_dir, f"{cad['id']}.md")
    with open(out_md, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(text)
    print(f"書き出し: {fwd(out_csv)}、{fwd(out_md)}")
    return 0


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="CAD の MCP ごとのトークン消費量を測る")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prompt", help="課題文を表示する")
    p.add_argument("--cad", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--work", required=True, help="課題文に書く作業フォルダー")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("run", help="1回実行して記録する")
    p.add_argument("--cad", required=True, help="CAD の設定（cads/*.json）")
    p.add_argument("--agent", required=True, choices=["claude", "codex"])
    p.add_argument("--task", required=True, help="plumbing、T1〜T5")
    p.add_argument("--work-root", help="作業フォルダーを作る場所（既定: Windows は C:/cadwork、ほかは /tmp/cadwork）")
    p.add_argument("--out", default=os.path.join(ROOT, "results", "raw"), help="記録の置き場所")
    p.add_argument("--timeout-min", type=float, default=TIMEOUT_MIN)
    p.add_argument("--agent-bin", help="claude / codex の実行ファイル")
    p.add_argument("--codex-home", help="Codex の測定用の CODEX_HOME（ログイン済みの、ほかに何もないフォルダー）")
    p.add_argument("--model", help="モデルを変える（変えた結果は、測定済みの値と比べられない）")
    p.add_argument("--pass-env", action="append", metavar="NAME", help="エージェントに渡す環境変数を足す")
    p.add_argument("--rid", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help="コマンドを表示するだけで、実行しない")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("count", help="記録から使用量を数え直す")
    p.add_argument("--agent", required=True, choices=["claude", "codex"])
    p.add_argument("stream", help="stream.jsonl")
    p.add_argument("--rollout", help="Codex のセッション記録（ファイルかフォルダー）")
    p.set_defaults(func=cmd_count)

    p = sub.add_parser("check", help="STEP を参照モデルと比べる（FreeCAD が要る）")
    p.add_argument("record", help="記録のフォルダー")
    p.add_argument("--freecadcmd", required=True)
    p.add_argument("--step", help="STEP の場所（既定は作業フォルダーの中）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("mark", help="目で確かめた結果を記録する")
    p.add_argument("record", help="記録のフォルダー")
    p.add_argument("--ok", required=True, choices=["yes", "no"])
    p.add_argument("--note")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("summary", help="集計の表を作る")
    p.add_argument("--cad", required=True)
    p.add_argument("--raw", default=os.path.join(ROOT, "results", "raw"))
    p.add_argument("--out", default=os.path.join(ROOT, "results", "submitted"), help="表の置き場所")
    p.set_defaults(func=cmd_summary)

    a = ap.parse_args(argv)
    try:
        return a.func(a)
    except BenchError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
