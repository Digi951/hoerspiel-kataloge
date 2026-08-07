#!/usr/bin/env python3
"""
Prüft alle Kataloge auf tote URLs und fehlende Links.
Schreibt einen Report nach reports/check_YYYYMMDD_HHmm.md

Verwendung:
  python scripts/check_catalogs.py            # Alle URLs prüfen
  python scripts/check_catalogs.py --sample 5 # 5 zufällige URLs pro Katalog
  python scripts/check_catalogs.py --no-report # Nur stdout, kein Report
"""

import difflib
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import random
import argparse

BASE_DIR = Path(__file__).parent.parent
CATALOG_DIR = BASE_DIR / "catalogs"
REPORT_DIR = BASE_DIR / "reports"

URL_FIELDS = ["spotifyURL", "appleMusicURL", "deezerURL", "audibleURL"]
FIELD_LABELS = {
    "spotifyURL": "Spotify",
    "appleMusicURL": "Apple Music",
    "deezerURL": "Deezer",
    "audibleURL": "Audible",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ASIN_RE = re.compile(r"/pd/([A-Z0-9]+)")
# Titel von Drittanbietern (Audible-Produkttitel, Spotify-Albumtitel) enthalten
# "Folge N: Titel" oft nicht am Stringanfang, sondern eingebettet
# (z.B. "Sonderermittler der Krone, Folge 1: Zeitenwechsel") - deshalb wird das
# letzte "Folge N:"-Vorkommen gesucht statt ein Präfix zu ankern. Manche Kataloge
# nutzen stattdessen "NNN/Titel" direkt am Anfang.
FOLGE_PREFIX_RE = re.compile(r"folge\s+\d+\s*[:\-]?\s*", re.I)
NUM_SLASH_PREFIX_RE = re.compile(r"^\s*\d+\s*/\s*")
TITLE_MISMATCH_THRESHOLD = 0.5  # unter diesem Ähnlichkeitswert gilt der Link als falsch verknüpft


def _strip_series_prefix(linked_title: str) -> str:
    matches = list(FOLGE_PREFIX_RE.finditer(linked_title))
    if matches:
        return linked_title[matches[-1].end():]
    return NUM_SLASH_PREFIX_RE.sub("", linked_title)


def _normalize_title(title: str) -> str:
    title = title.lower().replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def _title_mismatch(expected_title: str, linked_title: str) -> bool:
    stripped = _strip_series_prefix(linked_title)
    a, b = _normalize_title(expected_title), _normalize_title(stripped)
    if not a or not b:
        return True
    if a in b or b in a:
        # z.B. "Gefahr für Rom" vs. "Gefahr für Rom. Das Original Playmobil Hörspiel"
        return False
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio < TITLE_MISMATCH_THRESHOLD


def check_audible_title(url: str, expected_title: str, timeout: int = 12) -> tuple[Optional[str], Optional[str]]:
    """Vergleicht den Katalog-Titel mit dem Titel der verlinkten Audible-Produktseite.

    Returns (fremder_titel, error). fremder_titel ist None wenn der Abgleich passt oder
    nicht durchgeführt werden konnte (kein ASIN erkennbar, API-Fehler) - kein Fehlerfall,
    einfach übersprungen.
    """
    match = ASIN_RE.search(url)
    if not match:
        return None, None
    api_url = f"https://api.audible.de/1.0/catalog/products/{match.group(1)}?response_groups=product_desc"
    req = urllib.request.Request(api_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:
        return None, str(e)
    audible_title = data.get("product", {}).get("title")
    if not audible_title:
        return None, None
    if _title_mismatch(expected_title, audible_title):
        return audible_title, None
    return None, None


def check_spotify_title(url: str, expected_title: str, timeout: int = 12) -> tuple[Optional[str], Optional[str]]:
    """Vergleicht den Katalog-Titel mit dem Titel des verlinkten Spotify-Albums (via oEmbed).

    Returns (fremder_titel, error), analog zu check_audible_title. Spotify liefert
    Alben-Titel meist als "Folge N: Titel" oder "NNN/Titel" - dieses Präfix wird vor
    dem Vergleich entfernt.
    """
    api_url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api_url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:
        return None, str(e)
    spotify_title = data.get("title")
    if not spotify_title:
        return None, None
    if _title_mismatch(expected_title, spotify_title):
        return spotify_title, None
    return None, None


def check_url(url: str, timeout: int = 12) -> tuple[Optional[int], Optional[str]]:
    """Returns (status_code, error_message). Tries HEAD, falls back to GET on 405/403."""
    req = urllib.request.Request(url, method="HEAD", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        if e.code in (405, 403):
            req2 = urllib.request.Request(url, headers=HEADERS)
            try:
                with urllib.request.urlopen(req2, timeout=timeout) as resp:
                    return resp.status, None
            except urllib.error.HTTPError as e2:
                return e2.code, None
            except Exception as e2:
                return None, str(e2)
        return e.code, None
    except Exception as e:
        return None, str(e)


def find_missing_urls(entries: list[dict]) -> list[dict]:
    result = []
    for entry in entries:
        absent = [f for f in URL_FIELDS if not entry.get(f)]
        if absent:
            result.append({
                "number": entry.get("number", "?"),
                "title": entry.get("title", ""),
                "kind": entry.get("kind", "regular"),
                "missing": absent,
            })
    return result


TITLE_CHECKERS = {
    "audibleURL": check_audible_title,
    "spotifyURL": check_spotify_title,
}


def check_catalog_urls(
    entries: list[dict],
    sample: Optional[int],
    executor: ThreadPoolExecutor,
) -> tuple[list[dict], list[dict]]:
    """Queues URL checks and returns (broken, mismatched) entries."""
    if sample:
        entries = random.sample(entries, min(sample, len(entries)))

    url_tasks = []
    title_tasks = []
    for entry in entries:
        number = entry.get("number", "?")
        title = entry.get("title", "")
        for field in URL_FIELDS:
            url = entry.get(field)
            if url:
                future = executor.submit(check_url, url)
                url_tasks.append((future, number, title, field, url))
                checker = TITLE_CHECKERS.get(field)
                if checker:
                    future2 = executor.submit(checker, url, title)
                    title_tasks.append((future2, number, title, field, url))

    broken = []
    for future, number, title, field, url in url_tasks:
        status, error = future.result()
        if error or (status and status >= 400):
            broken.append({
                "number": number,
                "title": title,
                "field": field,
                "label": FIELD_LABELS[field],
                "url": url,
                "status": status,
                "error": error,
            })

    mismatched = []
    for future, number, title, field, url in title_tasks:
        linked_title, _error = future.result()
        if linked_title:
            mismatched.append({
                "number": number,
                "title": title,
                "field": field,
                "label": FIELD_LABELS[field],
                "url": url,
                "linked_title": linked_title,
            })
    return broken, mismatched


def sort_key(entry: dict) -> tuple:
    n = entry.get("number", 0)
    return (0 if isinstance(n, int) else 1, n if isinstance(n, int) else 0)


def build_report(
    catalog_results: list[dict],
    sample: Optional[int],
    now: datetime,
) -> str:
    sample_note = f" (Stichprobe: {sample} Einträge/Katalog)" if sample else ""
    total_broken = sum(len(r["broken"]) for r in catalog_results)
    total_missing = sum(len(r["missing"]) for r in catalog_results)
    total_mismatched = sum(len(r["mismatched"]) for r in catalog_results)

    lines = [
        "# Katalog-Check Report",
        f"Erstellt: {now.strftime('%Y-%m-%d %H:%M')}{sample_note}",
        "",
        "## Zusammenfassung",
        f"- Kataloge geprüft: {len(catalog_results)}",
        f"- Tote URLs: **{total_broken}**",
        f"- Einträge mit fehlenden Links: **{total_missing}**",
        f"- Falsch verknüpfte Links (lebend, falscher Titel): **{total_mismatched}**",
        "",
        "---",
        "",
    ]

    for r in catalog_results:
        has_issues = bool(r["broken"] or r["missing"] or r["mismatched"])
        icon = "⚠️ " if has_issues else "✅ "
        lines.append(f"## {icon}{r['name']}")
        lines.append(f"Stand: {r['lastUpdated']} | Einträge: {r['entryCount']}")
        lines.append("")

        if r["broken"]:
            lines.append("### Tote URLs")
            for b in sorted(r["broken"], key=sort_key):
                err = f"HTTP {b['status']}" if b["status"] else b["error"]
                lines.append(f"- **Folge {b['number']}** „{b['title']}“ - {b['label']}: {err}")
                lines.append(f"  `{b['url']}`")
            lines.append("")

        if r["mismatched"]:
            lines.append("### Falsch verknüpfte Links")
            for m in sorted(r["mismatched"], key=sort_key):
                lines.append(
                    f"- **Folge {m['number']}** „{m['title']}“ - {m['label']} zeigt auf „{m['linked_title']}“"
                )
                lines.append(f"  `{m['url']}`")
            lines.append("")

        if r["missing"]:
            lines.append("### Fehlende Links")
            for m in sorted(r["missing"], key=sort_key):
                labels = ", ".join(FIELD_LABELS[f] for f in m["missing"])
                kind = f" _{m['kind']}_" if m["kind"] != "regular" else ""
                lines.append(f"- **Folge {m['number']}**{kind} \"{m['title']}\" - fehlt: {labels}")
            lines.append("")

        if not has_issues:
            lines.append("_Alles in Ordnung._")
            lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Prüft Kataloge auf tote URLs und fehlende Links")
    parser.add_argument(
        "--sample", type=int, default=None, metavar="N",
        help="Nur N zufällige Einträge pro Katalog prüfen (schneller)"
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Keinen Report schreiben, nur stdout"
    )
    args = parser.parse_args()

    catalog_paths = sorted(CATALOG_DIR.glob("**/*.json"))
    print(f"Prüfe {len(catalog_paths)} Kataloge...\n")

    catalog_results = []

    with ThreadPoolExecutor(max_workers=20) as executor:
        for path in catalog_paths:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            name = data.get("collectionName", path.stem)
            entries = data.get("entries", [])

            missing = find_missing_urls(entries)
            broken, mismatched = check_catalog_urls(entries, args.sample, executor)

            catalog_results.append({
                "name": name,
                "lastUpdated": data.get("lastUpdated", "?"),
                "entryCount": data.get("entryCount", len(entries)),
                "broken": broken,
                "missing": missing,
                "mismatched": mismatched,
            })

            icon = "⚠️ " if (broken or missing or mismatched) else "✅"
            print(
                f"{icon} {name}: {len(broken)} tote URLs, {len(missing)} Einträge ohne alle Links, "
                f"{len(mismatched)} falsch verknüpfte Links"
            )

    now = datetime.now()
    total_broken = sum(len(r["broken"]) for r in catalog_results)
    total_missing = sum(len(r["missing"]) for r in catalog_results)
    total_mismatched = sum(len(r["mismatched"]) for r in catalog_results)

    print(f"\n{'='*60}")
    print(
        f"Gesamt: {total_broken} tote URLs, {total_missing} Einträge mit fehlenden Links, "
        f"{total_mismatched} falsch verknüpfte Links"
    )

    if not args.no_report:
        REPORT_DIR.mkdir(exist_ok=True)
        report_path = REPORT_DIR / f"check_{now.strftime('%Y%m%d_%H%M')}.md"
        report_path.write_text(build_report(catalog_results, args.sample, now), encoding="utf-8")
        print(f"Report: {report_path}")

    sys.exit(1 if (total_broken or total_mismatched) else 0)


if __name__ == "__main__":
    main()
