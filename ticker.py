"""
Desktop Ticker Bar — Thanh hiển thị giá tài sản đa loại trên Desktop Windows.
Hỗ trợ: Crypto (Binance), P2P (Binance), Vàng SJC (vnappmob), Chứng khoán (Yahoo Finance).
"""

import tkinter as tk
from tkinter import messagebox, colorchooser
import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.util.retry import Retry
import threading
import json
import os
import sys
import time
import logging
import webbrowser
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor

# ── Tắt cảnh báo SSL (cho API vnappmob) ───────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Hằng số ────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "ticker.log")

API_TIMEOUT = 5              # Thời gian chờ API tối đa (giây)
# Session tự retry 1 lần (xem Retry ở PriceProvider) nên 1 request có thể kéo dài
# tới ~2×API_TIMEOUT. Timeout của future phải lớn hơn để không bỏ future khi thread
# nền vẫn đang chạy hợp lệ.
FUTURE_TIMEOUT = API_TIMEOUT * 2 + 2
DEFAULT_UPDATE_INTERVAL = 10  # Chu kỳ cập nhật mặc định (giây)
TYPE_INTERVALS = {           # Chu kỳ cập nhật riêng theo loại tài sản (giây)
    "crypto": 10,
    "p2p": 20,
    "gold": 60,
    "stock": 30,
}
TICK_INTERVAL = 5            # Nhịp kiểm tra "đến hạn cập nhật" của vòng lặp chính
KEEP_ON_TOP_INTERVAL = 2000  # Chu kỳ giữ cửa sổ ở trên cùng (ms)
SNAP_THRESHOLD = 20          # Khoảng cách (px) để tự hít vào mép màn hình khi kéo
AUTOSTART_NAME = "DesktopTickerBar"  # Tên value trong HKCU\...\Run (khởi động cùng Windows)
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TRANSPARENT_COLOR = "#000001"
ALERT_FLASH_MS = 3000        # Thời gian highlight khi có cảnh báo giá (ms)

REQUIRED_KEYS = {"type", "symbol", "label"}
VALID_TYPES = {"crypto", "p2p", "gold", "stock"}

DEFAULT_SYMBOLS = [
    {"type": "crypto", "symbol": "BTC", "label": "BTC"},
    {"type": "p2p", "symbol": "USDT", "label": "USDT"},
    {"type": "gold", "symbol": "sjc", "label": "SJC"},
    {"type": "stock", "symbol": "^VNINDEX.VN", "label": "VNI"},
    {"type": "stock", "symbol": "FPT.VN", "label": "FPT"},
    {"type": "stock", "symbol": "E1VFVN30.VN", "label": "VN30"},
]

# Danh mục mã gợi ý để chọn nhanh trong Settings (người dùng vẫn gõ tay mã khác được).
SYMBOL_PRESETS = {
    "crypto": ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "TRX",
               "DOT", "LINK", "AVAX", "MATIC", "LTC", "SHIB"],
    "p2p": ["USDT", "USDC", "BTC", "ETH", "BNB"],
    "gold": ["sjc"],
    "stock": ["^VNINDEX.VN", "E1VFVN30.VN", "FPT.VN", "VCB.VN", "VIC.VN",
              "VHM.VN", "HPG.VN", "MWG.VN", "VNM.VN", "MSN.VN", "TCB.VN", "ACB.VN"],
}

DEFAULT_THEME = {
    "font_family": "Consolas",
    "font_size": 9,
    # Nền đặc để kéo được thanh ở mọi chỗ (đặt = TRANSPARENT_COLOR nếu muốn nền
    # trong suốt hoàn toàn như cũ — nhưng khi đó chỉ kéo được khi bấm trúng chữ).
    "bg": "#11111b",
    "fg": "white",
    "positive_color": "#a6e3a1",
    "negative_color": "#f38ba8",
    "separator_color": "#585b70",
    "alert_color": "#f9e2af",
    "opacity": 1.0,
    "pad_x": 10,  # Đệm ngang (px mỗi bên) — tăng để thanh rộng/thoáng hơn
    "pad_y": 2,   # Đệm dọc (px mỗi bên) — tăng để thanh cao/dày hơn
}

# Preset theme chọn nhanh trong Settings (preset "Trong suốt" dùng lại TRANSPARENT_COLOR
# để ai thích kiểu "chữ nổi" không nền như bản gốc).
THEME_PRESETS = {
    "Tối (Mocha)": {"bg": "#11111b", "fg": "#cdd6f4", "separator_color": "#585b70"},
    "Sáng (Light)": {"bg": "#eff1f5", "fg": "#4c4f69", "separator_color": "#9ca0b0"},
    "Trong suốt": {"bg": TRANSPARENT_COLOR, "fg": "#ffffff", "separator_color": "#585b70"},
}

# Danh sách font gợi ý trong Settings (ưu tiên monospace cho bảng giá thẳng cột).
FONT_FAMILIES = ["Consolas", "Cascadia Mono", "Courier New", "Segoe UI", "Tahoma", "Arial", "Verdana"]

# Đường dẫn trang nguồn giá của từng loại tài sản (mở qua menu chuột phải)
SOURCE_URLS = {
    "crypto": lambda sym: f"https://www.binance.com/en/trade/{sym}_USDT",
    "p2p": lambda sym: f"https://p2p.binance.com/en/trade/all-payments/{sym}?fiat=VND",
    "gold": lambda sym: "https://sjc.com.vn/bieu-do-gia-vang",
    "stock": lambda sym: f"https://finance.yahoo.com/quote/{sym}",
}


# ═══════════════════════════════════════════════════════════
#  Logging
# ═══════════════════════════════════════════════════════════

def _setup_logger():
    logger = logging.getLogger("ticker")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


logger = _setup_logger()


# ═══════════════════════════════════════════════════════════
#  Hàm thuần túy (dễ unit test, không phụ thuộc Tkinter)
# ═══════════════════════════════════════════════════════════

def format_crypto_usd(price):
    """Định dạng số USD cho crypto theo độ lớn để không làm tròn mất số lẻ với coin giá nhỏ."""
    abs_p = abs(price)
    if abs_p == 0:
        return "0"
    if abs_p >= 100:
        return f"{price:,.0f}"
    if abs_p >= 1:
        return f"{price:,.2f}"
    if abs_p >= 0.01:
        return f"{price:,.4f}"
    return f"{price:,.6f}"


def format_price_text(label, price, symbol_type, is_fallback=False, no_data=False):
    """Định dạng chuỗi hiển thị giá cho một mã."""
    if no_data:
        return f"{label}:--"
    suffix = "*" if is_fallback else ""
    if symbol_type == "crypto":
        return f"{label}:${format_crypto_usd(price)}{suffix}"
    return f"{label}:{price:,.0f}đ{suffix}"


def format_change_display(change_pct, theme=None):
    """Trả về (text, color) cho % thay đổi giá."""
    theme = theme or DEFAULT_THEME
    if change_pct is None:
        return "", theme["fg"]
    arrow = "▲" if change_pct >= 0 else "▼"
    sign = "+" if change_pct >= 0 else ""
    text = f"{arrow}{sign}{change_pct:.1f}%"
    color = theme["positive_color"] if change_pct >= 0 else theme["negative_color"]
    return text, color


def vn_market_open(lt=None):
    """True nếu đang trong giờ giao dịch sàn VN (Thứ 2–6, 09:00–15:00 giờ địa phương).

    ``lt`` là ``time.struct_time`` (mặc định thời gian hiện tại) — tách ra để dễ test.
    """
    lt = lt or time.localtime()
    if lt.tm_wday >= 5:  # 5 = Thứ 7, 6 = Chủ nhật
        return False
    minutes = lt.tm_hour * 60 + lt.tm_min
    return 9 * 60 <= minutes <= 15 * 60


def calc_tooltip_pos(
    x_root,
    y_root,
    screen_w,
    screen_h,
    tip_w=0,
    tip_h=0,
    offset_x=15,
    offset_y=15,
    offset_y_above=95,
    screen_margin=8,
):
    """Tính tọa độ (x, y) cho Tooltip, tự động lật/kẹp để không tràn mép phải/dưới màn hình."""
    x = x_root + offset_x
    if tip_w > 0 and x + tip_w + screen_margin > screen_w:
        x = max(screen_margin, x_root - tip_w - offset_x)

    if y_root > screen_h / 2 or (tip_h > 0 and y_root + offset_y + tip_h + screen_margin > screen_h):
        above_offset = (tip_h + offset_y) if tip_h > 0 else offset_y_above
        y = max(screen_margin, y_root - above_offset)
    else:
        y = y_root + offset_y
    return x, y


def clamp_window_pos(x, y, win_w, win_h, screen_w, screen_h, screen_x=0, screen_y=0):
    """Kẹp tọa độ (x, y) để thanh giá luôn nằm gọn trong vùng màn hình.

    ``screen_x``/``screen_y`` là gốc của vùng (mặc định 0,0 = màn hình chính). Khi
    truyền gốc âm/offset của virtual desktop, hàm hỗ trợ cả cấu hình đa màn hình.
    """
    max_x = max(screen_x, screen_x + screen_w - win_w)
    max_y = max(screen_y, screen_y + screen_h - win_h)
    clamped_x = max(screen_x, min(int(x), max_x))
    clamped_y = max(screen_y, min(int(y), max_y))
    return clamped_x, clamped_y


def snap_to_edges(x, y, win_w, win_h, screen_x, screen_y, screen_w, screen_h,
                  threshold=SNAP_THRESHOLD):
    """Tự "hít" cửa sổ vào mép màn hình gần nhất nếu đang cách mép <= threshold px.

    Giúp canh thẳng thanh giá theo mép trên/dưới/trái/phải khi thả chuột.
    """
    right = screen_x + screen_w
    bottom = screen_y + screen_h
    if abs(x - screen_x) <= threshold:
        x = screen_x
    elif abs((x + win_w) - right) <= threshold:
        x = right - win_w
    if abs(y - screen_y) <= threshold:
        y = screen_y
    elif abs((y + win_h) - bottom) <= threshold:
        y = bottom - win_h
    return x, y


def get_virtual_screen():
    """Trả về (left, top, width, height) của toàn bộ desktop ảo (gộp mọi màn hình) trên Windows.

    Trả ``None`` nếu không lấy được (không phải Windows) để caller fallback về màn hình chính.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, SM_CXVIRTUALSCREEN=78, SM_CYVIRTUALSCREEN=79
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        if width > 0 and height > 0:
            return left, top, width, height
    except Exception:
        pass
    return None


def compute_bar_size(content_w, content_h, pad_x, pad_y, min_w=80, min_h=18):
    """Tính (width, height) cửa sổ thanh giá từ kích thước nội dung + đệm, có cận tối thiểu."""
    return (max(min_w, int(content_w) + 2 * int(pad_x)),
            max(min_h, int(content_h) + 2 * int(pad_y)))


def default_label(symbol):
    """Gợi ý nhãn gọn từ mã: bỏ dấu '^' đầu và đuôi '.VN' (vd ^VNINDEX.VN -> VNINDEX)."""
    s = (symbol or "").lstrip("^")
    if s.upper().endswith(".VN"):
        s = s[:-3]
    return s or symbol


def parse_symbol_rows(rows):
    """Chuyển danh sách dòng (chuỗi) từ form Settings thành list symbol dict.

    - Bỏ qua dòng trống hoàn toàn.
    - ``label`` rỗng sẽ mặc định bằng ``symbol``.
    - ``alert_above``/``alert_below`` nếu có phải là số (cho phép dấu phẩy ngăn cách).
    Ném ``ValueError`` nếu ô cảnh báo không phải số hợp lệ.
    """
    result = []
    for i, r in enumerate(rows, 1):
        typ = (r.get("type") or "").strip()
        sym = (r.get("symbol") or "").strip()
        label = (r.get("label") or "").strip()
        a_above = (r.get("alert_above") or "").strip()
        a_below = (r.get("alert_below") or "").strip()

        if not sym and not label and not a_above and not a_below:
            continue  # dòng trống -> bỏ

        item = {"type": typ, "symbol": sym, "label": label or sym}
        for field, raw in (("alert_above", a_above), ("alert_below", a_below)):
            if raw:
                try:
                    item[field] = float(raw.replace(",", ""))
                except ValueError:
                    raise ValueError(f"Dòng #{i}: '{field}' phải là số (nhận '{raw}')")
        result.append(item)
    return result


# ═══════════════════════════════════════════════════════════
#  Tooltip — Cửa sổ thông tin chi tiết khi Hover
# ═══════════════════════════════════════════════════════════

class Tooltip:
    """Quản lý hiển thị cửa sổ Tooltip động theo con trỏ chuột.

    Tái sử dụng một ``tk.Toplevel`` duy nhất (``withdraw``/``deiconify``) và hỗ trợ
    trì hoãn ẩn ngắn để không bị nhấp nháy khi di chuột qua các nhãn con trong cùng 1 mã.
    """

    # Khoảng lệch (px) của tooltip so với con trỏ chuột
    OFFSET_X = 15
    OFFSET_Y = 15
    OFFSET_Y_ABOVE = 95  # Lệch lên mặc định khi chưa đo kích thước thật của tooltip
    HIDE_DELAY_MS = 40   # Độ trễ ẩn ngắn để chống nhấp nháy khi di qua ranh giới widget con

    def __init__(self, widget, font=("Consolas", 9)):
        self.widget = widget
        self.font = font
        self.tipwindow = None
        self._label = None
        self._current_text = None
        self._tip_w = 0
        self._tip_h = 0
        self._hide_after_id = None

    def set_font(self, font):
        """Đổi font tooltip; hủy cửa sổ cũ để lần hiện sau dựng lại với font mới."""
        self.font = font
        if self.tipwindow is not None:
            try:
                self.tipwindow.destroy()
            except Exception:
                pass
            self.tipwindow = None
            self._label = None
            self._current_text = None

    def _cancel_scheduled_hide(self):
        if self._hide_after_id is not None:
            try:
                self.widget.after_cancel(self._hide_after_id)
            except Exception:
                pass
            self._hide_after_id = None

    def _calc_pos(self, x_root, y_root):
        """Tính vị trí tooltip; tự lật lên trên / sang trái nếu gần mép màn hình."""
        screen_w = self.widget.winfo_screenwidth()
        screen_h = self.widget.winfo_screenheight()
        return calc_tooltip_pos(
            x_root, y_root, screen_w, screen_h,
            tip_w=self._tip_w, tip_h=self._tip_h,
            offset_x=self.OFFSET_X, offset_y=self.OFFSET_Y,
            offset_y_above=self.OFFSET_Y_ABOVE,
        )

    def showtip(self, text, x_root, y_root):
        """Hiển thị hoặc cập nhật Tooltip tại vị trí chuột."""
        self._cancel_scheduled_hide()
        if not text:
            return

        if self.tipwindow is None:
            self.tipwindow = tw = tk.Toplevel(self.widget)
            tw.wm_overrideredirect(True)
            tw.attributes("-topmost", True)
            try:
                # Trên Windows: cửa sổ tooltip không chặn sự kiện chuột,
                # tránh phát sinh <Leave> giả khi con trỏ chạm vào tooltip.
                tw.wm_attributes("-disabled", True)
            except tk.TclError:
                pass

            # Style Tooltip: Nền tối Catppuccin Mocha, viền mỏng
            self._label = tk.Label(
                tw, text=text, justify="left",
                background="#1e1e2e", foreground="#cdd6f4",
                relief="solid", borderwidth=1,
                font=self.font,
                padx=8, pady=6
            )
            self._label.pack(ipadx=1)
            self._current_text = text
            tw.update_idletasks()
            self._tip_w = tw.winfo_reqwidth()
            self._tip_h = tw.winfo_reqheight()
        else:
            if self._current_text != text:
                self._label.config(text=text)
                self._current_text = text
                self.tipwindow.update_idletasks()
                self._tip_w = self.tipwindow.winfo_reqwidth()
                self._tip_h = self.tipwindow.winfo_reqheight()
            if self.tipwindow.state() == "withdrawn":
                self.tipwindow.deiconify()
                self.tipwindow.attributes("-topmost", True)

        x, y = self._calc_pos(x_root, y_root)
        self.tipwindow.wm_geometry(f"+{x}+{y}")

    def movetip(self, x_root, y_root):
        """Di chuyển Tooltip theo chuột."""
        self._cancel_scheduled_hide()
        if self.tipwindow and self.tipwindow.state() != "withdrawn":
            x, y = self._calc_pos(x_root, y_root)
            self.tipwindow.wm_geometry(f"+{x}+{y}")

    def schedule_hide(self):
        """Lên lịch ẩn Tooltip sau một khoảng ngắn (hủy nếu chuột vào ngay widget kế tiếp)."""
        self._cancel_scheduled_hide()
        self._hide_after_id = self.widget.after(self.HIDE_DELAY_MS, self.hidetip)

    def hidetip(self):
        """Ẩn Tooltip ngay lập tức (giữ lại Toplevel để tái sử dụng)."""
        self._cancel_scheduled_hide()
        if self.tipwindow and self.tipwindow.state() != "withdrawn":
            self.tipwindow.withdraw()


# ═══════════════════════════════════════════════════════════
#  PriceProvider — Quản lý gọi API lấy giá
# ═══════════════════════════════════════════════════════════

class PriceProvider:
    """Lớp chuyên trách gọi API lấy giá và thông tin chi tiết.

    Sử dụng ``requests.Session`` để tái sử dụng TCP connection.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        # Tự động retry 1 lần khi gặp lỗi server tạm thời
        retry_strategy = Retry(
            total=1,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._gold_token = None
        self._gold_token_lock = threading.Lock()

    # ── Crypto (Binance) ───────────────────────────────────

    def get_crypto_price(self, symbol):
        """Lấy giá crypto + % thay đổi + thông tin High/Low/Volume 24h."""
        sym = f"{symbol}USDT" if "USDT" not in symbol else symbol
        try:
            r = self.session.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
                timeout=API_TIMEOUT,
            )
            r.raise_for_status()
            d = r.json()
            price = float(d["lastPrice"])
            change_pct = float(d["priceChangePercent"])
            extra_info = {
                "high": float(d["highPrice"]),
                "low": float(d["lowPrice"]),
                "volume": float(d["volume"]),
                "quote_volume": float(d["quoteVolume"])
            }
            return price, change_pct, extra_info
        except requests.exceptions.HTTPError as e:
            logger.warning("Crypto API HTTP error (%s): %s", sym, e)
        except Exception as e:
            logger.warning("Crypto API error (%s): %s", sym, e)
        return None, None, None

    # ── P2P (Binance) ──────────────────────────────────────

    def get_p2p_price(self, asset="USDT", fiat="VND", trade_type="BUY"):
        """Lấy giá P2P mua/bán từ Binance C2C."""
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        payload = {
            "page": 1, "rows": 1, "payTypes": [],
            "asset": asset, "tradeType": trade_type,
            "fiat": fiat, "publisherType": None,
        }
        try:
            r = self.session.post(url, json=payload, timeout=API_TIMEOUT)
            if r.status_code in (403, 429):
                logger.warning("P2P API bị chặn/giới hạn (status=%s) cho %s", r.status_code, asset)
                return None, None, None
            r.raise_for_status()
            res = r.json()
            if res.get("code") == "000000" and res.get("data"):
                price = float(res["data"][0]["adv"]["price"])
                extra_info = {
                    "trade_type": "MUA" if trade_type == "BUY" else "BÁN",
                    "fiat": fiat,
                    "price": price
                }
                return price, None, extra_info
        except Exception as e:
            logger.warning("P2P API error (%s): %s", asset, e)
        return None, None, None

    # ── Vàng (vnappmob) ────────────────────────────────────

    def _refresh_gold_token(self):
        """Làm mới Bearer token cho API vàng vnappmob (thread-safe).

        Gọi mạng NGOÀI lock để không chặn các luồng khác trong lúc chờ I/O;
        chỉ khóa khi ghi token vào state.
        """
        try:
            r = self.session.get(
                "https://api.vnappmob.com/api/request_api_key?scope=gold",
                timeout=API_TIMEOUT, verify=False,
            )
            if r.status_code == 200:
                data = r.json()
                if "results" in data:
                    with self._gold_token_lock:
                        self._gold_token = data["results"]
                    return True
        except Exception as e:
            logger.warning("Không thể làm mới gold token: %s", e)
        return False

    def get_gold_price(self, symbol):
        """Lấy giá vàng SJC từ vnappmob."""
        sym = symbol.lower()

        if not self._gold_token and not self._refresh_gold_token():
            return None, None, None

        url = f"https://api.vnappmob.com/api/v2/gold/{sym}"

        try:
            with self._gold_token_lock:
                token = self._gold_token
            headers = {"Authorization": f"Bearer {token}"}
            r = self.session.get(url, headers=headers, timeout=API_TIMEOUT, verify=False)

            # Tự động refresh token nếu bị hết hạn (401)
            if r.status_code == 401:
                if self._refresh_gold_token():
                    with self._gold_token_lock:
                        token = self._gold_token
                    headers["Authorization"] = f"Bearer {token}"
                    r = self.session.get(url, headers=headers, timeout=API_TIMEOUT, verify=False)

            if r.status_code == 200:
                data = r.json()
                if "results" in data and len(data["results"]) > 0:
                    info = data["results"][0]
                    price_str = info.get("sell_1l") or info.get("sell_1c")
                    buy_str = info.get("buy_1l") or info.get("buy_1c")
                    if price_str:
                        price = float(price_str)
                        extra_info = {
                            "buy": float(buy_str) if buy_str else None,
                            "sell": price,
                            "type": info.get("gold_type", "SJC")
                        }
                        return price, None, extra_info
            else:
                logger.warning("Gold API trả về status=%s", r.status_code)
        except Exception as e:
            logger.warning("Gold API error (%s): %s", sym, e)
        return None, None, None

    # ── Chứng khoán (Yahoo Finance) ───────────────────────

    def get_stock_price(self, symbol):
        """Lấy giá chứng khoán từ Yahoo Finance.

        Thử lần lượt query1 rồi query2 — query1 đôi khi trả 401/cần crumb,
        query2 thường vẫn phục vụ được.
        """
        for host in ("query1", "query2"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
            try:
                r = self.session.get(url, timeout=API_TIMEOUT)
                r.raise_for_status()
                data = r.json()
                result = data["chart"]["result"]
                if result:
                    meta = result[0]["meta"]
                    price = float(meta["regularMarketPrice"])
                    extra_info = {
                        "high": float(meta.get("regularMarketDayHigh", 0)) or None,
                        "low": float(meta.get("regularMarketDayLow", 0)) or None,
                        "volume": float(meta.get("regularMarketVolume", 0)) or None
                    }
                    return price, None, extra_info
            except Exception as e:
                logger.warning("Stock API error (%s @ %s): %s", symbol, host, e)
        return None, None, None

    # ── Dispatch ───────────────────────────────────────────

    def fetch_price(self, item):
        """Lấy giá cho một mã dựa trên ``type``."""
        t = item["type"]
        sym = item["symbol"]
        if t == "crypto":
            return self.get_crypto_price(sym)
        elif t == "p2p":
            return self.get_p2p_price(sym)
        elif t == "gold":
            return self.get_gold_price(sym)
        else:
            return self.get_stock_price(sym)


# ═══════════════════════════════════════════════════════════
#  TickerApp — Ứng dụng chính
# ═══════════════════════════════════════════════════════════

class TickerApp:
    """Ứng dụng Desktop Ticker Bar."""

    def __init__(self):
        self.root = tk.Tk()
        self.provider = PriceProvider()
        self.last_prices = {}
        self._prices_lock = threading.Lock()
        # Bảo vệ state chia sẻ giữa worker thread và main/UI thread:
        # _next_fetch, _last_rendered, _alert_state.
        self._state_lock = threading.Lock()
        self._reload_event = threading.Event()
        self.symbols = DEFAULT_SYMBOLS[:]
        self.theme = DEFAULT_THEME.copy()

        self.tooltip = Tooltip(self.root)
        self.symbol_widgets = {}
        self._settings_win = None
        self._next_fetch = {}
        self._last_rendered = {}
        self._alert_state = {}
        self._last_order_sig = None  # Chữ ký thứ tự lần render gần nhất (để tránh re-pack thừa)
        self._tray_icon = None
        self._locked = False         # Khóa vị trí: chặn kéo thả khi bật
        self._lock_var = None        # tk.BooleanVar đồng bộ trạng thái khóa trên menu

        self._load_and_apply_config()
        self._setup_ui()
        self._setup_context_menu()
        self._start_update_thread()
        self._setup_tray()

    # ── Config ─────────────────────────────────────────────

    @staticmethod
    def _load_config():
        """Đọc cấu hình từ file JSON."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Không đọc được config.json: %s", e)
        return None

    def _save_config(self, x=None, y=None, symbols=None, theme=None):
        """Ghi cấu hình ra file JSON."""
        if x is None:
            x = self.root.winfo_x()
        if y is None:
            y = self.root.winfo_y()
        if symbols is None:
            symbols = self.symbols
        if theme is None:
            theme = self.theme
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"x": x, "y": y, "symbols": symbols, "theme": theme,
                     "locked": getattr(self, "_locked", False)},
                    f, ensure_ascii=False, indent=4
                )
        except Exception as e:
            logger.warning("Không ghi được config.json: %s", e)

    def _load_and_apply_config(self):
        """Đọc config và áp dụng vị trí + danh sách symbols + theme."""
        config = self._load_config()
        sx, sy, screen_w, screen_h = self._screen_bounds()
        self._width = 300
        self._height = 25

        if config and "x" in config and "y" in config:
            self._x, self._y = clamp_window_pos(
                config["x"], config["y"], self._width, self._height,
                screen_w, screen_h, sx, sy,
            )
            self.symbols = config.get("symbols", DEFAULT_SYMBOLS)
            self.theme = {**DEFAULT_THEME, **config.get("theme", {})}
            self._locked = bool(config.get("locked", False))
        else:
            self._x = 180
            self._y = sy + screen_h - self._height - 10
            self._save_config(self._x, self._y, DEFAULT_SYMBOLS, DEFAULT_THEME)

    def _screen_bounds(self):
        """(x, y, w, h) của vùng để kẹp cửa sổ — ưu tiên virtual desktop đa màn hình."""
        vs = get_virtual_screen()
        if vs:
            return vs
        return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())

    # ── UI Setup ───────────────────────────────────────────

    def _setup_ui(self):
        """Khởi tạo giao diện chính."""
        root = self.root
        root.overrideredirect(True)
        root.configure(bg=self.theme["bg"])
        # Vẫn giữ -transparentcolor để ai muốn nền trong suốt chỉ cần đặt
        # theme["bg"] = TRANSPARENT_COLOR; mặc định bg khác màu này nên thanh đặc,
        # không bị "xuyên chuột" → kéo được ở mọi chỗ trên thanh.
        root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        root.attributes("-alpha", self.theme.get("opacity", 1.0))
        root.geometry(f"{self._width}x{self._height}+{self._x}+{self._y}")

        # Trạng thái kéo thả
        self._drag = {"x": 0, "y": 0, "win_x": self._x, "win_y": self._y, "dragging": False}

        # Frame chứa tất cả các nhãn giá (cho phép hiển thị nhiều màu và hover)
        self.container_frame = tk.Frame(root, bg=self.theme["bg"])
        self.container_frame.pack(expand=True, fill="both",
                                  padx=self.theme.get("pad_x", 10),
                                  pady=self.theme.get("pad_y", 2))

        # Tooltip dùng font theo theme (nhất quán với thanh giá)
        self.tooltip.set_font((self.theme["font_family"], self.theme["font_size"]))

        # Gán sự kiện chuột kéo thả / menu nền trên root (widget con kế thừa qua bindtags)
        self._bind_widget_events(root)

        self._keep_on_top()

    def _setup_context_menu(self):
        """Khởi tạo menu chuột phải (nền thanh giá)."""
        self._lock_var = tk.BooleanVar(value=self._locked)
        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="⚙ Cài đặt", command=self._open_settings)
        self._context_menu.add_command(label="🔄 Tải lại ngay", command=self._force_reload)
        self._context_menu.add_separator()
        self._context_menu.add_checkbutton(label="🔒 Khóa vị trí", variable=self._lock_var,
                                           command=self._apply_lock)
        self._context_menu.add_command(label="🎯 Về vị trí mặc định", command=self._reset_position)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="✕ Thoát", command=self._on_quit)

    def _apply_lock(self):
        """Đồng bộ trạng thái khóa từ BooleanVar của menu và lưu config."""
        self._locked = bool(self._lock_var.get()) if self._lock_var is not None else not self._locked
        self._save_config()

    def _toggle_lock(self):
        """Đảo trạng thái khóa (dùng cho menu từng mã)."""
        self._locked = not self._locked
        if self._lock_var is not None:
            self._lock_var.set(self._locked)
        self._save_config()

    def _reset_position(self):
        """Đưa thanh giá về vị trí mặc định (góc dưới trái vùng màn hình)."""
        sx, sy, sw, sh = self._screen_bounds()
        x = sx + 180
        y = sy + sh - self._height - 10
        x, y = clamp_window_pos(x, y, self._width, self._height, sw, sh, sx, sy)
        self.root.geometry(f"+{x}+{y}")
        self._save_config(x, y)

    def _setup_tray(self):
        """Khởi tạo icon khay hệ thống (tùy chọn, cần pystray + Pillow)."""
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            logger.info("pystray/Pillow chưa cài — bỏ qua tray icon (xem requirements.txt).")
            return

        try:
            img = Image.new("RGB", (32, 32), "#1e1e2e")
            draw = ImageDraw.Draw(img)
            draw.text((8, 8), "$", fill="#a6e3a1")

            menu = pystray.Menu(
                pystray.MenuItem("Hiện / Ẩn", lambda: self.root.after(0, self._toggle_visibility)),
                pystray.MenuItem("Tải lại ngay", lambda: self._force_reload()),
                pystray.MenuItem("Thoát", lambda: self.root.after(0, self._on_quit)),
            )
            self._tray_icon = pystray.Icon("desktop_ticker_bar", img, "Desktop Ticker Bar", menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            logger.warning("Không khởi tạo được tray icon: %s", e)
            self._tray_icon = None

    def _toggle_visibility(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        else:
            self.root.withdraw()

    # ── Bind sự kiện (tận dụng bindtags của root để không lặp handler) ──

    def _bind_widget_events(self, widget, key=None):
        """Gán sự kiện chuột cần thiết cho 1 widget.

        Các sự kiện kéo thả, double-click và menu nền chỉ gán 1 lần trên ``root``
        (vì mọi widget con đều có ``root`` trong ``bindtags``). Widget thuộc về 1 mã
        cụ thể (``key is not None``) chỉ gán riêng ``<Button-3>`` và trả về ``"break"``
        để không kích hoạt trùng menu nền của ``root``.
        """
        if key is not None:
            widget.bind("<Button-3>", lambda e: self._show_item_context_menu(e, key))
        else:
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)
            widget.bind("<ButtonRelease-1>", self._on_drag_stop)
            widget.bind("<Double-Button-1>", self._open_settings)
            widget.bind("<Button-3>", self._show_context_menu)

    # ── Drag & Drop / Mở trang nguồn ───────────────────────

    def _on_drag_start(self, event):
        if getattr(self, "_locked", False):
            return  # Đã khóa vị trí -> không cho kéo
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._drag["x"] = event.x_root
        self._drag["y"] = event.y_root
        self._drag["win_x"] = self.root.winfo_x()
        self._drag["win_y"] = self.root.winfo_y()
        self._drag["dragging"] = True
        self.tooltip.hidetip()

    def _on_drag_motion(self, event):
        if not self._drag.get("dragging"):
            return
        dx = event.x_root - self._drag["x"]
        dy = event.y_root - self._drag["y"]
        nx = self._drag["win_x"] + dx
        ny = self._drag["win_y"] + dy
        self.root.geometry(f"+{nx}+{ny}")

    def _on_drag_stop(self, _event):
        if not self._drag.get("dragging"):
            return
        self._drag["dragging"] = False
        # Hít vào mép màn hình gần nhất khi thả (canh thẳng thanh giá)
        sx, sy, sw, sh = self._screen_bounds()
        cur_x, cur_y = self.root.winfo_x(), self.root.winfo_y()
        snap_x, snap_y = snap_to_edges(cur_x, cur_y, self._width, self._height, sx, sy, sw, sh)
        if (snap_x, snap_y) != (cur_x, cur_y):
            self.root.geometry(f"+{snap_x}+{snap_y}")
        # Chỉ ghi config khi vị trí thực sự thay đổi
        if (self.root.winfo_x() != self._drag["win_x"]
                or self.root.winfo_y() != self._drag["win_y"]):
            self._save_config()

    def _open_source_link(self, key):
        symbol_type, symbol = key.split("_", 1)
        builder = SOURCE_URLS.get(symbol_type)
        if not builder:
            return
        try:
            webbrowser.open(builder(symbol))
        except Exception as e:
            logger.warning("Không mở được link nguồn giá (%s): %s", key, e)

    # ── Context Menu ───────────────────────────────────────

    def _show_context_menu(self, event):
        self.tooltip.hidetip()
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()
        return "break"

    def _show_item_context_menu(self, event, key):
        """Menu chuột phải riêng cho từng mã: mở trang nguồn, di chuyển trái/phải, xóa."""
        self.tooltip.hidetip()
        idx = next((i for i, it in enumerate(self.symbols)
                    if f"{it['type']}_{it['symbol']}" == key), None)
        if idx is None:
            return self._show_context_menu(event)

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="🌐 Mở trang nguồn", command=lambda: self._open_source_link(key))
        menu.add_separator()
        menu.add_command(label="⬅ Di chuyển trái", command=lambda: self._move_symbol(key, -1),
                          state="normal" if idx > 0 else "disabled")
        menu.add_command(label="➡ Di chuyển phải", command=lambda: self._move_symbol(key, 1),
                          state="normal" if idx < len(self.symbols) - 1 else "disabled")
        menu.add_separator()
        menu.add_command(label="⚙ Cài đặt", command=self._open_settings)
        menu.add_command(label="🔄 Tải lại ngay", command=self._force_reload)
        menu.add_checkbutton(label="🔒 Khóa vị trí", variable=self._lock_var,
                             command=self._apply_lock)
        menu.add_command(label="🎯 Về vị trí mặc định", command=self._reset_position)
        menu.add_command(label="✕ Thoát", command=self._on_quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _move_symbol(self, key, direction):
        """Di chuyển 1 mã sang trái (-1) hoặc phải (+1) trong danh sách hiển thị."""
        idx = next((i for i, it in enumerate(self.symbols)
                    if f"{it['type']}_{it['symbol']}" == key), None)
        if idx is None:
            return
        new_idx = idx + direction
        if not (0 <= new_idx < len(self.symbols)):
            return
        # Tạo danh sách mới rồi gán lại (gán reference là atomic trong CPython),
        # tránh sửa in-place gây race với worker thread đang lặp self.symbols.
        new_symbols = list(self.symbols)
        new_symbols[idx], new_symbols[new_idx] = new_symbols[new_idx], new_symbols[idx]
        self.symbols = new_symbols
        self._save_config()
        with self._state_lock:
            snapshot = dict(self._last_rendered)
        self._render_symbols(snapshot)

    def _remove_symbol(self, key):
        """Xóa 1 mã khỏi danh sách hiển thị."""
        self.symbols = [it for it in self.symbols if f"{it['type']}_{it['symbol']}" != key]
        self._save_config()
        if key in self.symbol_widgets:
            self.symbol_widgets[key]["frame"].destroy()
            del self.symbol_widgets[key]
        with self._state_lock:
            self._last_rendered.pop(key, None)
            snapshot = dict(self._last_rendered)
        self._render_symbols(snapshot)

    # ── Quit ───────────────────────────────────────────────

    def _on_quit(self):
        """Thoát ứng dụng, đóng HTTP session và tray icon đúng cách."""
        try:
            self.provider.session.close()
        except Exception:
            pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    # ── Keep on Top ────────────────────────────────────────

    def _keep_on_top(self):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(KEEP_ON_TOP_INTERVAL, self._keep_on_top)

    # ── Tooltip Event Handlers ─────────────────────────────

    def _show_tooltip(self, event, key):
        """Tạo nội dung và hiển thị Tooltip khi hover vào mã."""
        with self._prices_lock:
            data = self.last_prices.get(key)
        if not data or "extra_info" not in data or not data["extra_info"]:
            self.tooltip.hidetip()
            return

        symbol = key.split("_", 1)[1]
        t = key.split("_", 1)[0]
        info = data["extra_info"]
        tip_text = ""

        if t == "crypto":
            high_str = f"{info['high']:,.2f}" if abs(info["high"]) >= 1 else format_crypto_usd(info["high"])
            low_str = f"{info['low']:,.2f}" if abs(info["low"]) >= 1 else format_crypto_usd(info["low"])
            tip_text = (
                f"⭐ Binance Crypto: {symbol}\n"
                f"📈 Cao nhất 24h : ${high_str}\n"
                f"📉 Thấp nhất 24h: ${low_str}\n"
                f"📊 Khối lượng    : {info['volume']:,.2f} {symbol}"
            )
        elif t == "stock":
            high_str = f"{info['high']:,.0f}đ" if info.get("high") else "N/A"
            low_str = f"{info['low']:,.0f}đ" if info.get("low") else "N/A"
            vol_str = f"{info['volume']:,.0f}" if info.get("volume") else "N/A"
            tip_text = (
                f"📈 Chứng khoán: {symbol}\n"
                f"🔺 Cao nhất ngày : {high_str}\n"
                f"🔻 Thấp nhất ngày: {low_str}\n"
                f"📊 Khối lượng    : {vol_str}"
            )
        elif t == "gold":
            buy_str = f"{info['buy']:,.0f}đ" if info.get("buy") else "N/A"
            tip_text = (
                f"🏆 Vàng Việt Nam: {symbol.upper()}\n"
                f"💰 Giá Bán: {info['sell']:,.0f}đ\n"
                f"📥 Giá Mua: {buy_str}\n"
                f"🏷️ Loại vàng: {info['type']}"
            )
        elif t == "p2p":
            tip_text = (
                f"🤝 Binance P2P: {symbol}\n"
                f"💵 Giá {info['trade_type']}: {info['price']:,.0f} {info['fiat']}\n"
                f"⚡ Thanh toán: Chuyển khoản ngân hàng"
            )

        self.tooltip.showtip(tip_text, event.x_root, event.y_root)

    def _move_tooltip(self, event):
        self.tooltip.movetip(event.x_root, event.y_root)

    def _hide_tooltip(self, _event):
        self.tooltip.schedule_hide()

    # ── Data Fetching & Rendering ──────────────────────────

    def _check_alert(self, cache_key, item, price):
        """Kiểm tra ngưỡng cảnh báo giá (alert_above/alert_below) nếu có cấu hình."""
        above = item.get("alert_above")
        below = item.get("alert_below")
        if above is None and below is None:
            return False

        breached = (above is not None and price >= above) or (below is not None and price <= below)
        with self._state_lock:
            prev = self._alert_state.get(cache_key, False)
            self._alert_state[cache_key] = breached
        if breached and not prev:
            logger.info("Cảnh báo giá: %s đạt %.2f (ngưỡng trên=%s, dưới=%s)",
                        cache_key, price, above, below)
            return True
        return False

    def _fetch_single(self, item):
        """Lấy giá cho 1 mã, có cache fallback khi lỗi mạng."""
        symbol_type = item["type"]
        symbol = item["symbol"]
        label_str = item["label"]

        price, change_pct, extra_info = self.provider.fetch_price(item)

        cache_key = f"{symbol_type}_{symbol}"
        is_fallback = False

        if price is not None:
            with self._prices_lock:
                self.last_prices[cache_key] = {
                    "price": price,
                    "change_pct": change_pct,
                    "extra_info": extra_info
                }
        else:
            with self._prices_lock:
                cached = self.last_prices.get(cache_key)
            if cached:
                price = cached["price"]
                change_pct = cached.get("change_pct")
                extra_info = cached.get("extra_info")
                is_fallback = True

        if price is None:
            # Chưa từng lấy được dữ liệu và không có cache — vẫn hiển thị placeholder rõ ràng
            return {
                "key": cache_key,
                "price_text": format_price_text(label_str, None, symbol_type, no_data=True),
                "change_text": "",
                "change_color": self.theme["fg"],
                "alert": False,
            }

        alert = self._check_alert(cache_key, item, price)

        price_text = format_price_text(label_str, price, symbol_type, is_fallback=is_fallback)
        change_text, change_color = format_change_display(change_pct, self.theme)

        return {
            "key": cache_key,
            "price_text": price_text,
            "change_text": change_text,
            "change_color": change_color,
            "alert": alert,
        }

    def _notify_alert(self, data):
        """Phát âm thanh + balloon tray khi giá vượt ngưỡng (ngoài việc flash nền)."""
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.notify(data.get("price_text", "Giá vượt ngưỡng"), "⚠ Cảnh báo giá")
            except Exception:
                pass

    def _flash_alert(self, frame):
        """Highlight tạm thời nền của 1 mã khi có cảnh báo giá vượt ngưỡng."""
        try:
            frame.configure(bg=self.theme["alert_color"])
            for child in frame.winfo_children():
                child.configure(bg=self.theme["alert_color"])
            self.root.after(ALERT_FLASH_MS, lambda: self._reset_flash(frame))
        except Exception:
            pass

    def _reset_flash(self, frame):
        try:
            frame.configure(bg=self.theme["bg"])
            for child in frame.winfo_children():
                child.configure(bg=self.theme["bg"])
        except Exception:
            pass

    def _render_symbols(self, data_dict):
        """Dựng giao diện động cho các mã từ dữ liệu mới (không bị nhấp nháy)."""
        current_keys = {f"{item['type']}_{item['symbol']}" for item in self.symbols}

        # Xóa các widget của mã không còn tồn tại trong config
        for key in list(self.symbol_widgets.keys()):
            if key not in current_keys:
                self.symbol_widgets[key]["frame"].destroy()
                del self.symbol_widgets[key]

        font = (self.theme["font_family"], self.theme["font_size"])
        created_new = False

        # Cập nhật hoặc tạo mới các widget con
        for i, item in enumerate(self.symbols):
            key = f"{item['type']}_{item['symbol']}"
            data = data_dict.get(key)
            if not data:
                continue

            # Nếu chưa có widget, tiến hành tạo mới
            if key not in self.symbol_widgets:
                created_new = True
                frame = tk.Frame(self.container_frame, bg=self.theme["bg"])
                self._bind_widget_events(frame, key=key)

                lbl_price = tk.Label(
                    frame, fg=self.theme["fg"], bg=self.theme["bg"],
                    font=font, anchor="w"
                )
                lbl_price.pack(side="left")
                self._bind_widget_events(lbl_price, key=key)

                lbl_change = tk.Label(
                    frame, bg=self.theme["bg"],
                    font=font, anchor="w"
                )
                lbl_change.pack(side="left")
                self._bind_widget_events(lbl_change, key=key)

                lbl_sep = tk.Label(
                    frame, text="|", fg=self.theme["separator_color"], bg=self.theme["bg"],
                    font=font
                )

                self.symbol_widgets[key] = {
                    "frame": frame,
                    "lbl_price": lbl_price,
                    "lbl_change": lbl_change,
                    "lbl_sep": lbl_sep
                }

                # Bind sự kiện hover Tooltip cho tất cả widget con của cụm
                for w in (frame, lbl_price, lbl_change):
                    w.bind("<Enter>", lambda e, k=key: self._show_tooltip(e, k))
                    w.bind("<Leave>", self._hide_tooltip)
                    w.bind("<Motion>", self._move_tooltip)

            # Cập nhật văn bản
            widgets = self.symbol_widgets[key]
            widgets["lbl_price"].config(text=data["price_text"])

            # Cập nhật % thay đổi (nếu có)
            if data["change_text"]:
                widgets["lbl_change"].config(text=data["change_text"], fg=data["change_color"])
                widgets["lbl_change"].pack(side="left")
            else:
                widgets["lbl_change"].pack_forget()

            # Quản lý dấu gạch phân cách
            if i < len(self.symbols) - 1:
                widgets["lbl_sep"].pack(side="left")
            else:
                widgets["lbl_sep"].pack_forget()

            if data.get("alert"):
                self._flash_alert(widgets["frame"])
                self._notify_alert(data)

        # Chỉ re-pack lại toàn bộ khi thứ tự thay đổi hoặc vừa tạo widget mới —
        # tránh pack_forget/pack thừa mỗi lần cập nhật giá (giảm CPU & nhấp nháy).
        order_sig = tuple(f"{item['type']}_{item['symbol']}" for item in self.symbols)
        if created_new or order_sig != self._last_order_sig:
            for key in order_sig:
                widgets = self.symbol_widgets.get(key)
                if widgets:
                    widgets["frame"].pack_forget()
                    widgets["frame"].pack(side="left", padx=0)
            self._last_order_sig = order_sig

        self._fit_window_to_content()

    def _fit_window_to_content(self):
        """Tự co giãn chiều rộng & chiều cao cửa sổ vừa khít nội dung và giữ trong màn hình."""
        if not self.symbol_widgets:
            return
        self.root.update_idletasks()
        req_w, req_h = compute_bar_size(
            self.container_frame.winfo_reqwidth(),
            self.container_frame.winfo_reqheight(),
            self.theme.get("pad_x", 10), self.theme.get("pad_y", 2),
        )
        if req_w == self._width and req_h == self._height:
            return

        self._width = req_w
        self._height = req_h
        if self._drag.get("dragging"):
            self.root.geometry(f"{self._width}x{self._height}")
            return

        sx, sy, screen_w, screen_h = self._screen_bounds()
        cur_x, cur_y = clamp_window_pos(
            self.root.winfo_x(), self.root.winfo_y(),
            self._width, self._height, screen_w, screen_h, sx, sy,
        )
        self.root.geometry(f"{self._width}x{self._height}+{cur_x}+{cur_y}")

    def _update_data(self):
        """Vòng lặp cập nhật dữ liệu song song, mỗi loại tài sản có chu kỳ riêng."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            while True:
                now = time.time()
                forced = self._reload_event.is_set()

                # Chụp nhanh danh sách symbols để tránh race khi main thread sửa list
                symbols_snapshot = list(self.symbols)
                market_open = vn_market_open()
                with self._prices_lock:
                    cached_keys = set(self.last_prices.keys())

                due_items = []
                with self._state_lock:
                    for item in symbols_snapshot:
                        key = f"{item['type']}_{item['symbol']}"
                        interval = TYPE_INTERVALS.get(item["type"], DEFAULT_UPDATE_INTERVAL)
                        if not (forced or self._next_fetch.get(key, 0) <= now):
                            continue
                        # Ngoài giờ giao dịch: bỏ qua cập nhật CK sàn VN nếu đã có giá
                        # đóng cửa trong cache (vẫn fetch lần đầu để lấy giá đóng cửa).
                        if (item["type"] == "stock" and item["symbol"].endswith(".VN")
                                and not market_open and not forced and key in cached_keys):
                            self._next_fetch[key] = now + interval
                            continue
                        due_items.append(item)
                        self._next_fetch[key] = now + interval

                if due_items:
                    futures = [executor.submit(self._fetch_single, item) for item in due_items]
                    results = []
                    for fut in futures:
                        try:
                            res = fut.result(timeout=FUTURE_TIMEOUT)
                            if res:
                                results.append(res)
                        except Exception as e:
                            logger.warning("Lỗi khi lấy dữ liệu: %s", e)

                    # Chỉ render khi thực sự có dữ liệu mới (bỏ render dư mỗi tick)
                    if results:
                        with self._state_lock:
                            for res in results:
                                self._last_rendered[res["key"]] = res
                            snapshot = dict(self._last_rendered)
                        self.root.after(0, lambda d=snapshot: self._render_symbols(d))

                self._reload_event.wait(TICK_INTERVAL)
                self._reload_event.clear()

    def _force_reload(self):
        """Kích hoạt tải lại dữ liệu ngay lập tức."""
        self._reload_event.set()

    def _start_update_thread(self):
        threading.Thread(target=self._update_data, daemon=True).start()

    # ── Theme & Autostart ──────────────────────────────────

    def _apply_theme(self):
        """Áp theme hiện tại lên cửa sổ và buộc dựng lại widget mã với màu/font mới."""
        self.root.attributes("-alpha", self.theme.get("opacity", 1.0))
        self.root.configure(bg=self.theme["bg"])
        self.container_frame.configure(bg=self.theme["bg"])
        self.container_frame.pack_configure(padx=self.theme.get("pad_x", 10),
                                            pady=self.theme.get("pad_y", 2))
        self.tooltip.set_font((self.theme["font_family"], self.theme["font_size"]))
        for _key, widgets in list(self.symbol_widgets.items()):
            widgets["frame"].destroy()
        self.symbol_widgets.clear()
        self._last_order_sig = None  # buộc re-pack ở lần render kế

    def _rerender_now(self):
        """Dựng lại ngay thanh giá từ dữ liệu đã render gần nhất (dùng cho live-preview)."""
        with self._state_lock:
            snap = dict(self._last_rendered)
        self._render_symbols(snap)

    @staticmethod
    def _autostart_command():
        """Lệnh chạy khi khởi động Windows (ưu tiên pythonw.exe để ẩn console)."""
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        return f'"{exe}" "{os.path.abspath(__file__)}"'

    @staticmethod
    def _is_autostart_enabled():
        """True nếu đã bật khởi động cùng Windows (có value trong HKCU\\...\\Run)."""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY)
            try:
                winreg.QueryValueEx(key, AUTOSTART_NAME)
                return True
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def _set_autostart(self, enabled):
        """Bật/tắt khởi động cùng Windows qua registry Run key (HKCU)."""
        try:
            import winreg
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY, 0,
                                     winreg.KEY_SET_VALUE)
            try:
                if enabled:
                    winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                      self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, AUTOSTART_NAME)
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
            return True
        except Exception as e:
            logger.warning("Không đặt được autostart: %s", e)
            return False

    # ── Settings Window ────────────────────────────────────

    def _open_settings(self, _event=None):
        """Mở cửa sổ chỉnh sửa danh sách mã hiển thị và các tùy chọn giao diện."""
        self._drag["dragging"] = False
        self.tooltip.hidetip()
        if self._settings_win is not None:
            try:
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
            except Exception:
                self._settings_win = None

        # ── Bảng màu (Catppuccin Mocha) ──
        CRUST, BASE, S0, S1, S2 = "#181825", "#1e1e2e", "#313244", "#45475a", "#585b70"
        TEXT, SUB, MUT = "#cdd6f4", "#a6adc8", "#6c7086"
        BLUE, BLUE_H, GREEN, RED = "#89b4fa", "#b4befe", "#a6e3a1", "#f38ba8"
        UI = "Segoe UI"

        win = tk.Toplevel(self.root)
        win.title("Cài đặt")
        win.geometry("640x700")
        win.minsize(560, 560)
        win.attributes("-topmost", True)
        win.configure(bg=BASE)
        self._settings_win = win
        # Chụp theme gốc để Hủy/đóng-không-lưu có thể hoàn tác live-preview
        self._settings_orig_theme = dict(self.theme)
        self._theme_committed = False
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings_win(win))

        LBL = {"bg": BASE, "fg": TEXT, "font": (UI, 9)}
        ENT = {"bg": S0, "fg": TEXT, "insertbackground": TEXT, "relief": "flat",
               "font": ("Consolas", 9), "highlightthickness": 1,
               "highlightbackground": S0, "highlightcolor": BLUE}

        def hover(w, normal, hot):
            w.bind("<Enter>", lambda e: w.configure(bg=hot))
            w.bind("<Leave>", lambda e: w.configure(bg=normal))

        def style_om(om, width=14):
            om.configure(bg=S0, fg=TEXT, highlightthickness=0, relief="flat",
                         activebackground=S2, font=(UI, 9), width=width,
                         anchor="w", indicatoron=True)
            om["menu"].configure(bg=S0, fg=TEXT, activebackground=BLUE,
                                 activeforeground=CRUST, relief="flat")

        def section(parent, text):
            tk.Label(parent, text=text, font=(UI, 9, "bold"), fg=SUB,
                     bg=BASE, anchor="w").pack(fill="x", padx=14, pady=(12, 2))
            tk.Frame(parent, height=1, bg=S0).pack(fill="x", padx=14, pady=(0, 4))

        def cfg_cols(fr):
            fr.columnconfigure(0, minsize=84, weight=0)   # Loại
            fr.columnconfigure(1, weight=3, uniform="d")  # Mã
            fr.columnconfigure(2, weight=2, uniform="d")  # Nhãn
            fr.columnconfigure(3, weight=2, uniform="d")  # ≥
            fr.columnconfigure(4, weight=2, uniform="d")  # ≤
            fr.columnconfigure(5, minsize=38, weight=0)   # xóa

        # ── Tiêu đề ──
        tk.Label(win, text="⚙  Cài đặt", font=(UI, 14, "bold"),
                 fg=TEXT, bg=BASE, anchor="w").pack(fill="x", padx=14, pady=(12, 0))

        # ── Mục: Danh sách mã ──
        section(win, "DANH SÁCH MÃ HIỂN THỊ")

        header = tk.Frame(win, bg=BASE)
        header.pack(fill="x", padx=14)
        cfg_cols(header)
        for col, text in enumerate(("Loại", "Mã", "Nhãn", "Cảnh báo ≥", "Cảnh báo ≤", "")):
            tk.Label(header, text=text, anchor="w", fg=MUT, bg=BASE,
                     font=(UI, 8)).grid(row=0, column=col, sticky="w", padx=2)

        table_wrap = tk.Frame(win, bg=BASE)
        table_wrap.pack(fill="both", expand=True, padx=14)
        canvas = tk.Canvas(table_wrap, bg=BASE, highlightthickness=0, height=170)
        vbar = tk.Scrollbar(table_wrap, orient="vertical", command=canvas.yview)
        rows_frame = tk.Frame(canvas, bg=BASE)
        rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win_item = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_item, width=e.width))
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        # Cuộn bằng con lăn chuột khi trỏ nằm trong bảng
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>", lambda ev: canvas.yview_scroll(int(-ev.delta / 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        rows = []

        def _as_str(v):
            return "" if v is None else str(v)

        def add_row(item=None):
            item = item or {}
            rf = tk.Frame(rows_frame, bg=BASE)
            rf.pack(fill="x", pady=1)
            cfg_cols(rf)
            type_var = tk.StringVar(value=item.get("type", "crypto"))
            sym_var = tk.StringVar(value=item.get("symbol", ""))
            lbl_var = tk.StringVar(value=item.get("label", ""))
            above_var = tk.StringVar(value=_as_str(item.get("alert_above")))
            below_var = tk.StringVar(value=_as_str(item.get("alert_below")))

            om = tk.OptionMenu(rf, type_var, *sorted(VALID_TYPES))
            style_om(om, width=6)
            om.grid(row=0, column=0, sticky="ew", padx=2, pady=1)
            tk.Entry(rf, textvariable=sym_var, **ENT).grid(row=0, column=1, sticky="ew", padx=2)
            tk.Entry(rf, textvariable=lbl_var, **ENT).grid(row=0, column=2, sticky="ew", padx=2)
            tk.Entry(rf, textvariable=above_var, **ENT).grid(row=0, column=3, sticky="ew", padx=2)
            tk.Entry(rf, textvariable=below_var, **ENT).grid(row=0, column=4, sticky="ew", padx=2)

            row = {"frame": rf, "type": type_var, "symbol": sym_var, "label": lbl_var,
                   "above": above_var, "below": below_var, "alive": True}

            def remove():
                row["alive"] = False
                rf.destroy()

            db = tk.Button(rf, text="🗑", command=remove, bg=S1, fg=RED,
                           relief="flat", cursor="hand2", width=2, bd=0)
            db.grid(row=0, column=5, padx=2)
            hover(db, S1, S2)
            rows.append(row)

        for it in self.symbols:
            add_row(it)

        # ── Thêm nhanh từ danh mục gợi ý ──
        picker = tk.Frame(win, bg=BASE)
        picker.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(picker, text="Thêm nhanh:", bg=BASE, fg=SUB, font=(UI, 9)).pack(side="left")
        pick_type = tk.StringVar(value="crypto")
        pick_sym = tk.StringVar(value=SYMBOL_PRESETS["crypto"][0])

        pt_om = tk.OptionMenu(picker, pick_type, *sorted(VALID_TYPES),
                              command=lambda v: refresh_syms())
        style_om(pt_om, width=7)
        pt_om.pack(side="left", padx=(6, 4))
        ps_om = tk.OptionMenu(picker, pick_sym, *SYMBOL_PRESETS["crypto"])
        style_om(ps_om, width=14)
        ps_om.pack(side="left", padx=4)

        def refresh_syms():
            syms = SYMBOL_PRESETS.get(pick_type.get(), [])
            m = ps_om["menu"]
            m.delete(0, "end")
            for s in syms:
                m.add_command(label=s, command=lambda v=s: pick_sym.set(v))
            pick_sym.set(syms[0] if syms else "")

        def add_from_picker():
            s = pick_sym.get().strip()
            if not s:
                return
            add_row({"type": pick_type.get(), "symbol": s, "label": default_label(s)})
            win.update_idletasks()
            canvas.yview_moveto(1.0)

        ab = tk.Button(picker, text="➕ Thêm", command=add_from_picker, bg=S1, fg=GREEN,
                       relief="flat", cursor="hand2", font=(UI, 9), bd=0, padx=10)
        ab.pack(side="left", padx=4)
        hover(ab, S1, S2)

        ab2 = tk.Button(picker, text="＋ Dòng trống", command=lambda: add_row(), bg=BASE,
                        fg=MUT, relief="flat", cursor="hand2", font=(UI, 9), bd=0)
        ab2.pack(side="left", padx=4)

        tk.Label(win, text="Bỏ trống ô Cảnh báo nếu không dùng · Nhãn trống sẽ lấy theo Mã · "
                           "Xóa mã tại đây (không còn ở menu chuột phải).",
                 font=(UI, 8), fg=MUT, bg=BASE, anchor="w", justify="left",
                 wraplength=600).pack(fill="x", padx=14, pady=(4, 0))

        # ── Mục: Giao diện ──
        section(win, "GIAO DIỆN")
        tf = tk.Frame(win, bg=BASE)
        tf.pack(fill="x", padx=14)

        bg_var = tk.StringVar(value=self.theme.get("bg", "#11111b"))
        fg_var = tk.StringVar(value=self.theme.get("fg", "white"))
        font_family_var = tk.StringVar(value=self.theme.get("font_family", "Consolas"))
        opacity_var = tk.DoubleVar(value=self.theme.get("opacity", 1.0))
        font_size_var = tk.IntVar(value=self.theme.get("font_size", 9))
        padx_var = tk.IntVar(value=self.theme.get("pad_x", 10))
        pady_var = tk.IntVar(value=self.theme.get("pad_y", 2))
        autostart_var = tk.BooleanVar(value=self._is_autostart_enabled())

        # ── Live-preview: áp dụng ngay khi kéo/chọn, chưa ghi config ──
        def preview_opacity(_=None):
            try:
                self.root.attributes("-alpha", float(opacity_var.get()))
            except Exception:
                pass

        def preview_size(_=None):
            self.theme["pad_x"] = int(padx_var.get())
            self.theme["pad_y"] = int(pady_var.get())
            try:
                self.container_frame.pack_configure(padx=self.theme["pad_x"],
                                                    pady=self.theme["pad_y"])
                self._fit_window_to_content()
            except Exception:
                pass

        def preview_appearance(*_):
            self.theme["bg"] = bg_var.get()
            self.theme["fg"] = fg_var.get()
            self.theme["font_family"] = font_family_var.get()
            try:
                self.theme["font_size"] = int(font_size_var.get())
            except Exception:
                pass
            try:
                self._apply_theme()
                self._rerender_now()
            except Exception:
                pass

        def make_swatch(parent, var):
            holder = tk.Frame(parent, bg=S0, highlightbackground=S2, highlightthickness=1)
            sw = tk.Frame(holder, bg=var.get() or "#000000", width=26, height=18)
            sw.pack(side="left", padx=2, pady=2)
            sw.pack_propagate(False)
            tk.Label(holder, textvariable=var, bg=S0, fg=TEXT, font=("Consolas", 9),
                     width=9, anchor="w").pack(side="left", padx=(4, 6))

            def pick(_e=None):
                c = colorchooser.askcolor(color=var.get(), parent=win)
                if c and c[1]:
                    var.set(c[1])
                    sw.configure(bg=c[1])
                    preview_appearance()

            for wdg in holder.winfo_children() + [holder]:
                wdg.bind("<Button-1>", pick)
                try:
                    wdg.configure(cursor="hand2")
                except Exception:
                    pass
            return holder, sw

        tk.Label(tf, text="Preset:", **LBL).grid(row=0, column=0, sticky="w", pady=4)
        preset_var = tk.StringVar(value="— chọn —")

        def apply_preset(name):
            p = THEME_PRESETS.get(name)
            if not p:
                return
            bg_var.set(p["bg"]); bg_sw.configure(bg=p["bg"])
            fg_var.set(p["fg"]); fg_sw.configure(bg=p["fg"])
            self.theme["separator_color"] = p.get("separator_color",
                                                  self.theme.get("separator_color"))
            preview_appearance()

        pom = tk.OptionMenu(tf, preset_var, *THEME_PRESETS.keys(), command=apply_preset)
        style_om(pom)
        pom.grid(row=0, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Màu nền:", **LBL).grid(row=1, column=0, sticky="w", pady=4)
        bg_holder, bg_sw = make_swatch(tf, bg_var)
        bg_holder.grid(row=1, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Màu chữ:", **LBL).grid(row=2, column=0, sticky="w", pady=4)
        fg_holder, fg_sw = make_swatch(tf, fg_var)
        fg_holder.grid(row=2, column=1, sticky="w", padx=(8, 0))

        families = list(dict.fromkeys([font_family_var.get()] + FONT_FAMILIES))
        tk.Label(tf, text="Font:", **LBL).grid(row=3, column=0, sticky="w", pady=4)
        fom = tk.OptionMenu(tf, font_family_var, *families, command=lambda v: preview_appearance())
        style_om(fom)
        fom.grid(row=3, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Cỡ chữ:", **LBL).grid(row=4, column=0, sticky="w", pady=4)
        tk.Spinbox(tf, from_=7, to=16, textvariable=font_size_var, width=5, relief="flat",
                   bg=S0, fg=TEXT, buttonbackground=S1, insertbackground=TEXT,
                   command=preview_appearance).grid(row=4, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Độ mờ:", **LBL).grid(row=5, column=0, sticky="w", pady=4)
        tk.Scale(tf, from_=0.3, to=1.0, resolution=0.05, orient="horizontal",
                 variable=opacity_var, bg=BASE, fg=TEXT, highlightthickness=0,
                 troughcolor=S0, activebackground=BLUE, length=210,
                 font=(UI, 8), command=preview_opacity).grid(row=5, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Đệm ngang:", **LBL).grid(row=6, column=0, sticky="w", pady=4)
        tk.Scale(tf, from_=0, to=40, resolution=1, orient="horizontal",
                 variable=padx_var, bg=BASE, fg=TEXT, highlightthickness=0,
                 troughcolor=S0, activebackground=BLUE, length=210,
                 font=(UI, 8), command=preview_size).grid(row=6, column=1, sticky="w", padx=(8, 0))

        tk.Label(tf, text="Đệm dọc (độ dày):", **LBL).grid(row=7, column=0, sticky="w", pady=4)
        tk.Scale(tf, from_=0, to=24, resolution=1, orient="horizontal",
                 variable=pady_var, bg=BASE, fg=TEXT, highlightthickness=0,
                 troughcolor=S0, activebackground=BLUE, length=210,
                 font=(UI, 8), command=preview_size).grid(row=7, column=1, sticky="w", padx=(8, 0))

        tk.Checkbutton(tf, text="Khởi động cùng Windows", variable=autostart_var,
                       bg=BASE, fg=TEXT, selectcolor=S0, activebackground=BASE,
                       activeforeground=TEXT, font=(UI, 9)).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def save():
            try:
                raw_rows = [{
                    "type": r["type"].get(), "symbol": r["symbol"].get(),
                    "label": r["label"].get(), "alert_above": r["above"].get(),
                    "alert_below": r["below"].get(),
                } for r in rows if r["alive"]]
                new_symbols = parse_symbol_rows(raw_rows)
                self._validate_symbols(new_symbols)
                self.symbols = new_symbols

                self.theme["opacity"] = opacity_var.get()
                self.theme["font_size"] = int(font_size_var.get())
                self.theme["font_family"] = font_family_var.get()
                self.theme["bg"] = bg_var.get()
                self.theme["fg"] = fg_var.get()
                self.theme["pad_x"] = int(padx_var.get())
                self.theme["pad_y"] = int(pady_var.get())
                self._apply_theme()
                self._set_autostart(bool(autostart_var.get()))

                self._save_config(symbols=new_symbols, theme=self.theme)
                self._theme_committed = True  # đã lưu -> đóng không hoàn tác
                self._close_settings_win(win)
                self._force_reload()
            except Exception as e:
                messagebox.showerror("Lỗi", str(e), parent=win)

        # ── Thanh nút dưới cùng ──
        btnbar = tk.Frame(win, bg=BASE)
        btnbar.pack(fill="x", padx=14, pady=12)
        cancel_b = tk.Button(btnbar, text="Hủy", command=lambda: self._close_settings_win(win),
                             bg=S1, fg=TEXT, relief="flat", cursor="hand2",
                             font=(UI, 9), padx=16, pady=6, bd=0)
        cancel_b.pack(side="right", padx=(8, 0))
        hover(cancel_b, S1, S2)
        save_b = tk.Button(btnbar, text="💾  Lưu cấu hình", command=save,
                           bg=BLUE, fg=CRUST, relief="flat", cursor="hand2",
                           font=(UI, 10, "bold"), padx=20, pady=6, bd=0)
        save_b.pack(side="right")
        hover(save_b, BLUE, BLUE_H)

    def _close_settings_win(self, win):
        """Đóng cửa sổ settings; nếu chưa Lưu thì hoàn tác các thay đổi live-preview."""
        orig = getattr(self, "_settings_orig_theme", None)
        if orig is not None and not getattr(self, "_theme_committed", False):
            self.theme = orig
            try:
                self._apply_theme()
                self._rerender_now()
            except Exception:
                pass
        self._settings_orig_theme = None
        self._theme_committed = False
        self._settings_win = None
        win.destroy()

    @staticmethod
    def _validate_symbols(symbols):
        """Kiểm tra tính hợp lệ của cấu trúc JSON symbols."""
        if not isinstance(symbols, list):
            raise ValueError("Config phải là một danh sách dạng [ {...}, {...} ]")
        seen = set()
        for i, item in enumerate(symbols, 1):
            if not isinstance(item, dict):
                raise ValueError(f"Phần tử #{i} phải là object JSON, nhận được {type(item).__name__}")
            missing = REQUIRED_KEYS - item.keys()
            if missing:
                raise ValueError(f"Phần tử #{i} thiếu key: {missing}")
            if item["type"] not in VALID_TYPES:
                raise ValueError(
                    f"Phần tử #{i}: type '{item['type']}' không hợp lệ. "
                    f"Chọn: {', '.join(sorted(VALID_TYPES))}"
                )
            dedup_key = (item["type"], item["symbol"])
            if dedup_key in seen:
                raise ValueError(
                    f"Phần tử #{i}: mã '{item['symbol']}' (type '{item['type']}') bị trùng lặp"
                )
            seen.add(dedup_key)

    # ── Entry Point ────────────────────────────────────────

    def run(self):
        """Khởi chạy vòng lặp chính của ứng dụng."""
        self.root.mainloop()


if __name__ == "__main__":
    app = TickerApp()
    app.run()
