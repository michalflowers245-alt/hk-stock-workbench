"""Persistent quote history, campaign revisions, and the personal reward ledger."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from operations import OperationsMixin, migrate, money

HK = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc
SEED_WATCHLIST = (
    ("HK.800000", "恒生指数"),
    ("HK.800700", "恒生科技指数"),
    ("HK.800100", "恒生中国企业指数"),
    ("HK.00700", "腾讯控股"),
    ("HK.09988", "阿里巴巴"),
    ("HK.01810", "小米集团"),
    ("HK.03690", "美团"),
)
FACTS = ("eligibility", "capital", "holding", "fees", "reward", "payout")
KINDS = ("cash", "fees", "voucher", "pending_cash", "pending_voucher", "trading_pnl", "fx_pnl")
QUOTE_FIELDS = ("last_price", "prev_close", "open", "high", "low", "volume", "turnover")
REVIEW_STATES = ("pending", "eligible", "ineligible")
PLATFORM_DOMAINS = {
    "za": ("bank.za.group", "app.za.group", "cdn.zaticdn.com", "cdn.za.group"),
    "welab": ("welab.bank",),
    "eddid": ("eddid.com.hk", "eddidone.com"),
    "fafa": ("fafa.hk",),
    "phillip": ("poems.com.hk",),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quote_latest (
    symbol TEXT PRIMARY KEY, quote_at TEXT NOT NULL, captured_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_history (
    symbol TEXT NOT NULL, source TEXT NOT NULL, minute TEXT NOT NULL,
    quote_at TEXT NOT NULL, captured_at TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (symbol, source, minute)
);
CREATE INDEX IF NOT EXISTS quote_history_source_time ON quote_history(symbol,source,quote_at);
CREATE TABLE IF NOT EXISTS service_state (
    name TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_checks (
    scope TEXT NOT NULL, url TEXT NOT NULL, checked_at TEXT NOT NULL, last_success TEXT,
    ok INTEGER NOT NULL, error TEXT, coverage TEXT, PRIMARY KEY (scope, url)
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, offer_key TEXT NOT NULL,
    seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, followed INTEGER NOT NULL DEFAULT 0,
    review_state TEXT NOT NULL DEFAULT 'pending', review TEXT NOT NULL DEFAULT '{}',
    deadline_override TEXT, version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (platform, offer_key)
);
CREATE TABLE IF NOT EXISTS offer_versions (
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, created_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY (offer_id, version)
);
CREATE TABLE IF NOT EXISTS ipos (
    identity TEXT PRIMARY KEY, listing_date TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY, offer_id INTEGER REFERENCES offers(id) ON DELETE SET NULL,
    kind TEXT NOT NULL, cents INTEGER NOT NULL, note TEXT NOT NULL, occurred_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL, due_at TEXT NOT NULL, expires_at TEXT, sent_at TEXT, cancelled_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, offer_id INTEGER,
    expected_deadline TEXT
);
CREATE TABLE IF NOT EXISTS refresh_queue (scope TEXT PRIMARY KEY, requested_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_attempts (
    offer_id INTEGER NOT NULL, version INTEGER NOT NULL, day TEXT NOT NULL,
    PRIMARY KEY (offer_id, version, day)
);
CREATE TABLE IF NOT EXISTS ai_excerpts (
    offer_id INTEGER NOT NULL, version INTEGER NOT NULL, facts TEXT NOT NULL,
    PRIMARY KEY (offer_id, version)
);
"""

def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed.astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        pass
    return None


def calendar_date(value):
    if not value:
        return None
    try:
        raw = str(value)
        parsed = date.fromisoformat(raw)
        return raw if raw == parsed.isoformat() else None
    except ValueError:
        return None


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def clean_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def official_url(platform, value, *, remove_query=False):
    if not isinstance(value, str) or len(value) > 1800 or re.search(r"[\x00-\x20\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    domains = PLATFORM_DOMAINS.get(platform, ())
    if (parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443)
            or not any(host == suffix or host.endswith("." + suffix) for suffix in domains)):
        return ""
    if remove_query:
        return parsed._replace(query="", fragment="").geturl()
    return parsed.geturl()


def public_link(value):
    if not isinstance(value, str) or len(value) > 1800 or re.search(r"[\x00-\x20\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return parsed.geturl()
    except ValueError:
        pass
    return ""


def normalize_symbol(value):
    match = re.fullmatch(r"(?:HK\.)?(\d{1,6})(?:\.HK)?", str(value or "").strip().upper())
    if not match or int(match[1]) == 0:
        raise ValueError("请输入有效港股代码，例如 00700 或 HK.00700。")
    return "HK." + match[1].zfill(5)


def as_cents(value):
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number <= Decimal("100000000") and number == number.quantize(Decimal("0.01")):
            return int(number * 100)
    except (InvalidOperation, ValueError):
        pass
    raise ValueError("请输入大于 0 的港元金额，最多保留两位小数。")


class Store(OperationsMixin):
    def __init__(self, data_dir):
        self.root = Path(data_dir).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "workbench.sqlite3"
        with (self.root / ".setup.lock").open("a") as setup:
            fcntl.flock(setup, fcntl.LOCK_EX)
            if self.path.exists():
                with self.connect() as previous:
                    version = previous.execute("PRAGMA user_version").fetchone()[0]
                if version < 4:
                    self.backup(self.root / "backups" / ("pre-upgrade-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"))
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
                migrate(db)
                if db.execute("INSERT OR IGNORE INTO settings VALUES ('watchlist_seeded','1')").rowcount:
                    db.executemany("INSERT OR IGNORE INTO watchlist VALUES (?,?)", SEED_WATCHLIST)

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def backup(self, output):
        destination = Path(output).expanduser().resolve()
        if destination == self.path.resolve():
            raise ValueError("请选择与运行数据库不同的备份路径。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        destination.chmod(0o600)

    def status(self, name, status, detail):
        with self.connect() as db:
            db.execute(
                "INSERT INTO service_state VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at",
                (name, status, str(detail)[:340], now()),
            )

    def watchlist(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM watchlist ORDER BY rowid")]

    def set_symbol(self, symbol, name):
        symbol = normalize_symbol(symbol)
        name = str(name or "").strip()[:60] or symbol
        with self.connect(write=True) as db:
            existing = db.execute("SELECT 1 FROM watchlist WHERE symbol=?", (symbol,)).fetchone()
            total = db.execute("SELECT count(*) FROM watchlist").fetchone()[0]
            if not existing and total >= 30:
                raise ValueError("自选列表最多 30 个标的。")
            db.execute("INSERT INTO watchlist VALUES (?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name",
                       (symbol, name))
        return {"symbol": symbol, "name": name}

    def remove_symbol(self, symbol):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM watchlist WHERE symbol=?", (normalize_symbol(symbol),)).rowcount)

    def save_quote(self, record):
        try:
            symbol = normalize_symbol(record.get("symbol"))
        except ValueError:
            return
        quote_at = timestamp(record.get("quote_at"))
        captured_at = timestamp(record.get("captured_at")) or now()
        if not quote_at or record.get("last_price") is None:
            return
        payload = {key: record.get(key) for key in QUOTE_FIELDS}
        payload.update(symbol=symbol, quote_at=quote_at, captured_at=captured_at,
                       source=record.get("source") or "富途 OpenAPI", status=record.get("status") or "unavailable")
        if payload["status"] == "public_snapshot":
            payload.update(source_url=public_link(record.get("source_url")),
                           snapshot_note=clean_text(record.get("snapshot_note"))[:300])
        last = payload["last_price"]
        previous = payload["prev_close"]
        payload["change_percent"] = round((last / previous - 1) * 100, 4) if previous and last is not None else None
        minute = captured_at[:16] + ":00+00:00"
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO quote_latest VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "quote_at=excluded.quote_at,captured_at=excluded.captured_at,data=excluded.data "
                "WHERE excluded.quote_at >= quote_latest.quote_at",
                (symbol, quote_at, captured_at, dump(payload)),
            )
            already_sampled = payload["status"] == "public_snapshot" and db.execute(
                "SELECT 1 FROM quote_history WHERE symbol=? AND source=? AND quote_at=? LIMIT 1",
                (symbol, payload["source"], quote_at),
            ).fetchone()
            if payload["status"] in ("live", "delayed", "public_snapshot") and not already_sampled:
                db.execute(
                    "INSERT INTO quote_history VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(symbol,source,minute) DO UPDATE SET quote_at=excluded.quote_at,"
                    "captured_at=excluded.captured_at,data=excluded.data "
                    "WHERE excluded.quote_at >= quote_history.quote_at AND excluded.captured_at >= quote_history.captured_at",
                    (symbol, payload["source"], minute, quote_at, captured_at, dump(payload)),
                )

    def history(self, symbol=None, days=1):
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        where = "WHERE h.minute>=?"
        params = [cutoff]
        if symbol:
            where += " AND h.symbol=?"
            params.append(normalize_symbol(symbol))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT h.data, w.name FROM quote_history h LEFT JOIN watchlist w ON h.symbol=w.symbol "
                f"{where} ORDER BY h.minute,h.symbol", params
            )
            return [json.loads(row["data"]) | {"name": row["name"]} for row in rows]

    def quotes_dashboard(self, service):
        instant = datetime.now(UTC)
        with self.connect() as db:
            rows = list(db.execute(
                "SELECT w.*, q.data FROM watchlist w LEFT JOIN quote_latest q ON w.symbol=q.symbol ORDER BY w.rowid"
            ))
        quotes = []
        for row in rows:
            record = json.loads(row["data"] or "{}")
            state = service["status"]
            if state == "public_snapshot":
                if not record or record.get("status") != "public_snapshot":
                    state = "unavailable"
                elif (instant - datetime.fromisoformat(record["captured_at"])).total_seconds() > 1800:
                    state = "stale"
            if state in ("live", "delayed"):
                if not record:
                    state = "unavailable"
                elif (instant - datetime.fromisoformat(record["captured_at"])).total_seconds() > 120:
                    state = "stale"
                else:
                    state = record["status"] if state == "live" or record["status"] != "live" else "stale"
            record.update(symbol=row["symbol"], name=row["name"], status=state)
            quotes.append(record)
        return quotes

    def record_checks(self, scope, checks):
        with self.connect() as db:
            for check in checks:
                url = public_link(check.get("url"))
                if not url:
                    continue
                mark = now()
                ok = bool(check.get("ok"))
                db.execute(
                    "INSERT INTO source_checks VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(scope,url) DO UPDATE SET checked_at=excluded.checked_at,"
                    "last_success=coalesce(excluded.last_success,source_checks.last_success),"
                    "ok=excluded.ok,error=excluded.error,coverage=excluded.coverage",
                    (scope, url, mark, mark if ok else None, ok, check.get("error"), check.get("coverage")),
                )

    def source_checks(self, scope):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM source_checks WHERE scope=? ORDER BY checked_at DESC,url", (scope,)
            )]

    def source_status(self, platforms):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM source_checks ORDER BY scope,checked_at DESC,url"
            )]
        results = []
        for scope, meta in platforms.items():
            matches = [
                row for row in rows if row["scope"] == scope
                and (not row["ok"] or row["coverage"] not in ("offer_detail", "terms", "ipo_document"))
            ]
            if not matches:
                matches = [{"scope": scope, "url": meta["url"], "checked_at": None,
                            "last_success": None, "ok": None, "error": None, "coverage": "等待首次检查"}]
            for row in matches:
                results.append(row | {"platform": scope, "name": meta["name"]})
        return results

    def baseline_earned(self, scope):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM settings WHERE key=?", ("baseline:" + scope,)).fetchone())

    def set_baseline(self, scope):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", ("baseline:" + scope, "1"))

    def save_offer(self, item, *, notify=False, manual=False):
        platform = item.get("platform")
        if platform not in PLATFORM_DOMAINS:
            raise ValueError("请选择五家平台之一。")
        title = clean_text(item.get("title"))[:180]
        if not title:
            raise ValueError("请填写活动名称。")
        url = official_url(platform, item.get("url"), remove_query=manual)
        if item.get("url") and not url:
            raise ValueError("请输入该平台的 HTTPS 官方链接。")
        incoming_key = str(item.get("key") or url or title).strip()[:1800]
        offer_key = ("manual:" if manual else "official:") + incoming_key
        deadline = timestamp(item.get("deadline"))
        ends_on = calendar_date(item.get("ends_on"))
        if manual and item.get("deadline") and not deadline:
            raise ValueError("截止时间需要包含时区。")
        if manual and item.get("ends_on") and not ends_on:
            raise ValueError("截止日格式无效。")
        text = str(item.get("text") or "").replace("\x00", "").strip()[:36000]
        facts = item.get("facts") or {}
        evidence = item.get("evidence") or {}
        payload = {
            "title": title, "url": url, "text": text, "facts": facts, "evidence": evidence,
            "source_url": official_url(platform, item.get("source_url"), remove_query=manual) or url,
            "starts_at": timestamp(item.get("starts_at")) or calendar_date(item.get("starts_at")),
            "ends_on": ends_on, "deadline": deadline, "source_kind": "manual" if manual else item.get("source_kind", "official"),
            "terms_urls": [] if manual else [link for raw in item.get("terms_urls", []) if (link := official_url(platform, raw))],
            "terms_hashes": [] if manual else sorted(item.get("terms_hashes", [])),
        }
        signature = {key: clean_text(payload[key]) for key in ("title", "starts_at", "ends_on", "deadline")}
        signature["terms"] = clean_text(text if manual else item.get("revision_text") or text)
        signature["documents"] = payload["terms_hashes"]
        fingerprint = sha256(dump(signature).encode()).hexdigest()
        mark = now()
        with self.connect(write=True) as db:
            current = db.execute(
                "SELECT o.*,v.fingerprint,v.payload FROM offers o JOIN offer_versions v "
                "ON v.offer_id=o.id AND v.version=o.version WHERE o.platform=? AND o.offer_key=?",
                (platform, offer_key),
            ).fetchone()
            if current:
                old_terms = json.loads(current["payload"]).get("terms_hashes", [])
                missing_terms = old_terms and (
                    item.get("terms_complete") is False
                    or (payload["terms_urls"] and len(payload["terms_hashes"]) < len(old_terms))
                )
                if current["fingerprint"] == fingerprint or missing_terms:
                    return current["id"], "unchanged"
            if current:
                offer_id, version = current["id"], current["version"] + 1
                db.execute("INSERT INTO review_history(offer_id,version,review_state,review,deadline_override,created_at,reason) VALUES (?,?,?,?,?,?,?)",
                           (offer_id, current["version"], current["review_state"], current["review"], current["deadline_override"], mark, "official_change"))
                db.execute("UPDATE offers SET version=?,updated_at=?,review_state='pending',review_stale=1 WHERE id=?", (version, mark, offer_id))
                outcome = "changed"
            else:
                offer_id = db.execute("INSERT INTO offers(platform,offer_key,seen_at,updated_at) VALUES (?,?,?,?)",
                                      (platform, offer_key, mark, mark)).lastrowid
                version, outcome = 1, "new"
            db.execute("INSERT INTO offer_versions VALUES (?,?,?,?,?)", (offer_id, version, mark, fingerprint, dump(payload)))
            if notify and not manual and not self.is_expired(payload):
                deadline_label = self.date_label(deadline, ends_on)
                safe_url = official_url(platform, url, remove_query=True)
                label = "新活动线索" if outcome == "new" else "活动条款有变动"
                body = f"{title}\n{deadline_label}\n\n官方来源：{safe_url}\n\n请打开港股工作台核对适用条件。"
                expires = deadline or (datetime.now(UTC) + timedelta(days=3)).isoformat(timespec="seconds")
                self.add_event(db, f"offer:{offer_id}:{version}", f"【港股工作台】{label} · {title}", body, expires=expires)
            return offer_id, outcome

    @staticmethod
    def is_expired(item):
        if item.get("deadline"):
            return item["deadline"] < now()
        return bool(item.get("ends_on") and item["ends_on"] < datetime.now(HK).date().isoformat())

    @staticmethod
    def date_label(deadline, ends_on):
        if deadline:
            return "截止：" + datetime.fromisoformat(deadline).astimezone(HK).strftime("%Y-%m-%d %H:%M 香港时间")
        if ends_on:
            return "截止日：" + ends_on + "（具体时点待核）"
        return "截止时间待核"

    @staticmethod
    def unpack_offer(row, platforms):
        item = json.loads(row["payload"])
        item.update({key: row[key] for key in ("id", "platform", "seen_at", "updated_at", "version", "review_state")})
        item["followed"] = bool(row["followed"])
        item["review_stale"] = bool(row["review_stale"])
        item["reviewed_version"] = row["reviewed_version"]
        item["deadline_override"] = row["deadline_override"]
        item["platform_name"] = platforms[item["platform"]]["name"]
        item["review"] = json.loads(row["review"])
        item["official_deadline"] = item.get("deadline") if item["source_kind"] != "manual" else None
        if row["deadline_override"]:
            item["deadline"] = row["deadline_override"]
        item["expired"] = Store.is_expired(item)
        return item

    def offers(self, platforms, *, offer_id=None, detail=False):
        sql = ("SELECT o.*,v.payload FROM offers o JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version")
        with self.connect() as db:
            if offer_id is None:
                rows = db.execute(sql + " ORDER BY o.followed DESC,o.updated_at DESC,o.id DESC")
                items = []
                for row in rows:
                    item = self.unpack_offer(row, platforms)
                    for field in ("text", "review", "evidence"):
                        item.pop(field, None)
                    item["assessment"] = self.assess_offer(item["id"])
                    items.append(item)
                return sorted(items, key=lambda item: (-item["assessment"]["priority"], -item["id"]))
            row = db.execute(sql + " WHERE o.id=?", (offer_id,)).fetchone()
            if not row:
                return None
            item = self.unpack_offer(row, platforms)
            item["assessment"] = self.assess_offer(offer_id)
            if detail:
                item["requirements"] = self.requirements(offer_id)
                item["tasks"] = self.tasks(offer_id)
                item["retrospective"] = self.retrospective(offer_id)
                item["economics"] = self.offer_economics(offer_id)
                item["review_history"] = [dict(entry) | {"review": json.loads(entry["review"])} for entry in db.execute("SELECT * FROM review_history WHERE offer_id=? ORDER BY id DESC", (offer_id,))]
                history = db.execute("SELECT * FROM offer_versions WHERE offer_id=? ORDER BY version DESC", (offer_id,))
                item["history"] = []
                for past in history:
                    old = json.loads(past["payload"])
                    item["history"].append({key: old[key] for key in ("title", "text", "deadline", "ends_on")} | {
                        "version": past["version"], "created_at": past["created_at"]
                    })
            return item

    def patch_offer(self, offer_id, data):
        with self.connect(write=True) as db:
            current = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if not current:
                return False
            followed = data.get("followed", bool(current["followed"]))
            state = data.get("review_state", current["review_state"])
            if type(followed) is not bool or state not in REVIEW_STATES:
                raise ValueError("活动跟进状态无效。")
            review = json.loads(current["review"])
            if "review" in data:
                if not isinstance(data["review"], dict):
                    raise ValueError("复核内容格式无效。")
                for key in (*FACTS, "note"):
                    if key in data["review"]:
                        review[key] = str(data["review"][key] or "").strip()[:2500]
            override = current["deadline_override"]
            if "deadline" in data:
                override = timestamp(data["deadline"])
                if data["deadline"] and override is None:
                    raise ValueError("截止时间需要包含时区。")
            confirms = "review_state" in data or data.get("review_confirmed") is True
            supplied_version = data.get("reviewed_version", current["version"])
            if confirms and ((current["review_stale"] and "reviewed_version" not in data) or supplied_version != current["version"]):
                raise ValueError("官方条款刚有更新，请刷新并核对最新版本后再保存复核。")
            db.execute("INSERT INTO review_history(offer_id,version,review_state,review,deadline_override,created_at,reason) VALUES (?,?,?,?,?,?,?)",
                       (offer_id, current["version"], current["review_state"], current["review"], current["deadline_override"], now(), "user_edit"))
            db.execute("UPDATE offers SET followed=?,review_state=?,review=?,deadline_override=?,review_stale=?,reviewed_version=? WHERE id=?",
                       (followed, state, dump(review), override, 0 if confirms else current["review_stale"],
                        current["version"] if confirms else current["reviewed_version"], offer_id))
        return True

    def save_ipos(self, items):
        with self.connect() as db:
            for item in items:
                if not item.get("name"):
                    continue
                identity = str(item.get("key") or ":".join(
                    str(value or "") for value in (item.get("board"), item.get("code"), clean_text(item["name"]))
                ))
                payload = dict(item)
                for key in ("source_url", "prospectus_url", "allotment_url", "announcement_url", "deadline_source_url"):
                    payload[key] = public_link(payload.get(key))
                payload["deadline"] = timestamp(payload.get("deadline"))
                payload["listing_date"] = calendar_date(payload.get("listing_date"))
                previous = db.execute("SELECT payload FROM ipos WHERE identity=?", (identity,)).fetchone()
                if previous:
                    old = json.loads(previous["payload"])
                    for key in ("prospectus_url", "allotment_url", "deadline", "listing_date", "deadline_evidence", "deadline_source_url"):
                        if not payload.get(key) and old.get(key):
                            payload[key] = old[key]
                db.execute(
                    "INSERT INTO ipos VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET "
                    "listing_date=excluded.listing_date,payload=excluded.payload",
                    (identity, payload["listing_date"], dump(payload)),
                )

    def ipos(self):
        with self.connect() as db:
            items = []
            for row in db.execute("SELECT * FROM ipos ORDER BY listing_date IS NULL,listing_date DESC,identity LIMIT 200"):
                item = json.loads(row["payload"]) | {"identity": row["identity"]}
                item["broker_cutoffs"] = [json.loads(cutoff["payload"]) | {"updated_at": cutoff["updated_at"]} for cutoff in db.execute("SELECT * FROM ipo_cutoffs WHERE identity=?", (row["identity"],))]
                items.append(item)
            return items

    @staticmethod
    def totals(db):
        cents = {kind: 0 for kind in KINDS}
        expected = {"pending_cash": 0, "pending_voucher": 0}
        cents.update({"overdue_pending_cash": 0, "overdue_pending_voucher": 0, "redeemed_voucher": 0})
        today = datetime.now(HK).date().isoformat()
        for row in db.execute("SELECT * FROM ledger WHERE voided_at IS NULL"):
            if row["kind"] in expected:
                settled = db.execute("SELECT coalesce(sum(cents),0) FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL", (row["id"],)).fetchone()[0]
                remaining = max(0, row["cents"] - settled)
                pending_hkd = int((Decimal(remaining) * Decimal(row["rate_hkd"])).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
                cents[row["kind"]] += pending_hkd
                expected[row["kind"]] += row["hkd_cents"]
                if row["expected_on"] and row["expected_on"] < today:
                    cents["overdue_" + row["kind"]] += pending_hkd
            else:
                cents[row["kind"]] += row["hkd_cents"]
                if row["kind"] == "voucher":
                    cents["redeemed_voucher"] += int((Decimal(row["redeemed_cents"]) * Decimal(row["rate_hkd"])).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
        cents["net"] = cents["cash"] - cents["fees"]
        cents["combined_net"] = cents["net"] + cents["trading_pnl"] + cents["fx_pnl"]
        cents["expected_cash"] = expected["pending_cash"]
        cents["expected_voucher"] = expected["pending_voucher"]
        return {key: value / 100 for key, value in cents.items()}

    def ledger(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT l.*,v.payload AS offer FROM ledger l LEFT JOIN offers o ON l.offer_id=o.id "
                "LEFT JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version WHERE l.voided_at IS NULL ORDER BY l.occurred_on DESC,l.id DESC"
            ).fetchall()
            entries = []
            for row in rows:
                item = {key: row[key] for key in ("id", "offer_id", "kind", "note", "occurred_on", "currency", "expected_entry_id", "idempotency_key", "created_at", "expected_on", "voucher_scope")}
                item.update({"amount": row["cents"] / 100, "amount_hkd": row["hkd_cents"] / 100, "rate_hkd": float(row["rate_hkd"]), "offer_title": json.loads(row["offer"] or "{}").get("title"), "redeemed_value": row["redeemed_cents"] / 100})
                if row["kind"] == "voucher":
                    item["voucher_redemptions"] = [dict(event) | {"old_value": event["old_cents"] / 100, "new_value": event["new_cents"] / 100} for event in db.execute("SELECT * FROM voucher_redemptions WHERE ledger_id=? ORDER BY id", (row["id"],))]
                if row["kind"].startswith("pending_"):
                    settled = db.execute("SELECT coalesce(sum(cents),0) FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL", (row["id"],)).fetchone()[0]
                    item["overdue"] = bool(row["expected_on"] and row["expected_on"] < datetime.now(HK).date().isoformat() and settled < row["cents"])
                    item["due_status"] = "settled" if settled >= row["cents"] else "overdue" if item["overdue"] else "pending"
                    item.update({"settled_amount": settled / 100, "remaining_amount": (row["cents"] - settled) / 100, "settlement_ids": [r[0] for r in db.execute("SELECT id FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL ORDER BY id", (row["id"],))]})
                entries.append(item)
            return {"entries": entries, "totals": self.totals(db), "base_currency": "HKD", "platform_performance": self.performance()}

    @staticmethod
    def ledger_extras(data, kind, amount):
        expected = calendar_date(data.get("expected_on"))
        if data.get("expected_on") and not expected:
            raise ValueError("预期到账日格式无效。")
        redeemed, _, _, _ = money({"amount": data.get("redeemed_value") or 0}, signed=True)
        if redeemed < 0 or redeemed > max(0, amount) or (redeemed and kind != "voucher"):
            raise ValueError("实际抵扣值只适用于已到账券，且须介于 0 与券面值之间。")
        return {"expected_on": expected if kind.startswith("pending_") else None,
                "voucher_scope": str(data.get("voucher_scope") or "")[:1500] if "voucher" in kind else "", "redeemed_cents": redeemed}

    def performance(self):
        result = []
        with self.connect() as db:
            for platform in PLATFORM_DOMAINS:
                totals = {key: 0 for key in ("cash", "fees", "voucher", "pending_cash", "pending_voucher", "trading_pnl", "fx_pnl")}
                income_offers = set()
                rows = db.execute("SELECT l.* FROM ledger l JOIN offers o ON o.id=l.offer_id WHERE o.platform=? AND l.voided_at IS NULL", (platform,))
                for row in rows:
                    if row["kind"] in ("cash", "fees"):
                        income_offers.add(row["offer_id"])
                    if row["kind"].startswith("pending_"):
                        settled = db.execute("SELECT coalesce(sum(cents),0) FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL", (row["id"],)).fetchone()[0]
                        totals[row["kind"]] += int((Decimal(row["cents"] - settled) * Decimal(row["rate_hkd"])).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
                    else:
                        totals[row["kind"]] += row["hkd_cents"]
                timed = {r["offer_id"]: json.loads(r["payload"]).get("actual_minutes", 0) for r in db.execute("SELECT r.offer_id,r.payload FROM retrospectives r JOIN offers o ON o.id=r.offer_id WHERE o.platform=?", (platform,))}
                minutes = sum(timed.get(key, 0) for key in income_offers)
                covered = income_offers & timed.keys()
                complete = bool(income_offers) and len(covered) == len(income_offers)
                totals["net"] = totals["cash"] - totals["fees"]
                totals = {key: value / 100 for key, value in totals.items()}
                result.append(totals | {"platform": platform, "actual_minutes": minutes,
                    "total_recorded_minutes": sum(timed.values()), "income_offer_count": len(income_offers), "timed_income_offer_count": len(covered),
                    "time_coverage_complete": complete,
                    "cash_per_hour": round(totals["net"] * 60 / minutes, 2) if complete and minutes else None})
        return result

    def offer_economics(self, offer_id):
        """Recorded one-off cash reward economics; never treats coupons or trades as cash reward."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
                return None
            entries = [dict(row) for row in db.execute("SELECT * FROM ledger WHERE offer_id=? AND voided_at IS NULL", (offer_id,))]
            reservations = [json.loads(row[0]) for row in db.execute("SELECT payload FROM capital_reservations WHERE offer_id=?", (offer_id,))]
            retro = db.execute("SELECT payload FROM retrospectives WHERE offer_id=?", (offer_id,)).fetchone()
        sums = {key: 0 for key in ("cash", "fees", "pending_cash", "voucher", "pending_voucher")}
        invalid_kinds = set()
        reasons = []
        has_pending = False
        has_cash_result = False
        def convert(cents, currency, rate):
            if type(cents) is not int or currency not in ("HKD", "CNY", "USD") or rate is None:
                raise ValueError
            number = Decimal(str(rate))
            if not number.is_finite() or not 0 < number <= 100000 or currency == "HKD" and number != 1:
                raise ValueError
            return int((Decimal(cents) * number).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
        for entry in entries:
            kind = entry["kind"]
            if kind not in sums:
                continue
            try:
                converted = convert(entry["cents"], entry["currency"], entry["rate_hkd"])
                if entry["hkd_cents"] != converted:
                    raise ValueError
                if kind.startswith("pending_"):
                    actual_cents = sum(row["cents"] for row in entries if row["expected_entry_id"] == entry["id"])
                    remainder = entry["cents"] - actual_cents
                    if remainder < 0:
                        raise ValueError
                    has_pending = has_pending or remainder > 0
                    converted = convert(remainder, entry["currency"], entry["rate_hkd"])
                elif kind in ("cash", "fees"):
                    has_cash_result = True
                sums[kind] += converted
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                invalid_kinds.add(kind)
        if invalid_kinds:
            reasons.append("收益记录存在缺失或不一致的金额、币种或汇率")
        if not has_cash_result:
            reasons.append("尚未登记本活动的实际现金奖励或费用")
        if has_pending:
            reasons.append("仍有待到账现金或券")
        if not reservations:
            reasons.append("尚未登记实际资金占用")
        elif any(row.get("status") != "released" for row in reservations):
            reasons.append("仍有计划中或占用中的资金，尚未全部释放")
        today = datetime.now(HK).date().isoformat()
        capital_days_cents = 0
        movements = {}
        capital_valid = True
        future_release = False
        for reservation in reservations:
            try:
                raw_amount = Decimal(str(reservation.get("amount")))
                if not raw_amount.is_finite() or raw_amount < 0 or raw_amount != raw_amount.quantize(Decimal(".01")):
                    raise ValueError
                cents = convert(int(raw_amount * 100), reservation.get("currency"), reservation.get("rate_hkd"))
                if Decimal(str(reservation.get("amount_hkd"))) != Decimal(cents) / 100:
                    raise ValueError
                start = calendar_date(reservation.get("starts_on"))
                end = calendar_date(reservation.get("ends_on"))
                if not start or not end or end < start:
                    raise ValueError
                begins, ends = date.fromisoformat(start), date.fromisoformat(end)
                after = (ends + timedelta(days=1)).isoformat()
                capital_days_cents += cents * ((ends - begins).days + 1)
                movements[start] = movements.get(start, 0) + cents
                movements[after] = movements.get(after, 0) - cents
                future_release = future_release or (reservation.get("status") == "released" and end > today)
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                capital_valid = False
        if not capital_valid:
            reasons.append("资金占用的金额、汇率或日期不完整")
        if future_release:
            reasons.append("标记释放的资金仍含未来结束日，暂不视为已完成")
        if capital_valid and reservations and capital_days_cents <= 0:
            reasons.append("资金占用为 0，无法计算年化")
        peak = running = 0
        for day in sorted(movements):
            running += movements[day]
            peak = max(peak, running)
        net_cents = sums["cash"] - sums["fees"]
        annualized = None
        if not reasons and capital_days_cents > 0:
            annualized = float((Decimal(net_cents) * 36500 / Decimal(capital_days_cents)).quantize(Decimal(".01"), rounding="ROUND_HALF_UP"))
        explanation = "仅按已登记的一次性活动测算，不代表可重复收益；现金奖励减实际费用，券、交易损益和汇兑损益均不计入。资金日含起止当日，简单年化＝净现金收益×365÷资金日×100%。"
        if reasons:
            explanation = "暂不计算年化：" + "；".join(reasons) + "。" + explanation
        return {**{key: None if key in invalid_kinds else value / 100 for key, value in sums.items()},
                "net": None if {"cash", "fees"} & invalid_kinds else net_cents / 100,
                "actual_minutes": json.loads(retro[0]).get("actual_minutes") if retro else None,
                "capital_peak_hkd": peak / 100 if capital_valid else None,
                "capital_days_hkd": capital_days_cents / 100 if capital_valid else None,
                "simple_annualized_percent": annualized, "annualized_note": explanation}

    @staticmethod
    def submission(data, payload):
        token = data.get("idempotency_key")
        if token is not None and (not isinstance(token, str) or not re.fullmatch(r"[a-zA-Z0-9_:.\-]{8,160}", token)):
            raise ValueError("提交编号无效，请刷新页面重试。")
        return token, sha256(dump(payload).encode()).hexdigest()

    @staticmethod
    def _existing_submission(db, token, fingerprint):
        if not token:
            return None
        row = db.execute("SELECT id,payload_hash,voided_at FROM ledger WHERE idempotency_key=?", (token,)).fetchone()
        if row and (row["payload_hash"] != fingerprint or row["voided_at"]):
            raise ValueError("该提交编号已用于其他或已撤销记录，请重新提交。")
        return row["id"] if row else None

    def add_entry(self, data):
        kind = data.get("kind")
        if kind not in KINDS:
            raise ValueError("请选择有效的记账类型。")
        if data.get("expected_entry_id"):
            raise ValueError("关联预计奖励请使用分次到账入口。")
        amount, currency, rate, hkd = money(data, signed=kind in ("trading_pnl", "fx_pnl"))
        day = calendar_date(data.get("occurred_on"))
        if not day:
            raise ValueError("请填写有效的记账日期。")
        offer_id = data.get("offer_id") or None
        note = str(data.get("note") or "")[:1200]
        extra = self.ledger_extras(data, kind, amount)
        payload = extra | {"kind": kind, "cents": amount, "currency": currency, "rate_hkd": rate, "day": day, "offer_id": offer_id, "note": note}
        token, fingerprint = self.submission(data, payload)
        with self.connect(write=True) as db:
            existing = self._existing_submission(db, token, fingerprint)
            if existing:
                return existing
            if offer_id and not db.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone():
                raise ValueError("关联活动不存在。")
            result = db.execute("INSERT INTO ledger(offer_id,kind,cents,note,occurred_on,currency,rate_hkd,hkd_cents,idempotency_key,payload_hash,created_at,expected_on,voucher_scope,redeemed_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (offer_id, kind, amount, note, day, currency, rate, hkd, token, fingerprint, now(),extra["expected_on"],extra["voucher_scope"],extra["redeemed_cents"]))
            return result.lastrowid

    def settle_entry(self, item_id, data):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM ledger WHERE id=? AND voided_at IS NULL", (item_id,)).fetchone()
            if not row:
                return False
            kind = data.get("kind")
            if row["kind"] not in ("pending_cash", "pending_voucher") or kind != row["kind"].removeprefix("pending_"):
                raise ValueError("只能按原预计奖励类型登记实际到账。")
            day = calendar_date(data.get("occurred_on") or datetime.now(HK).date().isoformat())
            if not day:
                raise ValueError("到账日期无效。")
            if data.get("currency", row["currency"]) != row["currency"]:
                raise ValueError("分次到账须与预计奖励币种一致；汇兑损益请单独记账。")
            note = str(data.get("note") or row["note"] or "")[:1200]
            # Hash the submitted intent, including omitted amount, before computing the remaining balance.
            # A retried full settlement therefore returns the first result even after the balance is zero.
            intent = {"expected_entry_id": item_id, "kind": kind, "amount": data.get("amount"), "currency": row["currency"],
                      "rate_hkd": str(data.get("rate_hkd", row["rate_hkd"])), "day": day, "note": note,
                      "voucher_scope": data.get("voucher_scope", row["voucher_scope"]), "redeemed_value": data.get("redeemed_value", 0)}
            token, fingerprint = self.submission(data, intent)
            existing = self._existing_submission(db, token, fingerprint)
            if existing:
                return True
            settled = db.execute("SELECT coalesce(sum(cents),0) FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL", (item_id,)).fetchone()[0]
            remaining = row["cents"] - settled
            amount, currency, rate, hkd = money({"amount": data.get("amount", remaining / 100), "currency": row["currency"], "rate_hkd": data.get("rate_hkd", row["rate_hkd"])})
            extra = self.ledger_extras({"voucher_scope": row["voucher_scope"]} | data, kind, amount)
            if amount > remaining:
                raise ValueError("本次到账金额超过尚未到账的预计奖励；超出部分请单独登记并注明原因。")
            db.execute("INSERT INTO ledger(offer_id,kind,cents,note,occurred_on,currency,rate_hkd,hkd_cents,expected_entry_id,idempotency_key,payload_hash,created_at,expected_on,voucher_scope,redeemed_cents) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (row["offer_id"], kind, amount, note, day, currency, rate, hkd, item_id, token, fingerprint, now(),extra["expected_on"],extra["voucher_scope"],extra["redeemed_cents"]))
        return True

    def redeem_voucher(self, item_id, data):
        """Record the cumulative actual saving without changing the voucher's face value."""
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM ledger WHERE id=? AND voided_at IS NULL", (item_id,)).fetchone()
            if not row:
                raise ValueError("券记录不存在或已撤销。")
            if row["kind"] != "voucher":
                raise ValueError("只能更新已到账券的实际抵扣值。")
            values = {"redeemed_value": row["redeemed_cents"] / 100, "voucher_scope": row["voucher_scope"]} | data
            if "redeemed_value" in data and data["redeemed_value"] in (None, ""):
                raise ValueError("请填写累计实际抵扣值；未使用请填写 0。")
            extra = self.ledger_extras(values, "voucher", row["cents"])
            if extra["redeemed_cents"] != row["redeemed_cents"] or extra["voucher_scope"] != row["voucher_scope"]:
                db.execute("INSERT INTO voucher_redemptions(ledger_id,old_cents,new_cents,recorded_at,note) VALUES (?,?,?,?,?)",
                           (item_id, row["redeemed_cents"], extra["redeemed_cents"], now(), str(data.get("note") or "")[:1200]))
                db.execute("UPDATE ledger SET redeemed_cents=?,voucher_scope=? WHERE id=?",
                           (extra["redeemed_cents"], extra["voucher_scope"], item_id))
        return next(entry for entry in self.ledger()["entries"] if entry["id"] == item_id)

    def delete_entry(self, item_id):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM ledger WHERE id=? AND voided_at IS NULL", (item_id,)).fetchone()
            if not row:
                return False
            if db.execute("SELECT 1 FROM ledger WHERE expected_entry_id=? AND voided_at IS NULL", (item_id,)).fetchone():
                raise ValueError("该预计奖励已有实际到账记录，请先撤销关联到账；预计原记录会保留备查。")
            db.execute("UPDATE ledger SET voided_at=? WHERE id=?", (now(), item_id))
        return True

    @staticmethod
    def add_event(db, key, subject, body, *, expires=None, offer_id=None, deadline=None):
        mark = now()
        db.execute(
            "INSERT INTO outbox(event_key,subject,body,created_at,due_at,expires_at,offer_id,expected_deadline) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET cancelled_at=NULL,due_at=excluded.due_at "
            "WHERE outbox.cancelled_at IS NOT NULL AND outbox.sent_at IS NULL AND excluded.expected_deadline IS NOT NULL",
            (key, subject, body, mark, mark, expires, offer_id, deadline),
        )

    def queue_test_email(self):
        mark = datetime.now(UTC)
        key = "test:" + mark.strftime("%Y%m%d%H%M")
        expires = (mark + timedelta(hours=1)).isoformat(timespec="seconds")
        with self.connect() as db:
            self.add_event(db, key, "【港股工作台】邮件提醒测试", "这是一封港股工作台的测试邮件。", expires=expires)

    def add_deadline_notices(self, platforms):
        instant = datetime.now(UTC)
        limit = (instant + timedelta(hours=24)).isoformat(timespec="seconds")
        with self.connect(write=True) as db:
            rows = db.execute(
                "SELECT o.*,v.payload FROM offers o JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version "
                "WHERE o.followed=1 AND o.review_state!='ineligible' AND o.review_stale=0"
            )
            for row in rows:
                offer = self.unpack_offer(row, platforms)
                ends = offer["deadline"]
                if not ends or not instant.isoformat(timespec="seconds") < ends <= limit:
                    continue
                title = offer["title"]
                safe_url = official_url(offer["platform"], offer["url"], remove_query=True)
                body = title + "\n" + self.date_label(ends, None)
                if safe_url and offer["source_kind"] != "manual":
                    body += "\n\n官方来源：" + safe_url
                self.add_event(db, f"deadline:{offer['id']}:{ends}", f"【港股工作台】活动即将截止 · {title}",
                               body, expires=ends, offer_id=offer["id"], deadline=ends)

    def pending_events(self, platforms):
        mark = now()
        with self.connect(write=True) as db:
            db.execute("UPDATE outbox SET cancelled_at=? WHERE sent_at IS NULL AND cancelled_at IS NULL "
                       "AND expires_at<=?", (mark, mark))
            events = db.execute(
                "SELECT * FROM outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND due_at<=? ORDER BY id LIMIT 10",
                (mark,),
            ).fetchall()
            deliver = []
            for event in events:
                if event["expected_deadline"]:
                    row = db.execute(
                        "SELECT o.*,v.payload FROM offers o JOIN offer_versions v ON v.offer_id=o.id AND v.version=o.version "
                        "WHERE o.id=?", (event["offer_id"],)
                    ).fetchone()
                    offer = self.unpack_offer(row, platforms) if row else None
                    if (not offer or not offer["followed"] or offer["review_state"] == "ineligible" or offer["review_stale"]
                            or offer["deadline"] != event["expected_deadline"]):
                        db.execute("UPDATE outbox SET cancelled_at=? WHERE id=?", (mark, event["id"]))
                        continue
                deliver.append(dict(event))
            return deliver

    def delivery_result(self, event_id, error=None):
        with self.connect(write=True) as db:
            if error:
                attempts = db.execute("SELECT attempts FROM outbox WHERE id=?", (event_id,)).fetchone()[0] + 1
                due = (datetime.now(UTC) + timedelta(seconds=min(3600, 60 * 2 ** min(6, attempts - 1)))).isoformat(timespec="seconds")
                db.execute("UPDATE outbox SET attempts=?,due_at=?,last_error=? WHERE id=?", (attempts, due, error, event_id))
            else:
                db.execute("UPDATE outbox SET sent_at=?,last_error=NULL WHERE id=?", (now(), event_id))

    def request_refresh(self, scopes):
        with self.connect() as db:
            for scope in scopes:
                db.execute("INSERT OR IGNORE INTO refresh_queue VALUES (?,?)", (scope, now()))

    def read_refresh(self):
        with self.connect(write=True) as db:
            values = {row[0] for row in db.execute("SELECT scope FROM refresh_queue")}
            db.execute("DELETE FROM refresh_queue")
            return values

    def trim(self):
        cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("DELETE FROM quote_history WHERE minute<?", (cutoff,))

    def get_ai(self, offer):
        with self.connect() as db:
            row = db.execute("SELECT facts FROM ai_excerpts WHERE offer_id=? AND version=?",
                             (offer["id"], offer["version"])).fetchone()
        return json.loads(row[0]) if row else {}

    def reserve_ai(self, offer):
        day = datetime.now(HK).date().isoformat()
        with self.connect(write=True) as db:
            count = db.execute("SELECT count(*) FROM ai_attempts WHERE day=?", (day,)).fetchone()[0]
            if count >= 10:
                return False
            return bool(db.execute("INSERT OR IGNORE INTO ai_attempts VALUES (?,?,?)",
                                   (offer["id"], offer["version"], day)).rowcount)

    def save_ai(self, offer, facts):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO ai_excerpts VALUES (?,?,?)",
                       (offer["id"], offer["version"], dump(facts)))

    def dashboard(self, platforms):
        with self.connect() as db:
            service = {row["name"]: dict(row) for row in db.execute("SELECT * FROM service_state")}
            counts = {
                "offers": db.execute("SELECT count(*) FROM offers").fetchone()[0],
                "following": db.execute("SELECT count(*) FROM offers WHERE followed=1").fetchone()[0],
                "ipos": db.execute("SELECT count(*) FROM ipos").fetchone()[0],
            }
            ledger = self.totals(db)
        service.setdefault("quotes", {"status": "pending_auth", "detail": "等待富途行情授权。", "updated_at": None})
        service.setdefault("worker", {"status": "pending", "detail": "等待后台采集服务。", "updated_at": None})
        service.setdefault("email", {"status": "pending", "detail": "等待配置发件邮箱。", "updated_at": None})
        worker_state = service["worker"]
        if worker_state["updated_at"] and (datetime.now(UTC) - datetime.fromisoformat(worker_state["updated_at"])).total_seconds() > 90:
            service["worker"] = worker_state | {"status": "degraded", "detail": "后台采集服务心跳已中断。"}
            service["quotes"] = service["quotes"] | {"status": "stale", "detail": "采集服务心跳已中断。"}
        return {"quotes": self.quotes_dashboard(service["quotes"]), "sources": self.source_status(platforms),
                "service": service, "counts": counts, "ledger": ledger, "ipo_sources": self.source_checks("hkex"),
                "capital": self.reservations(), "operations": {"tasks": self.tasks(None)},
                "opportunities": self.offers(platforms)[:6]}
