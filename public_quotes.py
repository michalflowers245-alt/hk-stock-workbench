"""Read Tencent's public quote-page data as occasional, explicitly non-live snapshots.

The public HK page https://gu.qq.com/hk00700/gp references qt.gtimg.cn.
Its own bundle (https://st.gtimg.com/quotes/hk/bundle.73b44cab.js, inspected
2026-11-17) maps timestamp/last/previous/open/high/low to 30/3/4/5/33/34,
and shares/turnover to 36/37; index turnover is scaled by 10,000. This is
a public-page adapter, not a contracted market-data API or an entitlement.
No cookies, login, CAPTCHA handling or alternate-host retry are used.
"""
from datetime import datetime, timedelta, timezone
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc
SOURCE = "腾讯自选股公开行情"
HOST = "qt.gtimg.cn"
USER_AGENT = "HK-Workbench/2 (personal local public snapshots)"
INDEX_CODES = {"HK.800000": "hkHSI", "HK.800700": "hkHSTECH", "HK.800100": "hkHSCEI"}
ROW = re.compile(r'v_(hk[A-Za-z0-9]+)="([^"\r\n]*)"\s*;')


def provider_symbol(symbol):
    if symbol in INDEX_CODES:
        return INDEX_CODES[symbol]
    if isinstance(symbol, str) and re.fullmatch(r"HK\.\d{5}", symbol):
        return "hk" + symbol[3:]
    return None


def quote_page_url(symbol):
    code = provider_symbol(symbol)
    if not code:
        return "https://gu.qq.com/"
    return "https://gu.qq.com/" + code + ("/zs" if symbol in INDEX_CODES else "/gp")


def number(raw, *, positive=False):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0 or (positive and value <= 0):
        return None
    return value


def parse_quotes(body, watchlist, captured_at=None):
    """Parse literal quote rows only; never eval remote JavaScript or invent time."""
    captured = captured_at or datetime.now(UTC)
    if captured.tzinfo is None:
        raise ValueError("采集时间须包含时区。")
    captured = captured.astimezone(UTC)
    raw_rows = {match[1]: match[2].split("~") for match in ROW.finditer(body)}
    quotes, checks = [], []
    for item in watchlist:
        symbol = item["symbol"]
        code = provider_symbol(symbol)
        check = {"url": quote_page_url(symbol), "symbol": symbol, "coverage": "public_snapshot", "ok": False}
        row = raw_rows.get(code)
        if not code:
            check["error"] = "此标的暂没有公开快照适配，可打开公开行情页查看。"
        elif not row or len(row) < 38 or row[2] != code[2:]:
            check["error"] = "公开来源未返回可核对的标的报价。"
        else:
            try:
                observed = datetime.strptime(row[30], "%Y/%m/%d %H:%M:%S").replace(tzinfo=HK).astimezone(UTC)
            except ValueError:
                observed = None
            last, previous = number(row[3], positive=True), number(row[4], positive=True)
            if not observed or observed > captured + timedelta(minutes=5):
                check["error"] = "来源报价时间缺失、无效或超前；未写入快照。"
            elif last is None or previous is None:
                check["error"] = "来源价格无效；未写入快照。"
            else:
                opened, high, low = number(row[5], positive=True), number(row[33], positive=True), number(row[34], positive=True)
                if high is not None and low is not None and (high < low or last > high + 0.01 or last < low - 0.01):
                    check["error"] = "来源价格字段不一致，等待下一次核对。"
                else:
                    turnover = number(row[37])
                    volume = None if symbol in INDEX_CODES else number(row[36])
                    if symbol in INDEX_CODES and turnover is not None:
                        turnover *= 10000
                    quotes.append({"symbol": symbol, "name": item.get("name") or row[1],
                                   "last_price": last, "prev_close": previous, "open": opened,
                                   "high": high, "low": low, "volume": volume, "turnover": turnover,
                                   "change_percent": round((last / previous - 1) * 100, 4),
                                   "quote_at": observed.isoformat(timespec="seconds"),
                                   "captured_at": captured.isoformat(timespec="seconds"),
                                   "source": SOURCE, "source_url": check["url"], "status": "public_snapshot",
                                   "snapshot_note": "公开页面快照，非实时推送；请同时核对来源报价时间和采集时间。"})
                    check["ok"], check["error"] = True, ""
        checks.append(check)
    return {"quotes": quotes, "checks": checks,
            "detail": f"已读取 {len(quotes)} / {len(watchlist)} 个公开行情快照。按来源报价时点保存，不承诺实时。"}


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != HOST or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise URLError("公开报价来源发生非预期跳转")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, *, limit=250000):
    with build_opener(SameHostRedirect()).open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=10) as response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("公开来源响应超出大小限制")
        return body


def collect_quotes(watchlist):
    """A single bounded batch, intended for manual refresh or >=15 minute checks."""
    watchlist = list(watchlist)[:30]
    codes = [provider_symbol(item.get("symbol")) for item in watchlist]
    url = "https://" + HOST + "/q=" + ",".join(dict.fromkeys(code for code in codes if code))
    try:
        # The public endpoint currently returns plain "bad request" for robots.txt;
        # no robots directives then apply. Explicit denials, or failed checks, stop.
        try:
            robots = fetch("https://" + HOST + "/robots.txt", limit=50000).decode("utf-8", errors="replace")
        except HTTPError as error:
            if error.code not in (404, 410):
                raise
            robots = ""
        rules = RobotFileParser()
        rules.parse(robots.splitlines())
        if not rules.can_fetch(USER_AGENT, url):
            raise ValueError("公开来源的访问规则不允许读取")
        if not any(codes):
            return parse_quotes("", watchlist)
        body = fetch(url).decode("gb18030", errors="strict")
        return parse_quotes(body, watchlist, datetime.now(UTC))
    except (OSError, ValueError, UnicodeError) as error:
        detail = "公开行情暂时无法读取（" + type(error).__name__ + "），已保存的历史记录不受影响。"
        return {"quotes": [], "checks": [{"url": quote_page_url(item["symbol"]), "symbol": item["symbol"],
                "coverage": "public_snapshot", "ok": False, "error": detail} for item in watchlist], "detail": detail}
