import tkinter as tk
from tkinter import simpledialog, messagebox
import requests
import urllib3
import threading
import time
import json
import os
from concurrent.futures import ThreadPoolExecutor

# Tắt cảnh báo SSL khi gọi API vnappmob (để tránh lỗi chứng chỉ trên một số máy Windows)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Xác định đường dẫn tuyệt đối động để tránh lỗi khi chạy từ thư mục Startup
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

DEFAULT_SYMBOLS = [
    {"type": "crypto", "symbol": "BTC", "label": "BTC"},
    {"type": "p2p", "symbol": "USDT", "label": "USDT"},
    {"type": "gold", "symbol": "sjc", "label": "SJC"},
    {"type": "stock", "symbol": "^VNINDEX.VN", "label": "VNI"},
    {"type": "stock", "symbol": "FPT.VN", "label": "FPT"},
    {"type": "stock", "symbol": "E1VFVN30.VN", "label": "VN30"}
]

# Bộ nhớ tạm (cache) lưu trữ giá trị gần nhất để làm fallback khi lỗi mạng
last_prices = {}
# Event điều khiển chu kỳ cập nhật (dùng cho tính năng Tải lại ngay lập tức)
reload_event = threading.Event()

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_config(x, y, symbols=None):
    if symbols is None:
        cfg = load_config() or {}
        symbols = cfg.get("symbols", DEFAULT_SYMBOLS)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"x": x, "y": y, "symbols": symbols}, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def get_crypto_price(symbol):
    try:
        sym = f"{symbol}USDT" if "USDT" not in symbol else symbol
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5)
        return float(r.json()["price"])
    except Exception:
        return None

def get_binance_p2p_price(asset="USDT", fiat="VND", trade_type="BUY"):
    url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    data = {
        "page": 1,
        "rows": 1,
        "payTypes": [],
        "asset": asset,
        "tradeType": trade_type,
        "fiat": fiat,
        "publisherType": None
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=5)
        res = r.json()
        if res.get("code") == "000000" and res.get("data"):
            return float(res["data"][0]["adv"]["price"])
    except Exception:
        pass
    return None

GOLD_TOKEN_CACHE = {"token": None}

def get_gold_price(symbol):
    sym = symbol.lower()
    
    def refresh_token():
        try:
            r = requests.get("https://api.vnappmob.com/api/request_api_key?scope=gold", timeout=5, verify=False)
            if r.status_code == 200:
                data = r.json()
                if "results" in data:
                    GOLD_TOKEN_CACHE["token"] = data["results"]
                    return True
        except Exception:
            pass
        return False

    if not GOLD_TOKEN_CACHE["token"]:
        refresh_token()
        
    if not GOLD_TOKEN_CACHE["token"]:
        return None
        
    url = f"https://api.vnappmob.com/api/v2/gold/{sym}"
    headers = {
        "Authorization": f"Bearer {GOLD_TOKEN_CACHE['token']}"
    }
    
    try:
        r = requests.get(url, headers=headers, timeout=5, verify=False)
        if r.status_code == 401:
            if refresh_token():
                headers["Authorization"] = f"Bearer {GOLD_TOKEN_CACHE['token']}"
                r = requests.get(url, headers=headers, timeout=5, verify=False)
                
        if r.status_code == 200:
            data = r.json()
            if "results" in data and len(data["results"]) > 0:
                gold_info = data["results"][0]
                price_str = gold_info.get("sell_1l") or gold_info.get("sell_1c")
                if price_str:
                    return float(price_str)
    except Exception:
        pass
    return None

def get_yahoo_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    try:
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        if data["chart"]["result"]:
            return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
        return None
    except Exception:
        return None

def fetch_single_price(item):
    """
    Hàm gọi API lấy giá cho một mã cụ thể, hỗ trợ cache fallback nếu gặp lỗi mạng.
    """
    symbol_type = item["type"]
    symbol = item["symbol"]
    label_str = item["label"]
    
    price = None
    if symbol_type == "crypto":
        price = get_crypto_price(symbol)
    elif symbol_type == "p2p":
        price = get_binance_p2p_price(symbol)
    elif symbol_type == "gold":
        price = get_gold_price(symbol)
    else:
        price = get_yahoo_price(symbol)
        
    cache_key = f"{symbol_type}_{symbol}"
    is_fallback = False
    
    if price is not None:
        last_prices[cache_key] = price
    else:
        # Lấy giá trị cũ từ cache nếu lỗi mạng
        price = last_prices.get(cache_key)
        is_fallback = True
        
    if price is not None:
        suffix = "*" if is_fallback else ""
        if symbol_type == "crypto":
            return f"{label_str}:${price:,.0f}{suffix}"
        else:
            return f"{label_str}:{price:,.0f}đ{suffix}"
    return None

def update_data():
    """
    Vòng lặp cập nhật dữ liệu song song sử dụng ThreadPoolExecutor.
    """
    # Khởi tạo executor với tối đa 5 luồng để gọi API song song
    with ThreadPoolExecutor(max_workers=5) as executor:
        while True:
            cfg = load_config() or {}
            symbols = cfg.get("symbols", DEFAULT_SYMBOLS)
            
            # Đệ trình các tác vụ lấy giá song song
            futures = [executor.submit(fetch_single_price, item) for item in symbols]
            
            parts = []
            for fut in futures:
                try:
                    res = fut.result(timeout=7)
                    if res:
                        parts.append(res)
                except Exception:
                    pass
            
            if not parts:
                text = " Loading Data..."
            else:
                text = " | ".join(parts)
                
            # Đẩy cập nhật UI về main thread an toàn
            root.after(0, lambda t=text: label.config(text=t))
            
            # Chờ 10 giây hoặc cho đến khi reload_event được kích hoạt
            reload_event.wait(10)
            reload_event.clear()

def force_reload():
    """
    Kích hoạt tải lại dữ liệu ngay lập tức.
    """
    root.after(0, lambda: label.config(text=" Đang tải lại dữ liệu... "))
    reload_event.set()

def open_settings(event=None):
    cfg = load_config() or {}
    symbols = cfg.get("symbols", DEFAULT_SYMBOLS)
    current_json = json.dumps(symbols, indent=4)
    
    settings_win = tk.Toplevel(root)
    settings_win.title("Cài đặt mã")
    settings_win.geometry("450x350")
    settings_win.attributes("-topmost", True)
    
    lbl = tk.Label(settings_win, text="Chỉnh sửa danh sách hiển thị (định dạng JSON):", font=("Arial", 10))
    lbl.pack(pady=5)
    
    text_area = tk.Text(settings_win, height=14)
    text_area.insert(tk.END, current_json)
    text_area.pack(padx=10, fill=tk.BOTH, expand=True)
    
    def save_settings():
        new_json = text_area.get("1.0", tk.END).strip()
        try:
            new_symbols = json.loads(new_json)
            if not isinstance(new_symbols, list):
                raise ValueError("Config phải là một danh sách dạng [ {...}, {...} ]")
            save_config(root.winfo_x(), root.winfo_y(), new_symbols)
            settings_win.destroy()
            # Kích hoạt cập nhật ngay lập tức sau khi lưu cấu hình
            force_reload()
        except Exception as e:
            messagebox.showerror("Lỗi Cú pháp", f"Lỗi định dạng JSON:\n{e}", parent=settings_win)

    btn_save = tk.Button(settings_win, text="Lưu Cấu Hình", command=save_settings, bg="#2196F3", fg="white", font=("Arial", 10, "bold"))
    btn_save.pack(pady=10)

def show_context_menu(event):
    """
    Hiển thị menu chuột phải.
    """
    try:
        context_menu.tk_popup(event.x_root, event.y_root)
    finally:
        context_menu.grab_release()

if __name__ == "__main__":
    root = tk.Tk()
    root.overrideredirect(True)

    TRANSPARENT_COLOR = "#000001"
    root.configure(bg=TRANSPARENT_COLOR)
    root.attributes("-transparentcolor", TRANSPARENT_COLOR)

    # Tự động điều chỉnh kích thước cửa sổ theo màn hình
    screen_w = root.winfo_screenwidth()
    width = screen_w - 200
    height = 25
    screen_h = root.winfo_screenheight()

    config = load_config()
    if config and "x" in config and "y" in config:
        x = config["x"]
        y = config["y"]
        if "symbols" not in config:
            save_config(x, y, DEFAULT_SYMBOLS)
    else:
        x = 180
        y = screen_h - height - 10
        save_config(x, y, DEFAULT_SYMBOLS)

    root.geometry(f"{width}x{height}+{x}+{y}")

    drag_data = {"x": 0, "y": 0, "win_x": 0, "win_y": 0}

    def drag_start(event):
        drag_data["x"] = event.x_root
        drag_data["y"] = event.y_root
        drag_data["win_x"] = root.winfo_x()
        drag_data["win_y"] = root.winfo_y()

    def drag_motion(event):
        deltax = event.x_root - drag_data["x"]
        deltay = event.y_root - drag_data["y"]
        new_x = drag_data["win_x"] + deltax
        new_y = drag_data["win_y"] + deltay
        root.geometry(f"+{new_x}+{new_y}")

    def drag_stop(event):
        save_config(root.winfo_x(), root.winfo_y())

    root.bind("<ButtonPress-1>", drag_start)
    root.bind("<B1-Motion>", drag_motion)
    root.bind("<ButtonRelease-1>", drag_stop)
    root.bind("<Double-Button-1>", open_settings)

    def keep_on_top():
        root.lift()
        root.attributes("-topmost", True)
        root.after(2000, keep_on_top)
        
    keep_on_top()

    label = tk.Label(
        root,
        text="Đang tải dữ liệu...",
        fg="white",
        bg=TRANSPARENT_COLOR,
        font=("Consolas", 9),
        justify="left",
        anchor="w"
    )

    label.bind("<ButtonPress-1>", drag_start)
    label.bind("<B1-Motion>", drag_motion)
    label.bind("<ButtonRelease-1>", drag_stop)
    label.bind("<Double-Button-1>", open_settings)
    label.pack(expand=True, fill="both", padx=10)

    # Khởi tạo Context Menu chuột phải
    context_menu = tk.Menu(root, tearoff=0)
    context_menu.add_command(label="Cài đặt", command=open_settings)
    context_menu.add_command(label="Tải lại ngay", command=force_reload)
    context_menu.add_separator()
    context_menu.add_command(label="Thoát", command=root.destroy)

    # Bind chuột phải (Button-3) để mở menu
    root.bind("<Button-3>", show_context_menu)
    label.bind("<Button-3>", show_context_menu)

    # Khởi chạy luồng cập nhật dữ liệu
    threading.Thread(target=update_data, daemon=True).start()

    root.mainloop()
