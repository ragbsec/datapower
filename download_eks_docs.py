#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


GUIDES = {
    "userguide": {
        "base": "https://docs.aws.amazon.com/eks/latest/userguide/",
        "entry": "what-is-eks.html",
    },
    "best-practices": {
        "base": "https://docs.aws.amazon.com/eks/latest/best-practices/",
        "entry": "introduction.html",
    },
}

MD_LINKS = re.compile(
    r"\]\(\s*<?([^\s)>]+)"
    r"|^\s*\[[^\]]+\]:\s*<?([^\s>]+)"
    r"|href\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.MULTILINE,
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in ("a", "link"):
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def strings_in_json(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings_in_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in_json(item)


def page_url(url, base):
    parts = urlsplit(url)
    scope = urlsplit(base)

    if parts.scheme != "https" or parts.netloc != scope.netloc:
        return None

    if not parts.path.startswith(scope.path):
        return None

    path = parts.path
    if path.endswith(".md"):
        path = path[:-3] + ".html"
    elif not path.endswith(".html"):
        return None

    relative = Path(path[len(scope.path):])
    if ".." in relative.parts:
        return None

    return urlunsplit(("https", parts.netloc, path, "", ""))


def markdown_url(html_url):
    return html_url[:-5] + ".md"


def is_html(data, content_type):
    opening = data.lstrip()[:1000].lower()
    return (
        "text/html" in content_type.lower()
        or opening.startswith(b"<!doctype html")
        or opening.startswith(b"<html")
    )


def fetch(url, args):
    last_error = None

    for attempt in range(args.retries):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "EKS-Docs-Assessment-Downloader/2.0",
                    "Accept": "*/*",
                },
            )
            with urlopen(request, timeout=args.timeout) as response:
                body = response.read()
                metadata = {
                    "final_url": response.geturl(),
                    "content_type": response.headers.get("Content-Type", ""),
                    "last_modified": response.headers.get("Last-Modified"),
                    "etag": response.headers.get("ETag"),
                }

            if not body.strip():
                raise ValueError("Empty response")

            return body, metadata

        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_error = exc

            if isinstance(exc, HTTPError) and exc.code in (400, 401, 403, 404):
                break

            if attempt + 1 < args.retries:
                time.sleep(min(2 ** attempt, 20))

        finally:
            time.sleep(args.delay)

    raise RuntimeError(str(last_error))


def html_links(text):
    parser = HTMLLinks()
    parser.feed(text)
    return parser.links


def markdown_links(text):
    return [
        next(group for group in match.groups() if group is not None)
        for match in MD_LINKS.finditer(text)
    ]


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def download_guide(name, config, args):
    base = config["base"]
    output = args.output / name
    output.mkdir(parents=True, exist_ok=True)

    queue = deque()
    discovered = set()
    visited = set()
    toc_pages = set()
    records = []
    html_failures = []
    discovery_sources = []

    def enqueue(target, source, from_toc=False):
        resolved = page_url(urljoin(source, unescape(target)), base)
        if not resolved:
            return

        if from_toc:
            toc_pages.add(resolved)

        if resolved not in discovered:
            discovered.add(resolved)
            queue.append(resolved)

    enqueue(config["entry"], base)
    enqueue("index.html", base)

    if name == "userguide":
        for topic in [
            "automode.html",
            "create-node-pool.html",
            "auto-upgrade.html",
            "auto-change.html",
            "node-health.html",
            "node-repair.html",
            "rollback-automode.html",
        ]:
            enqueue(topic, base)
    else:
        enqueue("automode.html", base)

    toc_url = urljoin(base, "toc-contents.json")

    try:
        body, metadata = fetch(toc_url, args)
        document = json.loads(body.decode("utf-8-sig"))

        for target in strings_in_json(document):
            enqueue(target, base, from_toc=True)

        discovery_sources.append({
            "url": toc_url,
            "status": "loaded",
            "pages_discovered": len(toc_pages),
            "sha256": hashlib.sha256(body).hexdigest(),
            **metadata,
        })

    except Exception as exc:
        discovery_sources.append({
            "url": toc_url,
            "status": "unavailable",
            "error": str(exc),
        })
        print(f"[{name}] TOC unavailable; using link discovery: {exc}")

    manifest_path = output / "download-manifest.json"
    started_at = utc_now()

    def snapshot(finished=False):
        downloaded = {
            row["html_url"]
            for row in records
            if row["status"] == "downloaded"
        }

        return {
            "guide": name,
            "base_url": base,
            "started_at_utc": started_at,
            "updated_at_utc": utc_now(),
            "finished": finished,
            "scope": (
                "TOC-listed and recursively linked HTML/Markdown topics "
                "within this guide"
            ),
            "discovery_sources": discovery_sources,
            "discovered_pages": len(discovered),
            "processed_pages": len(visited),
            "downloaded_markdown": len(downloaded),
            "failed_markdown": sum(
                row["status"] == "failed" for row in records
            ),
            "remaining_queued_pages": len(queue),
            "toc_listed_pages": len(toc_pages),
            "toc_pages_without_download": sorted(toc_pages - downloaded),
            "html_discovery_failures": html_failures,
            "records": records,
        }

    while queue:
        html_url = queue.popleft()
        if html_url in visited:
            continue

        visited.add(html_url)
        md_url = markdown_url(html_url)
        fetched_at = utc_now()

        try:
            body, metadata = fetch(md_url, args)

            if is_html(body, metadata["content_type"]):
                raise ValueError("Received HTML instead of Markdown")

            if page_url(metadata["final_url"], base) is None:
                raise ValueError("Redirected outside the approved guide")

            text = body.decode("utf-8-sig")
            relative = urlsplit(md_url).path[len(urlsplit(base).path):]
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)

            records.append({
                "html_url": html_url,
                "markdown_url": md_url,
                "local_file": str(destination),
                "downloaded_at_utc": fetched_at,
                "status": "downloaded",
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                **metadata,
            })

            for target in markdown_links(text):
                enqueue(target, md_url)

            print(f"[{name}] Downloaded: {relative}")

        except Exception as exc:
            records.append({
                "html_url": html_url,
                "markdown_url": md_url,
                "checked_at_utc": fetched_at,
                "status": "failed",
                "error": str(exc),
            })
            print(f"[{name}] Markdown failed: {md_url}: {exc}")

        try:
            body, metadata = fetch(html_url, args)

            if page_url(metadata["final_url"], base) is None:
                raise ValueError("Redirected outside the approved guide")

            text = body.decode("utf-8-sig", errors="replace")
            for target in html_links(text):
                enqueue(target, html_url)

        except Exception as exc:
            html_failures.append({
                "url": html_url,
                "error": str(exc),
            })

        write_json(manifest_path, snapshot())

    result = snapshot(finished=True)
    write_json(manifest_path, result)

    with (output / "download-inventory.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        fields = [
            "status",
            "html_url",
            "markdown_url",
            "local_file",
            "downloaded_at_utc",
            "sha256",
            "bytes",
            "error",
        ]
        writer = csv.DictWriter(
            handle, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(records)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Download discoverable AWS EKS documentation Markdown."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("assessment-output/reference-docs/aws-eks"),
    )
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    if args.delay < 0 or args.timeout <= 0 or args.retries <= 0:
        parser.error("delay must be nonnegative; timeout/retries must be positive")

    args.output.mkdir(parents=True, exist_ok=True)
    summaries = {}

    for name, config in GUIDES.items():
        result = download_guide(name, config, args)
        summaries[name] = {
            key: result[key]
            for key in [
                "discovered_pages",
                "processed_pages",
                "downloaded_markdown",
                "failed_markdown",
                "toc_listed_pages",
                "toc_pages_without_download",
                "remaining_queued_pages",
            ]
        }
        summaries[name]["html_discovery_failure_count"] = len(
            result["html_discovery_failures"]
        )

    summary = {
        "completed_at_utc": utc_now(),
        "guides": summaries,
        "completeness_note": (
            "All discovered topics were attempted. Unlisted/unlinked pages "
            "cannot be guaranteed. Review per-guide discovery sources, "
            "failed downloads, and HTML discovery failures."
        ),
    }

    write_json(args.output / "download-summary.json", summary)
    print(json.dumps(summary, indent=2))

    failed = any(
        item["failed_markdown"] > 0
        or item["html_discovery_failure_count"] > 0
        for item in summaries.values()
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())