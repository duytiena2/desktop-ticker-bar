# Desktop Ticker Bar

Ứng dụng thanh hiển thị giá tiện lợi chạy trực tiếp trên Desktop Windows sử dụng Python và Tkinter. Cho phép theo dõi nhanh giá tiền mã hóa (Crypto), giá P2P, giá Vàng và các mã chứng khoán (hoặc chỉ số chứng khoán) trong thời gian thực.

![Desktop Ticker Bar Preview](https://raw.githubusercontent.com/username/desktop-ticker-bar/main/preview.png) *(Thay thế link này bằng ảnh thực tế sau khi deploy)*

---

## ✨ Tính năng chính

- 🚀 **Cập nhật đa dạng nguồn giá**:
  - **Crypto**: Lấy trực tiếp từ Binance API (BTC, ETH, v.v.).
  - **P2P**: Lấy giá mua/bán P2P từ Binance (USDT/VND).
  - **Vàng**: Lấy giá SJC từ vnappmob API.
  - **Chứng khoán**: Lấy từ Yahoo Finance (VNI, FPT, VN30, v.v.).
- 🧵 **Tải dữ liệu song song**: Sử dụng `ThreadPoolExecutor` để gọi API đồng thời, không làm đơ giao diện (UI).
- 💾 **Lưu vị trí thông minh**: Tự động lưu tọa độ của thanh giá trên màn hình (lần sau mở lại sẽ đúng vị trí cũ).
- 🖱️ **Kéo thả linh hoạt**: Nhấp giữ chuột trái để di chuyển thanh giá đến bất kỳ đâu trên màn hình.
- ⚙️ **Menu chuột phải**:
  - **Cài đặt**: Chỉnh sửa trực tiếp danh sách mã hiển thị bằng định dạng JSON.
  - **Tải lại ngay**: Buộc cập nhật dữ liệu lập tức mà không cần đợi hết chu kỳ.
  - **Thoát**: Tắt ứng dụng an toàn.
- 🛡️ **Chống mất kết nối (Offline Cache)**: Lưu lại giá trị gần nhất để hiển thị kèm dấu hiệu nhận biết (`*`) khi mạng bị lỗi hoặc mất kết nối.

---

## 🛠️ Yêu cầu hệ thống & Cài đặt

1. **Python 3.x** đã được cài đặt trên máy.
2. Cài đặt các thư viện cần thiết bằng cách mở Command Prompt / PowerShell tại thư mục này và chạy:
   ```bash
   pip install -r requirements.txt
   ```

---

## 🚀 Hướng dẫn sử dụng

1. **Chạy ứng dụng**:
   ```bash
   python ticker.py
   ```
2. **Di chuyển**: Nhấp chuột trái và kéo để di chuyển thanh giá.
3. **Cài đặt (Đổi mã/Đổi nhãn)**:
   - Nhấp đúp chuột trái vào thanh giá hoặc nhấp chuột phải chọn **Cài đặt** (Settings).
   - Chỉnh sửa file JSON cấu hình trực tiếp trên cửa sổ ứng dụng và nhấn **Lưu Cấu Hình**.
4. **Thoát**: Nhấp chuột phải chọn **Thoát**.

---

## 📝 Định dạng cấu hình JSON mẫu

Dưới đây là định dạng cấu hình hiển thị:
```json
[
    {"type": "crypto", "symbol": "BTC", "label": "BTC"},
    {"type": "p2p", "symbol": "USDT", "label": "USDT"},
    {"type": "gold", "symbol": "sjc", "label": "SJC"},
    {"type": "stock", "symbol": "FPT.VN", "label": "FPT"}
]
```

*Giải thích các loại `type`:*
- `crypto`: Binance spot price. `symbol` là mã token (ví dụ: `BTC`, `ETH`).
- `p2p`: Binance P2P. `symbol` là loại asset cần theo dõi (ví dụ: `USDT`, `FDUSD`).
- `gold`: Giá vàng SJC Việt Nam. `symbol` là `sjc`.
- `stock`: Chứng khoán Yahoo Finance. `symbol` cần định dạng theo Yahoo (ví dụ: `FPT.VN`, `^VNINDEX.VN` cho chỉ số VN-Index).

---

## ⚙️ Cấu hình khởi động cùng Windows (Startup)

Để ứng dụng tự động chạy mỗi khi bạn mở máy tính:
1. Nhấn tổ hợp phím `Win + R`, gõ `shell:startup` và nhấn **Enter**. Thư mục Startup của Windows sẽ hiện ra.
2. Tạo một file Shortcut (hoặc file `.bat` trung gian) trỏ đến file chạy của ứng dụng:
   - Nội dung file `.bat` (ví dụ `run_ticker.bat`):
     ```bat
     @echo off
     cd /d "Đường_dẫn_thư_mục_chứa_code"
     start pythonw.exe ticker.py
     ```
   *(Sử dụng `pythonw.exe` thay vì `python.exe` giúp chạy ẩn cửa sổ console đen chỉ hiển thị thanh giá).*
3. Kéo file `.bat` này vào thư mục Startup vừa mở.
