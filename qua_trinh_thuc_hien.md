# 🖥️ Web Dashboard — Toàn bộ quá trình thực hiện

## Tổng quan kiến trúc cuối cùng

```
PMS7003 + DHT22
      ↓
   ESP32 (WiFi)
   ├──► AWS RainMaker Cloud → App Android
   │         (thông báo đẩy, điều khiển, hẹn giờ)
   │
   └──► Railway Cloud (HTTPS) → Web Dashboard
             https://web-production-12968.up.railway.app
             (xem từ mọi nơi, mọi thiết bị)
```

---

## Phần 1 — Backend Python (Flask + SQLite)

**Tạo thư mục và cài Flask:**
```cmd
mkdir d:\HUST\DATN\Code\Main\Dashboard
pip install flask
```

**File [`app.py`](file:///d:/HUST/DATN/Code/Main/Dashboard/app.py) — Backend Flask:**
- Khởi tạo SQLite database (`/tmp/airquality.db`)
- `POST /api/push` — nhận dữ liệu từ ESP32 (pm25, temp, humidity, relay_on)
- `GET /api/history` — trả 100 bản ghi gần nhất cho Dashboard
- `GET /api/latest` — trả bản ghi mới nhất
- `GET /` — phục vụ trang Web Dashboard

**Schema database:**
```sql
CREATE TABLE sensor_data (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    pm25      REAL    NOT NULL,
    temp      REAL    NOT NULL,
    humidity  REAL    NOT NULL,
    relay_on  INTEGER NOT NULL DEFAULT 0
)
```

---

## Phần 2 — Web Dashboard Frontend

**File [`templates/index.html`](file:///d:/HUST/DATN/Code/Main/Dashboard/templates/index.html):**
- Giao diện Dark mode, responsive
- Cards hiển thị PM2.5, Nhiệt độ, Độ ẩm real-time
- Biểu đồ đường (Chart.js) hiển thị lịch sử
- Bảng dữ liệu 20 bản ghi gần nhất
- Tự động refresh mỗi **15 giây** (khớp với chu kỳ ESP32)

---

## Phần 3 — Firmware ESP32 gửi dữ liệu lên Dashboard

### Thay đổi [`CMakeLists.txt`](file:///d:/HUST/DATN/Code/Main/AirQualityNode/main/CMakeLists.txt)

```diff
+ esp_http_client
+ json
```

### Thay đổi [`app_driver.c`](file:///d:/HUST/DATN/Code/Main/AirQualityNode/main/app_driver.c)

| Thay đổi | Mục đích |
|---|---|
| Thêm `#include <esp_http_client.h>` | Dùng HTTP client |
| Thêm `#include <esp_crt_bundle.h>` | Hỗ trợ HTTPS/SSL |
| Thêm `g_relay_state` variable | Theo dõi trạng thái relay |
| Cập nhật `app_driver_set_purifier_state()` | Lưu trạng thái relay vào biến |
| Thêm hàm `http_post_to_dashboard()` | Gửi JSON lên Dashboard server |
| Gọi hàm sau mỗi lần đọc PM2.5 | Gửi dữ liệu mỗi 15 giây |

**Hàm gửi dữ liệu (POST JSON):**
```c
#define DASHBOARD_URL "https://web-production-12968.up.railway.app/api/push"

static void http_post_to_dashboard(float pm25, float temp, float humi, bool relay_on) {
    // Tạo JSON body
    // Gửi HTTP POST với SSL certificate bundle
    // Log kết quả HTTP status code
}
```

---

## Phần 4 — Test Local

**Bước thực hiện:**
1. Chạy server: `python app.py`
2. ESP32 cùng WiFi với laptop (IP: `10.77.69.22`)
3. ESP32 gửi thành công → log: `[HTTP] Dashboard OK (HTTP 200)`
4. Xem Dashboard tại `http://localhost:5000` và từ điện thoại

---

## Phần 5 — Deploy lên Railway (Server Public)

### Chuẩn bị code

**[`requirements.txt`](file:///d:/HUST/DATN/Code/Main/Dashboard/requirements.txt):**
```
flask==3.1.3
gunicorn==21.2.0
```

**[`Procfile`](file:///d:/HUST/DATN/Code/Main/Dashboard/Procfile):**
```
web: gunicorn app:app --bind 0.0.0.0:$PORT
```

**Cập nhật `app.py`:** Dùng `os.environ.get("PORT", 5000)` để Railway tự gán port.

### Đẩy lên GitHub

```cmd
git init
git add .
git commit -m "Air Quality Dashboard - initial commit"
git remote add origin https://github.com/caovankhai016/air-quality-dashboard.git
git branch -M main
git push -u origin main
```

### Deploy Railway

1. Vào [railway.app](https://railway.app) → Login with GitHub
2. New Project → Deploy from GitHub repo → chọn `air-quality-dashboard`
3. Railway tự build và deploy (~2 phút)
4. Link public: `https://web-production-12968.up.railway.app`

### Cập nhật firmware dùng link Railway

```c
#define DASHBOARD_URL "https://web-production-12968.up.railway.app/api/push"
```

---

## Vấn đề gặp phải và cách giải quyết

| Vấn đề | Nguyên nhân | Cách fix |
|---|---|---|
| `ESP_ERR_HTTP_CONNECT` (local) | Laptop và ESP32 khác mạng WiFi | Kết nối cùng WiFi |
| `ESP_ERR_HTTP_CONNECT` (local) | Windows Firewall chặn port 5000 | Mở port 5000 qua cmd Admin |
| DB không có cột `relay_on` | File `airquality.db` cũ được push lên git | Thêm vào `.gitignore`, xóa khỏi git |
| `HTTP 405` từ Railway | Railway cache response 405 từ endpoint `/api/data` | Đổi endpoint sang `/api/push` |
| `HTTP 200` nhưng không lưu data | Railway cache GET request, Flask không chạy | Xác nhận bằng Railway logs |
| DB write failed trên Railway | Không có quyền ghi vào app directory | Đổi `DB_PATH` sang `/tmp/airquality.db` |

---

## Kết quả cuối cùng

✅ ESP32 gửi dữ liệu lên **2 server song song** mỗi 15 giây  
✅ Web Dashboard cập nhật **real-time**  
✅ Truy cập từ **bất kỳ đâu, bất kỳ mạng nào**  
✅ Hiển thị trạng thái **relay ON/OFF**  
✅ Lưu lịch sử trong **SQLite database**  

---

## Tính năng có thể bổ sung thêm

| Tính năng | Độ khó |
|---|---|
| Lọc biểu đồ 1h / 24h / 7 ngày | ⭐⭐ |
| Xuất CSV | ⭐ |
| Cảnh báo màu đỏ khi PM2.5 > 50 | ⭐⭐ |
| Hiển thị trạng thái Online/Offline | ⭐⭐ |
