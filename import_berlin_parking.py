

#!/usr/bin/env python3

"""
ParkShare Berlin Parking Importer

Downloads mapped parking facilities from OpenStreetMap
and stores them in a local SQLite database.

Install:
    python -m pip install requests

Run:
    python import_berlin_parking.py

Output:
    parkshare_berlin.sqlite
"""

import json
import sqlite3
import time

from datetime import datetime, timezone
from pathlib import Path

import requests


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

OUT = Path(__file__).with_name("parkshare_berlin.sqlite")

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

HEADERS = {
    "User-Agent": "ParkShareBerlinUniversityPrototype/1.0"
}


# --------------------------------------------------
# BERLIN BOUNDING BOXES
# --------------------------------------------------

SOUTH = 52.33
WEST = 13.08
NORTH = 52.68
EAST = 13.76

MID_LAT = (SOUTH + NORTH) / 2
MID_LON = (WEST + EAST) / 2

BOXES = [
    (SOUTH, WEST, MID_LAT, MID_LON),
    (SOUTH, MID_LON, MID_LAT, EAST),
    (MID_LAT, WEST, NORTH, MID_LON),
    (MID_LAT, MID_LON, NORTH, EAST),
]


# --------------------------------------------------
# FETCH PARKING DATA
# --------------------------------------------------

def fetch_box(box, tile_num):

    south, west, north, east = box

    query = f"""
    [out:json][timeout:90];
    (
      nwr["amenity"="parking"]({south},{west},{north},{east});
      nwr["amenity"="parking_entrance"]({south},{west},{north},{east});
      nwr["parking"="street_side"]({south},{west},{north},{east});
    );
    out center tags;
    """

    last_error = None

    for attempt in range(1, 4):

        for endpoint in ENDPOINTS:

            try:

                print(
                    f"Tile {tile_num}/4 - "
                    f"Attempt {attempt}: requesting data...",
                    flush=True
                )

                response = requests.post(
                    endpoint,
                    data={"data": query},
                    headers=HEADERS,
                    timeout=(20, 120)
                )

                response.raise_for_status()

                payload = response.json()

                if "elements" not in payload:
                    raise RuntimeError(
                        "Overpass response missing elements"
                    )

                elements = payload["elements"]

                print(
                    f"Tile {tile_num}/4 - "
                    f"Received {len(elements):,} features.",
                    flush=True
                )

                return elements

            except Exception as exc:

                last_error = exc

                print(
                    f"Tile {tile_num}/4 - "
                    f"Server failed: {exc}",
                    flush=True
                )

                time.sleep(3 * attempt)

    raise RuntimeError(
        f"Failed downloading tile {tile_num}: {last_error}"
    )


# --------------------------------------------------
# GET COORDINATES
# --------------------------------------------------

def coords(element):

    if element.get("type") == "node":

        return (
            element.get("lat"),
            element.get("lon")
        )

    center = element.get("center") or {}

    return (
        center.get("lat"),
        center.get("lon")
    )


# --------------------------------------------------
# CLASSIFY PARKING
# --------------------------------------------------

def classify(tags):

    access = tags.get("access", "")

    if access in {"private", "no"}:
        return "restricted"

    if (
        access in {"customers", "customer"}
        or tags.get("parking") in {"customers", "customer"}
    ):
        return "customer"

    if tags.get("parking") in {
        "multi-storey",
        "underground"
    }:
        return "garage"

    return "parking"


# --------------------------------------------------
# MAIN IMPORT FUNCTION
# --------------------------------------------------

def main():

    all_elements = {}

    print("\nStarting Berlin parking data import...\n")

    # Download data from all four boxes

    for i, box in enumerate(BOXES, start=1):

        elements = fetch_box(box, i)

        for element in elements:

            osm_id = (
                f"{element.get('type')}/"
                f"{element.get('id')}"
            )

            all_elements[osm_id] = element

        if i < len(BOXES):

            time.sleep(2)

    print("\nDownload completed.")
    print(f"Unique features received: {len(all_elements):,}")

    # --------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------

    stamp = datetime.now(timezone.utc).isoformat()

    con = sqlite3.connect(OUT)

    con.execute("""
        CREATE TABLE IF NOT EXISTS parking (

            osm_id TEXT PRIMARY KEY,
            osm_type TEXT NOT NULL,
            name TEXT,

            latitude REAL NOT NULL,
            longitude REAL NOT NULL,

            category TEXT,
            parking_type TEXT,

            access TEXT,
            fee TEXT,
            capacity TEXT,

            operator TEXT,
            opening_hours TEXT,
            address TEXT,

            tags_json TEXT NOT NULL,
            osm_url TEXT,

            imported_at TEXT NOT NULL

        )
    """)

    # Indexes for fast searches

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_parking_coords
        ON parking(latitude, longitude)
    """)

    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_parking_category
        ON parking(category)
    """)

    # --------------------------------------------------
    # PREPARE DATABASE ROWS
    # --------------------------------------------------

    rows = []

    for osm_id, element in all_elements.items():

        lat, lon = coords(element)

        if lat is None or lon is None:
            continue

        tags = element.get("tags", {})

        osm_type = element.get("type", "")

        address = " ".join(
            filter(
                None,
                [
                    tags.get("addr:housenumber"),
                    tags.get("addr:street"),
                    tags.get("addr:postcode"),
                    tags.get("addr:city", "Berlin"),
                ]
            )
        )

        rows.append(
            (
                osm_id,
                osm_type,
                tags.get("name"),

                lat,
                lon,

                classify(tags),
                tags.get("parking"),

                tags.get("access"),
                tags.get("fee"),
                tags.get("capacity"),

                tags.get("operator"),
                tags.get("opening_hours"),

                address or None,

                json.dumps(
                    tags,
                    ensure_ascii=False
                ),

                f"https://www.openstreetmap.org/"
                f"{osm_type}/{element.get('id')}",

                stamp,
            )
        )

    # --------------------------------------------------
    # INSERT DATA
    # --------------------------------------------------

    con.executemany("""
        INSERT OR REPLACE INTO parking

        (
            osm_id,
            osm_type,
            name,

            latitude,
            longitude,

            category,
            parking_type,

            access,
            fee,
            capacity,

            operator,
            opening_hours,
            address,

            tags_json,
            osm_url,

            imported_at
        )

        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?
        )

    """, rows)

    # --------------------------------------------------
    # IMPORT METADATA
    # --------------------------------------------------

    con.execute("""
        CREATE TABLE IF NOT EXISTS import_metadata (

            id INTEGER PRIMARY KEY CHECK(id=1),

            source TEXT,
            license TEXT,

            imported_at TEXT,
            record_count INTEGER

        )
    """)

    con.execute("""
        INSERT OR REPLACE INTO import_metadata

        VALUES (1, ?, ?, ?, ?)

    """, (
        "OpenStreetMap / Overpass API",
        "ODbL 1.0",
        stamp,
        len(rows)
    ))

    # --------------------------------------------------
    # SAVE DATABASE
    # --------------------------------------------------

    con.commit()

    con.close()

    print("\n--------------------------------------")
    print("PARKSHARE BERLIN DATABASE CREATED")
    print("--------------------------------------")

    print(
        f"Total parking features: {len(rows):,}"
    )

    print(
        f"Database location: {OUT.resolve()}"
    )

    print("--------------------------------------")

    print(
        "Note: Parking availability is NOT live."
    )

    print(
        "Data source: © OpenStreetMap contributors"
    )


# --------------------------------------------------
# EXECUTION
# --------------------------------------------------

if __name__ == "__main__":

    main()
