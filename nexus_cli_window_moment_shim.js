// nexus-cli.js (cli\cli\dist\nexus-cli.js) は元々Obsidianプラグインのコードで、
// 日付処理に window.moment (Obsidianアプリが実行時に注入するグローバル) を参照している。
// CLIモードでは nexus-cli.js 自身が `globalThis.window = globalThis` というスタブを
// 用意するが、.moment は付与しないため window.moment は常に undefined になり、
// DateParser.parseDate が全件例外を吐く（既存会話の日付比較が機能しない）ほか、
// 既存ノート更新時に "moment2 is not a function" で更新自体が失敗する。
//
// moment パッケージ自体は cli\cli\node_modules に正しくインストールされているので、
// nexus-cli.js が自前の window スタブを作る前にここで window.moment を注入しておく
// （nexus-cli.js 側のスタブは `typeof window === "undefined"` のときだけ動くので、
// 先にセットしておけば上書きされない）。
//
// 使い方: run_import.ps1 から `node --require <このファイル> nexus-cli.js ...` で読み込む。
"use strict";

const path = require("path");
const momentPath = path.join(__dirname, "cli", "cli", "node_modules", "moment");

// nexus-cli.js 自身のスタブ (`window = globalThis`) と同じ形にしておかないと、
// setTimeout や crypto など window 経由で参照される他のグローバルが失われる。
globalThis.window = globalThis;
globalThis.window.moment = require(momentPath);
