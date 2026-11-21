#!/usr/bin/env python3
"""本机按需启动与停止：进程脱离终端，数据和日志保留在本机。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from backup import create_daily_backup

PROJECT = Path(__file__).resolve().parents[1]


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "uid=", "-o", "lstart=", "-o", "command="],
                            capture_output=True, text=True, timeout=4)
    parts = result.stdout.strip().split(None, 6)
    if result.returncode or len(parts) != 7:
        return None
    try:
        return {"uid": int(parts[0]), "started": " ".join(parts[1:6]), "command": parts[6]}
    except ValueError:
        return None


class LocalRuntime:
    def __init__(self, data_dir, port=8738):
        self.data = Path(data_dir).expanduser().resolve()
        self.port = int(port)
        if not 1024 <= self.port <= 65535:
            raise ValueError("本机端口须在 1024 至 65535 之间。")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs = self.data / "local-logs"
        self.logs.mkdir(mode=0o700, exist_ok=True)
        self.state_path = self.data / "local-runtime.json"
        self.instance = hashlib.sha256(str(self.data).encode()).hexdigest()[:16]

    @contextmanager
    def lock(self):
        with (self.data / ".local-runtime.lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield

    def load(self):
        try:
            state = json.loads(self.state_path.read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, state):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def owned(self, record, role):
        if not isinstance(record, dict) or record.get("role") != role or record.get("data_dir") != str(self.data):
            return False
        identity = process_identity(record.get("pid"))
        if not identity or identity["uid"] != os.getuid() or identity["started"] != record.get("started"):
            return False
        command = identity["command"]
        return (str(PROJECT / "workbench.py") in command and "--data-dir " + str(self.data) in command
                and re.search(r"\s" + role + r"(?:\s|$)", command) is not None)

    def health(self):
        try:
            with urlopen(f"http://127.0.0.1:{self.port}/api/local-health", timeout=2) as response:
                value = json.load(response)
        except HTTPError:
            return {"web": False, "occupied": True, "message": "该端口不是当前本机版工作台，请先停止旧版本或占用该端口的程序。"}
        except (OSError, URLError, ValueError):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                    occupied = True
            except OSError:
                occupied = False
            return {"web": False, "occupied": occupied, "message": "网页尚未就绪。"}
        matches = (value.get("app", value.get("application")) == "hk-workbench"
                   and value.get("instance_id") == self.instance and value.get("mode") == "local")
        if not matches:
            return {"web": False, "occupied": True, "message": "该端口对应其他工作台或数据目录，未修改该进程。"}
        return value | {"web": True, "occupied": True}

    def worker_locked(self):
        with (self.data / ".worker.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(file, fcntl.LOCK_UN)
            return False

    def launch(self, role):
        log_path = self.logs / ("web.log" if role == "serve" else "worker.log")
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            log_path.replace(log_path.with_suffix(".previous.log"))
        command = [sys.executable, str(PROJECT / "workbench.py"), "--data-dir", str(self.data), role]
        if role == "serve":
            command += ["--port", str(self.port)]
        env = os.environ.copy()
        env["HK_RUN_MODE"] = "local"
        env["HK_HEALTHCHECK_WEB_PORT"] = str(self.port)
        with log_path.open("ab", buffering=0) as output, open(os.devnull, "rb") as null:
            child = subprocess.Popen(command, cwd=PROJECT, env=env, stdin=null, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        # start_new_session makes closing the launcher Terminal independent of the worker.
        identity = process_identity(child.pid)
        if not identity:
            raise RuntimeError(f"{role} 启动失败，请查看 {log_path}。")
        return {"pid": child.pid, "started": identity["started"], "role": role,
                "data_dir": str(self.data), "port": self.port}

    def stop_owned(self, record, role):
        if not self.owned(record, role):
            return False
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            return False
        for _ in range(40):
            if not self.owned(record, role):
                return True
            time.sleep(.1)
        # Do not force-kill a process whose identity has changed.
        if self.owned(record, role):
            raise RuntimeError("服务尚未退出，请稍后再次停止。未强制结束其他程序。")
        return True

    def backup_daily(self):
        try:
            create_daily_backup(self.data)
        except (OSError, ValueError, sqlite3.Error):
            return "本次自动备份未成功，请到 data/local-logs 查看日志并保留当前数据目录。"
        return None

    def start(self):
        with self.lock():
            state = self.load()
            health = self.health()
            if not health["web"]:
                if health["occupied"]:
                    raise RuntimeError(health["message"])
                if self.owned(state.get("web"), "serve"):
                    self.stop_owned(state["web"], "serve")
                state["web"] = self.launch("serve")
                self.save(state)
            deadline = time.monotonic() + 12
            while not self.health()["web"]:
                if time.monotonic() > deadline:
                    raise RuntimeError("网页未能启动，请查看本机日志 web.log。")
                time.sleep(.2)
            if not self.worker_locked():
                state["worker"] = self.launch("worker")
                self.save(state)
            deadline = time.monotonic() + 12
            health = self.health()
            while not health.get("worker_fresh") and time.monotonic() < deadline:
                if not self.worker_locked() and not self.owned(state.get("worker"), "worker"):
                    raise RuntimeError("采集器未能启动，请查看本机日志 worker.log。")
                time.sleep(.2)
                health = self.health()
            if not health.get("worker_fresh"):
                raise RuntimeError("网页已打开，但采集器尚无有效心跳；请在运行状态查看来源和本机日志。")
            backup_warning = self.backup_daily()
            if backup_warning:
                with (self.logs / "runtime.log").open("a") as log:
                    log.write(datetime.now(timezone.utc).isoformat() + " 自动备份执行失败。\n")
            state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.save(state)
            return {"status": "running", "url": f"http://127.0.0.1:{self.port}", "web": True,
                    "worker": True, "backup_warning": backup_warning,
                    "message": "本地工作台已就绪。可以关闭启动终端；睡眠或关机时采集会暂停。" + ("\n" + backup_warning if backup_warning else "")}

    def stop(self):
        with self.lock():
            state = self.load()
            stopped = []
            for key, role in (("worker", "worker"), ("web", "serve")):
                if self.stop_owned(state.get(key), role):
                    stopped.append(key)
                state.pop(key, None)
            self.save(state)
            health = self.health()
            foreign_running = bool(health.get("web") or self.worker_locked())
            return {"status": "other_session" if foreign_running else "stopped", "stopped": stopped,
                    "message": "本次启动的服务已停止，数据保留。另一个会话启动的服务未被结束。" if foreign_running else "本地工作台已停止，已有数据保留。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=PROJECT / "data")
    parser.add_argument("--port", type=int, default=8738)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", choices=("start", "stop", "status"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        runtime = LocalRuntime(args.data_dir, args.port)
        if args.command == "status":
            result = runtime.health()
        else:
            result = getattr(runtime, args.command)()
        if args.command == "start" and not args.no_open:
            subprocess.run(["open", result["url"]], check=True)
        print(json.dumps(result, ensure_ascii=False) if args.json else result.get("message", json.dumps(result, ensure_ascii=False)))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("暂未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
