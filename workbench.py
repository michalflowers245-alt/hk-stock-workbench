"""Private Hong Kong stock workbench HTTP server and operator commands."""
import argparse
import asyncio
import csv
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, unquote

from account_import import AccountData
from collectors import PLATFORMS
from services import check_all_platforms, check_ipos, extract_with_ai, generate_tutorial, load_env, mail_settings, run_worker, local_mode, runtime_info
from storage import QUOTE_FIELDS, Store, dump
from notifications import Notifications

STATIC = Path(__file__).resolve().parent / "static"
LOG = logging.getLogger("workbench.http")
CSP = "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            return
        LOG.debug("%s", fmt % args)

    @property
    def store(self):
        return self.server.store

    def allowed(self, mutate):
        host = self.headers.get("Host", "")
        try:
            address = urlsplit("//" + host)
            hostname = address.hostname
            if (not hostname or address.username or address.password or address.path or address.query
                    or address.fragment or hostname.lower() not in self.server.allowed_hosts):
                return False
            if not mutate:
                return True
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower():
                    return False
            return True
        except ValueError:
            return False

    def respond(self, status, content, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, data, status=200):
        self.respond(status, dump(data).encode(), "application/json; charset=utf-8")

    def fail(self, status, message):
        self.send_json({"error": message}, status)

    def read_body(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 JSON。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求正文不是有效的 JSON 对象，或超过大小限制。") from None

    def route(self, method):
        mutate = method in ("POST", "PUT", "PATCH", "DELETE")
        if not self.allowed(mutate):
            return self.fail(403, "此工作台仅允许本机或已配置的私人地址访问。")
        request = urlsplit(self.path)
        path = request.path.rstrip("/") or "/"
        if not path.startswith("/api/"):
            return self.serve_static(path) if method == "GET" else self.fail(404, "此操作不存在。")
        if path.startswith("/api/account-data/"):
            hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
            if not local_mode() or hostname not in {"127.0.0.1", "localhost"}:
                return self.fail(403, "本人账户资料只在本机工作台打开。")
        params = parse_qs(request.query)
        try:
            if method == "GET":
                return self.read_api(path, params)
            data = self.read_body() if method in ("POST", "PUT", "PATCH") else {}
            return self.write_api(path, params, data, method)
        except ValueError as exc:
            return self.fail(400, str(exc))
        except Exception:
            LOG.exception("工作台请求处理失败")
            return self.fail(500, "服务暂时无法处理此请求。")

    def serve_static(self, path):
        files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css",
                 "/app.js": "app.js", "/static/style.css": "style.css", "/static/app.js": "app.js"}
        if path not in files:
            return self.fail(404, "页面不存在。")
        filename = STATIC / files[path]
        if not filename.is_file():
            return self.fail(503, "前端文件暂不可用。")
        content_type = mimetypes.guess_type(filename)[0] or "text/plain"
        self.respond(200, filename.read_bytes(), content_type + "; charset=utf-8")

    def read_api(self, path, params):
        match = re.fullmatch(r"/api/account-data/(za|welab|eddid)", path)
        if match:
            return self.send_json(AccountData(self.store).list_data(match[1]))
        match = re.fullmatch(r"/api/account-data/template/(balances|positions|transactions)", path)
        if match:
            csv_data = AccountData(self.store).template(match[1])
            return self.respond(200, csv_data.encode("utf-8"), "text/csv; charset=utf-8",
                                {"Content-Disposition": f'attachment; filename="hk-{match[1]}.csv"'})
        if path == "/api/local-health":
            with self.store.connect() as db:
                row = db.execute("SELECT status,updated_at FROM service_state WHERE name='worker'").fetchone()
            fresh = bool(row and row["status"] == "running" and 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds() <= 90)
            root = str(self.store.root.resolve())
            return self.send_json({"app": "hk-workbench", "application": "hk-workbench", "web": True,
                                   "pid": os.getpid(), "data_dir": root, "mode": runtime_info()["mode"],
                                   "worker_fresh": fresh, "worker_updated_at": row["updated_at"] if row else None,
                                   "instance_id": sha256(root.encode()).hexdigest()[:16]})
        if path == "/api/notices":
            return self.send_json(self.server.notifications.inbox())
        if path == "/api/profiles":
            return self.send_json(self.store.profiles())
        if path == "/api/tasks":
            return self.send_json(self.store.tasks())
        if path == "/api/budget":
            return self.send_json(self.store.budget())
        if path == "/api/reservations":
            return self.send_json(self.store.reservations())
        if path == "/api/alerts":
            return self.send_json(self.server.notifications.rules())
        if path == "/api/notifications":
            return self.send_json(self.server.notifications.settings())
        match = re.fullmatch(r"/api/offers/(\d+)/(requirements|tasks|retrospective)", path)
        if match:
            return self.send_json(getattr(self.store, match[2])(int(match[1])))
        if path == "/api/dashboard":
            dashboard = self.store.dashboard(PLATFORMS)
            dashboard["runtime"] = runtime_info()
            dashboard["notices"] = self.server.notifications.inbox()
            return self.send_json(dashboard)
        if path == "/api/ipos":
            return self.send_json(self.store.ipos())
        if path == "/api/offers":
            return self.send_json(self.store.offers(PLATFORMS))
        if path == "/api/ledger":
            return self.send_json(self.store.ledger())
        if path == "/api/watchlist":
            return self.send_json(self.store.watchlist())
        if path in ("/api/quotes/history", "/api/quotes/export.csv"):
            try:
                days = int(params.get("days", ["1"])[0])
            except ValueError:
                raise ValueError("查询范围应为 1 至 90 天。") from None
            if not 1 <= days <= 90:
                raise ValueError("查询范围应为 1 至 90 天。")
            symbol = params.get("symbol", [None])[0]
            if not symbol and days > 7:
                raise ValueError("超过 7 天时请指定一个港股标的。")
            rows = self.store.history(symbol, days)
            if path.endswith(".csv"):
                return self.export_quotes(rows)
            return self.send_json(rows)
        if path == "/api/health":
            return self.send_json({"status": "ok"})
        match = re.fullmatch(r"/api/offers/(\d+)(/tutorial)?", path)
        if match:
            item = self.store.offers(PLATFORMS, offer_id=int(match[1]), detail=not match[2])
            if not item:
                return self.fail(404, "活动不存在。")
            if match[2]:
                return self.send_json(generate_tutorial(self.store, item, params.get("kind", ["personal"])[0]))
            return self.send_json(item)
        return self.fail(404, "此操作不存在。")

    def write_api(self, path, params, data, method):
        match = re.fullmatch(r"/api/account-data/(za|welab|eddid)/(balances|positions|transactions)/(\d{1,18})", path)
        if method == "DELETE" and match:
            platform, kind, record_id = match.groups()
            removed = AccountData(self.store).remove_record(platform, kind, int(record_id))
            if not removed:
                return self.fail(404, "未找到这条账户记录。")
            return self.send_json({"removed": True})
        match = re.fullmatch(r"/api/account-data/(za|welab|eddid)/(balances|positions|transactions)/(preview|import)", path)
        if method == "POST" and match:
            platform, kind, action = match.groups()
            content = data.get("csv")
            if not isinstance(content, str):
                raise ValueError("请选择 CSV 文件或粘贴 CSV 内容。")
            account_data = AccountData(self.store)
            digest = sha256(content.encode("utf-8")).hexdigest()
            if action == "preview":
                return self.send_json(account_data.preview(platform, kind, content) | {"digest": digest})
            if data.get("digest") != digest:
                raise ValueError("资料内容已变动，请重新预览后导入。")
            return self.send_json(account_data.import_csv(platform, kind, content), 201)
        if method == "PUT":
            if path == "/api/notifications":
                return self.send_json(self.server.notifications.save_settings(data))
            if path == "/api/budget":
                return self.send_json(self.store.save_budget(data))
            match = re.fullmatch(r"/api/profiles/([a-z]+)", path)
            if match:
                return self.send_json(self.store.save_profile(match[1], data))
            match = re.fullmatch(r"/api/offers/(\d+)/(requirements|retrospective)", path)
            if match:
                return self.send_json(getattr(self.store, "save_" + match[2])(int(match[1]), data))
        if method == "POST":
            if path == "/api/notices/read-all":
                return self.send_json({"updated": self.server.notifications.read_notice()})
            match = re.fullmatch(r"/api/notices/(\d+)/read", path)
            if match:
                return self.send_json({"updated": self.server.notifications.read_notice(int(match[1]))})
            if path == "/api/alerts":
                return self.send_json(self.server.notifications.save_rule(data), 201)
            if path == "/api/reservations":
                return self.send_json(self.store.save_reservation(data), 201)
            match = re.fullmatch(r"/api/offers/(\d+)/tasks", path)
            if match:
                return self.send_json(self.store.save_task(int(match[1]), data), 201)
            match = re.fullmatch(r"/api/ipos/(.+)/cutoffs", path)
            if match:
                return self.send_json(self.store.save_ipo_cutoff(unquote(match[1]), data), 201)
            if path == "/api/watchlist":
                return self.send_json(self.store.set_symbol(data.get("symbol"), data.get("name")), 201)
            if path == "/api/offers":
                offer_id, _ = self.store.save_offer(data, manual=True)
                return self.send_json(self.store.offers(PLATFORMS, offer_id=offer_id, detail=True), 201)
            if path == "/api/ledger":
                return self.send_json({"id": self.store.add_entry(data)}, 201)
            if path == "/api/refresh":
                scope = data.get("scope")
                if scope not in ("all", "offers", "ipos", "quotes"):
                    raise ValueError("请选择有效的刷新范围。")
                self.store.request_refresh(("offers", "ipos", "quotes") if scope == "all" else (scope,))
                return self.send_json({"queued": True}, 202)
            if path == "/api/email/test":
                if local_mode():
                    return self.fail(409, "本地版使用工作台内提醒，无需邮箱。")
                if not mail_settings():
                    return self.fail(409, "请先设置发件邮箱和唯一收件地址。")
                self.store.queue_test_email()
                return self.send_json({"queued": True}, 202)
            match = re.fullmatch(r"/api/offers/(\d+)/tutorial", path)
            if match:
                kind = data.get("kind", params.get("kind", ["personal"])[0])
                if kind not in ("personal", "public"):
                    raise ValueError("请选择本人实操或分享草稿。")
                offer = self.store.offers(PLATFORMS, offer_id=int(match[1]))
                if not offer:
                    return self.fail(404, "活动不存在。")
                if data.get("ai") is True and not local_mode():
                    extract_with_ai(self.store, offer)
                return self.send_json(generate_tutorial(self.store, offer, kind))
        if method == "PATCH":
            match = re.fullmatch(r"/api/ledger/(\d+)/voucher", path)
            if match:
                return self.send_json(self.store.redeem_voucher(int(match[1]), data))
            match = re.fullmatch(r"/api/offers/(\d+)", path)
            if match:
                offer_id = int(match[1])
                if self.store.patch_offer(offer_id, data):
                    return self.send_json(self.store.offers(PLATFORMS, offer_id=offer_id, detail=True))
                return self.fail(404, "活动不存在。")
            match = re.fullmatch(r"/api/ledger/(\d+)", path)
            if match:
                if self.store.settle_entry(int(match[1]), data):
                    return self.send_json({"updated": True})
                return self.fail(404, "收益记录不存在。")
        if method == "DELETE":
            match = re.fullmatch(r"/api/alerts/(\d+)", path)
            if match:
                return self.send_json({"removed": self.server.notifications.delete_rule(int(match[1]))})
            match = re.fullmatch(r"/api/watchlist/([^/]+)", path)
            if match:
                return self.send_json({"removed": self.store.remove_symbol(match[1])})
            match = re.fullmatch(r"/api/ledger/(\d+)", path)
            if match:
                return self.send_json({"removed": self.store.delete_entry(int(match[1]))})
        return self.fail(404, "此操作不存在。")

    def export_quotes(self, rows):
        target = io.StringIO()
        fields = ("symbol", "name", *QUOTE_FIELDS, "change_percent", "quote_at", "captured_at", "source", "status")
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in rows:
            name = item.get("name") or ""
            if name.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                item["name"] = "'" + name
            writer.writerow(item)
        self.respond(200, target.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                     {"Content-Disposition": 'attachment; filename="hk-quotes.csv"'})

    def do_GET(self):
        self.route("GET")

    def do_HEAD(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")


def serve(store, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.store = store
    server.notifications = Notifications(store)
    hosts = os.getenv("HK_ALLOWED_HOSTS", "").lower().split(",")
    server.allowed_hosts = {"localhost", "127.0.0.1"} | {host.strip() for host in hosts if host.strip()}
    print(f"港股工作台已启动：http://127.0.0.1:{server.server_port}", flush=True)
    with server:
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.getenv("HK_ENV_FILE", ".env"))
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="command", required=True)
    app = sub.add_parser("serve")
    app.add_argument("--port", type=int, default=8738)
    sub.add_parser("worker")
    refresh = sub.add_parser("check")
    refresh.add_argument("scope", choices=("offers", "ipos", "all"), default="all", nargs="?")
    backup = sub.add_parser("backup")
    backup.add_argument("output")
    args = parser.parse_args()
    load_env(args.env)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.data_dir or os.getenv("HK_DATA_DIR", "data"))
    try:
        if args.command == "serve":
            serve(store, args.port)
        elif args.command == "worker":
            asyncio.run(run_worker(store))
        elif args.command == "backup":
            store.backup(args.output)
        elif args.command == "check":
            if args.scope != "ipos":
                check_all_platforms(store)
            if args.scope != "offers":
                check_ipos(store)
            summary = store.dashboard(PLATFORMS)
            print(json.dumps({"来源": summary["sources"], "数量": summary["counts"]}, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
