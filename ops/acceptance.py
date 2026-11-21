#!/usr/bin/env python3
"""记录真实云端观测并生成验收报告；人工证据缺失时不会宣称全部通过。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import urllib.request

UTC = timezone.utc
INDICES = ("HK.800000", "HK.800700", "HK.800100")
PLATFORMS = ("za", "welab", "eddid", "fafa", "phillip")


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须含时区。")
    return parsed.astimezone(UTC)


def now():
    return datetime.now(UTC)


@contextmanager
def db_open(data_dir):
    db = sqlite3.connect((data_dir.resolve() / "workbench.sqlite3").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def locate(data_dir):
    root = data_dir / "acceptance"
    active = root / "current.json"
    if not active.exists():
        return None, None
    record = json.loads(active.read_text(encoding="utf-8"))
    run_id = record.get("run_id", "")
    if not re.fullmatch(r"\d{8}T\d{12}Z", run_id):
        raise ValueError("验收记录编号无效。")
    return root / run_id, record


def start(data_dir):
    root = data_dir / "acceptance"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root / "current.json").exists():
        raise ValueError("已有正在观测的验收记录；请先运行 stop，历史记录会保留。")
    when = now()
    run_id = when.strftime("%Y%m%dT%H%M%S%fZ")
    run = root / run_id
    run.mkdir(mode=0o700)
    record = {"run_id": run_id, "started_at": when.isoformat(), "system": platform.system(),
              "host_hash": hashlib.sha256(platform.node().encode()).hexdigest()[:16]}
    body = json.dumps(record, ensure_ascii=False, indent=2)
    (run / "run.json").write_text(body, encoding="utf-8")
    (root / "current.json").write_text(body, encoding="utf-8")
    return {"状态": "观测已开始", "目录": str(run), "说明": "由 hk-acceptance.timer 每分钟记录一次；尚未通过任何上线验收。"}


def sample(data_dir):
    run, _ = locate(data_dir)
    if run is None:
        return {"状态": "没有进行中的验收"}
    record = {"at": now().isoformat(), "health": False, "worker_at": None, "quote_at": None,
              "boot_hash": None}
    try:
        with urllib.request.urlopen("http://127.0.0.1:8738/api/health", timeout=5) as response:
            record["health"] = json.load(response).get("status") == "ok"
    except (OSError, ValueError):
        pass
    try:
        with db_open(data_dir) as db:
            for row in db.execute("SELECT name,updated_at FROM service_state WHERE name IN ('worker','quotes')"):
                record["worker_at" if row["name"] == "worker" else "quote_at"] = row["updated_at"]
    except sqlite3.Error:
        record["database_read_failed"] = True
    boot = Path("/proc/sys/kernel/random/boot_id")
    if boot.is_file():
        record["boot_hash"] = hashlib.sha256(boot.read_bytes()).hexdigest()[:16]
    with (run / "samples.jsonl").open("a", encoding="utf-8") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"状态": "已记录本次实际观测", "health": record["health"]}


def session_minutes(values, begin, end):
    expected = set()
    sessions = []
    for value in values:
        left, right = value.split("/", 1)
        start_at, end_at = instant(left), instant(right)
        if not begin <= start_at < end_at <= end:
            raise ValueError("交易时段必须完整落在本次真实观测区间内。")
        if start_at.second or start_at.microsecond or end_at.second or end_at.microsecond:
            raise ValueError("交易时段应对齐整分钟。")
        sessions.append({"start": start_at.isoformat(), "end": end_at.isoformat()})
        current = start_at
        while current < end_at:
            expected.add(current.isoformat(timespec="seconds"))
            current += timedelta(minutes=1)
    return expected, sessions


def report(data_dir, run_dir, sessions, stock, evidence):
    run, active = locate(data_dir) if run_dir is None else (run_dir, None)
    if run is None:
        raise ValueError("没有验收记录；先运行 start。")
    info = json.loads((run / "run.json").read_text(encoding="utf-8"))
    begin = instant(info["started_at"])
    end = instant(info["ended_at"]) if info.get("ended_at") else now()
    samples = []
    path = run / "samples.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if begin <= instant(value["at"]) <= end:
                    samples.append(value)
            except (ValueError, KeyError, TypeError):
                continue
    samples.sort(key=lambda row: row["at"])
    checkpoints = [begin, *(instant(row["at"]) for row in samples), end]
    gaps = [{"start": left.isoformat(), "end": right.isoformat(), "seconds": round((right-left).total_seconds())}
            for left, right in zip(checkpoints, checkpoints[1:]) if (right-left).total_seconds() > 180]
    expected, sessions = session_minutes(sessions, begin, end)
    if stock in INDICES or not re.fullmatch(r"HK\.\d{5}", stock):
        raise ValueError("验收需在三个指数之外指定至少一只五位代码股票。")
    quote_results, sources = [], []
    with db_open(data_dir) as db:
        for symbol in (*INDICES, stock):
            rows = db.execute("SELECT minute,quote_at,captured_at,data FROM quote_history WHERE symbol=? AND captured_at>=? AND captured_at<=?",
                              (symbol, begin.isoformat(), end.isoformat())).fetchall()
            actual, valid_quotes = set(), set()
            for row in rows:
                try:
                    payload = json.loads(row["data"])
                    quote_at, captured = instant(row["quote_at"]), instant(row["captured_at"])
                    if payload.get("status") == "live" and -5 <= (captured-quote_at).total_seconds() <= 65:
                        minute = captured.replace(second=0, microsecond=0).isoformat(timespec="seconds")
                        if minute in expected:
                            actual.add(minute)
                            valid_quotes.add(row["quote_at"])
                except (ValueError, KeyError, TypeError):
                    continue
            missing = sorted(expected - actual)
            quote_results.append({"symbol": symbol, "expected_minutes": len(expected), "live_minutes": len(actual),
                                  "missing_minutes": missing, "quote_time_advanced": len(valid_quotes) > 1,
                                  "passed": bool(expected) and not missing and len(valid_quotes) > 1})
        for scope in PLATFORMS:
            rows = db.execute("SELECT url,ok,last_success,error FROM source_checks WHERE scope=?", (scope,)).fetchall()
            good = [row for row in rows if row["last_success"] and begin <= instant(row["last_success"]) <= end]
            sources.append({"platform": scope, "successful_sources_in_run": len(good),
                            "failed_sources": [{"url": row["url"], "last_success": row["last_success"], "error": row["error"]}
                                               for row in rows if not row["ok"]], "passed": bool(good)})
    hours = (end - begin).total_seconds()/3600
    worker_ok = bool(samples) and all(row.get("worker_at") and
        -10 <= (instant(row["at"]) - instant(row["worker_at"])).total_seconds() <= 180 for row in samples)
    automated = {"观测不少于24小时": hours >= 24, "观测系统为Linux（云主机归属仍需人工核实）": info["system"] == "Linux",
                 "探针无超过3分钟空档": bool(samples) and not gaps,
                 "网页探针全部健康": bool(samples) and all(row["health"] for row in samples),
                 "采集心跳持续推进": worker_ok, "三个指数及股票的指定交易时段记录完整": all(row["passed"] for row in quote_results),
                 "五家来源在本次观测中有成功记录": all(row["passed"] for row in sources)}
    notes = json.loads(evidence.read_text(encoding="utf-8")) if evidence else {}
    required = ("cloud_host_confirmed", "mac_powered_off_24h", "full_hk_trading_day", "restart_and_recovery", "email_received",
                "same_change_only_one_email", "unchanged_page_no_email", "real_offer_full_flow", "external_heartbeat_alert")
    manual = {key: {"状态": "本人记录，尚需保留证据" if isinstance(notes.get(key), str) and notes[key].strip() else "待实测",
                    "说明": notes.get(key) if isinstance(notes.get(key), str) else ""} for key in required}
    result = {"结论": "自动项通过；人工证据请逐项复核" if all(automated.values()) else "验收未完成",
              "说明": "脚本不会以模拟数据、SMTP服务器接收或本机测试代替实收邮件和 Mac 关机实测。人工记录不等于机器验证。",
              "observed_hours": round(hours, 3), "sample_count": len(samples), "自动检查": automated,
              "observation_gaps": gaps, "交易时段": sessions, "行情": quote_results, "来源": sources, "人工实测": manual}
    destination = run / "report.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result | {"报告": str(destination)}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/hk-workbench"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start")
    commands.add_parser("sample")
    commands.add_parser("stop")
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--run-dir", type=Path)
    report_parser.add_argument("--session", action="append", default=[], help="官方交易时段开始/结束，ISO时间含时区；午间分两段")
    report_parser.add_argument("--stock", default="HK.00700")
    report_parser.add_argument("--evidence", type=Path, help="本人实测证据说明 JSON，不含密钥")
    args = parser.parse_args()
    try:
        if args.command == "start":
            result = start(args.data_dir)
        elif args.command == "sample":
            result = sample(args.data_dir)
        elif args.command == "stop":
            run, info = locate(args.data_dir)
            if run is None:
                raise ValueError("没有正在进行的验收。")
            info["ended_at"] = now().isoformat()
            (run / "run.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
            (args.data_dir / "acceptance/current.json").unlink()
            result = {"状态": "观测已停止，未声明验收通过", "目录": str(run)}
        else:
            result = report(args.data_dir, args.run_dir, args.session, args.stock, args.evidence)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.command == "report" and not all(result["自动检查"].values()) else 0
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        parser.exit(2, "尚不能完成：" + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
