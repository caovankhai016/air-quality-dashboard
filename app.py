from flask import Flask, request, jsonify, render_template, Response
from datetime import datetime, timezone, timedelta
import os
import io
import csv
from pymongo import MongoClient

app = Flask(__name__)
app.url_map.strict_slashes = False  # ngăn Railway proxy chuyển POST → GET do redirect trailing slash

# ── CẤU HÌNH DATABASE MONGODB ATLAS ──────────────────────────────────────────
MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("DATABASE_URL")

if not MONGO_URI:
    # Nếu chạy local mà chưa cài biến môi trường, thông báo lỗi cụ thể để người dùng cấu hình
    raise ValueError(
        "[DB ERROR] Chưa cấu hình biến môi trường MONGO_URI!\n"
        "Vui lòng thiết lập biến MONGO_URI trong hệ thống của bạn hoặc trên Railway."
    )

try:
    # Kết nối MongoDB Atlas với timeout 5 giây
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Kiểm tra thử kết nối bằng lệnh ping
    client.admin.command('ping')
    
    # Lấy database mặc định trong URI, nếu không có thì dùng database 'airquality'
    try:
        db = client.get_default_database()
    except Exception:
        db = None
    if db is None:
        db = client['airquality']
        
    print("[DB] Kết nối thành công MongoDB Atlas!")
except Exception as e:
    raise ConnectionError(f"[DB ERROR] Không thể kết nối tới MongoDB Atlas qua URI được cung cấp. Lỗi: {e}")

def init_db():
    """Khởi tạo database và chỉ mục (index) cần thiết."""
    try:
        # Tạo index cho timestamp để tối ưu hóa truy vấn sắp xếp dữ liệu
        db.sensor_data.create_index([("timestamp", -1)])
        print("[DB] Khởi tạo chỉ mục MongoDB Atlas thành công.")
    except Exception as e:
        print(f"[ERROR] Lỗi khởi tạo chỉ mục MongoDB: {e}")

# ── API: ESP32 GỬI DỮ LIỆU LÊN ───────────────────────────────────────────────
@app.route("/api/push", methods=["GET", "POST"])
@app.route("/api/data", methods=["GET", "POST"])  # giữ lại để tương thích
def receive_data():
    """
    Nhận dữ liệu từ ESP32 qua 2 cách:
    - GET:  /api/data?pm25=12.4&pm10=18.0&temp=28.5&humidity=65.0&relay_on=false
    - POST: body JSON {"pm25":12.4,"pm10":18.0,"temp":28.5,"humidity":65.0,"relay_on":false}
    """
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        pm25     = float(data.get("pm25", 0))
        pm10     = float(data.get("pm10", 0))
        temp     = float(data.get("temp", 0))
        humidity = float(data.get("humidity", 0))
        relay_on = 1 if data.get("relay_on", False) else 0
        aqi      = int(data.get("aqi", 0))
    else:  # GET — Railway proxy chuyển POST→GET
        pm25     = float(request.args.get("pm25", 0))
        pm10     = float(request.args.get("pm10", 0))
        temp     = float(request.args.get("temp", 0))
        humidity = float(request.args.get("humidity", 0))
        relay_on = 1 if request.args.get("relay_on", "false").lower() == "true" else 0
        aqi      = int(request.args.get("aqi", 0))

    VN_TZ = timezone(timedelta(hours=7))
    timestamp = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    try:
        db.sensor_data.insert_one({
            "timestamp": timestamp,
            "pm25": pm25,
            "pm10": pm10,
            "temp": temp,
            "humidity": humidity,
            "relay_on": relay_on,
            "aqi": aqi
        })
        print(f"[DATA] SAVED: {timestamp} | PM2.5={pm25} | PM10={pm10} | Temp={temp} | Humi={humidity} | Relay={relay_on}")
        return jsonify({"status": "ok", "timestamp": timestamp}), 200
    except Exception as e:
        print(f"[ERROR] MongoDB write failed: {e}")
        return jsonify({"error": str(e)}), 500

# ── API: WEB DASHBOARD LẤY LỊCH SỬ ──────────────────────────────────────────
@app.route("/api/history", methods=["GET"])
def get_history():
    """
    Trả về 100 bản ghi gần nhất dưới dạng JSON.
    Web Dashboard dùng API này để vẽ biểu đồ.
    """
    limit = request.args.get("limit", 100, type=int)
    
    try:
        # Lấy các bản ghi mới nhất, loại bỏ _id để Flask có thể jsonify trực tiếp
        rows = list(db.sensor_data.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit))
        # Đảo ngược để hiển thị theo thứ tự thời gian tăng dần trên biểu đồ
        result = list(reversed(rows))
        return jsonify(result), 200
    except Exception as e:
        print(f"[ERROR] MongoDB read history failed: {e}")
        return jsonify({"error": str(e)}), 500

# ── API: LẤY GIÁ TRỊ MỚI NHẤT ───────────────────────────────────────────────
@app.route("/api/latest", methods=["GET"])
def get_latest():
    """Trả về bản ghi mới nhất — dùng để hiển thị real-time."""
    try:
        row = db.sensor_data.find_one({}, {"_id": 0}, sort=[("timestamp", -1)])
        if row is None:
            return jsonify({"error": "No data yet"}), 404
        # Nếu bản ghi mới nhất chưa có AQI (=0, vd ESP32 vừa khởi động lại,
        # chưa chốt được giờ nào) thì lấy AQI của bản ghi gần nhất có giá
        # trị hợp lệ để vẫn hiển thị giá trị cuối khi tải lại trang.
        if not row.get("aqi"):
            last_aqi = db.sensor_data.find_one(
                {"aqi": {"$gt": 0}}, {"_id": 0, "aqi": 1}, sort=[("timestamp", -1)])
            if last_aqi:
                row["aqi"] = last_aqi["aqi"]
        return jsonify(row), 200
    except Exception as e:
        print(f"[ERROR] MongoDB read latest failed: {e}")
        return jsonify({"error": str(e)}), 500

# ── API: XUẤT FILE CSV DỮ LIỆU CÓ BỘ LỌC THỜI GIAN ───────────────────────────
@app.route("/api/export", methods=["GET"])
def export_data():
    """
    Xuất dữ liệu dưới dạng file CSV để tải về.
    Tham số: range = all (tất cả), 30d (30 ngày), 7d (7 ngày), 1d (1 ngày)
    """
    time_range = request.args.get("range", "all")
    VN_TZ = timezone(timedelta(hours=7))
    now = datetime.now(VN_TZ)
    limit_str = None

    # Tính toán mốc thời gian giới hạn dựa trên bộ lọc
    if time_range == "1d":
        limit_str = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    elif time_range == "7d":
        limit_str = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    elif time_range == "30d":
        limit_str = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        query = {}
        if limit_str:
            query["timestamp"] = {"$gte": limit_str}
        # Sắp xếp thời gian tăng dần (cũ đến mới) khi xuất báo cáo
        rows = db.sensor_data.find(query, {"_id": 0}).sort("timestamp", 1)
        records = list(rows)
    except Exception as e:
        print(f"[ERROR] MongoDB export query failed: {e}")
        return f"Lỗi truy vấn MongoDB: {e}", 500

    # Tạo nội dung file CSV trong bộ nhớ RAM
    output = io.StringIO()
    # Ghi mã UTF-8 BOM ở đầu file để Microsoft Excel đọc được tiếng Việt có dấu
    output.write('\ufeff')
    writer = csv.writer(output)
    
    # Ghi tiêu đề các cột
    writer.writerow(["Thời gian", "PM2.5 (µg/m³)", "PM10 (µg/m³)", "AQI", "Nhiệt độ (°C)", "Độ ẩm (%)"])
    
    # Ghi dữ liệu từng dòng
    for r in records:
        writer.writerow([
            r.get("timestamp"),
            f"{float(r.get('pm25', 0)):.1f}",
            f"{float(r.get('pm10', 0)):.1f}",
            int(r.get('aqi', 0)),
            f"{float(r.get('temp', 0)):.1f}",
            f"{float(r.get('humidity', 0)):.1f}"
        ])

    # Tạo Response trả về cho trình duyệt bắt đầu tải xuống
    response = Response(output.getvalue(), mimetype="text/csv")
    
    # Định dạng tên file: du_lieu_khong_khi_<range>_<YYYYMMDD_HHMMSS>.csv
    range_names = {"all": "tat_ca", "30d": "30_ngay", "7d": "7_ngay", "1d": "1_ngay"}
    range_lbl = range_names.get(time_range, "tat_ca")
    file_time = now.strftime("%Y%m%d_%H%M%S")
    filename = f"du_lieu_khong_khi_{range_lbl}_{file_time}.csv"
    
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response

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
    app.run(host="0.0.0.0", port=port, debug=True)

# Railway gọi trực tiếp khi import khởi chạy ứng dụng
init_db()
