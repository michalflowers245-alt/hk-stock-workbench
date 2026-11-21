"""Persistent quote thresholds, personal due dates and a Hong Kong time digest."""
from datetime import datetime, timedelta, timezone
import json
import math
import re

from storage import HK, clean_text, dump, normalize_symbol, timestamp

DEFAULTS = {"daily_digest": True, "digest_hour": 20,
            "urgent_changes": True, "task_reminders": True}


class Notifications:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, field TEXT NOT NULL,
                operator TEXT NOT NULL, threshold REAL NOT NULL, enabled INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, last_fired TEXT, last_quote_at TEXT
            );
            CREATE TABLE IF NOT EXISTS digest_members (
                event_id INTEGER PRIMARY KEY, digest_key TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notice_reads (
                event_id INTEGER PRIMARY KEY REFERENCES outbox(id), read_at TEXT NOT NULL
            );
            """)

    def prepare_local(self, instant=None):
        instant = instant or datetime.now(timezone.utc)
        config = self.settings()
        self.task_notices(config, instant)
        self.reward_notices(config, instant)
        self.ipo_notices(config, instant)
        # The local inbox does not depend on SMTP, delivery attempts or a digest clock.
        from collectors import PLATFORMS
        with self.store.connect() as db:
            deadlines = db.execute("SELECT id,offer_id,expected_deadline FROM outbox WHERE expected_deadline IS NOT NULL AND cancelled_at IS NULL").fetchall()
        invalid = []
        for event in deadlines:
            offer = self.store.offers(PLATFORMS, offer_id=event["offer_id"])
            if not offer or not offer["followed"] or offer["review_stale"] or offer["review_state"] == "ineligible" or offer.get("deadline") != event["expected_deadline"]:
                invalid.append(event["id"])
        if invalid:
            with self.store.connect(write=True) as db:
                db.executemany("UPDATE outbox SET cancelled_at=? WHERE id=?", [(instant.isoformat(timespec="seconds"), key) for key in invalid])

    def inbox(self):
        mark = datetime.now(timezone.utc).isoformat(timespec="seconds")
        condition = "e.cancelled_at IS NULL AND e.event_key NOT LIKE 'test:%' AND e.event_key NOT LIKE 'futu-renewal:%' AND e.event_key NOT LIKE 'digest:%'"
        with self.store.connect() as db:
            rows = db.execute("SELECT e.*,r.read_at FROM outbox e LEFT JOIN notice_reads r ON r.event_id=e.id WHERE " + condition + " ORDER BY (r.read_at IS NULL) DESC,e.id DESC LIMIT 100").fetchall()
            unread = db.execute("SELECT count(*) FROM outbox e LEFT JOIN notice_reads r ON r.event_id=e.id WHERE " + condition + " AND r.read_at IS NULL").fetchone()[0]
        items = []
        for row in rows:
            urgent = row["event_key"].startswith(("deadline:", "task:", "reward:", "ipo:", "quote:")) or bool(re.fullmatch(r"offer:\d+:(?:[2-9]|\d{2,})", row["event_key"]))
            items.append({"id": row["id"], "subject": row["subject"].removeprefix("【港股工作台】"),
                          "body": row["body"], "created_at": row["created_at"], "read_at": row["read_at"],
                          "level": "urgent" if urgent else "info", "expired": bool(row["expires_at"] and row["expires_at"] <= mark)})
        return {"events": items, "unread": unread}

    def read_notice(self, event_id=None):
        mark = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.store.connect(write=True) as db:
            if event_id is None:
                db.execute("INSERT OR IGNORE INTO notice_reads SELECT id,? FROM outbox WHERE cancelled_at IS NULL", (mark,))
                return True
            if not db.execute("SELECT 1 FROM outbox WHERE id=?", (event_id,)).fetchone():
                return False
            db.execute("INSERT OR IGNORE INTO notice_reads VALUES (?,?)", (event_id, mark))
            return True

    def settings(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='notifications'").fetchone()
        return DEFAULTS | (json.loads(row[0]) if row else {})

    def save_settings(self, data):
        result = self.settings()
        for key in ("daily_digest", "urgent_changes", "task_reminders"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise ValueError("提醒开关应为开启或关闭。")
                result[key] = data[key]
        if "digest_hour" in data:
            hour = data["digest_hour"]
            if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
                raise ValueError("日报时间应为香港时间 0 至 23 点。")
            result["digest_hour"] = hour
        with self.store.connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('notifications',?)", (dump(result),))
        return result

    def rules(self):
        with self.store.connect() as db:
            rules = [dict(r) for r in db.execute("SELECT * FROM alert_rules ORDER BY id DESC")]
            events = [dict(r) for r in db.execute(
                "SELECT id,subject,body,created_at,sent_at,last_error,cancelled_at FROM outbox "
                "WHERE event_key LIKE 'quote:%' ORDER BY id DESC LIMIT 50")]
        for rule in rules:
            rule["enabled"] = bool(rule["enabled"])
        return {"rules": rules, "events": events}

    def save_rule(self, data):
        symbol = normalize_symbol(data.get("symbol"))
        if symbol not in {r["symbol"] for r in self.store.watchlist()}:
            raise ValueError("请先把标的加入自选，再设置提醒。")
        field, operator = data.get("field"), data.get("operator")
        if field not in ("price", "change_percent") or operator not in ("gte", "lte"):
            raise ValueError("请选择价格或涨跌幅，以及达到或跌至条件。")
        try:
            threshold = float(data.get("threshold"))
        except (TypeError, ValueError):
            raise ValueError("请输入有效的阈值。") from None
        if not math.isfinite(threshold) or (field == "price" and threshold <= 0):
            raise ValueError("价格应大于零，阈值必须是有效数字。")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("提醒开关无效。")
        with self.store.connect(write=True) as db:
            if data.get("id"):
                rule_id = int(data["id"])
                updated = db.execute("UPDATE alert_rules SET symbol=?,field=?,operator=?,threshold=?,enabled=?,"
                                     "active=0,last_quote_at=NULL WHERE id=?",
                                     (symbol, field, operator, threshold, int(enabled), rule_id)).rowcount
                if not updated:
                    raise ValueError("提醒规则不存在。")
            else:
                if db.execute("SELECT count(*) FROM alert_rules").fetchone()[0] >= 100:
                    raise ValueError("最多保留 100 条行情提醒。")
                rule_id = db.execute("INSERT INTO alert_rules(symbol,field,operator,threshold,enabled) VALUES(?,?,?,?,?)",
                                     (symbol, field, operator, threshold, int(enabled))).lastrowid
        return {"id": rule_id}

    def delete_rule(self, rule_id):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE outbox SET cancelled_at=? WHERE event_key LIKE ? AND sent_at IS NULL",
                       (datetime.now(timezone.utc).isoformat(timespec="seconds"), f"quote:{rule_id}:%"))
            return bool(db.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,)).rowcount)

    def evaluate_quote(self, quote, instant=None):
        instant = instant or datetime.now(timezone.utc)
        quoted, captured = timestamp(quote.get("quote_at")), timestamp(quote.get("captured_at"))
        if quote.get("status") != "live" or not quoted or not captured:
            return
        if any(not -5 <= (instant - datetime.fromisoformat(t)).total_seconds() <= 120 for t in (quoted, captured)):
            return
        with self.store.connect(write=True) as db:
            for row in db.execute("SELECT * FROM alert_rules WHERE enabled=1 AND symbol=?", (quote.get("symbol"),)).fetchall():
                if row["last_quote_at"] and quoted <= row["last_quote_at"]:
                    continue
                value = quote.get("last_price" if row["field"] == "price" else "change_percent")
                if value is None and row["field"] == "change_percent" and quote.get("prev_close"):
                    value = (quote["last_price"] / quote["prev_close"] - 1) * 100
                if not isinstance(value, (int, float)) or not math.isfinite(value):
                    continue
                active = value >= row["threshold"] if row["operator"] == "gte" else value <= row["threshold"]
                last = datetime.fromisoformat(row["last_fired"]) if row["last_fired"] else None
                cooled = not last or instant - last >= timedelta(hours=24)
                fire = active and not row["active"] and cooled
                if fire:
                    mark = instant.isoformat(timespec="seconds")
                    label = "价格" if row["field"] == "price" else "涨跌幅（%）"
                    body = f"{row['symbol']} {label}：{value:g}；你设置的阈值：{row['threshold']:g}。\n报价时间：{quoted}"
                    self.store.add_event(db, f"quote:{row['id']}:{quoted}", f"【港股工作台】行情阈值提醒 · {row['symbol']}",
                                         body, expires=(instant + timedelta(hours=1)).isoformat(timespec="seconds"))
                else:
                    mark = row["last_fired"]
                db.execute("UPDATE alert_rules SET active=?,last_fired=?,last_quote_at=? WHERE id=?",
                           (int(active), mark, quoted, row["id"]))

    def prepare(self, instant=None):
        instant = instant or datetime.now(timezone.utc)
        config = self.settings()
        self.task_notices(config, instant)
        self.reward_notices(config, instant)
        self.ipo_notices(config, instant)
        self.digest(config, instant)

    def reward_notices(self, config, instant):
        entries = self.store.ledger()["entries"]
        mark = instant.isoformat(timespec="seconds")
        with self.store.connect(write=True) as db:
            live = set()
            for entry in entries:
                if not config["task_reminders"] or not entry.get("overdue"):
                    continue
                key = f"reward:{entry['id']}:{entry['expected_on']}"
                live.add(key)
                body = f"第 {entry['id']} 笔预计奖励已超过登记的到账日 {entry['expected_on']}。\n尚待核对：{entry['remaining_amount']} {entry.get('currency', 'HKD')}。\n请先核对平台到账记录，再更新台账。"
                self.store.add_event(db, key, "【港股工作台】预计奖励到账待核对", body)
            for event in db.execute("SELECT id,event_key FROM outbox WHERE event_key LIKE 'reward:%' AND sent_at IS NULL AND cancelled_at IS NULL").fetchall():
                if event["event_key"] not in live:
                    db.execute("UPDATE outbox SET cancelled_at=? WHERE id=?", (mark, event["id"]))

    def ipo_notices(self, config, instant):
        ipos = self.store.ipos()
        mark = instant.isoformat(timespec="seconds")
        with self.store.connect(write=True) as db:
            live = set()
            for item in ipos:
                for cutoff in item.get("broker_cutoffs", []):
                    deadline = datetime.fromisoformat(cutoff["deadline"])
                    if not config["task_reminders"] or not cutoff.get("verified") or not instant < deadline <= instant + timedelta(hours=24):
                        continue
                    key = f"ipo:{item['identity']}:{cutoff['platform']}:{cutoff['deadline']}"
                    live.add(key)
                    body = f"{item.get('name') or item.get('issuer') or item.get('code', '新股')}\n券商截单：{deadline.astimezone(HK).strftime('%Y-%m-%d %H:%M')} 香港时间。\n官方来源：{cutoff['source_url']}"
                    self.store.add_event(db, key, "【港股工作台】券商申购即将截单", body, expires=cutoff["deadline"])
            for event in db.execute("SELECT id,event_key FROM outbox WHERE event_key LIKE 'ipo:%' AND sent_at IS NULL AND cancelled_at IS NULL").fetchall():
                if event["event_key"] not in live:
                    db.execute("UPDATE outbox SET cancelled_at=? WHERE id=?", (mark, event["id"]))

    def task_notices(self, config, instant):
        mark = instant.isoformat(timespec="seconds")
        tasks = self.store.tasks()
        local = instant.astimezone(HK)
        today, tomorrow = local.date().isoformat(), (local.date() + timedelta(days=1)).isoformat()
        with self.store.connect(write=True) as db:
            live_keys = set()
            for task in tasks:
                if not config["task_reminders"] or task.get("completed") or not task.get("due_on"):
                    continue
                due = task["due_on"]
                if due > tomorrow:
                    continue
                key = f"task:{task['id']}:{due}"
                live_keys.add(key)
                label = "已过期" if due < today else ("今天到期" if due == today else "明天到期")
                body = f"{task.get('offer_title', '')}\n{task['title']}：{label}（{due}，具体时点请核对）。"
                self.store.add_event(db, key, "【港股工作台】实操任务待处理", body)
            for event in db.execute("SELECT id,event_key FROM outbox WHERE event_key LIKE 'task:%' AND sent_at IS NULL AND cancelled_at IS NULL").fetchall():
                if event["event_key"] not in live_keys:
                    db.execute("UPDATE outbox SET cancelled_at=? WHERE id=?", (mark, event["id"]))

    def digest(self, config, instant):
        local = instant.astimezone(HK)
        mark = instant.isoformat(timespec="seconds")
        scheduled = local.replace(hour=config["digest_hour"], minute=0, second=0, microsecond=0)
        key = "digest:" + local.date().isoformat()
        with self.store.connect(write=True) as db:
            candidates = db.execute("SELECT * FROM outbox WHERE event_key LIKE 'offer:%' AND sent_at IS NULL "
                                    "AND cancelled_at IS NULL AND (expires_at IS NULL OR expires_at>?) ORDER BY id", (mark,)).fetchall()
            ordinary = []
            for item in candidates:
                match = re.fullmatch(r"offer:(\d+):(\d+)", item["event_key"])
                is_change = bool(match and int(match[2]) > 1)
                expires_soon = bool(item["expires_at"] and datetime.fromisoformat(item["expires_at"]) <= instant + timedelta(hours=24))
                if expires_soon or (is_change and config["urgent_changes"]):
                    # A followed critical change may need attention before the daily digest.
                    db.execute("UPDATE outbox SET due_at=min(due_at,?) WHERE id=?", (mark, item["id"]))
                else:
                    ordinary.append(item)
            if not config["daily_digest"]:
                for event in ordinary:
                    db.execute("UPDATE outbox SET due_at=min(due_at,?) WHERE id=?", (mark, event["id"]))
                return
            exists = db.execute("SELECT id FROM outbox WHERE event_key=?", (key,)).fetchone()
            if local < scheduled or exists:
                next_time = scheduled if local < scheduled else scheduled + timedelta(days=1)
                for event in ordinary:
                    db.execute("UPDATE outbox SET due_at=? WHERE id=?", (next_time.astimezone(timezone.utc).isoformat(timespec="seconds"), event["id"]))
                return
            if not ordinary:
                return
            body = "以下为今天新发现或待核对的活动线索，资格和收益仍需核实：\n\n" + "\n\n".join(r["body"] for r in ordinary)
            self.store.add_event(db, key, f"【港股工作台】每日活动摘要 · {len(ordinary)} 条", body,
                                 expires=(instant + timedelta(days=2)).isoformat(timespec="seconds"))
            for event in ordinary:
                db.execute("INSERT OR IGNORE INTO digest_members VALUES (?,?)", (event["id"], key))
                db.execute("UPDATE outbox SET cancelled_at=? WHERE id=?", (mark, event["id"]))
