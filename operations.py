"""Personal eligibility, workflow and capital planning; no automated participation."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re

CURRENCIES = ("HKD", "CNY", "USD")
STAGES = ("registration", "deposit", "qualify", "hold", "claim", "payout")
STAGE_LABELS = {"registration": "登记报名", "deposit": "入金", "qualify": "达标", "hold": "持有", "claim": "领取", "payout": "到账"}


def money(data, *, signed=False, optional=False, field="amount"):
    raw = data.get(field)
    if optional and raw in (None, ""):
        return None, "HKD", "1", None
    currency = str(data.get("currency") or "HKD").upper()
    if currency not in CURRENCIES:
        raise ValueError("币种仅支持 HKD、CNY、USD。")
    try:
        value = Decimal(str(raw))
        rate = Decimal(str(data.get("rate_hkd") if data.get("rate_hkd") is not None else (1 if currency == "HKD" else "NaN")))
        if not value.is_finite() or abs(value) > 100000000 or (value <= 0 and not signed) or value != value.quantize(Decimal(".01")):
            raise ValueError
        if not rate.is_finite() or not 0 < rate <= 100000 or (currency == "HKD" and rate != 1):
            raise ValueError
        cents = int(value * 100)
        hkd = int((value * rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return cents, currency, str(rate), hkd
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("请填写有效金额（最多两位小数）及明确的港元换算汇率；港元汇率为 1。")


def migrate(db):
    """Additive schema upgrade. The caller backs up a v1 database first."""
    additions = {
        "offers": {"review_stale": "INTEGER NOT NULL DEFAULT 0", "reviewed_version": "INTEGER"},
        "ledger": {"currency": "TEXT NOT NULL DEFAULT 'HKD'", "rate_hkd": "TEXT NOT NULL DEFAULT '1'",
                   "hkd_cents": "INTEGER", "expected_entry_id": "INTEGER REFERENCES ledger(id)",
                   "idempotency_key": "TEXT", "payload_hash": "TEXT", "created_at": "TEXT", "voided_at": "TEXT",
                   "expected_on": "TEXT", "voucher_scope": "TEXT NOT NULL DEFAULT ''", "redeemed_cents": "INTEGER NOT NULL DEFAULT 0"},
    }
    for table, columns in additions.items():
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.execute("UPDATE ledger SET hkd_cents=cents WHERE hkd_cents IS NULL")
    statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS ledger_submission ON ledger(idempotency_key) WHERE idempotency_key IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ledger_expected ON ledger(expected_entry_id)",
        "CREATE TABLE IF NOT EXISTS voucher_redemptions (id INTEGER PRIMARY KEY,ledger_id INTEGER NOT NULL REFERENCES ledger(id),old_cents INTEGER NOT NULL,new_cents INTEGER NOT NULL,recorded_at TEXT NOT NULL,note TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS review_history (id INTEGER PRIMARY KEY,offer_id INTEGER NOT NULL REFERENCES offers(id),version INTEGER NOT NULL,review_state TEXT NOT NULL,review TEXT NOT NULL,deadline_override TEXT,created_at TEXT NOT NULL,reason TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS profiles (platform TEXT PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS requirements (offer_id INTEGER PRIMARY KEY REFERENCES offers(id),version INTEGER NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS activity_tasks (id INTEGER PRIMARY KEY,offer_id INTEGER NOT NULL REFERENCES offers(id),stage TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS capital_reservations (id INTEGER PRIMARY KEY,offer_id INTEGER REFERENCES offers(id),payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS retrospectives (offer_id INTEGER PRIMARY KEY REFERENCES offers(id),payload TEXT NOT NULL,updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS ipo_cutoffs (identity TEXT NOT NULL REFERENCES ipos(identity),platform TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(identity,platform))",
    ]
    for statement in statements:
        db.execute(statement)
    db.execute("PRAGMA user_version=4")


def _bool_or_none(value, label):
    if value is not None and type(value) is not bool:
        raise ValueError(label + "请填写是、否或未知。")
    return value


class OperationsMixin:
    def budget(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='capital_budget'").fetchone()
        return json.loads(row[0]) if row else {"amount": None, "currency": "HKD", "rate_hkd": 1, "available_hkd": None}

    def save_budget(self, data):
        from storage import dump
        cents, currency, rate, hkd = money(data, optional=True, signed=True)
        if cents is not None and cents < 0:
            raise ValueError("总可用资金不能为负。")
        payload = {"amount": cents / 100 if cents is not None else None, "currency": currency,
                   "rate_hkd": float(rate), "available_hkd": hkd / 100 if hkd is not None else None}
        with self.connect(write=True) as db:
            db.execute("INSERT INTO settings VALUES ('capital_budget',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dump(payload),))
        return payload

    def profiles(self):
        from storage import PLATFORM_DOMAINS
        with self.connect() as db:
            saved = {row["platform"]: json.loads(row["payload"]) | {"updated_at": row["updated_at"]} for row in db.execute("SELECT * FROM profiles")}
        return {"profiles": [{"platform": platform, "status": "unknown", "opened_on": None,
                "invited": None, "prior_owner": None, "available_capital": None, "currency": "HKD",
                "rate_hkd": 1, "capital_hkd": None, "notes": ""} | saved.get(platform, {}) for platform in PLATFORM_DOMAINS], "budget": self.budget()}

    def save_profile(self, platform, data):
        from storage import PLATFORM_DOMAINS, calendar_date, now, dump
        if platform not in PLATFORM_DOMAINS:
            raise ValueError("请选择五家平台之一。")
        current = next(p for p in self.profiles()["profiles"] if p["platform"] == platform)
        values = current | data
        status = values.get("status")
        if status not in ("unknown", "none", "active", "closed"):
            raise ValueError("账户状态无效。")
        opened = calendar_date(values.get("opened_on"))
        if values.get("opened_on") and not opened:
            raise ValueError("开户日期格式无效。")
        cents, currency, rate, hkd = money(values, signed=True, optional=True, field="available_capital")
        if cents is not None and cents < 0:
            raise ValueError("账户可用资金不能为负。")
        payload = {"platform": platform, "status": status, "opened_on": opened,
            "invited": _bool_or_none(values.get("invited"), "受邀状态"),
            "prior_owner": _bool_or_none(values.get("prior_owner"), "历史开户状态"),
            "available_capital": cents / 100 if cents is not None else None, "currency": currency,
            "rate_hkd": float(rate), "capital_hkd": hkd / 100 if hkd is not None else None,
            "notes": str(values.get("notes") or "")[:2500]}
        with self.connect(write=True) as db:
            db.execute("INSERT INTO profiles VALUES (?,?,?) ON CONFLICT(platform) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (platform, dump(payload), now()))
        return payload

    def requirements(self, offer_id):
        with self.connect() as db:
            row = db.execute("SELECT r.*,o.version AS current_version FROM requirements r JOIN offers o ON r.offer_id=o.id WHERE offer_id=?", (offer_id,)).fetchone()
        if not row:
            return {"reviewed": False, "source_url": "", "eligibility": {}, "capital_hkd": None, "holding_days": None, "notes": "", "stale": False}
        return json.loads(row["payload"]) | {"version": row["version"], "stale": row["version"] != row["current_version"], "updated_at": row["updated_at"]}

    def save_requirements(self, offer_id, data):
        from storage import now, dump, official_url, calendar_date
        with self.connect(write=True) as db:
            offer = db.execute("SELECT platform,version,review_stale FROM offers WHERE id=?", (offer_id,)).fetchone()
            if not offer:
                raise ValueError("活动不存在。")
            if data.get("reviewed") and (data.get("reviewed_version", offer["version"]) != offer["version"] or offer["review_stale"] and "reviewed_version" not in data):
                raise ValueError("活动条款已变更，请刷新并按最新版本核对参与要求。")
            eligibility = data.get("eligibility") or {}
            if not isinstance(eligibility, dict):
                raise ValueError("参与要求格式无效。")
            checked = {}
            for key in ("new_customer", "invitation_required"):
                checked[key] = _bool_or_none(eligibility.get(key), "参与要求")
            for key in ("opened_after", "opened_before"):
                checked[key] = calendar_date(eligibility.get(key))
                if eligibility.get(key) and not checked[key]:
                    raise ValueError("开户要求日期无效。")
            if checked["opened_after"] and checked["opened_before"] and checked["opened_after"] > checked["opened_before"]:
                raise ValueError("开户要求日期范围倒置。")
            reviewed = data.get("reviewed", False)
            if type(reviewed) is not bool:
                raise ValueError("复核状态无效。")
            source = official_url(offer["platform"], data.get("source_url"))
            if data.get("source_url") and not source or reviewed and not source:
                raise ValueError("已复核条件必须附该平台官方来源。")
            capital = data.get("capital_hkd")
            if capital in (None, ""):
                capital = None
            else:
                cents, _, _, _ = money({"amount": capital}, signed=True)
                if cents < 0:
                    raise ValueError("最低资金不能为负。")
                capital = cents / 100
            holding = data.get("holding_days")
            if holding in (None, ""):
                holding = None
            elif isinstance(holding, bool) or not str(holding).isdigit() or not 0 <= int(holding) <= 36500:
                raise ValueError("持有天数须为非负整数。")
            else:
                holding = int(holding)
            payload = {"reviewed": reviewed, "source_url": source, "eligibility": checked,
                       "capital_hkd": capital, "holding_days": holding, "notes": str(data.get("notes") or "")[:4000]}
            db.execute("INSERT INTO requirements VALUES (?,?,?,?) ON CONFLICT(offer_id) DO UPDATE SET version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at", (offer_id, offer["version"], dump(payload), now()))
        return self.requirements(offer_id)

    def assess_offer(self, offer_id):
        with self.connect() as db:
            offer = db.execute("SELECT o.*,v.payload FROM offers o JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version WHERE o.id=?", (offer_id,)).fetchone()
        if not offer:
            return None
        payload = json.loads(offer["payload"])
        profile = next(p for p in self.profiles()["profiles"] if p["platform"] == offer["platform"])
        req = self.requirements(offer_id)
        reasons = []
        def add(label, state, detail):
            reasons.append({"label": label, "state": state, "detail": detail})
        if self.is_expired(payload):
            add("活动期限", "unmet", "官方已确认期限已过")
        if offer["review_stale"]:
            add("复核版本", "unknown", "官方条款已更新，原复核需重新确认")
        if offer["review_state"] == "ineligible":
            add("本人复核", "unmet", "已标记不符合参与条件")
        if not req.get("reviewed") or req.get("stale"):
            add("参与要求", "unknown", "结构化要求尚未按最新官方条款复核")
        else:
            eligibility = req["eligibility"]
            if eligibility.get("new_customer"):
                state = "unmet" if profile["prior_owner"] is True or profile["status"] in ("active", "closed") else "satisfied" if profile["prior_owner"] is False and profile["status"] == "none" else "unknown"
                add("新客条件", state, "比对当前账户及历史开户情况；以官方新客定义为准")
            if eligibility.get("invitation_required"):
                add("受邀条件", "unknown" if profile["invited"] is None else "satisfied" if profile["invited"] else "unmet", "比对本人受邀状态")
            for key, label, compare in (("opened_after", "开户起日", lambda a,b: a >= b), ("opened_before", "开户末日", lambda a,b: a <= b)):
                if eligibility.get(key):
                    add(label, "unknown" if not profile["opened_on"] else "satisfied" if compare(profile["opened_on"], eligibility[key]) else "unmet", "要求 " + eligibility[key] + "；本人 " + (profile["opened_on"] or "待补充"))
            budget = self.budget()["available_hkd"]
            if req.get("capital_hkd") is None:
                add("资金要求", "unknown", "最低资金尚未按官方条款确认；无资金要求请明确填 0")
            else:
                add("可用资金", "unknown" if budget is None else "satisfied" if Decimal(str(budget)) >= Decimal(str(req["capital_hkd"])) else "unmet", f"要求 HKD {req['capital_hkd']:,.2f}；预算 " + ("待填写" if budget is None else f"HKD {budget:,.2f}"))
            if not reasons:
                add("已录入条件", "satisfied", "已复核的结构化条件均满足；其他官方条件仍须本人核对")
        state = "unmet" if any(r["state"] == "unmet" for r in reasons) else "unknown" if any(r["state"] == "unknown" for r in reasons) else "satisfied"
        priority = {"satisfied": 80, "unknown": 45, "unmet": 5}[state]
        priority_reasons = [{"satisfied": "已核对条件满足", "unknown": "仍有条件待核实", "unmet": "存在明确不符合条件"}[state]]
        title = payload.get("title", "")
        is_loan = bool(re.search(r"貸款|贷款|借貸|借贷|信貸|信贷|\bloan\b", title, re.I))
        if is_loan:
            priority -= 30
            priority_reasons.append("贷款类活动需承担借款成本，默认降低优先级")
        if offer["followed"]:
            priority += 10
            priority_reasons.append("已关注")
        return {"state": state, "reasons": reasons, "priority": priority, "priority_reasons": priority_reasons, "loan_promotion": is_loan}

    def tasks(self, offer_id=None):
        with self.connect() as db:
            rows = db.execute("SELECT t.*,v.payload AS offer FROM activity_tasks t JOIN offers o ON o.id=t.offer_id JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version" + (" WHERE t.offer_id=?" if offer_id is not None else "") + " ORDER BY t.id", (offer_id,) if offer_id is not None else ()).fetchall()
        items = [json.loads(row["payload"]) | {"id": row["id"], "offer_id": row["offer_id"], "stage": row["stage"], "updated_at": row["updated_at"], "offer_title": json.loads(row["offer"]).get("title")} for row in rows]
        complete = {item["id"] for item in items if item["completed"]}
        for item in items:
            item["blocked_by"] = [key for key in item["prerequisite_ids"] if key not in complete]
            item["overdue"] = bool(not item["completed"] and item["due_on"] and item["due_on"] < datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat())
        return sorted(items, key=lambda x: (STAGES.index(x["stage"]), x["due_on"] or "9999", x["id"]))

    def save_task(self, offer_id, data):
        from storage import now, dump, calendar_date
        stage = data.get("stage")
        if stage not in STAGES:
            raise ValueError("请选择有效执行阶段。")
        due = calendar_date(data.get("due_on"))
        if data.get("due_on") and not due:
            raise ValueError("任务日期无效。")
        completed = data.get("completed", False)
        if type(completed) is not bool:
            raise ValueError("任务完成状态无效。")
        prereqs = data.get("prerequisite_ids") or []
        if not isinstance(prereqs, list) or len(prereqs) > 100 or any(type(i) is not int or i <= 0 for i in prereqs):
            raise ValueError("前置任务编号无效。")
        prereqs = list(dict.fromkeys(prereqs))
        evidence = str(data.get("evidence") or "")[:4000]
        if completed and not evidence.strip():
            raise ValueError("完成任务前请填写实际凭据或记录。")
        item_id = data.get("id")
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
                raise ValueError("活动不存在。")
            old = db.execute("SELECT * FROM activity_tasks WHERE id=? AND offer_id=?", (item_id, offer_id)).fetchone() if item_id else None
            if item_id and not old:
                raise ValueError("任务不存在。")
            all_tasks = {row["id"]: json.loads(row["payload"]) | {"stage": row["stage"]} for row in db.execute("SELECT * FROM activity_tasks WHERE offer_id=?", (offer_id,))}
            for key in prereqs:
                if key not in all_tasks or key == item_id or STAGES.index(all_tasks[key]["stage"]) > STAGES.index(stage):
                    raise ValueError("前置任务必须属于本活动，且不能在后续阶段或指向自身。")
                if completed and not all_tasks[key]["completed"]:
                    raise ValueError("请先完成前置任务。")
            graph = {key: task["prerequisite_ids"] for key, task in all_tasks.items()}
            if item_id:
                graph[item_id] = prereqs
                def visits(key, path):
                    if key in path:
                        raise ValueError("前置任务形成循环。")
                    for parent in graph.get(key, []):
                        visits(parent, path | {key})
                visits(item_id, set())
                if not completed and any(t["completed"] and item_id in t["prerequisite_ids"] for key, t in all_tasks.items() if key != item_id):
                    raise ValueError("已有已完成任务依赖此任务，请先撤销后续任务的完成状态。")
            payload = {"title": str(data.get("title") or STAGE_LABELS[stage])[:160], "due_on": due,
                       "evidence": evidence, "completed": completed, "completed_at": (json.loads(old["payload"]).get("completed_at") if old and json.loads(old["payload"]).get("completed") else now()) if completed else None,
                       "prerequisite_ids": prereqs}
            if old:
                db.execute("UPDATE activity_tasks SET stage=?,payload=?,updated_at=? WHERE id=?", (stage, dump(payload), now(), item_id))
            else:
                item_id = db.execute("INSERT INTO activity_tasks(offer_id,stage,payload,updated_at) VALUES (?,?,?,?)", (offer_id, stage, dump(payload), now())).lastrowid
        return next(item for item in self.tasks(offer_id) if item["id"] == item_id)

    def save_reservation(self, data):
        from storage import now, dump, calendar_date
        cents, currency, rate, hkd = money(data)
        start, end = calendar_date(data.get("starts_on")), calendar_date(data.get("ends_on"))
        if not start or not end or end < start or end >= "9999-12-31":
            raise ValueError("资金占用须填写有效起止日期，结束日不能早于开始日。")
        status = data.get("status", "planned")
        if status not in ("planned", "active", "released"):
            raise ValueError("资金占用状态无效。")
        offer_id, item_id = data.get("offer_id") or None, data.get("id")
        payload = {"amount": cents / 100, "currency": currency, "rate_hkd": float(rate), "amount_hkd": hkd / 100,
                   "starts_on": start, "ends_on": end, "status": status, "note": str(data.get("note") or "")[:2500]}
        with self.connect(write=True) as db:
            if offer_id and not db.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
                raise ValueError("关联活动不存在。")
            if item_id:
                if not db.execute("UPDATE capital_reservations SET offer_id=?,payload=?,updated_at=? WHERE id=?", (offer_id, dump(payload), now(), item_id)).rowcount:
                    raise ValueError("资金占用记录不存在。")
            else:
                item_id = db.execute("INSERT INTO capital_reservations(offer_id,payload,updated_at) VALUES (?,?,?)", (offer_id, dump(payload), now())).lastrowid
        return payload | {"id": item_id, "offer_id": offer_id}

    def reservations(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM capital_reservations ORDER BY id DESC").fetchall()
        entries = [json.loads(row["payload"]) | {"id": row["id"], "offer_id": row["offer_id"], "updated_at": row["updated_at"]} for row in rows]
        active = [r for r in entries if r["status"] != "released"]
        budget = self.budget()
        points = sorted({point for r in active for point in (r["starts_on"], (date.fromisoformat(r["ends_on"]) + timedelta(days=1)).isoformat())})
        calendar = []
        for start, stop in zip(points, points[1:]):
            overlapping = [r for r in active if r["starts_on"] <= start <= r["ends_on"]]
            if not overlapping:
                continue
            total_cents = sum(int(Decimal(str(r["amount_hkd"])) * 100) for r in overlapping)
            total = total_cents / 100
            available_cents = None if budget["available_hkd"] is None else int(Decimal(str(budget["available_hkd"])) * 100) - total_cents
            available = None if available_cents is None else available_cents / 100
            calendar.append({"starts_on": start, "ends_on": (date.fromisoformat(stop) - timedelta(days=1)).isoformat(), "reserved_hkd": total,
                "available_hkd": available, "shortfall_hkd": max(0, -available) if available is not None else None, "reservation_ids": [r["id"] for r in overlapping]})
        today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        current = next((row for row in calendar if row["starts_on"] <= today <= row["ends_on"]), None)
        return {"entries": entries, "budget": budget, "calendar": calendar, "conflicts": [r for r in calendar if r["shortfall_hkd"]],
                "peak_reserved_hkd": max((r["reserved_hkd"] for r in calendar), default=0),
                "available_hkd": current["available_hkd"] if current else budget["available_hkd"]}

    def retrospective(self, offer_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM retrospectives WHERE offer_id=?", (offer_id,)).fetchone()
        return json.loads(row["payload"]) | {"updated_at": row["updated_at"]} if row else None

    def save_retrospective(self, offer_id, data):
        from storage import dump, now
        minutes = data.get("actual_minutes")
        if isinstance(minutes, bool) or not str(minutes).isdigit() or not 0 <= int(minutes) <= 1000000:
            raise ValueError("实际耗时请填写非负整数分钟。")
        approved = data.get("shareable_approved", False)
        if type(approved) is not bool:
            raise ValueError("分享确认状态无效。")
        payload = {"actual_minutes": int(minutes), "problems": str(data.get("problems") or "")[:8000], "shareable_notes": str(data.get("shareable_notes") or "")[:8000], "shareable_approved": approved}
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
                raise ValueError("活动不存在。")
            db.execute("INSERT INTO retrospectives VALUES (?,?,?) ON CONFLICT(offer_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (offer_id, dump(payload), now()))
        return self.retrospective(offer_id)

    def save_ipo_cutoff(self, identity, data):
        from storage import official_url, timestamp, dump, now, PLATFORM_DOMAINS
        platform = data.get("platform")
        if platform not in PLATFORM_DOMAINS:
            raise ValueError("请选择平台。")
        deadline = timestamp(data.get("deadline"))
        source = official_url(platform, data.get("source_url"))
        verified = data.get("verified", False)
        if not deadline or not source or type(verified) is not bool:
            raise ValueError("券商截止时间须包含时区，并附该平台官方来源及核实状态。")
        payload = {"platform": platform, "deadline": deadline, "source_url": source, "verified": verified, "note": str(data.get("note") or "")[:2500]}
        with self.connect(write=True) as db:
            if not db.execute("SELECT 1 FROM ipos WHERE identity=?", (identity,)).fetchone():
                raise ValueError("新股记录不存在。")
            db.execute("INSERT INTO ipo_cutoffs VALUES (?,?,?,?) ON CONFLICT(identity,platform) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (identity, platform, dump(payload), now()))
        return payload
