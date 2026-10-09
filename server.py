"""Douyin MCP Server — 讓 AI 透過瀏覽器刷抖音、按讚、留言、發佈作品、改個人資料、做推薦。

兩種執行方式：
  本機（預設）：stdio，自動開啟 Chromium，登入狀態存在本機 profile。
  雲端：MCP_TRANSPORT=http，連到遠端瀏覽器（Browserbase，或任何 CDP 網址），並以 token 保護。
"""
import asyncio
import hmac
import json
import os
import random
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP, Image
from playwright.async_api import Page, async_playwright

PROFILE_DIR = Path(os.getenv("DOUYIN_PROFILE_DIR", "~/.douyin-mcp/profile")).expanduser()
STATE_FILE = PROFILE_DIR.parent / "state.json"
HEADLESS = os.getenv("DOUYIN_HEADLESS", "0") == "1"
CDP_URL = os.getenv("DOUYIN_CDP_URL", "")  # 固定的 CDP 網址（如 Browserless）；與 Browserbase 擇一
BB_KEY = os.getenv("BROWSERBASE_API_KEY", "")
BB_PROJECT = os.getenv("BROWSERBASE_PROJECT_ID", "")
BB_CONTEXT = os.getenv("BROWSERBASE_CONTEXT_ID", "")  # 用來保存登入狀態；留空會自動建立
BB_TIMEOUT = int(os.getenv("BROWSERBASE_TIMEOUT", "0"))  # 秒；0 = 用方案預設
MIN_INTERVAL = float(os.getenv("DOUYIN_MIN_INTERVAL", "20"))
LIMITS = {
    "comment": int(os.getenv("DOUYIN_DAILY_COMMENT_LIMIT", "20")),
    "publish": int(os.getenv("DOUYIN_DAILY_PUBLISH_LIMIT", "3")),
    "profile": int(os.getenv("DOUYIN_DAILY_PROFILE_LIMIT", "2")),
}
MAX_DOWNLOAD = 500 * 1024 * 1024  # 下載檔案上限 500MB

HOME_URL = "https://www.douyin.com/?recommend=1"
SELF_URL = "https://www.douyin.com/user/self"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"

mcp = FastMCP("douyin", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

_pw = None
_browser = None
_ctx = None
_page: Page | None = None
_bb_session = None
_bb_context_id = BB_CONTEXT
_lock = asyncio.Lock()
_last_action = {k: 0.0 for k in LIMITS}


# ---------- 基礎工具 ----------
def _bb_client():
    from browserbase import Browserbase

    return Browserbase(api_key=BB_KEY)


async def _bb_new_session() -> str:
    """建立 Browserbase session（綁定 context 以保存登入），回傳 CDP 連線網址。"""
    global _bb_session, _bb_context_id
    if not BB_PROJECT:
        raise RuntimeError("請設定 BROWSERBASE_PROJECT_ID")

    def make():
        bb = _bb_client()
        ctx_id = _bb_context_id or bb.contexts.create(project_id=BB_PROJECT).id
        kwargs = {
            "project_id": BB_PROJECT,
            "browser_settings": {"context": {"id": ctx_id, "persist": True}},
        }
        if BB_TIMEOUT:
            kwargs["timeout"] = BB_TIMEOUT
        return ctx_id, bb.sessions.create(**kwargs)

    _bb_context_id, _bb_session = await asyncio.to_thread(make)
    return _bb_session.connect_url


async def _alive() -> bool:
    try:
        await _page.evaluate("1")
        return True
    except Exception:
        return False


async def get_page() -> Page:
    global _pw, _browser, _ctx, _page
    if _ctx is not None and not await _alive():  # 雲端 session 到期 → 重新連線
        _browser = _ctx = _page = None
    if _ctx is None:
        if _pw is None:
            _pw = await async_playwright().start()
        url = CDP_URL or (await _bb_new_session() if BB_KEY else "")
        if url:
            _browser = await _pw.chromium.connect_over_cdp(url)
            _ctx = _browser.contexts[0] if _browser.contexts else await _browser.new_context()
        else:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            _ctx = await _pw.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=HEADLESS,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
        if _page.url in ("", "about:blank"):
            await _page.goto(HOME_URL, wait_until="domcontentloaded")
    return _page


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _check_and_count(kind: str) -> str | None:
    """超過每日上限或間隔太短回傳錯誤訊息；否則計數並回傳 None。"""
    now = asyncio.get_event_loop().time()
    wait = MIN_INTERVAL - (now - _last_action[kind])
    if _last_action[kind] and wait > 0:
        return f"操作太頻繁，請 {wait:.0f} 秒後再試。"
    state = _load_state()
    today = str(date.today())
    day = state.get(today, {})
    if day.get(kind, 0) >= LIMITS[kind]:
        return f"今日 {kind} 已達上限 {LIMITS[kind]}（可用環境變數調整）。"
    day[kind] = day.get(kind, 0) + 1
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({today: day}))
    except OSError:
        pass
    _last_action[kind] = now
    return None


def _fetch(url: str, suffix: str) -> str:
    """下載 http(s) 檔案到暫存檔，回傳路徑。"""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("只支援 http(s) 網址")
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    size = 0
    with os.fdopen(fd, "wb") as f, urllib.request.urlopen(url, timeout=60) as r:
        while chunk := r.read(1 << 20):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise ValueError("檔案過大")
            f.write(chunk)
    return tmp


async def _resolve_file(path: str, url: str, suffix: str) -> str | None:
    if url:
        return await asyncio.to_thread(_fetch, url, suffix)
    if path and Path(path).expanduser().is_file():
        return str(Path(path).expanduser())
    return None


async def _first_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count():
                text = (await el.inner_text(timeout=1500)).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _video_info(page: Page) -> dict:
    scope = 'div[data-e2e="feed-active-video"]'
    return {
        "url": page.url,
        "author": await _first_text(page, [f"{scope} [data-e2e='feed-video-nickname']", "[data-e2e='feed-video-nickname']"]),
        "description": await _first_text(page, [f"{scope} [data-e2e='video-desc']", "[data-e2e='video-desc']"]),
        "likes": await _first_text(page, [f"{scope} [data-e2e='video-player-digg']", "[data-e2e='video-player-digg']"]),
        "comments": await _first_text(page, [f"{scope} [data-e2e='feed-comment-icon']", "[data-e2e='feed-comment-icon']"]),
    }


# ---------- 瀏覽 ----------
@mcp.tool()
async def open_douyin(wait_for_login: bool = False) -> str:
    """開啟抖音推薦頁。第一次使用請把 wait_for_login 設 True，並在瀏覽器手動登入。"""
    async with _lock:
        page = await get_page()
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        if wait_for_login:
            await page.wait_for_selector("[data-e2e='live-avatar'], [data-e2e='user-info']", timeout=180_000)
        return "已開啟抖音推薦頁。" + ("登入完成。" if wait_for_login else "")


@mcp.tool()
async def browser_status() -> dict:
    """回傳目前頁面網址；Browserbase 模式另含 live_view_url（打開可手動登入／過驗證）與 context_id。"""
    async with _lock:
        page = await get_page()
        info = {"url": page.url, "context_id": _bb_context_id or None}
        if _bb_session:
            d = await asyncio.to_thread(lambda: _bb_client().sessions.debug(_bb_session.id))
            info["live_view_url"] = getattr(d, "debugger_fullscreen_url", None) or getattr(d, "debugger_url", None)
        return info


@mcp.tool()
async def close_browser() -> str:
    """結束雲端瀏覽器 session（Browserbase 會在 session 結束時保存登入狀態）。下次使用會自動重開。"""
    global _browser, _ctx, _page, _bb_session
    async with _lock:
        if _bb_session:
            sid = _bb_session.id
            await asyncio.to_thread(
                lambda: _bb_client().sessions.update(sid, project_id=BB_PROJECT, status="REQUEST_RELEASE")
            )
        elif _ctx:
            await _ctx.close()
        _browser = _ctx = _page = _bb_session = None
        return "已結束瀏覽器 session。"


@mcp.tool()
async def screenshot() -> Image:
    """對目前頁面截圖，用來確認狀態或查看驗證碼／彈窗（雲端模式特別有用）。"""
    async with _lock:
        page = await get_page()
        return Image(data=await page.screenshot(type="png"), format="png")


@mcp.tool()
async def current_video() -> dict:
    """取得目前正在播放的影片資訊（作者、描述、讚數、留言數、網址）。"""
    async with _lock:
        return await _video_info(await get_page())


@mcp.tool()
async def next_video(watch_seconds: float = 8) -> dict:
    """先停留 watch_seconds 秒（會加一點隨機），再切換到下一支影片並回傳其資訊。"""
    async with _lock:
        page = await get_page()
        await asyncio.sleep(max(0.0, watch_seconds) + random.uniform(0, 2))
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(1.5)
        return await _video_info(page)


@mcp.tool()
async def previous_video() -> dict:
    """回到上一支影片。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("ArrowUp")
        await asyncio.sleep(1.5)
        return await _video_info(page)


@mcp.tool()
async def browse_for_recommendations(count: int = 5, watch_seconds: float = 6) -> list[dict]:
    """連續刷 count 支影片（上限 20）並回傳每支的資訊，供 AI 整理推薦清單。"""
    count = max(1, min(count, 20))
    results = []
    async with _lock:
        page = await get_page()
        for _ in range(count):
            results.append(await _video_info(page))
            await asyncio.sleep(max(0.0, watch_seconds) + random.uniform(0, 2))
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(1.5)
    return results


# ---------- 互動 ----------
@mcp.tool()
async def like_video() -> str:
    """對目前影片按讚（快捷鍵 Z）。再按一次會取消讚。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("z")
        return "已送出按讚操作。"


@mcp.tool()
async def read_comments(limit: int = 10) -> list[str]:
    """開啟目前影片的留言區並讀取前 limit 則留言文字。"""
    async with _lock:
        page = await get_page()
        await page.keyboard.press("x")
        await asyncio.sleep(2)
        items = page.locator("[data-e2e='comment-item']")
        n = min(await items.count(), max(1, limit))
        return [(await items.nth(i).inner_text()).strip() for i in range(n)]


@mcp.tool()
async def post_comment(text: str) -> str:
    """在目前影片底下留言。有每日上限與最短間隔限制，避免被判定為洗版。"""
    if not text.strip():
        return "留言內容不可為空。"
    async with _lock:
        err = _check_and_count("comment")
        if err:
            return err
        page = await get_page()
        await page.keyboard.press("x")
        await asyncio.sleep(1.5)
        box = page.locator(
            "[data-e2e='comment-input'] [contenteditable='true'], div[contenteditable='true']"
        ).first
        await box.click()
        await page.keyboard.type(text, delay=random.randint(40, 120))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
        return "已送出留言。"


# ---------- 個人資料 ----------
@mcp.tool()
async def update_profile(
    nickname: str = "", avatar_path: str = "", avatar_url: str = ""
) -> str:
    """修改暱稱與／或頭像。頭像可給伺服器上的 avatar_path，或可公開下載的 avatar_url（雲端建議用這個）。
    抖音限制改名頻率，且新資料可能需要審核；若跳出驗證請用 screenshot 查看並手動處理。"""
    if not (nickname.strip() or avatar_path or avatar_url):
        return "請至少提供 nickname 或頭像。"
    avatar = await _resolve_file(avatar_path, avatar_url, ".png")
    if (avatar_path or avatar_url) and not avatar:
        return "找不到頭像檔案。"
    async with _lock:
        err = _check_and_count("profile")
        if err:
            return err
        page = await get_page()
        await page.goto(SELF_URL, wait_until="domcontentloaded")
        await page.get_by_text("编辑资料").first.click()
        await asyncio.sleep(1.5)
        if avatar:
            await page.set_input_files("input[type='file']", avatar)
            await asyncio.sleep(1.5)
            for label in ("确定", "确认", "保存"):  # 裁切視窗
                btn = page.get_by_role("button", name=label, exact=True)
                if await btn.count():
                    await btn.first.click()
                    break
        if nickname.strip():
            name_input = page.locator("input[placeholder*='名字'], input[placeholder*='昵称'], input[type='text']").first
            await name_input.fill(nickname.strip())
        await asyncio.sleep(1)
        await page.get_by_role("button", name="保存", exact=True).last.click()
        await asyncio.sleep(2)
        return "已送出修改，請用 screenshot 確認結果（可能需要審核）。"


# ---------- 發佈 ----------
@mcp.tool()
async def publish_video(caption: str, video_path: str = "", video_url: str = "") -> str:
    """從創作者中心上傳影片並發佈。給伺服器上的 video_path，或可下載的 video_url。"""
    video = await _resolve_file(video_path, video_url, ".mp4")
    if not video:
        return "找不到影片檔案。"
    async with _lock:
        err = _check_and_count("publish")
        if err:
            return err
        page = await get_page()
        await page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        await page.set_input_files("input[type='file']", video)
        await page.get_by_text("重新上传").first.wait_for(timeout=600_000)
        editor = page.locator("div[contenteditable='true']").first
        await editor.click()
        await page.keyboard.type(caption, delay=random.randint(30, 80))
        await asyncio.sleep(1)
        await page.get_by_role("button", name="发布", exact=True).click()
        await asyncio.sleep(3)
        return "已點擊發佈，請到創作者中心確認狀態。"


# ---------- 雲端模式：token 保護 ----------
class TokenAuth:
    """所有請求都要帶 Authorization: Bearer <token> 或 ?token=<token>；/healthz 例外。"""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.responses import PlainTextResponse

        if scope["path"] == "/healthz":
            return await PlainTextResponse("ok")(scope, receive, send)
        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
        if not supplied:
            q = parse_qs(scope.get("query_string", b"").decode())
            supplied = (q.get("token") or [""])[0]
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            return await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
        return await self.app(scope, receive, send)


def _log(msg: str):
    print(f"[douyin-mcp] {msg}", file=sys.stderr, flush=True)


def main():
    mode = os.getenv("MCP_TRANSPORT", "").strip().lower()
    on_render = bool(os.getenv("RENDER"))  # Render 會自動設定此變數
    if mode in ("http", "streamable-http") or (on_render and mode != "stdio"):
        token = os.getenv("MCP_AUTH_TOKEN", "").strip()
        if len(token) < 24:
            _log("錯誤：雲端模式必須設定 MCP_AUTH_TOKEN（至少 24 字元）。")
            sys.exit(1)
        if not (CDP_URL or (BB_KEY and BB_PROJECT)):
            _log("錯誤：請設定 BROWSERBASE_API_KEY 與 BROWSERBASE_PROJECT_ID（或 DOUYIN_CDP_URL）。")
            sys.exit(1)
        import uvicorn

        _log(f"http 模式啟動，port={mcp.settings.port}")
        uvicorn.run(TokenAuth(mcp.streamable_http_app(), token), host="0.0.0.0", port=mcp.settings.port)
    else:
        _log("stdio 模式啟動（本機用）。")
        mcp.run()


if __name__ == "__main__":
    main()
