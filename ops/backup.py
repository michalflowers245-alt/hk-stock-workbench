#!/usr/bin/env python3
"""仅备份 SQLite 业务库；恢复演练永远写入独立空目录。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from zoneinfo import ZoneInfo


def inspect(path):
    # 此函数只用于已经完成的一致性快照，immutable 防止检查时创建 WAL/SHM。
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError("数据库完整性检查失败。")
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in tables}
    return counts


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, content):
    with path.open("x", encoding="utf-8") as dst:
        json.dump(content, dst, ensure_ascii=False, indent=2)
        dst.write("\n")
    path.chmod(0o600)


def create_backup(data_dir, backup_dir, retain=14):
    source = data_dir.resolve() / "workbench.sqlite3"
    if not source.is_file():
        raise ValueError("业务库尚未创建，不能生成空备份。")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_dir.is_symlink():
        raise ValueError("备份目录不能是符号链接。")
    backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / ("hk-workbench-" + stamp + ".sqlite3")
    descriptor, temporary = tempfile.mkstemp(prefix=".backup-", suffix=".sqlite3", dir=backup_dir)
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
        counts = inspect(temporary)
        os.replace(temporary, destination)
        destination.chmod(0o600)
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "file": destination.name,
                    "sha256": digest(destination), "bytes": destination.stat().st_size,
                    "tables": counts, "includes": "仅 SQLite 业务库；含个人台账",
                    "excludes": ["邮箱配置", "富途授权文件和加密密钥", "心跳地址", "AI 密钥"]}
        write_json(destination.with_suffix(".json"), manifest)
        # 只删除本脚本命名且具有配套清单的旧文件，不触碰其他备份。
        pairs = sorted((p for p in backup_dir.glob("hk-workbench-*.sqlite3")
                        if p.with_suffix(".json").is_file()), reverse=True)
        for old in pairs[max(1, retain):]:
            old.unlink()
            old.with_suffix(".json").unlink()
        return {"status": "备份完成", "path": str(destination), "sha256": manifest["sha256"],
                "tables": counts, "credentials_included": False}
    finally:
        temporary.unlink(missing_ok=True)


def create_daily_backup(data_dir):
    """Use a single daily snapshot even if the launcher and collector start together."""
    backup_dir = data_dir / "backups"
    hong_kong = ZoneInfo("Asia/Hong_Kong")
    with (data_dir / ".daily-backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        today = datetime.now(hong_kong).date()
        for manifest in backup_dir.glob("hk-workbench-*.json"):
            try:
                record = json.loads(manifest.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(record["created_at"])
                if created.astimezone(hong_kong).date() == today and manifest.with_suffix(".sqlite3").is_file():
                    return
            except (OSError, ValueError, KeyError, TypeError):
                continue
        create_backup(data_dir, backup_dir)


def restore_drill(backup, target):
    backup = backup.resolve()
    manifest_path = backup.with_suffix(".json")
    if not manifest_path.is_file():
        raise ValueError("缺少配套校验清单，不能确认备份完整性。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if digest(backup) != manifest.get("sha256"):
        raise ValueError("备份 SHA-256 与清单不符，停止恢复。")
    if target.exists() and (target.is_symlink() or any(target.iterdir())):
        raise ValueError("恢复演练只允许使用新目录或空目录，不能覆盖运行数据。")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    restored = target / "workbench.sqlite3"
    shutil.copyfile(backup, restored)
    restored.chmod(0o600)
    counts = inspect(restored)
    if counts != manifest.get("tables") or digest(restored) != manifest["sha256"]:
        restored.unlink(missing_ok=True)
        raise ValueError("恢复后的校验或各表行数不一致。")
    return {"status": "隔离恢复演练通过", "path": str(restored), "tables": counts,
            "production_replaced": False, "credentials_restored": False}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create", help="生成经过校验的一致性备份")
    backup.add_argument("--data-dir", type=Path, default=Path("/var/lib/hk-workbench"))
    backup.add_argument("--backup-dir", type=Path, default=Path("/var/lib/hk-workbench/backups"))
    backup.add_argument("--retain", type=int, default=14)
    restore = commands.add_parser("drill", help="只在独立空目录恢复并检查")
    restore.add_argument("backup", type=Path)
    restore.add_argument("target", type=Path)
    args = parser.parse_args()
    try:
        result = create_backup(args.data_dir, args.backup_dir, args.retain) if args.command == "create" else restore_drill(args.backup, args.target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2, "操作未完成：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
