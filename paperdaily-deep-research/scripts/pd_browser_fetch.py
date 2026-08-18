#!/usr/bin/env python3
"""
pd_browser_fetch.py — get a PDF out of the browser session you already have.

The layer above this one (fetch_fulltext.py) speaks plain HTTP and therefore
sees the internet as an anonymous client: Cloudflare-fronted journals answer
403, EZproxy/SSO-gated content answers a login form, and repositories that
build their download link in JavaScript answer HTML. Your own browser sees all
three fine, because it holds the cookies — `cf_clearance` from a challenge you
already passed, the campus proxy session, the publisher SSO.

This script borrows that session instead of trying to reproduce it. It attaches
to a Chrome you are already running, over the DevTools Protocol, and pulls the
bytes down the same authenticated path the browser would use.

────────────────────────────────────────────────────────────────────────────
WHY IT IS NOT "JUST DRIVE THE BROWSER"
────────────────────────────────────────────────────────────────────────────
Screenshot-and-click automation can *display* a PDF and still never produce a
file — Chrome renders inline PDFs in a viewer plugin whose bytes are not in the
DOM, and a "download" click lands in the browser's own download machinery with
no path back to the caller. That failure (2026-08-02: PDF visible on screen,
nothing on disk, 22 minutes spent) is what this script exists to remove. There
are exactly two mechanisms that actually yield bytes, and it uses both:

  ① in-page `fetch(url, {credentials:'include'})` executed **in the landing
     page's own context**, base64 back over CDP. Same-origin, so cookies and
     Referer go automatically and Cloudflare sees an already-cleared client.
     Run it from the landing page, not from the PDF viewer page.
  ② `Browser.setDownloadBehavior {behavior:"allowAndName"}` + navigate, then
     watch `Browser.downloadProgress` to completion. Covers
     `Content-Disposition: attachment` and pages whose CSP blocks ①.

────────────────────────────────────────────────────────────────────────────
COMPLIANCE / SCOPE
────────────────────────────────────────────────────────────────────────────
This is the user's own browser, the user's own logins, the user's own
entitlements — the same line fetch_fulltext.py's L5 already draws, one step
further along. It contains no Sci-Hub or shadow mirror, no credential handling
(it never sees or stores a password), no cookie export, and **no CAPTCHA
solving**: when a challenge or login form is detected the script stops and
waits for a human to clear it in the visible browser window. The agent never
answers a challenge; it waits for the person who is entitled to.

A page that, once loaded, exposes no PDF and no download is recorded `denied`
and the script moves on. Nothing here tries to defeat a wall.

────────────────────────────────────────────────────────────────────────────
SETUP (once)
────────────────────────────────────────────────────────────────────────────
Enable remote debugging in the Chrome you already use:
    open  chrome://inspect/#remote-debugging   → toggle the switch on
(or launch Chrome with `--remote-debugging-port=9222`). Then:
    python3 pd_browser_fetch.py --list

Using your everyday profile is the point — a fresh isolated profile has none of
the sessions that make this work.

────────────────────────────────────────────────────────────────────────────
CLI
────────────────────────────────────────────────────────────────────────────
  pd_browser_fetch.py --list
  pd_browser_fetch.py --url <landing-or-pdf-url> --out paper.pdf
  pd_browser_fetch.py --worklist w.jsonl --out pdfs/ [--report r.jsonl]
  pd_browser_fetch.py --search "<paper title>" [--n 8]

  --port N          CDP port (default 9222, or env PD_CDP_PORT)
  --wait-human N    seconds to wait for a human to clear a challenge/login
                    (default 180; 0 = never wait, fail immediately)
  --timeout N       per-navigation timeout, seconds (default 45)
  --keep-tab        leave the working tab open (default: close it)

Stdlib only — no playwright, no node, no pip install. The WebSocket client is
~120 lines at the bottom of this file, so any agent on any machine with python3
can run it. Python 3.9+.

Exit codes: 0 ok · 2 one or more failed · 1 usage/setup error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.1"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
PDF_MAGIC = b"%PDF"
DEFAULT_PORT = 9222
DEFAULT_WAIT_HUMAN = 180
DEFAULT_TIMEOUT = 45

# Substrings that mean "a human has to act before this page will give up its
# content". Kept deliberately broad: a false positive costs one poll cycle, a
# false negative costs a mysterious `denied` on a page the user could have
# cleared in three seconds.
_CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "cf-browser-verification",
    "enable javascript and cookies", "verify you are human", "captcha",
    "unusual traffic", "attention required",
)
_LOGIN_MARKERS = (
    "sign in", "log in", "login", "institutional access", "shibboleth",
    "ezproxy", "athens", "subscribe to view", "purchase access",
)


# ═══════════════════════════ minimal CDP client ═══════════════════════════
class CDPError(RuntimeError):
    pass


class WS:
    """RFC 6455 client, only what CDP needs: text frames, no extensions, no TLS.

    Why hand-rolled: CDP is a WebSocket protocol and the python standard library
    has no WebSocket client. The alternatives were a pip dependency (breaks
    "any agent, any machine") or shelling out to node (macOS ships python3, not
    node). 120 lines against a loopback socket is the smaller price.
    """

    def __init__(self, url: str, timeout: float = 30.0):
        p = urllib.parse.urlsplit(url)
        if p.scheme != "ws":
            raise CDPError("only ws:// supported (CDP is loopback): %s" % url)
        host, port = p.hostname or "127.0.0.1", p.port or 80
        path = p.path + (("?" + p.query) if p.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        self.sock.sendall(req.encode())
        try:
            head = self._read_until(b"\r\n\r\n")
        except (socket.timeout, TimeoutError):
            # A listening socket that accepts the connection and then says
            # nothing is the signature of a *stale* endpoint: Chrome kept the
            # port open after remote debugging was switched back off, and the
            # DevToolsActivePort file still names it. Silence here otherwise
            # surfaces as a bare TimeoutError with no way to act on it.
            raise CDPError(
                "connected to %s:%d but Chrome never answered the WebSocket "
                "upgrade.\n  That port is stale — remote debugging is most "
                "likely switched off now.\n%s" % (host, port, _SETUP_HINT))
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CDPError("websocket handshake failed: %s" % head[:200])

    # ── framing ──
    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("connection closed during handshake")
            self._buf += chunk
        i = self._buf.index(marker) + len(marker)
        out, self._buf = self._buf[:i], self._buf[i:]
        return out

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise CDPError("connection closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        payload = text.encode("utf-8")
        n = len(payload)
        header = bytearray([0x81])  # FIN + text
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        """Next complete text message; transparently handles fragmentation,
        ping/pong and binary frames (CDP never sends binary, but a stray one
        must not desynchronise the stream)."""
        chunks: List[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            if b1 & 0x80:  # server frames must not be masked, but be liberal
                mask = self._read_exact(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(ln)))
            else:
                data = self._read_exact(ln)
            if opcode == 0x8:
                raise CDPError("websocket closed by browser")
            if opcode == 0x9:  # ping → pong, keep the same payload
                frame = bytearray([0x8A, 0x80 | len(data)])
                mask = os.urandom(4)
                frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                self.sock.sendall(bytes(frame))
                continue
            if opcode == 0xA:
                continue
            chunks.append(data)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


_SETUP_HINT = (
    "  Enable it: open chrome://inspect/#remote-debugging and toggle the switch,\n"
    "  or start Chrome with --remote-debugging-port=9222. Use your everyday\n"
    "  profile — a fresh one has none of the sessions this relies on.")


def _port_file_candidates() -> List[str]:
    home = os.path.expanduser("~")
    env = os.environ.get("CDP_PORT_FILE")
    out = [env] if env else []
    brands = ("Google/Chrome", "Google/Chrome Beta", "Google/Chrome Canary",
              "Chromium", "BraveSoftware/Brave-Browser", "Microsoft Edge", "Vivaldi")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = os.path.join(home, ".config")
        brands = tuple(b.split("/")[-1].lower().replace(" ", "-") for b in brands)
    for b in brands:
        for tail in ("DevToolsActivePort", os.path.join("Default", "DevToolsActivePort"),
                     os.path.join("User Data", "DevToolsActivePort")):
            out.append(os.path.join(base, b, tail))
    return [p for p in out if p]


def _read_port_file(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().strip().split("\n")
    except OSError:
        return None
    if len(lines) >= 2 and lines[0].strip().isdigit() and lines[1].startswith("/"):
        return "ws://127.0.0.1:%s%s" % (lines[0].strip(), lines[1].strip())
    return None


def _discover_ws(port: int) -> str:
    """Browser-level WebSocket URL.

    Order matters twice over:

      ① An explicit `CDP_PORT_FILE` wins outright — that is the escape hatch for
        a non-standard profile location and must not be second-guessed.
      ② Then `/json/version` on the requested port, so an explicit `--port`
        actually selects that browser. Reading the port file first (the obvious
        arrangement) silently ignores `--port` and connects to whatever the
        default profile last wrote — which is how this function's first draft
        dialled a *stale* endpoint while a perfectly good browser was answering
        on the port that was asked for.
      ③ Finally the DevToolsActivePort files. This is the path that matters for
        the low-friction setup: remote debugging toggled on from
        chrome://inspect serves the WebSocket but leaves the /json HTTP
        endpoints returning 404, so ② cannot see it at all.
    """
    env = os.environ.get("CDP_PORT_FILE")
    if env:
        ws = _read_port_file(env)
        if ws:
            return ws
        raise CDPError("CDP_PORT_FILE=%s is not a readable DevToolsActivePort file"
                       % env)
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % port, timeout=5) as r:
            return json.loads(r.read().decode())["webSocketDebuggerUrl"]
    except Exception:
        pass
    for p in _port_file_candidates():
        ws = _read_port_file(p)
        if ws:
            return ws
    raise CDPError("cannot reach Chrome DevTools (nothing on 127.0.0.1:%d, no "
                   "DevToolsActivePort file).\n%s" % (port, _SETUP_HINT))


class CDP:
    """One browser connection, flat-session mode."""

    def __init__(self, port: int, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.ws = WS(_discover_ws(port), timeout=timeout)
        self._id = 0
        self._events: List[dict] = []

    def call(self, method: str, params: Optional[dict] = None,
             session: Optional[str] = None, timeout: Optional[float] = None) -> dict:
        self._id += 1
        mid = self._id
        msg: Dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            if time.monotonic() > deadline:
                raise CDPError("timeout waiting for %s" % method)
            try:
                m = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                raise CDPError("timeout waiting for %s" % method)
            if m.get("id") == mid:
                if "error" in m:
                    raise CDPError("%s: %s" % (method, m["error"].get("message")))
                return m.get("result") or {}
            if "method" in m:
                self._events.append(m)

    def wait_event(self, method: str, timeout: float,
                   pred=None) -> Optional[dict]:
        """Return the next matching event, checking already-buffered ones first."""
        for i, e in enumerate(self._events):
            if e.get("method") == method and (pred is None or pred(e)):
                return self._events.pop(i)
        deadline = time.monotonic() + timeout
        # Shrinking the socket timeout to poll for an event and then NOT
        # restoring it leaves every later call running on a fraction of a
        # second — which surfaces far away from here as an unhandled
        # TimeoutError in the middle of an unrelated command.
        prev = self.ws.sock.gettimeout()
        try:
            while time.monotonic() < deadline:
                self.ws.sock.settimeout(max(0.5, deadline - time.monotonic()))
                try:
                    m = json.loads(self.ws.recv())
                except (socket.timeout, OSError):
                    break
                except CDPError:
                    break
                if m.get("method") == method and (pred is None or pred(m)):
                    return m
                if "method" in m:
                    self._events.append(m)
            return None
        finally:
            try:
                self.ws.sock.settimeout(prev)
            except OSError:
                pass

    def close(self) -> None:
        self.ws.close()


# ═══════════════════════════ page helpers ═══════════════════════════
_JS_PAGE_STATE = """
(() => {
  const txt = (document.body ? document.body.innerText : '').slice(0, 4000);
  const metas = Array.from(document.querySelectorAll('meta')).filter(
      m => (m.getAttribute('name')||m.getAttribute('property')||'').toLowerCase()
           === 'citation_pdf_url');
  const links = Array.from(document.querySelectorAll('a[href]'))
      .map(a => a.href)
      .filter(h => /\\.pdf($|[?#])|\\/pdf\\/|pdfdirect|\\/doi\\/pdf|type=printable/i.test(h));
  return {
    url: location.href,
    title: document.title || '',
    contentType: document.contentType || '',
    text: txt,
    citationPdf: metas.length ? metas[0].getAttribute('content') : null,
    links: links.slice(0, 12),
  };
})()
"""

_JS_FETCH_B64 = """
(async () => {
  try {
    const r = await fetch(%s, {credentials: 'include'});
    if (!r.ok) return {error: 'http ' + r.status};
    const buf = new Uint8Array(await r.arrayBuffer());
    if (buf.length > %d) return {error: 'exceeds_50mb'};
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < buf.length; i += CH) {
      s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
    }
    return {b64: btoa(s), n: buf.length,
            ct: r.headers.get('content-type') || ''};
  } catch (e) {
    return {error: String(e).slice(0, 200)};
  }
})()
"""


def _looks_blocked(state: dict) -> Optional[str]:
    hay = ((state.get("title") or "") + " " + (state.get("text") or "")).lower()
    for m in _CHALLENGE_MARKERS:
        if m in hay:
            return "challenge"
    # A login marker only counts when the page is otherwise empty of content —
    # every publisher page has a "Sign in" link in its header, and treating
    # that as a wall would stall on pages that are perfectly readable.
    if len(state.get("text") or "") < 1200:
        for m in _LOGIN_MARKERS:
            if m in hay:
                return "login"
    return None


def _evaluate(cdp: CDP, session: str, expr: str, timeout: float) -> Any:
    r = cdp.call("Runtime.evaluate",
                 {"expression": expr, "returnByValue": True,
                  "awaitPromise": True, "timeout": int(timeout * 1000)},
                 session=session, timeout=timeout + 10)
    if r.get("exceptionDetails"):
        raise CDPError("js: %s" % json.dumps(r["exceptionDetails"])[:200])
    return (r.get("result") or {}).get("value")


def _pdf_candidates(state: dict, page_url: str) -> List[str]:
    out: List[str] = []
    if (state.get("contentType") or "").lower().startswith("application/pdf"):
        out.append(state.get("url") or page_url)
    cu = state.get("citationPdf")
    if cu:
        out.append(urllib.parse.urljoin(state.get("url") or page_url, cu))
    for h in (state.get("links") or []):
        if h not in out:
            out.append(h)
    return out


def _download_via_behavior(cdp: CDP, session: str, target_id: str, url: str,
                           dest_dir: str, timeout: float) -> Optional[bytes]:
    """Mechanism ②: let Chrome's own downloader write the file, then read it.

    This is the path that the click-a-button approach was missing — the browser
    happily downloads, but only `setDownloadBehavior` gives the caller a
    directory and a completion event to key off.
    """
    tmp = os.path.join(dest_dir, ".pd-cdp-dl")
    os.makedirs(tmp, exist_ok=True)
    for f in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, f))
        except OSError:
            pass
    try:
        cdp.call("Browser.setDownloadBehavior",
                 {"behavior": "allowAndName", "downloadPath": os.path.abspath(tmp),
                  "eventsEnabled": True})
    except CDPError:
        return None
    try:
        cdp.call("Page.navigate", {"url": url}, session=session, timeout=timeout)
    except CDPError:
        pass  # a pure download navigation often reports as aborted; that's fine
    ev = cdp.wait_event("Browser.downloadProgress", timeout,
                        pred=lambda e: (e.get("params") or {}).get("state")
                        in ("completed", "canceled"))
    if not ev or (ev.get("params") or {}).get("state") != "completed":
        return None
    files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    if os.path.getsize(newest) > MAX_DOWNLOAD_BYTES:
        return None
    with open(newest, "rb") as fh:
        return fh.read()


# 出版商页面上"你的机构是谁"的回显。Springer 最实在，会把出口 IP、机构名和
# 授权账号一起写在页面上；其余几家只给机构名。
_WHOAMI_PROBES = (
    ("Springer", "https://link.springer.com/journal/10683"),
    ("Emerald",  "https://www.emerald.com/insight/publication/issn/0307-4358"),
)
_JS_WHOAMI = r"""(() => {
  const t = document.body ? document.body.innerText : '';
  const out = [];
  const pats = [
    /Access provided by[^\n]{0,90}/gi,
    /\b\d{1,3}(?:\.\d{1,3}){3}\s+[A-Z][^\n]{0,90}(?:University|Institute|College|Consortium)[^\n]{0,60}/g,
    /[^\n]{0,60}(?:University|Institute|College)\s+of\s+[^\n]{0,50}/g,
  ];
  for (const p of pats) { const m = t.match(p); if (m) out.push(...m.slice(0, 2)); }
  return { hits: [...new Set(out)].slice(0, 4) };
})()"""


def whoami(cdp: "CDP", timeout: float) -> int:
    """出版商眼里，这个浏览器属于哪个机构？

    本地直连模式最容易踩的坑，是**以为自己在校园网内而实际不在**——挂着 VPN、
    在家、或走了运营商出口。那时每一篇都会返回"无权限"，看上去像图书馆没订，
    其实只是出口 IP 不对。IP 段自己是猜不出来的（学校的授权网段没有公开清单），
    所以这里不猜：直接打开出版商页面，读它自己回显的机构名。

    这也解释了为什么不能靠 headless/无 cookie 的客户端做这件事——授权是按出口
    IP 判的，而判定结果必须由真正要去取全文的那个会话来验证。
    """
    try:
        ip = urllib.request.urlopen("https://ifconfig.me/ip", timeout=10).read().decode().strip()
    except Exception:
        ip = "?"
    sys.stderr.write("浏览器出口 IP: %s\n" % ip)

    found = False
    for name, url in _WHOAMI_PROBES:
        t = cdp.call("Target.createTarget", {"url": "about:blank"})
        tid = t["targetId"]
        sess = cdp.call("Target.attachToTarget",
                        {"targetId": tid, "flatten": True})["sessionId"]
        try:
            cdp.call("Page.enable", session=sess)
            cdp.call("Runtime.enable", session=sess)
            cdp.call("Page.navigate", {"url": url}, session=sess, timeout=timeout)
            cdp.wait_event("Page.loadEventFired", min(timeout, 30.0))
            time.sleep(1.5)
            raw = _evaluate(cdp, sess, _JS_WHOAMI, timeout)
            hits = (raw or {}).get("hits") or []
            if hits:
                found = True
                sys.stderr.write("  %-9s → %s\n" % (name, hits[0][:110]))
            else:
                sys.stderr.write("  %-9s → 页面没有回显机构（未必代表没授权）\n" % name)
        except Exception as e:
            sys.stderr.write("  %-9s → 打不开：%s\n" % (name, str(e)[:70]))
        finally:
            try:
                cdp.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass

    if found:
        sys.stderr.write("\n认出了机构身份——这个浏览器处在授权网络内，可以直接取全文。\n")
        return 0
    sys.stderr.write(
        "\n没有任何出版商认出机构身份。\n"
        "  多半是出口 IP 不在学校的授权网段（VPN / 在家 / 走了运营商出口）。\n"
        "  在这种状态下跑批量取全文，会把一整批**订阅内**的论文记成没权限。\n"
        "  先接校园网（或断开 VPN）再跑。\n")
    return 1


def _focus_for_human(cdp: "CDP", target_id: str, session: str) -> None:
    """把需要人工处理的那一页顶到人眼前。

    没有这一步，"停下来等人"就是空转：标签页是后台建的，Chrome 窗口多半压在
    终端后面，提示只有 stderr 上一行字——人根本不知道要去点什么，于是等满
    180 秒超时，账本记一笔失败，而验证其实点一下就过了。

    三层都试，各自失败都不致命：标签页在窗口内置顶、窗口在 Chrome 内置顶、
    Chrome 应用本身抢到焦点。最后一层是平台相关的，只在 macOS 做——抢焦点
    是有代价的行为，只在确实需要人动手时才做，正常取全文的路径上不碰。
    """
    for method, params, kw in (
        ("Target.activateTarget", {"targetId": target_id}, {}),
        ("Page.bringToFront", {}, {"session": session}),
    ):
        try:
            cdp.call(method, params, **kw)
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to activate'],
                capture_output=True, timeout=5)
        except Exception:
            pass


def fetch_one(cdp: CDP, url: str, dest_dir: str, wait_human: int,
              timeout: float, keep_tab: bool) -> Dict[str, Any]:
    """Open `url` in a new tab, clear any human gate, return the PDF bytes."""
    rec: Dict[str, Any] = {"url": url, "status": "failed", "mechanism": None,
                           "pdf_url": None, "bytes": None, "note": None,
                           "_pdf": None}
    t = cdp.call("Target.createTarget", {"url": "about:blank"})
    target_id = t["targetId"]
    session = cdp.call("Target.attachToTarget",
                       {"targetId": target_id, "flatten": True})["sessionId"]
    try:
        cdp.call("Page.enable", session=session)
        cdp.call("Runtime.enable", session=session)
        try:
            cdp.call("Page.navigate", {"url": url}, session=session, timeout=timeout)
        except CDPError as e:
            rec["note"] = "navigate: %s" % e
            return rec
        cdp.wait_event("Page.loadEventFired", min(timeout, 30.0))
        time.sleep(1.0)  # let JS-built download links attach

        state = _evaluate(cdp, session, _JS_PAGE_STATE, timeout)
        blocked = _looks_blocked(state or {})
        if blocked:
            if wait_human <= 0:
                rec["status"] = "blocked"
                rec["note"] = "%s detected and --wait-human 0" % blocked
                return rec
            _focus_for_human(cdp, target_id, session)
            sys.stderr.write(
                "\a\n  ⟨需要人工⟩ %s 触发了%s。\n"
                "  已把该标签页切到前台，请在 Chrome 里点完验证/登录，脚本会自动继续\n"
                "  （最多等 %d 秒；这一步 agent 不会、也不该代做）。\n    %s\n\n"
                % (urllib.parse.urlsplit(url).hostname or url,
                   "人机验证" if blocked == "challenge" else "登录墙",
                   wait_human, url))
            sys.stderr.flush()
            deadline = time.monotonic() + wait_human
            while time.monotonic() < deadline:
                time.sleep(3.0)
                try:
                    state = _evaluate(cdp, session, _JS_PAGE_STATE, timeout)
                except CDPError:
                    continue
                if not _looks_blocked(state or {}):
                    sys.stderr.write("  ⟨已放行⟩ 继续取全文\n")
                    sys.stderr.flush()
                    break
            else:
                # 人没来点，不代表没权限——判 denied 会让上游"别重试"，
                # 而这恰恰是最该重试（或让人再点一次）的一种失败。
                rec["status"] = "blocked"
                rec["note"] = ("%s 在 %ds 内没被清掉（人未处理，非权限问题）"
                               % (blocked, wait_human))
                return rec

        cands = _pdf_candidates(state or {}, url)
        if not cands:
            rec["status"] = "denied"
            rec["note"] = "no pdf link on the rendered page"
            return rec

        for cand in cands[:4]:
            # ① same-origin fetch from the landing page's own context
            try:
                res = _evaluate(cdp, session,
                                _JS_FETCH_B64 % (json.dumps(cand), MAX_DOWNLOAD_BYTES),
                                timeout)
            except CDPError as e:
                res = {"error": str(e)[:120]}
            if isinstance(res, dict) and res.get("b64"):
                raw = base64.b64decode(res["b64"])
                if raw[:4] == PDF_MAGIC:
                    rec.update(status="ok", mechanism="in_page_fetch",
                               pdf_url=cand, bytes=len(raw), _pdf=raw)
                    return rec
                rec["note"] = "fetch returned non-pdf (%s)" % (res.get("ct") or "?")
            elif isinstance(res, dict):
                rec["note"] = "fetch: %s" % res.get("error")

            # ② hand it to Chrome's own downloader
            raw = _download_via_behavior(cdp, session, target_id, cand,
                                         dest_dir, timeout)
            if raw and raw[:4] == PDF_MAGIC:
                rec.update(status="ok", mechanism="browser_download",
                           pdf_url=cand, bytes=len(raw), _pdf=raw)
                return rec

        rec["status"] = "denied"
        rec["note"] = rec["note"] or "no candidate yielded %PDF bytes"
        return rec
    finally:
        if not keep_tab:
            try:
                cdp.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass


def search_titles(cdp: CDP, query: str, n: int, timeout: float,
                  keep_tab: bool) -> List[str]:
    """Run a web search in the user's own browser and return candidate URLs.

    The scriptable search APIs do not substitute for this: DuckDuckGo's HTML
    endpoints answer scripted clients with a results-free shell, Bing wants a
    paid key, and keyless Semantic Scholar 429s on the first request. A real
    browser session searching the open web is the one path that reaches the
    working-paper copies (author homepages, conference sites, seminar pages)
    that no scholarly aggregator indexes.
    """
    url = "https://duckduckgo.com/?q=" + urllib.parse.quote(query)
    t = cdp.call("Target.createTarget", {"url": "about:blank"})
    target_id = t["targetId"]
    session = cdp.call("Target.attachToTarget",
                       {"targetId": target_id, "flatten": True})["sessionId"]
    try:
        cdp.call("Page.enable", session=session)
        cdp.call("Runtime.enable", session=session)
        cdp.call("Page.navigate", {"url": url}, session=session, timeout=timeout)
        cdp.wait_event("Page.loadEventFired", min(timeout, 30.0))
        time.sleep(2.5)  # results render client-side
        js = """
        (() => Array.from(document.querySelectorAll('a[href]'))
            .map(a => a.href)
            .filter(h => /^https?:/.test(h) && !/duckduckgo\\.com/.test(h))
            .slice(0, 60))()
        """
        links = _evaluate(cdp, session, js, timeout) or []
        out, seen = [], set()
        for h in links:
            if h in seen:
                continue
            seen.add(h)
            out.append(h)
            if len(out) >= n:
                break
        return out
    finally:
        if not keep_tab:
            try:
                cdp.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass


# ═══════════════════════════ CLI ═══════════════════════════
def _safe_filename(paper_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(paper_id).strip()).strip("._") or "paper"
    return name[:200] + ".pdf"


def _cmd_list(port: int) -> int:
    """Targets over CDP, not over /json/list — the HTTP endpoints are 404 when
    debugging was enabled from chrome://inspect (see _discover_ws)."""
    try:
        cdp = CDP(port, timeout=10)
    except CDPError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    try:
        infos = cdp.call("Target.getTargets").get("targetInfos") or []
    finally:
        cdp.close()
    pages = [t for t in infos if t.get("type") == "page"]
    sys.stderr.write("connected — %d page target(s)\n" % len(pages))
    for t in pages[:20]:
        sys.stderr.write("  %-40s %s\n" % ((t.get("title") or "")[:40],
                                           (t.get("url") or "")[:80]))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pd_browser_fetch.py",
        description="Fetch full-text PDFs through the Chrome session you already have.")
    ap.add_argument("--list", action="store_true", help="show attachable Chrome targets")
    ap.add_argument("--url", help="single landing/PDF url to fetch")
    ap.add_argument("--worklist", help="worklist.jsonl (uses each row's oa_url/urls_extra/doi)")
    ap.add_argument("--search", help="run a web search in the browser, print candidate urls")
    ap.add_argument("--n", type=int, default=8, help="--search: how many urls to print")
    ap.add_argument("--out", help="output file (--url) or directory (--worklist)")
    ap.add_argument("--report", help="ledger jsonl path (--worklist mode)")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PD_CDP_PORT") or DEFAULT_PORT))
    ap.add_argument("--wait-human", type=int, default=DEFAULT_WAIT_HUMAN,
                    help="seconds to wait for a human to clear a challenge (0 = never)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--keep-tab", action="store_true")
    ap.add_argument("--whoami", action="store_true",
                    help="问出版商：这个浏览器属于哪个机构？（跑批量前先做一次）")
    args = ap.parse_args(argv)

    if args.list:
        return _cmd_list(args.port)
    if args.whoami:
        try:
            cdp = CDP(args.port, timeout=args.timeout)
        except Exception as e:
            sys.stderr.write("%s\n" % e)
            return 2
        try:
            return whoami(cdp, args.timeout)
        finally:
            try:
                cdp.close()
            except Exception:
                pass
    modes = sum(bool(x) for x in (args.url, args.worklist, args.search))
    if modes != 1:
        sys.stderr.write("error: need exactly one of --url / --worklist / --search "
                         "(or --list / --whoami)\n")
        return 1
    if not args.search and not args.out:
        sys.stderr.write("error: --out is required\n")
        return 1

    try:
        cdp = CDP(args.port, timeout=args.timeout)
    except CDPError as e:
        sys.stderr.write("%s\n" % e)
        return 1

    try:
        if args.search:
            for u in search_titles(cdp, args.search, args.n, args.timeout, args.keep_tab):
                print(u)
            return 0

        if args.url:
            dest = os.path.abspath(args.out)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            rec = fetch_one(cdp, args.url, os.path.dirname(dest) or ".",
                            args.wait_human, args.timeout, args.keep_tab)
            pdf = rec.pop("_pdf", None)
            if pdf:
                with open(dest, "wb") as fh:
                    fh.write(pdf)
                rec["file"] = dest
            print(json.dumps(rec, ensure_ascii=False))
            return 0 if rec["status"] == "ok" else 2

        # ── worklist mode ──
        out_dir = os.path.abspath(args.out)
        os.makedirs(out_dir, exist_ok=True)
        report = args.report or os.path.normpath(
            os.path.join(out_dir, "..", "browser_fetch_report.jsonl"))
        os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
        rows: List[dict] = []
        with open(args.worklist, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    if isinstance(o, dict):
                        rows.append(o)
                except json.JSONDecodeError:
                    pass
        ok = bad = 0
        with open(report, "w", encoding="utf-8") as rep:
            for i, row in enumerate(rows, 1):
                pid = str(row.get("id") or "paper%d" % i)
                fpath = os.path.join(out_dir, _safe_filename(pid))
                if os.path.isfile(fpath):
                    with open(fpath, "rb") as fh:
                        if fh.read(4) == PDF_MAGIC:
                            rep.write(json.dumps(
                                {"id": pid, "status": "already",
                                 "file": os.path.basename(fpath)}, ensure_ascii=False) + "\n")
                            rep.flush()
                            ok += 1
                            continue
                targets = [u for u in (list(row.get("urls_extra") or [])
                                       + [row.get("oa_url")]
                                       + (["https://doi.org/%s" % row["doi"]]
                                          if row.get("doi") else []))
                           if u]
                rec: Dict[str, Any] = {"id": pid, "status": "failed",
                                       "note": "no url to try"}
                for u in targets:
                    sys.stderr.write("[%d/%d] %s → %s\n" % (i, len(rows), pid, u[:70]))
                    sys.stderr.flush()
                    r = fetch_one(cdp, u, out_dir, args.wait_human,
                                  args.timeout, args.keep_tab)
                    pdf = r.pop("_pdf", None)
                    if pdf:
                        tmp = fpath + ".part"
                        with open(tmp, "wb") as fh:
                            fh.write(pdf)
                        os.replace(tmp, fpath)
                        r["file"] = os.path.basename(fpath)
                        r["carrier"] = "binary-pdf"
                    r["id"] = pid
                    rec = r
                    if r["status"] == "ok":
                        break
                rep.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rep.flush()
                if rec.get("status") in ("ok", "already"):
                    ok += 1
                else:
                    bad += 1
                sys.stderr.write("[%d/%d] %-8s %s\n" % (i, len(rows), rec["status"], pid))
                sys.stderr.flush()
        sys.stderr.write("browser: ok %d / failed %d of %d\nreport: %s\n"
                         % (ok, bad, len(rows), report))
        return 2 if bad else 0
    finally:
        # Best effort: leave the browser's download behaviour as we found it.
        try:
            cdp.call("Browser.setDownloadBehavior", {"behavior": "default"})
        except Exception:
            pass
        cdp.close()
        stale = os.path.join(os.path.abspath(args.out or "."), ".pd-cdp-dl")
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
