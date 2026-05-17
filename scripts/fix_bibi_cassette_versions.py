#!/usr/bin/env python3
"""
Ersetzt alte Kassetten-Versionen (<=10 Tracks) bei Bibi Blocksberg
durch die digitalen Versionen mit den meisten Tracks.
"""

import json
import re
import time
import urllib.request
import urllib.parse


CATALOG_PATH = "catalogs/de/bibi_blocksberg.json"
CASSETTE_THRESHOLD = 10
MIN_DIGITAL_TRACKS = 15


def itunes_lookup(album_id: str):
    url = f"https://itunes.apple.com/lookup?id={album_id}&entity=song&country=de"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            resp = json.loads(r.read())
        results = resp.get("results", [])
        return next((x for x in results if x.get("wrapperType") == "collection"), None)
    except Exception as e:
        print(f"    LOOKUP ERROR {album_id}: {e}")
        return None


def itunes_search(query: str, limit: int = 10) -> list:
    term = urllib.parse.quote(query)
    url = f"https://itunes.apple.com/search?term={term}&media=music&entity=album&country=de&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            resp = json.loads(r.read())
        return resp.get("results", [])
    except Exception as e:
        print(f"    SEARCH ERROR '{query}': {e}")
        return []


def extract_album_id(apple_music_url: str):
    m = re.search(r"/album/[^/]+/(\d+)", apple_music_url)
    return m.group(1) if m else None


def normalize_url(url: str) -> str:
    """Entfernt Query-Parameter aus Apple Music URLs."""
    return re.sub(r"\?.*$", "", url)


def matches_episode(result: dict, episode_number: int) -> bool:
    """Prüft ob ein iTunes-Suchergebnis zur richtigen Folge gehört."""
    url = result.get("collectionViewUrl", "").lower()
    name = result.get("collectionName", "").lower()

    # URL enthält folge-{nr} (z.B. folge-1-, folge-21-)
    url_pattern = rf"folge-{episode_number}[^0-9]"
    if re.search(url_pattern, url):
        return True

    # Name enthält "folge X:" oder "folge X " am Anfang
    name_pattern = rf"folge {episode_number}[^0-9]"
    if re.search(name_pattern, name):
        return True

    # Name enthält "(folge X)" oder ähnliches
    if f"folge {episode_number}:" in name or name.startswith(f"folge {episode_number} "):
        return True

    return False


def find_digital_version(episode_number: int, title: str):
    """Gibt (neue_URL, track_count) zurück oder None."""
    query = f"Bibi Blocksberg Folge {episode_number}"
    results = itunes_search(query, limit=10)
    time.sleep(1.0)

    best_url = None
    best_tracks = 0

    for r in results:
        tc = r.get("trackCount", 0)
        url = normalize_url(r.get("collectionViewUrl", ""))

        if tc < MIN_DIGITAL_TRACKS:
            continue

        if not matches_episode(r, episode_number):
            continue

        if tc > best_tracks:
            best_tracks = tc
            best_url = url

    return (best_url, best_tracks) if best_url else None


def main():
    with open(CATALOG_PATH, encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("entries", [])

    # Schritt 1: Kassetten-Versionen identifizieren
    print("Prüfe bestehende Apple-Music-URLs auf Track-Count...")
    cassette_entries = []

    for entry in entries:
        url = entry.get("appleMusicURL", "")
        if not url:
            continue
        album_id = extract_album_id(url)
        if not album_id:
            continue
        info = itunes_lookup(album_id)
        time.sleep(0.5)
        if info is None:
            continue
        tc = info.get("trackCount", 999)
        if tc <= CASSETTE_THRESHOLD:
            cassette_entries.append((entry, tc))
            print(f"  Folge {entry['number']:3}: {entry['title']} — {tc} Tracks (KASSETTE)")

    print(f"\n{len(cassette_entries)} Kassetten-Versionen gefunden. Suche digitale Ersatz-Versionen...\n")

    updated = 0
    not_found = []

    for entry, old_tc in cassette_entries:
        nr = entry["number"]
        title = entry["title"]
        print(f"  Folge {nr:3}: {title}")

        result = find_digital_version(nr, title)
        if result:
            new_url, new_tc = result
            print(f"    ✓ Ersetzt: {old_tc} → {new_tc} Tracks")
            print(f"    Neu: {new_url}")
            entry["appleMusicURL"] = new_url
            updated += 1
        else:
            print(f"    ✗ Keine digitale Version gefunden")
            not_found.append(nr)

    # Version inkrementieren
    current_version = data.get("version", 1)
    data["version"] = int(current_version) + 1

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"Fertig: {updated} Einträge aktualisiert")
    if not_found:
        print(f"Nicht gefunden: {len(not_found)} — Folgen {not_found}")
    print(f"Katalog-Version: {current_version} → {data['version']}")


if __name__ == "__main__":
    main()
