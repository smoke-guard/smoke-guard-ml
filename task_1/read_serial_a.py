#!/usr/bin/env python3
"""Đọc dữ liệu cảm biến từ ESP32 qua Serial và lưu liên tục vào file CSV."""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from urllib import request as url_request

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[LỖI] Chưa cài 'pyserial'. Chạy: pip install pyserial")
    sys.exit(1)


# ============================================================================
# 1. CẤU HÌNH MẶC ĐỊNH
# ============================================================================
DEFAULT_BAUD = 115200
DEFAULT_CSV = "sensor_data_a.csv"
PORT_KEYWORDS = ("usbmodem", "usbserial", "cp210", "ch340", "ch9102", "ftdi", "uart", "esp32")
IGNORED_PORTS = ("bluetooth", "debug-console")
CSV_HEADERS = [
    "timestamp", "temp", "hum", "pres",
    "gas_ohm", "gas_ok", "pm1", "pm2_5", "pm10", "bme_ok", "pms_ok",
]


# ============================================================================
# 2. HÀM TÌM CỔNG SERIAL, CHUẨN HÓA JSON & LƯU CSV
# ============================================================================
def find_esp32_port() -> str | None:
    """Tự động tìm cổng Serial của ESP32."""
    ports = [
        p for p in serial.tools.list_ports.comports()
        if not any(x in p.device.lower() for x in IGNORED_PORTS)
    ]
    for p in ports:
        if any(k in f"{p.description} {p.device}".lower() for k in PORT_KEYWORDS):
            return p.device
    return ports[0].device if len(ports) == 1 else None


def list_ports() -> None:
    """In danh sách các cổng Serial hiện có."""
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("  [!] Không có cổng Serial nào.")
        return
    for p in ports:
        print(f"  - {p.device}: {p.description}")


def parse_sensor_json(raw_line: str) -> dict | None:
    """Chuyển dòng JSON từ ESP32 thành dict chuẩn; trả về None nếu không phải JSON."""
    if not (raw_line.startswith("{") and raw_line.endswith("}")):
        return None
    try:
        d = json.loads(raw_line)
    except json.JSONDecodeError:
        return None

    return {
        "bme_ok": bool(d.get("bme_ok", d.get("bm668_valid", False))),
        "temp": float(d.get("temp", 0.0)),
        "hum": float(d.get("hum", d.get("humidity", 0.0))),
        "pres": float(d.get("pres", d.get("pressure", 0.0))),
        "gas_ok": bool(d.get("gas_ok", d.get("gas_valid", False))),
        "gas_ohm": float(d.get("gas_ohm", d.get("gas_res", 0.0))),
        "pms_ok": bool(d.get("pms_ok", d.get("pms_valid", False))),
        "pm1": int(d.get("pm1", d.get("pm1_0", 0))),
        "pm2_5": int(d.get("pm2_5", 0)),
        "pm10": int(d.get("pm10", 0)),
    }


def save_row_to_csv(csv_path: str, timestamp: str, s: dict) -> None:
    """Tạo header nếu file chưa có và ghi thêm 1 dòng dữ liệu mới."""
    is_new = not (os.path.isfile(csv_path) and os.path.getsize(csv_path) > 0)
    with open(csv_path, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(CSV_HEADERS)
        writer.writerow([
            timestamp,
            f"{s['temp']:.2f}", f"{s['hum']:.2f}", f"{s['pres']:.2f}",
            f"{s['gas_ohm']:.0f}", s["gas_ok"],
            s["pm1"], s["pm2_5"], s["pm10"], s["bme_ok"], s["pms_ok"],
        ])


def post_to_api(api_url: str, s: dict) -> None:
    """Gửi tùy chọn dữ liệu lên API bằng thư viện chuẩn Python."""
    body = json.dumps({
        "temp": s["temp"], "humidity": s["hum"], "pressure": s["pres"],
        "gas_res": s["gas_ohm"], "gas_valid": s["gas_ok"],
        "pm1": s["pm1"], "pm2_5": s["pm2_5"], "pm10": s["pm10"],
    }).encode("utf-8")
    req = url_request.Request(api_url, data=body, headers={"Content-Type": "application/json"})
    with url_request.urlopen(req, timeout=2.0):
        pass


# ============================================================================
# 3. HIỂN THỊ RA MÀN HÌNH
# ============================================================================
def print_sample(timestamp: str, s: dict) -> None:
    """In dữ liệu cảm biến gọn gàng theo từng mốc thời gian."""
    print(f"\n[{timestamp}]")
    if s["bme_ok"]:
        gas = f"{s['gas_ohm']:,.0f} Ω" if s["gas_ok"] else "Đang ổn định..."
        print(f"  BME688 : {s['temp']:.2f}°C | {s['hum']:.2f}% | {s['pres']:.2f} hPa | Gas {gas}")
    else:
        print("  BME688 : Lỗi / chưa kết nối")

    if s["pms_ok"]:
        print(f"  PMS5003: PM1.0={s['pm1']} | PM2.5={s['pm2_5']} | PM10={s['pm10']} µg/m³")
    else:
        print("  PMS5003: Chưa có dữ liệu UART")


# ============================================================================
# 4. VÒNG LẶP ĐỌC & LƯU LIÊN TỤC
# ============================================================================
def open_serial(port: str, baud: int) -> serial.Serial:
    """Mở cổng Serial và xóa bộ đệm cũ."""
    ser = serial.Serial(port=port, baudrate=baud, timeout=1.0)
    time.sleep(0.5)
    ser.reset_input_buffer()
    print(f"[OK] Đang đọc liên tục {port} ({baud} baud). Nhấn Ctrl+C để dừng.")
    return ser


def read_serial_to_csv(
    port: str,
    baud: int = DEFAULT_BAUD,
    csv_path: str | None = DEFAULT_CSV,
    api_url: str | None = None,
) -> None:
    """Đọc Serial liên tục và lưu từng mẫu vào CSV (tự kết nối lại nếu đứt)."""
    if csv_path:
        print(f"[CSV] Lưu liên tục vào: {csv_path}")

    ser = None
    try:
        while True:
            try:
                if ser is None or not ser.is_open:
                    ser = open_serial(port, baud)

                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                sample = parse_sensor_json(line)
                if sample is None:
                    print(f"[LOG] {line}")
                    continue

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print_sample(now, sample)
                if csv_path:
                    save_row_to_csv(csv_path, now, sample)
                if api_url:
                    try:
                        post_to_api(api_url, sample)
                    except Exception as exc:
                        print(f"  [API LỖI] {exc}")

            except (serial.SerialException, OSError) as exc:
                print(f"\n[MẤT KẾT NỐI] {exc} -> Thử lại sau 2s...")
                if ser is not None and ser.is_open:
                    ser.close()
                ser = None
                time.sleep(2.0)

    except KeyboardInterrupt:
        print("\n[ĐÃ DỪNG] Đóng cổng Serial.")
    finally:
        if ser is not None and ser.is_open:
            ser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Đọc Serial từ ESP32 và lưu CSV liên tục")
    parser.add_argument("-p", "--port", help="Cổng Serial (vd: /dev/cu.usbmodem11401, COM3)")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help="Baudrate (115200)")
    parser.add_argument("-s", "--save-csv", default=DEFAULT_CSV, help="File CSV lưu dữ liệu")
    parser.add_argument("--no-csv", action="store_true", help="Không lưu CSV")
    parser.add_argument("-a", "--api", help="URL Backend API (tùy chọn)")
    parser.add_argument("-l", "--list", action="store_true", help="Liệt kê cổng Serial")
    args = parser.parse_args()

    if args.list:
        list_ports()
        return

    port = args.port or find_esp32_port()
    if not port:
        print("[LỖI] Không tìm thấy ESP32. Các cổng hiện có:")
        list_ports()
        return

    read_serial_to_csv(
        port=port,
        baud=args.baud,
        csv_path=None if args.no_csv else args.save_csv,
        api_url=args.api,
    )


if __name__ == "__main__":
    main()
