import json
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from notifications import Notifications
from storage import Store
from workbench import Handler


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.store = self.store
        self.server.notifications = Notifications(self.store)
        self.server.allowed_hosts = {"127.0.0.1"}
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, method="GET", headers=None):
        request = Request(self.base + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json"} | (headers or {}))
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_v2_routes_persist_full_workflow_and_protect_put(self):
        status, _ = self.request("/api/budget", {"amount": 20000}, "PUT", {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        for path, data in (("/api/budget", {"amount": 20000}),
                           ("/api/notifications", {"daily_digest": True, "digest_hour": 19}),
                           ("/api/profiles/welab", {"status": "active", "opened_on": "2026-01-01"})):
            status, body = self.request(path, data, "PUT")
            self.assertEqual(status, 200, body)
        status, offer = self.request("/api/offers", {"platform": "welab", "title": "隔离验收活动", "text": "测试数据，不是真实个人活动", "url": "https://www.welab.bank/zh/test"}, "POST")
        self.assertEqual(status, 201, offer)
        offer_id = offer["id"]
        path = f"/api/offers/{offer_id}"
        status, task = self.request(path + "/tasks", {"stage": "registration", "title": "登记", "completed": True, "evidence": "隔离测试记录"}, "POST")
        self.assertEqual(status, 201, task)
        status, _ = self.request(path + "/retrospective", {"actual_minutes": 20, "problems": "私有记录不会公开", "shareable_notes": "核对官方渠道后完成登记。", "shareable_approved": True}, "PUT")
        self.assertEqual(status, 200)
        status, tutorial = self.request(path + "/tutorial?kind=public")
        self.assertEqual(status, 200)
        self.assertNotIn("私有记录不会公开", tutorial["text"])
        self.assertIn("核对官方渠道", tutorial["text"])
        status, _ = self.request("/api/reservations", {"offer_id": offer_id, "amount": 10000, "starts_on": "2026-11-17", "ends_on": "2026-12-01"}, "POST")
        self.assertEqual(status, 201)
        for endpoint in ("dashboard", "profiles", "budget", "tasks", "reservations", "alerts", "notifications", "ledger", "ipos"):
            status, body = self.request("/api/" + endpoint)
            self.assertEqual(status, 200, (endpoint, body))
        for resource in ("requirements", "tasks", "retrospective"):
            self.assertEqual(self.request(path + "/" + resource)[0], 200)


if __name__ == "__main__":
    unittest.main()
