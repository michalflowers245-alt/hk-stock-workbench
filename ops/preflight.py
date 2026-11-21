#!/usr/bin/env python3
"""只读上线预检；不会创建云资源、升级账户、发邮件或打印密钥。"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request

FREE_SOURCE = "https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm"


def free_check(record):
    """验证人工填写的控制台核对记录；不把该记录冒充实时账单 API。"""
    failures = []
    if record.get("account_type") != "free":
        failures.append("账户必须保持 Free；试用赠金或 Pay As You Go 不视为本方案已确认免费")
    if record.get("always_free_labels_verified") is not True:
        failures.append("尚未核对资源的 Always Free 标识")
    if record.get("home_region_verified") is not True:
        failures.append("尚未确认资源位于账户主区域")
    if isinstance(record.get("console_estimated_monthly_cost"), bool) or record.get("console_estimated_monthly_cost") != 0:
        failures.append("控制台预估月费尚未确认为 0")
    if record.get("other_paid_resources_absent") is not True:
        failures.append("尚未确认账户没有其他收费资源")
    for key, limit in (("a1_total_ocpus", 2), ("a1_total_memory_gb", 12),
                       ("e2_micro_instance_count", 2), ("total_boot_and_block_volume_gb", 200)):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= limit:
            failures.append(f"{key} 需记录全部资源合计，范围 0–{limit}")
    try:
        checked = datetime.fromisoformat(record["verified_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        if not 0 <= age <= 86400:
            failures.append("控制台核对记录需为最近 24 小时内")
    except (KeyError, ValueError, TypeError):
        failures.append("缺少带时区的控制台核对时间")
    if not str(record.get("evidence_note") or "").strip():
        failures.append("缺少不含凭据的控制台核对说明")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="只检查本机开发环境，不代表云端已上线")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/hk-workbench.env"))
    parser.add_argument("--data-dir", type=Path, default=Path("/var/lib/hk-workbench"))
    parser.add_argument("--free-record", type=Path, help="按模板填写的控制台免费资源核对记录")
    parser.add_argument("--runtime", action="store_true", help="同时检查已安装的服务和本机健康接口")
    args = parser.parse_args()
    checks = []

    def check(name, ok, detail, pending=False):
        checks.append({"项目": name, "状态": "通过" if ok else "待核实" if pending else "未通过", "说明": detail})

    check("Python", sys.version_info >= (3, 11), "需要 Python 3.11 或以上")
    check("SQLite", importlib.util.find_spec("sqlite3") is not None, "使用内置 SQLite")
    if not args.local:
        check("云主机系统", platform.system() == "Linux" and shutil.which("systemctl") is not None,
              "需在 Linux + systemd 云主机执行；本机或容器结果不能替代云端")
        check("系统已启动 systemd", Path("/run/systemd/system").is_dir(), "检查运行中的 systemd，不只检查命令是否安装")
        if args.free_record:
            try:
                errors = free_check(json.loads(args.free_record.read_text(encoding="utf-8")))
                check("免费资源控制台记录", not errors, "；".join(errors) if errors else "人工记录符合免费额度；脚本没有访问实时账单")
            except (OSError, ValueError) as exc:
                check("免费资源控制台记录", False, str(exc))
        else:
            check("免费资源控制台记录", False, "缺少 --free-record；不能确认账户与资源免费", pending=True)
    if args.env_file.is_file():
        mode = stat.S_IMODE(args.env_file.stat().st_mode)
        check("配置权限", mode & 0o077 == 0, "配置文件仅应允许所有者读取")
        keys = {}
        for line in args.env_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                keys[name.strip()] = value.strip().strip('\"\'')
        required = ("HK_SMTP_HOST", "HK_SMTP_USER", "HK_SMTP_PASSWORD", "HK_MAIL_FROM", "HK_MAIL_TO")
        missing = [name for name in required if not keys.get(name)]
        check("发件配置", not missing, "字段已填写，仍需实收邮件确认" if not missing else "缺少：" + "、".join(missing), pending=True)
        check("私人入口", bool(keys.get("HK_PRIVATE_URL", "").startswith("https://") and keys.get("HK_ALLOWED_HOSTS")),
              "要求私人 HTTPS 地址与允许的域名；脚本不输出其值", pending=True)
        check("独立心跳", bool(keys.get("HK_HEALTHCHECK_URL", "").startswith("https://")),
              "需配置外部免费心跳并实测停机通知", pending=True)
    elif not args.local or args.runtime:
        check("服务配置", False, "配置文件不存在", pending=True)
    if args.data_dir.exists():
        check("数据目录权限", stat.S_IMODE(args.data_dir.stat().st_mode) & 0o077 == 0,
              "数据目录只应允许服务所有者访问")
        free_gb = shutil.disk_usage(args.data_dir).free / (1024 ** 3)
        check("磁盘余量", free_gb >= 1, f"剩余 {free_gb:.1f} GB；至少预留 1 GB")
    if args.runtime:
        for module in ("cryptography", "websockets"):
            check(module, importlib.util.find_spec(module) is not None, "已安装的行情依赖")
        for unit in ("hk-workbench.service", "hk-worker.service", "hk-backup.timer", "hk-acceptance.timer"):
            if shutil.which("systemctl"):
                active = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
                check(unit, active.returncode == 0, active.stdout.strip() or "服务未运行")
            else:
                check(unit, False, "本系统没有 systemctl")
        try:
            request = urllib.request.Request("http://127.0.0.1:8738/api/health")
            with urllib.request.urlopen(request, timeout=5) as response:
                check("网页健康接口", json.load(response).get("status") == "ok", "只测试本机回环接口")
        except (OSError, ValueError) as exc:
            check("网页健康接口", False, type(exc).__name__)
    success = all(item["状态"] == "通过" for item in checks)
    print(json.dumps({"结果": "本机开发预检通过" if success and args.local else "自动预检通过，继续实测" if success else "仍有未完成项",
                      "检查": checks, "免费依据": FREE_SOURCE,
                      "边界": "不创建资源、不确认实时账单；不证明 Mac 关机 24 小时、邮箱收到邮件或真实活动完成。"}, ensure_ascii=False, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
