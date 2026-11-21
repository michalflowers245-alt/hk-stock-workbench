from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest

from local_runtime import LocalRuntime, PROJECT


class LocalLauncherTests(unittest.TestCase):
    def test_detached_start_reuses_web_repairs_worker_and_stops(self):
        with TemporaryDirectory(prefix="hk-local-launcher-") as directory:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            runtime = LocalRuntime(directory, port)
            command = [sys.executable, str(PROJECT / "ops/local_runtime.py"), "--data-dir", directory,
                       "--port", str(port), "--no-open", "--json", "start"]
            try:
                first = subprocess.run(command, capture_output=True, text=True, timeout=25)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertTrue(runtime.health()["web"])
                self.assertTrue(runtime.health()["worker_fresh"])
                saved = runtime.load()
                # The launcher process has exited, while detached services stay available.
                self.assertTrue(runtime.owned(saved["web"], "serve"))
                self.assertTrue(runtime.owned(saved["worker"], "worker"))
                again = subprocess.run(command, capture_output=True, text=True, timeout=25)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertEqual(runtime.load()["web"]["pid"], saved["web"]["pid"])
                self.assertEqual(runtime.load()["worker"]["pid"], saved["worker"]["pid"])
                runtime.stop_owned(saved["worker"], "worker")
                restarted = subprocess.run(command, capture_output=True, text=True, timeout=25)
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                self.assertNotEqual(runtime.load()["worker"]["pid"], saved["worker"]["pid"])
                self.assertEqual(runtime.load()["web"]["pid"], saved["web"]["pid"])
                self.assertEqual(len(list((Path(directory) / "backups").glob("hk-workbench-*.sqlite3"))), 1)
            finally:
                result = runtime.stop()
            self.assertEqual(result["status"], "stopped")
            self.assertFalse(runtime.health()["web"])
            self.assertTrue((Path(directory) / "workbench.sqlite3").is_file())

    def test_other_service_on_port_is_preserved(self):
        class OtherHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b'{"app":"another-app","web":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), OtherHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory(prefix="hk-local-conflict-") as directory:
                runtime = LocalRuntime(directory, server.server_port)
                with self.assertRaisesRegex(RuntimeError, "其他工作台"):
                    runtime.start()
                self.assertTrue(runtime.health()["occupied"])
                self.assertEqual(runtime.stop()["status"], "stopped")
                self.assertTrue(runtime.health()["occupied"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
