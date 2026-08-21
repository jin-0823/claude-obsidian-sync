"""Claude Code のローカル会話ログ (~/.claude/projects/**/*.jsonl) を
Obsidian Vault 用の日次 Markdown ファイルに変換する。

設計方針:
  - 出力は「その日 (JST) のログを対象ファイルから全件読み直して上書き」する。
    追記や uuid 差分管理はしない。再実行しても同じ入力からは同じ出力になるため、
    重複や取りこぼしが構造的に起きない (冪等性は再生成で担保する)。
  - デフォルトの対象日は「昨日 (JST)」。cron/タスクスケジューラで毎日 0:00 に
    走らせる想定 (0:00 に前日分を確定させる)。
  - jsonl の type は user/assistant 以外にも queue-operation, attachment,
    last-prompt, custom-title, ai-title, system 等が混在する。会話として
    扱うのは user/assistant のみ。既知でも未知でもない type が出てきた場合は
    無視した上で実行ログに警告を残す (KNOWN_IGNORED_TYPES に無い type)。
  - isSidechain (サブエージェント内部のやり取り) はデフォルトで除外する。
    メインの会話の流れをジャーナルとして読みたいため。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_VAULT_OUTPUT_DIR = (
    Path.home() / "Projects" / "obsidian-vault" / "claude-code-logs"
)

CONVERSATION_TYPES = {"user", "assistant"}
KNOWN_IGNORED_TYPES = {
    "queue-operation",
    "attachment",
    "last-prompt",
    "custom-title",
    "ai-title",
    "system",
    "file-history-delta",
    "file-history-snapshot",
    "mode",
    "permission-mode",
    "teleported-from",
}

TOOL_RESULT_MAX_CHARS = 1000
TOOL_INPUT_SUMMARY_MAX_CHARS = 200


class RunLog:
    """実行ログを行のリストとして貯め、最後にファイルへ書く。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warning_types_seen: set[str] = set()
        self.parse_errors = 0
        self.had_warning = False

    def info(self, msg: str) -> None:
        self.lines.append(f"[INFO] {msg}")

    def warn(self, msg: str) -> None:
        self.had_warning = True
        self.lines.append(f"[WARN] {msg}")

    def error(self, msg: str) -> None:
        self.lines.append(f"[ERROR] {msg}")

    def dump(self) -> str:
        return "\n".join(self.lines) + "\n"


def iter_session_files(projects_dir: Path):
    if not projects_dir.exists():
        return
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            yield project_dir.name, jsonl_path


def decode_project_folder_name(folder_name: str) -> str:
    """`C--Users-jinai-Projects` のようなフォルダ名を推定パスに戻す (表示補助用)。
    実際のパスは行ごとの `cwd` の方が正確なので、cwd が取れない場合のフォールバック。
    """
    if folder_name.startswith("C--"):
        rest = folder_name[3:]
        return "C:\\" + rest.replace("-", "\\")
    return folder_name


def to_jst(timestamp_str: str) -> datetime:
    # 例: "2026-08-20T07:52:25.146Z"
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    return dt.astimezone(JST)


def summarize_tool_input(name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input)[:TOOL_INPUT_SUMMARY_MAX_CHARS]
    for key in ("command", "file_path", "pattern", "path", "url", "prompt"):
        if key in tool_input:
            val = str(tool_input[key])
            if len(val) > TOOL_INPUT_SUMMARY_MAX_CHARS:
                val = val[:TOOL_INPUT_SUMMARY_MAX_CHARS] + "…"
            return val
    raw = json.dumps(tool_input, ensure_ascii=False)
    if len(raw) > TOOL_INPUT_SUMMARY_MAX_CHARS:
        raw = raw[:TOOL_INPUT_SUMMARY_MAX_CHARS] + "…"
    return raw


def tool_result_content_to_text(content) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(str(block))
        text = "\n".join(parts)
    else:
        text = str(content)
    if len(text) > TOOL_RESULT_MAX_CHARS:
        text = text[:TOOL_RESULT_MAX_CHARS] + f"\n…(truncated, {len(text)} chars total)"
    return text


def extract_blocks(content, run_log: RunLog, include_tools: bool):
    """message.content (str または content-block のリスト) を
    [(kind, text), ...] に変換する。kind: text / tool_use / tool_result / image / unknown
    """
    if isinstance(content, str):
        if content.strip():
            return [("text", content)]
        return []

    if not isinstance(content, list):
        run_log.warn(f"unexpected content type: {type(content)!r}")
        return []

    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text.strip():
                blocks.append(("text", text))
        elif btype == "thinking":
            continue  # 内部思考は出力しない
        elif btype == "tool_use":
            if not include_tools:
                continue
            name = block.get("name", "?")
            summary = summarize_tool_input(name, block.get("input", {}))
            blocks.append(("tool_use", f"{name}: {summary}"))
        elif btype == "tool_result":
            if not include_tools:
                continue
            text = tool_result_content_to_text(block.get("content"))
            blocks.append(("tool_result", text))
        elif btype == "image":
            if include_tools:
                blocks.append(("image", "[image]"))
        else:
            run_log.warning_types_seen.add(f"content_block:{btype}")
            blocks.append(("unknown", f"[unknown content block: {btype}]"))
    return blocks


def render_message(role: str, ts_jst: datetime, blocks) -> str:
    kinds = {kind for kind, _ in blocks}
    if role == "assistant":
        label = "🤖 Claude"
    elif kinds and kinds.issubset({"tool_result", "image"}):
        label = "⚙️ Tool Result"
    else:
        label = "🧑 User"
    time_str = ts_jst.strftime("%H:%M:%S")
    out = [f"**{label}** `{time_str}`", ""]
    for kind, text in blocks:
        if kind == "text":
            out.append(text.rstrip())
            out.append("")
        elif kind == "tool_use":
            out.append(f"<details><summary>🔧 {text.splitlines()[0][:120]}</summary>\n")
            out.append("```")
            out.append(text)
            out.append("```")
            out.append("</details>")
            out.append("")
        elif kind == "tool_result":
            out.append("<details><summary>📄 tool result</summary>\n")
            out.append("```")
            out.append(text)
            out.append("```")
            out.append("</details>")
            out.append("")
        elif kind == "image":
            out.append("_[image]_")
            out.append("")
        else:
            out.append(f"_{text}_")
            out.append("")
    return "\n".join(out)


def collect_records(projects_dir: Path, run_log: RunLog, include_tools: bool, include_sidechain: bool):
    """全セッションファイルを読み、date(JST) -> project -> session_id -> [record] にまとめる。
    record は (timestamp_jst, role, rendered_markdown) のタプル。
    """
    by_date: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    session_project_display: dict[str, str] = {}

    total_messages = 0
    total_sessions = 0

    for folder_name, jsonl_path in iter_session_files(projects_dir):
        session_id = jsonl_path.stem
        touched = False
        with jsonl_path.open(encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as e:
                    run_log.parse_errors += 1
                    run_log.warn(f"{jsonl_path.name}:{lineno} JSON parse error: {e}")
                    continue

                obj_type = obj.get("type")
                if obj_type not in CONVERSATION_TYPES:
                    if obj_type not in KNOWN_IGNORED_TYPES:
                        run_log.warning_types_seen.add(f"line_type:{obj_type}")
                    continue

                if obj.get("isSidechain") and not include_sidechain:
                    continue

                timestamp_str = obj.get("timestamp")
                if not timestamp_str:
                    run_log.warn(f"{jsonl_path.name}:{lineno} missing timestamp, skipped")
                    continue
                ts_jst = to_jst(timestamp_str)
                date_key = ts_jst.strftime("%Y-%m-%d")

                message = obj.get("message", {})
                role = message.get("role", obj_type)
                content = message.get("content")
                blocks = extract_blocks(content, run_log, include_tools)
                if not blocks:
                    continue

                cwd = obj.get("cwd")
                if cwd:
                    session_project_display[session_id] = cwd
                elif session_id not in session_project_display:
                    session_project_display[session_id] = decode_project_folder_name(folder_name)

                rendered = render_message(role, ts_jst, blocks)
                by_date[date_key][folder_name][session_id].append((ts_jst, rendered))
                total_messages += 1
                touched = True

        if touched:
            total_sessions += 1

    return by_date, session_project_display, total_messages, total_sessions


def render_day_markdown(date_key: str, day_data: dict, session_project_display: dict) -> str:
    projects = sorted(day_data.keys())
    session_count = sum(len(sessions) for sessions in day_data.values())
    message_count = sum(
        len(records) for sessions in day_data.values() for records in sessions.values()
    )
    project_names = sorted(
        {session_project_display.get(sid, folder) for folder, sessions in day_data.items() for sid in sessions}
    )

    generated_at = datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    lines = []
    lines.append("---")
    lines.append(f"date: {date_key}")
    lines.append("type: claude-code-log")
    lines.append("projects:")
    for name in project_names:
        escaped = name.replace('"', '\\"')
        lines.append(f'  - "{escaped}"')
    lines.append(f"session_count: {session_count}")
    lines.append(f"message_count: {message_count}")
    lines.append(f"generated_at: {generated_at}")
    lines.append("---")
    lines.append("")
    lines.append(f"# Claude Code ログ — {date_key}")
    lines.append("")

    for folder_name in projects:
        sessions = day_data[folder_name]
        first_session_id = next(iter(sessions))
        display_name = session_project_display.get(first_session_id, folder_name)
        lines.append(f"## 📁 {display_name}")
        lines.append("")

        sorted_sessions = sorted(
            sessions.items(), key=lambda kv: min(ts for ts, _ in kv[1])
        )
        for session_id, records in sorted_sessions:
            records_sorted = sorted(records, key=lambda r: r[0])
            start = records_sorted[0][0].strftime("%H:%M")
            end = records_sorted[-1][0].strftime("%H:%M")
            short_id = session_id.split("-")[0]
            lines.append(f"### セッション `{short_id}` ({start}–{end})")
            lines.append("")
            for _, rendered in records_sorted:
                lines.append(rendered)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--date",
        action="append",
        dest="dates",
        help="対象日 (YYYY-MM-DD, JST)。複数指定可。省略時は昨日 (JST)。",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="ログに存在する全ての日付を対象にする (初回バックフィル用)。",
    )
    p.add_argument(
        "--today",
        action="store_true",
        help="今日 (JST) も対象に含める (通常は日が確定していないので除外)。",
    )
    p.add_argument(
        "--no-tools",
        action="store_true",
        help="tool_use / tool_result を出力から除外し、発話テキストのみにする。",
    )
    p.add_argument(
        "--include-sidechain",
        action="store_true",
        help="サブエージェント (isSidechain=true) のやり取りも含める。",
    )
    p.add_argument(
        "--projects-dir",
        type=Path,
        default=CLAUDE_PROJECTS_DIR,
        help="~/.claude/projects のパス (デフォルト: %(default)s)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_VAULT_OUTPUT_DIR,
        help="出力先ディレクトリ (デフォルト: %(default)s)",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    run_log = RunLog()
    started = datetime.now(JST)
    run_log.info(f"start: {started.isoformat()}")
    run_log.info(f"projects_dir: {args.projects_dir}")
    run_log.info(f"output_dir: {args.output_dir}")

    include_tools = not args.no_tools

    by_date, session_project_display, total_messages, total_sessions = collect_records(
        args.projects_dir, run_log, include_tools, args.include_sidechain
    )
    run_log.info(f"scanned sessions: {total_sessions}, conversation messages: {total_messages}")

    today_key = datetime.now(JST).strftime("%Y-%m-%d")
    yesterday_key = (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.all:
        target_dates = sorted(by_date.keys())
    elif args.dates:
        target_dates = sorted(set(args.dates))
    else:
        target_dates = [yesterday_key]

    if not args.today and not args.dates and not args.all:
        pass  # yesterday only, already excludes today by construction
    elif not args.today:
        target_dates = [d for d in target_dates if d != today_key]

    run_log.info(f"target dates: {target_dates}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for date_key in target_dates:
        day_data = by_date.get(date_key)
        if not day_data:
            run_log.info(f"{date_key}: no conversation messages found, skipping file write")
            continue
        markdown = render_day_markdown(date_key, day_data, session_project_display)
        out_path = args.output_dir / f"{date_key}.md"
        out_path.write_text(markdown, encoding="utf-8")
        session_count = sum(len(s) for s in day_data.values())
        message_count = sum(len(r) for s in day_data.values() for r in s.values())
        run_log.info(
            f"{date_key}: wrote {out_path} (sessions={session_count}, messages={message_count})"
        )
        written += 1

    if run_log.warning_types_seen:
        run_log.warn(
            "unrecognized types encountered (ignored): " + ", ".join(sorted(run_log.warning_types_seen))
        )
    if run_log.parse_errors:
        run_log.warn(f"total JSON parse errors: {run_log.parse_errors}")

    finished = datetime.now(JST)
    run_log.info(f"done: {finished.isoformat()} (elapsed {(finished - started).total_seconds():.1f}s)")
    run_log.info(f"files written: {written}")

    sys.stdout.write(run_log.dump())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
