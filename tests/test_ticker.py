"""Unit test cho các hàm/logic thuần túy trong ticker.py (không cần màn hình/Tkinter loop)."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ticker import (
    TickerApp,
    format_price_text,
    format_change_display,
    DEFAULT_THEME,
)


# ── Helpers ────────────────────────────────────────────────

def _make_app(provider=None):
    """Tạo TickerApp không gọi __init__ (tránh khởi tạo Tk) để test logic thuần."""
    app = TickerApp.__new__(TickerApp)
    app._state_lock = threading.Lock()
    app._prices_lock = threading.Lock()
    app._alert_state = {}
    app.last_prices = {}
    app.theme = DEFAULT_THEME.copy()
    app.provider = provider
    return app


class FakeProvider:
    """Provider giả trả về giá cố định, không gọi API thật."""

    def __init__(self, ret):
        self.ret = ret
        self.calls = 0

    def fetch_price(self, item):
        self.calls += 1
        return self.ret


# ── format_price_text ──────────────────────────────────────

def test_format_price_text_crypto():
    assert format_price_text("BTC", 65000.4, "crypto") == "BTC:$65,000"


def test_format_price_text_stock_vnd():
    assert format_price_text("FPT", 95000, "stock") == "FPT:95,000đ"


def test_format_price_text_fallback_suffix():
    assert format_price_text("BTC", 100, "crypto", is_fallback=True) == "BTC:$100*"


def test_format_price_text_no_data_placeholder():
    assert format_price_text("VNI", None, "stock", no_data=True) == "VNI:--"


# ── format_change_display ──────────────────────────────────

def test_format_change_display_positive():
    text, color = format_change_display(2.5, DEFAULT_THEME)
    assert text == "▲+2.5%"
    assert color == DEFAULT_THEME["positive_color"]


def test_format_change_display_negative():
    text, color = format_change_display(-1.2, DEFAULT_THEME)
    assert text == "▼-1.2%"
    assert color == DEFAULT_THEME["negative_color"]


def test_format_change_display_none():
    text, color = format_change_display(None, DEFAULT_THEME)
    assert text == ""
    assert color == DEFAULT_THEME["fg"]


# ── TickerApp._validate_symbols ────────────────────────────

def test_validate_symbols_valid():
    TickerApp._validate_symbols([
        {"type": "crypto", "symbol": "BTC", "label": "BTC"},
        {"type": "stock", "symbol": "FPT.VN", "label": "FPT"},
    ])  # không raise


def test_validate_symbols_not_a_list():
    with pytest.raises(ValueError):
        TickerApp._validate_symbols({"type": "crypto"})


def test_validate_symbols_missing_key():
    with pytest.raises(ValueError):
        TickerApp._validate_symbols([{"type": "crypto", "symbol": "BTC"}])


def test_validate_symbols_invalid_type():
    with pytest.raises(ValueError):
        TickerApp._validate_symbols([{"type": "forex", "symbol": "BTC", "label": "BTC"}])


def test_validate_symbols_duplicate():
    with pytest.raises(ValueError):
        TickerApp._validate_symbols([
            {"type": "crypto", "symbol": "BTC", "label": "BTC"},
            {"type": "crypto", "symbol": "BTC", "label": "BTC2"},
        ])


# ── TickerApp._check_alert ─────────────────────────────────

def test_check_alert_no_threshold():
    app = _make_app()
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC"}
    assert app._check_alert("crypto_BTC", item, 100000) is False


def test_check_alert_above_rising_edge():
    app = _make_app()
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC", "alert_above": 70000}
    # Lần đầu vượt ngưỡng -> cảnh báo
    assert app._check_alert("crypto_BTC", item, 71000) is True
    # Vẫn còn vượt -> không cảnh báo lặp lại (rising edge)
    assert app._check_alert("crypto_BTC", item, 72000) is False


def test_check_alert_rearm_after_reset():
    app = _make_app()
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC", "alert_above": 70000}
    assert app._check_alert("crypto_BTC", item, 71000) is True
    # Rớt về dưới ngưỡng -> reset trạng thái
    assert app._check_alert("crypto_BTC", item, 60000) is False
    # Vượt lại -> cảnh báo lần nữa
    assert app._check_alert("crypto_BTC", item, 71000) is True


def test_check_alert_below():
    app = _make_app()
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC", "alert_below": 60000}
    assert app._check_alert("crypto_BTC", item, 59000) is True
    assert app._check_alert("crypto_BTC", item, 58000) is False


# ── TickerApp._fetch_single ────────────────────────────────

def test_fetch_single_success_updates_cache():
    app = _make_app(FakeProvider((65000.0, 2.5, {"high": 1, "low": 1, "volume": 1})))
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC"}
    res = app._fetch_single(item)
    assert res["key"] == "crypto_BTC"
    assert res["price_text"] == "BTC:$65,000"
    assert res["change_text"] == "▲+2.5%"
    assert app.last_prices["crypto_BTC"]["price"] == 65000.0


def test_fetch_single_fallback_uses_cache():
    app = _make_app(FakeProvider((None, None, None)))
    app.last_prices["crypto_BTC"] = {"price": 64000.0, "change_pct": 1.0, "extra_info": None}
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC"}
    res = app._fetch_single(item)
    # Có cache -> hiển thị giá cũ kèm dấu '*'
    assert res["price_text"] == "BTC:$64,000*"


def test_fetch_single_no_data_placeholder():
    app = _make_app(FakeProvider((None, None, None)))
    item = {"type": "stock", "symbol": "FPT.VN", "label": "FPT"}
    res = app._fetch_single(item)
    assert res["price_text"] == "FPT:--"


def test_fetch_single_triggers_alert():
    app = _make_app(FakeProvider((71000.0, 1.0, None)))
    item = {"type": "crypto", "symbol": "BTC", "label": "BTC", "alert_above": 70000}
    res = app._fetch_single(item)
    assert res["alert"] is True
