# 機能比較: herdr-discord-bridge vs 世の中の chat 連携 bot

調査対象（2026-07 時点の README より）:

| 略称 | プロジェクト | 対象 agent |
|---|---|---|
| claudecode | [chadingTV/claudecode-discord](https://github.com/chadingTV/claudecode-discord) | Claude Code |
| d-bridge | [DoBuDevel/discord-agent-bridge](https://github.com/DoBuDevel/discord-agent-bridge) | Claude Code / OpenCode |
| DisCode | [raylin01/DisCode](https://github.com/raylin01/DisCode) | Claude Code / Gemini / tmux |
| piscord | [pi.dev/packages/piscord](https://pi.dev/packages/piscord) | pi |
| ccdb | [ebibibi/claude-code-discord-bridge](https://github.com/ebibibi/claude-code-discord-bridge) | Claude Code / Codex |
| **herdr** | 本プロジェクト | codex / claude / pi（Herdr 経由） |

凡例: ○ 実装 / △ 部分・別実装 / × 未実装

## 機能マトリクス

| 機能 | claudecode | d-bridge | DisCode | piscord | ccdb | **herdr** |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| channel/thread = session（自動） | ○ | ○ | ○(private thread) | ○ | ○ | **×（手動 bind）** |
| ストリーミング / 常時出力共有 | × | ○(30秒poll) | ○(terminal watch) | × | × | **×** |
| approve / deny buttons | ○ | △ | ○ | × | ○ | ○ |
| stop / interrupt | ○+queue | × | △ | ○+queue | × | ○(C-c) |
| modal / interactive 入力 | ○(select+text) | × | × | × | × | ○(modal) |
| target picker（select menu） | × | × | × | × | × | **○（独自）** |
| dashboard | ○(sidebar) | × | × | × | × | ○(edit+select) |
| push 通知（@mention） | ○ | × | × | × | × | ○ |
| ファイル添付 → agent | ○ | × | × | ○(local path) | × | **×** |
| session resume / new | ○ | ○(tmux永続) | ○ | ○ | ○ | △(Herdrがpane管理) |
| リアルタイム進捗（tool/time） | ○ | △ | △ | × | × | △(statusのみ) |
| リッチ embeds | △ | △ | ○ | × | △ | **×（code block）** |
| action tracking（ユーザー向け履歴） | △ | △ | ○ | × | △ | △(auditは内部) |
| multi-machine / runner | ○(hub) | × | ○ | × | × | △(Herdr --remote) |
| message queue（順次実行） | ○ | × | × | ○(SQLite) | × | × |
| schedule（cron） | × | × | × | ○ | ○ | × |
| セッション間協調 | × | × | × | × | ○(AI Lounge) | × |
| security / audit | ○ | △ | ○ | ○ | △ | ○ |

## herdr の現状と強み

**強み（独自・先行）**:
- **target picker**: Herdr の pane/workspace を select menu で選ぶ。他は channel 直結なので picker 不要だが、herdr は pane 単位の細かい制御が可能。
- **agent 別承認戦略**: claude→Enter, codex→y, pi→Enter を設定で切替。
- **Herdr pane/workspace 活用**: workspace label で alias 自動解決、tab/pane 構造を活用。
- **dashboard + push 通知の組み合わせ**: 状況把握と即時通知の両方。
- **Deny / Stop / Ask modal**: 承認却下・中断・自由入力を card から統一操作。

## ギャップ分析（足りない機能）

使い勝手に効く順:

| 優先 | 機能 | 現状 | 影響 |
|:---:|---|---|---|
| **★1** | ストリーミング / 常時出力共有 | × | **文脈把握の根本**。tail を都度叩かないと分からない。d-bridge・DisCode は常時共有で解決。 |
| **★2** | channel/thread = session 自動化 | ×（手動 bind） | **操作性**。他は全部「channel/thread 開けば session」。herdr は手動 bind が手間。 |
| ☆3 | リッチ embeds | ×（code block） | 視認性。DisCode は色付き Embed で見やすい。 |
| ☆4 | ファイル添付 → agent | × | 利便性。画像・コードを Discord から agent に渡す。 |
| ☆5 | action tracking（ユーザー向け） | △（内部 audit） | 履歴の可視化。何をしたか Discord で振り返る。 |
| ― | message queue / schedule / multi-machine | × | herdr は Herdr が pane 管理の真実の情報源なので、これらは Herdr 側または別アプローチ。後回しでよい。 |

## 便利にする方向（提案）

世の中の主流は **「channel/thread を開けば session、出力は常時流れ、承認は button」**。herdr は承認・button・dashboard・push は揃っているが、**「出力の常時共有」と「session の自動化」** が一番のギャップ。

この2つを埋めれば、herdr の強み（pane 細かい制御・agent 別戦略）と世の中の使い勝手が両立する:

1. **常時共有（ストリーミング）**: bind 先チャンネルに agent の出力を継続共有。d-bridge の30秒ポーリング方式が Herdr に合う（`herdr pane read` の差分検出）。
2. **thread 自動化**: Herdr pane を検出したら Discord thread を自動作成・リネーム（status emoji 付き）。bind が自動化され、channel list が dashboard になる（claudecode の sidebar dashboard 相当）。
