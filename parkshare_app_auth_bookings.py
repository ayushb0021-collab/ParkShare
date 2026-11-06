from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3, os, secrets, hashlib, hmac
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app, supports_credentials=True)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parkshare_berlin.sqlite")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('renter','owner')), created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS private_listings(
      id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL REFERENCES users(id),
      title TEXT NOT NULL, address TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
      price_per_day REAL NOT NULL CHECK(price_per_day > 0), parking_type TEXT NOT NULL,
      available_from TEXT NOT NULL, available_until TEXT NOT NULL,
      proof_filename TEXT, status TEXT NOT NULL DEFAULT 'approved',
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bookings(
      id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL UNIQUE,
      listing_id INTEGER NOT NULL REFERENCES private_listings(id),
      renter_id INTEGER NOT NULL REFERENCES users(id), vehicle_plate TEXT NOT NULL,
      start_at TEXT NOT NULL, end_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'requested',
      created_at TEXT NOT NULL, owner_note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_bookings_overlap ON bookings(listing_id,start_at,end_at,status);
    """)
    con.commit()
    con.close()

def now():
    return datetime.now(timezone.utc).isoformat()

def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240000).hex()
    return f"{salt}${derived}"

def password_verify(password, stored):
    try:
        salt, expected = stored.split("$", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def issue_token(user_id):
    token = secrets.token_urlsafe(32)
    con = db()
    con.execute("INSERT INTO sessions(token_hash,user_id,created_at) VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, now()))
    con.commit()
    con.close()
    return token

def current_user():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    con = db()
    row = con.execute("""
      SELECT u.id,u.name,u.email,u.role FROM sessions s JOIN users u ON u.id=s.user_id
      WHERE s.token_hash=?
    """, (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    con.close()
    return dict(row) if row else None

def require_user(role=None):
    user = current_user()
    if not user:
        return None, (jsonify(error="Please sign in first."), 401)
    if role and user["role"] != role:
        return None, (jsonify(error=f"This action requires the {role} role."), 403)
    return user, None

@app.get("/")
def home():
    return jsonify(service="ParkShare Berlin API", status="running",
                   endpoints=["/api/health","/api/auth/register","/api/auth/login",
                              "/api/auth/me","/api/parking","/api/listings",
                              "/api/bookings","/api/owner/bookings"])

@app.get("/api/health")
def health():
    con=db()
    try:
        count=con.execute("SELECT COUNT(*) FROM parking").fetchone()[0]
    except sqlite3.OperationalError:
        count=0
    users=con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    listings=con.execute("SELECT COUNT(*) FROM private_listings").fetchone()[0]
    bookings=con.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    con.close()
    return jsonify(status="healthy", parking_records=count, users=users, private_listings=listings, bookings=bookings)

@app.post("/api/auth/register")
def register():
    data=request.get_json(silent=True) or {}
    name=str(data.get("name","")).strip()
    email=str(data.get("email","")).strip().lower()
    password=str(data.get("password",""))
    role=data.get("role","renter")
    if not name or len(name)>90 or "@" not in email or len(email)>180:
        return jsonify(error="Enter a valid name and email."),400
    if len(password)<8:
        return jsonify(error="Password must contain at least 8 characters."),400
    if role not in ("renter","owner"):
        return jsonify(error="Choose renter or owner role."),400
    con=db()
    try:
        cur=con.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                        (name,email,password_hash(password),role,now()))
        con.commit()
        uid=cur.lastrowid
    except sqlite3.IntegrityError:
        con.close()
        return jsonify(error="An account with this email already exists."),409
    con.close()
    token=issue_token(uid)
    return jsonify(token=token,user={"id":uid,"name":name,"email":email,"role":role}),201

@app.post("/api/auth/login")
def login():
    data=request.get_json(silent=True) or {}
    email=str(data.get("email","")).strip().lower()
    password=str(data.get("password",""))
    con=db()
    row=con.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
    con.close()
    if not row or not password_verify(password,row["password_hash"]):
        return jsonify(error="Email or password is incorrect."),401
    token=issue_token(row["id"])
    return jsonify(token=token,user={"id":row["id"],"name":row["name"],"email":row["email"],"role":row["role"]})

@app.get("/api/auth/me")
def me():
    user=current_user()
    if not user:
        return jsonify(error="Not signed in."),401
    return jsonify(user=user)

@app.post("/api/auth/logout")
def logout():
    auth=request.headers.get("Authorization","")
    if auth.startswith("Bearer "):
        token=auth[7:].strip()
        con=db()
        con.execute("DELETE FROM sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
        con.commit()
        con.close()
    return jsonify(message="Signed out.")

@app.get("/api/parking")
def parking():
    limit=max(1,min(int(request.args.get("limit",1500)),5000))
    con=db()
    try:
        rows=con.execute("""SELECT osm_id AS id,name,latitude,longitude,address,category,parking_type,
          access,fee,capacity,operator,opening_hours,osm_url FROM parking LIMIT ?""",(limit,)).fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        return jsonify(error="Parking table is not ready. Run the OpenStreetMap importer first.",detail=str(e)),503
    con.close()
    return jsonify(parking=[dict(r) for r in rows],count=len(rows))

@app.get("/api/listings")
def get_listings():
    con=db()
    rows=con.execute("""SELECT l.id,l.title,l.address,l.latitude AS lat,l.longitude AS lng,
       l.price_per_day AS price,l.parking_type AS type,l.available_from,l.available_until,
       l.status,l.owner_id,u.name AS owner_name
       FROM private_listings l JOIN users u ON u.id=l.owner_id
       WHERE l.status='approved' OR l.owner_id=?""",
       ((current_user() or {}).get("id",-1),)).fetchall()
    con.close()
    return jsonify(listings=[dict(r) for r in rows])

@app.post("/api/listings")
def create_listing():
    user,err=require_user("owner")
    if err:return err
    data=request.get_json(silent=True) or {}
    try:
        title=str(data["title"]).strip(); address=str(data["address"]).strip()
        lat=float(data["lat"]); lng=float(data["lng"]); price=float(data["price"])
        ptype=str(data["type"]); date_from=str(data["from"]); date_until=str(data["until"])
    except (KeyError,TypeError,ValueError):
        return jsonify(error="Please provide all listing fields in valid format."),400
    if not title or not address or not (52.3<=lat<=52.7 and 13.0<=lng<=13.8) or price<=0:
        return jsonify(error="Enter a valid Berlin address, coordinates and positive price."),400
    if date_until<date_from:
        return jsonify(error="Available-until date must be on or after available-from date."),400
    con=db()
    cur=con.execute("""INSERT INTO private_listings(owner_id,title,address,latitude,longitude,
       price_per_day,parking_type,available_from,available_until,proof_filename,status,created_at)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
       (user["id"],title,address,lat,lng,price,ptype,date_from,date_until,
        str(data.get("proof_filename",""))[:200],"approved",now()))
    con.commit(); lid=cur.lastrowid; con.close()
    return jsonify(id=lid,status="approved",message="Listing published. Demo verification is not a real legal review."),201

@app.post("/api/bookings")
def create_booking():
    user,err=require_user("renter")
    if err:return err
    data=request.get_json(silent=True) or {}
    try:
        listing_id=int(data["listing_id"])
        plate=str(data["plate"]).strip().upper()
        start=str(data["start"]); end=str(data["end"])
        start_dt=datetime.fromisoformat(start); end_dt=datetime.fromisoformat(end)
    except (KeyError,TypeError,ValueError):
        return jsonify(error="Enter listing, vehicle plate, and valid start/end date-times."),400
    if not plate or end_dt<=start_dt:
        return jsonify(error="Vehicle plate is required and end time must be after start time."),400
    con=db()
    listing=con.execute("SELECT * FROM private_listings WHERE id=? AND status='approved'",(listing_id,)).fetchone()
    if not listing:
        con.close()
        return jsonify(error="This listing is not approved for booking yet."),404
    if start_dt.date().isoformat()<listing["available_from"] or end_dt.date().isoformat()>listing["available_until"]:
        con.close()
        return jsonify(error="Requested dates are outside this listing's availability."),400
    overlap=con.execute("""SELECT 1 FROM bookings WHERE listing_id=?
       AND status IN ('requested','accepted') AND start_at < ? AND end_at > ? LIMIT 1""",
       (listing_id,end,end_dt.isoformat() if False else start)).fetchone()
    if overlap:
        con.close()
        return jsonify(error="This spot already has a booking request or booking during that time."),409
    ref="BK-"+secrets.token_hex(4).upper()
    con.execute("""INSERT INTO bookings(reference,listing_id,renter_id,vehicle_plate,start_at,end_at,status,created_at)
       VALUES(?,?,?,?,?,?,?,?)""",(ref,listing_id,user["id"],plate,start,end,"requested",now()))
    con.commit(); con.close()
    return jsonify(reference=ref,status="requested",message="Booking request sent to the owner."),201

@app.get("/api/bookings")
def my_bookings():
    user,err=require_user()
    if err:return err
    con=db()
    rows=con.execute("""SELECT b.id,b.reference,b.listing_id,b.vehicle_plate,b.start_at,b.end_at,b.status,
       b.created_at,b.owner_note,l.title AS spot,l.address,l.price_per_day,l.owner_id
       FROM bookings b JOIN private_listings l ON l.id=b.listing_id
       WHERE b.renter_id=? ORDER BY b.created_at DESC""",(user["id"],)).fetchall()
    con.close()
    return jsonify(bookings=[dict(r) for r in rows])

@app.get("/api/owner/bookings")
def owner_bookings():
    user,err=require_user("owner")
    if err:return err
    con=db()
    rows=con.execute("""SELECT b.id,b.reference,b.listing_id,b.vehicle_plate,b.start_at,b.end_at,b.status,
       b.created_at,b.owner_note,l.title AS spot,l.address,u.name AS renter_name,u.email AS renter_email
       FROM bookings b JOIN private_listings l ON l.id=b.listing_id JOIN users u ON u.id=b.renter_id
       WHERE l.owner_id=? ORDER BY b.created_at DESC""",(user["id"],)).fetchall()
    con.close()
    return jsonify(bookings=[dict(r) for r in rows])

@app.patch("/api/owner/bookings/<int:booking_id>")
def update_booking(booking_id):
    user,err=require_user("owner")
    if err:return err
    data=request.get_json(silent=True) or {}
    status=data.get("status")
    if status not in ("accepted","rejected"):
        return jsonify(error="Status must be accepted or rejected."),400
    con=db()
    row=con.execute("""SELECT b.id,b.status FROM bookings b JOIN private_listings l ON l.id=b.listing_id
       WHERE b.id=? AND l.owner_id=?""",(booking_id,user["id"])).fetchone()
    if not row:
        con.close()
        return jsonify(error="Booking not found for your listings."),404
    if row["status"]!="requested":
        con.close()
        return jsonify(error="Only requested bookings can be updated."),409
    if status=="accepted":
        b=con.execute("SELECT listing_id,start_at,end_at FROM bookings WHERE id=?",(booking_id,)).fetchone()
        overlap=con.execute("""SELECT 1 FROM bookings WHERE listing_id=? AND id<>?
           AND status='accepted' AND start_at < ? AND end_at > ? LIMIT 1""",
           (b["listing_id"],booking_id,b["end_at"],b["start_at"])).fetchone()
        if overlap:
            con.close()
            return jsonify(error="Another accepted booking overlaps these times."),409
    con.execute("UPDATE bookings SET status=? WHERE id=?",(status,booking_id))
    con.commit();con.close()
    return jsonify(message=f"Booking {status}.",status=status)

if __name__=="__main__":
    init_db()
    print("Starting ParkShare Berlin API...")
    print("Database:",DB_PATH)
    app.run(host="127.0.0.1",port=5000,debug=True)
