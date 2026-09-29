#!/usr/bin/env python3
"""
Prüft alle Kataloge auf tote URLs und fehlende Links.
Schreibt einen Report nach reports/check_YYYYMMDD_HHmm.md

Verwendung:
  python scripts/check_catalogs.py            # Alle URLs prüfen
  python scripts/check_catalogs.py --sample 5 # 5 zufällige URLs pro Katalog
  python scripts/check_catalogs.py --no-report # Nur stdout, kein Report
  python scripts/check_catalogs.py --fields audibleURL # Nur Audible-Links prüfen
"""

import difflib
import json
import re
import sys
import threading
import time
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

# Audible-Links gibt es als /pd/ASIN und als /pd/Titel-Slug/ASIN
ASIN_RE = re.compile(r"/pd/(?:[^/?#]+/)?([A-Z0-9]{10})(?:[/?#]|$)")
# Titel von Drittanbietern (Audible-Produkttitel, Spotify-Albumtitel) enthalten
# "Folge N: Titel" oft nicht am Stringanfang, sondern eingebettet
# (z.B. "Sonderermittler der Krone, Folge 1: Zeitenwechsel") - deshalb wird das
# letzte "Folge N:"-Vorkommen gesucht statt ein Präfix zu ankern. Manche Kataloge
# nutzen stattdessen "NNN/Titel" direkt am Anfang.
FOLGE_PREFIX_RE = re.compile(r"folge\s+\d+\s*[:\-]?\s*", re.I)
NUM_SLASH_PREFIX_RE = re.compile(r"^\s*\d+\s*/\s*")
PART_SUFFIX_RE = re.compile(r"\(\s*teil\s+\d+\s+von\s+\d+\s*\)", re.I)
TITLE_MISMATCH_THRESHOLD = 0.5  # unter diesem Ähnlichkeitswert gilt der Link als falsch verknüpft
# Widerspricht die Audible-Seriennummer der Folgennummer, zählt der Link nur dann
# als korrekt, wenn der Titel nahezu identisch ist (Audible nummeriert vereinzelt
# anders, z.B. John Sinclair Tonstudio Braun #78/#79 vertauscht).
STRONG_TITLE_MATCH = 0.85
AUDIBLE_API_HOSTS = {
    "www.audible.de": "api.audible.de",
    "www.audible.co.uk": "api.audible.co.uk",
}
# audible.de blockt automatisierte Seitenabrufe mit 503/405 - Existenz und
# Zuordnung dieser Links werden ausschließlich über die Katalog-API geprüft.
API_ONLY_FIELDS = {"audibleURL"}
AUDIBLE_EMPTY_RETRIES = 2
AUDIBLE_BATCH_SIZE = 40
AUDIBLE_BACKOFF_SECONDS = [5, 15, 45]
AUDIBLE_MAX_THROTTLED_FAILURES = 3


def _strip_series_prefix(linked_title: str) -> str:
    matches = list(FOLGE_PREFIX_RE.finditer(linked_title))
    if matches:
        return linked_title[matches[-1].end():]
    return NUM_SLASH_PREFIX_RE.sub("", linked_title)


def _normalize_title(title: str) -> str:
    title = title.lower().replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", title).strip()


def _title_mismatch(expected_title: str, linked_title: str) -> bool:
    # Specials heißen im Katalog oft "Präfix: Titel" (z.B. "Hörspiel 5. Kinofilm: Einfach Anders")
    if ":" in expected_title and not _title_mismatch(expected_title.rsplit(":", 1)[1], linked_title):
        return False
    stripped = _strip_series_prefix(linked_title)
    a, b = _normalize_title(expected_title), _normalize_title(stripped)
    if not a or not b:
        return True
    if a in b or b in a:
        # z.B. "Gefahr für Rom" vs. "Gefahr für Rom. Das Original Playmobil Hörspiel"
        return False
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio < TITLE_MISMATCH_THRESHOLD


def _strong_title_match(expected_title: str, linked_title: str) -> bool:
    a = _normalize_title(PART_SUFFIX_RE.sub("", expected_title))
    b = _normalize_title(PART_SUFFIX_RE.sub("", _strip_series_prefix(linked_title)))
    return bool(a and b) and difflib.SequenceMatcher(None, a, b).ratio() >= STRONG_TITLE_MATCH


class DeadLink(Exception):
    """Der Link zeigt auf ein Produkt, das es nicht (mehr) gibt."""


class Unchecked(Exception):
    """Der Link konnte nicht geprüft werden (z.B. API gedrosselt)."""


# ASIN -> Produkt, vorab per Sammelabfrage gefüllt (siehe prefetch_audible_products)
_audible_cache: dict[str, dict] = {}
_audible_lock = threading.Lock()


_audible_throttled_failures = 0


def _audible_api_get(url: str, timeout: int = 20) -> dict:
    """GET gegen die Audible-Katalog-API mit Backoff bei Drosselung (429/503).

    Bleibt die API nach AUDIBLE_MAX_THROTTLED_FAILURES Anfragen trotz Backoff gedrosselt,
    wird sie für den Rest des Laufs als nicht erreichbar behandelt - sonst würde
    jeder weitere Link erneut den vollen Backoff abwarten.
    """
    global _audible_throttled_failures
    if _audible_throttled_failures >= AUDIBLE_MAX_THROTTLED_FAILURES:
        raise RuntimeError("Audible-API gedrosselt, Prüfung übersprungen")
    for delay in AUDIBLE_BACKOFF_SECONDS + [None]:
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with _audible_lock:  # nie mehrere Audible-Anfragen parallel - vermeidet Drosselung
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503):
                raise
            if delay is None:
                _audible_throttled_failures += 1
                raise
        time.sleep(delay)
    raise RuntimeError("unreachable")


def _audible_link(url: str) -> Optional[tuple[str, str]]:
    match = ASIN_RE.search(url)
    api_host = AUDIBLE_API_HOSTS.get(urllib.parse.urlparse(url).netloc)
    return (api_host, match.group(1)) if match and api_host else None


def prefetch_audible_products(urls: list[str]) -> None:
    """Lädt alle Produkte per Sammelabfrage (bis zu 40 ASINs pro Request) in den Cache.

    Ohne diesen Schritt würde jeder Link einzeln abgefragt - bei ~2000 Links drosselt
    die API dann zuverlässig. Fehler hier sind unkritisch: nicht geladene ASINs
    werden in check_audible_title einzeln nachgeladen.
    """
    by_host: dict[str, set[str]] = {}
    for url in urls:
        link = _audible_link(url)
        if link and link[1] not in _audible_cache:
            by_host.setdefault(link[0], set()).add(link[1])
    for api_host, asins in by_host.items():
        ordered = sorted(asins)
        for i in range(0, len(ordered), AUDIBLE_BATCH_SIZE):
            batch = ordered[i:i + AUDIBLE_BATCH_SIZE]
            try:
                data = _audible_api_get(
                    f"https://{api_host}/1.0/catalog/products?asins={','.join(batch)}"
                    "&response_groups=product_desc,series"
                )
            except Exception:
                continue
            for product in data.get("products", []):
                if product.get("title"):
                    _audible_cache[product["asin"]] = product


def check_audible_title(
    url: str, expected_title: str, number: Optional[int] = None, timeout: int = 20
) -> tuple[Optional[str], Optional[str]]:
    """Vergleicht einen Katalog-Eintrag mit dem verlinkten Audible-Produkt (via Katalog-API).

    Returns (fremder_titel, None). fremder_titel ist None wenn der Abgleich passt oder
    kein ASIN erkennbar ist.
    Raises DeadLink wenn die API zum ASIN kein Produkt liefert, Unchecked wenn die API
    nicht erreichbar/gedrosselt ist - ein API-Fehler darf nie als "in Ordnung" durchgehen.

    Mit `number` (reguläre Folgen) wird zusätzlich die Audible-Seriennummer geprüft:
    passt sie, gilt der Link unabhängig vom Titel als korrekt (neuere Folgen heißen
    bei Audible oft nur "Bibi und Tina 108"); widerspricht sie, muss der Titel nahezu
    identisch sein.
    """
    link = _audible_link(url)
    if not link:
        return None, None
    api_host, asin = link
    product = _audible_cache.get(asin, {})
    # Nicht im Cache oder ohne Serieninfo: einzeln nachladen. Unter Last liefert die
    # API vereinzelt ein leeres Produkt oder eines ohne Serieninfo, obwohl beides
    # existiert - deshalb mehrfach versuchen, bevor der Link als tot gilt.
    attempt = 0
    while not (product.get("title") and (number is None or product.get("series"))):
        if attempt > AUDIBLE_EMPTY_RETRIES:
            break
        if attempt:
            time.sleep(2 * attempt)
        attempt += 1
        try:
            data = _audible_api_get(
                f"https://{api_host}/1.0/catalog/products/{asin}?response_groups=product_desc,series",
                timeout,
            )
        except Exception as e:
            raise Unchecked(str(e))
        fetched = data.get("product", {})
        if fetched.get("title") or not product.get("title"):
            product = fetched
    audible_title = product.get("title")
    if not audible_title:
        raise DeadLink(f"ASIN {asin} existiert bei Audible nicht")

    if number is not None:
        sequences = [s.get("sequence") for s in product.get("series") or [] if (s.get("sequence") or "").isdigit()]
        if str(number) in sequences:
            return None, None
        if sequences and not _strong_title_match(expected_title, audible_title):
            series = product["series"][0]
            return f"{audible_title} ({series.get('title')} {series.get('sequence')})", None

    # Manche Produkte führen den Folgentitel nur im Untertitel
    # (z.B. "Cabin Pressure" / "Zurich: The BBC Radio 4 airline")
    subtitle = product.get("subtitle") or ""
    if _title_mismatch(expected_title, audible_title) and _title_mismatch(expected_title, subtitle):
        return audible_title, None
    return None, None


def check_spotify_title(
    url: str, expected_title: str, number: Optional[int] = None, timeout: int = 12
) -> tuple[Optional[str], Optional[str]]:
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


def find_missing_urls(entries: list[dict], fields: list[str] = URL_FIELDS) -> list[dict]:
    result = []
    for entry in entries:
        absent = [f for f in fields if not entry.get(f)]
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


def find_duplicate_urls(entries: list[dict], fields: list[str]) -> list[dict]:
    """Links, die mehreren Einträgen desselben Katalogs zugeordnet sind - fast immer
    eine Fehlzuordnung (z.B. 12 Bibi-Blocksberg-Folgen, die alle auf #21 zeigten)."""
    result = []
    for field in fields:
        by_url: dict[str, list[dict]] = {}
        for entry in entries:
            url = entry.get(field)
            if url:
                by_url.setdefault(url, []).append(entry)
        for url, shared in by_url.items():
            if len(shared) > 1:
                result.append({
                    "field": field,
                    "label": FIELD_LABELS[field],
                    "url": url,
                    "entries": [(e.get("number", "?"), e.get("title", "")) for e in shared],
                })
    return result


def check_catalog_urls(
    entries: list[dict],
    sample: Optional[int],
    executor: ThreadPoolExecutor,
    fields: list[str] = URL_FIELDS,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Queues URL checks and returns (broken, mismatched, unchecked) entries."""
    if sample:
        entries = random.sample(entries, min(sample, len(entries)))
    if "audibleURL" in fields:
        prefetch_audible_products([e["audibleURL"] for e in entries if e.get("audibleURL")])

    url_tasks = []
    title_tasks = []
    for entry in entries:
        number = entry.get("number", "?")
        title = entry.get("title", "")
        # Specials haben eine eigene Nummerierung, die nicht zur Audible-Serie passt
        series_number = number if entry.get("kind") != "special" and isinstance(number, int) else None
        for field in fields:
            url = entry.get(field)
            if url:
                if field not in API_ONLY_FIELDS:
                    future = executor.submit(check_url, url)
                    url_tasks.append((future, number, title, field, url))
                checker = TITLE_CHECKERS.get(field)
                if checker:
                    future2 = executor.submit(checker, url, title, series_number)
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
    unchecked = []
    for future, number, title, field, url in title_tasks:
        try:
            linked_title, _error = future.result()
        except Unchecked as e:
            unchecked.append({
                "number": number,
                "title": title,
                "field": field,
                "label": FIELD_LABELS[field],
                "url": url,
                "error": str(e),
            })
            continue
        except DeadLink as e:
            broken.append({
                "number": number,
                "title": title,
                "field": field,
                "label": FIELD_LABELS[field],
                "url": url,
                "status": None,
                "error": str(e),
            })
            continue
        if linked_title:
            mismatched.append({
                "number": number,
                "title": title,
                "field": field,
                "label": FIELD_LABELS[field],
                "url": url,
                "linked_title": linked_title,
            })
    return broken, mismatched, unchecked


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
    total_duplicates = sum(len(r["duplicates"]) for r in catalog_results)
    total_unchecked = sum(len(r["unchecked"]) for r in catalog_results)

    lines = [
        "# Katalog-Check Report",
        f"Erstellt: {now.strftime('%Y-%m-%d %H:%M')}{sample_note}",
        "",
        "## Zusammenfassung",
        f"- Kataloge geprüft: {len(catalog_results)}",
        f"- Tote URLs: **{total_broken}**",
        f"- Einträge mit fehlenden Links: **{total_missing}**",
        f"- Falsch verknüpfte Links (lebend, falscher Titel/Folgennummer): **{total_mismatched}**",
        f"- Mehrfach vergebene Links: **{total_duplicates}**",
        f"- Nicht prüfbar (API-Fehler/gedrosselt): **{total_unchecked}**",
        "",
        "---",
        "",
    ]

    for r in catalog_results:
        has_issues = bool(r["broken"] or r["missing"] or r["mismatched"] or r["duplicates"] or r["unchecked"])
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

        if r["unchecked"]:
            lines.append("### Nicht prüfbar")
            for u in sorted(r["unchecked"], key=sort_key):
                lines.append(f"- **Folge {u['number']}** „{u['title']}“ - {u['label']}: {u['error']}")
            lines.append("")

        if r["duplicates"]:
            lines.append("### Mehrfach vergebene Links")
            for d in r["duplicates"]:
                shared = ", ".join(f"#{n} „{t}“" for n, t in d["entries"])
                lines.append(f"- {d['label']}: {shared}")
                lines.append(f"  `{d['url']}`")
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
    parser.add_argument(
        "--fields", nargs="+", choices=URL_FIELDS, default=URL_FIELDS, metavar="FIELD",
        help=f"Nur diese Link-Felder prüfen ({', '.join(URL_FIELDS)})"
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

            missing = find_missing_urls(entries, args.fields)
            duplicates = find_duplicate_urls(entries, args.fields)
            broken, mismatched, unchecked = check_catalog_urls(entries, args.sample, executor, args.fields)

            catalog_results.append({
                "name": name,
                "lastUpdated": data.get("lastUpdated", "?"),
                "entryCount": data.get("entryCount", len(entries)),
                "broken": broken,
                "missing": missing,
                "mismatched": mismatched,
                "duplicates": duplicates,
                "unchecked": unchecked,
            })

            icon = "⚠️ " if (broken or missing or mismatched or duplicates or unchecked) else "✅"
            print(
                f"{icon} {name}: {len(broken)} tote URLs, {len(missing)} Einträge ohne alle Links, "
                f"{len(mismatched)} falsch verknüpfte Links, {len(duplicates)} mehrfach vergebene Links, "
                f"{len(unchecked)} nicht prüfbar"
            )

    now = datetime.now()
    total_broken = sum(len(r["broken"]) for r in catalog_results)
    total_missing = sum(len(r["missing"]) for r in catalog_results)
    total_mismatched = sum(len(r["mismatched"]) for r in catalog_results)
    total_duplicates = sum(len(r["duplicates"]) for r in catalog_results)
    total_unchecked = sum(len(r["unchecked"]) for r in catalog_results)

    print(f"\n{'='*60}")
    print(
        f"Gesamt: {total_broken} tote URLs, {total_missing} Einträge mit fehlenden Links, "
        f"{total_mismatched} falsch verknüpfte Links, {total_duplicates} mehrfach vergebene Links, "
        f"{total_unchecked} nicht prüfbar"
    )
    if total_unchecked:
        print("WARNUNG: Prüfung unvollständig - nicht prüfbare Links sind NICHT als in Ordnung zu werten.")

    if not args.no_report:
        REPORT_DIR.mkdir(exist_ok=True)
        report_path = REPORT_DIR / f"check_{now.strftime('%Y%m%d_%H%M')}.md"
        report_path.write_text(build_report(catalog_results, args.sample, now), encoding="utf-8")
        print(f"Report: {report_path}")

    sys.exit(1 if (total_broken or total_mismatched or total_duplicates or total_unchecked) else 0)


if __name__ == "__main__":
    main()
