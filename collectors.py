"""Read-only collectors for the five public campaign sources and HKEX new listings."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timedelta
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
USER_AGENT = "HongKongWorkbench/1.0"
HTML_LIMIT = 3_000_000
PDF_LIMIT = 14_000_000
IPO_PDF_LIMIT = 30_000_000

PLATFORMS = {
    "za": {
        "name": "ZA Bank",
        "url": "https://bank.za.group/hk",
        "sources": ("https://bank.za.group/hk", "https://bank.za.group/hk/open-account-mgm"),
        "hosts": frozenset({"bank.za.group", "cdn.zaticdn.com"}),
    },
    "welab": {
        "name": "WeLab Bank",
        "url": "https://www.welab.bank/zh/",
        "sources": ("https://www.welab.bank/zh/",),
        "hosts": frozenset({"www.welab.bank", "welab.bank"}),
        "campaigns": {
            "https://www.welab.bank/zh/feature/targeted-mgm-program-oct26/": "特选客户友赏齐齐分",
            "https://www.welab.bank/zh/feature/oct-fun-fund-reward-26/": "第四季度特选客户 Fun Fund 投资赏",
            "https://www.welab.bank/zh/feature/global-wallet-etb-offer-feb26/": "WeLab Global Wallet 扣賬卡全年激賞優惠",
            "https://www.welab.bank/zh/feature/level-up-rewards-may26/": "級級賞｜升級存款・升級回報",
        },
    },
    "eddid": {
        "name": "艾德证券",
        "url": "https://www.eddid.com.hk/sf/announcements/",
        "sources": ("https://www.eddid.com.hk/sf/announcements/",),
        "hosts": frozenset({"www.eddid.com.hk", "eddid.com.hk", "promo.eddid.com.hk"}),
    },
    "fafa": {
        "name": "FaFa 熊猫证券",
        "url": "https://fafa.hk/zh-hk/fafa/",
        "sources": ("https://fafa.hk/zh-hk/fafa/",),
        "hosts": frozenset({"fafa.hk", "www.fafa.hk"}),
    },
    "phillip": {
        "name": "辉立证券",
        "url": "https://www.poems.com.hk/zh-hk/customer-service/latest-promotion/",
        "sources": (
            "https://www.poems.com.hk/zh-hk/customer-service/latest-promotion/",
            "https://www.poems.com.hk/zh-hk/customer-service/acc_open_offer_2026/",
        ),
        "hosts": frozenset({"www.poems.com.hk", "www1.poems.com.hk", "poems.com.hk"}),
    },
}
IPO_SOURCES = {
    "主板": "https://www2.hkexnews.hk/new-listings/new-listing-information/main-board?sc_lang=en",
    "GEM": "https://www2.hkexnews.hk/New-Listings/New-Listing-Information/GEM?sc_lang=en",
}
IPO_HOSTS = frozenset({"www1.hkexnews.hk", "www2.hkexnews.hk", "www.hkexnews.hk"})

TRACKING_KEYS = {"fbclid", "gclid", "source", "_branch_match_id", "_branch_referrer"}
PROMOTION = re.compile(
    r"推[廣广]|[優优]惠|[奬獎奖][賞赏勵励]|迎新|限定[禮礼]|限[時时]|回[贈赠]|立[減减]|折扣|"
    r"返[現现還还]|[賞赏]祭|[薦荐]|[禮礼]遇|promot(?:ion|ional)|limited.time|rewards?|"
    r"welcome|cashback|coupon|offer|rebate|refer.a.friend",
    re.I,
)
RISK_NOTICE = re.compile(r"詐騙|诈骗|收[費费][調调]整|假期|[資资]料保障|[網网]絡安全|[條条]款.*修[訂订]")
TERMS = re.compile(r"[條条]款|terms|conditions|\btnc\b", re.I)
PERIOD = re.compile(r"(?:推[廣广]|推[廣广]活[動动]|[優优]惠|活[動动]|登[記记])(?:期[間间]|期|日期)|(?:promotion|offer|campaign) period|valid (?:from|until|through)", re.I)
DEADLINE = re.compile(
    r"(?:截[止至]|截止[時时][間间]|[結结]束日期|完[結结]日期|[優优]惠有效至|直至|推[廣广]期至|"
    r"valid until|expires? on|closes? on|deadline|ends? on|即日起至)",
    re.I,
)
DATE_TOKEN = re.compile(
    r"(?P<ymd>(?P<year>20\d{2})\s*(?:年\s*|[-/.])(?P<month>\d{1,2})\s*(?:月\s*|[-/.])(?P<day>\d{1,2})(?:\s*[日號号])?)"
    r"|(?P<cn>(?P<cnmonth>\d{1,2})\s*月\s*(?P<cnday>\d{1,2})\s*[日號号])"
    r"|(?P<en>(?:(?P<enday>\d{1,2})(?:st|nd|rd|th)?\s+(?P<enmonth>JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
    r"|(?P<enmonth_alt>JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(?P<enday_alt>\d{1,2})(?:st|nd|rd|th)?)(?:\s*,?\s*(?P<enyear>20\d{2}))?)",
    re.I,
)
CLOCK = re.compile(
    r"(?:(?P<meridiem_cn>上[午晝昼]|下午|晚上|早上|凌晨)\s*)?"
    r"(?:(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>[0-5]\d)|(?P<hour_cn>\d{1,2})\s*[時时點点](?:(?P<minute_cn>\d{1,2})\s*分)?)"
    r"(?:\s*(?P<meridiem_en>[ap]\.?(?:m)\.?|noon))?|(?P<noon>\bnoon\b|中午(?:十二|12)[時时點点])",
    re.I,
)
BETWEEN_DATES = re.compile(r"至|到|[~～—–]|\b(?:to|through|until)\b|^\s*-\s*$", re.I)
MONTH_NAMES = {datetime(2020, number, 1).strftime("%B").lower(): number for number in range(1, 13)}
FACT_PATTERNS = {
    "eligibility": re.compile(r"合[資资]格|[適适]用於|[適适]用[对對]象|新客[戶户]|指定客[戶户]|現有客[戶户]|eligible|eligibility|new customers?", re.I),
    "capital": re.compile(r"存款|入[金錢钱]|存入|最低[結结][餘余]|[資资][產产]|[驗验][資资]|本金|交易[額额]|deposit|funding|minimum balance", re.I),
    "holding": re.compile(r"留存|[維维]持[持滿满存]|[持連连][續续]|[持維维]持.{0,20}\d|[鎖锁]定|持有期|存期|holding period|maintain|consecutive days", re.I),
    "fees": re.compile(r"手[續续][費费]|平台[費费]|佣金|收[費费]|免[費费]|[費费]率|commission|fees?|charge", re.I),
    "reward": re.compile(r"[獎奖奬][賞赏勵励]|回[贈赠]|回款|[優优]惠券|現金券|[禮礼]券|[禮礼]品|返[現现還还]|rebate|voucher|reward|cashback", re.I),
    "payout": re.compile(r"[發发]放|派[發发]|存入[戶户]口|到[賬账帳]|[發发]出[時时]間|[獎奖奬][賞赏勵励].{0,20}[日月]|credited|payout|distributed|redemption", re.I),
}

_robots_lock = threading.Lock()
_robots_cache: dict[str, tuple[float, RobotFileParser | None, str | None]] = {}

# These are separately published ZA Bank documents, not copies of the inaccessible homepage.
ZA_STUDENT_TERMS = {
    "https://cdn.zaticdn.com/if/ipage/prd/data/agreement/zh_HK/1002655.pdf": "大學生開戶優惠",
    "https://cdn.zaticdn.com/if/ipage/prd/data/agreement/zh_HK/1002656.pdf": "大學生專屬優惠",
}
ZA_STOCK_PROMOTION_PAGE = (
    "https://cdn.zaticdn.com/if/ipage/prd/data/blog/"
    "d383b63c-0f51-474b-9d94-15f6176acc3c/hk/investupgrade.html"
)


class SourceError(Exception):
    """A public source could not be safely or reliably read."""


def official_url(href: str, base: str, hosts: frozenset[str]) -> str:
    """Resolve a public link within the collector's exact HTTPS allowlist."""
    if not href or href.lstrip().startswith("#"):
        return ""
    try:
        parts = urlsplit(urljoin(base, href.strip()))
        host = (parts.hostname or "").lower()
        if (
            host not in hosts
            or parts.scheme not in {"http", "https"}
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            return ""
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")
        ]
        return urlunsplit(("https", host, parts.path or "/", urlencode(query), ""))
    except ValueError:
        return ""


class OfficialRedirect(HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = official_url(newurl, req.full_url, self.hosts)
        if not target or urlsplit(newurl).scheme.lower() not in {"", "https"}:
            raise SourceError("来源跳转到未授权地址")
        return super().redirect_request(req, fp, code, msg, headers, target)


def download(url: str, hosts: frozenset[str], limit: int) -> tuple[str, str, bytes]:
    url = official_url(url, url, hosts)
    if not url:
        raise SourceError("来源地址不在官方允许范围内")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,text/plain"})
    try:
        with build_opener(OfficialRedirect(hosts)).open(request, timeout=10) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise SourceError("来源文件超过读取上限")
            return response.geturl(), response.headers.get_content_type(), content
    except HTTPError as exc:
        raise SourceError(f"HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError, socket.timeout) as exc:
        message = str(getattr(exc, "reason", exc))
        if "CERTIFICATE" in message or "SSL" in message or "TLS" in message:
            raise SourceError("HTTPS 连接失败") from exc
        raise SourceError("来源连接超时或不可达") from exc


def permitted(url: str, hosts: frozenset[str]) -> None:
    host = urlsplit(url).hostname
    if not host:
        raise SourceError("来源地址无效")
    with _robots_lock:
        data = _robots_cache.get(host)
    if not data or data[0] < time.monotonic():
        location = f"https://{host}/robots.txt"
        parser = None
        error = None
        try:
            _, content_type, payload = download(location, hosts, 600_000)
            if content_type.startswith("text/") and content_type != "text/html":
                parser = RobotFileParser(location)
                parser.parse(payload.decode("utf-8", errors="replace").splitlines())
            elif content_type == "text/html":
                if re.search(br"<title>[^<]*(?:access denied|forbidden|challenge)", payload, re.I):
                    error = "来源禁止自动读取"
        except SourceError as exc:
            if not re.fullmatch(r"HTTP (?:400|404|410)", str(exc)):
                error = f"来源访问规则不可用（{exc}）"
        data = (time.monotonic() + (1800 if error else 3600), parser, error)
        with _robots_lock:
            _robots_cache[host] = data
    _, parser, error = data
    if error:
        raise SourceError(error)
    if parser and not parser.can_fetch(USER_AGENT, url):
        raise SourceError("来源规则禁止自动读取此页面")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)


class DocumentParser(HTMLParser):
    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self, html: str):
        super().__init__()
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def append_node(self, tag, attrs):
        node = Node(tag, dict((key, value or "") for key, value in attrs))
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        if len(self.stack) >= 220:
            return
        if tag not in self.VOID:
            for active_tag in ("li", "p", "option", "tr", "td", "th"):
                if tag == active_tag and self.stack[-1].tag == active_tag:
                    self.stack.pop()
        node = self.append_node(tag, attrs)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.append_node(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.stack[-1].tag not in {"script", "style", "template", "noscript"}:
            self.stack[-1].children.append(data)


IGNORE = frozenset({"script", "style", "template", "noscript", "head", "svg", "footer"})
BLOCK_TAGS = frozenset({"article", "blockquote", "div", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "li", "main", "p", "section", "table", "tr", "ul", "ol"})


def normalized(text: str) -> str:
    return " ".join(text.replace("\u200b", "").replace("\ufeff", "").split())


def nodes(node: Node, tag: str) -> list[Node]:
    found = [node] if node.tag == tag else []
    for child in node.children:
        if isinstance(child, Node) and child.tag not in IGNORE:
            found.extend(nodes(child, tag))
    return found


def node_text(node: Node, *, strip_layout: bool = False) -> str:
    if node.tag in IGNORE or (strip_layout and node.tag in {"header", "nav", "form", "button"}):
        return ""
    if node.tag in {"br", "hr"}:
        return "\n"
    if node.tag == "img":
        return node.attrs.get("alt", "")
    result = "".join(
        node_text(child, strip_layout=strip_layout) if isinstance(child, Node) else child
        for child in node.children
    )
    if node.tag in BLOCK_TAGS or node.tag in {"td", "th"}:
        return f"\n{result}\n"
    return result


def page_lines(root: Node) -> list[str]:
    main = nodes(root, "main")
    content = main[0] if len(main) == 1 else root
    lines = [normalized(line) for line in node_text(content, strip_layout=True).splitlines()]
    return list(dict.fromkeys(line for line in lines if line and len(line) < 3000))


def headline(root: Node, fallback: str) -> str:
    return next((value for item in nodes(root, "h1") if (value := normalized(node_text(item)))), fallback)


def document_title(root: Node) -> str:
    html = nodes(root, "html")
    if not html:
        return ""
    head = next((child for child in html[0].children if isinstance(child, Node) and child.tag == "head"), None)
    if head is None:
        return ""
    for child in head.children:
        if isinstance(child, Node) and child.tag == "meta" and child.attrs.get("property") == "og:title":
            if value := normalized(child.attrs.get("content", "")):
                return value
    return next((normalized(node_text(child)) for child in head.children if isinstance(child, Node) and child.tag == "title"), "")


def get_html(url: str, hosts: frozenset[str]) -> tuple[str, Node]:
    permitted(url, hosts)
    final_url, media_type, content = download(url, hosts, HTML_LIMIT)
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise SourceError("来源未返回网页")
    encoding = "utf-8-sig"
    charset = re.search(br'<meta[^>]{0,150}charset\s*=\s*["\']?([a-z0-9_-]+)', content[:4096], re.I)
    if charset:
        encoding = charset[1].decode("ascii")
    try:
        html = content.decode(encoding, errors="replace")
    except LookupError:
        html = content.decode("utf-8", errors="replace")
    title = re.search(r"<title[^>]*>([^<]*)</title>", html, re.I)
    if title and re.search(r"access denied|just a moment|attention required|verify you are|page not found|403 forbidden", title[1], re.I):
        raise SourceError("来源要求验证或页面不可用")
    if len(html) < 400:
        raise SourceError("来源响应内容过少")
    return final_url, DocumentParser(html).root


class PDFEncodingFilter(logging.Filter):
    def filter(self, record):
        # This optional font fallback is very noisy. Actual parsing failures are surfaced in checks.
        return not record.getMessage().startswith("fontTools is required to fully parse")


logging.getLogger("pypdf._cmap").addFilter(PDFEncodingFilter())


def pdf_text(url: str, hosts: frozenset[str], pages: int = 6, *, limit: int = PDF_LIMIT) -> tuple[list[str], str]:
    permitted(url, hosts)
    _, _, payload = download(url, hosts, limit)
    if not payload.lstrip().startswith(b"%PDF"):
        raise SourceError("条款链接未返回 PDF")
    digest = hashlib.sha256(payload).hexdigest()
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SourceError("缺少 PDF 文本解析组件") from exc
    try:
        reader = PdfReader(io.BytesIO(payload))
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:pages])
        return [line for raw in text.splitlines() if (line := normalized(raw))][:850], digest
    except Exception as exc:
        raise SourceError("PDF 文本暂时无法解析") from exc


def parsed_date(match: re.Match, year: int | None = None) -> date | None:
    group = match.groupdict()
    if group["ymd"]:
        year, month, day = int(group["year"]), int(group["month"]), int(group["day"])
    elif group["cn"]:
        month, day = int(group["cnmonth"]), int(group["cnday"])
    else:
        year = int(group["enyear"]) if group["enyear"] else year
        month = MONTH_NAMES[(group["enmonth"] or group["enmonth_alt"]).lower()]
        day = int(group["enday"] or group["enday_alt"])
    if year:
        try:
            return date(year, month, day)
        except ValueError:
            pass
    return None


def parsed_clock(match: re.Match) -> clock_time | None:
    if match["noon"] or match["meridiem_en"] == "noon":
        return clock_time(12)
    hour = int(match["hour"] or match["hour_cn"])
    minute = int(match["minute"] or match["minute_cn"] or 0)
    marker = (match["meridiem_en"] or match["meridiem_cn"] or "").lower().replace(".", "")
    if marker in {"pm", "下午", "晚上"} and hour < 12:
        hour += 12
    if marker in {"am", "凌晨"} and hour == 12:
        hour = 0
    if hour == 24 and minute == 0:
        return None
    try:
        return clock_time(hour, minute)
    except ValueError:
        return None


def deadline_clock(line: str, dates: list[re.Match], index: int) -> clock_time | None:
    match = dates[index]
    after = line[match.end():match.end() + 26]
    if index + 1 < len(dates):
        after = line[match.end():min(match.end() + 26, dates[index + 1].start())]
    after = re.split(r"[。；;]|\b(?:through|until)\b", after, maxsplit=1, flags=re.I)[0]
    weekday = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|(?:星期)?[一二三四五六日]"
    trailing = CLOCK.search(after)
    if trailing and re.fullmatch(rf"[\s,，]*(?:[（(](?:{weekday})[）)][\s,，]*)?(?:(?:at|在|於|于)\s*)?", after[:trailing.start()], re.I):
        return parsed_clock(trailing)
    start = dates[index - 1].end() if index else 0
    before = line[max(start, match.start() - 45):match.start()]
    before = re.sub(r"[(（](?:星期)?[一二三四五六日][)）]", " ", before)
    for candidate in reversed(list(CLOCK.finditer(before))):
        between = before[candidate.end():]
        if re.fullmatch(r"\s*[,，.．·:]*\s*(?:of |on )?(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,， ]*)?", between, re.I):
            return parsed_clock(candidate)
    return None


def periods(lines: list[str]) -> dict:
    results = []
    for line in lines:
        period = PERIOD.search(line)
        closing = DEADLINE.search(line)
        dates = list(DATE_TOKEN.finditer(line))
        if not dates:
            continue
        explicit = [parsed_date(item) for item in dates]
        pairs = [
            (index, start, end)
            for index, (first, second) in enumerate(zip(dates, dates[1:]))
            if BETWEEN_DATES.search(line[first.end():second.start()])
            and (start := parsed_date(first, explicit[index + 1].year if explicit[index + 1] else None))
            and (end := parsed_date(second, start.year))
            and start <= end
        ]
        if pairs and period:
            index, start, end = pairs[0]
            priority = 4
        elif pairs and closing:
            index, start, end = pairs[0]
            priority = 3
        elif closing:
            candidates = [(index, day) for index, day in enumerate(explicit) if day and dates[index].start() >= closing.start()]
            if not candidates:
                continue
            index, end = candidates[0]
            index -= 1
            start = None
            priority = 2
        else:
            continue
        day_time = deadline_clock(line, dates, index + 1)
        deadline = datetime.combine(end, day_time, HKT).isoformat() if day_time else None
        results.append((priority, bool(day_time), start, end, deadline, line[:460]))
    if not results:
        return {"starts_at": None, "ends_on": None, "deadline": None, "evidence": {}}
    _, _, start, end, deadline, line = max(results, key=lambda item: item[:2])
    evidence = {"ends_on": line}
    if start:
        evidence["starts_at"] = line
    if deadline:
        evidence["deadline"] = line
    return {
        "starts_at": start.isoformat() if start else None,
        "ends_on": end.isoformat(),
        "deadline": deadline,
        "evidence": evidence,
    }


def relevant_facts(lines: list[str]) -> dict[str, str]:
    facts = {}
    for key, regex in FACT_PATTERNS.items():
        candidates = [line for line in lines if 8 <= len(line) <= 520 and regex.search(line)]
        if candidates:
            ranked = sorted(
                enumerate(candidates),
                key=lambda item: (
                    bool(re.search(r"\d|須|必[須须]|[適适]用|must|required", item[1], re.I)),
                    bool(re.search(r"[。；.]$", item[1])),
                    -item[0],
                ),
                reverse=True,
            )
            excerpts = list(dict.fromkeys(item[1] for item in ranked[:3]))
            facts[key] = "\n".join(excerpts)[:900]
    return facts


def make_offer(
    platform: str,
    title: str,
    url: str,
    source_url: str,
    lines: list[str],
    *,
    key: str | None = None,
    terms_hashes: list[str] | None = None,
    terms_urls: list[str] | None = None,
    terms_checked: bool = False,
    terms_complete: bool = True,
) -> dict:
    dates = periods(lines)
    facts = relevant_facts(lines)
    important = list(dict.fromkeys(
        line for line in lines
        if PERIOD.search(line) or DEADLINE.search(line)
        or any(regex.search(line) for regex in FACT_PATTERNS.values())
    ))
    evidence = {**dates.pop("evidence"), **facts}
    digest_text = "\n".join(normalized(line) for line in important if len(line) < 650)[:18000]
    return {
        "platform": platform, "key": key or url, "title": title[:170],
        "url": url, "source_url": source_url,
        "text": "\n".join(lines)[:26000],
        "revision_text": digest_text or "\n".join(lines)[:5000],
        "terms_hashes": terms_hashes or [], "terms_urls": terms_urls or [],
        "terms_complete": terms_complete,
        "source_kind": "official_terms" if terms_checked else "official",
        "facts": facts, "evidence": evidence, **dates,
    }


def links(root: Node, base: str, hosts: frozenset[str]) -> list[tuple[str, str]]:
    found = []
    for anchor in nodes(root, "a"):
        url = official_url(anchor.attrs.get("href", ""), base, hosts)
        label = normalized(node_text(anchor) or anchor.attrs.get("title", ""))
        if url and label:
            found.append((url, label))
    return list(dict.fromkeys(found))


def discovery_link(platform: str, url: str, label: str) -> bool:
    path = urlsplit(url).path.lower()
    if re.search(r"download|privacy|contact|disclaimer|\.pdf$|/login|/report", path):
        return False
    if platform == "za":
        return path.startswith("/hk/") and bool(PROMOTION.search(label) or re.search(r"powerdraw|za-card|reward|promo|campaign|open-account-mgm", path))
    if platform == "welab":
        return path.startswith("/zh/feature/") and bool(PROMOTION.search(label) or PROMOTION.search(path))
    if platform == "eddid":
        if urlsplit(url).hostname == "promo.eddid.com.hk":
            return bool(PROMOTION.search(label) or PROMOTION.search(path))
        return bool(re.fullmatch(r"/sf/announcements/\d+\.html", path) and PROMOTION.search(label) and not RISK_NOTICE.search(label))
    if platform == "phillip":
        if not path.startswith("/zh-hk/") or re.search(r"/pfa/|/phillip-apps/|/latest-promotion/?$", path):
            return False
        return bool(re.search(r"acc_open_offer_|/promotion/[^/]|/ipo-0-int|/promotions/[^/]", path) or PROMOTION.search(label))
    return False


def candidate_terms(root: Node, base: str, hosts: frozenset[str]) -> list[str]:
    candidates = [
        (url, label) for url, label in links(root, base, hosts)
        if urlsplit(url).path.lower().endswith(".pdf") and (TERMS.search(url) or TERMS.search(label))
        and not re.search(r"privacy|disclaimer|general|archive|保密|私[隱隐]", url, re.I)
    ]
    return list(dict.fromkeys(url for url, _ in candidates))[:2]


def check(url: str, ok: bool, error: str = "", *, coverage: str = "public", scope: str = "source") -> dict:
    return {"url": url, "ok": ok, "error": error, "coverage": coverage, "scope": scope}


def fafa_offers(page: Node, url: str) -> list[dict]:
    groups = (
        ("fafa:invitation", re.compile(r"費率.{0,12}行情|優惠費率|優惠佣金|邀請開戶", re.I)),
        ("fafa:ipo-rebate", re.compile(r"新股申購|新股認購|IPO", re.I)),
    )
    offers = []
    for key, pattern in groups:
        for article in nodes(page, "article"):
            header = next((normalized(node_text(item)) for item in nodes(article, "h2")), "")
            details = page_lines(article)
            if header and pattern.search(header):
                offers.append(make_offer("fafa", header, url, url, details, key=key))
                break
    return offers


def collect_za_public_documents(hosts: frozenset[str]) -> tuple[list[dict], list[dict]]:
    """Check known public documents independently of bank.za.group availability."""
    offers, checks = [], []
    for url, title in ZA_STUDENT_TERMS.items():
        try:
            lines, _ = pdf_text(url, hosts)
            if not lines or title not in " ".join(lines[:4]):
                raise SourceError("公開條款與監測的活動不符")
            # A PDF can wrap the offer period across lines. Join only its first numbered clause.
            opening = []
            for line in lines[1:8]:
                if re.match(r"^2[.．、]", line):
                    break
                opening.append(line)
            period = " ".join(opening)
            if not periods([period])["ends_on"]:
                raise SourceError("未能從公開條款確認活動期限")
            offers.append(make_offer("za", title, url, url, [period, *lines],
                                     terms_urls=[url], terms_checked=True,
                                     terms_hashes=[hashlib.sha256("\n".join(lines).encode()).hexdigest()]))
            checks.append(check(url, True, coverage="官方公開活動條款"))
        except SourceError as exc:
            checks.append(check(url, False, str(exc), coverage="官方公開活動條款"))

    url = ZA_STOCK_PROMOTION_PAGE
    try:
        _, document = get_html(url, hosts)
        paragraphs = [line for line in page_lines(document) if "「股票最勁抽」推廣期" in line]
        if not paragraphs or not periods(paragraphs)["ends_on"]:
            raise SourceError("未能從公開說明確認活動期限")
        offers.append(make_offer("za", "股票最勁抽", url, url, paragraphs, key=f"{url}::stock-power-draw",
                                 terms_complete=False))
        checks.append(check(url, True, coverage="官方公開活動說明"))
    except SourceError as exc:
        checks.append(check(url, False, str(exc), coverage="官方公開活動說明"))
    return offers, checks


def collect_offers(platform_id: str) -> dict:
    """Collect official marketing leads; distinguish index, detail and terms failures."""
    config = PLATFORMS[platform_id]
    hosts = config["hosts"]
    sources = config["sources"]
    pages: dict[str, tuple[str, Node]] = {}
    checks: list[dict] = []
    offers: list[dict] = []
    candidates: dict[str, tuple[str, str]] = {
        url: (title, url) for url, title in config.get("campaigns", {}).items()
    }
    detail_cache: dict[str, tuple[str, Node] | SourceError] = {}
    terms_cache: dict[str, tuple[list[str], str] | SourceError] = {}

    for source in sources:
        try:
            current_url, page = get_html(source, hosts)
            pages[source] = (current_url, page)
            if platform_id == "fafa":
                sections = fafa_offers(page, current_url)
                offers.extend(sections)
                message = "" if sections else "公开页面已读取，优惠区结构尚未识别"
                checks.append(check(source, bool(sections), message, coverage="public_and_app_manual"))
                continue
            found = [(url, label) for url, label in links(page, current_url, hosts) if discovery_link(platform_id, url, label)]
            recognizable = bool(found) or (platform_id == "eddid" and "公司公告" in headline(page, ""))
            checks.append(check(source, recognizable, "" if recognizable else "公开活动区结构尚未识别"))
            for url, label in found:
                if url not in {current_url, *sources}:
                    candidates.setdefault(url, (label, current_url))
        except SourceError as exc:
            checks.append(check(source, False, str(exc)))

    direct_campaigns = {
        "https://bank.za.group/hk/open-account-mgm": "好友推荐开户",
    }
    for source, fallback in direct_campaigns.items():
        if source in pages:
            candidates.setdefault(source, (fallback, source))

    for url, (label, source_url) in list(candidates.items())[:15]:
        try:
            if url in pages:
                final_url, page = pages[url]
            else:
                cached = detail_cache.get(url)
                if isinstance(cached, SourceError):
                    raise cached
                final_url, page = cached or get_html(url, hosts)
                detail_cache[url] = (final_url, page)
            lines = page_lines(page)
            if len("".join(lines)) < 50:
                raise SourceError("活动页没有可辨识的公开条款")
            terms_urls = candidate_terms(page, final_url, hosts)
            if platform_id == "welab" and "/targeted-mgm-program-" in urlsplit(final_url).path:
                # This landing page describes both the referrer and the referred new customer.
                # Use the referrer terms for the targeted-referrer offer's extracted facts.
                terms_urls.sort(key=lambda link: "targeted-mgm-program" not in urlsplit(link).path)
            terms_hashes = []
            terms_lines = []
            terms_complete = True
            for terms_url in terms_urls:
                try:
                    result = terms_cache.get(terms_url)
                    if isinstance(result, SourceError):
                        raise result
                    excerpts, _ = result or pdf_text(terms_url, hosts, pages=6)
                    terms_cache[terms_url] = (excerpts, "")
                    if not excerpts:
                        raise SourceError("PDF 未检测到可读取的文字")
                    terms_hashes.append(hashlib.sha256("\n".join(excerpts).encode()).hexdigest())
                    if not terms_lines:
                        terms_lines = excerpts
                    checks.append(check(terms_url, True, coverage="terms", scope="detail"))
                except SourceError as exc:
                    terms_complete = False
                    terms_cache[terms_url] = exc
                    checks.append(check(terms_url, False, str(exc), coverage="terms", scope="detail"))
            # On overview pages the H1 can be the provider name. Keep the specific card label in that case.
            title = headline(page, label)
            if len(title) > 90 or title in {"查看詳情", "進一步了解", "條款及細則", "最新推廣"}:
                title = document_title(page).split(" | ")[0] or title
            if not PROMOTION.search(title) and PROMOTION.search(label) and len(label) <= 100:
                title = label
            if title in {"查看詳情", "進一步了解"}:
                title = next((line for line in lines if 5 <= len(line) <= 65 and PROMOTION.search(line)), urlsplit(final_url).path)
            merged = list(dict.fromkeys([*terms_lines, *lines]))
            offers.append(make_offer(
                platform_id, title, final_url, source_url, merged,
                terms_hashes=terms_hashes, terms_urls=terms_urls,
                terms_checked=bool(terms_lines or any(TERMS.search(line) for line in lines)),
                terms_complete=terms_complete,
            ))
            if url not in sources:
                checks.append(check(url, True, coverage="offer_detail", scope="detail"))
        except SourceError as exc:
            detail_cache[url] = exc
            checks.append(check(url, False, str(exc), coverage="offer_detail", scope="detail"))

    if platform_id == "za":
        published, document_checks = collect_za_public_documents(hosts)
        offers.extend(published)
        checks.extend(document_checks)
    unique = {offer["key"]: offer for offer in offers}
    return {"offers": list(unique.values()), "checks": checks}


IPO_CLOSING = re.compile(
    r"close(?:s)? of the application lists?|closing of (?:the )?(?:application lists?|public offer)|"
    r"application lists? (?:closes?|ends?)|"
    r"(?:Hong Kong Public Offering|public offer)[^\n]{0,45}(?:closes?|ends?)|"
    r"截止[辦办]理申[請请]登[記记]|截止辦理認購登記|公開發售.{0,18}(?:截止|結束)",
    re.I,
)
IPO_LISTING = re.compile(r"dealings in .{0,100} expected to (?:commence|begin)|(?:expected )?date of listing|股份.{0,18}開始買賣", re.I)


def ipo_dates(lines: list[str], year: int | None = None) -> dict:
    result: dict[str, str | None] = {"deadline": None, "ends_on": None, "deadline_evidence": None, "listing_date": None}
    for label, pattern in (("deadline", IPO_CLOSING), ("listing_date", IPO_LISTING)):
        for index, line in enumerate(lines):
            excerpt = " ".join(lines[index:index + 3])
            match = pattern.search(excerpt)
            if not match or match.start() >= len(line):
                continue
            dates = list(DATE_TOKEN.finditer(excerpt, match.end()))
            if not dates:
                continue
            target_date = parsed_date(dates[0], year)
            if not target_date:
                continue
            if label == "listing_date":
                result[label] = target_date.isoformat()
            else:
                result["ends_on"] = target_date.isoformat()
                tick = deadline_clock(excerpt, dates, 0)
                if tick:
                    result[label] = datetime.combine(target_date, tick, HKT).isoformat()
                result["deadline_evidence"] = excerpt[:dates[0].end() + 25].strip()[:460]
            break
    return result


_ipo_cache_lock = threading.Lock()
_ipo_cache: dict[str, tuple[float, dict]] = {}


def read_ipo_document(document: str, *, prospectus: bool = False) -> dict:
    with _ipo_cache_lock:
        known = _ipo_cache.get(document)
    if known and known[0] > time.monotonic():
        return known[1]
    lines, _ = pdf_text(document, IPO_HOSTS, pages=8, limit=IPO_PDF_LIMIT if prospectus else PDF_LIMIT)
    if not lines:
        raise SourceError("公告 PDF 未检测到可读取的文字")
    published = re.search(r"/(20\d{2})/\d{4}/", document)
    result = ipo_dates(lines, int(published[1]) if published else None)
    if result.get("deadline_evidence"):
        result["deadline_source_url"] = document
    with _ipo_cache_lock:
        if len(_ipo_cache) >= 50:
            _ipo_cache.pop(next(iter(_ipo_cache)))
        _ipo_cache[document] = (time.monotonic() + 12 * 3600, result)
    return result


def ipo_rows(root: Node, board: str, base: str, *, today: date | None = None) -> tuple[list[dict], bool]:
    today = today or datetime.now(HKT).date()
    output = []
    recognized = False
    for table in nodes(root, "table"):
        headers = " ".join(normalized(node_text(item)).lower() for item in nodes(table, "th"))
        if "stock code" not in headers or "allotment results" not in headers:
            continue
        recognized = True
        for row in nodes(table, "tr"):
            cells = [child for child in row.children if isinstance(child, Node) and child.tag == "td"]
            if len(cells) < 5:
                continue
            code = normalized(node_text(cells[0]))
            name = normalized(node_text(cells[1]))
            if not re.fullmatch(r"\d{1,5}", code) or not name:
                continue
            docs = [[url for url, _ in links(cell, base, IPO_HOSTS)] for cell in cells[2:5]]
            announcement, prospectus, allotment = [doc[0] if doc else None for doc in docs]
            if not (announcement or prospectus or allotment):
                continue
            published = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", prospectus or announcement or "")
            document_day = None
            if published:
                try:
                    document_day = date(*map(int, published.groups()))
                except ValueError:
                    pass
            output.append({
                "code": code.zfill(5), "name": name, "board": board,
                "source_url": base, "announcement_url": announcement,
                "prospectus_url": prospectus, "allotment_url": allotment,
                "deadline": None, "ends_on": None, "deadline_evidence": None, "listing_date": None,
                "fresh_prospectus": bool(prospectus and document_day and today - timedelta(days=24) <= document_day <= today + timedelta(days=1)),
            })
    return output[:35], recognized


def collect_ipos() -> dict:
    """Use final HKEX published rows, with deadline evidence from recent prospectuses."""
    checks = []
    ipos = []
    for board, source in IPO_SOURCES.items():
        try:
            final_url, root = get_html(source, IPO_HOSTS)
            extracted, recognized = ipo_rows(root, board, final_url)
            checks.append(check(source, recognized, "" if recognized else "无法识别港交所上市表格", coverage="ipo"))
            ipos.extend(extracted)
        except SourceError as exc:
            checks.append(check(source, False, str(exc), coverage="ipo"))
    selected = [row for row in ipos if row.pop("fresh_prospectus")]

    def enrich(row: dict) -> None:
        for key in ("announcement_url", "prospectus_url"):
            url = row[key]
            if not url:
                continue
            try:
                extracted = read_ipo_document(url, prospectus=key == "prospectus_url")
                row.update((field, value) for field, value in extracted.items() if value)
                checks.append(check(url, True, coverage="ipo_document", scope="detail"))
                if row["deadline"] and row["listing_date"]:
                    break
            except SourceError as exc:
                checks.append(check(url, False, str(exc), coverage="ipo_document", scope="detail"))

    if selected:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(enrich, selected[:8]))
    return {"ipos": ipos, "checks": checks}
