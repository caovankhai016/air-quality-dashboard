# ═══════════════════════════════════════════════════════════════════════════
# forecast.py — Dự báo PM2.5/PM10 ngắn hạn bằng học máy (Ridge regression)
# ───────────────────────────────────────────────────────────────────────────
# Quy trình mỗi lần dự báo (cache 10 phút):
#   1. Lấy tối đa 14 ngày dữ liệu gần nhất từ MongoDB
#   2. Lọc mẫu lỗi cảm biến, gộp trung bình 10 phút/điểm, nội suy gap ngắn
#   3. Tạo đặc trưng: lag PM2.5/PM10, trung bình trượt, giờ trong ngày
#      (mã hóa sin/cos), nhiệt độ, độ ẩm
#   4. Huấn luyện Ridge cho từng mốc 10..180 phút, dự báo 3 giờ tới
#   5. Quy đổi AQI dự kiến bằng đúng công thức EPA 2024 của firmware
#      (xem AirQualityNode/main/aqi_calculator.h)
#
# Chỉ ĐỌC database — không ghi/sửa. Mọi lỗi trả về status thay vì ném
# exception để không ảnh hưởng các tính năng khác của dashboard.
# ═══════════════════════════════════════════════════════════════════════════
from datetime import datetime, timezone, timedelta
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error

VN_TZ = timezone(timedelta(hours=7))

TRAIN_DAYS    = 14          # lấy tối đa bao nhiêu ngày dữ liệu để huấn luyện
STEP_MIN      = 10          # gộp dữ liệu về 10 phút/điểm
HORIZON_STEPS = 18          # dự báo 18 bước × 10 phút = 3 giờ
LAGS          = [1, 2, 3, 6, 12, 18]   # quá khứ 10', 20', 30', 1h, 2h, 3h
MIN_TRAIN_PTS = 144         # cần tối thiểu ~1 ngày dữ liệu mới dự báo
CACHE_TTL_SEC = 600         # huấn luyện lại tối đa 10 phút/lần

_cache = {"at": 0.0, "result": None}

# ── AQI: EPA 2024 breakpoints (chuyển từ aqi_calculator.h của firmware) ──────
PM25_BREAKPOINTS = [
    (0.0,     9.0,   0,  50),
    (9.1,    35.4,  51, 100),
    (35.5,   55.4, 101, 150),
    (55.5,  125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
]
PM10_BREAKPOINTS = [
    (0.0,    54.0,   0,  50),
    (55.0,  154.0,  51, 100),
    (155.0, 254.0, 101, 150),
    (255.0, 354.0, 151, 200),
    (355.0, 424.0, 201, 300),
    (425.0, 504.0, 301, 400),
    (505.0, 604.0, 401, 500),
]


def _aqi_interp(cp, breakpoints, cp_max):
    if cp < 0:
        cp = 0.0
    if cp > cp_max:
        return 500
    for bp_lo, bp_hi, i_lo, i_hi in breakpoints:
        if cp <= bp_hi:
            return int((i_hi - i_lo) / (bp_hi - bp_lo) * (cp - bp_lo) + i_lo + 0.5)
    return 500


def aqi_from_pm25(pm25):
    cp = int(pm25 * 10.0) / 10.0        # truncate 1 chữ số thập phân (EPA)
    return _aqi_interp(cp, PM25_BREAKPOINTS, 500.4)


def aqi_from_pm10(pm10):
    cp = float(int(pm10))               # truncate về số nguyên (EPA)
    return _aqi_interp(cp, PM10_BREAKPOINTS, 604.0)


def aqi_overall(pm25, pm10):
    return max(aqi_from_pm25(pm25), aqi_from_pm10(pm10))


# ── Chuẩn bị dữ liệu ─────────────────────────────────────────────────────────
def _prepare_series(records):
    """Bản ghi thô Mongo → chuỗi 10 phút/điểm đã làm sạch."""
    df = pd.DataFrame(records)
    df["time"] = pd.to_datetime(df["timestamp"], errors="coerce")
    for col in ("pm25", "pm10", "temp", "humidity"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["time", "pm25"])
    # loại mẫu lỗi cảm biến (DHT22 trả 0 khi đọc hỏng)
    df = df[(df["temp"] > 5) & (df["humidity"] > 5)]

    ts = (df.set_index("time")[["pm25", "pm10", "temp", "humidity"]]
            .resample(f"{STEP_MIN}min").mean())
    # nội suy gap ngắn (tối đa 6 bước = 1 giờ); gap dài giữ NaN để loại khỏi train
    ts = ts.interpolate(limit=6)
    return ts


def _make_features(ts):
    X = pd.DataFrame(index=ts.index)
    for lag in LAGS:
        X[f"pm25_lag{lag}"] = ts["pm25"].shift(lag)
    X["pm25_ma6"]   = ts["pm25"].rolling(6).mean()
    X["pm10_lag1"]  = ts["pm10"].shift(1)
    X["pm10_ma6"]   = ts["pm10"].rolling(6).mean()
    hour = ts.index.hour + ts.index.minute / 60.0
    X["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    X["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    X["temp"] = ts["temp"]
    X["humidity"] = ts["humidity"]
    return X


# ── Huấn luyện + dự báo ──────────────────────────────────────────────────────
def _forecast_from_records(records):
    if not records:
        return {"status": "not_enough_data",
                "message": "Chưa có dữ liệu trong database."}

    ts = _prepare_series(records)
    n_valid = int(ts["pm25"].notna().sum())
    if n_valid < MIN_TRAIN_PTS:
        return {"status": "not_enough_data",
                "message": f"Cần tối thiểu ~1 ngày dữ liệu để huấn luyện "
                           f"(hiện có {n_valid}/{MIN_TRAIN_PTS} điểm 10 phút)."}

    X = _make_features(ts)
    last_x = X.iloc[[-1]]
    if last_x.isna().any(axis=None):
        return {"status": "not_enough_data",
                "message": "Đang tích lũy dữ liệu — cần ~3 giờ đo liên tục "
                           "gần nhất để tính đặc trưng dự báo."}

    last_time = ts.index[-1]
    forecast = []
    mae_1h = None
    train_points = 0

    for h in range(1, HORIZON_STEPS + 1):
        point = {"time": (last_time + pd.Timedelta(minutes=STEP_MIN * h))
                 .strftime("%Y-%m-%d %H:%M")}
        for target in ("pm25", "pm10"):
            y = ts[target].shift(-h)
            data = pd.concat([X, y.rename("y")], axis=1).dropna()
            if len(data) < MIN_TRAIN_PTS:
                return {"status": "not_enough_data",
                        "message": "Chưa đủ dữ liệu sạch để huấn luyện mô hình."}
            model = Ridge(alpha=1.0)
            model.fit(data.drop(columns="y"), data["y"])
            pred = float(model.predict(last_x)[0])
            point[target] = round(max(pred, 0.0), 1)

            # đánh giá MAE dự báo 1h cho PM2.5: giữ lại 24h cuối làm test
            if target == "pm25" and h == 6:
                train_points = len(data)
                cutoff = last_time - pd.Timedelta(hours=24)
                tr, te = data[data.index < cutoff], data[data.index >= cutoff]
                if len(tr) >= MIN_TRAIN_PTS and len(te) >= 12:
                    m = Ridge(alpha=1.0).fit(tr.drop(columns="y"), tr["y"])
                    mae_1h = round(float(mean_absolute_error(
                        te["y"], m.predict(te.drop(columns="y")))), 2)
        point["aqi"] = aqi_overall(point["pm25"], point["pm10"])
        forecast.append(point)

    # 6 giờ thực tế gần nhất (cùng thang 10 phút) để vẽ liền mạch với dự báo
    hist = ts.tail(36).dropna(subset=["pm25"])
    history = [{"time": t.strftime("%Y-%m-%d %H:%M"),
                "pm25": round(float(r["pm25"]), 1),
                "pm10": round(float(r["pm10"]), 1) if pd.notna(r["pm10"]) else None}
               for t, r in hist.iterrows()]

    summary = {}
    for label, h in (("1h", 6), ("2h", 12), ("3h", 18)):
        p = forecast[h - 1]
        summary[label] = {"pm25": p["pm25"], "pm10": p["pm10"], "aqi": p["aqi"]}

    return {
        "status": "ok",
        "generated_at": datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "last_data_time": last_time.strftime("%Y-%m-%d %H:%M"),
        "history": history,
        "forecast": forecast,
        "summary": summary,
        "model": {
            "name": "Ridge regression",
            "train_points": train_points,
            "train_days": TRAIN_DAYS,
            "mae_1h": mae_1h,
        },
    }


# ── API chính cho app.py ─────────────────────────────────────────────────────
def get_forecast(db):
    """Trả về dự báo (dict JSON-hóa được). Có cache 10 phút."""
    now = time.time()
    if _cache["result"] is not None and now - _cache["at"] < CACHE_TTL_SEC:
        return _cache["result"]

    cutoff = (datetime.now(VN_TZ) - timedelta(days=TRAIN_DAYS)) \
        .strftime("%Y-%m-%d %H:%M:%S")
    records = list(db.sensor_data.find(
        {"timestamp": {"$gte": cutoff}},
        {"_id": 0, "timestamp": 1, "pm25": 1, "pm10": 1, "temp": 1, "humidity": 1}
    ).sort("timestamp", 1))

    result = _forecast_from_records(records)
    _cache["at"] = now
    _cache["result"] = result
    return result
