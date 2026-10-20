# -*- coding: utf-8 -*-
"""
夸克网盘分享监控 —— 发现更新自动下载到本地

设计要点：
  * 列表接口可匿名访问 → 监控永远在线，不受 Cookie 过期影响
  * 下载接口需要登录 → Cookie 失效时只提醒，不崩

用法：
  python quark_watch.py --init        # 建立基线（不下载，只记录当前文件状态）
  python quark_watch.py               # 正常一次：检查 → 有更新则下载
  python quark_watch.py --check       # 只检查，列出变化，不下载
  python quark_watch.py --probe       # 自检：接口/登录状态/目录
  python quark_watch.py --launch      # 启动下载目录里最新的匹配文件
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = "https://drive-pc.quark.cn/1/clouddrive"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

_URL_RE = re.compile(r"https?://[^\s'\"<>)\]，。]+")
# requests 的连接异常写的是相对路径 —— 没有 scheme，_URL_RE 抓不到：
#   Max retries exceeded with url: /you7O6QO/…?auth_key=…&token=…
_URLPATH_RE = re.compile(r"(with url:\s*)[^\s'\"<>)\]，。]+")


def _redact(msg: str) -> str:
    """把异常消息里的完整 URL / 签名路径掐掉，只留 host。

    夸克的下载直链形如 /…?auth_key=…&token=…&ud=…，**整条就是凭据**，
    而 requests 的连接异常会把请求路径原样塞进异常消息。
    直接 log.error(e) 等于把凭据抄到磁盘上，日志一转发就泄露。
    """
    if not msg:
        return msg

    def _sub(m: re.Match) -> str:
        try:
            from urllib.parse import urlsplit
            p = urlsplit(m.group(0))
            return f"{p.scheme}://{p.netloc}/…"
        except Exception:
            return "<url>"

    return _URLPATH_RE.sub(r"\1<path>", _URL_RE.sub(_sub, msg))


ROOT = (Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)


def data_dir() -> Path:
    """配置/状态/日志统一放在用户目录下并设为隐藏，避免散落在 exe 旁边"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    d = base / "QuarkUpdateHelper"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(d), 0x02)   # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass
    return d


DATA_DIR = data_dir()
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_DIR = DATA_DIR / "logs"

log = logging.getLogger("quarkwatch")


# ---------------------------------------------------------------- 基础
def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_DIR / f"watch_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    # 打包成窗口程序后 stdout 可能为 None
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit(f"缺少配置文件：{CONFIG_FILE}")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # 环境变量优先，方便临时替换
    if os.environ.get("QUARK_COOKIE"):
        cfg["cookie"] = os.environ["QUARK_COOKIE"]
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("state.json 损坏，重建")
    return {"files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class _Session(requests.Session):
    """先直连；连不上且允许时自动改用系统代理再试一次"""

    def __init__(self, auto_env: bool):
        super().__init__()
        self._auto_env = auto_env
        self._switched = False

    def request(self, *a, **kw):
        try:
            return super().request(*a, **kw)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError):
            if self._auto_env and not self._switched:
                self._switched = True
                self.trust_env = True
                log.warning("直连失败，改用系统代理重试")
                return super().request(*a, **kw)
            raise


def session_with(cookie: str | None, with_cookie: bool = True, proxy: str | None = None) -> requests.Session:
    proxy = (proxy or "auto").strip()
    auto = proxy.lower() in ("", "auto")
    s = _Session(auto_env=auto)
    s.headers.update({"User-Agent": UA, "Referer": "https://pan.quark.cn/"})
    s.trust_env = False
    if proxy.lower() == "env":
        s.trust_env = True
    elif proxy and not auto:
        s.proxies = {"http": proxy, "https": proxy}
    if with_cookie and cookie:
        s.headers["Cookie"] = cookie
    return s


def _retry(fn, times: int = 3, delay: float = 2.0):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as e:
            last = e
            if i + 1 < times:
                log.warning("请求失败（第 %d/%d 次），%.0f 秒后重试：%s", i + 1, times, delay, _redact(str(e)))
                time.sleep(delay)
    raise last  # type: ignore[misc]


def api_get(s: requests.Session, path: str, params: dict, timeout=20) -> dict:
    return _retry(lambda: _check(s.get(f"{BASE}{path}", params=params, timeout=timeout)))


def api_post(s: requests.Session, path: str, payload: dict, timeout=25, params=None) -> dict:
    p = {"pr": "ucpro", "fr": "pc"}
    if params:
        p.update(params)
    return _retry(lambda: _check(s.post(f"{BASE}{path}", params=p, json=payload, timeout=timeout)))


def _check(r: requests.Response) -> dict:
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"非 JSON 响应 HTTP {r.status_code}: {r.text[:200]}")
    if j.get("status") != 200 or j.get("code") != 0:
        raise RuntimeError(f"接口错误 status={j.get('status')} code={j.get('code')} "
                           f"msg={j.get('message')}")
    return j


# ---------------------------------------------------------------- 分享侧
def get_share_token(s: requests.Session, pwd_id: str, passcode: str = "") -> dict:
    j = api_post(s, "/share/sharepage/token", {"pwd_id": pwd_id, "passcode": passcode})
    return j["data"]


def list_share_dir(s: requests.Session, pwd_id: str, stoken: str, pdir_fid: str,
                   page_size: int = 100) -> list[dict]:
    out, page = [], 1
    while True:
        j = api_get(s, "/share/sharepage/detail", {
            "pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": pdir_fid, "force": "0", "_page": page, "_size": page_size,
            "_fetch_banner": "0", "_fetch_share": "0", "_fetch_total": "1",
            "_sort": "file_type:asc,updated_at:desc",
        })
        lst = j["data"].get("list") or []
        out.extend(lst)
        total = j["data"].get("metadata", {}).get("_total", 0)
        if len(lst) < page_size or len(out) >= total:
            break
        page += 1
        time.sleep(0.6)          # 官方接口有频率限制，别打太快
    return out


def walk_share(s: requests.Session, pwd_id: str, stoken: str, pdir_fid: str,
               rel: str = "", depth: int = 0) -> list[dict]:
    """递归收集文件（跳过目录本身，返回带相对路径的文件信息）"""
    if depth > 4:
        return []
    files = []
    for f in list_share_dir(s, pwd_id, stoken, pdir_fid):
        name = f["file_name"]
        if f.get("dir"):
            sub_rel = f"{rel}/{name}" if rel else name
            files.extend(walk_share(s, pwd_id, stoken, f["fid"], sub_rel, depth + 1))
        else:
            files.append({
                "fid": f["fid"],
                "name": name,
                "rel": rel,
                "dir_fid": pdir_fid,
                "size": f.get("size", 0),
                "updated_at": f.get("updated_at", 0),
                "fid_token": f.get("share_fid_token", ""),
            })
    return files


# ---------------------------------------------------------------- 自有网盘
def list_dir(s: requests.Session, pdir_fid: str, page_size: int = 200) -> list[dict]:
    """列目录（自动翻页）"""
    out, page = [], 1
    while True:
        j = api_get(s, "/file/sort", {
            "pr": "ucpro", "fr": "pc", "pdir_fid": pdir_fid, "_page": page,
            "_size": page_size, "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
        })
        lst = j["data"].get("list") or []
        out.extend(lst)
        total = j["data"].get("metadata", {}).get("_total", 0)
        if len(lst) < page_size or len(out) >= total:
            break
        page += 1
        time.sleep(0.4)
    return out


def find_or_create_folder(s: requests.Session, name: str) -> str:
    """在网盘根目录找同名文件夹，没有就建一个，返回 fid"""
    for f in list_dir(s, "0"):
        if f.get("dir") and f.get("file_name") == name:
            return f["fid"]
    j = api_post(s, "/file", {
        "pdir_fid": "0", "file_name": name, "dir_path": "",
        "dir_init_lock": False,
    })
    log.info("已在我的网盘创建目录：%s", name)
    return j["data"]["fid"]


def save_share_files(s: requests.Session, pwd_id: str, stoken: str, src_dir_fid: str,
                     files: list[dict], to_fid: str) -> list[str]:
    """把分享里的文件转存到自己的网盘，返回新 fid 列表"""
    payload = {
        "fid_list": [f["fid"] for f in files],
        "fid_token_list": [f["fid_token"] for f in files],
        "to_pdir_fid": to_fid,
        "pwd_id": pwd_id,
        "stoken": stoken,
        "pdir_fid": src_dir_fid,
        "scene": "link",
    }
    j = api_post(s, "/share/sharepage/save", payload)
    task_id = j["data"].get("task_id")
    if not task_id:
        raise RuntimeError(f"转存未返回 task_id: {j}")

    # task_sync=true 时 save 的响应里已经带了结果，不用再轮询
    pre = ((j["data"].get("task_resp") or {}).get("data") or {})
    if pre.get("status") == 2:
        return _task_fids(pre)

    for i in range(60):
        time.sleep(1.2)
        t = api_get(s, "/task", {"pr": "ucpro", "fr": "pc", "task_id": task_id, "retry_index": i})
        d = t.get("data") or {}
        if d.get("status") == 2:
            return _task_fids(d)
        if d.get("status") not in (0, 1):
            raise RuntimeError(f"转存任务异常: {d}")
    raise RuntimeError("转存任务超时")


def _task_fids(d: dict) -> tuple[list[str], str]:
    """从 /task 的 data 里取新建 fid 和服务端说明。

    夸克转存失败时不一定给非 2 的 status，有时只是 fids 为空 + message 里写原因，
    所以把这个 message 一并带出来，出错时能直接看到。
    """
    sa = d.get("save_as") or {}
    fids = list(dict.fromkeys(
        (sa.get("save_as_top_fids") or []) + (sa.get("save_as_select_top_fids") or [])))
    msg = sa.get("message") or d.get("message") or ""
    if fids or msg:
        log.info("转存任务：新增 %d 个%s", len(fids), f"；服务端说明：{msg}" if msg else "")
    return fids, msg


_DUP_RE = re.compile(r"\s*\(\d+\)$")


def _norm_name(name: str) -> str:
    """归一化文件名，用来把「猫薄荷2.15(1).exe」对回「猫薄荷2.15.exe」。

    夸克转存遇到同名文件会自动加 (1)(2)，所以不能直接拿名字当 key。
    """
    p = Path(name)
    return (_DUP_RE.sub("", p.stem) + p.suffix).lower()


def _setcookies(r: requests.Response) -> list[str]:
    """把响应里所有 Set-Cookie 原样取出来（r.headers 会把多条用逗号拼起来，不能直接切）"""
    for getter in ("getlist", "get_all"):
        try:
            vals = getattr(r.raw.headers, getter)("Set-Cookie")
            if vals:
                return list(vals)
        except Exception:
            continue
    v = r.headers.get("set-cookie")
    return [v] if v else []


# 这里原本有个 _purge_dupes()：转存前把中转目录里的同名副本删掉，避免撞名变 (1)。
# 已彻底移除，别再加回来 —— 夸克网盘的回收站会让「删了再转存同名同内容」变成死局：
#   /file/delete 只是把文件丢进回收站，/share/sharepage/save 仍然认得这份内容，
#   于是把**回收站里那个 fid** 返回给你；拿它取直链 404 file not found，
#   拿它再删又报 23004 文件已经删除。删得越干净，下一次越下不动。
# 正确做法：留着副本当缓存（见 do_download 里的 _match_existing 复用逻辑）。


def _match_existing(items: list[dict], target: dict) -> str:
    """在网盘已有文件里找「同名 + 同大小」的副本。

    必须比大小：对方可能出新版但沿用同名，只按名字会拿到旧文件。
    """
    key = _norm_name(target["name"])
    want = int(target.get("size") or 0)
    for f in items:
        if f.get("dir"):
            continue
        if _norm_name(f["file_name"]) == key and int(f.get("size") or 0) == want:
            return f["fid"]
    return ""


def _transfer_and_locate(s: requests.Session, cfg: dict, stoken: str, src_fid: str,
                         to_fid: str, group: list[dict]) -> dict[str, str]:
    """转存，然后在自己的网盘里按「文件名 + 大小」找回真实 fid。

    为什么不信 /share/sharepage/save 返回的 save_as_top_fids：
    同名内容在回收站里时它会吐回**那个已经被删掉**的 fid，
    再拿它去 /file/download 就是 file not found。以目录实际内容为准最稳。
    """
    found: dict[str, str] = {}
    todo = group
    for attempt in (1, 2):
        task_fids, task_msg = save_share_files(s, cfg["pwd_id"], stoken, src_fid, todo, to_fid)

        # 文件真正出现在目录里才算数：列表接口有延迟，多等几拍
        items: list[dict] = []
        for _ in range(6):
            time.sleep(1.2)
            try:
                items = [f for f in list_dir(s, to_fid) if not f.get("dir")]
            except Exception as e:
                log.warning("转存后列举目录失败：%s", _redact(str(e)))
                continue
            if any(_match_existing(items, g) for g in todo):
                break

        for g in todo:
            hit = _match_existing(items, g)
            if hit:
                found[_norm_name(g["name"])] = hit

        # 单个文件时，名字没对上就信任务返回的 fid（下载那步还会再兜一次底）
        if len(todo) == 1 and len(task_fids) == 1:
            found.setdefault(_norm_name(todo[0]["name"]), task_fids[0])

        todo = [g for g in group if _norm_name(g["name"]) not in found]
        if not todo:
            break
        if attempt == 1:
            log.warning("转存后还差 %d 个文件没出现，再试一次", len(todo))
            time.sleep(1.5)
        elif task_msg:
            log.error("转存未成功，服务端说明：%s", task_msg)
    return found


def _download_one(s: requests.Session, fid: str, dest: Path, expect: int = 0) -> int:
    """按单个 fid 取直链再下载。批量取直链时一个失败会拖垮整批，所以逐个来。"""
    item = None
    for attempt in (1, 2):
        try:
            j = api_post(s, "/file/download", {"fids": [fid]})
            for it in j.get("data") or []:
                if it.get("download_url"):
                    item = it
                    break
        except RuntimeError as e:
            log.warning("取直链失败（第 %d 次）：%s", attempt, _redact(str(e)))
        if item:
            break
        time.sleep(1.5)
    if not item:
        raise RuntimeError("拿不到下载直链")

    size = download_stream(s, item["download_url"], dest)
    exp = int(item.get("size") or expect or 0)
    if exp and size != exp:
        raise RuntimeError(f"大小不符（{size} != {exp}）")
    return size


def _strip_title(rel: str, title: str) -> str:
    """分享的顶级目录往往和分享标题同名，落盘时去掉避免双层嵌套"""
    if not rel:
        return ""
    parts = rel.split("/")
    if parts and parts[0] == title:
        parts = parts[1:]
    return "/".join(parts)


def download_stream(s: requests.Session, url: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    with s.get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as fp:
            for chunk in r.iter_content(1024 * 512):
                if not chunk:
                    continue
                fp.write(chunk)
                got += len(chunk)
                if total and got % (5 * 1024 * 1024) < 1024 * 512:
                    log.info("  ... %s  %.1f/%.1f MB", dest.name, got / 1048576, total / 1048576)
    try:
        tmp.replace(dest)
    except PermissionError:
        raise RuntimeError(f"{dest.name} 正在运行中，先关掉它再更新")
    return got

# ---------------------------------------------------------------- 主流程
def fetch_snapshot(cfg: dict) -> tuple[dict, list[dict]]:
    """匿名读分享，返回 (分享信息, 文件列表)"""
    s = session_with(None, with_cookie=False, proxy=cfg.get("proxy"))
    info = get_share_token(s, cfg["pwd_id"], cfg.get("passcode", ""))
    stoken = info["stoken"]
    files = walk_share(s, cfg["pwd_id"], stoken, cfg.get("root_fid") or "0")
    return {"title": info.get("title", "夸克分享"), "stoken": stoken}, files


def diff(state_files: dict, share_files: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    new, changed = [], []
    current = {f["fid"] for f in share_files}
    for f in share_files:
        old = state_files.get(f["fid"])
        if old is None:
            new.append(f)
        elif old.get("size") != f["size"] or old.get("updated_at") != f["updated_at"]:
            changed.append(f)
    removed = [fid for fid in state_files if fid not in current]
    return new, changed, removed


def do_download(cfg: dict, share: dict, info: dict, targets: list[dict]) -> int:
    cookie = cfg.get("cookie") or ""
    if not cookie.strip():
        log.warning("未配置 Cookie —— 只提醒不下载。请把浏览器 Cookie 填进 config.json")
        return 0

    s = session_with(cookie, proxy=cfg.get("proxy"))
    raw_dir = (cfg.get("download_dir") or "").strip()
    if raw_dir:
        dest_root = Path(raw_dir).expanduser()
    else:
        # 空 = 与程序同目录（exe 的所在文件夹）
        exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        dest_root = exe.resolve().parent
    if not cfg.get("flat"):
        dest_root = dest_root / info["title"]
    dest_root.mkdir(parents=True, exist_ok=True)

    # 按来源目录分组转存（转存需要指定源目录 fid）
    ok = 0
    failed = 0
    try:
        to_fid = find_or_create_folder(s, cfg.get("drive_folder", "夸克同步"))
    except RuntimeError as e:
        log.error("登录态可能已失效：%s", _redact(str(e)))
        _write_todo(dest_root, targets, "Cookie 失效")
        return 0

    # 一次性转存所有目标（都在同一源目录时），否则逐个目录处理
    by_dir: dict[str, list[dict]] = {}
    for f in targets:
        by_dir.setdefault(f.get("dir_fid") or (cfg.get("root_fid") or "0"), []).append(f)

    for src_fid, group in by_dir.items():
        # 先用网盘里已有的同版本副本；没有再转存。
        # 为什么不转存完就删：删掉的会进回收站，同一份内容再次转存时
        # 夸克会把**回收站里的 fid** 返回给你（取直链必 404、删又说已删除）。
        # 留着副本不给账号制造墓碑，下次更新也更快。
        reuse: dict[str, str] = {}
        pending: list[dict] = []
        try:
            existing = [f for f in list_dir(s, to_fid) if not f.get("dir")]
        except Exception:
            existing = []
        for g in group:
            hit = _match_existing(existing, g)
            if hit:
                reuse[_norm_name(g["name"])] = hit
                log.info("网盘已有同版本副本，直接取直链：%s", g["name"])
            else:
                pending.append(g)

        located = dict(reuse)
        if pending:
            try:
                located.update(_transfer_and_locate(
                    s, cfg, info["stoken"], src_fid, to_fid, pending))
            except RuntimeError as e:
                log.error("转存失败：%s", _redact(str(e)))
                _write_todo(dest_root, pending, _redact(str(e)))
                failed += len(pending)

        for g in group:
            fid = located.get(_norm_name(g["name"]))
            if not fid:
                log.error("网盘里没有可用的副本：%s", g["name"])
                _write_todo(dest_root, [g], "转存后找不到文件")
                failed += 1
                continue
            rel = _strip_title(g.get("rel") or "", info["title"])
            dest = dest_root / rel / g["name"] if rel else dest_root / g["name"]
            if dest.exists() and dest.stat().st_size == int(g.get("size") or 0):
                log.info("已存在，跳过：%s", g["name"])
                ok += 1
                continue
            try:
                size = _download_one(s, fid, dest, int(g.get("size") or 0))
                log.info("下载完成：%s  (%.2f MB)", g["name"], size / 1048576)
                ok += 1
            except Exception as e:
                log.error("下载失败 %s：%s", g["name"], _redact(str(e)))
                _write_todo(dest_root, [g], _redact(str(e)))
                failed += 1

    # 这轮全成功了就把上一次的待办清掉，免得留个「未完成」误导人
    if failed == 0:
        try:
            (DATA_DIR / "待办.txt").unlink(missing_ok=True)
        except Exception:
            pass
    return ok


def _write_todo(dest_root: Path, files: list[dict], reason: str) -> None:
    """只写进隐藏的日志目录，绝不在下载目录（通常是桌面）留任何杂文件"""
    try:
        p = DATA_DIR / "待办.txt"
        lines = [f"# {datetime.now():%Y-%m-%d %H:%M}  自动下载未完成：{reason}", ""]
        for f in files:
            lines.append(f"{f['name']}    ({f['size'] / 1048576:.2f} MB)")
        p.write_text("\n".join(lines), encoding="utf-8")
        log.warning("待办已记录到隐藏目录：%s", p)
    except Exception:
        log.warning("待办写入失败（不影响主流程）")


def launch_newest(cfg: dict, title: str) -> None:
    """启动下载目录里最新的匹配文件（默认关闭，需在 config 里开启）"""
    if not cfg.get("auto_launch"):
        return
    raw_dir = (cfg.get("download_dir") or "").strip()
    if raw_dir:
        base = Path(raw_dir).expanduser()
    else:
        exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        base = exe.resolve().parent
    dest_root = base if cfg.get("flat") else base / title
    pat = cfg.get("launch_pattern", "*.exe")
    own = {"夸克更新助手.exe", "QuarkWatcher.exe", Path(sys.executable).name}
    cands = [p for p in dest_root.rglob(pat)
             if p.is_file() and p.name not in own]
    if not cands:
        log.info("没有匹配 %s 的文件可启动", pat)
        return
    newest = max(cands, key=lambda p: p.stat().st_mtime)
    log.info("启动：%s", newest)
    os.startfile(str(newest))  # noqa: S606


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="建立基线，不下载")
    ap.add_argument("--check", action="store_true", help="只检查不下载")
    ap.add_argument("--probe", action="store_true", help="自检")
    ap.add_argument("--launch", action="store_true", help="启动最新的匹配文件")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()

    if args.probe:
        info, files = fetch_snapshot(cfg)
        log.info("分享标题：%s", info["title"])
        log.info("匿名读取成功，文件 %d 个", len(files))
        cookie = cfg.get("cookie") or ""
        if not cookie.strip():
            log.warning("Cookie：未配置")
        else:
            try:
                s = session_with(cookie, proxy=cfg.get("proxy"))
                find_or_create_folder(s, cfg.get("drive_folder", "夸克同步"))
                log.info("Cookie：有效")
            except Exception as e:
                log.error("Cookie：无效（%s）", _redact(str(e)))
        return 0

    info, files = fetch_snapshot(cfg)
    log.info("分享「%s」当前 %d 个文件", info["title"], len(files))

    state = load_state()
    new, changed, removed = diff(state.get("files", {}), files)

    if args.init:
        state["files"] = {f["fid"]: {"name": f["name"], "size": f["size"],
                                     "updated_at": f["updated_at"]} for f in files}
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        log.info("基线已建立：%d 个文件", len(files))
        return 0

    if removed:
        for fid in removed:
            log.info("分享中已移除：%s", state["files"][fid]["name"])

    if not new and not changed:
        log.info("无更新")
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return 0

    for f in new:
        log.info("新增：%s  (%.2f MB)", f["name"], f["size"] / 1048576)
    for f in changed:
        log.info("更新：%s  (%.2f MB)", f["name"], f["size"] / 1048576)

    if args.check:
        return 0

    targets = new + changed
    # 记录源目录 fid，供转存使用
    s_anon = session_with(None, with_cookie=False, proxy=cfg.get("proxy"))
    targets = _annotate_dirs(cfg, s_anon, info["stoken"], targets)

    done = do_download(cfg, {}, info, targets)

    # 只要下载过，就更新状态（失败的文件名记下来，下轮还会重试）
    for f in targets:
        state["files"][f["fid"]] = {"name": f["name"], "size": f["size"],
                                    "updated_at": f["updated_at"]}
    state["last_check"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)
    log.info("本轮完成，成功 %d / %d", done, len(targets))

    if done:
        launch_newest(cfg, info["title"])
    return 0


def _annotate_dirs(cfg: dict, s: requests.Session, stoken: str, targets: list[dict]) -> list[dict]:
    """给每个目标文件标上它所在的分享目录 fid（walk_share 已带的话直接跳过）"""
    if targets and all(f.get("dir_fid") for f in targets):
        return targets
    root = cfg.get("root_fid") or "0"
    wanted = {f["fid"] for f in targets}
    index: dict[str, str] = {}

    def rec(pdir: str, depth: int = 0):
        if depth > 4:
            return
        for e in list_share_dir(s, cfg["pwd_id"], stoken, pdir):
            if e["fid"] in wanted:
                index[e["fid"]] = pdir
            if e.get("dir"):
                rec(e["fid"], depth + 1)

    rec(root)
    for f in targets:
        f["dir_fid"] = index.get(f["fid"], root)
    return targets


if __name__ == "__main__":
    raise SystemExit(main())
