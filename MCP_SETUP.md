# Slack 請求書 Bot — Remote MCP セットアップ手順

このリポジトリには2つの Render service が含まれます:

1. **moneybird-slack-bot** (既存): Moneybird Webhook 受信 → Slack に
   購入請求書/領収書の起票候補を投稿。`app.py` (Flask)。
2. **moneybird-mcp** (新規): Claude Code Routine 用の Remote MCP server。
   Slack スレッド駆動の販売請求書作成フローのバックエンド。`mcp_server.py`
   (FastMCP + Starlette + uvicorn)。

両方とも `render.yaml` 一発でデプロイされます。

---

## 1. 新規環境変数を準備

すでに設定済み（変更不要）:
- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACK_CHANNEL_ID`
- `MONEYBIRD_TOKEN`
- `MONEYBIRD_ADMINISTRATION_ID`
- `ANTHROPIC_API_KEY`

**新規に必要**:
- `MCP_AUTH_TOKEN` — Routine と MCP server 間の共有秘密鍵。
  ランダムな長い文字列を生成して使う。例:

  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

  生成例: `xK7vQ2pN9_R3sM-1bL5dF8jW6tH4yA0cE_uG-V9oZqI`

  この値は **Routine 側と Render 側の両方に同じ値を入れる**。

---

## 2. Render に2つ目の service を追加

`render.yaml` を push すれば Render が自動検出して **moneybird-mcp** を
新規 service として作成してくれる（Render Blueprint Sync）。

1. このリポジトリを GitHub に push
2. Render dashboard → 既存の service と同じ Blueprint で
   "Sync Blueprint" をクリック → moneybird-mcp が新規作成される
3. moneybird-mcp service の **Environment** タブで以下を設定:
   - `MONEYBIRD_TOKEN` — 既存と同じ値
   - `MONEYBIRD_ADMINISTRATION_ID` — 既存と同じ値
   - `SLACK_BOT_TOKEN` — 既存と同じ値（HARRO Bot の xoxb-...）
   - `MCP_AUTH_TOKEN` — Step 1 で生成した値

   ※ 既存 moneybird-slack-bot service には変更不要

4. Deploy が走り、URL が割り当てられる:
   `https://moneybird-mcp.onrender.com/`

5. ヘルスチェック:
   ```bash
   curl https://moneybird-mcp.onrender.com/health
   # → {"status":"ok"}
   ```

   ```bash
   # 認証なしだと401
   curl https://moneybird-mcp.onrender.com/mcp/
   # → {"error":"missing bearer token"}

   # 認証ありだと MCP の handshake 用エンドポイント
   curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
        https://moneybird-mcp.onrender.com/mcp/
   ```

---

## 3. Slack Bot をテストチャンネルに招待

```
/invite @HARRO Bot
```
を Slack で `C0AFF6RAN1H` のチャンネルに対して実行。

---

## 4. Claude Code Routine を作成

ターミナルで Claude Code を起動して `/schedule` を実行、もしくは
`claude.ai/code/routines` から作成。設定:

### 名前
`HARRO Sales Invoice Bot`

### スケジュール
平日 9-18時を1時間おき + 8時/20時 (CET):

```
0 8,9,10,11,12,13,14,15,16,17,18,20 * * 1-5
```

### Remote MCP 設定
`mcp.json` にあたる部分:

```json
{
  "mcpServers": {
    "harro-invoice": {
      "type": "http",
      "url": "https://moneybird-mcp.onrender.com/mcp/",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN ここに貼る>"
      }
    }
  }
}
```

### プロンプト
`routine_prompt.md` の "ROUTINE PROMPT" 以下をそのままコピペ。

---

## 5. テスト

1. Slack `C0AFF6RAN1H` チャンネルに以下を投稿:
   ```
   ZAPASS BVに2026年4月分 顧問料 €1,500 21%税抜、参照 2026-04
   ```
2. Routine を手動実行（`/schedule run <routine-id>` or dashboard）
3. 1-2分以内にスレッドにドラフトが投稿される
4. スレッドで `承認` と返信
5. 次回の Routine 実行で発行される

### 動作トラブルシュート

- **MCP server が応答しない** → Render dashboard で moneybird-mcp の
  ログを確認。Free dyno は15分無アクセスでスリープするので、初回は
  起動に20-30秒かかる。
- **401 Unauthorized** → MCP_AUTH_TOKEN が Routine 側と Render 側で
  一致しているか確認。
- **`channel_not_found`** → Bot がチャンネルに招待されていない。
- **`missing_scope`** → Slack App の OAuth & Permissions タブで
  追加スコープが必要 → Reinstall to Workspace。
- **Moneybird API 4xx** → moneybird-mcp ログで詳細確認。Token に
  販売請求書の権限があるか。

---

## アーキテクチャ図

```
┌──────────────────────────────────────┐
│ Claude Code Routine (Anthropic cloud)│
│  - cron 平日 8/9-18時毎時/20時       │
│  - routine_prompt.md の指示で動く    │
└───────────┬──────────────────────────┘
            │ HTTPS (Bearer auth)
            ▼
┌──────────────────────────────────────┐
│ moneybird-mcp.onrender.com           │
│ (mcp_server.py / FastMCP)            │
│                                      │
│ Slack tools:                         │
│   list_channel_messages              │
│   get_thread_replies                 │
│   post_thread_reply                  │
│   add/remove_reaction                │
│   get_user_info                      │
│                                      │
│ Moneybird tools:                     │
│   search_contact / create_contact    │
│   list_tax_rates                     │
│   create/get/delete_sales_invoice    │
│   send_sales_invoice                 │
└─────┬──────────────────┬─────────────┘
      ▼                  ▼
   Slack API       Moneybird API

(別 service: moneybird-slack-bot.onrender.com は購入請求書フローで継続稼働)
```
