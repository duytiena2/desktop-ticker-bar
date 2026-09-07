# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Tổng quan

`desktop-ticker-bar` là ứng dụng thanh giá desktop Windows viết bằng **Python + Tkinter**
(single-file: [`ticker.py`](ticker.py)). Hiển thị giá Crypto/P2P (Binance), Vàng SJC
(vnappmob) và Chứng khoán (Yahoo Finance) trên một thanh mỏng luôn nổi trên desktop.

- Chạy: `python ticker.py` (hoặc `pythonw.exe` để ẩn console — xem README).
- Không có build step; phụ thuộc trong [`requirements.txt`](requirements.txt)
  (`pystray`/`Pillow` là tùy chọn cho tray icon).
- Test: `python -m pytest tests/ -q`.

## Kiến trúc

Ba lớp chính trong [`ticker.py`](ticker.py):

- **Hàm thuần** (đầu file): `format_price_text`, `format_change_display`,
  `vn_market_open` — không phụ thuộc Tkinter, là nơi đặt logic dễ unit-test.
- **`PriceProvider`** — mọi lời gọi API. Dùng chung một `requests.Session`
  (tái dùng TCP) với `Retry(total=1)` cho lỗi 5xx. Mỗi nguồn có 1 method
  (`get_crypto_price`, `get_p2p_price`, `get_gold_price`, `get_stock_price`),
  `fetch_price(item)` dispatch theo `item["type"]`. Tất cả trả về tuple
  `(price, change_pct, extra_info)` và trả `(None, None, None)` khi lỗi.
- **`TickerApp`** — UI, config, vòng lặp cập nhật, context menu, tray, settings.
  `Tooltip` là helper hiển thị popup chi tiết khi hover.

## Mô hình threading & lock (QUAN TRỌNG)

Có **worker thread** nền (`_update_data`) và **main/UI thread** (Tkinter mainloop).
Cập nhật UI từ worker LUÔN qua `self.root.after(0, ...)` — không gọi widget trực tiếp
từ worker.

Hai lock, **không được lồng theo thứ tự ngược nhau** (tránh deadlock):

- `_prices_lock` — bảo vệ `self.last_prices` (cache giá gần nhất).
- `_state_lock` — bảo vệ `_next_fetch`, `_last_rendered`, `_alert_state`.

Quy ước hiện tại: chỉ có 1 chỗ lồng lock là đọc `last_prices` (dưới `_prices_lock`)
*trước* khi vào `_state_lock` trong `_update_data`. **Đừng** acquire `_prices_lock`
khi đang giữ `_state_lock`.

`self.symbols` được sửa bằng cách **gán lại list mới** (atomic trong CPython), không
sửa in-place — worker chụp `list(self.symbols)` mỗi vòng. Giữ nguyên quy ước này khi
thêm thao tác sửa symbols (xem `_move_symbol`, `_remove_symbol`).

`FUTURE_TIMEOUT` phải ≥ thời gian tối đa 1 request (đã tính cả retry của Session),
nếu không future bị bỏ trong khi thread nền vẫn chạy hợp lệ.

## Vòng lặp cập nhật (`_update_data`)

- Mỗi loại tài sản có chu kỳ riêng: `TYPE_INTERVALS` (crypto 10s, p2p 20s, stock 30s,
  gold 60s); nhịp kiểm tra "đến hạn" là `TICK_INTERVAL` (5s).
- Chạy song song bằng `ThreadPoolExecutor(max_workers=5)`.
- **Chỉ render khi thực sự có dữ liệu mới** (`results`), không render mỗi tick.
- Chứng khoán sàn VN (symbol kết thúc `.VN`, gồm cả `^VNINDEX.VN`) **ngừng poll ngoài
  giờ giao dịch** (`vn_market_open`, T2–T6 9h–15h) một khi đã có giá đóng cửa trong
  cache — lần fetch đầu vẫn chạy để lấy giá đóng cửa.
- `_force_reload()` set `_reload_event` để cập nhật ngay (bỏ qua chu kỳ và cả check
  giờ giao dịch).

## Render (`_render_symbols`)

- Chạy trên main thread. Tái sử dụng widget theo `key = f"{type}_{symbol}"` để tránh
  nhấp nháy — chỉ tạo/xóa widget khi symbol thay đổi.
- **Chỉ re-pack lại toàn bộ** khi thứ tự đổi (`order_sig`) hoặc vừa tạo widget mới,
  không pack lại mỗi lần cập nhật giá.

## Cache & fallback

- Lỗi mạng → hiển thị giá cache gần nhất kèm hậu tố `*`.
- Chưa từng lấy được và không có cache → hiển thị `--` (placeholder).
- `get_stock_price` tự fallback `query1` → `query2` của Yahoo.
- Gold: `get_gold_price` tự refresh Bearer token khi 401; `_refresh_gold_token` gọi
  mạng NGOÀI lock, chỉ khóa khi ghi token.

## Config

- [`config.json`](config.json) (được gitignore) lưu `x`, `y`, `symbols`, `theme`.
  Tự tạo với `DEFAULT_SYMBOLS`/`DEFAULT_THEME` nếu chưa có.
- `symbols` là list object `{type, symbol, label}` + tùy chọn `alert_above`/`alert_below`.
  `type` hợp lệ: `crypto | p2p | gold | stock` (`VALID_TYPES`). Sửa qua cửa sổ Settings
  (chỉnh JSON trực tiếp) — được kiểm tra bằng `_validate_symbols`.
- `ticker.log*` được gitignore (RotatingFileHandler, 512KB × 2 backup).

## Quy ước khi sửa code

- Comment/docstring bằng **tiếng Việt** theo phong cách hiện có; giữ các "banner"
  phân mục `# ═══...`.
- Logic mới nên tách thành **hàm thuần** (không đụng Tkinter) để test được, rồi thêm
  test vào [`tests/test_ticker.py`](tests/test_ticker.py). Test dùng
  `TickerApp.__new__` + `FakeProvider`/`_FakeSession` để **không gọi API thật** và
  không cần màn hình — giữ nguyên cách này.
- Chạy `python -m pytest tests/ -q` trước khi commit.
