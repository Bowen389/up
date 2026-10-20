"""CSQAQ Whale Monitor V7 — 自动绑定出口 IP + 真正的逐轮增量报告。

Colab: 把本文件的代码粘贴到一个代码格，或直接打开同目录的 .ipynb。
本地: CSQAQ_TOKEN=... WHALE_ROOT=./whale_monitor python whale_monitor_v7.py
离线演示: WHALE_SELF_TEST=1 python whale_monitor_v7.py （运行两次看增量）

接口文档：
  https://docs.csqaq.com/api-342090738  POST /sys/bind_local_ip
  https://docs.csqaq.com/api-358158458 POST /task/get_task_business
  https://docs.csqaq.com/api-187131813 POST /monitor/rank
仅使用账号自己的 ApiToken；不要把 Token 写进 Drive 或公开分享调试文件。
"""
from __future__ import annotations

import builtins
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

API_BASE = "https://api.csqaq.com/api/v1"
BIND_PATH = "/sys/bind_local_ip"
RANK_PATH = "/monitor/rank"
DYNAMICS_PATH = "/task/get_task_business"
PAGE_SIZE = 50
SEP = "\x01"                         # 和 V6 的内容 key 完全一致，用于无缝读取旧快照
NAME_KEYS = ("market_name", "market_hash_name", "item_name", "goods_name",
             "good_name", "name", "title")
TIME_KEYS = ("created_at", "create_time", "occur_time", "event_time", "time")
ROW_LIST_KEYS = ("trades", "business_list", "list", "rows", "records", "items",
                 "data", "results", "content", "detail", "dynamics")
EVENT_COLUMNS = ("first_seen_at", "since_scan_at", "event_date", "time", "steam_id",
                 "steam_name", "item", "item_base", "good_id", "price", "quantity",
                 "delta", "total_count", "row_id", "key_mode", "type", "row_type",
                 "task_id", "is_first_scan", "key", "fingerprint")


_PROGRESS = {"start": time.monotonic(), "last_output": time.monotonic(), "stage": "启动"}


def out(*values):
    _PROGRESS["last_output"] = time.monotonic()
    print(*values, flush=True)


def stage(label):
    _PROGRESS["stage"] = label
    out("▶", label)


def heartbeat_loop(stop, idle):
    while not stop.wait(5):
        now = time.monotonic()
        if now - _PROGRESS["last_output"] >= idle:
            print(f"⏳ 仍在运行（静默 {int(now - _PROGRESS['last_output'])} 秒）"
                  f" | 阶段：{_PROGRESS['stage']}"
                  f" | 运行 {int(now - _PROGRESS['start'])} 秒", flush=True)
            _PROGRESS["last_output"] = now


def env_bool(key, default=False):
    return os.getenv(key, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


def env_int(key, default, minimum=0):
    try:
        return max(minimum, int(os.getenv(key, str(default))))
    except ValueError as exc:
        raise ValueError(f"{key} 必须是整数") from exc


def valid_day(value):
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
            return None
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # 旧程序静默把坏文件当空状态会导致把历史重新报一遍：这里必须停。
        raise RuntimeError(f"状态文件损坏，已停止以避免重复上报：{path}。请先备份/修复。") from exc


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".whale_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # 某些 Colab Drive/FUSE 挂载不实现 fsync，仍以 flush + os.replace 保底
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def atomic_csv(path, columns, rows):
    from io import StringIO
    buf = StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    # Excel 友好 UTF-8 BOM，不向现有 append-only 文件插入额外 BOM。
    atomic_write(path, "\ufeff" + buf.getvalue())


def prepare_root(self_test=False):
    stage("检查持久化状态目录 / Google Drive")
    if self_test:
        root = Path(os.getenv("WHALE_LOCAL_ROOT", "/tmp/whale_monitor_v7_selftest"))
    else:
        try:
            import google.colab  # noqa: F401
            in_colab = True
        except ImportError:
            in_colab = False
        if in_colab and env_bool("WHALE_USE_DRIVE", True):
            from google.colab import drive
            if not Path("/content/drive/MyDrive").is_dir():
                out("⏳ 挂载 Google Drive（弹窗需要授权）。Drive 挂载失败时不会悄悄改用临时目录。")
                drive.mount("/content/drive")
            if not Path("/content/drive/MyDrive").is_dir():
                raise RuntimeError("Drive 未挂载；中止以保护跨会话快照。")
            root = Path(os.getenv("WHALE_ROOT", "/content/drive/MyDrive/whale_monitor"))
            if not str(root).startswith("/content/drive/MyDrive/"):
                raise RuntimeError("Colab 持久化模式下 WHALE_ROOT 必须在 /content/drive/MyDrive/ 内")
        else:
            root = Path(os.getenv("WHALE_ROOT", "./whale_monitor"))
            if in_colab:
                out("⚠️ WHALE_USE_DRIVE=0：Colab 重启会丢本地快照；请显式指定持久目录。")
    for folder in ("data", "reports", "reports/debug", "reports/logs"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    out(f"✅ 状态目录：{root}")
    return root


def load_token(self_test=False):
    if self_test:
        return "SELF_TEST_ONLY"
    token = os.getenv("CSQAQ_TOKEN", "").strip()
    if not token:
        try:
            from google.colab import userdata
            token = str(userdata.get("CSQAQ_TOKEN") or "").strip()
        except (ImportError, KeyError):
            pass
        except Exception as exc:
            out(f"ℹ️ Colab Secrets 读取失败：{type(exc).__name__}（改用环境变量）")
    if not token and Path("/content/csqaq_token.txt").is_file():
        token = Path("/content/csqaq_token.txt").read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("缺少 CSQAQ_TOKEN。请在 Colab 左侧 🔑 Secrets 新建 CSQAQ_TOKEN，或设环境变量；不在格子中等待输入。")
    out("✅ ApiToken 已加载（不会打印、不会写进 Drive）")
    return token


@dataclass
class Config:
    root: Path
    start_date: date
    tz_offset: float
    daily_pages: int
    first_pages: int
    catchup_pages: int
    rank_users: int
    refresh_hours: float
    source: str
    min_whales: int
    bucket: str
    first_report_history: bool
    max_whales: int
    max_seconds: int
    checkpoint_every: int
    api_interval: float
    api_timeout: int
    self_test: bool = False

    @classmethod
    def from_env(cls):
        self_test = env_bool("WHALE_SELF_TEST")
        root = prepare_root(self_test)
        day_text = os.getenv("WHALE_EVENT_START_DATE", "2026-09-20").strip()
        start = valid_day(day_text)
        if start is None:
            raise ValueError("WHALE_EVENT_START_DATE 必须是有效的 YYYY-MM-DD")
        daily = env_int("WHALE_DYNAMICS_MAX_PAGES", 2, 1)
        first = env_int("WHALE_FIRST_SCAN_DYNAMICS_PAGES", 20, 1)
        catchup = env_int("WHALE_CATCHUP_MAX_PAGES", 20, 1)
        bucket = os.getenv("WHALE_FREQ_BUCKET", "day").strip().lower()
        if bucket not in ("day", "hour"):
            raise ValueError("WHALE_FREQ_BUCKET 只支持 day/hour")
        tz = float(os.getenv("WHALE_TZ_OFFSET", "0"))
        if not -12 <= tz <= 14:
            raise ValueError("WHALE_TZ_OFFSET 超出有效时区范围")
        source = os.getenv("WHALE_TOP30_SOURCE", "auto").strip().lower()
        if source not in ("auto", "snapshot", "playwright"):
            raise ValueError("WHALE_TOP30_SOURCE 只支持 auto/snapshot/playwright")
        return cls(root, start, tz, daily, first, max(daily, catchup),
                   env_int("WHALE_RANK_USERS", 10, 1),
                   float(os.getenv("WHALE_TOP30_REFRESH_HOURS", "24")), source,
                   env_int("WHALE_FREQ_MIN_WHALES", 2, 1), bucket,
                   env_bool("WHALE_FIRST_SCAN_REPORT_HISTORY", False),
                   env_int("WHALE_MAX_WHALES_PER_ROUND", 0),
                   env_int("WHALE_MAX_RUN_SECONDS", 3600),
                   env_int("WHALE_CHECKPOINT_WHALES", 10, 1),
                   max(1.3, float(os.getenv("WHALE_API_INTERVAL", "1.3"))),
                   env_int("WHALE_API_TIMEOUT", 30, 5), self_test)


class APIError(RuntimeError):
    pass


class AuthError(APIError):
    pass


class CSQAQClient:
    def __init__(self, token, cfg):
        self.token = token
        self.cfg = cfg
        self.last_request = 0.0
        self.interval = cfg.api_interval
        self.requests = 0
        self.rebound = False
        self.token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _request(self, path, payload=None):
        delay = self.interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        self.requests += 1
        raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"ApiToken": self.token}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API_BASE + path, data=raw, method="POST", headers=headers)
        start = time.monotonic()
        hint = "" if not isinstance(payload, dict) else " ".join(
            f"{k}={payload[k]}" for k in ("task_id", "good_id", "page_index") if k in payload)
        out(f"    → POST {path} {hint}")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.api_timeout) as response:
                status, headers, text = (response.status, response.headers,
                                         response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            status, headers, text = (exc.code, exc.headers,
                                     exc.read().decode("utf-8", "replace"))
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise APIError(f"{path} 网络异常：{type(exc).__name__}: {exc}") from exc
        out(f"    ← HTTP {status} ({time.monotonic() - start:.1f}s，{len(text)} 字)")
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        return status, body, text, headers

    def bind_ip(self, force=False):
        """官方接口绑定当前请求出口 IP；同一运行时/Token 避免重复调用。"""
        if not env_bool("WHALE_AUTO_BIND_IP", True):
            out("ℹ️ 已关闭自动绑 IP，要求事先手动绑定。")
            return
        cache = getattr(builtins, "_whale_v7_bind_cache", {})
        same = (isinstance(cache, dict) and cache.get("pid") == os.getpid()
                and cache.get("hash") == self.token_hash)
        if same and cache.get("success") and not force:
            out("✅ 此 Colab 会话已自动绑定过 IP；跳过重复调用")
            return
        if same and cache.get("attempt_at"):
            wait = 31 - (time.monotonic() - cache["attempt_at"])
            if wait > 0:
                out(f"⏳ 官方绑 IP 接口限制 30 秒/次，等待 {wait:.0f}s")
                time.sleep(wait)
        stage("自动绑定当前 CSQAQ 请求出口 IP（无需复制白名单）")
        for attempt in range(2):
            builtins._whale_v7_bind_cache = {"pid": os.getpid(), "hash": self.token_hash,
                                             "attempt_at": time.monotonic(), "success": False}
            status, body, text, headers = self._request(BIND_PATH)
            try:
                code = int(body.get("code", -1))
            except (TypeError, ValueError):
                code = -1
            if status == 200 and code in (0, 200):
                builtins._whale_v7_bind_cache["success"] = True
                msg = str(body.get("data") or "绑定成功")[:140]
                out(f"✅ {msg}")
                return
            if status == 429 or code == 429:
                if attempt == 0:
                    retry = headers.get("Retry-After", "31")
                    try:
                        wait = max(31, min(120, float(retry)))
                    except ValueError:
                        wait = 31
                    out(f"⏳ 绑定太频繁，{wait:.0f}s 后只重试一次")
                    time.sleep(wait)
                    continue
            msg = str(body.get("msg") or text[:200]).replace(self.token, "***")
            raise AuthError(f"自动绑定 IP 失败（HTTP {status}, code={code}）：{msg}。检查 Token；必要时手动加白名单。")
        raise AuthError("自动绑定 IP 限流后仍未成功；至少 30 秒后再试。")

    def post(self, path, payload):
        retries, did_rebind = 0, False
        while retries < 4:
            try:
                status, body, text, headers = self._request(path, payload)
            except APIError:
                retries += 1
                if retries == 4:
                    raise
                time.sleep(min(2 ** retries, 8))
                continue
            code_raw = body.get("code")
            try:
                code = int(code_raw)
            except (ValueError, TypeError):
                code = -1
            if status == 200 and code in (0, 200):
                return body
            msg = str(body.get("msg") or text[:250]).replace(self.token, "***")
            if status == 429 or code == 429:
                self.interval = min(5.0, self.interval * 1.5)
                try:
                    wait = min(120.0, max(2.0, float(headers.get("Retry-After", "5"))))
                except ValueError:
                    wait = 5.0
                out(f"    ⚠️ 限速；等 {wait:.0f}s，新的请求间隔 {self.interval:.1f}s")
                time.sleep(wait)
                retries += 1
                continue
            auth_words = ("apitoken", "token", "令牌", "白名单", "whitelist", "无权限", "权限")
            auth_message = (any(k in msg.lower() for k in auth_words) or
                            re.search(r"\bip\b|ip\s*地址|ip\s*不在", msg.lower()) is not None)
            auth_rejection = (status in (401, 403) or code in (401, 403) or
                              ((status == 400 or code == 400) and auth_message))
            if auth_rejection:
                if not did_rebind and env_bool("WHALE_AUTO_BIND_IP", True):
                    out("    ⚠️ 检测到 IP/Token 鉴权错误，自动重绑一次并重试原请求")
                    self.bind_ip(force=True)
                    did_rebind = True
                    continue
                raise AuthError(f"{path} 鉴权仍失败（HTTP {status}, code={code}）：{msg}")
            if status >= 500 or code >= 500:
                retries += 1
                if retries < 4:
                    time.sleep(min(2 ** retries, 10))
                    continue
            raise APIError(f"{path} 请求失败（HTTP {status}, code={code}）：{msg[:230]}")
        raise APIError(f"{path} 重试耗尽")


class DemoClient:
    """不触网的演示：第 1 次建基线，第 2 次新增 2 条，第 3 次再新增 2 条。"""
    def __init__(self, root):
        path = root / "data/demo_round.json"
        self.round = int(read_json(path, {"round": 0})["round"]) + 1
        atomic_json(path, {"round": self.round})
        self.requests = 0
        out(f"🧪 离线演示第 {self.round} 轮（不请求 CSQAQ）")

    def bind_ip(self):
        return

    def post(self, path, payload):
        self.requests += 1
        if path == RANK_PATH:
            return {"code": 200, "data": [
                {"id": 101, "steam_id": "76561190000000001", "steam_name": "演示鲸鱼甲", "num": 11},
                {"id": 102, "steam_id": "76561190000000002", "steam_name": "演示鲸鱼乙", "num": 10},
            ]}
        if path == DYNAMICS_PATH:
            page = int(payload["page_index"])
            rows = [{"id": 10, "good_id": 144, "market_name": "AK-47 | 皇后 (崭新出厂)",
                     "created_at": "2026-09-22T09:00:00", "type": 7, "count": 1}]
            if self.round >= 2:
                rows.insert(0, {"id": 20, "good_id": 144,
                                "market_name": "AK-47 | 皇后 (崭新出厂)",
                                "created_at": "2026-09-25T09:00:00", "type": 7, "count": 2})
            if self.round >= 3:
                rows.insert(0, {"id": 30, "good_id": 144,
                                "market_name": "AK-47 | 皇后 (崭新出厂)",
                                "created_at": "2026-09-26T09:00:00", "type": 7, "count": 1})
            return {"code": 200, "data": {"total": len(rows),
                                           "trades": rows if page == 1 else []}}
        raise APIError(f"未知演示接口：{path}")


def clean_goods(raw):
    if isinstance(raw, dict):
        raw = raw.get("goods", [])
    if not isinstance(raw, list):
        return []
    result, seen = [], set()
    for entry in raw:
        if isinstance(entry, (int, str)):
            entry = {"good_id": entry}
        if not isinstance(entry, dict):
            continue
        gid = str(entry.get("good_id") or entry.get("goods_id") or "").strip()
        if not gid or gid == "None" or gid in seen:
            continue
        seen.add(gid)
        result.append({"good_id": gid, "name": str(entry.get("name") or gid).strip(),
                       "rank": entry.get("rank", len(result) + 1)})
    return result


def scrape_top30():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out("⏳ 首次安装 Playwright（之后使用 TOP30 快照时不再安装）")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "playwright"], check=True)
        from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception:
            out("⏳ 首次安装 Chromium / 依赖…")
            subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=True)
            browser = playwright.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.goto("https://csqaq.com/detail", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            found, seen = [], set()
            for step in range(12):
                for anchor in page.locator('a[href*="/goods/"]').all():
                    href = anchor.get_attribute("href") or ""
                    match = re.search(r"/goods/(\d+)", href)
                    if not match or match.group(1) in seen:
                        continue
                    gid = match.group(1)
                    found.append({"good_id": gid, "name": (anchor.inner_text() or gid).strip(),
                                  "rank": len(found) + 1})
                    seen.add(gid)
                    if len(found) >= 30:
                        return found
                out(f"    滚动 {step+1}/12：已找到 {len(found)}/30")
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            raise RuntimeError(f"TOP30 页面只解析到 {len(found)}/30 个商品（可能遇到验证码或页面改版）")
        finally:
            browser.close()


def top_goods(cfg):
    path = cfg.root / "data/top30_latest.json"
    if cfg.self_test:
        return [{"good_id": "144", "name": "AK-47 | 皇后", "rank": 1}]
    cached = clean_goods(read_json(path, []))
    age_hours = (time.time() - path.stat().st_mtime) / 3600 if path.exists() else float("inf")
    if cfg.source == "snapshot" or (cfg.source == "auto" and cached and age_hours < cfg.refresh_hours):
        if not cached:
            raise RuntimeError(f"TOP30 快照不存在：{path}；改用 WHALE_TOP30_SOURCE=auto 获取")
        out(f"✅ TOP30 快照 {len(cached)} 个（{age_hours:.1f} 小时前）")
        return cached[:30]
    stage("抓取 TOP30（仅快照过期或强制刷新时使用浏览器）")
    try:
        goods = scrape_top30()
        atomic_json(path, goods)
        out(f"✅ 新鲜 TOP30 {len(goods)} 个")
        return goods
    except Exception as exc:
        if cached:
            out(f"⚠️ 浏览器失败：{exc}。使用旧快照 {len(cached)} 个（{age_hours:.1f} 小时前）")
            return cached[:30]
        raise


def extra_goods(cfg):
    raw = read_json(cfg.root / "data/extra_goods.json", [])
    goods = clean_goods(raw)
    for part in os.getenv("WHALE_EXTRA_GOODS", "").split(","):
        if part.strip():
            gid, _, name = part.strip().partition(":")
            goods += clean_goods([{"good_id": gid, "name": name or gid}])
    return clean_goods(goods)


def load_pool(root):
    pool = read_json(root / "data/whales_monitor_pool.json", {})
    if not isinstance(pool, dict):
        raise RuntimeError("鲸鱼池格式错误，已停止以防重置历史状态")
    return pool


def load_seen_goods(root):
    raw = read_json(root / "data/top30_seen_goods.json", [])
    if isinstance(raw, dict):
        raw = raw.get("good_ids", [])
    return set(str(x) for x in raw) if isinstance(raw, list) else set()


def rank_new_goods(cfg, client, goods):
    root = cfg.root
    pool = load_pool(root)
    seen = load_seen_goods(root)
    combined = clean_goods(goods + extra_goods(cfg))
    if not pool:
        out("⚠️ 鲸鱼池为空：忽略已处理商品标记，重建 rank")
        seen.clear()
    new_goods = [g for g in combined if g["good_id"] not in seen]
    stage(f"监控商品 {len(combined)} 个，首次需要 rank {len(new_goods)} 个；当前鲸鱼 {len(pool)} 只")
    for pos, good in enumerate(new_goods, 1):
        gid = good["good_id"]
        _PROGRESS["stage"] = f"rank 新商品 {pos}/{len(new_goods)} good_id={gid}"
        body = client.post(RANK_PATH, {"good_id": gid})
        data = body.get("data", [])
        if isinstance(data, dict):
            data = next((data[k] for k in ("list", "rows", "records", "items")
                         if isinstance(data.get(k), list)), [])
        users = data if isinstance(data, list) else []
        count = 0
        for user in users:
            if not isinstance(user, dict):
                continue
            sid = str(next((user[k] for k in ("steam_id", "steamid64", "steamid", "steamId")
                            if user.get(k)), "")).strip()
            task_id = next((user[k] for k in ("task_id", "monitor_id", "id")
                            if user.get(k)), None)
            if not sid or task_id is None:
                continue
            whale = pool.setdefault(sid, {"steam_id": sid, "sources": []})
            whale["steam_name"] = str(user.get("steam_name") or user.get("nickname") or whale.get("steam_name") or sid)
            whale["task_id"] = task_id
            sources = whale.get("sources", [])
            if not isinstance(sources, list):
                sources = []
            if gid not in {str(x.get("good_id")) for x in sources if isinstance(x, dict)}:
                sources.append({"good_id": gid, "good_name": good["name"], "rank": count + 1})
            whale["sources"] = sources
            count += 1
            if count >= cfg.rank_users:
                break
        if count:
            # 先保存池、后标记完成；反过来遇中断可能导致漏监控。
            atomic_json(root / "data/whales_monitor_pool.json", pool)
            seen.add(gid)
            atomic_json(root / "data/top30_seen_goods.json",
                        {"updated_at": datetime.now(timezone.utc).isoformat(), "good_ids": sorted(seen)})
        else:
            atomic_json(root / f"reports/debug/rank_zero_{gid}.json", body)
        out(f"    [{pos}/{len(new_goods)}] good_id={gid} → {count} 只鲸鱼" +
            ("；空结果未标记完成，下轮重试" if not count else ""))
    if not pool:
        raise RuntimeError("rank 没解析出任何鲸鱼；请检查 reports/debug/rank_zero_*.json 或 TOP30")
    return pool


def maybe_json(value):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def looks_like_row(row):
    if not isinstance(row, dict):
        return False
    if any(row.get(k) not in (None, "") for k in NAME_KEYS):
        return True
    if row.get("good_id") is not None and any(row.get(k) for k in TIME_KEYS):
        return True
    return any(isinstance(row.get(k), dict) and looks_like_row(row[k])
               for k in ("info", "goods", "item", "detail", "row"))


def extract_rows(body):
    """支持 data.trades、list、info 包装、JSON 字符串、id→row 字典。"""
    def walk(node, depth=0):
        if depth > 8:
            return None
        node = maybe_json(node)
        if isinstance(node, list):
            if not node:
                return []
            if all(isinstance(x, dict) for x in node) and any(looks_like_row(x) for x in node):
                return node
            for x in node:
                found = walk(x, depth + 1)
                if found:
                    return found
        if isinstance(node, dict):
            for key in ROW_LIST_KEYS:
                if key in node:
                    found = walk(node[key], depth + 1)
                    if found is not None:
                        return found
            values = list(node.values())
            if values and all(isinstance(x, dict) for x in values) and any(looks_like_row(x) for x in values):
                return values
            for x in values:
                found = walk(x, depth + 1)
                if found:
                    return found
        return None
    rows = walk(body.get("data") if isinstance(body, dict) else body)
    return rows if isinstance(rows, list) else []


def extract_total(body):
    data = maybe_json(body.get("data", {})) if isinstance(body, dict) else {}
    if isinstance(data, dict):
        value = data.get("total")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            pass
    return None


def unwrap_row(row):
    if not isinstance(row, dict):
        return {}
    for key in ("info", "goods", "item", "detail", "row", "record", "data"):
        inner = row.get(key)
        if isinstance(inner, dict) and looks_like_row(inner):
            return {**row, **inner}  # 内层的物品字段优先，外层保留 id/时间等
    return row


def pick(row, keys, default=None):
    return next((row[k] for k in keys if row.get(k) is not None and row.get(k) != ""), default)


def parse_event_time(value, tz_offset):
    """严格解析时间：不从 first_seen_at 猜测事件日期；无效返回 None。"""
    if value is None or value == "":
        return None
    tz = timezone(timedelta(hours=tz_offset))
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            ts = float(value)
            if ts > 1e11:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
        else:
            text = str(value).strip()
            if valid_day(text):  # 日期字段没有时分秒：已经是事件当地日期
                dt = datetime.combine(date.fromisoformat(text), dtime(0), tz)
            else:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-"))
                dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
                dt = dt.astimezone(tz)
        return dt.date().isoformat(), dt.isoformat(timespec="seconds"), dt.timestamp()
    except (ValueError, OverflowError, TypeError, OSError):
        return None


def normalize_row(row, cfg):
    row = unwrap_row(row)
    name = pick(row, NAME_KEYS, "")
    if not name:
        for inner in row.values():
            if isinstance(inner, dict):
                name = pick(inner, NAME_KEYS, "")
                if name:
                    break
    if not name:
        return None
    typ = pick(row, ("type", "dynamic_type", "action_type", "event_type"))
    # 已向服务端请求 CD_RECOVER：数字枚举不能按英文比对；但显式非 CD 字符串要丢弃。
    if isinstance(typ, str) and typ.upper() in ("SALE", "MOVE", "ALL"):
        return None
    raw_time = pick(row, TIME_KEYS, "")
    parsed = parse_event_time(raw_time, cfg.tz_offset)
    gid = str(pick(row, ("good_id", "goods_id", "item_id"), ""))
    row_id = pick(row, ("id", "dynamic_id", "event_id", "record_id", "log_id", "business_id"))
    if row_id is not None:
        key, mode = "id:" + str(row_id), "id"
    else:
        # 不加 type/价格/数量，否则旧的 V6 快照会全部变成“新事件”。
        key, mode = SEP.join((str(name), gid, str(raw_time))), "content"
    raw_quantity = pick(row, ("num", "quantity", "count"), 1)
    try:
        unit_quantity = float(raw_quantity)
    except (TypeError, ValueError):
        unit_quantity = 1.0
    return {"key": key, "key_mode": mode, "row_id": str(row_id) if row_id is not None else "",
            "item": str(name), "item_base": re.sub(r"\s*[(（][^()（）]*[)）]\s*$", "", str(name)).strip(),
            "good_id": gid, "time": str(raw_time), "event_date": parsed[0] if parsed else "",
            "event_time": parsed[1] if parsed else "", "event_ts": parsed[2] if parsed else None,
            "price": pick(row, ("price", "sell_price", "unit_price")),
            "unit_quantity": unit_quantity, "row_type": typ}


def aggregate_rows(rows, cfg):
    counts, info = Counter(), {}
    stats = Counter()
    for raw in rows:
        row = normalize_row(raw, cfg)
        if row is None:
            stats["invalid_row"] += 1
            continue
        key = row["key"]
        if row["key_mode"] == "id":
            counts[key] = 1       # 重叠页的同一 id 只能出现一次
        else:
            counts[key] += 1    # 无 id 的完全相同行以“出现次数”区分
        info[key] = row
        if not row["event_date"]:
            stats["invalid_date"] += 1
        stats[row["key_mode"]] += 1
    return counts, info, stats


def fetch_whale(client, whale, known, cfg):
    sid = str(whale["steam_id"])
    first = known is None
    cap = cfg.first_pages if first else cfg.daily_pages
    hard_cap = max(cap, cfg.catchup_pages) if not first else cap
    rows, page, stopped, saw_known, last_sig = [], 0, "max_pages", False, None
    truncated = False
    while page < cap:
        page += 1
        task_id = whale.get("task_id")
        if not task_id:
            raise APIError(f"{sid} 没有 task_id")
        body = client.post(DYNAMICS_PATH, {"page_index": page, "page_size": PAGE_SIZE,
                                           "task_id": task_id, "type": "CD_RECOVER"})
        batch = extract_rows(body)
        total = extract_total(body)
        if page == 1 and not batch:
            size = len(json.dumps(body, ensure_ascii=False, default=str))
            if (total is not None and total > 0) or (total is None and size > 2000):
                path = cfg.root / f"reports/debug/dynamics_raw_page1_{sid}.json"
                atomic_json(path, {"task_id": task_id, "body": body, "parsed_rows": 0})
                raise APIError(f"响应有数据但解析为 0 行；未推进快照，原始响应：{path}")
        if not batch:
            stopped = "empty_page"
            break
        signature = hashlib.sha256(json.dumps(batch, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if last_sig == signature:
            stopped, truncated = "repeated_page", True
            break
        last_sig = signature
        normalized = [normalize_row(r, cfg) for r in batch]
        keys = [n["key"] for n in normalized if n is not None]
        saw_known = saw_known or bool(known and any(k in known for k in keys))
        rows.extend(batch)
        # 仅有 id 才可安全按“整页已读”早停；无 id 的内容计数必须至少看完整个日常窗口。
        if known and keys and len(keys) == len(batch) and all(k.startswith("id:") and k in known for k in keys):
            stopped = "page_all_seen"
            break
        valid = [valid_day(n["event_date"]) if n else None for n in normalized]
        if valid and len(valid) == len(batch) and all(d is not None and d < cfg.start_date for d in valid):
            stopped = "before_start_date"
            break
        if len(batch) < PAGE_SIZE:
            stopped = "short_page"
            break
        if page == cap and not first and not saw_known and cap < hard_cap:
            out(f"    ⚠️ {cap} 页均未碰到上次已读 key；自动加深到最多 {hard_cap} 页补漏")
            cap = hard_cap
    else:
        truncated = True
    return rows, {"pages": page, "stop": stopped, "first_scan": first,
                  "rows": len(rows), "truncated": truncated,
                  "coverage_risk": truncated and not saw_known and not first}


def load_snapshot(root):
    path = root / "data/whale_snapshots.json"
    raw = read_json(path, {})
    if not isinstance(raw, dict) or (raw and not isinstance(raw.get("whales"), dict)):
        raise RuntimeError(f"快照结构异常，请先检查 {path}；不能重置已读状态。")
    whales = {}
    for sid, counts in raw.get("whales", {}).items():
        if isinstance(counts, list):
            counts = {str(k): 1 for k in counts}
        if not isinstance(counts, dict):
            raise RuntimeError(f"快照 {sid} 的数量结构损坏；中止以免重报。")
        whales[str(sid)] = {str(k): max(0, int(v)) for k, v in counts.items()}
    last_scans = raw.get("last_success_scan", {})
    if not isinstance(last_scans, dict):
        last_scans = {}
    return whales, {str(k): str(v) for k, v in last_scans.items()}


def snapshot_backup_once(root):
    path = root / "data/whale_snapshots.json"
    backup = root / "data/whale_snapshots.pre_v7.bak.json"
    if path.exists() and not backup.exists():
        raw = read_json(path, {})
        if raw.get("version") != 7:
            import shutil
            shutil.copy2(path, backup)
            out(f"💾 已备份旧快照：{backup}")


def save_snapshot(root, snapshots, last_scans):
    snapshot_backup_once(root)
    atomic_json(root / "data/whale_snapshots.json", {
        "version": 7, "mode": "cd_recover_stream_counts", "endpoint": DYNAMICS_PATH,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "whales": snapshots, "last_success_scan": last_scans,
        "first_scan_done_ids": sorted(snapshots),
    })


def make_new_events(whale, old, counts, info, cfg, first_seen_at, since_scan_at):
    fresh = []
    first = old is None
    if first and not cfg.first_report_history:
        return fresh
    old = old or {}
    sid = str(whale["steam_id"])
    for key, total in counts.items():
        previous = int(old.get(key, 0))
        if total <= previous:
            continue
        n = info[key]
        day = valid_day(n["event_date"])
        if day is None or day < cfg.start_date:
            continue
        # 无 id 时逐出现序数成一条事件：若 checkpoint 前断线，重试后又多一条，
        # 已经放在 outbox 的序数不再重复发布，也不会把区间 x+1..x+2 错算成新事件。
        ordinals = [1] if n["key_mode"] == "id" else range(previous + 1, total + 1)
        for ordinal in ordinals:
            identity = f"{sid}|{key}" if n["key_mode"] == "id" else f"{sid}|{key}|{ordinal}"
            fp = hashlib.md5(identity.encode("utf-8")).hexdigest()[:16]
            fresh.append({"fingerprint": fp, "key": key, "key_mode": n["key_mode"],
                          "row_id": n["row_id"], "steam_id": sid,
                          "steam_name": whale.get("steam_name", sid),
                          "task_id": whale.get("task_id"), "item": n["item"],
                          "item_base": n["item_base"], "good_id": n["good_id"],
                          "event_date": n["event_date"], "time": n["event_time"],
                          "price": n["price"], "quantity": round(n["unit_quantity"], 6),
                          "delta": 1, "total_count": ordinal, "type": "CD_RECOVER",
                          "row_type": n["row_type"], "is_first_scan": first,
                          "first_seen_at": first_seen_at, "since_scan_at": since_scan_at})
    return fresh


def ordered_whales(pool, cursor_path):
    items = list(pool.items())
    state = read_json(cursor_path, {})
    last_sid = str(state.get("last_sid") or "") if isinstance(state, dict) else ""
    ids = [str(sid) for sid, _ in items]
    if last_sid in ids:
        index = ids.index(last_sid) + 1
        items = items[index:] + items[:index]
    return items


def scan_whales(cfg, client, pool):
    root = cfg.root
    snapshot, last_scans = load_snapshot(root)
    outbox_path = root / "data/pending_events_v7.json"
    pending = read_json(outbox_path, {})
    if not isinstance(pending, dict) or ("events" in pending and not isinstance(pending["events"], list)):
        raise RuntimeError("待发布事件队列损坏，请先修复，不能直接丢弃！")
    # 崩溃窗口：如果 latest_report.json 已经写出，但 outbox 尚未标记 published，
    # 则这批事件已交付；直接恢复标记，不在下一轮报告重复展示。
    last_result = read_json(root / "reports/latest_report.json", {})
    if (pending and not pending.get("published") and pending.get("batch_id") and
            isinstance(last_result, dict) and last_result.get("batch_id") == pending["batch_id"]):
        pending["published"] = True
        pending["published_at"] = last_result["generated_at"]
        atomic_json(outbox_path, pending)
        atomic_json(root / "data/last_published_v7.json",
                    {"published_at": last_result["generated_at"], "event_rows": last_result.get("new_event_rows", 0)})
        out("↪ 检测到上轮报告已写完，修复发布标记；本轮不重放上次已展示事件")
    if pending.get("published"):
        pending = {}
    recovered = len(pending.get("events", [])) if isinstance(pending.get("events"), list) else 0
    if recovered:
        out(f"↪ 恢复上次中断后未发布的 {recovered} 条增量；不会再次扫描历史日期来造新数据")
    if not pending:
        pending = {"batch_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
                   "events": [], "published": False}
    fingerprints = {e.get("fingerprint") for e in pending["events"] if isinstance(e, dict)}
    cursor_path = root / "data/scan_cursor_v7.json"
    items = ordered_whales(pool, cursor_path)
    if cfg.max_whales:
        items = items[:cfg.max_whales]
    stats, failures, last_sid = [], [], None
    began = time.monotonic()

    def checkpoint():
        # 日志队列先于快照落盘：若中间断电，重扫可凭稳定指纹去重，不会丢待发布的事件。
        atomic_json(outbox_path, pending)
        save_snapshot(root, snapshot, last_scans)
        atomic_json(root / "data/whales_monitor_pool.json", pool)
        if last_sid:
            atomic_json(cursor_path, {"last_sid": last_sid, "at": datetime.now(timezone.utc).isoformat()})

    stage(f"扫描 {len(items)} 只鲸鱼（首扫 {cfg.first_pages} 页、日常 {cfg.daily_pages} 页）")
    try:
        for pos, (sid, whale) in enumerate(items, 1):
            if cfg.max_seconds and time.monotonic() - began > cfg.max_seconds:
                out(f"⏰ 到时间预算：完成 {pos - 1}/{len(items)}，剩余留待下一轮；游标轮转防饿死")
                break
            sid = str(sid)
            _PROGRESS["stage"] = f"扫描鲸鱼 {pos}/{len(items)} | {sid}"
            whale["steam_id"] = sid
            previous = snapshot.get(sid)
            last_ok = last_scans.get(sid, "")
            out(f"\n[{pos}/{len(items)}] {whale.get('steam_name') or sid} | {sid}")
            try:
                rows, meta = fetch_whale(client, whale, previous, cfg)
                counts, info, diagnostics = aggregate_rows(rows, cfg)
            except AuthError:
                raise  # 鉴权错误不能当作“某只鲸鱼无数据”后继续出一份假的空报告
            except Exception as exc:
                failures.append({"steam_id": sid, "error": str(exc)})
                out(f"    ❌ 未推进该鲸鱼的快照：{exc}")
                continue
            if rows and not counts:
                path = root / f"reports/debug/invalid_rows_{sid}.json"
                atomic_json(path, {"rows": rows[:5], "meta": meta})
                failures.append({"steam_id": sid, "error": f"有行但归一化 0：{path}"})
                out(f"    ❌ 行解析失败；未推进快照。调试：{path}")
                continue
            now = datetime.now(timezone(timedelta(hours=cfg.tz_offset))).isoformat(timespec="seconds")
            fresh = make_new_events(whale, previous, counts, info, cfg, now, last_ok)
            for event in fresh:
                if event["fingerprint"] not in fingerprints:
                    pending["events"].append(event)
                    fingerprints.add(event["fingerprint"])
            merged = dict(previous or {})
            for key, count in counts.items():
                merged[key] = max(merged.get(key, 0), int(count))
            snapshot[sid] = merged
            last_scans[sid] = now
            last_sid = sid
            meta.update({"steam_id": sid, "new": len(fresh), "new_delta": sum(e["delta"] for e in fresh),
                         "valid_keys": len(counts), "invalid_dates": diagnostics["invalid_date"],
                         "id_rows": diagnostics["id"], "content_rows": diagnostics["content"]})
            stats.append(meta)
            out(f"    ✅ {meta['pages']}页/{meta['stop']}，{len(rows)}行，新增 {len(fresh)} 条（{sum(e['delta'] for e in fresh)} 次）"
                + (" | ⚠️ 窗口已满且没有读到旧记录，可能漏报" if meta["coverage_risk"] else "")
                + (" | 首扫只建立基线" if meta["first_scan"] and not cfg.first_report_history else ""))
            if pos % cfg.checkpoint_every == 0:
                checkpoint()
                out(f"    💾 checkpoint：{pos}/{len(items)}；待发布 {len(pending['events'])} 条")
    finally:
        checkpoint()  # 包括鉴权突然失败：已经处理过的鲸鱼与待发布队列必须安全落盘
    atomic_json(root / "reports/debug/scan_page_stats.json", stats)
    return pending, {"processed": len(stats), "selected": len(items), "pool_size": len(pool),
                     "failed": failures, "recovered_unpublished": recovered,
                     "coverage_risk": [x["steam_id"] for x in stats if x["coverage_risk"]],
                     "api_requests": client.requests}


def frequency_report(events, cfg):
    by_good, by_bucket = {}, {}
    for e in events:
        gid = str(e.get("good_id") or "")
        if not gid or not valid_day(e.get("event_date")):
            continue
        target = by_good.setdefault(gid, {"good_id": gid, "name": e["item"],
                                           "whales": set(), "events": 0, "items": 0})
        target["whales"].add(e["steam_id"])
        target["events"] += int(e["delta"])
        target["items"] += float(e["quantity"])
        bucket = e["event_date"] if cfg.bucket == "day" else e["time"][:13]
        record = by_bucket.setdefault((bucket, gid), {"bucket": bucket, "good_id": gid,
                                                       "name": e["item"], "whales": set(),
                                                       "events": 0, "items": 0})
        record["whales"].add(e["steam_id"])
        record["events"] += int(e["delta"])
        record["items"] += float(e["quantity"])

    def finalize(items, min_whales):
        results = []
        for r in items:
            if len(r["whales"]) >= min_whales:
                results.append({**r, "whale_count": len(r["whales"]),
                                "whale_ids": sorted(r["whales"])})
                del results[-1]["whales"]
        return sorted(results, key=lambda x: (-x["whale_count"], -x["events"], x["name"]))

    return {"source": "仅本轮相对各鲸鱼上一次成功扫描新增的 CD_RECOVER（非 9/20 后所有动态）",
            "event_start_date_floor": cfg.start_date.isoformat(),
            "covered_goods_new": len(by_good), "bucket": cfg.bucket,
            "min_whales": cfg.min_whales,
            "resonance": finalize(by_good.values(), cfg.min_whales),
            "same_window": finalize(by_bucket.values(), cfg.min_whales)}


def existing_fingerprints(path):
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return {r.get("fingerprint") for r in csv.DictReader(f) if r.get("fingerprint")}


def append_cumulative(path, events):
    known = existing_fingerprints(path)
    fresh = [e for e in events if e.get("fingerprint") not in known]
    if not fresh:
        return 0
    # ★ 旧 V6 累计 CSV 表头与 V7 不同；不能直接追加新字段顺序，否则历史文件会错列。
    # 新增明细仍完整写在 new_cd_items.csv；累计表保留原有表头兼容旧数据。
    if path.exists() and path.stat().st_size:
        with path.open("r", encoding="utf-8-sig", newline="") as old:
            columns = next(csv.reader(old), [])
        if not columns or "fingerprint" not in columns or "event_date" not in columns:
            raise RuntimeError(f"累计事件表表头异常；停止追加以避免破坏旧数据：{path}")
    else:
        columns = EVENT_COLUMNS
    with path.open("a", encoding="utf-8-sig" if not path.exists() else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if f.tell() == 0:
            writer.writeheader()
        writer.writerows(fresh)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # Drive/FUSE 不一定支持 fsync
    return len(fresh)


def write_daily(cfg, path):
    ledger = cfg.root / "reports/cd_recover_events_all.csv"
    grouped = {}
    if ledger.exists():
        with ledger.open("r", encoding="utf-8-sig", newline="") as f:
            for e in csv.DictReader(f):
                if e.get("type") != "CD_RECOVER":
                    continue  # 旧版可选的 CD_REMOVED 不能混入 CD_RECOVER 按日统计
                day = valid_day(e.get("event_date"))
                if day is None or day < cfg.start_date:
                    continue  # 不许用 first_seen_at 冒充事件日期
                name = e.get("item", "")
                key = (day.isoformat(), name)
                rec = grouped.setdefault(key, {"event_date": day.isoformat(), "item": name,
                                               "events": 0, "whales": set(), "quantity_sum": 0.0})
                try:
                    rec["events"] += int(float(e.get("delta") or 1))
                    rec["quantity_sum"] += float(e.get("quantity") or 0)
                except ValueError:
                    continue
                rec["whales"].add(e.get("steam_id", ""))
    rows = [{"event_date": x["event_date"], "item": x["item"], "events": x["events"],
             "whale_count": len(x["whales"]), "quantity_sum": x["quantity_sum"]}
            for x in grouped.values()]
    atomic_csv(path, ("event_date", "item", "events", "whale_count", "quantity_sum"),
               sorted(rows, key=lambda x: (x["event_date"], -x["whale_count"], x["item"])))


def publish(cfg, pending, scan):
    stage("生成只含本轮新增的同频报告 / 写归档 / 发布队列")
    events = pending.get("events", [])
    # 幂等：即便被中断后重新执行，待发布队列的指纹也不会重复。
    dedup = {e["fingerprint"]: e for e in events}
    events = list(dedup.values())
    root, report_dir = cfg.root, cfg.root / "reports"
    freq = frequency_report(events, cfg)  # 关键修复：绝不拿 counts / 本轮全量 rows 做同频报告
    generated = datetime.now(timezone(timedelta(hours=cfg.tz_offset))).isoformat(timespec="seconds")
    last_path = root / "data/last_published_v7.json"
    last = read_json(last_path, {})
    result = {"generated_at": generated, "batch_id": pending.get("batch_id"),
              "since_report_at": last.get("published_at"),
              "comparison": "每只鲸鱼上一次成功扫描的持久快照（首扫默认只建基线）",
              "date_floor_not_cursor": cfg.start_date.isoformat(),
              "new_event_rows": len(events), "new_occurrences": sum(int(e["delta"]) for e in events),
              "scan": scan, "frequency": freq}
    # 不管新事件是不是 0 都覆写最新报告，避免浏览到上轮残留。
    atomic_csv(report_dir / "new_cd_items.csv", EVENT_COLUMNS, events)
    atomic_json(report_dir / "new_cd_items.json", {"count": len(events), "events": events})
    atomic_json(report_dir / "same_freq_report.json", {"generated_at": generated, **freq})
    label_for = lambda e: ((e.get("item_base") if os.getenv("WHALE_SAME_FREQ_KEY") == "item_base"
                            else e.get("item")) or "")
    group_whales = {}
    for e in events:
        group_whales.setdefault(label_for(e), set()).add(e["steam_id"])
    ranks = [{"item": label_for(e), "whale_count": len(group_whales[label_for(e)]),
              "good_id": e.get("good_id"), "steam_id": e["steam_id"],
              "time": e["time"], "event_date": e["event_date"], "quantity": e["quantity"]}
             for e in events]
    ranks.sort(key=lambda x: (-x["whale_count"], x["item"], x["steam_id"]))
    atomic_csv(report_dir / "same_frequency_rank.csv",
               ("item", "whale_count", "good_id", "steam_id", "time", "event_date", "quantity"), ranks)
    rank_json = [{"item": label, "whale_count": len(sids),
                  "events": [e for e in events if label_for(e) == label]}
                 for label, sids in group_whales.items()]
    rank_json.sort(key=lambda x: (-x["whale_count"], x["item"]))
    atomic_json(report_dir / "same_frequency_rank.json",
                {"generated_at": generated, "source": "only_new_events", "rank": rank_json})
    freq_rows = ([{"type": "多鲸鱼共振", "bucket": "", **r} for r in freq["resonance"]] +
                 [{"type": "同时间段共振", **r} for r in freq["same_window"]])
    atomic_csv(report_dir / "same_freq_report.csv",
               ("type", "bucket", "good_id", "name", "whale_count", "events", "items", "whale_ids"),
               [{**r, "whale_ids": ",".join(r["whale_ids"])} for r in freq_rows])
    md = ["# CSQAQ 本轮新增 CD_RECOVER / 同频报告", "",
          f"- 出报告：{generated}",
          f"- 对比：每只鲸鱼上次成功扫描快照（上次出报告：{last.get('published_at') or '首次'}）",
          f"- {cfg.start_date} **只是事件日期下限，不是每次重新报告的起点**",
          f"- 本次新增：**{len(events)} 条 / {result['new_occurrences']} 次**；成功 {scan['processed']} 只；失败 {len(scan['failed'])} 只",
          f"- 本轮新增覆盖商品：{freq['covered_goods_new']}；统计粒度：{cfg.bucket}；共振门槛：{cfg.min_whales} 只鲸鱼", "",
          "## 本轮新增明细", "", "| 鲸鱼 | 事件时间 | 商品 | good_id | 件数 | row_id |",
          "|---|---|---|---|---:|---|"]
    escape = lambda x: str(x or "").replace("|", "\\|").replace("\n", " ")
    for e in events[:200]:
        md.append(f"| {escape(e['steam_name'])} | {escape(e['time'])} | {escape(e['item'])} | "
                  f"{escape(e['good_id'])} | {e['quantity']} | {escape(e['row_id'])} |")
    if len(events) > 200:
        md.append(f"\n> 仅展示前 200 条；完整列表见 new_cd_items.csv（共 {len(events)} 条）。")
    if not events:
        md.append("\n（本轮没有新增事件；不是 9/20 起的历史清单。）")
    md += ["", "## 多鲸鱼共振（仅本轮新增）", "",
           "| 商品 | good_id | 鲸鱼数 | 事件次数 | 件数 |",
           "|---|---|---:|---:|---:|"]
    for r in freq["resonance"]:
        md.append(f"| {escape(r['name'])} | {r['good_id']} | {r['whale_count']} | {r['events']} | {r['items']} |")
    md += ["", f"## 同{cfg.bucket}共振（仅本轮新增）", "",
           "| 时段 | 商品 | good_id | 鲸鱼数 | 事件次数 |",
           "|---|---|---|---:|---:|"]
    for r in freq["same_window"]:
        md.append(f"| {r['bucket']} | {escape(r['name'])} | {r['good_id']} | {r['whale_count']} | {r['events']} |")
    if scan["failed"] or scan["selected"] != scan["processed"]:
        md += ["", "> ⚠️ 本轮部分扫描未成功或超时；缺失鲸鱼将在下轮与它自己的上次成功快照比较。"]
    if scan["selected"] < scan["pool_size"]:
        md += ["", f"> ⚠️ 调试限额生效：仅扫描监控池 {scan['selected']}/{scan['pool_size']} 只鲸鱼，本报告不代表全池。"]
    if scan["coverage_risk"]:
        md += ["", f"> ⚠️ {len(scan['coverage_risk'])} 只鲸鱼的翻页窗口未遇到已读记录：超过抓取上限的更新可能漏报，见 scan_page_stats.json。"]
    atomic_write(report_dir / "same_freq_report.md", "\n".join(md) + "\n")
    # 累计表是归档而非本轮报告；追加时也只追加新发现的事件。
    result["archived_now"] = append_cumulative(report_dir / "cd_recover_events_all.csv", events)
    write_daily(cfg, report_dir / "cd_recover_daily.csv")
    atomic_json(report_dir / "latest_report.json", result)
    # 报告文件都写完后才确认发布；中途中断下次从 outbox 恢复。
    pending["published"] = True
    pending["published_at"] = generated
    atomic_json(root / "data/pending_events_v7.json", pending)
    atomic_json(last_path, {"published_at": generated, "event_rows": len(events)})
    out(f"\n📊 本轮新增 CD_RECOVER：{len(events)} 条（{result['new_occurrences']} 次），"
        f"共振商品：{len(freq['resonance'])}，同{cfg.bucket}共振：{len(freq['same_window'])}")
    out(f"📄 最新增量报告：{report_dir / 'same_freq_report.md'}")
    out(f"📄 最新增量明细：{report_dir / 'new_cd_items.csv'}")
    out(f"📚 历史累计（不是本轮）：{report_dir / 'cd_recover_events_all.csv'}")
    return result


def run_once(cfg, client):
    top = top_goods(cfg)
    pool = rank_new_goods(cfg, client, top)
    pending, scan = scan_whales(cfg, client, pool)
    return publish(cfg, pending, scan)


def main():
    out("=" * 72)
    out("🐋 CSQAQ WHALE MONITOR V7：自动绑定 IP；同频/明细只报上次扫描后的新增")
    out("=" * 72)
    stop = Event()
    heartbeat = Thread(target=heartbeat_loop,
                       args=(stop, env_int("WHALE_HEARTBEAT_IDLE", 30, 5)), daemon=True)
    heartbeat.start()
    try:
        cfg = Config.from_env()
        token = load_token(cfg.self_test)
        client = DemoClient(cfg.root) if cfg.self_test else CSQAQClient(token, cfg)
        if not cfg.self_test:
            client.bind_ip()  # 在第一次 API 请求前完成；同一运行时重复执行会跳过
        forever = env_bool("WHALE_RUN_FOREVER", False)
        max_rounds = env_int("WHALE_MAX_ROUNDS", 0)
        interval = env_int("WHALE_LOOP_INTERVAL", 300, 1)
        round_no = 0
        while True:
            round_no += 1
            out(f"\n▶ 第 {round_no} 轮开始 | 事件日期下限 {cfg.start_date}（不是比较基线）")
            try:
                run_once(cfg, client)
            except AuthError as exc:
                out(f"❌ 鉴权失败，本轮立即停止：{exc}")
                raise
            if not forever or (max_rounds and round_no >= max_rounds):
                break
            _PROGRESS["stage"] = f"等待下一轮（{interval}s）"
            out(f"😴 轮次结束，{interval}s 后启动下轮（Colab 长时间闲置可能断开）")
            time.sleep(interval)
        out("✅ 完成")
    finally:
        stop.set()
        heartbeat.join(timeout=2)


if __name__ == "__main__":
    main()
