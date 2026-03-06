from fastapi import FastAPI, HTTPException, Depends, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pathlib import Path
import json
import re
import os
from urllib.parse import quote, urlparse
from typing import Optional
import threading
import time
import uuid
from auth import verify_token, require_token_no_consume, consume_token_usage
from datetime import date, datetime
import traceback
from unlocker import process_url, get_download_watch_dirs, _move_into_files
from token_service import (
    load_tokens,
    create_token,
    create_token_total,
    disable_token,
    enable_token,
    set_daily_limit,
    delete_token
)

# ================= 基础配置 =================

BASE = Path(__file__).parent
CACHE = BASE / "cache"
HTML_DIR = CACHE / "html"
META_FILE = CACHE / "meta.json"
FILES_DIR = CACHE / "files"

CACHE.mkdir(exist_ok=True)
HTML_DIR.mkdir(exist_ok=True)
FILES_DIR.mkdir(exist_ok=True)

if not META_FILE.exists():
    META_FILE.write_text("[]", encoding="utf-8")


def inject_token_for_asset_urls(html: str, token: str) -> str:
    """为 /html/assets 与 /api/html/assets 链接补上 token，避免图片请求因缺少鉴权参数失败。"""
    if not html or not token:
        return html

    encoded = quote(token, safe="")
    pattern = re.compile(r'(/(?:api/)?html/assets/[^\s"\'\)]+)')

    def repl(match):
        url = match.group(1)
        if "token=" in url:
            return url
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}token={encoded}"

    return pattern.sub(repl, html)


def extract_http_url(raw: str) -> Optional[str]:
    """从文本中提取首个 http(s) URL。兼容“标题+链接”分享文案。"""
    if not raw:
        return None

    text = str(raw).strip()
    match = re.search(r"https?://[^\s\"'<>\]\)】]+", text, flags=re.IGNORECASE)
    candidate = match.group(0) if match else text
    candidate = candidate.strip().rstrip(".,;!?)]}，。；！？）】》")

    try:
        parsed = urlparse(candidate)
    except Exception:
        return None

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def normalize_download_csdn_url(url: str) -> str:
    """校验 download.csdn.net 链接，拦截列表页。

    注意：不主动修改 query 参数（避免把某些可用链接“规整”成不可用）。
    """
    try:
        p = urlparse((url or "").strip())
        host = (p.netloc or "").lower().split(":", 1)[0]
        if not host.endswith("download.csdn.net"):
            return url

        path = p.path or ""
        if path.startswith("/list"):
            raise ValueError(
                "该链接是下载列表页，请打开具体资源详情页（形如 https://download.csdn.net/download/作者/资源ID ）"
            )
        return url
    except ValueError:
        raise
    except Exception:
        return url

# ================= App =================

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
def index_page():
    client_index = BASE.parent / "csdn-client" / "index.html"
    if not client_index.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(client_index.read_text(encoding="utf-8"))


@app.get("/index.html", response_class=HTMLResponse)
def index_page_alias():
    return index_page()


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "csdn-unlocker",
        "time": datetime.now().isoformat(timespec="seconds"),
    }


# ================= 后台任务（用于避免公网 Nginx 504） =================
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except Exception:
        return default


def _is_download_job_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
        host = (p.netloc or "").lower().split(":", 1)[0]
    except Exception:
        return False
    return host.endswith("download.csdn.net")


def _is_supported_unlock_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    return h == "csdn.net" or h.endswith(".csdn.net")


# 下载执行槽位：默认 1（最稳，避免下载互相串单）
_DOWNLOAD_CONCURRENCY = max(1, _env_int("CSDN_DOWNLOAD_CONCURRENCY", 1))
_DOWNLOAD_SEM = threading.BoundedSemaphore(_DOWNLOAD_CONCURRENCY)

# 文章解锁执行槽位：默认 2（不被下载任务阻塞）
_ARTICLE_CONCURRENCY = max(1, _env_int("CSDN_ARTICLE_CONCURRENCY", 2))
_ARTICLE_SEM = threading.BoundedSemaphore(_ARTICLE_CONCURRENCY)


def _job_public_view(job: dict) -> dict:
    return {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": job.get("message"),
        "progress": job.get("progress"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "elapsed_s": int(time.time() - float(job.get("started_ts") or time.time())),
        "result": job.get("result"),
    }


def _update_job(job_id: str, **fields) -> None:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            return
        _JOBS[job_id].update({**fields, "updated_at": now})


def _is_file_stable(path: Path, seconds: float = 1.2) -> bool:
    try:
        st1 = path.stat()
    except Exception:
        return False
    time.sleep(seconds)
    try:
        st2 = path.stat()
    except Exception:
        return False
    return st1.st_size == st2.st_size and st1.st_mtime == st2.st_mtime


def _is_probable_download_file(path: Path) -> bool:
    """过滤明显不是目标资源的文件（如小图标/页面碎片）。"""
    try:
        size = int(path.stat().st_size)
    except Exception:
        return False

    suffix = (path.suffix or "").lower()
    deny_ext = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
        ".html", ".htm", ".json", ".xml", ".mhtml",
    }
    if suffix in deny_ext:
        return False

    # 太小的碎文件直接忽略（可按需调整）
    if size < 64 * 1024:
        return False

    return True


def _find_recent_complete_download(download_dir: Path, since_ts: float) -> Path | None:
    try:
        items = []
        for p in download_dir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith('.crdownload'):
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            # 兼容某些文件保留源 mtime 的场景：优先使用“事件时间” max(mtime, ctime)
            event_ts = max(float(getattr(st, "st_mtime", 0.0)), float(getattr(st, "st_ctime", 0.0)))
            if event_ts < since_ts:
                continue
            # 若同名 .crdownload 仍存在，认为未完成
            if (download_dir / (name + '.crdownload')).exists():
                continue
            if not _is_probable_download_file(p):
                continue
            items.append((event_ts, p))
        if not items:
            return None
        items.sort(key=lambda x: x[0], reverse=True)
        for _, p in items[:5]:
            if _is_file_stable(p):
                return p
    except Exception:
        return None
    return None


def _find_recent_complete_download_multi(download_dirs: list[Path], since_ts: float) -> Path | None:
    candidates: list[tuple[float, Path]] = []
    for d in download_dirs:
        p = _find_recent_complete_download(d, since_ts)
        if p is None:
            continue
        try:
            st = p.stat()
            ts = max(float(getattr(st, "st_mtime", 0.0)), float(getattr(st, "st_ctime", 0.0)))
        except Exception:
            ts = 0.0
        candidates.append((ts, p))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _salvage_latest_download_into_files(token: str, ip: str, since_ts: float) -> dict | None:
    download_dirs = get_download_watch_dirs()
    candidate = _find_recent_complete_download_multi(download_dirs, since_ts)
    if not candidate:
        return None

    stored_name, dst = _move_into_files(candidate, display_name=candidate.stem)
    entry = {
        "kind": "file",
        "filename": stored_name,
        "title": candidate.name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "size": dst.stat().st_size if dst.exists() else 0,
        "source_url": "",
        "token": token,
    }
    consume_token_usage(token, _DummyRequest(ip or "-"))
    meta = load_meta()
    meta.append(entry)
    save_meta(meta)
    return {"kind": "file", "title": entry.get("title"), "filename": stored_name}


class _DummyClient:
    def __init__(self, host: str):
        self.host = host


class _DummyRequest:
    def __init__(self, host: str):
        self.client = _DummyClient(host)


def _has_running_job_for_token(token: str) -> bool:
    with _JOBS_LOCK:
        for j in _JOBS.values():
            if j.get("token") == token and j.get("status") in ("queued", "running"):
                return True
    return False


def _get_running_job_id_for_token(token: str) -> str | None:
    with _JOBS_LOCK:
        # 优先 running，其次 queued
        running = None
        queued = None
        for job_id, j in _JOBS.items():
            if j.get("token") != token:
                continue
            st = j.get("status")
            if st == "running":
                running = job_id
            elif st == "queued":
                queued = job_id
        return running or queued


def _job_is_final(job_id: str) -> bool:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return True
        return j.get("status") in ("success", "error")


def _finalize_by_salvage(job_id: str, token: str, ip: str, since_ts: float, success_message: str) -> bool:
    """尝试从 Downloads 接管一个完成文件并将 job 标记成功。

    返回 True 表示本次已成功 finalize。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return False
        if job.get("status") in ("success", "error", "finalizing"):
            return False
        # 先占位，防止并发路径重复扣次数/重复写历史
        job.update({
            "status": "finalizing",
            "message": "检测到文件，正在匹配入库...",
            "progress": {"phase": "matching_file", "message": "检测到文件，正在匹配入库..."},
            "updated_at": now,
        })
        _JOBS[job_id] = job

    salvaged = None
    err = None
    try:
        salvaged = _salvage_latest_download_into_files(token, ip, since_ts)
    except Exception as e:
        err = e

    now2 = time.strftime("%Y-%m-%d %H:%M:%S")
    if salvaged:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
            if job:
                job.update({"status": "success", "message": success_message, "updated_at": now2, "result": salvaged})
                _JOBS[job_id] = job
        return True

    # 没接管到就恢复 running，继续等待 worker
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job and job.get("status") == "finalizing":
            job.update({
                "status": "running",
                "message": "后台处理中...",
                "updated_at": now2,
            })
            _JOBS[job_id] = job
    return False


def _grace_salvage_after_worker(job_id: str, token: str, ip: str, since_ts: float, seconds: int = 35) -> bool:
    """worker 结束后短暂继续轮询下载目录，避免“文件刚落盘就被判失败”。"""
    deadline = time.time() + max(1, int(seconds))
    while time.time() < deadline:
        if _job_is_final(job_id):
            return True
        if _finalize_by_salvage(job_id, token, ip, since_ts, "检测到已下载文件，已导入到 files"):
            return True
        time.sleep(2)
    return False


def _run_unlock_job(job_id: str, token: str, url: str, ip: str):
    start_ts = time.time()
    is_download_job = _is_download_job_url(url)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    queued_msg = "下载任务排队中，等待浏览器执行..." if is_download_job else "解锁任务排队中，等待执行..."
    with _JOBS_LOCK:
        job = _JOBS.get(job_id) or {}
        job.update({
            "status": "queued",
            "message": queued_msg,
            "progress": {"phase": "queued", "message": queued_msg},
            "updated_at": now,
            "started_ts": start_ts,
        })
        _JOBS[job_id] = job

    out: dict = {"result": None, "error": None, "traceback": None}

    def _target():
        acquired = False
        sem = _DOWNLOAD_SEM if is_download_job else _ARTICLE_SEM
        try:
            acquired = sem.acquire(timeout=15 * 60)
            if not acquired:
                raise RuntimeError("系统繁忙，任务排队超时，请稍后重试")

            _update_job(
                job_id,
                status="running",
                message="任务启动中...",
                progress={"phase": "starting", "message": "任务启动中..."},
            )

            def _status_cb(payload: dict):
                # payload: {phase,message,filename,bytes_downloaded,total_bytes,speed_bps,...}
                if not isinstance(payload, dict):
                    return
                phase = str(payload.get("phase") or "")

                if phase in ("starting", "init", "page_ready", "clicked", "download_started", "downloading"):
                    msg = "后台下载中..."
                elif phase in ("downloaded", "moving", "stored"):
                    msg = "下载完成，处理中..."
                else:
                    msg = str(payload.get("message") or "")

                if msg:
                    _update_job(job_id, message=msg, progress=payload)
                else:
                    _update_job(job_id, progress=payload)

            out["result"] = process_url(url, status_cb=_status_cb)
        except Exception as e:
            out["error"] = e
            out["traceback"] = traceback.format_exc()
            try:
                print(f"[job:{job_id}] worker error: {type(e).__name__}: {e}")
                print(out["traceback"])
            except Exception:
                pass
        finally:
            if acquired:
                try:
                    sem.release()
                except Exception:
                    pass

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()

    # 仅下载任务启用 watcher/salvage，避免文章任务误命中下载目录
    if is_download_job:
        def _watch_downloads():
            while worker.is_alive():
                if _job_is_final(job_id):
                    return
                if _finalize_by_salvage(job_id, token, ip, start_ts, "检测到已下载文件，已导入到 files"):
                    return
                time.sleep(2)

        watcher = threading.Thread(target=_watch_downloads, daemon=True)
        watcher.start()

    max_runtime = 12 * 60  # 最多 12 分钟
    while worker.is_alive() and (time.time() - start_ts) < max_runtime:
        if _job_is_final(job_id):
            return
        # 用兜底心跳保证前端看到“还活着”。
        # 如果已有进度（status_cb 在更新），不要覆盖更细的 message。
        has_progress = False
        with _JOBS_LOCK:
            j = _JOBS.get(job_id) or {}
            has_progress = bool(j.get("progress"))

        if has_progress:
            _update_job(job_id, status="running")
        else:
            _update_job(
                job_id,
                status="running",
                message=f"后台处理中... 已用时 {int(time.time() - start_ts)}s",
            )
        time.sleep(2)

    if worker.is_alive():
        if _job_is_final(job_id):
            return
        if is_download_job:
            # 超时：尝试接管 Downloads 里最近生成的完成文件
            if _finalize_by_salvage(job_id, token, ip, start_ts, "检测到已下载文件，已导入到 files"):
                return
            # 再给一点落盘缓冲时间，避免刚下载完就误判 error
            if _grace_salvage_after_worker(job_id, token, ip, start_ts, seconds=35):
                return

        now2 = time.strftime("%Y-%m-%d %H:%M:%S")
        with _JOBS_LOCK:
            if job_id in _JOBS and _JOBS[job_id].get("status") not in ("success", "error"):
                _JOBS[job_id].update({
                    "status": "error",
                    "message": "任务超时，且未在 Downloads 中发现可导入的完成文件",
                    "updated_at": now2,
                })
        return

    # worker 已结束
    if _job_is_final(job_id):
        return
    if out.get("error") is None and out.get("result") is not None:
        result = out["result"]

        consume_token_usage(token, _DummyRequest(ip or "-"))
        entry = dict(result)
        entry["token"] = token
        meta = load_meta()
        meta.append(entry)
        save_meta(meta)

        msg = "下载完成" if (entry.get("kind") == "file") else "解锁成功"
        now2 = time.strftime("%Y-%m-%d %H:%M:%S")
        with _JOBS_LOCK:
            if job_id in _JOBS:
                _JOBS[job_id].update({
                    "status": "success",
                    "message": msg,
                    "updated_at": now2,
                    "result": {
                        "kind": entry.get("kind") or "article",
                        "title": entry.get("title"),
                        "filename": entry.get("filename"),
                    },
                })
        return

    # 失败：仅下载任务尝试接管（用户可能手动点了下载）
    if is_download_job:
        if _finalize_by_salvage(job_id, token, ip, start_ts, "自动化失败，但检测到已下载文件，已导入到 files"):
            return
        if _grace_salvage_after_worker(job_id, token, ip, start_ts, seconds=35):
            return

    now2 = time.strftime("%Y-%m-%d %H:%M:%S")

    err = out.get("error")
    err_msg = "未知错误"
    if err:
        err_msg = f"{type(err).__name__}: {err}"
    with _JOBS_LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update({
                "status": "error",
                "message": err_msg[:300],
                "updated_at": now2,
            })


def _enqueue_job(token: str, url: str, ip: str) -> str:
    job_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    job = {
        "job_id": job_id,
        "token": token,
        "url": url,
        "ip": ip,
        "status": "queued",
        "message": "已进入队列",
        "created_at": now,
        "updated_at": now,
        "result": None,
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    t = threading.Thread(target=_run_unlock_job, args=(job_id, token, url, ip), daemon=True)
    t.start()
    return job_id

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================= Meta =================

def load_meta():
    return json.loads(META_FILE.read_text(encoding="utf-8"))

def save_meta(data):
    META_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def load_merged_history():
    """返回历史记录（仅使用 meta.json）。"""
    meta_hist = load_meta()

    try:
        meta_hist.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    except Exception:
        pass

    return meta_hist


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    try:
        # 兼容旧格式：YYYY-MM-DD
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            return datetime.fromisoformat(text + " 00:00:00")
    except Exception:
        return None
    return None


def _token_period(info: dict) -> tuple[str, str]:
    activated_dt = _parse_iso_dt(info.get("activated_at"))
    expired_dt = _parse_iso_dt(info.get("expired_at"))

    if not activated_dt or not expired_dt:
        try:
            duration_days = int(info.get("duration_days") or 0)
        except Exception:
            duration_days = 0
        if duration_days == 1:
            return "day", "日（1天）"
        if duration_days == 7:
            return "week", "周（7天）"
        if duration_days == 30:
            return "month", "月（30天）"
        if duration_days == 365:
            return "year", "年（365天）"
        if duration_days > 0:
            return "custom", f"未激活（{duration_days}天）"
        return "custom", "未激活"

    days = (expired_dt.date() - activated_dt.date()).days
    if days == 1:
        return "day", "日（1天）"
    if days == 7:
        return "week", "周（7天）"
    if days == 30:
        return "month", "月（30天）"
    if days == 365:
        return "year", "年（365天）"
    return "custom", f"自定义（{days}天）"

# ================= 管理后台 =================

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    tokens = load_tokens()
    group_labels = {
        "day": "日（1天）",
        "week": "周（7天）",
        "month": "月（30天）",
        "year": "年（365天）",
        "custom": "自定义",
    }
    group_order = ["day", "week", "month", "year", "custom"]
    grouped_rows = {key: [] for key in group_order}

    now = datetime.now().replace(microsecond=0)

    for token, info in tokens.items():
        enabled = info.get("enabled", True)
        period_key, period_label = _token_period(info)
        activated_at = info.get("activated_at", "")
        expired_at = info.get("expired_at", "")
        limit_mode = str(info.get("limit_mode") or "daily").strip().lower()
        limit_mode = "total" if limit_mode == "total" else "daily"

        daily_limit = info.get("daily_limit", "")
        total_limit = info.get("total_limit", "")
        used_total = info.get("used_total", "")

        is_activated = bool(str(activated_at or "").strip()) and bool(str(expired_at or "").strip())
        if not is_activated:
            activated_at = "未激活"
            expired_at = "未激活"

        expired_dt = _parse_iso_dt(expired_at)
        if not is_activated:
            status = "未激活" if enabled else "已禁用"
        elif expired_dt and expired_dt < now:
            status = "已过期"
        else:
            status = "启用中" if enabled else "已禁用"

        enable_btn = ""
        disable_btn = ""
        if enabled:
            disable_btn = f"""
            <form method=\"post\" action=\"/admin/disable\" class=\"admin-op-form\" style=\"display:inline-block;\">
              <input type=\"hidden\" name=\"token\" value=\"{token}\">
              <button>禁用</button>
            </form>
            """
        else:
            enable_btn = f"""
            <form method=\"post\" action=\"/admin/enable\" class=\"admin-op-form\" style=\"display:inline-block;\">
              <input type=\"hidden\" name=\"token\" value=\"{token}\">
              <button>启用</button>
            </form>
            """

        if limit_mode == "total":
            try:
                used_total_i = int(used_total or 0)
            except Exception:
                used_total_i = 0
            try:
                total_limit_i = int(total_limit or 0)
            except Exception:
                total_limit_i = 0
            limit_cell = f"总 {used_total_i}/{total_limit_i}"
        else:
            limit_cell = f"""
                        <form method="post" action="/admin/set_daily_limit" class="admin-op-form" style="display:inline-block;">
                            <input type="hidden" name="token" value="{token}">
                            <input name="daily_limit" type="number" min="1" value="{daily_limit}" style="width:80px;">
                            <button>保存</button>
                        </form>
            """

        row = f"""
                <tr>
                    <td style="max-width:420px;word-break:break-all;">{token}</td>
                    <td>{period_label}</td>
                    <td>{activated_at}</td>
                    <td>{expired_at}</td>
                    <td>
                        {limit_cell}
                    </td>
                    <td>{status}</td>
                    <td>
                        {enable_btn}
                        {disable_btn}
                        <form method="post" action="/admin/delete" class="admin-op-form" style="display:inline-block; margin-left:6px;">
                            <input type="hidden" name="token" value="{token}">
                            <button>删除</button>
                        </form>
                    </td>
                </tr>
                """
        grouped_rows[period_key].append(row)

    group_blocks = ""
    total_count = sum(len(v) for v in grouped_rows.values())

    for key in group_order:
        rows = grouped_rows[key]
        if not rows:
            continue

        label = group_labels.get(key, key)
        count = len(rows)
        group_blocks += f"""
        <details open style="margin:10px 0 14px; border:1px solid #ddd; border-radius:8px; padding:8px;">
            <summary style="cursor:pointer; font-weight:600;">{label}（{count}个）</summary>
            <table border="1" style="width:100%; margin-top:8px; border-collapse:collapse;">
                <tr>
                    <th>Token</th>
                    <th>周期</th>
                    <th>激活</th>
                    <th>过期</th>
                    <th>限制</th>
                    <th>状态</th>
                    <th>操作</th>
                </tr>
                {''.join(rows)}
            </table>
        </details>
        """

    if not group_blocks:
        group_blocks = "<p>暂无 Token 数据</p>"

    return f"""
    <html>
    <body>
        <h2>Token 管理后台</h2>

        <form method="post" action="/admin/create" id="createForm">
            <label>天数：</label>
            <input name="days" type="number" min="1" value="30">
            <label>限制方式：</label>
            <select name="limit_mode">
                <option value="daily" selected>每日次数</option>
                <option value="total">总次数（有效期内）</option>
            </select>
            <label>次数：</label>
            <input name="limit_value" type="number" min="1" value="3">
            <button>生成 Token</button>
        </form>
        <p id="adminMsg" style="color:#666;"></p>

        <h3>Tokens（共 {total_count} 个）</h3>
        <p style="color:#666; margin-top:0;">按周期聚合：日 / 周 / 月 / 年 / 自定义</p>
        {group_blocks}

        <h3>查看全部历史解锁记录</h3>
        <form method="get" action="/admin/history">
            <button>查看全部历史</button>
        </form>

        <h3>批量导出 Token 链接</h3>
        <form method="post" action="/admin/export_tokens" id="exportForm">
            <label>周期：</label>
            <select name="period">
                <option value="day">日（1天）</option>
                <option value="week">周（7天）</option>
                <option value="month">月（30天）</option>
                <option value="year" selected>年（365天）</option>
            </select>
            <label>数量：</label>
            <input name="count" type="number" value="5" min="1" max="500">
            <label>限制方式：</label>
            <select name="limit_mode">
                <option value="daily" selected>每日次数</option>
                <option value="total">总次数（有效期内）</option>
            </select>
            <label>次数：</label>
            <input name="limit_value" type="number" value="5" min="1">
            <button>批量生成</button>
        </form>
        <p>
            <textarea id="exportResult" rows="10" style="width:100%;max-width:980px;" placeholder="生成后这里会按顺序显示下载链接" readonly></textarea>
        </p>
        <p>
            <button type="button" id="copyExportBtn">复制全部链接</button>
            <input id="exportFilename" value="token_export.txt" style="margin-left:10px; width:180px;" />
            <button type="button" id="downloadExportBtn" style="margin-left:6px;">导出下载</button>
        </p>

        <script>
        (function() {{
            const msgEl = document.getElementById('adminMsg');
            const exportResultEl = document.getElementById('exportResult');

            function showMsg(text, isError) {{
                if (!msgEl) return;
                msgEl.textContent = text || '';
                msgEl.style.color = isError ? '#d93025' : '#188038';
            }}

            async function submitAdminForm(form) {{
                const formData = new FormData(form);
                try {{
                    const res = await fetch(form.action, {{ method: 'POST', body: formData }});
                    const data = await res.json().catch(() => ({{}}));
                    if (!res.ok) {{
                        showMsg(data.detail || '操作失败', true);
                        return;
                    }}
                    if (form.id === 'exportForm') {{
                        const urls = Array.isArray(data.urls) ? data.urls : [];
                        if (exportResultEl) exportResultEl.value = urls.join('\\n');
                        showMsg('批量生成成功：' + (data.count || urls.length) + ' 个', false);
                        return;
                    }}

                    if (data.token) {{
                        showMsg('生成成功：' + data.token, false);
                    }} else {{
                        showMsg('操作成功', false);
                    }}
                    setTimeout(() => window.location.reload(), 300);
                }} catch (e) {{
                    showMsg('网络异常，请稍后重试', true);
                }}
            }}

            const forms = document.querySelectorAll('form.admin-op-form, #createForm, #exportForm');
            forms.forEach((form) => {{
                form.addEventListener('submit', (e) => {{
                    e.preventDefault();
                    submitAdminForm(form);
                }});
            }});

            const copyBtn = document.getElementById('copyExportBtn');
            if (copyBtn) {{
                copyBtn.addEventListener('click', async () => {{
                    const text = exportResultEl ? exportResultEl.value.trim() : '';
                    if (!text) {{
                        showMsg('没有可复制的链接，请先生成', true);
                        return;
                    }}
                    try {{
                        await navigator.clipboard.writeText(text);
                        showMsg('已复制到剪贴板', false);
                    }} catch (e) {{
                        if (exportResultEl) {{
                            exportResultEl.focus();
                            exportResultEl.select();
                        }}
                        showMsg('复制失败，请手动 Ctrl/Cmd+C', true);
                    }}
                }});
            }}

            const downloadBtn = document.getElementById('downloadExportBtn');
            if (downloadBtn) {{
                downloadBtn.addEventListener('click', () => {{
                    const text = exportResultEl ? exportResultEl.value.trim() : '';
                    if (!text) {{
                        showMsg('没有可导出的链接，请先生成', true);
                        return;
                    }}
                    const nameInput = document.getElementById('exportFilename');
                    const filename = (nameInput && nameInput.value ? String(nameInput.value).trim() : '') || 'token_export.txt';
                    const blob = new Blob([text + '\\n'], {{ type: 'text/plain;charset=utf-8' }});
                    const downloadUrl = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = downloadUrl;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    URL.revokeObjectURL(downloadUrl);
                    showMsg('已导出下载：' + filename, false);
                }});
            }}
        }})();
        </script>

    </body>
    </html>
    """


@app.get("/admin/history", response_class=HTMLResponse)
def admin_history_page(token: Optional[str] = None):

    hist = load_merged_history()
    keyword = (token or "").strip()
    if keyword:
        hist = [h for h in hist if keyword in str(h.get("token") or "")]
    rows = ""
    for h in hist:
        filename = h.get("filename") or ""
        read_link = f"/admin/read/{filename}" if filename else "#"
        download_link = f"/admin/download/{filename}" if filename else "#"
        rows += f"""
        <tr>
            <td>{h.get('token') or ''}</td>
            <td>{h.get('title') or '未命名资源'}</td>
            <td>{h.get('created_at') or ''}</td>
            <td>{filename}</td>
            <td>
                <a href="{read_link}" target="_blank">在线查看</a>
                &nbsp;|&nbsp;
                <a href="{download_link}">下载</a>
            </td>
        </tr>
        """

    if not rows:
        rows = """
        <tr>
          <td colspan=\"5\">暂无历史记录</td>
        </tr>
        """

    token_value = keyword.replace('"', '&quot;')
    return f"""
    <html>
    <body>
        <h2>全部历史解锁记录</h2>
        <p>共 {len(hist)} 条</p>
        <form method="get" action="/admin/history" style="margin:10px 0;">
            <label>按 Token 搜索：</label>
            <input name="token" value="{token_value}" placeholder="输入 token（支持包含匹配）" style="width:420px;">
            <button>搜索</button>
            <a href="/admin/history" style="margin-left:10px;">清空</a>
        </form>
        <table border="1">
            <tr>
                <th>Token</th>
                <th>标题</th>
                <th>时间</th>
                <th>文件名</th>
                <th>操作</th>
            </tr>
            {rows}
        </table>
        <p><a href="/admin">返回管理后台</a></p>
    </body>
    </html>
    """


@app.get("/admin/read/{name}", response_class=HTMLResponse)
def admin_read_html(name: str):

    path = HTML_DIR / name
    if not path.exists():
        raise HTTPException(404)

    return path.read_text(encoding="utf-8")


@app.get("/admin/download/{name}")
def admin_download_html(name: str):

    path = HTML_DIR / name
    if not path.exists():
        raise HTTPException(404)

    return FileResponse(path, filename=name)

@app.post("/admin/create")
def admin_create(
    days: int = Form(...),
    daily_limit: Optional[int] = Form(None),
    limit_mode: str = Form("daily"),
    limit_value: Optional[int] = Form(None),
    total_limit: Optional[int] = Form(None),
):
    mode = (limit_mode or "daily").strip().lower()
    mode = "total" if mode == "total" else "daily"

    if mode == "total":
        tl = limit_value if limit_value is not None else (total_limit if total_limit is not None else daily_limit)
        if tl is None or int(tl) < 1:
            raise HTTPException(400, "total_limit must be >= 1")
        token, info = create_token_total(days=days, total_limit=int(tl))
    else:
        dl = limit_value if limit_value is not None else daily_limit
        if dl is None or int(dl) < 1:
            raise HTTPException(400, "daily_limit must be >= 1")
        token, info = create_token(days=days, daily_limit=int(dl))
    return {"token": token, **info}


@app.post("/admin/export_tokens")
def admin_export_tokens(
    period: str = Form(...),
    count: int = Form(...),
    daily_limit: Optional[int] = Form(None),
    limit_mode: str = Form("daily"),
    limit_value: Optional[int] = Form(None),
    total_limit: Optional[int] = Form(None),
):
    days_map = {
        "day": 1,
        "week": 7,
        "month": 30,
        "year": 365,
    }
    
    days = days_map.get(period)
    if not days:
        raise HTTPException(400, "invalid period")
    if count < 1 or count > 500:
        raise HTTPException(400, "count must be 1-500")

    mode = (limit_mode or "daily").strip().lower()
    mode = "total" if mode == "total" else "daily"

    if mode == "total":
        tl = limit_value if limit_value is not None else (total_limit if total_limit is not None else daily_limit)
        if tl is None or int(tl) < 1:
            raise HTTPException(400, "total_limit must be >= 1")
        tl = int(tl)
    else:
        dl = limit_value if limit_value is not None else daily_limit
        if dl is None or int(dl) < 1:
            raise HTTPException(400, "daily_limit must be >= 1")
        dl = int(dl)

    prefix = "http://download.dingjie.site/?code="
    urls = []
    for _ in range(count):
        if mode == "total":
            token, _ = create_token_total(days=days, total_limit=tl)
        else:
            token, _ = create_token(days=days, daily_limit=dl)
        urls.append(prefix + token)

    filename = f"token_export_{period}_{count}.txt"
    return {
        "ok": True,
        "period": period,
        "days": days,
        "count": len(urls),
        "filename": filename,
        "urls": urls,
    }

@app.post("/admin/disable")
def admin_disable(token: str = Form(...)):
    disable_token(token)
    return {"ok": True}


@app.post("/admin/enable")
def admin_enable(token: str = Form(...)):
    enable_token(token)
    return {"ok": True}


@app.post("/admin/set_daily_limit")
def admin_set_daily_limit(token: str = Form(...), daily_limit: int = Form(...)):
    if daily_limit < 1:
        raise HTTPException(400, "daily_limit must be >= 1")

    tokens = load_tokens()
    info = tokens.get(token) or {}
    mode = str(info.get("limit_mode") or "daily").strip().lower()
    if mode == "total":
        raise HTTPException(400, "该 Token 为总次数模式，无法设置每日次数")

    set_daily_limit(token, daily_limit)
    return {"ok": True}

@app.post("/admin/delete")
def admin_delete(token: str = Form(...)):
    delete_token(token)
    return {"ok": True}

# ================= API =================

@app.post("/api/unlock")
async def unlock(request: Request, token: str = Depends(verify_token)):
    # 兼容 JSON 或表单提交
    try:
        payload = await request.json()
    except Exception:
        form = await request.form()
        payload = dict(form)

    raw_url = payload.get("url")
    if not raw_url:
        raise HTTPException(400, "url required")

    url = extract_http_url(raw_url)
    if not url:
        raise HTTPException(400, "invalid url")

    # 规整 download.csdn.net 链接（去追踪参数、拦截列表页）
    try:
        url = normalize_download_csdn_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 下载类链接可能很慢，公网 Nginx 容易 504；改为后台任务并返回 job_id。
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().split(":", 1)[0]
    except Exception:
        host = ""

    if not _is_supported_unlock_host(host):
        raise HTTPException(400, "该链接暂不支持，请更换后重试")

    try:
        ip = request.client.host
    except Exception:
        ip = "-"

    job_id = _enqueue_job(token, url, ip)
    message = "已开始处理（后台下载中），请稍后自动完成" if host.endswith("download.csdn.net") else "已开始处理（后台解锁中），请稍后自动完成"
    return JSONResponse(
        status_code=202,
        content={
            "message": message,
            "job_id": job_id,
        },
    )


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, token: str = Depends(require_token_no_consume)):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        if job.get("token") != token:
            raise HTTPException(403, "forbidden")
        return _job_public_view(job)


@app.get("/api/history")
def history(token: str = Depends(require_token_no_consume)):
    merged = load_merged_history()
    filtered = [h for h in merged if h.get("token") == token]
    for h in filtered:
        if (h.get("kind") == "file") and h.get("filename"):
            h["file_exists"] = _resolve_file_path(str(h.get("filename"))) is not None
    return filtered


@app.get("/api/token_info")
def token_info(token: str = Depends(require_token_no_consume)):
    """返回 token 的到期时间与剩余额度。"""

    tokens = load_tokens()
    info = tokens.get(token)
    if not info:
        raise HTTPException(403, "Invalid token")

    mode = str(info.get("limit_mode") or "daily").strip().lower()
    mode = "total" if mode == "total" else "daily"

    if mode == "total":
        try:
            total_limit = int(info.get("total_limit") or 0)
        except Exception:
            total_limit = 0
        try:
            used_total = int(info.get("used_total") or 0)
        except Exception:
            used_total = 0
        remain = max(0, total_limit - used_total)
        return {
            "expired_at": info.get("expired_at"),
            "limit_mode": "total",
            "total_limit": total_limit,
            "used_total": used_total,
            "remain": remain,
        }

    today = date.today().isoformat()
    used = info.get("used", {}).get(today, {}).get("count", 0)
    remain = max(0, info.get("daily_limit", 0) - used)

    return {
        "expired_at": info.get("expired_at"),
        "limit_mode": "daily",
        "daily_limit": info.get("daily_limit"),
        "used_today": used,
        "remain": remain,
    }


@app.get("/api/all_history")
def all_history():
    """返回全部历史。"""
    return load_merged_history()


@app.get("/html/{name}", response_class=HTMLResponse)
@app.get("/api/html/{name}", response_class=HTMLResponse)
def read_html(name: str, token: str = Depends(require_token_no_consume)):
    path = HTML_DIR / name
    if not path.exists():
        raise HTTPException(404)

    html = path.read_text(encoding="utf-8")
    html = inject_token_for_asset_urls(html, token)
    return html


@app.get("/html/assets/{article_id}/{filename}")
@app.get("/api/html/assets/{article_id}/{filename}")
def read_asset(article_id: str, filename: str):
    path = HTML_DIR / "assets" / article_id / filename
    if not path.exists():
        raise HTTPException(404)

    return FileResponse(path)

@app.get("/download/{name}")
@app.get("/api/download/{name}")
def download(name: str, token: str = Depends(require_token_no_consume)):
    path = HTML_DIR / name
    if not path.exists():
        raise HTTPException(404)

    return FileResponse(path, filename=name)


def _resolve_file_path(name: str) -> Path | None:
    p = FILES_DIR / name
    if p.exists() and p.is_file():
        return p

    # 兼容旧历史：可能只记录了原文件名（无 uuid 前缀）
    try:
        matched = [x for x in FILES_DIR.glob(f"*__{name}") if x.is_file()]
    except Exception:
        matched = []

    if not matched:
        # 兼容重复改名链：uuid3__uuid2__uuid1__原名.ext
        # 当历史里是 uuid1__原名.ext 时，直接 *__{name} 可能匹配不到。
        if "__" in name:
            original_tail = name.split("__", 1)[-1]
            try:
                matched = [x for x in FILES_DIR.iterdir() if x.is_file() and x.name.endswith(original_tail)]
            except Exception:
                matched = []

    if not matched:
        return None

    matched.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return matched[0]


@app.get("/files/{name}")
@app.get("/api/files/{name}")
def download_file(name: str, token: str = Depends(require_token_no_consume)):
    path = _resolve_file_path(name)
    if not path:
        raise HTTPException(404)

    real_name = path.name
    download_name = real_name.split("__", 1)[-1] if "__" in real_name else real_name
    return FileResponse(path, filename=download_name)


@app.get("/file_view/{name}", response_class=HTMLResponse)
@app.get("/api/file_view/{name}", response_class=HTMLResponse)
def file_view(name: str, token: str = Depends(require_token_no_consume)):
    path = _resolve_file_path(name)
    if not path:
        raise HTTPException(404)

    real_name = path.name
    display_name = real_name.split("__", 1)[-1] if "__" in real_name else real_name
    token_q = quote(token, safe="")
    download_url = f"/api/files/{quote(real_name, safe='')}?token={token_q}"

    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>文件下载</title>
    <style>
        body {{ max-width: 860px; margin: 40px auto; padding: 0 16px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial; }}
        .card {{ border: 1px solid #e5e7eb; border-radius: 14px; padding: 18px; }}
        .name {{ font-size: 18px; font-weight: 700; color: #111827; word-break: break-all; }}
        .meta {{ margin-top: 8px; color: #6b7280; font-size: 13px; }}
        .btn {{ display: inline-block; margin-top: 16px; padding: 10px 14px; border-radius: 10px; text-decoration: none; background: #1677ff; color: #fff; font-weight: 600; }}
    </style>
</head>
<body>
    <div class=\"card\">
        <div class=\"name\">{display_name}</div>
        <div class=\"meta\">点击下方按钮开始下载</div>
        <a class=\"btn\" href=\"{download_url}\">下载</a>
    </div>
</body>
</html>"""
