"""Futu Web API client restricted to personal Hong Kong quote data."""

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


API = "https://webapi.futunn.com"
QUOTE_SOCKET = "wss://webapi-quote.futunn.com/ws"
SOURCE = "富途 OpenAPI"
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
QUOTE_SCOPE = "quote:read"
SYMBOL = re.compile(r"HK\.\d{5,6}\Z")
OPEN_STATES = {"MORNING", "AFTERNOON", "HK_CAS", "CLOSE_AUCTION", 3, 5, 19, 30}
CLOSED_STATES = {"AUCTION", "WAITING_OPEN", "REST", "CLOSED", "AFTERNOON_END", "NONE", 0, 1, 2, 4, 6, 31}
LIFETIME = 14 * 86400
PRICE_FIELDS = {
    "last_price": "last_price",
    "prev_close": "prev_close_price",
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "volume": "volume",
    "turnover": "turnover",
}


class FutuError(RuntimeError):
    pass


class AuthorizationRequired(FutuError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def post(path, values, *, token=None, form=False):
    data = urlencode(values).encode() if form else json.dumps(values).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(API + path, data=data, headers=headers, method="POST")
    try:
        with build_opener(NoRedirect()).open(request, timeout=12) as response:
            message = json.loads(response.read(2_000_000))
    except HTTPError as error:
        if form and error.code in {400, 401, 403}:
            raise AuthorizationRequired("富途拒绝授权或续期，请重新授权只读行情。") from None
        raise FutuError(f"富途接口返回 HTTP {error.code}。") from None
    except (URLError, TimeoutError, ValueError, OSError):
        raise FutuError("富途接口暂时不可用。") from None
    if not isinstance(message, dict):
        raise FutuError("富途接口返回格式异常。")
    error_code = message.get("error")
    if error_code or message.get("ret_code", 0) not in (0, "0"):
        if form and isinstance(error_code, str) and error_code in {"invalid_grant", "invalid_client", "access_denied"}:
            raise AuthorizationRequired("富途会话已过期或撤销，需重新授权。")
        raise FutuError("富途拒绝请求。请检查行情权限或稍后再试。")
    return message


def verify_scope(scope):
    if not isinstance(scope, str) or set(scope.split()) != {QUOTE_SCOPE}:
        raise AuthorizationRequired("仅接受 quote:read。请在富途授权页只选择只读行情，关闭账户及交易权限。")


class TokenVault:
    def __init__(self, data_dir):
        self.directory = Path(data_dir) / "futu-private"
        self.key = self.directory / "key"
        self.session = self.directory / "session.enc"

    def load(self):
        if not self.session.exists():
            return None
        try:
            for path in (self.key, self.session):
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                    raise AuthorizationRequired("富途凭证文件权限不安全，请检查文件所有者并设为 600。")
            session = json.loads(Fernet(self.key.read_bytes()).decrypt(self.session.read_bytes()))
            if not isinstance(session, dict) or not all(
                isinstance(session.get(key), str) and session[key]
                for key in ("client_id", "redirect_uri", "scope", "refresh_token")
            ) or not isinstance(session.get("renew_at"), (int, float)) or not number(session["renew_at"]):
                raise ValueError
            return session
        except (OSError, InvalidToken, ValueError):
            raise AuthorizationRequired("富途凭证无法读取，请重新授权。") from None

    def save(self, session):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(self.directory.lstat().st_mode) or self.directory.stat().st_uid != os.getuid():
            raise AuthorizationRequired("富途凭证目录必须由当前服务账号持有。")
        self.directory.chmod(0o700)
        try:
            fd = os.open(self.key, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600)
            with os.fdopen(fd, "wb") as key:
                key.write(Fernet.generate_key())
        except FileExistsError:
            pass
        if not stat.S_ISREG(self.key.lstat().st_mode) or self.key.stat().st_uid != os.getuid():
            raise AuthorizationRequired("富途凭证密钥文件必须由当前服务账号持有。")
        self.key.chmod(0o600)
        encrypted = Fernet(self.key.read_bytes()).encrypt(json.dumps(session).encode())
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as temp:
            staging = Path(temp.name)
            temp.write(encrypted)
        try:
            staging.replace(self.session)
        finally:
            staging.unlink(missing_ok=True)


def callback_code(url, redirect_uri, state):
    callback = urlsplit(url.strip())
    registered = urlsplit(redirect_uri)
    if (callback.scheme, callback.netloc, callback.path) != (registered.scheme, registered.netloc, registered.path):
        raise AuthorizationRequired("回调地址与富途授权申请不匹配。")
    query = parse_qs(callback.query, max_num_fields=12)
    if len(query.get("state", [])) != 1 or not hmac.compare_digest(query["state"][0].encode(), state.encode()):
        raise AuthorizationRequired("授权回调校验失败。")
    if len(query.get("code", [])) != 1 or query.get("error"):
        raise AuthorizationRequired("富途未返回授权码，请重新授权。")
    return query["code"][0]


def browser_callback(port, redirect_uri, state, url):
    result = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                code = callback_code(f"http://localhost:{port}{self.path}", redirect_uri, state)
            except (AuthorizationRequired, ValueError):
                status, message = 400, "富途授权回调无效，请返回授权页重试。"
            else:
                result.append(code)
                status, message = 200, "已接收富途授权回调。请返回终端查看授权结果。"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(message.encode())

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", port), CallbackHandler) as server:
        server.timeout = 1
        print("在浏览器打开下面的富途授权页，只授权行情读取：", url, sep="\n", flush=True)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result:
        raise AuthorizationRequired("未收到富途授权。请检查浏览器到服务器的端口转发，或使用 --paste。")
    return result[0]


def authorize(data_dir, port, paste):
    vault = TokenVault(data_dir)
    redirect_uri = f"http://localhost:{port}/callback"
    try:
        old = vault.load() or {}
    except AuthorizationRequired:
        old = {}
    if old.get("redirect_uri") == redirect_uri:
        client_id = old["client_id"]
    else:
        registration = post("/oauth2/register", {
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "港股工作台",
            "scope": QUOTE_SCOPE,
        })
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise AuthorizationRequired("富途未返回有效的应用 ID。")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = API + "/oauth2/authorize/confirm?" + urlencode({
        "client_id": client_id,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": QUOTE_SCOPE,
        "state": state,
    })
    if paste:
        if not sys.stdin.isatty():
            raise AuthorizationRequired("--paste 需要私人交互终端。")
        print("在浏览器打开下面的富途授权页，只授权行情读取：", url, sep="\n", flush=True)
        code = callback_code(getpass.getpass("粘贴浏览器完整回调地址（不会回显）："), redirect_uri, state)
    else:
        code = browser_callback(port, redirect_uri, state, url)
    issued = post("/oauth2/token", {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }, form=True)
    verify_scope(issued.get("scope"))
    refresh_token = issued.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthorizationRequired("富途未返回自动续期所需的授权。")
    vault.save({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": QUOTE_SCOPE,
        "refresh_token": refresh_token,
        "renew_at": time.time() + LIFETIME,
    })
    print("已保存富途只读行情授权。实时覆盖将在港股交易时段验证。")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def observation(symbol, data, captured, status):
    quote_time = number(data.get("data_time_ms") or data.get("update_time"))
    if not quote_time or not 946_684_800_000 <= quote_time <= (captured.timestamp() + 15) * 1000:
        return None
    values = {name: number(data.get(key)) for name, key in PRICE_FIELDS.items()}
    if not values["last_price"]:
        return None
    for name in ("prev_close", "open", "high", "low"):
        values[name] = values[name] or None
    return dict(
        values, symbol=symbol, source=SOURCE, status=status,
        quote_at=datetime.fromtimestamp(quote_time / 1000, timezone.utc).isoformat(timespec="milliseconds"),
        captured_at=captured.isoformat(timespec="milliseconds"),
    )


def result_rows(response, field):
    payload = response.get("data")
    rows = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise FutuError("富途行情接口返回格式异常。")
    return rows


class FutuQuotes:
    def __init__(self, data_dir, symbols, on_observation, on_status):
        self.vault = TokenVault(data_dir)
        self.get_symbols = symbols if callable(symbols) else lambda: symbols
        self.on_observation = on_observation
        self.on_status = on_status
        self.session = None
        self.access = None
        self.access_expires = 0
        self.market_state = None
        self.last_market_state_at = 0
        self.latest = {}
        self.push_times = {}
        self.push_verified = set()
        self.newest_push_at = {}
        self.published = {}
        self.last_status = None

    def symbols(self):
        return sorted({item for item in self.get_symbols() if isinstance(item, str) and SYMBOL.fullmatch(item)})[:60]

    def reauthorization_due(self):
        """Return the expected refresh credential expiry in ISO 8601, if available."""
        session = self.session or self.vault.load()
        if session and session.get("renew_at"):
            return datetime.fromtimestamp(session["renew_at"], timezone.utc).isoformat()
        return None

    def status(self, state, detail):
        if self.session:
            remaining = self.session["renew_at"] - time.time()
            if 0 < remaining < 48 * 3600:
                due = datetime.fromtimestamp(self.session["renew_at"], HONG_KONG)
                detail += f" 只读授权预计 {due:%m月%d日 %H:%M} 到期，请提前续期。"
        if (state, detail) != self.last_status:
            self.on_status(state, detail)
            self.last_status = (state, detail)

    async def access_token(self):
        session = self.vault.load()
        if not session:
            raise AuthorizationRequired("等待富途只读行情授权。")
        verify_scope(session.get("scope"))
        if session != self.session:
            self.session = session
            self.access = None
        if session["renew_at"] <= time.time():
            raise AuthorizationRequired("富途只读行情授权已到预计有效期，请重新授权。")
        if self.access and self.access_expires > time.monotonic() + 90:
            return self.access
        response = await asyncio.to_thread(post, "/oauth2/token", {
            "grant_type": "refresh_token",
            "client_id": session["client_id"],
            "refresh_token": session["refresh_token"],
        }, form=True)
        verify_scope(response.get("scope"))
        token = response.get("access_token")
        expires = number(response.get("expires_in"))
        if not isinstance(token, str) or not token or not expires:
            raise AuthorizationRequired("无法获得富途只读行情授权。")
        self.access = token
        self.access_expires = time.monotonic() + expires
        return token

    def accept(self, symbol, data, *, streamed):
        captured = datetime.now(timezone.utc)
        update = observation(symbol, data, captured, "stale")
        old = self.latest.get(symbol)
        if update is None or (old and update["quote_at"] < old["quote_at"]):
            return
        if streamed:
            previous = self.push_times.get(symbol)
            if not previous or previous < update["quote_at"]:
                self.newest_push_at[symbol] = time.monotonic()
            if previous and previous < update["quote_at"]:
                self.push_verified.add(symbol)
            self.push_times[symbol] = update["quote_at"]
        self.latest[symbol] = update

    def market(self):
        if time.monotonic() - self.last_market_state_at > 150:
            return "unknown"
        if self.market_state in OPEN_STATES:
            return "open"
        if self.market_state in CLOSED_STATES:
            return "closed"
        return "unknown"

    def quote_status(self, symbol, quote_at, captured):
        if self.market() == "closed":
            return "closed"
        if self.market() == "unknown":
            return "stale"
        age = (captured - datetime.fromisoformat(quote_at)).total_seconds()
        verified = symbol in self.push_verified and time.monotonic() - self.newest_push_at.get(symbol, 0) <= 65
        if verified and age <= 65:
            return "live"
        # An old snapshot can also mean a connection loss or inactive security.
        # Keep the stale label unless the transport demonstrates a moving delayed feed.
        if verified and 840 <= age <= 1020:
            return "delayed"
        return "stale"

    def handle_push(self, message, symbols):
        if not isinstance(message, dict) or not isinstance(message.get("data"), dict):
            return
        data = message["data"]
        if message.get("type") == "MARKET_STATE" and data.get("market") == "HK":
            state = data.get("status")
            if isinstance(state, (int, str)):
                self.market_state = state
                self.last_market_state_at = time.monotonic()
        if message.get("type") == "QUOTE" and message.get("symbol") in symbols:
            self.accept(message["symbol"], data, streamed=True)

    async def request_ws(self, connection, action, data, symbols):
        request_id = secrets.token_hex(8)
        await connection.send(json.dumps({"id": request_id, "action": action, **data}))
        async with asyncio.timeout(10):
            while True:
                frame = json.loads(await connection.recv())
                if not isinstance(frame, dict):
                    continue
                if frame.get("id") == request_id or (
                    action in {"auth", "refresh"} and frame.get("session_id") and not frame.get("type")
                ):
                    if frame.get("code", 0) not in (0, "0"):
                        return False
                    if action in {"auth", "refresh"}:
                        return bool(frame.get("session_id"))
                    return frame.get("code") in (0, "0")
                self.handle_push(frame, symbols)

    async def market_snapshot(self, symbols, token):
        states = await asyncio.to_thread(
            post, "/api/v1.0/quote/market-state", {"code_list": ["HK.00700"]}, token=token,
        )
        state_rows = result_rows(states, "market_state_list")
        if state_rows and isinstance(state_rows[0].get("market_state"), (str, int)):
            self.market_state = state_rows[0]["market_state"]
            self.last_market_state_at = time.monotonic()

        snapshots = await asyncio.to_thread(
            post, "/api/v1.0/quote/snapshot", {"code_list": symbols}, token=token,
        )
        for obj in result_rows(snapshots, "snapshot_list"):
            if obj.get("code") in symbols:
                self.accept(obj["code"], obj, streamed=False)

    def publish(self, symbols, subscription_count):
        now = datetime.now(timezone.utc)
        statuses = set()
        for symbol in symbols:
            latest = self.latest.get(symbol)
            if not latest:
                continue
            state = self.quote_status(symbol, latest["quote_at"], now)
            point = dict(latest, status=state)
            if point != self.published.get(symbol):
                self.on_observation(point)
                self.published[symbol] = point
            statuses.add(state)
        if self.market() == "closed":
            self.status("closed", f"港股当前休市或午休。已订阅 {subscription_count}/{len(symbols)} 个标的。")
        elif "live" in statuses:
            self.status("live", f"已验证港股实时推送。已订阅 {subscription_count}/{len(symbols)} 个标的。")
        elif "delayed" in statuses:
            self.status("delayed", "富途行情持续更新，但报价时间滞后约 15 分钟。")
        else:
            self.status("stale", "等待有效的港股实时推送。已有记录保留并显示采集时间。")

    async def stream(self, symbols, token):
        self.push_times.clear()
        self.push_verified.clear()
        self.newest_push_at.clear()
        async with connect(QUOTE_SOCKET, open_timeout=10, ping_interval=20, ping_timeout=15, max_size=1_000_000) as socket:
            if not await self.request_ws(socket, "auth", {"data": {
                "auth_type": "oauth2", "authorization": f"Bearer {token}",
            }}, symbols):
                self.access = None
                raise FutuError("富途行情推送鉴权未通过。")
            if await self.request_ws(socket, "subscribe", {"quote": symbols}, symbols):
                count = len(symbols)
            else:
                count = 0
                for symbol in symbols:
                    count += await self.request_ws(socket, "subscribe", {"quote": [symbol]}, symbols)
            next_refresh = time.monotonic() + 240
            next_snapshot = next_publish = 0
            while True:
                now = time.monotonic()
                if now >= next_publish:
                    configured = self.symbols()
                    if symbols != configured:
                        active = set(configured)
                        self.latest = {symbol: point for symbol, point in self.latest.items() if symbol in active}
                        self.published = {symbol: point for symbol, point in self.published.items() if symbol in active}
                        return
                    self.publish(symbols, count)
                    next_publish = now + 8
                if now >= next_snapshot:
                    try:
                        await self.market_snapshot(symbols, token)
                    except FutuError:
                        # A REST outage should not stop a healthy WebSocket.
                        pass
                    next_snapshot = now + 50
                if now >= next_refresh or now >= self.access_expires - 90:
                    token = await self.access_token()
                    if not await self.request_ws(socket, "refresh", {"data": {
                        "authorization": f"Bearer {token}",
                    }}, symbols):
                        self.access = None
                        raise FutuError("富途行情推送会话续期失败。")
                    next_refresh = now + 240
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=2)
                except TimeoutError:
                    continue
                try:
                    self.handle_push(json.loads(raw), symbols)
                except (ValueError, UnicodeDecodeError):
                    continue

    async def run(self):
        retry = 3
        while True:
            connection_started = time.monotonic()
            try:
                symbols = self.symbols()
                if not symbols:
                    self.status("unavailable", "请先添加港股自选标的。")
                    await asyncio.sleep(15)
                    continue
                token = await self.access_token()
                await self.stream(symbols, token)
            except AuthorizationRequired as error:
                self.access = None
                self.status("pending_auth", str(error))
                await asyncio.sleep(20)
            except (FutuError, WebSocketException, OSError, TimeoutError, ValueError, KeyError) as error:
                self.push_times.clear()
                self.push_verified.clear()
                detail = str(error) if isinstance(error, FutuError) else "富途行情连接中断，正在重连。"
                self.status("unavailable", detail)
                if time.monotonic() - connection_started > 90:
                    retry = 3
                await asyncio.sleep(retry)
                retry = min(retry * 2, 120)
            else:
                retry = 3


def main():
    parser = argparse.ArgumentParser(description="富途 OpenAPI 只读行情授权")
    parser.add_argument("command", choices=["authorize"])
    parser.add_argument("--data-dir", default=os.getenv("HK_DATA_DIR", "data"))
    parser.add_argument("--port", type=int, default=60355)
    parser.add_argument("--paste", action="store_true", help="用私人终端粘贴回调地址，适合无端口转发的服务器")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("授权回调端口须在 1024 至 65535 之间")
    try:
        authorize(args.data_dir, args.port, args.paste)
    except (FutuError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, FutuError) else "授权配置不可写或回调端口无法使用。"
        parser.exit(1, f"{message}\n")


if __name__ == "__main__":
    main()
