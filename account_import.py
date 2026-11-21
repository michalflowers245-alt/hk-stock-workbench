"""Import local, user-selected account observations into a separate history."""
import csv
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import io
import json
import re

from storage import HK, calendar_date, dump, now


ACCOUNT_PLATFORMS = ("za", "welab", "eddid")
ACCOUNT_HEADERS = {
    "balances": ("日期", "项目", "币种", "金额", "来源"),
    "positions": ("日期", "标的代码", "名称", "币种", "数量", "市值", "来源"),
    "transactions": ("日期", "流水编号", "说明", "币种", "金额", "来源"),
}
MAX_BYTES = 65_000
MAX_ROWS = 500
PREVIEW_ROWS = 20
HISTORY_ROWS = 100
DECIMAL = re.compile(r"[+-]?\d{1,15}(?:\.\d{1,8})?\Z", re.ASCII)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def require_platform(platform):
    if platform not in ACCOUNT_PLATFORMS:
        raise ValueError("请选择众安银行、WeLab Bank 或艾德证券。")


def header_for(kind):
    if not isinstance(kind, str) or kind not in ACCOUNT_HEADERS:
        raise ValueError("请选择余额、持仓或流水。")
    return ACCOUNT_HEADERS[kind]


def _text(value, title, limit):
    if not value or len(value) > limit or CONTROL.search(value):
        raise ValueError(f"{title}须填写，且不可超过 {limit} 个字符或包含换行。")
    return value


def _number(value, title):
    if not DECIMAL.fullmatch(value):
        raise ValueError(f"{title}应填写十进制数字，最多 15 位整数、8 位小数。请去掉逗号和货币符号。")
    decimal = Decimal(value)
    return "0" if not decimal else format(decimal.normalize(), "f")


def _row(kind, values, today):
    fields = dict(zip(ACCOUNT_HEADERS[kind], values))
    observed_on = calendar_date(fields["日期"])
    if not observed_on or observed_on > today:
        raise ValueError("日期须为 YYYY-MM-DD，且不能晚于今天。")
    currency = fields["币种"].upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("币种须为三位字母代码，例如 HKD、USD 或 CNY。")
    row = {"currency": currency, "observed_on": observed_on, "source": _text(fields["来源"], "来源", 120)}
    if kind == "balances":
        row["label"] = _text(fields["项目"], "项目", 120)
        row["amount"] = _number(fields["金额"], "金额")
    elif kind == "positions":
        symbol = fields["标的代码"].upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-:/_]{0,47}", symbol):
            raise ValueError("标的代码须由字母、数字或 . - : / _ 组成。")
        row["symbol"] = symbol
        row["label"] = _text(fields["名称"], "名称", 120)
        row["quantity"] = _number(fields["数量"], "数量")
        row["market_value"] = _number(fields["市值"], "市值") if fields["市值"] else None
    else:
        row["reference"] = _text(fields["流水编号"], "流水编号", 96)
        row["description"] = _text(fields["说明"], "说明", 260)
        row["amount"] = _number(fields["金额"], "金额")
    return row


def _fingerprints(kind, row):
    # A changed source label by itself does not make this a distinct observation.
    identity = {key: value for key, value in row.items() if key != "source"}
    fingerprint = sha256(dump(identity).encode()).hexdigest()
    key = sha256(row["reference"].encode()).hexdigest() if kind == "transactions" else fingerprint
    return key, fingerprint


def _parse(platform, kind, text):
    require_platform(platform)
    header = header_for(kind)
    if not isinstance(text, str) or not text or len(text) > MAX_BYTES:
        raise ValueError("请选择非空 CSV，单次最多 65 KB。")
    try:
        byte_length = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("CSV 须以 UTF-8 编码保存。") from None
    if byte_length > MAX_BYTES:
        raise ValueError("CSV 单次最多 65 KB，请拆分后导入。")
    today = datetime.now(HK).date().isoformat()
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline=""), strict=True)
    rows = []
    try:
        first = next(reader, ())
        if tuple(name.strip() for name in first) != header:
            raise ValueError("CSV 表头与模板不一致，请使用工作台下载的模板。")
        for values in reader:
            values = [value.strip() for value in values]
            if not any(values):
                continue
            if len(rows) == MAX_ROWS:
                raise ValueError("单次最多导入 500 条，请拆成多个 CSV。")
            if len(values) != len(header):
                raise ValueError(f"CSV 第 {reader.line_num} 行列数与模板不符。")
            try:
                row = _row(kind, values, today)
            except ValueError as exc:
                raise ValueError(f"CSV 第 {reader.line_num} 行：{exc}") from None
            key, fingerprint = _fingerprints(kind, row)
            rows.append((reader.line_num, key, fingerprint, row))
    except csv.Error:
        raise ValueError(f"CSV 第 {reader.line_num} 行格式不正确，请检查逗号和引号。") from None
    if not rows:
        raise ValueError("CSV 只有表头或空行，请至少填写一条记录。")
    return rows


class AccountData:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS account_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    observed_on TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(platform, kind, identity)
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS account_observations_history
                    ON account_observations(platform, kind, observed_on DESC, id DESC)
            """)

    @staticmethod
    def template(kind):
        stream = io.StringIO(newline="")
        csv.writer(stream).writerow(header_for(kind))
        return "\ufeff" + stream.getvalue()

    @staticmethod
    def _classified(db, platform, kind, rows):
        known = {}
        classified = []
        for line, key, fingerprint, row in rows:
            previous = known.get(key)
            if previous is None:
                existing = db.execute(
                    "SELECT fingerprint FROM account_observations WHERE platform=? AND kind=? AND identity=?",
                    (platform, kind, key),
                ).fetchone()
                previous = existing["fingerprint"] if existing else None
            if previous is not None and previous != fingerprint:
                raise ValueError(f"CSV 第 {line} 行流水编号已对应不同内容。请核对日期、说明、币种和金额后重试。")
            classified.append((key, fingerprint, row, previous is not None))
            known[key] = fingerprint
        return classified

    def preview(self, platform, kind, text):
        rows = _parse(platform, kind, text)
        with self.store.connect() as db:
            classified = self._classified(db, platform, kind, rows)
        existing = sum(duplicate for _, _, _, duplicate in classified)
        return {
            "platform": platform, "kind": kind, "total": len(rows), "existing": existing,
            "new": len(rows) - existing,
            "rows": [row | {"imported_at": None, "duplicate": duplicate}
                     for _, _, row, duplicate in classified[:PREVIEW_ROWS]],
        }

    def import_csv(self, platform, kind, text):
        rows = _parse(platform, kind, text)
        mark = now()
        with self.store.connect(write=True) as db:
            classified = self._classified(db, platform, kind, rows)
            fresh = [(platform, kind, key, fingerprint, row["observed_on"], mark, dump(row))
                     for key, fingerprint, row, duplicate in classified if not duplicate]
            db.executemany(
                """
                INSERT INTO account_observations
                    (platform, kind, identity, fingerprint, observed_on, imported_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                fresh,
            )
        return {"platform": platform, "kind": kind, "total": len(rows), "inserted": len(fresh),
                "existing": len(rows) - len(fresh), "imported_at": mark if fresh else None}

    def list_data(self, platform):
        require_platform(platform)
        history = {}
        with self.store.connect() as db:
            for kind in ACCOUNT_HEADERS:
                rows = db.execute(
                    """
                    SELECT id, imported_at, payload FROM account_observations
                    WHERE platform=? AND kind=? ORDER BY observed_on DESC, id DESC LIMIT ?
                    """,
                    (platform, kind, HISTORY_ROWS),
                )
                history[kind] = [json.loads(row["payload"]) | {"id": row["id"], "imported_at": row["imported_at"]}
                                 for row in rows]
            last = db.execute("SELECT MAX(imported_at) FROM account_observations WHERE platform=?", (platform,)).fetchone()[0]
        return {"platform": platform, "last_import_at": last, **history}

    def remove_record(self, platform, kind, record_id):
        require_platform(platform)
        header_for(kind)
        if type(record_id) is not int or not 0 < record_id < 2**63:
            raise ValueError("记录编号无效。请刷新账户资料后重试。")
        with self.store.connect(write=True) as db:
            result = db.execute(
                "DELETE FROM account_observations WHERE platform=? AND kind=? AND id=?",
                (platform, kind, record_id),
            )
        return result.rowcount == 1
