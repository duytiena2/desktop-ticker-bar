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
KEEP_ON_TOP_INTERVAL = 5000  # Chu kỳ giữ cửa sổ ở trên cùng (ms)
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
#  PriceProvider — Quản lý gọi API lấy giá
# ═══════════════════════════════════════════════════════════

class PriceProvider:
    """Lớp chuyên trách gọi API lấy giá từ nhiều nguồn khác nhau.

    Sử dụng ``requests.Session`` để tái sử dụng TCP connection (HTTP Keep-Alive),
    giảm latency ~30-50% so với tạo kết nối mới mỗi lần.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        # Tự động retry 1 lần khi gặp lỗi server tạm thời (500/502/503/504)
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
        """Lấy giá crypto + % thay đổi 24h từ Binance ``ticker/24hr``."""
        sym = f"{symbol}USDT" if "USDT" not in symbol else symbol
        try:
            r = self.session.get(
                f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}",
                timeout=API_TIMEOUT,
            )
            r.raise_for_status()
            d = r.json()
            return float(d["lastPrice"]), float(d["priceChangePercent"])
        except Exception:
            return None, None

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
                return float(res["data"][0]["adv"]["price"]), None
        except Exception:
            pass
        return None, None

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
            return None, None

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
                    if price_str:
                        return float(price_str), None
        except Exception:
            pass
        return None, None

    # ── Chứng khoán (Yahoo Finance) ───────────────────────

    def get_stock_price(self, symbol):
        """Lấy giá chứng khoán + % thay đổi từ Yahoo Finance."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            r = self.session.get(url, timeout=API_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            result = data["chart"]["result"]
            if result:
                meta = result[0]["meta"]
                price = float(meta["regularMarketPrice"])
                prev = float(meta.get("chartPreviousClose", meta.get("previousClose", 0)))
                change_pct = ((price - prev) / prev * 100) if prev else None
                return price, change_pct
        except Exception:
            pass
        return None, None

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
    """Ứng dụng Desktop Ticker Bar.

    Hiển thị thanh giá trong suốt trên Desktop, hỗ trợ kéo thả,
    chuột phải mở menu, double-click mở cài đặt.
    """

    def __init__(self):
        self.root = tk.Tk()
        self.provider = PriceProvider()
        self.last_prices = {}
        self._prices_lock = threading.Lock()
        self._reload_event = threading.Event()
        self.symbols = DEFAULT_SYMBOLS[:]
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

        # Label chính hiển thị giá
        self.label = tk.Label(
            root,
            text="Đang tải dữ liệu...",
            fg="white",
            bg=TRANSPARENT_COLOR,
            font=("Consolas", 9),
            justify="left",
            anchor="w",
        )
        self.label.pack(expand=True, fill="both", padx=10)

        # Gán sự kiện chuột cho cả root lẫn label
        for widget in (root, self.label):
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
        self._drag["x"] = event.x_root
        self._drag["y"] = event.y_root
        self._drag["win_x"] = self.root.winfo_x()
        self._drag["win_y"] = self.root.winfo_y()

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

    # ── Data Fetching ──────────────────────────────────────

    def _fetch_single(self, item):
        """Lấy giá cho 1 mã, có cache fallback khi lỗi mạng."""
        symbol_type = item["type"]
        symbol = item["symbol"]
        label_str = item["label"]

        price, change_pct = self.provider.fetch_price(item)

        cache_key = f"{symbol_type}_{symbol}"
        is_fallback = False

        if price is not None:
            with self._prices_lock:
                self.last_prices[cache_key] = {"price": price, "change_pct": change_pct}
        else:
            with self._prices_lock:
                cached = self.last_prices.get(cache_key)
            if cached:
                price = cached["price"]
                change_pct = cached.get("change_pct")
            is_fallback = True

        if price is None:
            return None

        # ── Format output ──
        suffix = "*" if is_fallback else ""
        if symbol_type == "crypto":
            text = f"{label_str}:${price:,.0f}{suffix}"
        else:
            text = f"{label_str}:{price:,.0f}đ{suffix}"

        # Thêm % thay đổi 24h (nếu có)
        if change_pct is not None:
            arrow = "▲" if change_pct >= 0 else "▼"
            sign = "+" if change_pct >= 0 else ""
            text += f"{arrow}{sign}{change_pct:.1f}%"

        return text

    def _update_data(self):
        """Vòng lặp cập nhật dữ liệu song song bằng ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=5) as executor:
            while True:
                futures = [executor.submit(self._fetch_single, item) for item in self.symbols]

                parts = []
                for fut in futures:
                    try:
                        res = fut.result(timeout=7)
                        if res:
                            parts.append(res)
                    except Exception:
                        pass

                if not parts:
                    display = " Loading Data..."
                else:
                    display = " | ".join(parts)

                self.root.after(0, lambda t=display: self.label.config(text=t))

                self._reload_event.wait(UPDATE_INTERVAL)
                self._reload_event.clear()

    def _force_reload(self):
        """Kích hoạt tải lại dữ liệu ngay lập tức."""
        self.root.after(0, lambda: self.label.config(text=" Đang tải lại dữ liệu... "))
        self._reload_event.set()

    def _start_update_thread(self):
        threading.Thread(target=self._update_data, daemon=True).start()

    # ── Settings Window ────────────────────────────────────

    def _open_settings(self, _event=None):
        """Mở cửa sổ chỉnh sửa danh sách mã hiển thị."""
        # Tránh mở nhiều cửa sổ settings trùng lặp
        if self._settings_win is not None:
            try:
                self._settings_win.lift()
                self._settings_win.focus_force()
                return
            except tk.TclError:
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
