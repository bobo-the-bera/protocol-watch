#!/usr/bin/env python3
"""Archive every observable protocol-doc delta; classify relevance later."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "watch.yaml"
DEFAULT_SNAPSHOT_DIR = ROOT / "snapshots"
USER_AGENT = "protocol-watch/3.0 (+hourly public documentation diffing)"


@dataclass(frozen=True)
class Target:
    protocol: str
    slug: str
    url: str
    mode: str = "html"
    tags: tuple[str, ...] = ()
    ignore_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrawlTarget:
    protocol: str
    slug: str
    sitemap: str
    include_prefixes: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    max_pages: int = 500


@dataclass(frozen=True)
class RepoTarget:
    protocol: str
    slug: str
    repo: str
    tags: tuple[str, ...] = ()


def load_config(path: Path) -> tuple[list[Target], list[CrawlTarget], list[RepoTarget], tuple[str, ...]]:
    """Keep the watch surface declarative so breadth can grow without changing code."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    global_ignores = tuple(raw.get("global_ignore_patterns", []))

    targets = [
        Target(
            protocol=str(item["protocol"]).strip().lower(),
            slug=str(item["slug"]).strip().lower(),
            url=str(item["url"]).strip(),
            mode=str(item.get("mode", "html")).strip().lower(),
            tags=tuple(item.get("tags", [])),
            ignore_patterns=tuple(item.get("ignore_patterns", [])),
        )
        for item in raw.get("targets", [])
    ]

    crawls = [
        CrawlTarget(
            protocol=str(item["protocol"]).strip().lower(),
            slug=str(item["slug"]).strip().lower(),
            sitemap=str(item["sitemap"]).strip(),
            include_prefixes=tuple(item.get("include_prefixes", [])),
            exclude_patterns=tuple(item.get("exclude_patterns", [])),
            tags=tuple(item.get("tags", [])),
            max_pages=int(item.get("max_pages", 500)),
        )
        for item in raw.get("crawls", [])
    ]

    repos = [
        RepoTarget(
            protocol=str(item["protocol"]).strip().lower(),
            slug=str(item["slug"]).strip().lower(),
            repo=str(item["repo"]).strip(),
            tags=tuple(item.get("tags", [])),
        )
        for item in raw.get("repositories", [])
    ]
    return targets, crawls, repos, global_ignores


def fetch(url: str, retries: int = 3, timeout: int = 25) -> requests.Response:
    """Retry transient failures so a flaky page does not silently become a fake deletion."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,text/plain,application/xml,text/xml,application/json;q=0.9,*/*;q=0.8",
                },
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def extract_html_text(html: str, base_url: str | None = None, include_scripts: bool = False) -> str:
    """Preserve visible content plus link destinations; optionally fingerprint frontend bundles."""
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    script_srcs = []
    if include_scripts:
        script_srcs = sorted({urljoin(base_url or "", node.get("src")) for node in soup.select("script[src]") if node.get("src")})

    for node in soup.select("script, style, noscript, svg, nav, header, footer"):
        node.decompose()
    root = soup.select_one("article") or soup.select_one("main") or soup.select_one('[role="main"]') or soup.body or soup
    visible = root.get_text("\n", strip=True)

    # Href-only edits can be economically meaningful even when anchor text does not change.
    links = []
    for node in root.select("a[href]"):
        href = urljoin(base_url or "", node.get("href", "").strip())
        anchor = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if href:
            links.append(f"LINK | {anchor or '[no text]'} | {href}")

    sections = [visible]
    if links:
        sections.append("\n".join(["[[LINK TARGETS]]", *sorted(set(links))]))
    if script_srcs:
        sections.append("\n".join(["[[FRONTEND SCRIPT ASSETS]]", *[f"SCRIPT | {src}" for src in script_srcs]]))
    return "\n".join(section for section in sections if section).strip()


def normalize_text(text: str, ignore_patterns: Iterable[str]) -> str:
    """Remove only known volatile metadata; do not semantic-filter the source before diffing."""
    patterns = [re.compile(pattern, re.IGNORECASE) for pattern in ignore_patterns]
    kept: list[str] = []
    previous = None
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or any(pattern.search(line) for pattern in patterns):
            continue
        if line == previous:
            continue
        kept.append(line)
        previous = line
    return "\n".join(kept).strip() + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unified_diff(old: str, new: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{label}:previous",
            tofile=f"{label}:current",
            n=4,
        )
    )


def safe_url_slug(url: str) -> str:
    """Create readable stable filenames while avoiding collisions on long or similar paths."""
    parsed = urlparse(url)
    raw = (parsed.path.strip("/") or "index") + (f"__{parsed.query}" if parsed.query else "")
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "__", raw).strip("._-") or "index"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{clean[:150]}__{digest}"


def parse_sitemap(xml_text: str) -> tuple[str, list[str]]:
    """Return sitemap kind and every <loc>, namespace-agnostic."""
    root = ET.fromstring(xml_text)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    locs = [node.text.strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text]
    return kind, locs


def discover_sitemap_urls(crawl: CrawlTarget, max_sitemaps: int = 25) -> list[str]:
    """Traverse sitemap indexes, then keep every allowed page URL before relevance analysis."""
    pending = [crawl.sitemap]
    seen_maps: set[str] = set()
    pages: set[str] = set()
    excludes = [re.compile(pattern, re.IGNORECASE) for pattern in crawl.exclude_patterns]

    while pending:
        sitemap_url = pending.pop(0)
        if sitemap_url in seen_maps:
            continue
        if len(seen_maps) >= max_sitemaps:
            raise RuntimeError(f"too many sitemap files under {crawl.sitemap}; raise max_sitemaps intentionally")
        seen_maps.add(sitemap_url)
        response = fetch(sitemap_url)
        kind, locs = parse_sitemap(response.text)

        if kind == "sitemapindex":
            pending.extend(loc for loc in locs if loc not in seen_maps)
            continue
        if kind != "urlset":
            raise RuntimeError(f"unsupported sitemap root {kind!r} at {sitemap_url}")

        for url in locs:
            if crawl.include_prefixes and not any(url.startswith(prefix) for prefix in crawl.include_prefixes):
                continue
            if any(pattern.search(url) for pattern in excludes):
                continue
            pages.add(url)
            if len(pages) > crawl.max_pages:
                raise RuntimeError(
                    f"{crawl.slug} discovered >{crawl.max_pages} pages; raise max_pages intentionally instead of truncating silently"
                )

    return sorted(pages)


def snapshot_target(target: Target, snapshot_dir: Path, global_ignores: tuple[str, ...], namespace: str = "direct") -> dict:
    """Persist normalized text only when content actually differs from the previous baseline."""
    response = fetch(target.url)

    if target.mode == "text":
        extracted = response.text
    else:
        # GitBook exposes stable raw Markdown links in the rendered page.
        # Prefer those over dynamic HTML whenever available.
        from urllib.parse import urljoin
    
soup = BeautifulSoup(response.text, "html.parser")

# Only follow Markdown links hosted on the same documentation domain.
# This prevents "Edit on GitHub" .md links from being archived as GitHub HTML.
target_host = urlparse(target.url).netloc.lower()
markdown_url = None

for link in soup.select('a[href$=".md"]'):
    href = link.get("href")
    if not href:
        continue

    candidate = urljoin(target.url, href)

    if urlparse(candidate).netloc.lower() == target_host:
        markdown_url = candidate
        break

if markdown_url:
    markdown_response = fetch(markdown_url)
    markdown_body = markdown_response.text

    # Safety fallback in case a supposed .md endpoint actually returns HTML.
    stripped = markdown_body.lstrip().lower()

    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        extracted = extract_html_text(
            response.text,
            target.url,
            include_scripts="frontend-bundles" in target.tags,
        )
    else:
        extracted = markdown_body
else:
    extracted = extract_html_text(
        response.text,
        target.url,
        include_scripts="frontend-bundles" in target.tags,
    )
    
    normalized = normalize_text(extracted, (*global_ignores, *target.ignore_patterns))

    protocol_dir = snapshot_dir / "pages" / target.protocol / namespace
    protocol_dir.mkdir(parents=True, exist_ok=True)
    text_path = protocol_dir / f"{target.slug}.txt"
    meta_path = protocol_dir / f"{target.slug}.json"
    diff_path = protocol_dir / f"{target.slug}.latest.diff"

    old = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    changed = old != normalized
    digest = sha256_text(normalized)
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    if changed:
        text_path.write_text(normalized, encoding="utf-8")
        diff_path.write_text(unified_diff(old, normalized, f"{target.protocol}/{namespace}/{target.slug}"), encoding="utf-8")

    metadata = {
        "protocol": target.protocol,
        "slug": target.slug,
        "url": target.url,
        "tags": list(target.tags),
        "sha256": digest,
        "content_changed_at": fetched_at if changed else None,
    }
    if changed or not meta_path.exists():
        meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {"protocol": target.protocol, "slug": target.slug, "changed": changed, "sha256": digest}


def snapshot_crawl(crawl: CrawlTarget, snapshot_dir: Path, global_ignores: tuple[str, ...]) -> list[dict]:
    """Snapshot every sitemap-discovered page and version the URL inventory itself."""
    urls = discover_sitemap_urls(crawl)
    manifest_dir = snapshot_dir / "manifests" / crawl.protocol
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{crawl.slug}.json"
    manifest = {"protocol": crawl.protocol, "slug": crawl.slug, "sitemap": crawl.sitemap, "urls": urls}
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if not manifest_path.exists() or manifest_path.read_text(encoding="utf-8") != manifest_text:
        manifest_path.write_text(manifest_text, encoding="utf-8")

    results = []
    for url in urls:
        target = Target(
            protocol=crawl.protocol,
            slug=safe_url_slug(url),
            url=url,
            tags=crawl.tags,
        )
        results.append(snapshot_target(target, snapshot_dir, global_ignores, namespace=crawl.slug))
    return results


def snapshot_repo(target: RepoTarget, snapshot_dir: Path) -> dict:
    """Record default-branch head; native GitHub commits remain the exact code archive."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(f"https://api.github.com/repos/{target.repo}/commits?per_page=1", timeout=25, headers=headers)
    response.raise_for_status()
    payload = response.json()
    if not payload:
        raise RuntimeError(f"no commits returned for {target.repo}")

    commit = payload[0]
    current = {
        "protocol": target.protocol,
        "slug": target.slug,
        "repo": target.repo,
        "tags": list(target.tags),
        "sha": commit["sha"],
        "html_url": commit.get("html_url"),
        "message": commit.get("commit", {}).get("message", "").split("\n", 1)[0],
        "committed_at": commit.get("commit", {}).get("committer", {}).get("date"),
    }

    repo_dir = snapshot_dir / "repos" / target.protocol
    repo_dir.mkdir(parents=True, exist_ok=True)
    path = repo_dir / f"{target.slug}.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    changed = old is None or old.get("sha") != current["sha"]
    if changed:
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"protocol": target.protocol, "slug": target.slug, "changed": changed, "sha": current["sha"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive public protocol docs and repository heads.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--snapshots", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    parser.add_argument("--protocol", action="append", help="Optional protocol filter; repeatable.")
    args = parser.parse_args()

    targets, crawls, repos, global_ignores = load_config(args.config)
    selected = set(args.protocol or [])
    if selected:
        targets = [target for target in targets if target.protocol in selected]
        crawls = [crawl for crawl in crawls if crawl.protocol in selected]
        repos = [repo for repo in repos if repo.protocol in selected]

    failures: list[str] = []

    for crawl in crawls:
        try:
            results = snapshot_crawl(crawl, args.snapshots, global_ignores)
            changed = sum(1 for result in results if result["changed"])
            print(f"[CRAWL  ] {crawl.protocol}/{crawl.slug}: {len(results)} pages, {changed} changed")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"crawl {crawl.protocol}/{crawl.slug}: {exc}")
            print(f"[ERROR  ] crawl {crawl.protocol}/{crawl.slug}: {exc}", file=sys.stderr)

    for target in targets:
        try:
            result = snapshot_target(target, args.snapshots, global_ignores)
            print(f"[{'CHANGED' if result['changed'] else 'same':7}] {target.protocol}/{target.slug} {result['sha256'][:12]}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{target.protocol}/{target.slug}: {exc}")
            print(f"[ERROR  ] {target.protocol}/{target.slug}: {exc}", file=sys.stderr)

    for repo in repos:
        try:
            result = snapshot_repo(repo, args.snapshots)
            print(f"[{'CHANGED' if result['changed'] else 'same':7}] repo {repo.protocol}/{repo.slug} {result['sha'][:12]}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"repo {repo.protocol}/{repo.slug}: {exc}")
            print(f"[ERROR  ] repo {repo.protocol}/{repo.slug}: {exc}", file=sys.stderr)

    if failures:
        print(f"{len(failures)} target(s) failed; successful snapshots were preserved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
