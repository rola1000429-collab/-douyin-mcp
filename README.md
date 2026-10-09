# douyin-mcp

讓 AI（Claude 等支援 MCP 的客戶端）透過瀏覽器操作抖音網頁版：刷影片、按讚、讀／發留言、發佈作品，並整理推薦清單。

> ⚠️ 非官方專案，透過 Playwright 模擬真人操作網頁。請遵守抖音使用條款與當地法規，勿用於洗版、刷量或騷擾。使用風險自負。

## 安裝

```bash
git clone https://github.com/<你的帳號>/douyin-mcp.git
cd douyin-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

## 設定

**Claude Desktop**（`claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "douyin": {
      "command": "/絕對路徑/douyin-mcp/.venv/bin/python",
      "args": ["/絕對路徑/douyin-mcp/server.py"]
    }
  }
}
```

**Claude Code**：

```bash
claude mcp add douyin -- /絕對路徑/douyin-mcp/.venv/bin/python /絕對路徑/douyin-mcp/server.py
```

## 第一次使用

跟 AI 說：「呼叫 open_douyin，wait_for_login 設 true」，在彈出的瀏覽器登入抖音。登入狀態會存在 `~/.douyin-mcp/profile`，之後不用重登。

## 工具

| 工具 | 說明 |
|---|---|
| `open_douyin` | 開啟推薦頁／等待登入 |
| `current_video` | 目前影片資訊 |
| `next_video` / `previous_video` | 切換影片 |
| `browse_for_recommendations` | 連刷多支並回傳清單，供 AI 推薦 |
| `like_video` | 按讚 |
| `read_comments` | 讀留言 |
| `post_comment` | 發留言（有頻率限制） |
| `publish_video` | 上傳並發佈作品（有每日上限） |

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `DOUYIN_PROFILE_DIR` | `~/.douyin-mcp/profile` | 瀏覽器登入資料 |
| `DOUYIN_HEADLESS` | `0` | 設 `1` 無頭模式（首次登入請勿用） |
| `DOUYIN_MIN_INTERVAL` | `20` | 留言／發佈最短間隔（秒） |
| `DOUYIN_DAILY_COMMENT_LIMIT` | `20` | 每日留言上限 |
| `DOUYIN_DAILY_PUBLISH_LIMIT` | `3` | 每日發佈上限 |

## 注意

抖音網頁改版時，頁面選擇器（`data-e2e`、快捷鍵）可能失效，需要更新 `server.py`。歡迎發 PR。

## License

MIT
