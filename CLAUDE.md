# claude-obsidian-sync

Claude関連の会話ログをObsidian Vault (`../obsidian-vault`) に自動同期する2つのパイプライン。

- **パイプラインB**: Claude Code のローカル会話ログ（`~/.claude/projects/**/*.jsonl`）を
  毎日 `claude-code-logs/YYYY-MM-DD.md` に変換して書き出す。
- **パイプラインA**: claude.ai の会話エクスポートZIP（仁さんが手動でExport dataをクリックして
  Downloadsフォルダに保存したもの）を検知し、[nexus-ai-chat-importer](https://github.com/Superkikim/nexus-ai-chat-importer)
  のCLIで `Nexus/Conversations/` 以下に取り込む。仁さんの手動作業はExport dataのクリックのみ。

## 使用言語 / 前提

- Python 3.14（標準ライブラリのみ、追加パッケージ不要）
- PowerShell 5.1（タスクスケジューラから実行する想定）
- Node.js 18+（パイプラインAのnexus-cliに必要。動作確認はNode 24.19.0）

## 構成

```
claude-obsidian-sync\
├── src\
│   ├── export_claude_code.py    # パイプラインB本体。jsonl → Obsidian Markdown 変換
│   └── validate_export_zip.py   # パイプラインA。ZIPがClaude.aiエクスポートか多段階検証
├── run_daily.ps1                 # パイプラインB エントリポイント（タスクスケジューラ用）
├── watch_downloads.ps1           # パイプラインA エントリポイント（タスクスケジューラ用）
├── run_import.ps1                # パイプラインA。nexus-cli呼び出し本体
├── nexus_cli_window_moment_shim.js # パイプラインA。nexus-cliのwindow.momentバグ回避シム（下記2.参照）
├── cli\                          # nexus-ai-chat-importer のclone（.gitignore対象、要ビルド）
├── archive\                      # パイプラインAで処理済みのZIP移動先（.gitignore対象）
├── state\
│   └── processed_zips.json       # パイプラインA。SHA256ハッシュで二重処理を防止（.gitignore対象）
└── logs\                         # 実行ログ。B: run_*.log, A: watch_*.log / import_*[_raw].log
```

## よく使うコマンド

```powershell
# パイプラインB（Claude Code ログ同期）
python src\export_claude_code.py                  # 前日分(JST)だけ処理
python src\export_claude_code.py --date 2026-08-15 # 特定日を再生成
python src\export_claude_code.py --all             # 全日付を再生成
.\run_daily.ps1                                     # タスクスケジューラと同じ経路

# パイプラインA（claude.ai エクスポート取り込み）
.\watch_downloads.ps1                               # Downloadsを見て候補ZIPを全部処理
.\run_import.ps1 -ZipPath "C:\path\to\data-....zip" # 特定のZIPを1本だけ手動で取り込み
python src\validate_export_zip.py "C:\path\to\x.zip" # ZIPがClaude形式か単体で検証
```

## パイプラインA: nexus-cli のセットアップ（初回のみ・手動）

`cli\` は `.gitignore` 対象（GPLの外部ツールをclone+ビルドしたもので、コミットしない）。
新しい環境では以下を1回だけ実行する。

```powershell
git clone https://github.com/Superkikim/nexus-ai-chat-importer.git cli
cd cli
npm install --legacy-peer-deps
cd cli
npm install --legacy-peer-deps
npm run build
```

- `--legacy-peer-deps` が必須（本家 `package.json` の eslint系peer依存関係が
  eslint 10 と食い合っていてこれがないと `npm install` が失敗する。lintは使わないので実害なし）。
- clone直下にもう一段 `cli\cli\` があるのが正しい（リポジトリ本体＝プラグイン、
  その中の `cli\` サブフォルダ＝CLI）。ビルド後の実体は `cli\cli\dist\nexus-cli.js`。
- `run_import.ps1` はこのパスをハードコードして呼び出しているので、上記構成からずらさないこと。

## パイプラインA: 誤検知防止の仕組み

`watch_downloads.ps1` はDownloadsフォルダの全ファイルを見るが、無関係なZIP
（授業資料、ドライバ、ソフトのインストーラ等）を誤って取り込まないよう多段階でチェックする。

1. **ファイル名フィルタ**: `data-*.zip` または `*batch*.zip` に一致するものだけを候補にする。
   一致しないファイル（他の無関係なZIP）はハッシュ計算すら行わず、ログにも残さず素通りする。
2. **ZIP構造チェック** (`src\validate_export_zip.py`): `conversations.json` と `users.json` が
   ZIPルートに存在するか。
3. **中身チェック**: `conversations.json` をパースし、各要素に `chat_messages` キーがあるか
   （Claude.aiエクスポート特有のフォーマット）。
4. すべて満たしたものだけ `run_import.ps1` に渡して実際に取り込む。途中で弾かれたものは
   `state\processed_zips.json` に `status: "invalid"` として記録し、以後は再チェックしない。

**二重処理防止**: 候補ZIPのSHA256ハッシュを `state\processed_zips.json` に記録する。
`status: "imported"` または `"invalid"` のハッシュは以後スキップする。`"failed"`
（nexus-cli実行失敗やarchive移動失敗）は次回また再試行する。

## パイプラインA: 実装上の既知の注意点

これらはすべて実機テストで踏んだ実際の問題。今後スクリプトを触るときも要注意。

1. **PowerShellスクリプトはUTF-8 BOM付きで保存すること。**
   Windows PowerShell 5.1 はBOM無しUTF-8の `.ps1` をシステムのコードページ（このマシンでは
   日本語）で読むことがあり、日本語コメントを含むスクリプトでパース位置がずれて
   `Cannot bind argument to parameter 'FilePath' because it is null.` のような、
   一見無関係な行でランダムに失敗する現象が実際に発生した（エラーの行番号も信用できなくなる）。
   `run_import.ps1` / `watch_downloads.ps1` はBOM付きで保存済み。エディタで保存し直すときは
   UTF-8 (BOM付き) を明示すること。

2. **[修正済み, 2026-08-21] nexus-cli は `window.moment` に依存しており、CLIモードでは常に壊れていた。**
   nexus-cli.js は元々Obsidianプラグインのコードで、日付処理をObsidianが注入する
   `window.moment` に頼っている（`nexus-cli.js:29192`, `nexus-cli.js:12334`）。CLIモード用の
   `window` スタブ（`nexus-cli.js:11655`）は `.moment` を付与しないため、`window.moment` は
   常に `undefined`。結果として:
   - `DateParser.parseDate` が既存会話を1件スキャンするたびに例外を吐く
     （`parseDate - FAILED: ... reading 'ISO_8601'`、1回のimportで数百行になることがある）。
   - **既存ノートの更新（Updated）が `moment2 is not a function` で毎回失敗し、実質「更新」が
     一度も成功しない状態だった。** この失敗は `Import Summary` の `Failed` に計上されないため、
     `run_import.ps1` の成功判定（終了コード + `Failed` 件数）をすり抜けて「成功」と記録され、
     `created: 0, updated: 0, skipped: 全件` が毎回続く形で気付きにくかった。
   - 対処: `moment` パッケージは `cli\cli\node_modules` に正しくインストール済みなので、
     `nexus_cli_window_moment_shim.js`（このリポジトリ直下、gitignore対象外）で
     nexus-cli.js自身のwindowスタブより先に `window.moment` を注入している。
     `run_import.ps1` は `node --require nexus_cli_window_moment_shim.js nexus-cli.js ...`
     の形で呼び出す。修正後、archive済みZIPの再import テストで `updated=2` を確認済み
     （修正前は同じZIPで `updated=0`）。
   - `cli\` を再clone/rebuildしてもこのシムはリポジトリ側のファイルなので影響を受けない。

3. **`Move-Item` でダウンロード直後のファイルを動かそうとすると
   "process cannot access the file" で失敗することがある**（読み取り・コピーは通るが、
   削除/リネームだけがロックされる。Windows Defenderのリアルタイム保護が疑わしい）。
   そのため archive\ への移動は `Copy-Item` → （ベストエフォートで）`Remove-Item` の順に行う。
   コピーさえ成功すればimportは成功扱いにする（重複防止はファイル内容のSHA256ハッシュ基準なので、
   元ファイルがDownloadsに残っていても実害はない。削除だけ後で手動でもよい）。

4. **`$ErrorActionPreference = "Stop"` と `2>&1`（外部コマンドのstderr合流）の組み合わせは危険。**
   Windows PowerShellではstderr行が非終端エラーではなく終端エラーとして扱われ、
   nexus-cliの無害なstderr出力（上記2.）だけでスクリプト全体が異常終了する。
   `run_import.ps1` はnodeコマンド呼び出しの前後だけ `$ErrorActionPreference` を
   一時的に `"Continue"` に緩めている。

## パイプラインA: Vaultパスについて

実際のObsidian Vaultは `C:\Users\jinai\Projects\obsidian-vault`（2026-08-20に
`Documents\Obsidian Vault` から移動済み。ルートの `Projects\CLAUDE.md` 参照）。
`run_import.ps1` は `$root` の一つ上の階層として自動的にこれを解決する
(`Join-Path (Split-Path -Parent $root) "obsidian-vault"`)ので、
このリポジトリの配置場所を変えない限り明示指定は不要。

## タスクスケジューラ登録

パイプラインAは毎日06:00に1回、`ClaudeObsidianSyncPipelineA` というタスク名で登録済み
（2026-08-21登録）。

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\jinai\Projects\claude-obsidian-sync\watch_downloads.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At "06:00"
Register-ScheduledTask -TaskName "ClaudeObsidianSyncPipelineA" -Action $action -Trigger $trigger -Description "claude.aiのエクスポートZIPをObsidian Vaultに取り込む"
```

頻度を変えたい場合（例: 数時間おき）は `-Trigger` を
`New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration ([TimeSpan]::MaxValue)`
に差し替えて `Set-ScheduledTask -TaskName "ClaudeObsidianSyncPipelineA" -Trigger $trigger` で更新する。

パイプラインBも `ClaudeObsidianSync` というタスク名で毎日00:00に登録済み（別セッションで実施）。
登録時に使われたコマンドは参考までに再掲：

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\Users\jinai\Projects\claude-obsidian-sync\run_daily.ps1`""
$trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
Register-ScheduledTask -TaskName "ClaudeObsidianSync" -Action $action -Trigger $trigger -Description "Claude Code会話ログをObsidian Vaultに毎日同期"
```

現状確認（両タスクとも `Get-ScheduledTask` で `State: Ready`、直近実行は正常終了）:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -match "ClaudeObsidianSync" }
```

## 設計メモ（パイプラインB、既存分）

- **冪等性は「差分検知」ではなく「対象日を毎回全量から再生成して上書き」で担保する。**
  uuid の処理済み記録や jsonl の追記検知は行わない。同じ入力からは同じ出力になるため、
  再実行しても重複しない。トレードオフとして毎回全 `.jsonl` を読み直すが、個人利用の
  ログ量であれば実用上問題にならない。
- 日付境界は JST（固定 UTC+9 オフセット。`zoneinfo` の tzdata が環境にないため使っていない）。
- `type` が `user`/`assistant` の行だけを会話として扱う。それ以外の既知の非会話 type
  （`queue-operation`, `attachment`, `last-prompt`, `custom-title`, `ai-title`, `system`,
  `file-history-delta`, `file-history-snapshot`, `mode`, `permission-mode`,
  `teleported-from`）は無視。未知の type が出てきた場合は実行ログに `[WARN]` を残す
  （`KNOWN_IGNORED_TYPES` に追加すれば警告は消える）。
- `isSidechain: true`（サブエージェント内部のやり取り）はデフォルトで除外（`--include-sidechain` で含められる）。
- `thinking` ブロックは出力しない。`tool_use` / `tool_result` は `<details>` で折りたたみ、
  `tool_result` は 1000 文字で truncate（`--no-tools` で完全に除外可能）。
- Git commit/push はこのスクリプトの範囲外。Vault への書き込みまでで、コミット・プッシュは
  手動 or 別途の仕組みに委ねる（無人実行でリモートに自動 push するのは意図的に避けている）。
