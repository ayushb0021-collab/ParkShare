
from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import math
from pathlib import Path

app = Flask(__name__)
CORS(app)

# Database is in the same folder as app.py
DATABASE = Path(__file__).with_name("parkshare_berlin.sqlite")


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two coordinates in kilometers."""

    earth_radius = 6371

    lat1 = math.radians(lat1)
    lon1 = math.radians(lon1)
    lat2 = math.radians(lat2)
    lon2 = math.radians(lon2)

    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(delta_lon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return earth_radius * c


def parking_to_dict(row):
    return {
        "id": row["osm_id"],
        "name": row["name"] or "Unnamed parking",
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "category": row["category"],
        "parking_type": row["parking_type"],
        "access": row["access"],
        "fee": row["fee"],
        "capacity": row["capacity"],
        "operator": row["operator"],
        "opening_hours": row["opening_hours"],
        "address": row["address"],
        "osm_url": row["osm_url"]
    }


@app.route("/")
def home():
    return jsonify({
        "app": "ParkShare Berlin API",
        "status": "running",
        "database": DATABASE.name
    })


@app.route("/api/health")
def health():
    try:
        conn = get_db_connection()

        count = conn.execute(
            "SELECT COUNT(*) FROM parking"
        ).fetchone()[0]

        conn.close()

        return jsonify({
            "status": "healthy",
            "parking_records": count
        })

    except Exception as error:
        return jsonify({
            "status": "error",
            "message": str(error)
        }), 500


@app.route("/api/parking")
def get_parking():

    category = request.args.get("category")
    search = request.args.get("search", "").strip()

    limit = request.args.get("limit", 500, type=int)
    limit = max(1, min(limit, 2000))

    query = """
        SELECT *
        FROM parking
        WHERE 1=1
    """

    params = []

    if category and category != "all":
        query += " AND category = ?"
        params.append(category)

    if search:
        query += """
            AND (
                name LIKE ?
                OR address LIKE ?
                OR operator LIKE ?
            )
        """

        search_term = f"%{search}%"

        params.extend([
            search_term,
            search_term,
            search_term
        ])

    query += " LIMIT ?"
    params.append(limit)

    conn = get_db_connection()

    rows = conn.execute(
        query,
        params
    ).fetchall()

    conn.close()

    parking = [
        parking_to_dict(row)
        for row in rows
    ]

    return jsonify({
        "count": len(parking),
        "parking": parking
    })


@app.route("/api/parking/nearby")
def get_nearby_parking():

    try:
        latitude = float(request.args.get("lat"))
        longitude = float(request.args.get("lng"))

    except (TypeError, ValueError):
        return jsonify({
            "error": "Latitude and longitude are required."
        }), 400

    radius = request.args.get("radius", 5, type=float)

    radius = max(0.1, min(radius, 50))

    category = request.args.get("category", "all")

    # Bounding box to reduce the number of database rows
    lat_delta = radius / 111.0

    lon_delta = radius / (
        111.0 * max(abs(math.cos(math.radians(latitude))), 0.01)
    )

    min_lat = latitude - lat_delta
    max_lat = latitude + lat_delta

    min_lon = longitude - lon_delta
    max_lon = longitude + lon_delta

    query = """
        SELECT *
        FROM parking
        WHERE latitude BETWEEN ? AND ?
        AND longitude BETWEEN ? AND ?
    """

    params = [
        min_lat,
        max_lat,
        min_lon,
        max_lon
    ]

    if category != "all":
        query += " AND category = ?"
        params.append(category)

    conn = get_db_connection()

    rows = conn.execute(
        query,
        params
    ).fetchall()

    conn.close()

    results = []

    for row in rows:

        spot = parking_to_dict(row)

        distance = calculate_distance(
            latitude,
            longitude,
            spot["latitude"],
            spot["longitude"]
        )

        if distance <= radius:

            spot["distance_km"] = round(distance, 2)

            results.append(spot)

    # Closest parking first
    results.sort(
        key=lambda spot: spot["distance_km"]
    )

    return jsonify({
        "count": len(results),
        "radius_km": radius,
        "parking": results
    })


if __name__ == "__main__":

    print("Starting ParkShare Berlin API...")
    print(f"Database: {DATABASE}")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
