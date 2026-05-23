#!/usr/bin/env python3
"""
Находит официальный сайт учреждения через поиск (DuckDuckGo / ddgs).

Не использует ссылки с fulledu.ru — только название + город + тип.

Запуск (из корня репозитория):
  python3 -m venv .venv && .venv/bin/pip install -r requirements-scraper.txt
  .venv/bin/python scripts/enrich_websites.py
  .venv/bin/python scripts/enrich_websites.py --resume
  .venv/bin/python scripts/enrich_websites.py --input data/fulledu_schools.csv --limit 50
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException
except ImportError as exc:
    raise SystemExit(
        "Установите зависимости: python3 -m venv .venv && "
        ".venv/bin/pip install -r requirements-scraper.txt"
    ) from exc

DEFAULT_INPUTS = [
    Path("data/fulledu_schools.csv"),
    Path("data/fulledu_sadiki.csv"),
    Path("data/fulledu_lagerya.csv"),
    Path("data/fulledu_moskva_institutions.csv"),
]

EXTRA_FIELDS = ["website", "website_source"]

SKIP_HOSTS = (
    "fulledu.ru",
    "wikipedia.org",
    "ruwiki.ru",
    "wikidata.org",
    "yandex.ru",
    "ya.ru",
    "google.com",
    "google.ru",
    "2gis.ru",
    "zoon.ru",
    "tripadvisor",
    "facebook.com",
    "instagram.com",
    "vk.com",
    "ok.ru",
    "t.me",
    "youtube.com",
    "avito.ru",
    "profi.ru",
    "uslugi.yandex",
    "otzovik.com",
    "irecommend.ru",
    "incamp.ru",
    "vsekolledzhi.ru",
    "ucheba.ru",
    "dayum.ru",
    "orgpage.ru",
    "spravker.ru",
    "rusprofile.ru",
    "list-org.com",
    "checko.ru",
    "dreamjob.ru",
    "hh.ru",
    "duckduckgo.com",
    "bing.com",
    "allterra.ru",
    "oshkolah.ru",
    "schoolotzyv.ru",
    "schoolme.ru",
    "edu-s.ru",
    "15kids.ru",
    "edusite.ru",
    "mo.mosreg.ru",
    "vbr.ru",
    "klerk.ru",
    "rosedu.ru",
    "pedcampus.ru",
    "uchmet.ru",
    "edu.gov.ru",
)


def build_query(row: dict[str, str]) -> str:
    name = row.get("name", "").strip().strip('"')
    legal = row.get("legal_name", "").strip().strip('"')
    town = row.get("town", "").strip() or "Москва"
    label = row.get("category_label", "").strip()

    if len(name) < 12 or name.lower() in {"лицей", "гимназия", "школа", "сад"}:
        base = legal if len(legal) > 20 else name
    else:
        base = name

    # убираем юр. префиксы для поиска
    base = re.sub(
        r"^(государственное|муниципальное|частное|автономное|негосударственное)\s+",
        "",
        base,
        flags=re.I,
    )
    base = base[:120].strip()
    return f"{base} {town} {label} официальный сайт"


def is_bad_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return any(s in host for s in SKIP_HOSTS)


def pick_best_url(results: list[dict]) -> str:
    candidates: list[str] = []
    for item in results:
        href = (item.get("href") or item.get("link") or "").strip()
        if href.startswith("http") and not is_bad_url(href):
            candidates.append(href.rstrip("/"))

    if not candidates:
        return ""

    def score(url: str) -> tuple[int, int]:
        host = urlparse(url).netloc.lower()
        s = 0
        if host.endswith(".ru") or host.endswith(".рф"):
            s -= 4
        if "mskobr.ru" in host or "edumsko.ru" in host:
            s -= 6
        if any(x in host for x in ("school", "shkola", "lgot", "camp")):
            s -= 2
        if "maps" in host or "wiki" in host:
            s += 8
        if any(x in host for x in ("otzyv", "sprav", "catalog", "rating", "portal")):
            s += 4
        if host.count(".") > 2:
            s += 1
        return (s, len(host))

    return sorted(set(candidates), key=score)[0]


def search_website(
    row: dict[str, str],
    ddgs: DDGS,
    *,
    retries: int = 3,
    retry_delay: float = 8.0,
) -> tuple[str, str]:
    query = build_query(row)
    backends = ("duckduckgo", "auto", "brave", "bing")

    for attempt in range(retries):
        for backend in backends:
            try:
                results = list(
                    ddgs.text(
                        query,
                        region="ru-ru",
                        max_results=8,
                        backend=backend,
                    )
                )
                site = pick_best_url(results)
                if site:
                    return site, f"search:{backend}"
            except DDGSException:
                continue
            except Exception:
                continue
        if attempt < retries - 1:
            time.sleep(retry_delay * (attempt + 1))

    return "", ""


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    for col in EXTRA_FIELDS:
        if col not in fields:
            fields.append(col)
    return fields, rows


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def enrich_file(
    path: Path,
    *,
    resume: bool,
    limit: int,
    delay: float,
    overwrite: bool,
) -> None:
    if not path.is_file():
        print(f"Пропуск: {path}")
        return

    fields, rows = read_csv(path)
    if overwrite:
        for row in rows:
            row["website"] = ""
            row["website_source"] = ""

    todo = [
        r
        for r in rows
        if r.get("url") and (not resume or not r.get("website"))
    ]
    if limit:
        todo = todo[:limit]

    print(f"\n{path.name}: всего {len(rows)}, ищем сайт для {len(todo)}")
    if not todo:
        return

    ddgs = DDGS()
    found = 0
    started = time.time()

    for i, row in enumerate(todo, 1):
        site, src = search_website(row, ddgs)
        if site:
            row["website"] = site
            row["website_source"] = src
            found += 1
        else:
            row["website"] = ""
            row["website_source"] = ""

        if i % 10 == 0 or i == len(todo):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            print(
                f"  {i}/{len(todo)} | найдено {found} | "
                f"{rate:.2f} зап/с | {elapsed:.0f}с"
            )
            write_csv(path, fields, rows)

        if delay and i < len(todo):
            time.sleep(delay)

    write_csv(path, fields, rows)
    total = sum(1 for r in rows if r.get("website"))
    print(f"  Готово: {total}/{len(rows)} с сайтом -> {path}")


def sync_websites(master: Path, targets: list[Path]) -> None:
    _, rows = read_csv(master)
    by_url = {
        r["url"].rstrip("/") + "/": (r.get("website", ""), r.get("website_source", ""))
        for r in rows
        if r.get("url")
    }
    for target in targets:
        if not target.is_file() or target == master:
            continue
        fields, trows = read_csv(target)
        n = 0
        for row in trows:
            key = row.get("url", "").rstrip("/") + "/"
            if key in by_url and by_url[key][0]:
                row["website"], row["website_source"] = by_url[key]
                n += 1
        write_csv(target, fields, trows)
        print(f"  {target.name}: перенесено {n} сайтов")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Поиск официальных сайтов через DuckDuckGo (ddgs)"
    )
    parser.add_argument("--input", action="append", type=Path)
    parser.add_argument(
        "--sync-to",
        nargs="*",
        type=Path,
        metavar="CSV",
        help="После обработки скопировать website в другие CSV по url",
    )
    parser.add_argument("--resume", action="store_true", help="Пропускать строки с website")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Очистить website и искать заново для всех строк",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--delay",
        type=float,
        default=2.5,
        help="Пауза между запросами (сек), по умолчанию 2.5",
    )
    args = parser.parse_args()

    inputs = args.input or [Path("data/fulledu_moskva_institutions.csv")]
    for path in inputs:
        try:
            enrich_file(
                path,
                resume=args.resume,
                limit=args.limit,
                delay=args.delay,
                overwrite=args.overwrite,
            )
        except KeyboardInterrupt:
            print("\nПрервано. Прогресс сохранён — запустите с --resume.")
            sys.exit(1)

    if args.sync_to is not None:
        master = inputs[0]
        targets = args.sync_to or [
            Path("data/fulledu_schools.csv"),
            Path("data/fulledu_sadiki.csv"),
            Path("data/fulledu_lagerya.csv"),
        ]
        print("\nСинхронизация website в отдельные файлы:")
        sync_websites(master, targets)


if __name__ == "__main__":
    main()
