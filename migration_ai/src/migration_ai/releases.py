"""Smart Planning agent: AAP patch releases from the Red Hat documentation.

For an AAP version (2.5, 2.6, 2.7) the agent reads the "Patch releases" page of the
Red Hat documentation, extracts every release (date, build label, link) and, for the
selected ones, the release notes (component versions, enhancements, bug fixes, CVEs).

Selection (what the user asked for):
  * releases published AFTER today, if any (the pages list published releases only,
    so this is normally empty);
  * otherwise the last three releases published up to today;
  * when the installed version of the target AAP is known (controller version from the
    anonymous /api/controller/v2/ping/), also every release newer than the installed one.

The AAP version (2.5, 2.6, 2.7...) is detected from the installed controller version when
the user does not name one: see aap_version_for().

Facts (dates, versions, IDs, CVEs) come only from the parser. The language model, when
available, writes the patch-scheduling summary from those facts and must cite their IDs.

Page layouts handled (October 2026):
  * 2.6 / 2.7: an index page ("whats_new-async_updates") with one link per release,
    titled "Ansible Automation Platform patch release <Month D, YYYY> (<build>)", and a
    page per release;
  * 2.5: one long page ("release_notes/patch_releases") with a section per release.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

log = logging.getLogger(__name__)

DOCS = "https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform"
DEFAULT_URLS = {
    "2.5": f"{DOCS}/2.5/html/release_notes/patch_releases",
    "2.6": f"{DOCS}/2.6/whats_new-async_updates",
    "2.7": f"{DOCS}/2.7/whats_new-async_updates",
}
MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December")
DATE_RE = re.compile(rf"\b({MONTHS})\s+(\d{{1,2}}),\s+(\d{{4}})\b")
AAP_ID_RE = re.compile(r"\bAAP-\d+\b")
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b")
CATEGORY_WORDS = {
    "overview": "Overview", "highlights": "Highlights", "features": "Features", "new features": "Features",
    "enhancements": "Enhancements", "bug fixes": "Bug fixes", "bug fix": "Bug fixes", "cve": "CVE",
    "cves": "CVE", "security": "CVE", "known issues": "Known issues", "deprecated": "Deprecated",
    "deprecated features": "Deprecated", "removed": "Removed", "general": "General",
    "technology preview": "Technology preview",
}
TECH_PREVIEW = ("mcp server", "metric service", "metrics service", "lightspeed")
RELEVANCE = [
    (r"migrat", 4), (r"upgrad", 3), (r"\brbac\b|\broles?\b|auditor|permission", 3),
    (r"credential|secret|encrypt|rotate_secret_key", 3), (r"authenticat|\bsso\b|ldap|saml|oidc|entra|mapping", 2),
    (r"organization|\bteams?\b|\busers?\b", 2), (r"import|export|backup|restore", 2),
    (r"project|inventor|job template|workflow|schedule|survey", 1),
    (r"execution environment|instance group|receptor|operator", 1),
    (r"gateway|controller|\bapi\b|token|database|postgres", 1),
]


@dataclass
class Block:
    kind: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)
    cells: list[str] = field(default_factory=list)


class _Blocks(HTMLParser):
    BLOCK = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "dt", "dd"}
    SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg", "button", "rh-tooltip", "rh-button"}

    def __init__(self, skip: set[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.SKIP = self.SKIP if skip is None else skip
        self.blocks: list[Block] = []
        self._stack: list[dict] = []
        self._skip = 0
        self._link: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        a = dict(attrs)
        if tag in self.BLOCK:
            self._stack.append({"tag": tag, "text": [], "links": []})
        elif tag == "a":
            self._link = {"href": a.get("href") or "", "text": []}
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br":
            self._data(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "a" and self._link is not None:
            text = _norm(" ".join(self._link["text"]))
            for b in self._stack:
                b["links"].append((text, self._link["href"]))
            if not self._stack:
                self.blocks.append(Block("a", text, [(text, self._link["href"])]))
            self._link = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_norm(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self.blocks.append(Block("row", " | ".join(self._row), cells=self._row))
            self._row = None
        elif tag in self.BLOCK and self._stack:
            for i in range(len(self._stack) - 1, -1, -1):
                if self._stack[i]["tag"] == tag:
                    b = self._stack.pop(i)
                    text = _norm(" ".join(b["text"]))
                    if text:
                        self.blocks.append(Block(tag, text, b["links"]))
                    break

    def handle_data(self, data):
        if not self._skip:
            self._data(data)

    def _data(self, data: str) -> None:
        if self._link is not None:
            self._link["text"].append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._stack and self._cell is None:
            self._stack[-1]["text"].append(data)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def html_blocks(html: str, include_nav: bool = False) -> list[Block]:
    p = _Blocks(_Blocks.SKIP - {"nav", "header"} if include_nav else None)
    p.feed(html)
    p.close()
    return p.blocks


def parse_date(text: str) -> date | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {int(m.group(2))} {m.group(3)}", "%B %d %Y").date()


@dataclass
class Item:
    category: str
    component: str
    text: str
    ids: list[str] = field(default_factory=list)
    cves: list[str] = field(default_factory=list)
    score: int = 0


@dataclass
class Release:
    version: str
    date: str
    title: str
    label: str = ""
    kind: str = "platform"
    url: str = ""
    components: dict[str, str] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    cves: list[str] = field(default_factory=list)
    detailed: bool = False

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.items:
            if i.category != "CVE":
                out[i.category] = out.get(i.category, 0) + 1
        out["CVE"] = len(self.cves)
        return out

    def controller_version(self) -> str:
        for k, v in self.components.items():
            if "controller" in k.lower():
                return v
        return ""


def _kind(text: str, label: str) -> str:
    return "portal" if "portal" in (text + " " + label).lower() else "platform"


def _label(text: str) -> str:
    m = DATE_RE.search(text)
    rest = text[m.end():] if m else text
    p = re.search(r"\(([^)]+)\)", rest)
    return p.group(1).strip() if p else ""


def _is_release_title(text: str) -> bool:
    t = text.lower()
    return bool(DATE_RE.search(text)) and ("elease" in t or "update" in t or "errata" in t)


def parse_index(html: str, base_url: str, version: str) -> list[Release]:
    """Index page (2.6, 2.7): one link per release."""
    out = _index_releases(html_blocks(html), base_url, version)
    if not out:
        out = _index_releases(html_blocks(html, include_nav=True), base_url, version)
    return sorted(out.values(), key=lambda r: r.date, reverse=True)


def _index_releases(blocks: list[Block], base_url: str, version: str) -> dict[str, Release]:
    out: dict[str, Release] = {}
    for b in blocks:
        candidates = list(b.links)
        if b.kind in ("li", "p", "dt") and _is_release_title(b.text) and b.links:
            candidates.append((b.text, b.links[0][1]))
        for text, href in candidates:
            if not href or href.startswith("#") or not _is_release_title(text):
                continue
            url = urljoin(base_url, href)
            if url in out:
                continue
            d = parse_date(text)
            label = _label(text)
            out[url] = Release(version=version, date=d.isoformat(), title=_norm(text), label=label,
                               kind=_kind(text, label), url=url)
    return out


COMPONENT_NAME_RE = re.compile(r"controller|hub|event|installer|receptor|gateway|platform|eda|operator", re.I)
COMPONENT_VERSION_RE = re.compile(
    r"((?:Automation controller|Automation hub|Event-Driven Ansible|Receptor|Platform gateway|Automation gateway|"
    r"Ansible Automation Platform Operator|(?:Container|RPM)-based installer[A-Za-z ,()]*?|Automation portal)"
    r"\s*:?)\s+v?(\d+(?:\.\d+)*(?:-[\w.]+)?)\b")


def _category(text: str) -> str | None:
    t = re.sub(r"^[\d.]+\s*", "", text.lower()).strip(" :")
    return CATEGORY_WORDS.get(t)


def parse_details(blocks: list[Block], rel: Release) -> Release:
    """Fill component versions and items of a release from its blocks."""
    category, component = "General", ""
    for b in blocks:
        if b.kind.startswith("h"):
            cat = _category(b.text)
            if cat:
                category, component = cat, ""
            elif not DATE_RE.search(b.text):
                component = re.sub(r"^[\d.]+\s*", "", b.text).strip()
            continue
        if b.kind == "row":
            if len(b.cells) >= 2:
                name, ver = b.cells[0], b.cells[1]
                if re.search(r"\d", ver) and COMPONENT_NAME_RE.search(name) and len(ver) < 40:
                    rel.components[name] = ver
            for cell in b.cells:
                for name, ver in COMPONENT_VERSION_RE.findall(cell):
                    rel.components.setdefault(_norm(name), ver)
            continue
        if b.kind not in ("li", "p", "dd"):
            continue
        ids, cves = AAP_ID_RE.findall(b.text), CVE_RE.findall(b.text)
        if not ids and not cves and b.kind == "li" and len(b.text) < 120:
            m = COMPONENT_VERSION_RE.fullmatch(b.text.strip())
            if m:
                rel.components.setdefault(_norm(m.group(1)).rstrip(":"), m.group(2))
                continue
        if not ids and not cves:
            if category in ("Overview", "Highlights", "Known issues", "Deprecated") and b.kind == "li":
                rel.items.append(Item(category, component, b.text[:600]))
            continue
        if category == "CVE" or (cves and not ids):
            for c in cves:
                if c not in rel.cves:
                    rel.cves.append(c)
            if category == "CVE":
                continue
        rel.items.append(Item(category, component, b.text[:600], sorted(set(ids)), sorted(set(cves))))
    seen, uniq = set(), []
    for i in rel.items:
        key = (i.category, i.text)
        if key not in seen:
            seen.add(key)
            uniq.append(i)
    rel.items = uniq
    if rel.controller_version():
        rel.kind = "platform"
    rel.detailed = True
    return rel


def parse_single_page(html: str, base_url: str, version: str) -> list[Release]:
    """2.5 layout: one page, a section per release ("10.1. ... patch release <date>")."""
    blocks = html_blocks(html)
    starts = [i for i, b in enumerate(blocks) if b.kind in ("h2", "h3") and _is_release_title(b.text)]
    out = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(blocks)
        head = blocks[i]
        d = parse_date(head.text)
        title = re.sub(r"^[\d.]+\s*", "", head.text)
        rel = Release(version=version, date=d.isoformat(), title=title, label=_label(head.text),
                      kind=_kind(head.text, ""), url=base_url)
        parse_details(blocks[i + 1:end], rel)
        if not rel.label:
            inst = {k.lower(): v for k, v in rel.components.items() if "container" in k.lower()}
            rel.label = (next((v for k, v in inst.items() if "bundle" in k), "")
                         or next((v for k, v in inst.items() if "online" in k), ""))
        out.append(rel)
    return sorted(out, key=lambda r: r.date, reverse=True)


DEFAULT_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Safari/537.36")


def http_headers() -> dict[str, str]:
    return {"User-Agent": (os.environ.get("AAP_RELEASE_NOTES_USER_AGENT") or "").strip() or DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"}


COPY_PREFIX = "release-notes"


def page_name(url: str) -> str:
    """File name of a page in the cache, in the bucket and in the copy script."""
    return hashlib.sha256(url.encode()).hexdigest()[:24] + ".html"


class Fetcher:
    """Reads the documentation pages: disk cache, docs.redhat.com, then the copy in the bucket.

    When the cluster cannot read docs.redhat.com (e.g. the CDN refuses the cloud egress IP
    with HTTP 403), cluster/10-copilot/06-copy-release-notes.sh downloads the pages from a
    workstation and copies them to the cache and to the bucket (prefix release-notes/).
    AAP_RELEASE_NOTES_SOURCE: "web" (default: docs.redhat.com, copies as fallback) or "copy"
    (copies only, no request to docs.redhat.com)."""

    def __init__(self, cache_dir: Path, ttl_hours: float | None = None, timeout: float = 30, store=None,
                 prefix: str = COPY_PREFIX):
        self.cache_dir = cache_dir
        self.ttl = 3600 * (ttl_hours if ttl_hours is not None else
                           float(os.environ.get("AAP_RELEASE_NOTES_CACHE_HOURS") or 6))
        self.timeout = timeout
        self.store = store
        self.prefix = prefix.strip("/")
        self.web = (os.environ.get("AAP_RELEASE_NOTES_SOURCE") or "web").strip().lower() != "copy"
        self.read: dict[str, dict] = {}

    def _meta(self, f: Path) -> dict:
        m = f.with_suffix(".json")
        try:
            return json.loads(m.read_text(encoding="utf-8")) if m.is_file() else {}
        except ValueError:
            return {}

    def _use(self, url: str, f: Path) -> str:
        meta = self._meta(f)
        self.read[url] = {"by": meta.get("by") or "docs.redhat.com",
                          "fetched": meta.get("fetched") or time.strftime(
                              "%Y-%m-%dT%H:%M:%S", time.localtime(f.stat().st_mtime))}
        return f.read_text(encoding="utf-8")

    def _from_store(self, f: Path) -> bool:
        if self.store is None:
            return False
        try:
            key = f"{self.prefix}/{f.name}"
            if not self.store.exists(key):
                return False
            self.store.get_file(key, f)
            mk = f"{self.prefix}/{f.with_suffix('.json').name}"
            if self.store.exists(mk):
                self.store.get_file(mk, f.with_suffix(".json"))
            return True
        except Exception as exc:
            log.warning("cannot read the copy of %s from the bucket: %s", f.name, exc)
            return False

    def get(self, url: str) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        f = self.cache_dir / page_name(url)
        if f.is_file() and (not self.web or time.time() - f.stat().st_mtime < self.ttl):
            return self._use(url, f)
        if not self.web:
            if self._from_store(f):
                return self._use(url, f)
            raise FileNotFoundError(f"no copy of {url} (AAP_RELEASE_NOTES_SOURCE=copy): run "
                                    "cluster/10-copilot/06-copy-release-notes.sh from a workstation")
        try:
            import httpx

            r = httpx.get(url, timeout=self.timeout, follow_redirects=True, headers=http_headers())
            if r.status_code == 403:
                raise PermissionError(f"docs.redhat.com refused the request (HTTP 403) for {url}: the site "
                                      "blocks this client or the cluster egress IP. Copy the pages from a "
                                      "workstation with cluster/10-copilot/06-copy-release-notes.sh "
                                      "(guide, troubleshooting)")
            r.raise_for_status()
            f.write_text(r.text, encoding="utf-8")
            f.with_suffix(".json").write_text(json.dumps({"url": url, "by": "docs.redhat.com",
                                                          "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}))
            return self._use(url, f)
        except Exception as exc:
            if f.is_file() or self._from_store(f):
                log.warning("using the copy of %s: %s", url, exc)
                return self._use(url, f)
            raise


def fetcher_for(cache_dir: Path, s3_settings=None) -> Fetcher:
    """Fetcher with the bucket copy as fallback when S3 is configured."""
    store = None
    if s3_settings is not None and getattr(s3_settings, "configured", False):
        try:
            from migration_ai.storage import Store

            store = Store(s3_settings)
        except Exception as exc:
            log.warning("bucket not available for the release notes copy: %s", exc)
    return Fetcher(cache_dir, store=store)


def urls() -> dict[str, str]:
    out = dict(DEFAULT_URLS)
    extra = (os.environ.get("AAP_RELEASE_NOTES_URLS") or "").strip()
    if extra:
        out.update(json.loads(extra))
    return out


def list_releases(version: str, fetcher: Fetcher) -> list[Release]:
    url = urls().get(version)
    if not url:
        raise ValueError(f"unknown AAP version '{version}' (known: {', '.join(sorted(urls()))})")
    html = fetcher.get(url)
    if "/html/" in url or version == "2.5":
        rels = parse_single_page(html, url, version)
        if rels:
            return rels
    return parse_index(html, url, version)


def ensure_details(rel: Release, fetcher: Fetcher) -> Release:
    if rel.detailed or not rel.url:
        return rel
    try:
        return parse_details(html_blocks(fetcher.get(rel.url)), rel)
    except Exception as exc:
        log.warning("release notes of %s not available: %s", rel.url, exc)
        return rel


def select(releases: list[Release], today: date, last: int = 3) -> tuple[str, list[Release]]:
    """The user's rule: releases after today; if none, the last `last` platform releases up to today."""
    future = [r for r in releases if r.day > today]
    if future:
        return "future", sorted(future, key=lambda r: r.date)
    published = [r for r in releases if r.day <= today]
    platform = [r for r in published if r.kind == "platform"][:last]
    if not platform:
        return "latest", published[:last]
    return "latest", [r for r in published if r.date >= platform[-1].date]


def newer_than_installed(releases: list[Release], controller_version: str) -> tuple[Release | None, list[Release]]:
    """Release matching the installed controller version, and the platform releases after it."""
    installed = next((r for r in releases if r.kind == "platform" and r.controller_version() == controller_version),
                     None)
    if not installed:
        return None, []
    return installed, [r for r in releases if r.kind == "platform" and r.date > installed.date]


def score(item: Item) -> int:
    t = item.text.lower()
    s = sum(w for pat, w in RELEVANCE if re.search(pat, t))
    if item.category in ("Bug fixes", "Known issues"):
        s += 1
    if any(tp in (item.component or "").lower() for tp in TECH_PREVIEW):
        s = 0
    return s


def relevant(rel: Release, top: int = 6) -> list[Item]:
    for i in rel.items:
        i.score = score(i)
    return sorted((i for i in rel.items if i.score >= 3), key=lambda i: -i.score)[:top]


def installed_version(target_url: str, verify: bool | str = True) -> dict:
    """Controller version of the target AAP from the anonymous ping endpoint."""
    if not target_url:
        return {"error": "TARGET__URL not set (secret aap-bridge-env)"}
    root = target_url.rstrip("/").split("/api/")[0]
    try:
        import httpx

        r = httpx.get(root + "/api/controller/v2/ping/", timeout=15, verify=verify)
        r.raise_for_status()
        data = r.json()
        return {"url": root, "controller_version": data.get("version"), "active_node": data.get("active_node")}
    except Exception as exc:
        return {"url": root, "error": f"{type(exc).__name__}: {exc}"}


CONTROLLER_TO_AAP = {"4.6": "2.5", "4.7": "2.6"}


def _minor(v: str) -> str:
    m = re.match(r"\s*(\d+)\.(\d+)", v or "")
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def aap_version_for(controller_version: str, fetcher: Fetcher | None = None, scan: int = 3) -> str:
    """AAP version (e.g. "2.6") of an installed controller version (e.g. "4.7.3"); "" if unknown."""
    mm = _minor(controller_version)
    if not mm:
        return ""
    if mm in CONTROLLER_TO_AAP and CONTROLLER_TO_AAP[mm] in urls():
        return CONTROLLER_TO_AAP[mm]
    if fetcher is None:
        return ""
    for version in sorted(urls(), key=lambda v: [int(x) for x in re.findall(r"\d+", v)], reverse=True):
        try:
            for r in [r for r in list_releases(version, fetcher) if r.kind == "platform"][:scan]:
                if _minor(ensure_details(r, fetcher).controller_version()) == mm:
                    return version
        except Exception as exc:
            log.warning("cannot read AAP %s release notes: %s", version, exc)
    return ""


def build_plan(version: str, fetcher: Fetcher, today: date | None = None, controller_version: str = "",
               last: int = 3, scan_installed: int = 12) -> dict[str, Any]:
    today = today or date.today()
    releases = list_releases(version, fetcher)
    rule, chosen = select(releases, today, last)
    installed, newer = None, []
    if controller_version:
        for r in [r for r in releases if r.kind == "platform"][:scan_installed]:
            ensure_details(r, fetcher)
        installed, newer = newer_than_installed(releases, controller_version)
    wanted = {r.url: r for r in chosen + newer}
    for r in wanted.values():
        ensure_details(r, fetcher)
    def view(r: Release) -> dict:
        d = asdict(r)
        d.pop("items")
        d["counts"] = r.counts()
        d["relevant"] = [asdict(i) for i in relevant(r)]
        d["tech_preview_cves"] = sum(1 for i in r.items if any(tp in i.component.lower() for tp in TECH_PREVIEW))
        return d
    index_read = fetcher.read.get(urls()[version], {})
    return {
        "version": version, "today": today.isoformat(), "source": urls()[version],
        "read_from": index_read.get("by") or "docs.redhat.com", "read_at": index_read.get("fetched") or "",
        "rule": rule, "total_listed": len(releases),
        "selected": [view(r) for r in chosen],
        "installed": ({"controller_version": controller_version, "release": installed.title if installed else None,
                       "label": installed.label if installed else None} if controller_version else None),
        "newer_than_installed": [view(r) for r in newer],
    }


def plan_markdown(plan: dict) -> str:
    """Deterministic patch-scheduling summary (no language model)."""
    v = plan["version"]
    lines = [f"**AAP {v} patch releases** — source: [Red Hat documentation]({plan['source']}), read {plan['today']}.", ""]
    if plan.get("read_from") and plan["read_from"] != "docs.redhat.com":
        lines += [f"*Release notes copied from docs.redhat.com on {str(plan.get('read_at'))[:16].replace('T', ' ')}: "
                  "releases published after that date are not listed yet.*", ""]
    if plan.get("note"):
        lines += [f"*Note: {plan['note']}.*", ""]
    if plan["rule"] == "future":
        lines.append("Releases announced after today:")
    else:
        n = sum(1 for r in plan["selected"] if r["kind"] == "platform")
        lines.append(f"No release dated after today: the last {n} platform releases"
                     + (" and the automation portal releases of the same period." if n < len(plan["selected"]) else "."))
    lines += ["", "| Date | Release | Kind | Controller | CVEs | Bug fixes | Enhancements |", "|---|---|---|---|---:|---:|---:|"]
    for r in plan["selected"]:
        c = r["counts"]
        lines.append(f"| {r['date']} | [{r['label'] or r['title']}]({r['url']}) | {r['kind']} | "
                     f"{r['components'].get(next((k for k in r['components'] if 'controller' in k.lower()), ''), '') or '—'} | "
                     f"{c.get('CVE', 0)} | {c.get('Bug fixes', 0)} | {c.get('Enhancements', 0) + c.get('Features', 0)} |")
    inst = plan.get("installed")
    if inst:
        lines += [""]
        if inst.get("release"):
            newer = plan["newer_than_installed"]
            lines.append(f"**Installed:** AAP {v}, controller {inst['controller_version']} = release **{inst['label']}**. "
                         + (f"**{len(newer)} newer platform release(s)** to plan." if newer else "Up to date."))
        else:
            lines.append(f"**Installed:** controller {inst['controller_version']} (no matching release among the "
                         "most recent ones).")
    focus = plan["newer_than_installed"] or [r for r in plan["selected"] if r["kind"] == "platform"]
    rel_lines = []
    for r in focus:
        for i in r["relevant"]:
            ids = [x for x in (i["ids"] or i["cves"][:3]) if x not in i["text"][:220]]
            rel_lines.append(f"* **{r['label'] or r['date']}** — {i['component'] or i['category']}: "
                             f"{i['text'][:220]}" + (f" ({', '.join(ids)})" if ids else ""))
    if rel_lines:
        lines += ["", "**Relevant to the migration** (selected by keywords: migration, RBAC, credentials, "
                  "authentication, organizations…):", ""] + rel_lines[:10]
    lines += ["", "**Before patching:** back up the target AAP, read the full notes of each release, update the operator "
              "in a maintenance window, then run the platform smoke test suite and compare it with the baseline. "
              "CVE severity is not in the release notes: check it on the Red Hat CVE pages."]
    return "\n".join(lines)


QUESTION_PROMPT = """

The user asked: {question}
The factual answer was already computed from the release notes and is shown above your note:
{direct}
Never restate, change or contradict it (computed_state in the JSON is authoritative). Add only what
helps the decision: priority and the items relevant to the migration."""

SUMMARY_PROMPT = """You are the Smart Planning agent of an AAP migration. From the JSON facts (parsed from the
Red Hat patch release notes) write a SHORT patch-scheduling note in English Markdown, at most 150 words:
- current state (installed release) and which releases to apply;
- at most 3 bullets "relevant to your migration", each citing its AAP-xxxxx or CVE id exactly as given;
- priority (high / medium / low) in one sentence; CVE severity is not in the release notes: say it must be
  checked on the Red Hat CVE pages, never invent it.
The full table of releases is shown below your note: do not repeat it. Use ONLY the facts in the JSON;
never invent versions, dates, ids or features."""


def compact_facts(plan: dict, question: str = "") -> dict:
    """The facts the LLM needs, small enough for a fast answer (the full plan stays in the page)."""
    with_cves = bool(re.search(r"\bcves?\b|security|vulnerab", question or "", re.I))

    def rel(r: dict) -> dict:
        comp = r.get("components") or {}
        out = {"date": r["date"], "release": r.get("label") or r.get("title"), "kind": r["kind"],
               "controller": next((v for k, v in comp.items() if "controller" in k.lower()), ""),
               "counts": r.get("counts"),
               "relevant": [{"ids": (i.get("ids") or i.get("cves") or [])[:3], "component": i.get("component"),
                             "text": (i.get("text") or "")[:220]} for i in (r.get("relevant") or [])[:4]]}
        if with_cves:
            out["cves"] = (r.get("cves") or [])[:25]
        return out

    newer = plan.get("newer_than_installed") or []
    chosen = newer or [r for r in plan.get("selected") or [] if r["kind"] == "platform"]
    inst = plan.get("installed") or {}
    return {"aap_version": plan.get("version"), "today": plan.get("today"), "installed": plan.get("installed"),
            "computed_state": {"installed_release": inst.get("label"), "up_to_date": not newer if inst.get("release")
                               else None, "newer_platform_releases": [r.get("label") or r["date"] for r in newer]},
            "releases_newer_than_installed" if newer else "latest_platform_releases": [rel(r) for r in chosen[:4]],
            "portal_releases": [{"date": r["date"], "release": r.get("label")} for r in plan.get("selected") or []
                                if r["kind"] == "portal"][:3]}


ID_RE = re.compile(r"\bAAP-\d+\b|\bCVE-\d{4}-\d{4,7}\b", re.I)
LABEL_RE = re.compile(r"\b\d\.\d+-\d+(?:\.\d+)?\b")
STATE_Q = re.compile(r"up to date|newer|latest|last|should i patch|need to patch|do i need|before migrating|"
                     r"which releases?|what releases?", re.I)
CVE_Q = re.compile(r"\bcves?\b|security|vulnerab", re.I)


def find_ids(version: str, fetcher: "Fetcher", ids: list[str]) -> dict[str, list[dict]]:
    """Where each AAP-xxxxx / CVE id appears in the release notes of a version (all releases read)."""
    want = {i.upper() for i in ids}
    found: dict[str, list[dict]] = {i: [] for i in want}
    for r in list_releases(version, fetcher):
        ensure_details(r, fetcher)
        for it in r.items:
            for i in set(it.ids + it.cves) & want:
                found[i].append({"release": r.label or r.title, "date": r.date, "kind": r.kind,
                                 "component": it.component or it.category, "text": it.text[:200]})
        for c in set(r.cves) & want:
            if not any(x["release"] == (r.label or r.title) for x in found[c]):
                found[c].append({"release": r.label or r.title, "date": r.date, "kind": r.kind,
                                 "component": "CVE", "text": ""})
    return found


def _rel_name(r: dict) -> str:
    return f"{r.get('label') or ('portal' if r.get('kind') == 'portal' else 'patch') + ' release'} ({r['date']})"


def drop_repeats(note: str, direct: str) -> str:
    """Remove from the LLM note the paragraphs that repeat the answer computed by code."""
    seen = {re.sub(r"\W+", " ", x).strip().lower() for x in direct.split("\n") if x.strip()}
    keep = [x for x in note.split("\n")
            if not (re.sub(r"\W+", " ", x).strip().lower() in seen
                    or re.match(r"\s*\**\s*(not )?up to date\b", x, re.I))]
    return "\n".join(keep).strip()


def direct_answer(plan: dict, question: str, found: dict[str, list[dict]] | None = None) -> str:
    """The factual part of the answer, computed from the parsed release notes (never by the LLM)."""
    v = plan.get("version")
    lines: list[str] = []
    if found:
        for i, hits in sorted(found.items()):
            if not hits:
                lines.append(f"**{i}** does not appear in the AAP {v} release notes ({plan.get('total_listed', 0)} "
                             "releases read). Check the id, or ask for another AAP version.")
            else:
                h = hits[-1]
                lines.append(f"**{i}** is listed in **{h['release']}** ({h['date']}, {h['kind']} release), "
                             f"{h['component']}" + (f": {h['text']}" if h["text"] else "") +
                             (f" — also in {', '.join(x['release'] for x in hits[:-1])}" if len(hits) > 1 else ""))
        return "\n\n".join(lines)
    inst, newer = plan.get("installed"), plan.get("newer_than_installed") or []
    if STATE_Q.search(question or "") or CVE_Q.search(question or ""):
        if inst and inst.get("release"):
            if newer:
                lines.append(f"**Not up to date.** Installed: AAP {v} release **{inst['label']}** (controller "
                             f"{inst['controller_version']}). {len(newer)} newer platform release(s): "
                             + ", ".join(f"**{_rel_name(r)}**" for r in newer) + ".")
            else:
                lines.append(f"**Up to date.** Installed: AAP {v} release **{inst['label']}** (controller "
                             f"{inst['controller_version']}), the latest platform release published.")
        elif inst:
            lines.append(f"Installed controller {inst['controller_version']}: no matching release among the recent "
                         f"AAP {v} releases, so the newer releases cannot be counted.")
        else:
            lines.append(f"Installed version of the target AAP not known: showing the latest AAP {v} releases.")
    if CVE_Q.search(question or ""):
        rels = newer or [r for r in plan.get("selected") or [] if r["kind"] == "platform"]
        for r in rels:
            cves = r.get("cves") or []
            lines.append(f"* **{_rel_name(r)}**: {len(cves)} CVEs" + (": " + ", ".join(cves[:12]) +
                                                                        (", …" if len(cves) > 12 else "") if cves else ""))
        if rels:
            lines.append("CVE severity is not in the release notes: check it on the Red Hat CVE pages.")
    return "\n\n".join(lines[:1]) + ("\n\n" + "\n".join(lines[1:]) if len(lines) > 1 else "")


def check_summary(summary: str, allowed_text: str, plan: dict) -> list[str]:
    """Facts in the LLM note that are not in the release notes, or that contradict them ([] = accepted)."""
    allowed = allowed_text.upper()
    bad = sorted({x for x in ID_RE.findall(summary) if x.upper() not in allowed})
    bad += sorted({x for x in LABEL_RE.findall(summary) if x not in allowed_text})
    newer = plan.get("newer_than_installed") or []
    if newer and re.search(r"(?<!not )\b(is|are|already)\s+(already\s+)?up[ -]to[ -]date\b|do not need to patch|"
                           r"no need to patch", summary, re.I):
        bad.append(f"says up to date, but {len(newer)} newer platform release(s) exist")
    return bad
