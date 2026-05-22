from flask import Flask, request, jsonify, render_template
from datetime import datetime
import sqlite3
import os

app = Flask(__name__)
app.url_map.strict_slashes = False  # ngăn Railway proxy chuyển POST → GET do redirect trailing slash

# ── CẤU HÌNH DATABASE ────────────────────────────────────────────────────────
# Dùng /tmp để đảm bảo có quyền ghi trong mọi môi trường (Railway, local...)
DB_PATH = os.path.join("/tmp", "airquality.db")

def get_db():
    """Mở kết nối tới SQLite database."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # trả về dict thay vì tuple
    return conn

def init_db():
    """Khởi tạo bảng dữ liệu nếu chưa có."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT    NOT NULL,
                pm25      REAL    NOT NULL,
                temp      REAL    NOT NULL,
                humidity  REAL    NOT NULL,
                relay_on  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    print("[DB] Database sẵn sàng.")

# ── API: ESP32 GỬI DỮ LIỆU LÊN ───────────────────────────────────────────────
@app.route("/api/data", methods=["GET", "POST"])
def receive_data():
    """
    Nhận dữ liệu từ ESP32 qua 2 cách:
    - GET:  /api/data?pm25=12.4&temp=28.5&humidity=65.0&relay_on=false
    - POST: body JSON {"pm25":12.4,"temp":28.5,"humidity":65.0,"relay_on":false}
    """
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        pm25     = float(data.get("pm25", 0))
        temp     = float(data.get("temp", 0))
        humidity = float(data.get("humidity", 0))
        relay_on = 1 if data.get("relay_on", False) else 0
    else:  # GET — Railway proxy chuyển POST→GET
        pm25     = float(request.args.get("pm25", 0))
        temp     = float(request.args.get("temp", 0))
        humidity = float(request.args.get("humidity", 0))
        relay_on = 1 if request.args.get("relay_on", "false").lower() == "true" else 0

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO sensor_data (timestamp, pm25, temp, humidity, relay_on) VALUES (?, ?, ?, ?, ?)",
            (timestamp, pm25, temp, humidity, relay_on)
        )
        conn.commit()
        print(f"[DATA] SAVED via {request.method}: {timestamp} | PM2.5={pm25} | Temp={temp} | Humi={humidity}")
    except Exception as e:
        print(f"[ERROR] DB write failed: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

    return jsonify({"status": "ok", "timestamp": timestamp}), 200

# ── API: WEB DASHBOARD LẤY LỊCH SỬ ──────────────────────────────────────────
@app.route("/api/history", methods=["GET"])
def get_history():
    """
    Trả về 100 bản ghi gần nhất dưới dạng JSON.
    Web Dashboard dùng API này để vẽ biểu đồ.
    """
    limit = request.args.get("limit", 100, type=int)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sensor_data ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # Đảo ngược để hiển thị theo thứ tự thời gian tăng dần
    result = [dict(row) for row in reversed(rows)]
    return jsonify(result), 200

# ── API: LẤY GIÁ TRỊ MỚI NHẤT ───────────────────────────────────────────────
@app.route("/api/latest", methods=["GET"])
def get_latest():
    """Trả về bản ghi mới nhất — dùng để hiển thị real-time."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sensor_data ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if row is None:
        return jsonify({"error": "No data yet"}), 404
    return jsonify(dict(row)), 200

# ── TRANG WEB DASHBOARD ───────────────────────────────────────────────────────
@app.route("/")
def index():
    """Phục vụ trang Web Dashboard chính."""
    return render_template("index.html")

# ── KHỞI ĐỘNG SERVER ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"[SERVER] Đang chạy tại http://localhost:{port}")
    # host='0.0.0.0' để ESP32 trong cùng mạng LAN có thể gửi dữ liệu vào
    app.run(host="0.0.0.0", port=port, debug=True)

# Railway gọi trực tiếp qua gunicorn — cần init DB khi import
init_db()
