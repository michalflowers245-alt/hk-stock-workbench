import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from websockets.asyncio.server import serve

from futu_quotes import (
    AuthorizationRequired,
    FutuQuotes,
    TokenVault,
    callback_code,
    observation,
    verify_scope,
)


def push(when, price=401):
    return {
        "type": "QUOTE",
        "symbol": "HK.00700",
        "data": {
            "data_time_ms": int(when.timestamp() * 1000),
            "last_price": price,
            "prev_close_price": 400,
        },
    }


class QuoteTests(unittest.TestCase):
    def test_vault_encrypts_token_and_enforces_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = TokenVault(directory)
            session = {
                "client_id": "test-client",
                "redirect_uri": "http://localhost:60355/callback",
                "scope": "quote:read",
                "refresh_token": "fixture_token",
                "renew_at": time.time() + 3600,
            }
            self.assertIsNone(vault.load())
            vault.save(session)
            self.assertEqual(vault.load(), session)
            self.assertNotIn(b"fixture_token", vault.session.read_bytes())
            self.assertEqual(vault.session.stat().st_mode & 0o777, 0o600)
            vault.session.chmod(0o644)
            with self.assertRaises(AuthorizationRequired):
                vault.load()

    def test_scope_and_callback_are_strict(self):
        verify_scope("quote:read")
        for scope in (None, "", "quote:write", "quote:read trade:read", "quote:read trade:write"):
            with self.assertRaises(AuthorizationRequired):
                verify_scope(scope)
        address = "http://localhost:60355/callback"
        self.assertEqual(callback_code(address + "?code=example&state=valid", address, "valid"), "example")
        for url in (
            address + "?code=example&state=other",
            address + "?code=example&code=second&state=valid",
            "http://localhost:60356/callback?code=example&state=valid",
        ):
            with self.assertRaises(AuthorizationRequired):
                callback_code(url, address, "valid")

    def test_quote_preserves_exchange_and_capture_times(self):
        captured = datetime.now(timezone.utc)
        exchange = captured - timedelta(seconds=5)
        point = observation("HK.00700", push(exchange)["data"], captured, "live")
        self.assertEqual(point["last_price"], 401)
        self.assertIsNone(point["volume"])
        self.assertLess(point["quote_at"], point["captured_at"])
        self.assertIsNone(observation("HK.00700", push(captured + timedelta(seconds=60))["data"], captured, "live"))

    def test_live_requires_advancing_pushes_and_recent_market_state(self):
        points, statuses = [], []
        symbols = ["HK.00700"]
        worker = FutuQuotes(Path("/tmp"), lambda: symbols, points.append, lambda *args: statuses.append(args))
        now = datetime.now(timezone.utc)
        worker.handle_push({"type": "MARKET_STATE", "data": {"market": "HK", "status": 3}}, symbols)
        worker.handle_push(push(now - timedelta(seconds=2)), symbols)
        worker.publish(symbols, 1)
        self.assertEqual(points[-1]["status"], "stale")
        worker.handle_push(push(now - timedelta(seconds=1)), symbols)
        worker.publish(symbols, 1)
        self.assertEqual(points[-1]["status"], "live")
        self.assertEqual(statuses[-1][0], "live")
        worker.newest_push_at["HK.00700"] = time.monotonic() - 120
        worker.publish(symbols, 1)
        self.assertEqual(points[-1]["status"], "stale")
        worker.handle_push({"type": "MARKET_STATE", "data": {"market": "HK", "status": 4}}, symbols)
        worker.publish(symbols, 1)
        self.assertEqual(points[-1]["status"], "closed")


class QuoteTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_consumes_real_websocket_frames(self):
        observed, statuses = [], []
        started = asyncio.Event()
        ended = asyncio.Event()
        symbols = ["HK.00700"]

        async def source(socket):
            auth = json.loads(await socket.recv())
            self.assertEqual(auth["action"], "auth")
            self.assertEqual(auth["data"]["authorization"], "Bearer fixture")
            await socket.send(json.dumps({"id": auth["id"], "session_id": "fixture", "server_time": 0}))
            subscribe = json.loads(await socket.recv())
            self.assertEqual(subscribe["quote"], symbols)
            await socket.send(json.dumps({"type": "MARKET_STATE", "data": {"market": "HK", "status": 3}}))
            now = datetime.now(timezone.utc)
            await socket.send(json.dumps(push(now - timedelta(seconds=2), price=401)))
            await socket.send(json.dumps(push(now - timedelta(seconds=1), price=402)))
            await socket.send(json.dumps({"id": subscribe["id"], "code": 0}))
            await ended.wait()

        def capture_state(*args):
            statuses.append(args)
            if args[0] == "live":
                started.set()

        worker = FutuQuotes(Path("/tmp"), symbols, observed.append, capture_state)
        worker.access_expires = time.monotonic() + 600
        async with serve(source, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with patch("futu_quotes.QUOTE_SOCKET", f"ws://127.0.0.1:{port}"), patch.object(worker, "market_snapshot"):
                task = asyncio.create_task(worker.stream(symbols, "fixture"))
                try:
                    await asyncio.wait_for(started.wait(), timeout=3)
                    self.assertEqual(observed[-1]["last_price"], 402)
                    self.assertEqual(observed[-1]["status"], "live")
                    self.assertEqual(statuses[-1][0], "live")
                finally:
                    ended.set()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_worker_waits_without_credentials(self):
        ready = asyncio.Event()
        statuses = []

        def capture(*args):
            statuses.append(args)
            ready.set()

        with tempfile.TemporaryDirectory() as directory:
            worker = FutuQuotes(directory, ["HK.00700"], lambda point: self.fail(str(point)), capture)
            task = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(ready.wait(), timeout=1)
                self.assertEqual(statuses[0][0], "pending_auth")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_documented_subscribe_ack_ignores_interleaved_frames(self):
        symbols = ["HK.00700"]

        class Socket:
            async def send(self, message):
                request = json.loads(message)
                self.request = request
                self.frames = iter([
                    push(datetime.now(timezone.utc)),
                    {"id": request["id"], "code": 0, "message": ""},
                ])

            async def recv(self):
                return json.dumps(next(self.frames))

        worker = FutuQuotes(Path("/tmp"), symbols, lambda _: None, lambda *_: None)
        socket = Socket()
        self.assertTrue(await worker.request_ws(socket, "subscribe", {"quote": symbols}, symbols))
        self.assertEqual(socket.request["quote"], symbols)
        self.assertIn("HK.00700", worker.latest)

    async def test_token_refresh_does_not_grant_account_access(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = FutuQuotes(directory, ["HK.00700"], lambda _: None, lambda *_: None)
            worker.vault.save({
                "client_id": "test-client",
                "redirect_uri": "http://localhost:60355/callback",
                "scope": "quote:read",
                "refresh_token": "fixture_token",
                "renew_at": time.time() + 3600,
            })
            with patch("futu_quotes.post", return_value={"scope": "quote:read trade:read"}):
                with self.assertRaises(AuthorizationRequired):
                    await worker.access_token()


if __name__ == "__main__":
    unittest.main()
