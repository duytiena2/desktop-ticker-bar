"""
Desktop Ticker Bar — Thanh hiển thị giá tài sản đa loại trên Desktop Windows.
Hỗ trợ: Crypto (Binance), P2P (Binance), Vàng SJC (vnappmob), Chứng khoán (Yahoo Finance).
"""

import tkinter as tk
from tkinter import messagebox
import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.util.retry import Retry
import threading
import json
import os
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
TRANSPARENT_COLOR = "#000001"
CLICK_MOVE_THRESHOLD = 4     # Ngưỡng (px) để phân biệt "click" và "kéo thả"
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

DEFAULT_THEME = {
    "font_family": "Consolas",
    "font_size": 9,
    "fg": "white",
    "positive_color": "#a6e3a1",
    "negative_color": "#f38ba8",
    "separator_color": "#585b70",
    "alert_color": "#f9e2af",
    "opacity": 1.0,
}

# Đường dẫn mở khi click (không kéo) vào một mã trên thanh giá
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

def format_price_text(label, price, symbol_type, is_fallback=False, no_data=False):
    """Định dạng chuỗi hiển thị giá cho một mã."""
    if no_data:
        return f"{label}:--"
    suffix = "*" if is_fallback else ""
    if symbol_type == "crypto":
        return f"{label}:${price:,.0f}{suffix}"
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


# ═══════════════════════════════════════════════════════════
#  Tooltip — Cửa sổ thông tin chi tiết khi Hover
# ═══════════════════════════════════════════════════════════

class Tooltip:
    """Quản lý hiển thị cửa sổ Tooltip động theo con trỏ chuột."""

    # Khoảng lệch (px) của tooltip so với con trỏ chuột
    OFFSET_X = 15
    OFFSET_Y = 15
    OFFSET_Y_ABOVE = 95  # Lệch lên khi chuột ở nửa dưới màn hình (tránh bị cắt)

    def __init__(self, widget):
        self.widget = widget
        self.tipwindow = None

    def _calc_pos(self, x_root, y_root):
        """Tính vị trí tooltip; hiển thị phía trên chuột nếu ở nửa dưới màn hình."""
        screen_h = self.widget.winfo_screenheight()
        x = x_root + self.OFFSET_X
        if y_root > screen_h / 2:
            y = y_root - self.OFFSET_Y_ABOVE
        else:
            y = y_root + self.OFFSET_Y
        return x, y

    def showtip(self, text, x_root, y_root):
        """Hiển thị Tooltip tại vị trí chuột."""
        if self.tipwindow or not text:
            return

        x, y = self._calc_pos(x_root, y_root)

        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)

        # Style Tooltip: Nền tối Catppuccin Mocha, viền mỏng
        label = tk.Label(
            tw, text=text, justify="left",
            background="#1e1e2e", foreground="#cdd6f4",
            relief="solid", borderwidth=1,
            font=("Consolas", 9),
            padx=8, pady=6
        )
        label.pack(ipadx=1)

    def movetip(self, x_root, y_root):
        """Di chuyển Tooltip theo chuột."""
        if self.tipwindow:
            x, y = self._calc_pos(x_root, y_root)
            self.tipwindow.wm_geometry(f"+{x}+{y}")

    def hidetip(self):
        """Ẩn Tooltip."""
        tw = self.tipwindow
        self.tipwindow = None
        if tw:
            tw.destroy()


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
                    {"x": x, "y": y, "symbols": symbols, "theme": theme},
                    f, ensure_ascii=False, indent=4
                )
        except Exception as e:
            logger.warning("Không ghi được config.json: %s", e)

    def _load_and_apply_config(self):
        """Đọc config và áp dụng vị trí + danh sách symbols + theme."""
        config = self._load_config()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self._width = screen_w - 200
        self._height = 25

        if config and "x" in config and "y" in config:
            self._x = config["x"]
            self._y = config["y"]
            self.symbols = config.get("symbols", DEFAULT_SYMBOLS)
            self.theme = {**DEFAULT_THEME, **config.get("theme", {})}
        else:
            self._x = 180
            self._y = screen_h - self._height - 10
            self._save_config(self._x, self._y, DEFAULT_SYMBOLS, DEFAULT_THEME)

    # ── UI Setup ───────────────────────────────────────────

    def _setup_ui(self):
        """Khởi tạo giao diện chính."""
        root = self.root
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT_COLOR)
        root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        root.attributes("-alpha", self.theme.get("opacity", 1.0))
        root.geometry(f"{self._width}x{self._height}+{self._x}+{self._y}")

        # Trạng thái kéo thả
        self._drag = {"x": 0, "y": 0, "win_x": 0, "win_y": 0, "key": None, "moved": False}

        # Frame chứa tất cả các nhãn giá (cho phép hiển thị nhiều màu và hover)
        self.container_frame = tk.Frame(root, bg=TRANSPARENT_COLOR)
        self.container_frame.pack(expand=True, fill="both", padx=10)

        # Gán sự kiện chuột kéo thả cho cả root và container frame
        for widget in (root, self.container_frame):
            self._bind_widget_events(widget)

        self._keep_on_top()

    def _setup_context_menu(self):
        """Khởi tạo menu chuột phải (nền thanh giá)."""
        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="⚙ Cài đặt", command=self._open_settings)
        self._context_menu.add_command(label="🔄 Tải lại ngay", command=self._force_reload)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="✕ Thoát", command=self._on_quit)

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

    # ── Bind sự kiện (thay cho việc lặp code + suy luận tên hàm bằng string) ──

    def _bind_widget_events(self, widget, key=None):
        """Gán toàn bộ sự kiện chuột cần thiết cho 1 widget.

        ``key`` là mã ``type_symbol`` nếu widget thuộc về 1 mã cụ thể (item),
        None nếu là vùng nền chung của thanh giá.
        """
        widget.bind("<ButtonPress-1>", lambda e: self._on_drag_start(e, key))
        widget.bind("<B1-Motion>", self._on_drag_motion)
        widget.bind("<ButtonRelease-1>", lambda e: self._on_drag_stop(e, key))
        widget.bind("<Double-Button-1>", self._open_settings)
        if key is not None:
            widget.bind("<Button-3>", lambda e: self._show_item_context_menu(e, key))
        else:
            widget.bind("<Button-3>", self._show_context_menu)

    # ── Drag & Drop / Click-to-open ────────────────────────

    def _on_drag_start(self, event, key=None):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._drag["x"] = event.x_root
        self._drag["y"] = event.y_root
        self._drag["win_x"] = self.root.winfo_x()
        self._drag["win_y"] = self.root.winfo_y()
        self._drag["key"] = key
        self._drag["moved"] = False
        self.tooltip.hidetip()

    def _on_drag_motion(self, event):
        dx = event.x_root - self._drag["x"]
        dy = event.y_root - self._drag["y"]
        if abs(dx) > CLICK_MOVE_THRESHOLD or abs(dy) > CLICK_MOVE_THRESHOLD:
            self._drag["moved"] = True
        nx = self._drag["win_x"] + dx
        ny = self._drag["win_y"] + dy
        self.root.geometry(f"+{nx}+{ny}")

    def _on_drag_stop(self, event, key=None):
        # Chỉ ghi config khi vị trí thực sự thay đổi
        if (self.root.winfo_x() != self._drag["win_x"]
                or self.root.winfo_y() != self._drag["win_y"]):
            self._save_config()

        # Nếu chuột gần như không di chuyển, coi đây là click -> mở link nguồn giá
        if key is not None and not self._drag.get("moved", False):
            self._open_source_link(key)

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

    def _show_item_context_menu(self, event, key):
        """Menu chuột phải riêng cho từng mã: di chuyển trái/phải, xóa."""
        self.tooltip.hidetip()
        idx = next((i for i, it in enumerate(self.symbols)
                    if f"{it['type']}_{it['symbol']}" == key), None)
        if idx is None:
            self._show_context_menu(event)
            return

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="⬅ Di chuyển trái", command=lambda: self._move_symbol(key, -1),
                          state="normal" if idx > 0 else "disabled")
        menu.add_command(label="➡ Di chuyển phải", command=lambda: self._move_symbol(key, 1),
                          state="normal" if idx < len(self.symbols) - 1 else "disabled")
        menu.add_command(label="🗑 Xóa mã", command=lambda: self._remove_symbol(key))
        menu.add_separator()
        menu.add_command(label="⚙ Cài đặt", command=self._open_settings)
        menu.add_command(label="🔄 Tải lại ngay", command=self._force_reload)
        menu.add_command(label="✕ Thoát", command=self._on_quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
            return

        symbol = key.split("_", 1)[1]
        t = key.split("_", 1)[0]
        info = data["extra_info"]
        tip_text = ""

        if t == "crypto":
            tip_text = (
                f"⭐ Binance Crypto: {symbol}\n"
                f"📈 Cao nhất 24h : ${info['high']:,.2f}\n"
                f"📉 Thấp nhất 24h: ${info['low']:,.2f}\n"
                f"📊 Khối lượng    : {info['volume']:,.2f} {symbol}\n"
                f"🖱️ Click để mở Binance"
            )
        elif t == "stock":
            high_str = f"{info['high']:,.0f}đ" if info.get("high") else "N/A"
            low_str = f"{info['low']:,.0f}đ" if info.get("low") else "N/A"
            vol_str = f"{info['volume']:,.0f}" if info.get("volume") else "N/A"
            tip_text = (
                f"📈 Chứng khoán: {symbol}\n"
                f"🔺 Cao nhất ngày : {high_str}\n"
                f"🔻 Thấp nhất ngày: {low_str}\n"
                f"📊 Khối lượng    : {vol_str}\n"
                f"🖱️ Click để mở Yahoo Finance"
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
                f"⚡ Thanh toán: Chuyển khoản ngân hàng\n"
                f"🖱️ Click để mở Binance P2P"
            )

        self.tooltip.showtip(tip_text, event.x_root, event.y_root)

    def _move_tooltip(self, event):
        self.tooltip.movetip(event.x_root, event.y_root)

    def _hide_tooltip(self, _event):
        self.tooltip.hidetip()

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
            frame.configure(bg=TRANSPARENT_COLOR)
            for child in frame.winfo_children():
                child.configure(bg=TRANSPARENT_COLOR)
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
                frame = tk.Frame(self.container_frame, bg=TRANSPARENT_COLOR)
                self._bind_widget_events(frame, key=key)

                lbl_price = tk.Label(
                    frame, fg=self.theme["fg"], bg=TRANSPARENT_COLOR,
                    font=font, anchor="w"
                )
                lbl_price.pack(side="left")
                self._bind_widget_events(lbl_price, key=key)

                lbl_change = tk.Label(
                    frame, bg=TRANSPARENT_COLOR,
                    font=font, anchor="w"
                )
                lbl_change.pack(side="left")
                self._bind_widget_events(lbl_change, key=key)

                lbl_sep = tk.Label(
                    frame, text="|", fg=self.theme["separator_color"], bg=TRANSPARENT_COLOR,
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

    # ── Settings Window ────────────────────────────────────

    def _open_settings(self, _event=None):
        """Mở cửa sổ chỉnh sửa danh sách mã hiển thị và các tùy chọn giao diện."""
        if self._settings_win is not None:
            try:
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
            except Exception:
                self._settings_win = None

        current_json = json.dumps(self.symbols, indent=4, ensure_ascii=False)

        win = tk.Toplevel(self.root)
        win.title("Cài đặt mã hiển thị")
        win.geometry("500x520")
        win.attributes("-topmost", True)
        win.configure(bg="#1e1e2e")
        self._settings_win = win
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_settings_win(win))

        # Tiêu đề
        tk.Label(
            win,
            text="Chỉnh sửa danh sách hiển thị (định dạng JSON):",
            font=("Consolas", 10), fg="#cdd6f4", bg="#1e1e2e",
        ).pack(pady=(10, 5))

        # Vùng soạn thảo JSON — Dark theme
        text_area = tk.Text(
            win, height=14,
            bg="#313244", fg="#a6e3a1", insertbackground="#cdd6f4",
            font=("Consolas", 10), relief="flat", padx=8, pady=8,
            selectbackground="#585b70", selectforeground="#cdd6f4",
        )
        text_area.insert(tk.END, current_json)
        text_area.pack(padx=10, fill=tk.BOTH, expand=True)

        # Gợi ý format
        tk.Label(
            win,
            text=('type: crypto | p2p | gold | stock  '
                  '(tùy chọn: "alert_above", "alert_below")'),
            font=("Consolas", 8), fg="#6c7086", bg="#1e1e2e",
        ).pack(pady=(2, 8))

        # ── Tùy chọn giao diện (Theme) ──
        theme_frame = tk.Frame(win, bg="#1e1e2e")
        theme_frame.pack(fill="x", padx=10, pady=(0, 5))

        tk.Label(theme_frame, text="Độ trong suốt:", font=("Consolas", 9),
                 fg="#cdd6f4", bg="#1e1e2e").grid(row=0, column=0, sticky="w")
        opacity_var = tk.DoubleVar(value=self.theme.get("opacity", 1.0))
        opacity_scale = tk.Scale(
            theme_frame, from_=0.3, to=1.0, resolution=0.05, orient="horizontal",
            variable=opacity_var, bg="#1e1e2e", fg="#cdd6f4", highlightthickness=0,
            troughcolor="#313244", length=200,
        )
        opacity_scale.grid(row=0, column=1, sticky="w", padx=(8, 0))

        tk.Label(theme_frame, text="Cỡ chữ:", font=("Consolas", 9),
                 fg="#cdd6f4", bg="#1e1e2e").grid(row=1, column=0, sticky="w", pady=(6, 0))
        font_size_var = tk.IntVar(value=self.theme.get("font_size", 9))
        font_size_spin = tk.Spinbox(
            theme_frame, from_=7, to=16, textvariable=font_size_var, width=5,
            bg="#313244", fg="#cdd6f4",
        )
        font_size_spin.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        def save():
            raw = text_area.get("1.0", tk.END).strip()
            try:
                new_symbols = json.loads(raw)
                self._validate_symbols(new_symbols)
                self.symbols = new_symbols

                self.theme["opacity"] = opacity_var.get()
                self.theme["font_size"] = font_size_var.get()
                self.root.attributes("-alpha", self.theme["opacity"])

                # Áp dụng cỡ chữ mới: buộc tạo lại toàn bộ widget mã
                for key, widgets in list(self.symbol_widgets.items()):
                    widgets["frame"].destroy()
                self.symbol_widgets.clear()

                self._save_config(symbols=new_symbols, theme=self.theme)
                self._close_settings_win(win)
                self._force_reload()
            except Exception as e:
                messagebox.showerror("Lỗi", str(e), parent=win)

        # Nút lưu
        tk.Button(
            win, text="💾 Lưu Cấu Hình", command=save,
            bg="#89b4fa", fg="#1e1e2e", activebackground="#74c7ec",
            font=("Arial", 10, "bold"), relief="flat", padx=20, pady=5,
            cursor="hand2",
        ).pack(pady=10)

    def _close_settings_win(self, win):
        """Đóng cửa sổ settings và reset trạng thái."""
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
