"""ZIPファイルがClaude.aiのエクスポート形式かどうかを多段階で検証する。

watch_downloads.ps1 から呼ばれる。標準出力に1行のJSONを返す:
    {"valid": true, "reason": "ok", "conversationCount": 27}
    {"valid": false, "reason": "..."}

usage: python validate_export_zip.py <zip_path>
終了コード: 検証自体が完了すれば（結果がvalid/invalidいずれでも）0。
           引数不足・ファイル不在など、検証を実行できなかった場合のみ2。
"""

import json
import sys
import zipfile


def result(valid: bool, reason: str, conversation_count: int | None = None) -> dict:
    out = {"valid": valid, "reason": reason}
    if conversation_count is not None:
        out["conversationCount"] = conversation_count
    return out


def validate(zip_path: str) -> dict:
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        return result(False, "not a valid zip file")

    names = set(z.namelist())
    if "conversations.json" not in names or "users.json" not in names:
        return result(False, "missing conversations.json or users.json in zip root")

    try:
        conversations = json.loads(z.read("conversations.json"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return result(False, "conversations.json is not valid JSON")

    if not isinstance(conversations, list):
        return result(False, "conversations.json is not a JSON array")

    if len(conversations) > 0:
        first = conversations[0]
        if not isinstance(first, dict) or "chat_messages" not in first:
            return result(
                False,
                "conversations.json entries lack 'chat_messages' key "
                "(not Claude.ai export format)",
            )

    return result(True, "ok", len(conversations))


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps(result(False, "usage: validate_export_zip.py <zip_path>")))
        return 2

    zip_path = sys.argv[1]
    try:
        out = validate(zip_path)
    except FileNotFoundError:
        print(json.dumps(result(False, f"file not found: {zip_path}")))
        return 2
    except Exception as e:  # noqa: BLE001 - any unexpected error is reported as invalid
        print(json.dumps(result(False, f"unexpected error during validation: {e}")))
        return 0

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
