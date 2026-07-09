"""Unit test cho các hàm/logic thuần túy trong ticker.py (không cần màn hình/Tkinter loop)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ticker import (
    TickerApp,
    format_price_text,
    format_change_display,
    DEFAULT_THEME,
)


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
