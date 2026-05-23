#!/usr/bin/env python3
"""
Выгрузка учреждений с moskva.fulledu.ru (sitemap + страницы /about/).

Примеры:
  python3 scripts/scrape_fulledu.py
  python3 scripts/scrape_fulledu.py --categories school sadik detskiy-otdyh
  python3 scripts/scrape_fulledu.py --resume   # продолжить прерванную выгрузку
  python3 scripts/scrape_fulledu.py --quick    # только URL и имя из slug (мгновенно)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import urllib.error
import urllib.request

BASE = "https://moskva.fulledu.ru"
SITEMAP = f"{BASE}/sitemap.xml"

CATEGORY_LABELS = {
    "school": "Школа",
    "sadik": "Детский сад",
    "camp": "Лагерь",
    "detskiy-otdyh": "Детский отдых / лагерь",
    "kolledj": "Колледж",
    "vuzi": "Вуз",
    "course": "Курс",
    "sekcii": "Секция",
}

DEFAULT_CATEGORIES = ("school", "sadik", "detskiy-otdyh", "camp", "kolledj", "vuzi", "course")

CSV_FIELDS = [
    "category",
    "category_label",
    "name",
    "legal_name",
    "town",
    "url",
]

USER_AGENT = "Mozilla/5.0 (compatible; FulleduExport/1.0)"


@dataclass
class Institution:
    category: str
    category_label: str
    name: str
    legal_name: str
    url: str
    town: str

    def row(self) -> dict[str, str]:
        return {
            "category": self.category,
            "category_label": self.category_label,
            "name": self.name,
            "legal_name": self.legal_name,
            "town": self.town,
            "url": self.url,
        }


def fetch(url: str, timeout: int = 45, max_bytes: int = 350_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        size = 0
        while True:
            part = resp.read(65536)
            if not part:
                break
            chunks.append(part)
            size += len(part)
            if size >= max_bytes:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")


def load_sitemap_urls(sitemap_url: str = SITEMAP) -> list[str]:
    xml = fetch(sitemap_url, max_bytes=5_000_000)
    root = ET.fromstring(xml)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [el.text.strip() for el in root.findall(".//sm:loc", ns) if el.text]


def filter_institution_urls(
    urls: Iterable[str], categories: tuple[str, ...]
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for url in urls:
        if not url.startswith(BASE):
            continue
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) < 3 or parts[-1] != "about":
            continue
        cat = parts[0]
        if cat not in categories:
            continue
        if parts[1] in {"town", "okrug"}:
            continue
        out.append((cat, url if url.endswith("/") else url + "/"))
    return out


def slug_to_name(slug: str) -> str:
    slug = re.sub(r"-\d+$", "", slug)
    return slug.replace("-", " ").strip()


def institution_from_url(category: str, url: str, *, quick: bool) -> Institution:
    slug = urlparse(url).path.strip("/").split("/")[1]
    name = slug_to_name(slug) if quick else ""
    return Institution(
        category=category,
        category_label=CATEGORY_LABELS.get(category, category),
        name=name,
        legal_name=name,
        url=url,
        town="",
    )


def _walk_json_ld(node: object) -> list[dict]:
    if isinstance(node, list):
        items: list[dict] = []
        for x in node:
            items.extend(_walk_json_ld(x))
        return items
    if isinstance(node, dict):
        return [node]
    return []


def parse_institution(html: str, category: str, url: str) -> Institution:
    inst = institution_from_url(category, url, quick=False)
    name = ""
    legal_name = ""
    town = ""

    for block in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in _walk_json_ld(data):
            t = item.get("@type")
            types = t if isinstance(t, list) else [t]
            if "EducationalOrganization" in types:
                legal_name = item.get("legalName") or item.get("name") or legal_name
                addr = item.get("address") or {}
                if isinstance(addr, dict):
                    town = addr.get("addressLocality") or town
            if t == "WebPage" and item.get("name"):
                name = item.get("name") or name

    if not name:
        h1 = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if h1:
            name = h1.group(1).strip()

    if not name:
        title = re.search(r"<title>([^<]+)</title>", html)
        if title:
            name = re.sub(r"\s+в\s+Москве.*$", "", title.group(1)).strip()

    if not name:
        name = slug_to_name(urlparse(url).path.strip("/").split("/")[1])

    inst.name = name.strip()
    inst.legal_name = (legal_name or name).strip()
    inst.town = town.strip()
    return inst


def scrape_one(item: tuple[str, str], *, quick: bool) -> Institution | None:
    category, url = item
    if quick:
        return institution_from_url(category, url, quick=True)
    try:
        html = fetch(url)
        inst = parse_institution(html, category, url)
        return inst if inst.name else None
    except Exception as exc:  # noqa: BLE001
        print(f"WARN {url}: {exc}", file=sys.stderr)
        return None


def load_done_urls(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    done: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("url"):
                done.add(row["url"].rstrip("/") + "/")
    return done


class IncrementalCsv:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._has_header = path.is_file() and path.stat().st_size > 0

    def append(self, inst: Institution) -> None:
        with self.lock:
            new_file = not self._has_header
            with self.path.open("a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if new_file:
                    w.writeheader()
                    self._has_header = True
                w.writerow(inst.row())


def finalize_csv(path: Path) -> int:
    if not path.is_file():
        return 0
    by_url: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("url"):
                continue
            u = row["url"].rstrip("/") + "/"
            clean = {k: row.get(k, "") for k in CSV_FIELDS}
            prev = by_url.get(u)
            if not prev or len(clean.get("legal_name", "")) > len(prev.get("legal_name", "")):
                by_url[u] = clean
    rows = sorted(by_url.values(), key=lambda r: (r["category"], r["name"].lower()))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Выгрузка fulledu.ru (Москва)")
    parser.add_argument("--categories", nargs="+", default=list(DEFAULT_CATEGORIES))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/fulledu_moskva_institutions.csv"),
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--resume", action="store_true", help="Пропустить URL из существующего CSV")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Без HTTP к карточкам — имя из URL (быстро, менее точно)",
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    categories = tuple(args.categories)
    print(f"Загрузка sitemap: {SITEMAP}")
    items = filter_institution_urls(load_sitemap_urls(), categories)

    if args.resume:
        done = load_done_urls(args.output)
        before = len(items)
        items = [(c, u) for c, u in items if u not in done]
        print(f"Resume: пропущено {before - len(items)}, осталось {len(items)}")

    if args.limit:
        items = items[: args.limit]

    by_cat: dict[str, int] = {}
    for cat, _ in items:
        by_cat[cat] = by_cat.get(cat, 0) + 1
    print(f"К выгрузке: {len(items)} страниц")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")

    if not items:
        n = finalize_csv(args.output)
        print(f"Нечего качать. В файле уже {n} записей: {args.output.resolve()}")
        return

    if not args.resume and args.output.is_file():
        args.output.unlink()

    writer = IncrementalCsv(args.output)
    started = time.time()
    ok = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(scrape_one, it, quick=args.quick): it for it in items
        }
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            done += 1
            inst = fut.result()
            if inst:
                writer.append(inst)
                ok += 1
            else:
                fail += 1
            if done % 200 == 0 or done == total:
                elapsed = time.time() - started
                rate = done / elapsed if elapsed else 0
                print(
                    f"  {done}/{total} | сохранено {ok} | ошибок {fail} | "
                    f"{rate:.1f} стр/с | {elapsed:.0f}s"
                )

    total_rows = finalize_csv(args.output)
    print(f"Готово: {total_rows} записей -> {args.output.resolve()}")


if __name__ == "__main__":
    main()
