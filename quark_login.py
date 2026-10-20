# -*- coding: utf-8 -*-
"""夸克网盘微信扫码登录。

走的是夸克网页版那个「微信登录」弹窗的同一套协议：

  1. GET open.weixin.qq.com/connect/qrconnect  -> HTML 里的 uuid
  2. 二维码内容 = https://open.weixin.qq.com/connect/confirm?uuid=<uuid>
  3. 长轮询 lp.open.weixin.qq.com/connect/l/qrconnect
       wx_errcode=408 未扫 / 404 已扫待确认 / wx_code=xxx 成功
  4. POST pan.quark.cn/account/third_party_info  {third_party_token: code,...}
       -> data.service_ticket
  5. GET pan.quark.cn/account/info?st=<ticket> -> Set-Cookie
  6. 用 cookie 打 file/sort 自检
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

import requests

log = logging.getLogger("quarkwatch")


def _dbg(*parts) -> None:
    """把换取登录的原始交互记到独立文件，下次失败能一眼看出服务端到底回了什么"""
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "QuarkUpdateHelper" / "logs"
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "wx_login_debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] "
                    + " ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

WX_APPID = "wxcde89ee65c11b66a"                 # 夸克网盘网页版用的开放平台 appid
WX_QR_CONNECT = "https://open.weixin.qq.com/connect/qrconnect"
WX_CONFIRM = "https://open.weixin.qq.com/connect/confirm"
WX_LONGPOLL = "https://lp.open.weixin.qq.com/connect/l/qrconnect"

BIZ_HOST = "https://pan.quark.cn"
# 不带 need_bind_mobile 时，页面会用 authorize 分支；带 =1 时用 getAuthCode。
# authorize 是「已绑定账号」的正路，实测会直接 200 并下发 cookie；
# getAuthCode 只是把 code 透传回页面让它再 POST 一次，实测会被 302 打回。
LOGIN_PAGE = BIZ_HOST + "/wechat_qrcode"
AUTH_PATH = "authorize"


def _friendly(e: Exception) -> str:
    """把 requests 那一长串原始报错转成人话，别把 URL 糊到脸上"""
    name = type(e).__name__
    if isinstance(e, requests.exceptions.ProxyError):
        return "代理连不上"
    if isinstance(e, requests.exceptions.Timeout):
        return "请求超时"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "网络连接被中断"
    if isinstance(e, requests.exceptions.SSLError):
        return "证书校验失败"
    return name


def _setcookies(r: requests.Response) -> list[str]:
    """原样取出所有 Set-Cookie（r.headers 会把多条用逗号拼起来，不能直接切）"""
    for getter in ("getlist", "get_all"):
        try:
            vals = getattr(r.raw.headers, getter)("Set-Cookie")
            if vals:
                return list(vals)
        except Exception:
            continue
    v = r.headers.get("set-cookie")
    return [v] if v else []


class QrLogin:
    """一次微信扫码会话"""

    def __init__(self) -> None:
        self._use_env = False            # 先直连，失败再自动切系统代理
        self.s = self._mk_session(False)
        self.uuid = ""
        self._csrf = ""
        self._warmed = False

    @staticmethod
    def _mk_session(use_env: bool) -> requests.Session:
        s = requests.Session()
        s.trust_env = use_env
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        return s

    def _get(self, url: str, **kw):
        """直连不通就切系统代理（切过一次就一直用）"""
        try:
            return self.s.get(url, **kw)
        except (requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if self._use_env:
                raise
            self._use_env = True
            jar = {c.name: c.value for c in self.s.cookies}
            self.s = self._mk_session(True)
            for k, v in jar.items():
                try:
                    self.s.cookies.set(k, v)
                except Exception:
                    pass
            return self.s.get(url, **kw)

    # ---------------------------------------------------------- 预热拿 ctoken
    def warm_up(self, force: bool = False) -> None:
        """先访问一次登录页，服务端会下发 ctoken，后面 POST 要拿它做 X-Csrf-Token。

        force=True 用于换取前强制刷新一次 —— ctoken 过期或首次没拿到时，
        POST 会被 302 打回首页，表现就是「微信显示成功但换不到 cookie」。
        """
        if self._warmed and not force:
            return
        self._warmed = True
        try:
            self._get(LOGIN_PAGE, timeout=15,
                      headers={"Referer": BIZ_HOST + "/"})
        except Exception:
            return
        self._csrf = self.s.cookies.get("ctoken", "") or ""

    # ---------------------------------------------------------- 1. 二维码
    def new_token(self) -> str:
        self.warm_up()
        last = None
        for i in range(3):
            try:
                cb = (f"https://uop.quark.cn/cas/thirdparty/{AUTH_PATH}?"
                      + urlencode({
                          "client_id": "532", "display": "pc", "change_uid": "0",
                          "t": int(time.time() * 1000), "third_party_name": "wechat",
                          "la": "zh", "redirect_uri": LOGIN_PAGE,
                      }))
                q = WX_QR_CONNECT + "?" + urlencode({
                    "appid": WX_APPID,
                    "scope": "snsapi_login",
                    "redirect_uri": cb,
                    "login_type": "jssdk",
                    "self_redirect": "true",
                    "fast_login": "0",
                })
                r = self._get(q, timeout=15,
                              headers={"Referer": "https://open.weixin.qq.com/"})
                m = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", r.text)
                if m:
                    self.uuid = m.group(1)
                    return f"{WX_CONFIRM}?uuid={self.uuid}"
                last = "微信没返回二维码"
            except Exception as e:
                last = _friendly(e)
            time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"连不上微信登录服务：{last}")

    # ---------------------------------------------------------- 2. 画二维码
    @staticmethod
    def render(content: str, size: int = 300):
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        q = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M,
                          box_size=10, border=2)
        q.add_data(content)
        q.make(fit=True)
        img = q.make_image(fill_color="#1b1512", back_color="white").convert("RGB")
        return img.resize((size, size))

    # ---------------------------------------------------------- 3. 轮询
    def poll(self) -> tuple[str, str]:
        """返回 (state, detail)
        state: pending（未扫）| scanned（已扫待确认）| ok（detail 是微信 code）| error
        """
        if not self.uuid:
            return "error", "还没拿到二维码"
        try:
            r = self._get(WX_LONGPOLL,
                          params={"uuid": self.uuid, "_": int(time.time() * 1000)},
                          timeout=40,
                          headers={"Referer": "https://open.weixin.qq.com/"})
            txt = r.text or ""
        except Exception as e:
            return "pending", f"网络波动（{_friendly(e)}），重试中"

        m = re.search(r"wx_code='([^']*)'", txt)
        if m and m.group(1):
            _dbg("poll-ok", f"raw={txt[:200]!r}")
            return "ok", m.group(1)

        m = re.search(r"wx_errcode=(\d+)", txt)
        ec = int(m.group(1)) if m else 0
        if ec == 408:
            return "pending", "等待扫码"
        if ec == 404:
            _dbg("poll-scanned", f"raw={txt[:200]!r}")
            return "scanned", "已扫码，请在手机上点“确认登录”"
        if ec == 405:
            return "scanned", "微信处理中…"
        return "pending", f"等待中（{ec}）"

    # ---------------------------------------------------------- 4. 换 cookie
    DRIVE_KEYS = ["__pus", "__uid", "__kps", "__ktd", "__puus",
                  "_UP_A4A_11_", "_UP_D_", "ctoken", "b-user-id", "web-grey-id"]

    def _authorize(self, wx_code: str) -> tuple[str, str, dict[str, str], str]:
        """把微信 code 送进夸克 CAS（就是 qrconnect 的 redirect_uri）。

        浏览器里这步由微信重定向自动完成，页面只是接收结果。
        返回 (回传的 code, service_ticket, 本跳直接下发的 cookie, 响应体摘要)
        """
        cb = (f"https://uop.quark.cn/cas/thirdparty/{AUTH_PATH}?"
              + urlencode({
                  "client_id": "532", "display": "pc", "change_uid": "0",
                  "t": int(time.time() * 1000), "third_party_name": "wechat",
                  "la": "zh", "redirect_uri": LOGIN_PAGE, "code": wx_code,
              }))
        jar: dict[str, str] = {}
        try:
            r = self._get(cb, timeout=25, allow_redirects=False,
                          headers={"Referer": LOGIN_PAGE})
        except Exception as e:
            _dbg("authorize", "error", _friendly(e))
            return wx_code, "", jar, ""
        for raw in _setcookies(r):
            k, _, v = raw.split(";", 1)[0].partition("=")
            if k.strip():
                jar[k.strip()] = v.strip()

        loc = r.headers.get("location") or ""
        body = (r.text or "")[:500]
        qs = parse_qs(urlparse(loc).query)
        st = (qs.get("service_ticket") or [""])[0]
        new_code = (qs.get("code") or [""])[0]
        if not st:
            m = re.search(r'service_ticket["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-]{8,})', body)
            if m:
                st = m.group(1)
        _dbg("authorize", f"status={r.status_code}", f"loc={loc[:300]}",
             f"cookies={sorted(jar)}", f"st={'Y' if st else 'N'}",
             f"body={body[:200]!r}")
        return (new_code or wx_code), st, jar, body

    def _enrich_puus(self, cookie: str) -> str:
        """兑换出来的 cookie 有时缺 __puus（下载接口要它），打 flush 补一个。

        参考 H1d3rOne/Qingcao_Tools 的 enrich_cookie_string_with_puus。
        """
        if "__puus=" in cookie:
            return cookie
        try:
            r = self._get("https://drive-pc.quark.cn/1/clouddrive/auth/pc/flush",
                          params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
                          headers={"Cookie": cookie,
                                   "Referer": "https://pan.quark.cn/"},
                          timeout=20)
        except Exception as e:
            log.warning("补 __puus 失败（不影响登录）：%s", _friendly(e))
            return cookie
        for raw in _setcookies(r):
            k, _, v = raw.split(";", 1)[0].partition("=")
            if k.strip() == "__puus" and v.strip():
                log.info("已补齐 __puus")
                return f"{cookie}; __puus={v.strip()}"
        return cookie

    def _exchange(self, st: str) -> dict[str, str]:
        """service_ticket → 网盘 cookie。手动跟重定向，沿途每条 Set-Cookie 都收。"""
        jar: dict[str, str] = {}
        url = (f"{BIZ_HOST}/account/info?st={quote(st)}"
               "&lw=scan&fr=pc&platform=pc")
        for _ in range(6):
            try:
                r = self._get(url, timeout=25, allow_redirects=False,
                              headers={"Referer": LOGIN_PAGE})
            except Exception as e:
                raise RuntimeError(f"兑换 Cookie 失败：{_friendly(e)}")
            for raw in _setcookies(r):
                k, _, v = raw.split(";", 1)[0].partition("=")
                if k.strip():
                    jar[k.strip()] = v.strip()
            _dbg("account-info", f"status={r.status_code}",
                 f"loc={(r.headers.get('location') or '')[:160]}",
                 f"setcookie={len(jar)}")
            loc = r.headers.get("location")
            if not (r.is_redirect and loc):
                break
            url = urljoin(url, loc)
        return jar

    def finish(self, wx_code: str) -> str:
        wx_code = unquote(wx_code)

        # ① 过夸克 CAS：authorize 分支可能直接下发登录 cookie，也可能给 ticket
        code, st, hop_jar, _body = self._authorize(wx_code)
        jar: dict[str, str] = dict(hop_jar)
        has_creds = bool(jar.get("__pus") and jar.get("__uid"))

        # ② 还没凭证，就按页面 JS 的做法 POST third_party_info
        if not st and not has_creds:
            payload = {
                "third_party_token": code,
                "third_party_name": "wechat",
                "need_bind_mobile": True,
                "token_type": 2,
                "client_id": "532",
            }
            h = {"Referer": LOGIN_PAGE, "Origin": BIZ_HOST,
                 "Accept": "application/json, text/plain, */*"}

            r = None
            for attempt in (1, 2):
                # 每次强制刷一次 ctoken：它过期了 POST 会被 302 打回首页
                self.warm_up(force=True)
                hh = dict(h)
                if self._csrf:
                    hh["X-Csrf-Token"] = self._csrf
                try:
                    r = self._get(BIZ_HOST + "/account/third_party_info",
                                  params={"pr": "ucpro"}, json=payload,
                                  headers=hh, timeout=25, allow_redirects=False)
                except Exception as e:
                    raise RuntimeError(f"换取登录状态失败：{_friendly(e)}")

                for raw in _setcookies(r):
                    k, _, v = raw.split(";", 1)[0].partition("=")
                    if k.strip():
                        jar.setdefault(k.strip(), v.strip())
                _dbg(f"finish#{attempt}", f"code={code[:10]}…",
                     f"status={r.status_code}", f"loc={r.headers.get('location')}",
                     f"ct={r.headers.get('content-type')}",
                     f"csrf={'Y' if self._csrf else 'N'}",
                     f"body={(r.text or '')[:300]!r}")

                if r.status_code in (301, 302, 303, 307) and attempt == 1:
                    log.warning("换取被 %d 打回，刷新 ctoken 后重试一次", r.status_code)
                    time.sleep(0.6)
                    continue
                break

            j = None
            try:
                j = json.loads(r.text or "")
            except Exception:
                j = None

            if not isinstance(j, dict):
                if r.status_code in (301, 302, 303, 307):
                    raise RuntimeError(
                        "微信登录凭证被服务端拒绝（302 回首页）。"
                        "请确认这个微信已在 pan.quark.cn 绑定过夸克账号")
                raise RuntimeError(
                    f"服务器返回了非预期内容（HTTP {r.status_code} "
                    f"{r.headers.get('content-type') or ''}）：{(r.text or '')[:100]}")

            if not j.get("success"):
                c = j.get("code")
                if c == 50022:
                    raise RuntimeError(
                        "这个微信还没绑夸克手机号，先到 pan.quark.cn 用微信登录一次完成绑定")
                raise RuntimeError(f"登录失败：{j.get('message') or c}")

            st = (j.get("data") or {}).get("service_ticket")
            if not st:
                raise RuntimeError("服务器没给登录凭证")

        # ③ 有 ticket 但还没 cookie，就去兑换
        if st and not (jar.get("__pus") and jar.get("__uid")):
            jar.update(self._exchange(st))

        # 会话里已有的 quark 域 cookie 也带上（ctoken / web-grey-id 这些）
        for c in self.s.cookies:
            if "quark.cn" in (c.domain or ""):
                jar.setdefault(c.name, c.value)

        if not jar.get("__pus") or not jar.get("__uid"):
            raise RuntimeError("登录成功但没拿到网盘凭证（可能该微信还没开通夸克网盘）")

        order = [k for k in self.DRIVE_KEYS if k in jar]
        keys = order + [k for k in jar if k not in order]
        cookie = "; ".join(f"{k}={jar[k]}" for k in keys)
        return self._enrich_puus(cookie)

    # ---------------------------------------------------------- 5. 自检
    @staticmethod
    def verify(cookie: str) -> tuple[str, str]:
        """返回 (state, msg)，state 为 ok / expired / net。

        网络问题**不能**当成登录失效 —— 否则代理抖一下就去弹扫码。
        直连失败再走系统代理，两种都失败才算 net。
        """
        last = ""
        for use_env in (False, True):
            s = requests.Session()
            s.trust_env = use_env
            s.headers.update({"User-Agent": UA, "Referer": "https://pan.quark.cn/",
                              "Cookie": cookie})
            try:
                r = s.get("https://drive-pc.quark.cn/1/clouddrive/file/sort",
                          params={"pr": "ucpro", "fr": "pc", "pdir_fid": "0",
                                  "_page": 1, "_size": 5, "_fetch_total": 1},
                          timeout=15)
                j = r.json()
            except Exception as e:
                last = type(e).__name__
                continue
            code = j.get("code")
            if code == 0:
                n = len((j.get("data") or {}).get("list") or [])
                return "ok", f"登录有效（根目录 {n} 项）"
            if code in (31001, 31002, 31003, 10000):
                return "expired", "登录已过期，需要重新扫码"
            return "expired", f"登录无效：code={code} {j.get('message') or ''}"
        return "net", f"网络异常（{last}），本次跳过登录校验"
