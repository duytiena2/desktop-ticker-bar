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
from concurrent.futures import ThreadPoolExecutor

# ── Tắt cảnh báo SSL (cho API vnappmob) ───────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Hằng số ────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

API_TIMEOUT = 5           # Thời gian chờ API tối đa (giây)
UPDATE_INTERVAL = 10      # Chu kỳ cập nhật dữ liệu (giây)
KEEP_ON_TOP_INTERVAL = 2000  # Chu kỳ giữ cửa sổ ở trên cùng (ms)
TRANSPARENT_COLOR = "#000001"

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


# ═══════════════════════════════════════════════════════════
#  Tooltip — Cửa sổ thông tin chi tiết khi Hover
# ═══════════════════════════════════════════════════════════

class Tooltip:
    """Quản lý hiển thị cửa sổ Tooltip động theo con trỏ chuột."""

    def __init__(self, widget):
        self.widget = widget
        self.tipwindow = None
        self.x = 0
        self.y = 0

    def showtip(self, text, x_root, y_root):
        """Hiển thị Tooltip tại vị trí chuột."""
        if self.tipwindow or not text:
            return
        
        # Nếu chuột nằm ở nửa dưới màn hình, hiển thị Tooltip phía trên chuột để tránh bị che
        screen_h = self.widget.winfo_screenheight()
        if y_root > screen_h / 2:
            x = x_root + 15
            y = y_root - 95
        else:
            x = x_root + 15
            y = y_root + 15

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
            screen_h = self.widget.winfo_screenheight()
            if y_root > screen_h / 2:
                x = x_root + 15
                y = y_root - 95
            else:
                x = x_root + 15
                y = y_root + 15
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
        except Exception:
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
        except Exception:
            pass
        return None, None, None

    # ── Vàng (vnappmob) ────────────────────────────────────

    def _refresh_gold_token(self):
        """Làm mới Bearer token cho API vàng vnappmob."""
        try:
            r = self.session.get(
                "https://api.vnappmob.com/api/request_api_key?scope=gold",
                timeout=API_TIMEOUT, verify=False,
            )
            if r.status_code == 200:
                data = r.json()
                if "results" in data:
                    self._gold_token = data["results"]
                    return True
        except Exception:
            pass
        return False

    def get_gold_price(self, symbol):
        """Lấy giá vàng SJC từ vnappmob."""
        sym = symbol.lower()

        if not self._gold_token and not self._refresh_gold_token():
            return None, None, None

        url = f"https://api.vnappmob.com/api/v2/gold/{sym}"
        headers = {"Authorization": f"Bearer {self._gold_token}"}

        try:
            r = self.session.get(url, headers=headers, timeout=API_TIMEOUT, verify=False)

            # Tự động refresh token nếu bị hết hạn (401)
            if r.status_code == 401:
                if self._refresh_gold_token():
                    headers["Authorization"] = f"Bearer {self._gold_token}"
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
        except Exception:
            pass
        return None, None, None

    # ── Chứng khoán (Yahoo Finance) ───────────────────────

    def get_stock_price(self, symbol):
        """Lấy giá chứng khoán từ Yahoo Finance."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
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
        except Exception:
            pass
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
        self._reload_event = threading.Event()
        self.symbols = DEFAULT_SYMBOLS[:]
        
        self.tooltip = Tooltip(self.root)
        self.symbol_widgets = {}
        self._settings_win = None

        self._load_and_apply_config()
        self._setup_ui()
        self._setup_context_menu()
        self._start_update_thread()

    # ── Config ─────────────────────────────────────────────

    @staticmethod
    def _load_config():
        """Đọc cấu hình từ file JSON."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return None

    def _save_config(self, x=None, y=None, symbols=None):
        """Ghi cấu hình ra file JSON."""
        if x is None:
            x = self.root.winfo_x()
        if y is None:
            y = self.root.winfo_y()
        if symbols is None:
            symbols = self.symbols
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": x, "y": y, "symbols": symbols}, f, ensure_ascii=False, indent=4)
        except Exception:
            pass

    def _load_and_apply_config(self):
        """Đọc config và áp dụng vị trí + danh sách symbols."""
        config = self._load_config()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self._width = screen_w - 200
        self._height = 25

        if config and "x" in config and "y" in config:
            self._x = config["x"]
            self._y = config["y"]
            self.symbols = config.get("symbols", DEFAULT_SYMBOLS)
        else:
            self._x = 180
            self._y = screen_h - self._height - 10
            self._save_config(self._x, self._y, DEFAULT_SYMBOLS)

    # ── UI Setup ───────────────────────────────────────────

    def _setup_ui(self):
        """Khởi tạo giao diện chính."""
        root = self.root
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT_COLOR)
        root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        root.geometry(f"{self._width}x{self._height}+{self._x}+{self._y}")

        # Trạng thái kéo thả
        self._drag = {"x": 0, "y": 0, "win_x": 0, "win_y": 0}

        # Frame chứa tất cả các nhãn giá (cho phép hiển thị nhiều màu và hover)
        self.container_frame = tk.Frame(root, bg=TRANSPARENT_COLOR)
        self.container_frame.pack(expand=True, fill="both", padx=10)

        # Gán sự kiện chuột kéo thả cho cả root và container frame
        for widget in (root, self.container_frame):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)
            widget.bind("<ButtonRelease-1>", self._on_drag_stop)
            widget.bind("<Double-Button-1>", self._open_settings)
            widget.bind("<Button-3>", self._show_context_menu)

        self._keep_on_top()

    def _setup_context_menu(self):
        """Khởi tạo menu chuột phải."""
        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="⚙ Cài đặt", command=self._open_settings)
        self._context_menu.add_command(label="🔄 Tải lại ngay", command=self._force_reload)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="✕ Thoát", command=self._on_quit)

    # ── Drag & Drop ────────────────────────────────────────

    def _on_drag_start(self, event):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self._drag["x"] = event.x_root
        self._drag["y"] = event.y_root
        self._drag["win_x"] = self.root.winfo_x()
        self._drag["win_y"] = self.root.winfo_y()
        self.tooltip.hidetip()

    def _on_drag_motion(self, event):
        dx = event.x_root - self._drag["x"]
        dy = event.y_root - self._drag["y"]
        nx = self._drag["win_x"] + dx
        ny = self._drag["win_y"] + dy
        self.root.geometry(f"+{nx}+{ny}")

    def _on_drag_stop(self, _event):
        # Chỉ ghi config khi vị trí thực sự thay đổi
        if (self.root.winfo_x() != self._drag["win_x"]
                or self.root.winfo_y() != self._drag["win_y"]):
            self._save_config()

    # ── Context Menu ───────────────────────────────────────

    def _show_context_menu(self, event):
        self.tooltip.hidetip()
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()

    # ── Quit ───────────────────────────────────────────────

    def _on_quit(self):
        """Thoát ứng dụng, đóng HTTP session đúng cách."""
        try:
            self.provider.session.close()
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
        self.tooltip.hidetip()

    # ── Data Fetching & Rendering ──────────────────────────

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
            return None

        # ── Format output ──
        suffix = "*" if is_fallback else ""
        if symbol_type == "crypto":
            price_text = f"{label_str}:${price:,.0f}{suffix}"
        else:
            price_text = f"{label_str}:{price:,.0f}đ{suffix}"

        # Định dạng text & màu sắc cho % thay đổi
        change_text = ""
        change_color = "white"
        if change_pct is not None:
            arrow = "▲" if change_pct >= 0 else "▼"
            sign = "+" if change_pct >= 0 else ""
            change_text = f"{arrow}{sign}{change_pct:.1f}%"
            change_color = "#a6e3a1" if change_pct >= 0 else "#f38ba8"  # Xanh / Đỏ Catppuccin

        return {
            "key": cache_key,
            "price_text": price_text,
            "change_text": change_text,
            "change_color": change_color
        }

    def _render_symbols(self, data_dict):
        """Dựng giao diện động cho các mã từ dữ liệu mới (không bị nhấp nháy)."""
        current_keys = {f"{item['type']}_{item['symbol']}" for item in self.symbols}
        
        # Xóa các widget của mã không còn tồn tại trong config
        for key in list(self.symbol_widgets.keys()):
            if key not in current_keys:
                self.symbol_widgets[key]["frame"].destroy()
                del self.symbol_widgets[key]

        # Cập nhật hoặc tạo mới các widget con
        for i, item in enumerate(self.symbols):
            key = f"{item['type']}_{item['symbol']}"
            data = data_dict.get(key)
            if not data:
                continue

            # Nếu chưa có widget, tiến hành tạo mới
            if key not in self.symbol_widgets:
                frame = tk.Frame(self.container_frame, bg=TRANSPARENT_COLOR)
                frame.pack(side="left", padx=5)

                # Bind các sự kiện drag & drop và click chuột lên frame con
                for event_name in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<Double-Button-1>", "<Button-3>"):
                    frame.bind(event_name, getattr(self, event_name.replace("<", "_on_").replace(">", "").replace("-", "_").lower(), None))

                lbl_price = tk.Label(
                    frame, fg="white", bg=TRANSPARENT_COLOR,
                    font=("Consolas", 9), anchor="w"
                )
                lbl_price.pack(side="left")
                for event_name in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<Double-Button-1>", "<Button-3>"):
                    lbl_price.bind(event_name, getattr(self, event_name.replace("<", "_on_").replace(">", "").replace("-", "_").lower(), None))

                lbl_change = tk.Label(
                    frame, bg=TRANSPARENT_COLOR,
                    font=("Consolas", 9), anchor="w"
                )
                lbl_change.pack(side="left")
                for event_name in ("<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<Double-Button-1>", "<Button-3>"):
                    lbl_change.bind(event_name, getattr(self, event_name.replace("<", "_on_").replace(">", "").replace("-", "_").lower(), None))

                lbl_sep = tk.Label(
                    frame, text=" |", fg="#585b70", bg=TRANSPARENT_COLOR,
                    font=("Consolas", 9)
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

    def _update_data(self):
        """Vòng lặp cập nhật dữ liệu song song bằng ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            while True:
                futures = [executor.submit(self._fetch_single, item) for item in self.symbols]

                data_dict = {}
                for fut in futures:
                    try:
                        res = fut.result(timeout=7)
                        if res:
                            data_dict[res["key"]] = res
                    except Exception:
                        pass

                # Đẩy cập nhật UI an toàn lên Main Thread
                self.root.after(0, lambda d=data_dict: self._render_symbols(d))

                self._reload_event.wait(UPDATE_INTERVAL)
                self._reload_event.clear()

    def _force_reload(self):
        """Kích hoạt tải lại dữ liệu ngay lập tức."""
        self._reload_event.set()

    def _start_update_thread(self):
        threading.Thread(target=self._update_data, daemon=True).start()

    # ── Settings Window ────────────────────────────────────

    def _open_settings(self, _event=None):
        """Mở cửa sổ chỉnh sửa danh sách mã hiển thị."""
        if self._settings_win is not None:
            try:
                self._settings_win.lock()
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
            except Exception:
                self._settings_win = None

        current_json = json.dumps(self.symbols, indent=4, ensure_ascii=False)

        win = tk.Toplevel(self.root)
        win.title("Cài đặt mã hiển thị")
        win.geometry("500x420")
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
            win, height=16,
            bg="#313244", fg="#a6e3a1", insertbackground="#cdd6f4",
            font=("Consolas", 10), relief="flat", padx=8, pady=8,
            selectbackground="#585b70", selectforeground="#cdd6f4",
        )
        text_area.insert(tk.END, current_json)
        text_area.pack(padx=10, fill=tk.BOTH, expand=True)

        # Gợi ý format
        tk.Label(
            win,
            text='type: crypto | p2p | gold | stock',
            font=("Consolas", 8), fg="#6c7086", bg="#1e1e2e",
        ).pack(pady=(2, 0))

        def save():
            raw = text_area.get("1.0", tk.END).strip()
            try:
                new_symbols = json.loads(raw)
                self._validate_symbols(new_symbols)
                self.symbols = new_symbols
                self._save_config(symbols=new_symbols)
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

    # ── Entry Point ────────────────────────────────────────

    def run(self):
        """Khởi chạy vòng lặp chính của ứng dụng."""
        self.root.mainloop()


if __name__ == "__main__":
    app = TickerApp()
    app.run()
