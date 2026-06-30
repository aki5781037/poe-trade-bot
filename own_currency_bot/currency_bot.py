from __future__ import annotations

import ctypes
import json
import math
import os
import re
import sys
import time
import tomllib
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pyautogui
import pyperclip
from rapidocr import RapidOCR

from input_backend import InputBackend, create_input_backend


VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F6 = 0x75
VK_F7 = 0x76
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def configure_text_encoding() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


configure_text_encoding()


STOP_REQUESTED = False


class BotStopRequested(RuntimeError):
    pass


def request_stop() -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    try:
        if input_backend is not None:
            input_backend.release_all()
    except NameError:
        pass


def clear_stop_request() -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = False


def stop_is_requested() -> bool:
    return STOP_REQUESTED


def check_stop() -> None:
    if STOP_REQUESTED:
        raise BotStopRequested("stop requested")


@dataclass
class Match:
    x: int
    y: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x, self.y


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


@dataclass
class TradeLayout:
    screen: np.ndarray
    origin: tuple[int, int]
    window: WindowInfo | None
    title: Match | None
    need: Match | None
    have: Match | None
    order: Match | None

    @property
    def is_open(self) -> bool:
        return self.order is not None or (self.need is not None and self.have is not None)

    def left_slot(self) -> tuple[int, int] | None:
        if not self.need:
            return None
        return (
            self.need.x + int(config.get("SLOT_LEFT_OFFSET_X", -78)),
            self.need.y + int(config.get("SLOT_OFFSET_Y", 39)),
        )

    def right_slot(self) -> tuple[int, int] | None:
        if self.have:
            return (
                self.have.x + int(config.get("SLOT_RIGHT_FROM_HAVE_OFFSET_X", -78)),
                self.have.y + int(config.get("SLOT_OFFSET_Y", 39)),
            )
        if not self.need:
            return None
        return (
            self.need.x + int(config.get("NEED_RIGHT_OFFSET_X", 369)),
            self.need.y + int(config.get("SLOT_OFFSET_Y", 39)),
        )

    def input_left(self) -> tuple[int, int] | None:
        if not self.need:
            return None
        return (
            self.need.x + int(config.get("INPUT_LEFT_OFFSET_X", 128)),
            self.need.y + int(config.get("INPUT_OFFSET_Y", 39)),
        )

    def input_right(self) -> tuple[int, int] | None:
        if not self.need:
            return None
        return (
            self.need.x + int(config.get("INPUT_RIGHT_OFFSET_X", 292)),
            self.need.y + int(config.get("INPUT_OFFSET_Y", 39)),
        )

    def selector_search_box(self) -> tuple[int, int] | None:
        if self.window:
            return (
                self.window.left + int(config.get("SELECT_SEARCH_WINDOW_X", 426)),
                self.window.top + int(config.get("SELECT_SEARCH_WINDOW_Y", 867)),
            )
        if not self.need:
            return None
        return (
            self.need.x + int(config.get("SELECT_SEARCH_OFFSET_X", 150)),
            self.need.y + int(config.get("SELECT_SEARCH_OFFSET_Y", 528)),
        )

    def selector_crop(self) -> tuple[np.ndarray, tuple[int, int]] | None:
        if self.window:
            popup_x = int(config.get("SELECT_POPUP_WINDOW_X", 130))
            popup_y = int(config.get("SELECT_POPUP_WINDOW_Y", 150))
            popup_w = int(config.get("SELECT_POPUP_WINDOW_W", 640))
            popup_h = int(config.get("SELECT_POPUP_WINDOW_H", 730))
            crop = self.screen[popup_y:popup_y + popup_h, popup_x:popup_x + popup_w]
            return crop, (self.origin[0] + popup_x, self.origin[1] + popup_y)
        if not self.need:
            return None
        popup_x = max(0, self.need.x - self.origin[0] + int(config.get("SELECT_POPUP_OFFSET_X", -30)))
        popup_y = max(0, self.need.y - self.origin[1] + int(config.get("SELECT_POPUP_OFFSET_Y", -180)))
        popup_w = int(config.get("SELECT_POPUP_W", 560))
        popup_h = int(config.get("SELECT_POPUP_H", 660))
        crop = self.screen[popup_y:popup_y + popup_h, popup_x:popup_x + popup_w]
        return crop, (self.origin[0] + popup_x, self.origin[1] + popup_y)


def selector_is_open() -> bool:
    screen, origin, win = capture_target_screen()
    if win:
        centers = ocr_text_centers_region(
            screen,
            origin,
            int(config.get("SELECT_POPUP_WINDOW_X", 130)),
            int(config.get("SELECT_POPUP_WINDOW_Y", 150)),
            int(config.get("SELECT_POPUP_WINDOW_W", 640)),
            int(config.get("SELECT_POPUP_WINDOW_H", 730)),
        )
    else:
        centers = ocr_text_centers(screen, origin)
    markers = [
        "我擁有的",
        "我拥有的",
        "全部",
        "通貨",
        "通货",
        "在此輸入",
        "在此输入",
        "關鍵字",
        "关键字",
        "通貨碎片",
        "品質通貨",
        "品質通货",
        "增幅石",
        "徽兆",
        "徵兆",
        "異域",
        "聯盟",
        "核心",
        "探險",
    ]
    return any(any(marker in text for marker in markers) for text, _x, _y in centers)


def game_unavailable_reason() -> str | None:
    if not find_window():
        return "未找到游戏窗口"
    centers = ocr_text_centers(*capture_target_screen()[:2])
    for text, _x, _y in centers:
        if "Deadlock" in text or "Exception" in text:
            return "game exception dialog"
        if text in {"登入", "登录"}:
            return "login screen"
    return None


def game_unavailable_guard() -> bool:
    global unavailable_streak, last_unavailable_reason
    reason = game_unavailable_reason()
    if not reason:
        if unavailable_streak:
            log("游戏窗口已恢复，重置不可用计数:", unavailable_streak)
        unavailable_streak = 0
        last_unavailable_reason = None
        return False

    unavailable_streak += 1
    if reason != last_unavailable_reason or unavailable_streak == 1:
        log("游戏当前不可用:", reason)
    last_unavailable_reason = reason

    threshold = int(config.get("GAME_UNAVAILABLE_COOLDOWN_THRESHOLD", 3))
    cooldown = float(config.get("GAME_UNAVAILABLE_COOLDOWN_SECONDS", 8))
    if unavailable_streak >= threshold:
        log("游戏不可用，进入冷却等待:", reason, "连续次数=", unavailable_streak, "等待秒数=", cooldown)
        if input_backend is not None:
            input_backend.release_all()
        time.sleep(cooldown)
        unavailable_streak = 0
    return True




def normalize_currency_text(text: str) -> str:
    return (
        text.replace(" ", "")
        .replace("[", "")
        .replace("]", "")
        .replace("抺", "抹")
        .replace("廢", "废")
        .replace("抺", "抹")
        .replace("废", "廢")
    )


def ocr_match_currency_name(name: str) -> tuple[int, int, str] | None:
    target = normalize_currency_text(name)
    screen, origin, win = capture_target_screen()
    max_result_y = (win.top + int(config.get("SELECT_SEARCH_WINDOW_Y", 867)) - 35) if win else 10_000
    candidates: list[tuple[int, int, str, bool]] = []
    for text, x, y in ocr_text_centers(screen, origin):
        if y >= max_result_y:
            continue
        normalized = normalize_currency_text(text)
        if target in normalized or normalized.startswith(target):
            candidates.append((x, y, text, "[" in text and "]" in text))
    if not candidates:
        return None
    candidates.sort(key=lambda row: (not row[3], row[1]))
    x, y, text, _priced = candidates[0]
    return x, y, text


def selected_currency_text(side: str, layout: TradeLayout | None = None) -> tuple[str, str]:
    layout = layout or current_trade_layout()
    slot = layout.left_slot() if side == "left" else layout.right_slot()
    if not slot:
        return "", "slot_missing"
    x, y = slot
    w = int(config.get("SELECTED_CURRENCY_TEXT_W", 250))
    h = int(config.get("SELECTED_CURRENCY_TEXT_H", 36))
    offset_x = int(config.get("SELECTED_CURRENCY_TEXT_OFFSET_X", 0))
    offset_y = int(config.get("SELECTED_CURRENCY_TEXT_OFFSET_Y", -18))
    rel_x = x - layout.origin[0] + offset_x
    rel_y = y - layout.origin[1] + offset_y
    crop = layout.screen[max(0, rel_y):rel_y + h, max(0, rel_x):rel_x + w]
    if crop.size == 0:
        return "", "empty_crop"
    text = ocr_small_text(crop, scale=4)
    return text, normalize_currency_text(text)


def selected_currency_icon_matches(name: str, side: str, layout: TradeLayout | None = None) -> tuple[bool, float | None]:
    layout = layout or current_trade_layout()
    slot = layout.left_slot() if side == "left" else layout.right_slot()
    if not slot:
        return False, None
    template = template_path(f"{name}.png")
    if not template.exists():
        return False, None
    crop_w = int(config.get("SELECTED_CURRENCY_ICON_CROP_W", 96))
    crop_h = int(config.get("SELECTED_CURRENCY_ICON_CROP_H", 96))
    x, y = slot
    rel_x = x - layout.origin[0] - crop_w // 2
    rel_y = y - layout.origin[1] - crop_h // 2
    x1 = max(0, rel_x)
    y1 = max(0, rel_y)
    x2 = min(layout.screen.shape[1], rel_x + crop_w)
    y2 = min(layout.screen.shape[0], rel_y + crop_h)
    crop = layout.screen[y1:y2, x1:x2]
    if crop.size == 0:
        return False, None
    match = find_image(
        template,
        screen=crop,
        screen_origin=(layout.origin[0] + x1, layout.origin[1] + y1),
        threshold=float(config.get("SELECTED_CURRENCY_ICON_THRESHOLD", 0.72)),
        quiet=True,
    )
    return (match is not None), (match.score if match else None)


def selected_currency_matches(name: str, side: str, layout: TradeLayout | None = None) -> bool:
    layout = layout or current_trade_layout()
    raw, normalized = selected_currency_text(side, layout)
    target = normalize_currency_text(name)
    text_ok = bool(target and (target in normalized or normalized.startswith(target)))
    icon_ok, icon_score = (False, None) if text_ok else selected_currency_icon_matches(name, side, layout)
    ok = text_ok or icon_ok
    log("栏位通货检查:", side, name, "OCR=", repr(raw), "图标匹配=", icon_score, "结果=", ok)
    return ok

def close_currency_selector_if_open() -> bool:
    layout = current_trade_layout()
    if layout.is_open and not selector_is_open():
        return False
    win = find_window()
    if not win:
        return False
    click_xy(
        win.left + int(config.get("SELECT_CLOSE_WINDOW_X", 585)),
        win.top + int(config.get("SELECT_CLOSE_WINDOW_Y", 106)),
    )
    time.sleep(0.4)
    if not current_trade_layout().is_open and selector_is_open():
        click_xy(
            win.left + int(config.get("SELECT_CLOSE_FALLBACK_WINDOW_X", 742)),
            win.top + int(config.get("SELECT_CLOSE_FALLBACK_WINDOW_Y", 106)),
        )
        time.sleep(0.4)
    return True


def click_selector_all_tab() -> None:
    win = find_window()
    if not win:
        return
    x = win.left + int(config.get("SELECT_ALL_TAB_WINDOW_X", 42))
    y = win.top + int(config.get("SELECT_ALL_TAB_WINDOW_Y", 214))
    click_xy(x, y, duration=0.08)
    log("已点击选择器“全部”选项卡:", (x, y))
    time.sleep(float(config.get("SELECT_ALL_TAB_DELAY", 0.18)))


def confirm_selected_currency_or_snapshot(name: str, side: str, reason: str) -> bool:
    layout = current_trade_layout()
    if selector_is_open():
        log("选择器仍未关闭，选择未完成:", side, name, "阶段=", reason)
        save_debug_snapshot(f"selector_still_open_{reason}_{side}_{name}")
        return False
    if selected_currency_matches(name, side, layout):
        return True
    log("栏位未确认目标通货，停止以避免选错:", side, name, "阶段=", reason)
    save_debug_snapshot(f"selector_unconfirmed_{reason}_{side}_{name}")
    return False


def close_blocking_vendor_panel() -> bool:
    centers = ocr_text_centers(*capture_target_screen()[:2])
    blocking_markers = ["赌博", "賭博", "在此輸入關鍵字", "在此输入关键字"]
    if not any(any(marker in text for marker in blocking_markers) for text, _x, _y in centers):
        return False
    win = find_window()
    if not win:
        return False
    click_xy(
        win.left + int(config.get("VENDOR_CLOSE_WINDOW_X", 641)),
        int(config.get("VENDOR_CLOSE_SCREEN_Y", 211)),
    )
    time.sleep(0.5)
    log("已关闭遮挡的 NPC 面板")
    return True


def reveal_npc_area_for_trade() -> bool:
    """Close only transient selector panels; do not press ESC and risk closing trade UI."""
    layout = current_trade_layout()
    if layout.is_open:
        return False

    closed = False
    if close_currency_selector_if_open():
        closed = True

    centers = ocr_text_centers(*capture_target_screen()[:2])
    visible_markers = [
        "通货交易",
        "通貨交易",
        "通貨交換",
        "通貨兌換",
        "安洁",
        "安潔",
        "安婕",
    ]
    if any(any(marker in text for marker in visible_markers) for text, _x, _y in centers):
        return closed

    log("交易界面未打开，OCR 未看到 NPC 或通货交易入口")
    return closed


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
ROOT_DIR = APP_DIR.parent


def find_project_dir() -> Path:
    candidates = [APP_DIR, ROOT_DIR, *APP_DIR.parents]
    for candidate in candidates:
        if (candidate / "config.toml").exists() and (candidate / "images").exists():
            return candidate
    return ROOT_DIR


PROJECT_DIR = find_project_dir()


def first_existing(candidates: list[Path], fallback: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return fallback


CONFIG_FILE = first_existing(
    [
        APP_DIR / "config.toml",
        PROJECT_DIR / "config.toml",
        BUNDLE_DIR / "config.toml",
        ROOT_DIR / "config.toml",
    ],
    PROJECT_DIR / "config.toml",
)

IMAGES_DIR = first_existing(
    [
        APP_DIR / "images",
        PROJECT_DIR / "images",
        BUNDLE_DIR / "images",
        ROOT_DIR / "images",
    ],
    PROJECT_DIR / "images",
)

LOG_DIR = PROJECT_DIR / "logs"
PRICE_FILE = LOG_DIR / "prices.json"
ORDER_STATE_FILE = LOG_DIR / "order_state.json"
ORDER_BOARD_FILE = LOG_DIR / "order_board.json"
TRADE_LOG_FILE = LOG_DIR / "trade_log.txt"
TRADE_LEDGER_FILE = LOG_DIR / "trade_ledger.jsonl"
RUNTIME_LOG_FILE = LOG_DIR / "runtime.log"
INGAME_SCAN_FILE = LOG_DIR / "ingame_arbitrage_scan.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "BASE_CURRENCY": "混沌石",
    "TRADING_PAIRS": [],
    "CHAOS_FLIP_PAIRS": [],
    "ORDER_BOARD_MODE": True,
    "ORDER_BOARD_PAIRS": [],
    "ORDER_BOARD_MAX_ORDERS": 10,
    "ORDER_BOARD_ORDER_SIZE": 1,
    "CHAOS_FLIP_DIVINE_TO_CHAOS": 10.14,
    "TITLE_OFFSET_X": -84,
    "TITLE_OFFSET_Y": 56,
    "CROP_W": 170,
    "CROP_H": 30,
    "INPUT_LEFT_OFFSET_X": 160,
    "INPUT_RIGHT_OFFSET_X": 360,
    "INPUT_OFFSET_Y": 40,
    "NEED_OFFSET_Y": 40,
    "NEED_RIGHT_OFFSET_X": 263,
    "SELECT_LEFT_RESULT_X": 382,
    "SELECT_RIGHT_RESULT_X": 724,
    "SELECT_RESULT_Y": 171,
    "SELECT_SEARCH_OFFSET_X": 150,
    "SELECT_SEARCH_OFFSET_Y": 528,
    "SELECT_CLOSE_WINDOW_X": 585,
    "SELECT_CLOSE_WINDOW_Y": 106,
    "SELECT_ALL_TAB_WINDOW_X": 42,
    "SELECT_ALL_TAB_WINDOW_Y": 170,
    "SELECT_POPUP_OFFSET_X": -30,
    "SELECT_POPUP_OFFSET_Y": -180,
    "SELECT_POPUP_W": 560,
    "SELECT_POPUP_H": 660,
    "ORDER_COUNT_OFFSET_X": 392,
    "ORDER_COUNT_OFFSET_Y": 87,
    "ORDER_COUNT_HAVE_OFFSET_X": 77,
    "ORDER_COUNT_HAVE_OFFSET_Y": 105,
    "ORDER_COUNT_W": 64,
    "ORDER_COUNT_H": 30,
    "ORDER_TIMEOUT_SECONDS": 600,
    "STOCK_BUY_X1": -100,
    "STOCK_BUY_Y1": -45,
    "STOCK_BUY_X2": -100,
    "STOCK_BUY_Y2": -45,
    "STOCK_SELL_X1": -100,
    "STOCK_SELL_Y1": -45,
    "STOCK_SELL_X2": -100,
    "STOCK_SELL_Y2": -45,
    "MAX_LIMIT": 0,
    "BUY_REDUCE_MIN": 1.0,
    "BUY_REDUCE_MAX": 1.5,
    "SELL_PROFIT_MIN": 1.0,
    "SELL_PROFIT_MAX": 1.5,
    "FIND_THRESHOLD": 0.94,
    "TRADE_LAYOUT_RETRIES": 1,
    "CLICK_DELAY": 0.12,
    "LOOP_DELAY": 0.25,
    "DRY_RUN": False,
    "INPUT_BACKEND": "pydm_driver",
    "PYDM_VID": "0xC216",
    "PYDM_PID": "0x0301",
    "PYDM_MOUSE_ENABLED": True,
    "PYDM_MOVE_MODE": "instant",
    "PYDM_MOVE_STEP_MS": 10,
    "PYDM_DLL_PATH": "",
    "GAME_WINDOW_TITLE": "Path of Exile 2",
    "USE_WINDOW_CAPTURE": True,
    "TRADE_GEOMETRY_FALLBACK": True,
    "TRADE_FALLBACK_TITLE_X": 356,
    "TRADE_FALLBACK_TITLE_Y": 151,
    "TRADE_FALLBACK_NEED_X": 139,
    "TRADE_FALLBACK_NEED_Y": 207,
    "TRADE_FALLBACK_HAVE_X": 559,
    "TRADE_FALLBACK_HAVE_Y": 207,
    "TRADE_FALLBACK_ORDER_X": 354,
    "TRADE_FALLBACK_ORDER_Y": 293,
    "TRADE_FALLBACK_CLOSE_X": 674,
    "TRADE_FALLBACK_CLOSE_Y": 151,
    "NPC_TRADE_ENTRY_FALLBACK_X": 490,
    "NPC_TRADE_ENTRY_FALLBACK_Y": 534,
    "NPC_OCR_WINDOW_X": 0,
    "NPC_OCR_WINDOW_Y": 260,
    "NPC_OCR_WINDOW_W": 620,
    "NPC_OCR_WINDOW_H": 520,
    "NPC_MENU_OCR_WINDOW_X": 300,
    "NPC_MENU_OCR_WINDOW_Y": 520,
    "NPC_MENU_OCR_WINDOW_W": 680,
    "NPC_MENU_OCR_WINDOW_H": 260,
    "INVENTORY_KEY": "i",
    "GOLD_CROP_X": 585,
    "GOLD_CROP_Y": 642,
    "GOLD_CROP_W": 90,
    "GOLD_CROP_H": 28,
    "INVENTORY_GRID_X": 716,
    "INVENTORY_GRID_Y": 515,
    "INVENTORY_GRID_USE_EDGE_ANCHOR": True,
    "INVENTORY_GRID_RIGHT_MARGIN": 580,
    "INVENTORY_GRID_BOTTOM_MARGIN": 484,
    "INVENTORY_GRID_CELL_W": 48,
    "INVENTORY_GRID_CELL_H": 48,
    "INVENTORY_GRID_COLS": 12,
    "INVENTORY_GRID_ROWS": 5,
    "INVENTORY_HOVER_DELAY": 0.18,
    "INVENTORY_CURRENCY_ICON_THRESHOLD": 0.75,
    "INVENTORY_ITEM_ICON_THRESHOLD": 0.72,
    "INVENTORY_CURRENCY_PARTIAL_STACK_THRESHOLD": 0.85,
    "INVENTORY_CURRENCY_STACK_MAX": 20,
    "INVENTORY_TO_SELL_SLOT_DELAY": 0.35,
    "ORDER_BUTTON_SINGLE_CLICK_OFFSET_Y": 8,
    "ORDER_AFTER_CLICK_VERIFY_DELAY": 0.55,
    "ORDER_AFTER_CONFIRM_VERIFY_DELAY": 0.45,
    "ORDER_BOARD_STARTUP_RECONCILE": True,
    "ALLOW_INITIAL_CHAOS_FALLBACK": False,
    "SESSION_INVENTORY_SNAPSHOT": True,
    "GOLD_USE_INVENTORY_ANCHOR": True,
    "INVENTORY_PANEL_STRICT": True,
    "INVENTORY_TITLE_RIGHT_MARGIN": 650,
    "INVENTORY_TITLE_Y": 20,
    "INVENTORY_TITLE_W": 620,
    "INVENTORY_TITLE_H": 160,
    "INVENTORY_GRID_VISUAL_MIN_SCORE": 0.035,
    "GOLD_FROM_GRID_X": 25,
    "GOLD_FROM_GRID_Y": 282,
    "GOLD_FROM_GRID_W": 155,
    "GOLD_FROM_GRID_H": 45,
    "MARKET_HOVER_OFFSET_X": -13,
    "MARKET_HOVER_OFFSET_Y": 47,
    "COMPETITION_PANEL_OFFSET_X": -77,
    "COMPETITION_PANEL_OFFSET_Y": 18,
    "COMPETITION_PANEL_W": 150,
    "COMPETITION_PANEL_H": 285,
    "COMPETITION_FIRST_ROW_X": 8,
    "COMPETITION_FIRST_ROW_Y": 214,
    "COMPETITION_FIRST_ROW_W": 66,
    "COMPETITION_FIRST_ROW_H": 22,
    "BUY_COMPETE_STEP": 1,
    "GOLD_FEE_PER_ORDER": 0,
    "GOLD_FEE_PER_CHAOS": 160,
    "GOLD_FEE_PER_OMEN": 800,
    "STRATEGY_CHAIN_MAX_OMEN": 500,
    "INGAME_SCAN_LIMIT": 4,
    "INGAME_SCAN_INTERVAL": 60,
    "INGAME_SCAN_MIN_SPREAD_PCT": 1.0,
    "INGAME_CONFIRM_ENABLED": True,
    "INGAME_CONFIRM_PRICE_TOLERANCE_PCT": 2.0,
    "AUTO_TRADE_CHAOS_BUDGET": 420,
    "DEBUG_SNAPSHOT_ON_FAILURE": True,
    "DEBUG_SNAPSHOT_INVENTORY_MISS": False,
    "GAME_UNAVAILABLE_COOLDOWN_THRESHOLD": 3,
    "GAME_UNAVAILABLE_COOLDOWN_SECONDS": 8,
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


config = load_config()
ocr_engine: RapidOCR | None = None
input_backend: InputBackend | None = None
unavailable_streak = 0
last_unavailable_reason: str | None = None
pyautogui.FAILSAFE = True
LOG_DIR.mkdir(parents=True, exist_ok=True)


def get_ocr() -> RapidOCR:
    global ocr_engine
    if ocr_engine is None:
        ocr_engine = RapidOCR()
    return ocr_engine


def get_input() -> InputBackend:
    global input_backend
    if input_backend is None:
        input_backend = create_input_backend(config)
        log("输入后端:", config.get("INPUT_BACKEND", "pydm_driver"))
    return input_backend


def log(*parts: object) -> None:
    text = " ".join(str(p) for p in parts)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        safe_line = line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8")
        print(safe_line, flush=True)
    with RUNTIME_LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def set_dpi_aware() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def find_window(title_contains: str | None = None) -> WindowInfo | None:
    set_dpi_aware()
    needle = (title_contains or str(config.get("GAME_WINDOW_TITLE", ""))).strip().lower()
    if not needle:
        return None

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("showCmd", ctypes.c_uint),
            ("ptMinPosition", POINT),
            ("ptMaxPosition", POINT),
            ("rcNormalPosition", RECT),
        ]

    found: list[WindowInfo] = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if needle not in title.lower():
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if bool(user32.IsIconic(hwnd)):
            placement = WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(WINDOWPLACEMENT)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                rect = placement.rcNormalPosition
                width = rect.right - rect.left
                height = rect.bottom - rect.top
        if width > 100 and height > 100:
            found.append(
                WindowInfo(
                    int(hwnd),
                    title,
                    int(rect.left),
                    int(rect.top),
                    int(rect.right),
                    int(rect.bottom),
                )
            )
            return False
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found[0] if found else None


def activate_window(win: WindowInfo | None = None) -> bool:
    win = win or find_window()
    if not win:
        return False
    user32 = ctypes.windll.user32
    if user32.IsIconic(win.hwnd):
        user32.ShowWindow(win.hwnd, 9)
    else:
        user32.ShowWindow(win.hwnd, 5)
    ok = bool(user32.SetForegroundWindow(win.hwnd))
    time.sleep(0.2)
    return ok


def template_path(name: str | Path) -> Path:
    p = Path(name)
    if p.is_absolute():
        return p
    if p.parts and p.parts[0] in {".", "images"}:
        p = Path(*p.parts[1:]) if p.parts[0] == "images" else p
    return IMAGES_DIR / p.name if len(p.parts) == 1 else ROOT_DIR / p


def screenshot_bgr(region: tuple[int, int, int, int] | None = None) -> np.ndarray:
    img = pyautogui.screenshot(region=region)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def capture_target_screen() -> tuple[np.ndarray, tuple[int, int], WindowInfo | None]:
    if bool(config.get("USE_WINDOW_CAPTURE", True)):
        win = find_window()
        if win:
            return screenshot_bgr(region=(win.left, win.top, win.width, win.height)), (win.left, win.top), win
        log("未找到绑定窗口，回退全屏截图:", config.get("GAME_WINDOW_TITLE", ""))
    return screenshot_bgr(), (0, 0), None


def ocr_text_centers(screen: np.ndarray, origin: tuple[int, int]) -> list[tuple[str, int, int]]:
    result = get_ocr()(screen)
    raw_txts = getattr(result, "txts", None)
    raw_boxes = getattr(result, "boxes", None)
    txts = list(raw_txts) if raw_txts is not None else []
    boxes = list(raw_boxes) if raw_boxes is not None else []
    if not txts and isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                boxes.append(item[0])
                txts.append(str(item[1]))
    centers: list[tuple[str, int, int]] = []
    for text, box in zip(txts, boxes):
        try:
            pts = np.array(box, dtype=float).reshape(-1, 2)
        except Exception:
            continue
        centers.append((
            text,
            int(origin[0] + float(pts[:, 0].mean())),
            int(origin[1] + float(pts[:, 1].mean())),
        ))
    return centers


def ocr_text_centers_region(
    screen: np.ndarray,
    origin: tuple[int, int],
    x: int,
    y: int,
    w: int,
    h: int,
) -> list[tuple[str, int, int]]:
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(screen.shape[1], x + w)
    y2 = min(screen.shape[0], y + h)
    if x2 <= x1 or y2 <= y1:
        return []
    return ocr_text_centers(screen[y1:y2, x1:x2], (origin[0] + x1, origin[1] + y1))


def ocr_match_from_centers(centers: list[tuple[str, int, int]], patterns: list[str]) -> Match | None:
    for text, x, y in centers:
        if any(pattern in text for pattern in patterns):
            return Match(x, y, 1.0)
    return None


def trade_geometry_fallback_allowed(screen: np.ndarray, win: WindowInfo) -> bool:
    x = int(config.get("TRADE_FALLBACK_CLOSE_X", 681))
    y = int(config.get("TRADE_FALLBACK_CLOSE_Y", 114))
    x1 = max(0, x - 10)
    y1 = max(0, y - 10)
    x2 = min(screen.shape[1], x + 10)
    y2 = min(screen.shape[0], y + 10)
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return False
    b, g, r = [float(v) for v in crop.reshape(-1, 3).mean(axis=0)]
    return r > 70 and r > g * 1.25 and r > b * 1.25


def detect_trade_layout_once(ocr_fallback: bool = True) -> TradeLayout:
    screen, origin, win = capture_target_screen()
    title = find_image("title.png", screen=screen, screen_origin=origin, threshold=0.6, quiet=True)
    need = find_image("need.png", screen=screen, screen_origin=origin, threshold=0.8, quiet=True)
    order = find_image("order.png", screen=screen, screen_origin=origin, threshold=0.8, quiet=True)
    have = None
    if win and bool(config.get("TRADE_GEOMETRY_FALLBACK", True)) and trade_geometry_fallback_allowed(screen, win):
        if title is None:
            title = Match(win.left + int(config.get("TRADE_FALLBACK_TITLE_X", 361)), win.top + int(config.get("TRADE_FALLBACK_TITLE_Y", 114)), 0.0)
        if need is None:
            need = Match(win.left + int(config.get("TRADE_FALLBACK_NEED_X", 146)), win.top + int(config.get("TRADE_FALLBACK_NEED_Y", 169)), 0.0)
        have = Match(win.left + int(config.get("TRADE_FALLBACK_HAVE_X", 566)), win.top + int(config.get("TRADE_FALLBACK_HAVE_Y", 169)), 0.0)
        if order is None:
            order = Match(win.left + int(config.get("TRADE_FALLBACK_ORDER_X", 360)), win.top + int(config.get("TRADE_FALLBACK_ORDER_Y", 283)), 0.0)
    elif ocr_fallback and bool(config.get("TRADE_LAYOUT_OCR_FALLBACK", True)):
        centers = (
            ocr_text_centers_region(screen, origin, 0, 90, min(screen.shape[1], 720), 280)
            if win
            else ocr_text_centers(screen, origin)
        )
        if title is None:
            title = ocr_match_from_centers(centers, ["通货交换", "通貨交換"])
        if need is None:
            need = ocr_match_from_centers(centers, ["我需要的", "我需要", "需要的"])
        have = ocr_match_from_centers(centers, ["我拥有的", "我擁有的"])
        if order is None:
            order = ocr_match_from_centers(centers, ["下订单", "下訂單"])
    return TradeLayout(
        screen=screen,
        origin=origin,
        window=win,
        title=title,
        need=need,
        have=have,
        order=order,
    )


def current_trade_layout() -> TradeLayout:
    retries = max(1, int(config.get("TRADE_LAYOUT_RETRIES", 3)))
    last = detect_trade_layout_once()
    if last.is_open:
        return last
    for _attempt in range(1, retries):
        time.sleep(0.15)
        current = detect_trade_layout_once()
        if current.is_open:
            return current
        last = current
    return last


def current_trade_layout_fast() -> TradeLayout:
    return detect_trade_layout_once(ocr_fallback=False)


def move_mouse_to_trade_safe_area() -> None:
    layout = current_trade_layout()
    if layout.order:
        click_x = layout.order.x
        click_y = layout.order.y + int(config.get("TRADE_SAFE_OFFSET_Y", 110))
    elif layout.title:
        click_x = layout.title.x
        click_y = layout.title.y + int(config.get("TRADE_SAFE_FROM_TITLE_Y", 260))
    else:
        win = find_window()
        if not win:
            return
        click_x = win.left + win.width // 3
        click_y = win.top + win.height // 2
    get_input().mouse_move(click_x, click_y, duration_ms=80)
    time.sleep(0.15)


def find_image(
    image_name: str | Path,
    screen: np.ndarray | None = None,
    threshold: float | None = None,
    quiet: bool = False,
    screen_origin: tuple[int, int] = (0, 0),
) -> Match | None:
    path = template_path(image_name)
    if not path.exists():
        if not quiet:
            log("图片不存在:", path)
        return None

    if screen is None:
        screen, screen_origin, _ = capture_target_screen()

    template = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        if not quiet:
            log("无法读取图片:", path)
        return None
    h, w = template.shape[:2]
    if screen.shape[0] < h or screen.shape[1] < w:
        if not quiet:
            log("图片匹配区域小于模板:", path.name, "区域=", screen.shape[:2], "模板=", (h, w))
        return None

    if threshold is None:
        threshold = float(config["FIND_THRESHOLD"])

    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(result)
    x = screen_origin[0] + loc[0] + w // 2
    y = screen_origin[1] + loc[1] + h // 2

    if score >= threshold:
        if not quiet:
            log("找到图片", path.name, "坐标:", (x, y), "匹配度:", f"{score:.3f}")
        return Match(x, y, float(score))

    if not quiet:
        log("未找到", path.name, "最佳匹配:", f"{score:.3f}")
    return None


def click_xy(x: int, y: int, button: str = "left", duration: float = 0.08) -> None:
    check_stop()
    if config.get("DRY_RUN"):
        log("[DRY_RUN] 点击", x, y, button)
        return
    get_input().mouse_move(x, y, duration_ms=int(duration * 1000))
    check_stop()
    get_input().mouse_click(button=button, hold_ms=35)
    time.sleep(float(config["CLICK_DELAY"]))


def click_image(
    image_name: str | Path,
    offset_x: int = 0,
    offset_y: int = 0,
    threshold: float | None = None,
    screen: np.ndarray | None = None,
    quiet: bool = False,
) -> tuple[int, int] | None:
    match = find_image(image_name, screen=screen, threshold=threshold, quiet=quiet)
    if not match:
        return None
    x = match.x + offset_x
    y = match.y + offset_y
    click_xy(x, y)
    log("点击坐标:", (x, y), "模板:", Path(str(image_name)).name)
    return x, y



def wait_for_image(
    image_name: str | Path,
    timeout: float = 2.0,
    threshold: float | None = None,
) -> Match | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_stop()
        match = find_image(image_name, threshold=threshold, quiet=True)
        if match:
            return match
        time.sleep(0.2)
    return None

def paste_text(text: str) -> None:
    check_stop()
    if config.get("DRY_RUN"):
        log("[DRY_RUN] 粘贴", text)
        return
    pyperclip.copy(text)
    check_stop()
    get_input().hotkey("ctrl", "v", hold_ms=35)
    time.sleep(0.2)


def replace_text(text: str) -> None:
    check_stop()
    if config.get("DRY_RUN"):
        log("[DRY_RUN] 替换文本", text)
        return
    get_input().hotkey("ctrl", "a", hold_ms=35)
    paste_text(text)


def input_and_verify(text: str, attempts: int = 5) -> bool:
    text = str(text)
    for i in range(attempts):
        check_stop()
        get_input().hotkey("ctrl", "a", hold_ms=35)
        paste_text(text)
        get_input().hotkey("ctrl", "a", hold_ms=35)
        get_input().hotkey("ctrl", "c", hold_ms=35)
        actual = pyperclip.paste()
        if actual == text:
            return True
        log("输入验证不匹配", i + 1, "期望:", text, "实际:", actual)
        time.sleep(0.2)
    return False


def read_market_text() -> str:
    title = find_image("title.png", threshold=0.65, quiet=True)
    if title:
        x = title.x + int(config["TITLE_OFFSET_X"])
        y = title.y + int(config["TITLE_OFFSET_Y"])
    else:
        log("未找到 title.png，使用屏幕左上默认 OCR 区域")
        x, y = 0, 0

    w = int(config["CROP_W"])
    h = int(config["CROP_H"])
    img = screenshot_bgr(region=(x, y, w, h))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    start = time.perf_counter()
    result = get_ocr()(gray, use_det=False, use_cls=False)
    elapsed = time.perf_counter() - start

    txts = getattr(result, "txts", None)
    if txts is None:
        txts = []
        if isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    txts.append(str(item[1]))
                else:
                    txts.append(str(item))

    text = "".join(re.findall(r"[\d.:]+", "".join(txts or [])))
    log("OCR:", text, "耗时:", f"{elapsed:.3f}s")
    return text


def read_gold_amount_from_screen(
    screen: np.ndarray,
    screen_origin: tuple[int, int] = (0, 0),
) -> tuple[int | None, str]:
    if bool(config.get("GOLD_USE_INVENTORY_ANCHOR", True)):
        grid = inventory_grid_origin()
        if grid:
            grid_x, grid_y, _method = grid
            x = grid_x + int(config.get("GOLD_FROM_GRID_X", 0)) - screen_origin[0]
            y = grid_y + int(config.get("GOLD_FROM_GRID_Y", 282)) - screen_origin[1]
            w = int(config.get("GOLD_FROM_GRID_W", 170))
            h = int(config.get("GOLD_FROM_GRID_H", 38))
        else:
            x = int(config.get("GOLD_CROP_X", 585))
            y = int(config.get("GOLD_CROP_Y", 642))
            w = int(config.get("GOLD_CROP_W", 90))
            h = int(config.get("GOLD_CROP_H", 28))
    else:
        x = int(config.get("GOLD_CROP_X", 585))
        y = int(config.get("GOLD_CROP_Y", 642))
        w = int(config.get("GOLD_CROP_W", 90))
        h = int(config.get("GOLD_CROP_H", 28))
    crop = screen[y : y + h, x : x + w]
    if crop.size == 0:
        return None, ""

    candidates: list[tuple[int, int, int, str, str]] = []
    raw_texts: list[str] = []
    for scale in (3, 4, 5, 6):
        up = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        variants = [
            (f"gray{scale}", gray),
            (f"otsu{scale}", cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]),
            (f"invotsu{scale}", cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]),
        ]
        for variant_name, image in variants:
            result = get_ocr()(image, use_det=False, use_cls=False)
            text = "".join(getattr(result, "txts", ()) or ())
            raw_texts.append(f"{variant_name}:{text}")
            for match in re.finditer(r"\d[\d,]*", text):
                matched = match.group(0)
                digits = re.sub(r"\D", "", matched)
                if len(digits) < 4:
                    continue
                groups = matched.split(",")
                valid_commas = int(
                    len(groups) > 1
                    and 1 <= len(groups[0]) <= 3
                    and all(len(group) == 3 for group in groups[1:])
                )
                candidates.append((valid_commas, len(digits), int(digits), matched, variant_name))

    if not candidates:
        return None, "; ".join(raw_texts)
    candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    _valid_commas, _length, amount, matched, variant_name = candidates[0]
    return amount, f"{variant_name}:{matched}; " + "; ".join(raw_texts)


def read_gold_amount() -> tuple[int | None, str]:
    screen, origin, _win = capture_target_screen()
    return read_gold_amount_from_screen(screen, origin)


def inventory_panel_visible_details() -> tuple[bool, str]:
    screen, origin, win = capture_target_screen()
    if not win:
        return False, "window_not_found"

    title_x = max(0, screen.shape[1] - int(config.get("INVENTORY_TITLE_RIGHT_MARGIN", 650)))
    title_y = int(config.get("INVENTORY_TITLE_Y", 20))
    title_w = int(config.get("INVENTORY_TITLE_W", 620))
    title_h = int(config.get("INVENTORY_TITLE_H", 160))
    title_patterns = ["背包", "物品欄", "物品栏", "Inventory", "使用預設", "使用预设"]
    title_hits = [
        text
        for text, _x, _y in ocr_text_centers_region(screen, origin, title_x, title_y, title_w, title_h)
        if any(pattern in text for pattern in title_patterns)
    ]

    grid_score = inventory_grid_visual_score(screen, origin)
    gold_amount, gold_raw = read_gold_amount_from_screen(screen, origin)
    grid_ok = grid_score >= float(config.get("INVENTORY_GRID_VISUAL_MIN_SCORE", 0.035))
    gold_ok = gold_amount is not None and gold_amount >= 1000

    if bool(config.get("INVENTORY_PANEL_STRICT", True)):
        visible = bool(title_hits) or (grid_ok and gold_ok)
    else:
        visible = bool(title_hits) or grid_ok or gold_ok

    return visible, (
        f"标题={title_hits[:2] or '无'} "
        f"格子={grid_score:.3f}/{grid_ok} "
        f"金币={gold_amount if gold_amount is not None else '无'}/{gold_ok} "
        f"金币OCR={gold_raw[:80]}"
    )


def inventory_panel_visible() -> bool:
    visible, raw = inventory_panel_visible_details()
    log("背包可见:", visible, raw)
    return visible


def parse_int_text(text: str) -> int | None:
    match = re.search(r"\d[\d,\.]*", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else None


def save_inventory_session_snapshot(reason: str) -> dict[str, str | None]:
    if not bool(config.get("SESSION_INVENTORY_SNAPSHOT", True)):
        return {"image": None, "ocr": None}
    safe_reason = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", reason).strip("_")[:80] or "session"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screen, origin, win = capture_target_screen()
    image_path = LOG_DIR / f"session_{stamp}_{safe_reason}.png"
    text_path = LOG_DIR / f"session_{stamp}_{safe_reason}_ocr.txt"
    grid = inventory_grid_origin()
    try:
        ok, encoded = cv2.imencode(".png", screen)
        if ok:
            encoded.tofile(str(image_path))
        centers = ocr_text_centers(screen, origin)
        lines = [
            f"reason={reason}",
            f"window={win}",
            f"origin={origin}",
            f"inventory_grid_origin={grid}",
            "",
        ]
        for text, x, y in centers:
            lines.append(f"{x},{y}\t{text}")
        text_path.write_text("\n".join(lines), encoding="utf-8")
        log("初始背包截图已保存:", image_path, text_path)
        return {"image": str(image_path), "ocr": str(text_path)}
    except Exception as exc:
        log("初始背包截图保存失败:", exc)
        return {"image": None, "ocr": None}


def read_number_from_slot_crop(slot_crop: np.ndarray) -> tuple[int | None, str]:
    if slot_crop.size == 0:
        return None, ""
    h, w = slot_crop.shape[:2]
    crops = [
        slot_crop[max(0, h // 2 - 4):h, max(0, w // 3):w],
        slot_crop[max(0, h // 3):h, 0:w],
        slot_crop,
    ]
    texts: list[str] = []
    for crop in crops:
        up = cv2.resize(crop, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        result = get_ocr()(thresh, use_det=False, use_cls=False)
        text = "".join(getattr(result, "txts", ()) or ())
        texts.append(text)
        amount = parse_int_text(text)
        if amount is not None:
            return amount, " | ".join(texts)
    return None, " | ".join(texts)


def currency_icon_templates(name: str) -> list[np.ndarray]:
    paths = [
        template_path(f"{name}_背包图标.png"),
        template_path(f"{name}.png"),
    ]
    templates: list[np.ndarray] = []
    for path in paths:
        if not path.exists():
            continue
        template = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if template is None:
            continue
        templates.append(template)
        h, w = template.shape[:2]
        side = min(h, w)
        if side >= 16 and w != h:
            templates.append(template[:side, :side])
    return templates


def backpack_icon_templates(name: str) -> list[np.ndarray]:
    path = template_path(f"{name}_背包图标.png")
    if not path.exists():
        return []
    template = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if template is None:
        return []
    return [template]


def inventory_item_icon_count(name: str) -> tuple[int, str]:
    templates = backpack_icon_templates(name)
    if not templates:
        return 0, "backpack_icon_template_missing"
    if game_unavailable_reason():
        return 0, "game_unavailable"
    if not ensure_inventory_open():
        return 0, "inventory_not_open"
    screen, origin, _win = capture_target_screen()
    grid = inventory_grid_origin()
    if not grid:
        return 0, "inventory_grid_not_found"

    start_x, start_y, _method = grid
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    threshold = float(config.get("INVENTORY_ITEM_ICON_THRESHOLD", 0.72))
    pad = int(config.get("INVENTORY_GRID_ICON_PAD", 6))
    x1 = max(0, start_x - origin[0] - pad)
    y1 = max(0, start_y - origin[1] - pad)
    x2 = min(screen.shape[1], start_x - origin[0] + cols * cell_w + pad)
    y2 = min(screen.shape[0], start_y - origin[1] + rows * cell_h + pad)
    grid_crop = screen[y1:y2, x1:x2]
    if grid_crop.size == 0:
        return 0, "empty_grid_crop"

    best_overall = -1.0
    points: list[tuple[int, int, float, int, int]] = []
    for template in templates:
        th, tw = template.shape[:2]
        if th > grid_crop.shape[0] or tw > grid_crop.shape[1]:
            continue
        result = cv2.matchTemplate(grid_crop, template, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(result)
        best_overall = max(best_overall, float(max_val))
        ys, xs = np.where(result >= threshold)
        for x, y in zip(xs, ys):
            points.append((int(x), int(y), float(result[y, x]), tw, th))

    points.sort(key=lambda row: row[2], reverse=True)
    selected: list[tuple[int, int, float, int, int]] = []
    for x, y, score, tw, th in points:
        cx = x + tw // 2
        cy = y + th // 2
        if any(abs(cx - (sx + sw // 2)) < cell_w // 2 and abs(cy - (sy + sh // 2)) < cell_h // 2 for sx, sy, _s, sw, sh in selected):
            continue
        selected.append((x, y, score, tw, th))

    hits = [
        f"({origin[0] + x1 + x + tw // 2},{origin[1] + y1 + y + th // 2})@{score:.3f}"
        for x, y, score, tw, th in selected
    ]
    avg_score = sum(score for _x, _y, score, _tw, _th in selected) / len(selected) if selected else 0.0
    raw = f"whole_grid best={best_overall:.3f} avg={avg_score:.3f}; " + "; ".join(hits)
    if selected:
        log("背包物品图标数量:", name, len(selected), f"平均匹配={avg_score:.3f}", raw)
    return len(selected), raw


def read_inventory_currency_amount(name: str) -> tuple[int | None, str]:
    if game_unavailable_reason():
        return None, "game_unavailable"
    if not ensure_inventory_open():
        return None, "inventory_not_open"
    screen, origin, _win = capture_target_screen()
    grid = inventory_grid_origin()
    if not grid:
        return None, "inventory_grid_not_found"

    start_x, start_y, _method = grid
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    pad = int(config.get("INVENTORY_GRID_OCR_PAD", 8))
    x1 = max(0, start_x - origin[0] - pad)
    y1 = max(0, start_y - origin[1] - pad)
    x2 = min(screen.shape[1], start_x - origin[0] + cols * cell_w + pad)
    y2 = min(screen.shape[0], start_y - origin[1] + rows * cell_h + pad)
    grid_crop = screen[y1:y2, x1:x2]
    if grid_crop.size == 0:
        return None, "empty_grid_crop"

    threshold = float(config.get("INVENTORY_CURRENCY_ICON_THRESHOLD", 0.75))
    partial_threshold = float(config.get("INVENTORY_CURRENCY_PARTIAL_STACK_THRESHOLD", 0.85))
    stack_max = int(config.get("INVENTORY_CURRENCY_STACK_MAX", 20))
    templates = currency_icon_templates(name)
    if not templates:
        return None, "currency_template_missing"

    best_scan: tuple[int, float, int, list[str], str] | None = None
    for y_offset in (0, cell_h):
        total = 0
        score_sum = 0.0
        hit_count = 0
        best_overall = -1.0
        hits: list[str] = []
        for row in range(rows):
            for col in range(cols):
                slot_x = start_x - origin[0] + col * cell_w
                slot_y = start_y + y_offset - origin[1] + row * cell_h
                slot_crop = screen[
                    max(0, slot_y): min(screen.shape[0], slot_y + cell_h),
                    max(0, slot_x): min(screen.shape[1], slot_x + cell_w),
                ]
                if slot_crop.size == 0:
                    continue
                best_score = -1.0
                for template in templates:
                    th, tw = template.shape[:2]
                    if th > slot_crop.shape[0] or tw > slot_crop.shape[1]:
                        continue
                    result = cv2.matchTemplate(slot_crop, template, cv2.TM_CCOEFF_NORMED)
                    _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(result)
                    best_score = max(best_score, float(max_val))
                best_overall = max(best_overall, best_score)
                if best_score < threshold:
                    continue
                amount = stack_max
                raw = "assume_full_stack"
                if best_score < partial_threshold:
                    parsed_amount, parsed_raw = read_number_from_slot_crop(slot_crop)
                    raw = parsed_raw
                    if parsed_amount is not None and 0 < parsed_amount <= stack_max:
                        amount = parsed_amount
                    else:
                        raw = f"堆叠数量OCR失败:{parsed_raw!r};按满堆叠估算"
                total += amount
                score_sum += best_score
                hit_count += 1
                hits.append(f"({col},{row})={amount}@{best_score:.3f}:{raw!r}")
        avg_score = score_sum / hit_count if hit_count else 0.0
        raw_summary = f"y_offset={y_offset} best={best_overall:.3f} avg={avg_score:.3f}; " + "; ".join(hits)
        candidate = (hit_count, avg_score, total, hits, raw_summary)
        if best_scan is None or (candidate[0], candidate[1]) > (best_scan[0], best_scan[1]):
            best_scan = candidate

    if best_scan is None or not best_scan[3]:
        return None, "currency_icon_not_found"
    hit_count, avg_score, total, hits, raw_summary = best_scan
    if hit_count == 0:
        return None, raw_summary
    log("背包通货总数:", name, total, f"命中={hit_count}", f"平均匹配={avg_score:.3f}", raw_summary)
    return total, raw_summary


def open_inventory_and_read_gold() -> tuple[int | None, str]:
    if not ensure_inventory_open():
        return None, "inventory_not_open"
    return read_gold_amount()


def ensure_inventory_open() -> bool:
    if inventory_panel_visible():
        return True
    if selector_is_open():
        log("通货选择器已打开，跳过背包开关")
        return False
    win = find_window()
    if win:
        activate_window(win)
    get_input().key_tap(str(config.get("INVENTORY_KEY", "i")), hold_ms=60)
    time.sleep(0.8)
    ok = inventory_panel_visible()
    _amount, raw = read_gold_amount() if ok else (None, "未检测到背包界面")
    log("确认背包打开:", "成功" if ok else "失败", raw)
    return ok


def ensure_trade_ui_open() -> bool:
    check_stop()
    start = time.perf_counter()
    win = find_window()
    if not win:
        log("游戏不可用，无法打开交易界面: 未找到窗口")
        return False

    check_stop()
    activate_window(win)

    layout = current_trade_layout_fast()
    if layout.is_open:
        log("确认交易界面: 已经打开", f"{time.perf_counter() - start:.2f}s")
        return True

    for attempt in range(2):
        check_stop()
        layout = current_trade_layout_fast()
        if layout.is_open:
            log("确认交易界面: 已打开", f"{time.perf_counter() - start:.2f}s")
            return True

        trade = find_npc_trade_entry()
        if trade:
            check_stop()
            log("OCR 点击通货交易入口:", trade)
            click_xy(trade[0], trade[1])
            time.sleep(0.6)
            layout = current_trade_layout_fast()
            if layout.is_open:
                log("确认交易界面: 通过交易入口打开", f"{time.perf_counter() - start:.2f}s")
                return True

        npc = find_trade_npc()
        if npc:
            check_stop()
            log("OCR 点击 NPC:", npc)
            click_xy(npc[0], npc[1])
            for _menu_attempt in range(4):
                check_stop()
                time.sleep(0.25)
                trade = find_npc_trade_entry()
                if trade:
                    check_stop()
                    log("OCR 点击 NPC 菜单里的通货交易:", trade)
                    click_xy(trade[0], trade[1])
                    time.sleep(0.6)
                    layout = current_trade_layout_fast()
                    if layout.is_open:
                        log("确认交易界面: 通过 NPC 打开", f"{time.perf_counter() - start:.2f}s")
                        return True
                    break
            log("NPC 菜单已打开，但 OCR 没找到通货交易入口")
            continue

        if attempt < 1:
            time.sleep(0.5)

    layout = current_trade_layout()
    ok = layout.is_open
    log("确认交易界面:", "成功" if ok else "失败", f"{time.perf_counter() - start:.2f}s")
    return ok


def ensure_trade_and_inventory_open() -> bool:
    check_stop()
    start = time.perf_counter()
    trade_ok = ensure_trade_ui_open()
    if not trade_ok:
        log("确认交易界面和背包: 交易界面失败", f"{time.perf_counter() - start:.2f}s")
        return False
    check_stop()
    inv_ok = ensure_inventory_open()
    log("确认交易界面和背包:", "成功" if inv_ok else "背包失败", f"{time.perf_counter() - start:.2f}s")
    return trade_ok and inv_ok


def parse_ratio(text: str) -> tuple[float, float] | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)", text)
    if match:
        parts = [match.group(1), match.group(2)]
    else:
        parts = text.split(":")
    if len(parts) != 2:
        return None
    parts = [collapse_repeated_number(part) for part in parts]
    try:
        left = float(parts[0])
        right = float(parts[1])
    except ValueError:
        return None
    if left <= 0 or right <= 0:
        return None
    return left, right


def collapse_repeated_number(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) == 2 and cleaned == "11":
        return "1"
    if len(cleaned) < 4 or len(cleaned) % 2:
        return cleaned
    half = len(cleaned) // 2
    if cleaned[:half] == cleaned[half:]:
        return cleaned[:half]
    return cleaned


def ocr_small_text(img: np.ndarray, scale: int = 3) -> str:
    if img.size == 0:
        return ""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    up = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    result = get_ocr()(up, use_det=False, use_cls=False)
    return "".join(getattr(result, "txts", ()) or ())


def ocr_find_text_center(patterns: list[str]) -> tuple[int, int, str] | None:
    screen, origin, _win = capture_target_screen()
    result = get_ocr()(screen)
    raw_txts = getattr(result, "txts", None)
    raw_boxes = getattr(result, "boxes", None)
    txts = list(raw_txts) if raw_txts is not None else []
    boxes = list(raw_boxes) if raw_boxes is not None else []
    if not txts and isinstance(result, (list, tuple)):
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                boxes.append(item[0])
                txts.append(str(item[1]))
    for text, box in zip(txts, boxes):
        if not any(pattern in text for pattern in patterns):
            continue
        pts = np.array(box, dtype=float).reshape(-1, 2)
        return (
            int(origin[0] + float(pts[:, 0].mean())),
            int(origin[1] + float(pts[:, 1].mean())),
            text,
        )
    return None


def median_ratio(candidates: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not candidates:
        return None
    lefts = sorted(left for left, _right in candidates)
    rights = sorted(right for _left, right in candidates)
    mid = len(candidates) // 2
    if len(candidates) % 2:
        return lefts[mid], rights[mid]
    return (lefts[mid - 1] + lefts[mid]) / 2, (rights[mid - 1] + rights[mid]) / 2


def consensus_ratio(candidates: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not candidates:
        return None
    buckets: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for left, right in candidates:
        buckets.setdefault((round(left, 2), round(right, 2)), []).append((left, right))
    bucket = max(
        buckets.values(),
        key=lambda values: (len(values), min(max(left, right) for left, right in values)),
    )
    return median_ratio(bucket)


def normalize_price_with_hint(price: float, expected_price: float | None) -> float:
    if not expected_price or expected_price <= 0 or price <= 0:
        return price
    normalized = price
    while normalized > expected_price * 3 and normalized >= 100:
        normalized /= 10
    while normalized < expected_price / 3:
        normalized *= 10
    return normalized


def ratio_from_price(price: float, target_side: str) -> tuple[float, float]:
    if target_side == "left":
        return 1.0, price
    if target_side == "right":
        return price, 1.0
    raise ValueError(f"unknown target_side: {target_side}")


def normalize_ratio_with_hint(
    ratio: tuple[float, float] | None,
    expected_price: float | None,
    target_side: str | None,
) -> tuple[float, float] | None:
    if not ratio or expected_price is None or target_side is None:
        return ratio
    left, right = ratio
    price = right / left if target_side == "left" else left / right
    price = normalize_price_with_hint(price, expected_price)
    if price < expected_price * 0.5 or price > expected_price * 1.6:
        return None
    return ratio_from_price(price, target_side)


def scan_competition_ratio_from_image(
    img: np.ndarray,
    panel_x: int,
    panel_y: int,
    *,
    expected_price: float | None = None,
    target_side: str | None = None,
) -> tuple[float, float] | None:
    row_w = int(config.get("COMPETITION_FIRST_ROW_W", 66))
    row_h = int(config.get("COMPETITION_FIRST_ROW_H", 22))
    x_offsets = [-25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 25]
    y_offsets = list(range(170, 233, 7))

    best_raw: list[str] = []
    for dy in y_offsets:
        row_candidates: list[tuple[float, float]] = []
        row_raw: list[str] = []
        for dx in x_offsets:
            x = panel_x + dx
            y = panel_y + dy
            if x < 0 or y < 0 or x + row_w > img.shape[1] or y + row_h > img.shape[0]:
                continue
            raw = ocr_small_text(img[y : y + row_h, x : x + row_w])
            ratio = parse_ratio(raw)
            if not ratio:
                continue
            left, right = ratio
            if left <= 0 or right <= 0 or left > 5000 or right > 5000:
                continue
            if expected_price is not None and target_side is not None:
                price = right / left if target_side == "left" else left / right
                price = normalize_price_with_hint(price, expected_price)
                if price < expected_price * 0.5 or price > expected_price * 1.6:
                    continue
                row_candidates.append(ratio_from_price(price, target_side))
            else:
                row_candidates.append((left, right))
            row_raw.append(raw)

        if len(row_candidates) >= 2:
            ratio = consensus_ratio(row_candidates)
            log("竞争单价扫描:", "dy=", dy, "raw=", row_raw, "=>", ratio)
            return ratio
        best_raw.extend(row_raw)

    if best_raw:
        log("竞争单价扫描未稳定命中:", best_raw)
    return None


def read_first_competition_ratio(
    expected_price: float | None = None,
    target_side: str | None = None,
) -> tuple[float, float] | None:
    layout = current_trade_layout()
    title = layout.title or find_image("title.png", threshold=0.65, quiet=True)
    if not title:
        log("未找到市场比率标题锚点，无法读取竞争价格")
        save_debug_snapshot("competition_no_title")
        return None

    backend = get_input()
    hover_x = title.x + int(config.get("MARKET_HOVER_OFFSET_X", -13))
    hover_y = title.y + int(config.get("MARKET_HOVER_OFFSET_Y", 47))
    panel_x = title.x + int(config.get("COMPETITION_PANEL_OFFSET_X", -77))
    panel_y = title.y + int(config.get("COMPETITION_PANEL_OFFSET_Y", 18))
    row_x = panel_x + int(config.get("COMPETITION_FIRST_ROW_X", 22))
    row_y = panel_y + int(config.get("COMPETITION_FIRST_ROW_Y", 186))
    row_w = int(config.get("COMPETITION_FIRST_ROW_W", 92))
    row_h = int(config.get("COMPETITION_FIRST_ROW_H", 24))

    backend.mouse_move(hover_x, hover_y, duration_ms=100)
    time.sleep(0.15)
    backend.key_down("alt")
    try:
        time.sleep(0.45)
        screen, origin, _win = capture_target_screen()
        scanned = scan_competition_ratio_from_image(
            screen,
            panel_x - origin[0],
            panel_y - origin[1],
            expected_price=expected_price,
            target_side=target_side,
        )
        if scanned:
            return scanned

        img = screenshot_bgr(region=(row_x, row_y, row_w, row_h))
        raw = ocr_small_text(img)
        ratio = parse_ratio(raw) or parse_single_unit_price(raw)
        ratio = normalize_ratio_with_hint(ratio, expected_price, target_side)
        log("竞争单价 OCR:", repr(raw), "=>", ratio)
        if not ratio:
            save_debug_snapshot("competition_ocr_failed")
        return ratio
    finally:
        backend.key_up("alt")
        # The Alt competition panel can linger over the order button. Move away
        # from the market-ratio hover point before filling fields/clicking order.
        try:
            if layout.order:
                backend.mouse_move(layout.order.x, layout.order.y + int(config.get("AFTER_ALT_SAFE_OFFSET_Y", 95)), duration_ms=80)
            elif layout.window:
                backend.mouse_move(
                    layout.window.left + int(config.get("AFTER_ALT_SAFE_WINDOW_X", 90)),
                    layout.window.top + int(config.get("AFTER_ALT_SAFE_WINDOW_Y", 430)),
                    duration_ms=80,
                )
        except Exception:
            pass
        time.sleep(float(config.get("AFTER_ALT_TOOLTIP_CLEAR_DELAY", 0.25)))


def parse_single_unit_price(text: str) -> tuple[float, float] | None:
    digits = re.sub(r"\D", "", text)
    digits = collapse_repeated_number(digits)
    if not digits:
        return None
    try:
        price = float(digits)
    except ValueError:
        return None
    return 1.0, price


def choose_buy_price_from_competition() -> tuple[int, int, float] | None:
    ratio = read_first_competition_ratio()
    if not ratio:
        return None
    return choose_buy_price_from_competition_ratio(ratio)


def choose_buy_price_from_competition_ratio(ratio: tuple[float, float]) -> tuple[int, int, float] | None:
    return choose_buy_price(*ratio)


def read_market_ratio(attempts: int = 10, delay: float = 0.5) -> tuple[tuple[float, float] | None, str]:
    raw_text = ""
    for attempt in range(attempts):
        raw_text = read_market_text()
        ratio = parse_ratio(raw_text)
        if ratio:
            return ratio, raw_text
        log("市场比率解析失败:", raw_text, "重试:", attempt + 1)
        time.sleep(delay)
    return None, raw_text


def load_prices() -> dict[str, dict[str, Any]]:
    if not PRICE_FILE.exists():
        return {}
    try:
        return json.loads(PRICE_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log("读取价格记录失败:", exc)
        return {}


def save_prices(data: dict[str, dict[str, Any]]) -> None:
    PRICE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_pair_price(pair_name: str, price: dict[str, Any]) -> None:
    data = load_prices()
    data[pair_name] = price
    save_prices(data)
    log("保存买入价格:", pair_name, price)


def remove_pair_price(pair_name: str) -> None:
    data = load_prices()
    if pair_name in data:
        del data[pair_name]
        save_prices(data)
    log("清除价格记录:", pair_name)


def get_pair_price(pair_name: str) -> dict[str, Any] | None:
    return load_prices().get(pair_name)


def load_order_state() -> dict[str, Any] | None:
    if not ORDER_STATE_FILE.exists():
        return None
    try:
        data = json.loads(ORDER_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log("读取订单状态失败:", exc)
        return None
    if not isinstance(data, dict) or not data.get("name") or data.get("action") not in {"buy", "sell"}:
        return None
    return data


def save_order_state(pair_name: str, action: str, details: dict[str, Any] | None = None) -> None:
    data = {
        "name": pair_name,
        "action": action,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "details": details or {},
    }
    ORDER_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log("保存订单状态:", data)


def clear_order_state() -> None:
    if ORDER_STATE_FILE.exists():
        ORDER_STATE_FILE.unlink()
    log("清除订单状态")


def clear_order_board_state() -> None:
    if ORDER_BOARD_FILE.exists():
        ORDER_BOARD_FILE.unlink()
    log("清除订单板状态")


def configured_max_limit(fallback: int = 500) -> int:
    raw_limit = int(config.get("MAX_LIMIT", 0))
    return raw_limit if raw_limit > 0 else fallback


def choose_buy_price(left: float, right: float, max_cost: float | None = None) -> tuple[int, int, float] | None:
    market_price = right / left
    candidates: list[tuple[float, int, int]] = []
    max_limit = configured_max_limit()
    min_reduce = float(config["BUY_REDUCE_MIN"])
    max_reduce = float(config["BUY_REDUCE_MAX"])

    for got in range(1, max_limit + 1):
        expected_cost = round(got * market_price)
        upper = min(max_limit, expected_cost + 5)
        if max_cost is not None:
            upper = min(upper, int(max_cost))
        for cost in range(max(1, expected_cost - 5), upper + 1):
            offer_price = cost / got
            reduce_pct = (market_price - offer_price) / market_price * 100
            if min_reduce <= reduce_pct <= max_reduce:
                candidates.append((reduce_pct, got, cost))

    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[2], row[1]))
    reduce_pct, got, cost = candidates[0]
    return got, cost, reduce_pct


def choose_sell_price_from_market(left: float, right: float, item_count: int) -> tuple[int, int, float] | None:
    market_price = left / right
    candidates: list[tuple[bool, float, int, int]] = []
    max_limit = configured_max_limit(int(item_count * market_price * 2) + 20)
    min_profit = float(config["SELL_PROFIT_MIN"])
    max_profit = float(config["SELL_PROFIT_MAX"])
    max_items = max(1, min(item_count, max_limit))

    for sell_item_count in range(1, max_items + 1):
        expected_chaos = round(sell_item_count * market_price)
        for sell_chaos in range(max(1, expected_chaos - 5), min(max_limit, expected_chaos + 5) + 1):
            offer_price = sell_chaos / sell_item_count
            profit_pct = (offer_price - market_price) / market_price * 100
            if min_profit <= profit_pct <= max_profit:
                candidates.append((sell_item_count != item_count, profit_pct, sell_item_count, sell_chaos))

    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], -row[2], row[3]))
    _partial, profit_pct, sell_item_count, sell_chaos = candidates[0]
    return sell_item_count, sell_chaos, profit_pct


def choose_sell_price(pair_name: str) -> tuple[int, int, float] | None:
    bought = get_pair_price(pair_name)
    if not bought:
        return None

    got = int(bought["got"])
    cost = int(bought["cost"])
    candidates: list[tuple[float, int, int]] = []
    max_limit = configured_max_limit(max(got, cost) * 2 + 20)
    min_profit = float(config["SELL_PROFIT_MIN"])
    max_profit = float(config["SELL_PROFIT_MAX"])

    for sell_item_count in range(1, max_limit + 1):
        base_chaos = sell_item_count * cost / got
        near_chaos = round(base_chaos)
        for sell_chaos in range(max(1, near_chaos - 3), min(max_limit, near_chaos + 3) + 1):
            profit_pct = (sell_chaos - base_chaos) / base_chaos * 100
            if min_profit <= profit_pct <= max_profit:
                candidates.append((profit_pct, sell_item_count, sell_chaos))

    if candidates:
        candidates.sort(key=lambda row: (row[0], row[2], row[1]))
        profit_pct, sell_item_count, sell_chaos = candidates[0]
        return sell_item_count, sell_chaos, profit_pct

    return got, cost + 1, ((cost + 1) - cost) / cost * 100


def wait_selector_open(timeout: float = 1.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if selector_is_open():
            return True
        time.sleep(0.15)
    return False


def open_currency_selector(side: str, layout: TradeLayout) -> TradeLayout | None:
    if not layout.need:
        log("选择器槽位锚点缺失:", side)
        save_debug_snapshot(f"selector_slot_missing_{side}")
        return None

    if side == "left":
        primary = (
            layout.need.x,
            layout.need.y + int(config.get("NEED_OFFSET_Y", 31)),
        )
    else:
        primary = (
            layout.need.x + int(config.get("NEED_RIGHT_OFFSET_X", 369)),
            layout.need.y + int(config.get("NEED_OFFSET_Y", 31)),
        )

    click_xy(*primary)
    time.sleep(0.35)
    opened_layout = current_trade_layout()
    if wait_selector_open() or (opened_layout.window is not None and not opened_layout.is_open):
        return opened_layout

    # 如果点击文字区域没打开选择器，再尝试点击图标/槽位区域。
    layout = current_trade_layout()
    slot = layout.left_slot() if side == "left" else layout.right_slot()
    if slot:
        click_xy(slot[0] + 25, slot[1])
        time.sleep(0.35)
        opened_layout = current_trade_layout()
        if wait_selector_open() or (opened_layout.window is not None and not opened_layout.is_open):
            return opened_layout

    log("通货选择器未打开:", side, "主点击=", primary, "槽位=", slot)
    save_debug_snapshot(f"selector_open_failed_{side}")
    return None


def ocr_find_text_center_region(
    patterns: list[str],
    window_x: int,
    window_y: int,
    window_w: int,
    window_h: int,
) -> tuple[int, int, str] | None:
    screen, origin, win = capture_target_screen()
    if not win:
        return None
    centers = ocr_text_centers_region(screen, origin, window_x, window_y, window_w, window_h)
    for text, x, y in centers:
        if any(pattern in text for pattern in patterns):
            return x, y, text
    return None


def find_trade_npc() -> tuple[int, int, str] | None:
    # Do not scan the whole screen: the minimap contains NPC names and can be
    # incorrectly clicked. This region covers the world/NPC label area only.
    return ocr_find_text_center_region(
        ["安洁", "安潔", "安婕"],
        int(config.get("NPC_OCR_WINDOW_X", 0)),
        int(config.get("NPC_OCR_WINDOW_Y", 260)),
        int(config.get("NPC_OCR_WINDOW_W", 620)),
        int(config.get("NPC_OCR_WINDOW_H", 520)),
    )


def find_npc_trade_entry() -> tuple[int, int, str] | None:
    return ocr_find_text_center_region(
        ["通货交易", "通貨交易", "通貨交換", "通貨兌換"],
        int(config.get("NPC_MENU_OCR_WINDOW_X", 250)),
        int(config.get("NPC_MENU_OCR_WINDOW_Y", 360)),
        int(config.get("NPC_MENU_OCR_WINDOW_W", 520)),
        int(config.get("NPC_MENU_OCR_WINDOW_H", 360)),
    )


def click_currency_search_result(name: str, side: str, layout: TradeLayout) -> bool:
    template = template_path(f"{name}.png")
    template_exists = template.exists()
    if not template_exists:
        log("通货模板缺失，将使用 OCR 兜底:", name)

    # 选择器会保留上次滚动/分类，先点“全部”并搜索，保证目标尽量在可见区域。
    search_xy = layout.selector_search_box()
    if not search_xy:
        log("选择器搜索框锚点缺失:", side, name)
        save_debug_snapshot(f"selector_search_anchor_missing_{side}_{name}")
        return False
    click_selector_all_tab()
    click_xy(*search_xy)
    replace_text(name)
    time.sleep(float(config.get("SELECT_SEARCH_RESULT_DELAY", 0.25)))

    if bool(config.get("SELECT_FIRST_RESULT_AFTER_SEARCH", True)):
        win = find_window()
        if win:
            first_x = win.left + int(config.get("SELECT_FIRST_RESULT_WINDOW_X", 178))
            first_y = win.top + int(config.get("SELECT_FIRST_RESULT_WINDOW_Y", 209))
            click_xy(first_x, first_y)
            log("已点击筛选后的第一个选择结果:", side, name, (first_x, first_y))
            time.sleep(float(config.get("SELECT_AFTER_CLICK_DELAY", 0.25)))
            if not selector_is_open():
                return confirm_selected_currency_or_snapshot(name, side, "first_result")
            log("第一个选择结果未确认命中，继续模板/OCR兜底:", side, name)

    match = None
    for _attempt in range(6):
        layout = current_trade_layout()
        popup = layout.selector_crop()
        if not popup:
            time.sleep(0.2)
            continue
        crop, crop_origin = popup
        if template_exists:
            match = find_image(
                template,
                screen=crop,
                screen_origin=crop_origin,
                threshold=0.72,
                quiet=True,
            )
            if match:
                break
        time.sleep(0.2)
    if not match:
        ocr_match = ocr_match_currency_name(name)
        if ocr_match:
            click_xy(ocr_match[0], ocr_match[1])
            log("已通过 OCR 点击通货:", side, name, (ocr_match[0], ocr_match[1]), repr(ocr_match[2]))
        elif bool(config.get("SELECT_FIRST_RESULT_AFTER_SEARCH", True)):
            win = find_window()
            if not win:
                log("选择器弹窗里未找到通货:", side, name)
                save_debug_snapshot(f"currency_not_found_{side}_{name}")
                return False
            first_x = win.left + int(config.get("SELECT_FIRST_RESULT_WINDOW_X", 178))
            first_y = win.top + int(config.get("SELECT_FIRST_RESULT_WINDOW_Y", 209))
            click_xy(first_x, first_y)
            log("已点击筛选后的第一个选择结果:", side, name, (first_x, first_y))
        else:
            log("选择器弹窗里未找到通货:", side, name)
            save_debug_snapshot(f"currency_not_found_{side}_{name}")
            return False
        time.sleep(0.35)
        if selector_is_open():
            win = find_window()
            if win:
                retry_x = win.left + int(config.get("SELECT_FIRST_RESULT_RETRY_WINDOW_X", 178))
                retry_y = win.top + int(config.get("SELECT_FIRST_RESULT_WINDOW_Y", 209))
                click_xy(retry_x, retry_y)
                log("选择器仍打开，重试点击第一个结果图标:", side, name, (retry_x, retry_y))
                time.sleep(0.35)
        return confirm_selected_currency_or_snapshot(name, side, "ocr_or_first_fallback")

    click_xy(match.x, match.y)
    log("已点击弹窗中的通货:", side, name, (match.x, match.y), f"匹配={match.score:.3f}")
    time.sleep(0.35)
    if selector_is_open():
        click_xy(match.x, match.y)
        log("选择器仍打开，重试模板匹配点击:", side, name, (match.x, match.y))
        time.sleep(0.35)
    return confirm_selected_currency_or_snapshot(name, side, "template_match")


def select_currency(left_name: str, right_name: str) -> bool:
    close_order_confirmation_if_visible()
    close_currency_selector_if_open()
    move_mouse_to_trade_safe_area()
    layout = current_trade_layout()
    if not layout.is_open:
        if not ensure_trade_ui_open():
            log("选择通货前交易界面未打开")
            return False
        layout = current_trade_layout()
    if not selected_currency_matches(left_name, "left", layout):
        layout = open_currency_selector("left", layout)
        if not layout:
            return False
        if not click_currency_search_result(left_name, "left", layout):
            return False

    layout = current_trade_layout()
    if selected_currency_matches(right_name, "right", layout):
        return True
    layout = open_currency_selector("right", layout)
    if not layout:
        return False
    return click_currency_search_result(right_name, "right", layout)


def select_currency_side(name: str, side: str) -> bool:
    close_order_confirmation_if_visible()
    close_currency_selector_if_open()
    move_mouse_to_trade_safe_area()
    layout = current_trade_layout()
    if not layout.is_open:
        if not ensure_trade_ui_open():
            log("选择通货前交易界面未打开")
            return False
        layout = current_trade_layout()
    if selected_currency_matches(name, side, layout):
        log("交易栏已有目标通货:", side, name)
        return True
    layout = open_currency_selector(side, layout)
    if not layout:
        return False
    return click_currency_search_result(name, side, layout)


def available_ingame_scan_pairs() -> list[str]:
    base = str(config["BASE_CURRENCY"])
    chaos_flip = [
        str(pair["name"])
        for pair in config.get("CHAOS_FLIP_PAIRS", [])
        if isinstance(pair, dict) and pair.get("name")
    ]
    ordered: list[str] = []
    for name in chaos_flip:
        if name not in ordered:
            ordered.append(name)
    return ordered


def competition_price_per_item(left_name: str, right_name: str, target_side: str) -> dict[str, Any]:
    if not select_currency(left_name, right_name):
        return {
            "ok": False,
            "left": left_name,
            "right": right_name,
            "target_side": target_side,
            "error": "select_currency_failed",
        }
    time.sleep(0.4)
    item_name = left_name if target_side == "left" else right_name
    pair_cfg = chaos_flip_pair_config(item_name) or {}
    expected_key = "buy_c" if target_side == "left" else "sell_c"
    expected_price = pair_cfg.get(expected_key)
    expected_price = float(expected_price) if expected_price is not None else None
    ratio = read_first_competition_ratio(expected_price=expected_price, target_side=target_side)
    source = "competition"
    if not ratio:
        raw_market = read_market_text()
        ratio = parse_ratio(raw_market)
        source = "market_ratio"
        if not ratio:
            return {
                "ok": False,
                "left": left_name,
                "right": right_name,
                "target_side": target_side,
                "error": "competition_ocr_failed",
                "market_text": raw_market,
            }

    left, right = ratio
    if target_side == "left":
        price = right / left
    elif target_side == "right":
        price = left / right
    else:
        raise ValueError(f"unknown target_side: {target_side}")

    return {
        "ok": True,
        "left": left_name,
        "right": right_name,
        "target_side": target_side,
        "ratio": [left, right],
        "price_per_item": price,
        "source": source,
    }


def chaos_flip_pair_config(name: str) -> dict[str, Any] | None:
    target = normalize_currency_text(name)
    for pair in config.get("CHAOS_FLIP_PAIRS", []):
        if isinstance(pair, dict) and normalize_currency_text(str(pair.get("name"))) == target:
            return pair
    return None



def order_board_default_pairs() -> list[str]:
    return [
        "\u5de6\u65cb\u62ba\u9664\u4e4b\u5146",
        "\u53f3\u65cb\u62ba\u9664\u4e4b\u5146",
        "\u5de6\u65cb\u5ee2\u6b62\u4e4b\u5146",
        "\u53f3\u65cb\u5ee2\u6b62\u4e4b\u5146",
        "\u5149\u660e\u4e4b\u5146",
    ]


def order_board_pairs() -> list[str]:
    def collect(values: Any) -> list[str]:
        result: list[str] = []
        for item in values or []:
            name = str(item.get("name") if isinstance(item, dict) else item)
            if name and name not in result:
                result.append(name)
        return result

    configured = collect(config.get("ORDER_BOARD_PAIRS"))
    return configured or collect(order_board_default_pairs())

def load_order_board() -> dict[str, Any]:
    if not ORDER_BOARD_FILE.exists():
        return {"orders": {}}
    try:
        data = json.loads(ORDER_BOARD_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log("读取订单板失败:", exc)
        return {"orders": {}}
    if not isinstance(data, dict):
        return {"orders": {}}
    orders = data.get("orders")
    if not isinstance(orders, dict):
        data["orders"] = {}
    return data


def save_order_board(data: dict[str, Any]) -> None:
    ORDER_BOARD_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


LOG_FIELD_NAMES = {
    "item": "商品",
    "item_qty": "数量",
    "chaos_cost": "混沌成本",
    "cost": "成本",
    "chaos": "混沌",
    "chaos_receive": "预计收回混沌",
    "chaos_received": "收回混沌",
    "chaos_spent": "花费混沌",
    "chaos_profit": "混沌利润",
    "competition_price": "竞争单价",
    "buy_competition_price": "买入竞争单价",
    "balance": "余额",
    "totals": "汇总",
    "initial_chaos": "初始混沌",
    "current_chaos_available": "当前可用混沌",
    "realized_chaos_spent": "已结算花费混沌",
    "realized_chaos_received": "已结算收回混沌",
    "realized_chaos_profit": "已结算混沌利润",
    "active_buy_order_chaos_locked": "买单锁定混沌",
    "active_holding_cost": "持货成本",
    "active_sell_order_cost": "卖单成本",
    "active_sell_order_expected_chaos": "卖单预计收回混沌",
    "estimated_profit_if_active_sells_complete": "卖单完成后预计利润",
    "estimated_current_chaos_if_active_sells_complete": "卖单完成后预计混沌",
    "gold_used": "已用金币",
    "initial_gold": "初始金币",
    "current_gold": "当前金币",
    "sell_orders": "卖单数量",
    "active_orders": "活跃订单",
    "reason": "原因",
    "order": "订单",
    "orders": "订单",
    "state": "状态",
    "source": "来源",
    "ready": "已就绪",
    "error": "错误",
    "base_currency": "基准通货",
    "started_at": "开始时间",
    "created_at": "创建时间",
    "bought_at": "买入时间",
    "updated_at": "更新时间",
    "detected_at": "检测时间",
    "initial_gold_raw": "初始金币OCR",
    "initial_chaos_auto": "自动读取初始混沌",
    "initial_chaos_raw": "初始混沌OCR",
    "initial_chaos_source": "初始混沌来源",
    "initial_inventory_snapshot": "初始背包截图",
    "initial_inventory_ocr": "初始背包OCR",
}


def localize_log_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {LOG_FIELD_NAMES.get(str(k), str(k)): localize_log_data(v) for k, v in value.items()}
    if isinstance(value, list):
        return [localize_log_data(v) for v in value]
    return value


def append_ledger(event: str, **fields: Any) -> None:
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    TRADE_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRADE_LEDGER_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    event_names = {
        "session_start": "交易会话初始化",
        "session_start_failed": "交易会话初始化失败",
        "buy_order_placed": "买单已挂出",
        "sell_order_placed": "卖单已挂出",
        "buy_order_completed": "买单已成交",
        "sell_order_stocked": "卖单已入库",
        "trade_realized": "交易收益已结算",
        "sell_order_completed_unknown_profit": "卖单成交但收益未知",
        "startup_sell_order_detected": "启动检查发现卖单",
        "startup_inventory_holding_detected": "启动检查发现背包持货",
        "startup_sell_order_placed_from_inventory": "启动检查已从背包挂卖单",
        "startup_reconcile_complete": "启动检查完成",
    }
    log("账本:", event_names.get(event, event), localize_log_data(fields))


def normalize_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def ensure_ledger_session(data: dict[str, Any]) -> dict[str, Any]:
    session = data.get("session")
    if isinstance(session, dict) and session.get("started_at") and session.get("ready"):
        return session
    if not ensure_inventory_open():
        session = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "ready": False,
            "error": "inventory_not_open",
            "base_currency": str(config.get("BASE_CURRENCY", "混沌石")),
        }
        data["session"] = session
        save_order_board(data)
        append_ledger("session_start_failed", **session)
        return session
    snapshot = save_inventory_session_snapshot("initial_inventory")
    gold, raw_gold = read_gold_amount()
    base_currency = str(config.get("BASE_CURRENCY", "混沌石"))
    chaos_amount, raw_chaos = read_inventory_currency_amount(base_currency)
    fallback_chaos = config.get("INITIAL_CHAOS", config.get("AUTO_TRADE_CHAOS_BUDGET", None))
    allow_fallback = bool(config.get("ALLOW_INITIAL_CHAOS_FALLBACK", False))
    initial_chaos = chaos_amount if chaos_amount is not None else (fallback_chaos if allow_fallback else None)
    ready = gold is not None and initial_chaos is not None
    session = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ready": ready,
        "initial_gold": gold,
        "initial_gold_raw": raw_gold,
        "initial_chaos_auto": chaos_amount,
        "initial_chaos_raw": raw_chaos,
        "initial_chaos_source": "inventory_ocr" if chaos_amount is not None else ("config_fallback" if initial_chaos is not None else "missing"),
        "initial_chaos": initial_chaos,
        "base_currency": base_currency,
        "initial_inventory_snapshot": snapshot.get("image"),
        "initial_inventory_ocr": snapshot.get("ocr"),
    }
    data["session"] = session
    data["last_balance"] = {
        **order_board_balance_summary(data, refresh_gold=False),
        "current_gold": gold,
        "gold_used": 0 if isinstance(gold, int) else None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_order_board(data)
    append_ledger("session_start", **session)
    return session


def order_board_balance_summary(data: dict[str, Any], refresh_gold: bool = False) -> dict[str, int | None]:
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    initial_gold = session.get("initial_gold") if isinstance(session, dict) else None
    initial_chaos = normalize_int(session.get("initial_chaos") if isinstance(session, dict) else None)
    current_gold = None
    if refresh_gold:
        current_gold, _raw_gold = open_inventory_and_read_gold()

    realized = data.get("realized")
    realized_spent = int(realized.get("chaos_spent") or 0) if isinstance(realized, dict) else 0
    realized_received = int(realized.get("chaos_received") or 0) if isinstance(realized, dict) else 0
    realized_profit = realized_received - realized_spent

    active_buy_cost = 0
    active_holding_cost = 0
    active_sell_cost = 0
    active_sell_expected = 0
    for order in data.get("orders", {}).values():
        if not isinstance(order, dict):
            continue
        state = order.get("state")
        cost = int(order.get("cost") or 0)
        if state == "buy_order":
            active_buy_cost += cost
        elif state == "holding":
            active_holding_cost += cost
        elif state == "sell_order":
            active_sell_cost += cost
            active_sell_expected += int(order.get("chaos") or 0)

    active_cost = active_buy_cost + active_holding_cost + active_sell_cost
    estimated_profit = realized_profit + active_sell_expected - active_cost
    current_chaos_available = None
    if initial_chaos is not None:
        current_chaos_available = (
            initial_chaos
            - realized_spent
            + realized_received
            - active_buy_cost
            - active_holding_cost
            - active_sell_cost
        )
    return {
        "initial_chaos": initial_chaos,
        "current_chaos_available": current_chaos_available,
        "realized_chaos_spent": realized_spent,
        "realized_chaos_received": realized_received,
        "realized_chaos_profit": realized_profit,
        "active_buy_order_chaos_locked": active_buy_cost,
        "active_holding_cost": active_holding_cost,
        "active_sell_order_cost": active_sell_cost,
        "active_sell_order_expected_chaos": active_sell_expected,
        "estimated_profit_if_active_sells_complete": estimated_profit,
        "estimated_current_chaos_if_active_sells_complete": (
            initial_chaos + estimated_profit if initial_chaos is not None else None
        ),
        "gold_used": (int(initial_gold) - current_gold) if isinstance(initial_gold, int) and current_gold is not None else None,
        "initial_gold": initial_gold if isinstance(initial_gold, int) else None,
        "current_gold": current_gold,
    }


def ledger_totals(data: dict[str, Any]) -> dict[str, int | None]:
    return order_board_balance_summary(data, refresh_gold=True)


def order_board_available_chaos(data: dict[str, Any]) -> int | None:
    return order_board_balance_summary(data, refresh_gold=False).get("current_chaos_available")


def refresh_order_board_gold(data: dict[str, Any]) -> dict[str, int | None]:
    totals = order_board_balance_summary(data, refresh_gold=True)
    data["last_balance"] = {
        **totals,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_order_board(data)
    return totals


def update_order_board_balance(data: dict[str, Any]) -> dict[str, int | None]:
    totals = order_board_balance_summary(data, refresh_gold=False)
    previous = data.get("last_balance") if isinstance(data.get("last_balance"), dict) else {}
    data["last_balance"] = {
        **previous,
        **totals,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_order_board(data)
    return totals


def add_realized_trade(data: dict[str, Any], name: str, spent: int, received: int) -> None:
    realized = data.setdefault("realized", {})
    realized["chaos_spent"] = int(realized.get("chaos_spent") or 0) + int(spent)
    realized["chaos_received"] = int(realized.get("chaos_received") or 0) + int(received)
    realized["trade_count"] = int(realized.get("trade_count") or 0) + 1
    realized["updated_at"] = datetime.now().isoformat(timespec="seconds")
    totals = refresh_order_board_gold(data)
    append_ledger(
        "trade_realized",
        item=name,
        chaos_spent=int(spent),
        chaos_received=int(received),
        chaos_profit=int(received) - int(spent),
        totals=totals,
    )


def active_board_order_count(data: dict[str, Any]) -> int:
    orders = data.get("orders", {})
    return sum(1 for order in orders.values() if isinstance(order, dict) and order.get("state") in {"buy_order", "sell_order"})


def active_board_buy_cost(data: dict[str, Any]) -> int:
    total = 0
    for order in data.get("orders", {}).values():
        if isinstance(order, dict) and order.get("state") == "buy_order":
            total += int(order.get("cost") or 0)
    return total


def inventory_grid_origin() -> tuple[int, int, str] | None:
    win = find_window()
    if not win:
        return None
    if bool(config.get("INVENTORY_GRID_USE_EDGE_ANCHOR", True)):
        start_x = win.right - int(config.get("INVENTORY_GRID_RIGHT_MARGIN", 580))
        start_y = win.bottom - int(config.get("INVENTORY_GRID_BOTTOM_MARGIN", 484))
        return start_x, start_y, "window_edge"
    start_x = win.left + int(config.get("INVENTORY_GRID_X", 716))
    start_y = win.top + int(config.get("INVENTORY_GRID_Y", 515))
    return start_x, start_y, "window_offset"


def inventory_grid_visual_score(
    screen: np.ndarray | None = None,
    screen_origin: tuple[int, int] = (0, 0),
) -> float:
    if screen is None:
        screen, screen_origin, _win = capture_target_screen()
    grid = inventory_grid_origin()
    if not grid:
        return 0.0

    start_x, start_y, _method = grid
    rel_x = start_x - screen_origin[0]
    rel_y = start_y - screen_origin[1]
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    w = cell_w * cols
    h = cell_h * rows

    x1 = max(0, rel_x)
    y1 = max(0, rel_y)
    x2 = min(screen.shape[1], rel_x + w)
    y2 = min(screen.shape[0], rel_y + h)
    if x2 <= x1 or y2 <= y1:
        return 0.0

    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 110)

    masks: list[np.ndarray] = []
    for col in range(cols + 1):
        x = int(round(col * cell_w - max(0, -rel_x)))
        if 0 <= x < edges.shape[1]:
            masks.append(edges[:, max(0, x - 1) : min(edges.shape[1], x + 2)])
    for row in range(rows + 1):
        y = int(round(row * cell_h - max(0, -rel_y)))
        if 0 <= y < edges.shape[0]:
            masks.append(edges[max(0, y - 1) : min(edges.shape[0], y + 2), :])
    if not masks:
        return 0.0

    edge_pixels = sum(int(np.count_nonzero(mask)) for mask in masks)
    total_pixels = sum(int(mask.size) for mask in masks)
    return edge_pixels / max(1, total_pixels)


def inventory_slot_centers() -> list[tuple[int, int]]:
    origin = inventory_grid_origin()
    if not origin:
        return []
    start_x, start_y, _method = origin
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    return [
        (start_x + col * cell_w + cell_w // 2, start_y + row * cell_h + cell_h // 2)
        for row in range(rows)
        for col in range(cols)
    ]


def find_inventory_item_icon(name: str) -> Match | None:
    if not ensure_inventory_open():
        log("查找背包物品失败，背包未打开:", name)
        return None
    template = template_path(f"{name}.png")
    if not template.exists():
        log("查找背包物品失败，缺少物品模板:", name, template)
        return None
    grid = inventory_grid_origin()
    if not grid:
        log("查找背包物品失败，背包格子坐标未知:", name)
        return None
    screen, origin, _win = capture_target_screen()
    start_x, start_y, _method = grid
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    pad = int(config.get("INVENTORY_GRID_ICON_PAD", 6))
    rel_x = start_x - origin[0] - pad
    rel_y = start_y - origin[1] - pad
    w = cols * cell_w + pad * 2
    h = rows * cell_h + pad * 2
    x1 = max(0, rel_x)
    y1 = max(0, rel_y)
    x2 = min(screen.shape[1], rel_x + w)
    y2 = min(screen.shape[0], rel_y + h)
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    match = find_image(
        template,
        screen=crop,
        screen_origin=(origin[0] + x1, origin[1] + y1),
        threshold=float(config.get("INVENTORY_ITEM_ICON_THRESHOLD", 0.72)),
        quiet=True,
    )
    if match:
        log("背包找到物品图标:", name, match.center, f"匹配={match.score:.3f}")
    else:
        log("背包未找到物品图标:", name)
    return match


def put_inventory_item_to_sell_slot(name: str) -> bool:
    match = find_inventory_item_icon(name)
    if not match:
        return False
    if config.get("DRY_RUN"):
        log("[DRY_RUN] Ctrl+左键放入出售栏:", name, match.center)
        return True
    backend = get_input()
    backend.mouse_move(match.x, match.y, duration_ms=80)
    try:
        backend.key_down("ctrl")
        backend.mouse_click(button="left", hold_ms=45)
    finally:
        backend.key_up("ctrl")
    time.sleep(float(config.get("INVENTORY_TO_SELL_SLOT_DELAY", 0.35)))
    log("已从背包 Ctrl+左键放入出售栏:", name, match.center)
    return True


def return_sell_slot_item_to_inventory(name: str) -> bool:
    layout = current_trade_layout()
    slot = layout.right_slot()
    if not slot:
        log("回收出售栏物品失败，未找到右侧商品槽:", name)
        return False
    if not selected_currency_matches(name, "right", layout):
        log("回收出售栏物品跳过，右侧不是目标商品:", name)
        return False
    if config.get("DRY_RUN"):
        log("[DRY_RUN] Ctrl+左键回收出售栏物品:", name, slot)
        return True
    backend = get_input()
    backend.mouse_move(slot[0], slot[1], duration_ms=80)
    try:
        backend.key_down("ctrl")
        backend.mouse_click(button="left", hold_ms=45)
    finally:
        backend.key_up("ctrl")
    time.sleep(float(config.get("INVENTORY_TO_SELL_SLOT_DELAY", 0.35)))
    log("已从出售栏 Ctrl+左键回收到背包:", name, slot)
    return True


def inventory_has_item(name: str) -> bool:
    if game_unavailable_reason():
        return False
    icon_count, icon_raw = inventory_item_icon_count(name)
    if icon_count > 0:
        return True
    if icon_raw != "backpack_icon_template_missing":
        log("背包未找到物品图标:", name, icon_raw)
    if not ensure_inventory_open():
        return False
    target = normalize_currency_text(name)
    screen, origin, _win = capture_target_screen()
    grid = inventory_grid_origin()
    if not grid:
        return False
    start_x, start_y, _method = grid
    cols = int(config.get("INVENTORY_GRID_COLS", 12))
    rows = int(config.get("INVENTORY_GRID_ROWS", 5))
    cell_w = int(config.get("INVENTORY_GRID_CELL_W", 48))
    cell_h = int(config.get("INVENTORY_GRID_CELL_H", 48))
    pad = int(config.get("INVENTORY_GRID_OCR_PAD", 8))
    x1 = max(0, start_x - origin[0] - pad)
    y1 = max(0, start_y - origin[1] - pad)
    x2 = min(screen.shape[1], start_x - origin[0] + cols * cell_w + pad)
    y2 = min(screen.shape[0], start_y - origin[1] + rows * cell_h + pad)
    crop = screen[y1:y2, x1:x2]
    if crop.size == 0:
        return False
    for text, _tx, _ty in ocr_text_centers(crop, (origin[0] + x1, origin[1] + y1)):
        normalized = normalize_currency_text(text)
        if target in normalized or normalized.startswith(target):
            log("背包格子 OCR 检测到物品:", name, "OCR=", repr(text))
            return True
    if bool(config.get("DEBUG_SNAPSHOT_INVENTORY_MISS", False)):
        save_debug_snapshot(f"inventory_miss_{name}")
    return False


def find_visible_currency_text(name: str) -> Match | None:
    target = normalize_currency_text(name)
    screen, origin, _win = capture_target_screen()
    candidates: list[tuple[int, int, str]] = []
    for text, x, y in ocr_text_centers(screen, origin):
        normalized = normalize_currency_text(text)
        if target in normalized or normalized.startswith(target):
            candidates.append((x, y, text))
    if not candidates:
        return None
    # Prefer text in the exchange/order panel over inventory tooltip/search box text.
    candidates.sort(key=lambda row: (row[1] > int(config.get("SELECT_SEARCH_SCREEN_Y", 850)), row[1]))
    x, y, text = candidates[0]
    log("可见通货文字锚点:", name, (x, y), repr(text))
    return Match(x=x, y=y, score=1.0)


def count_visible_item_cards(name: str) -> int:
    template = template_path(f"{name}.png")
    if not template.exists():
        return 0
    screen, origin, _win = capture_target_screen()
    image = cv2.imdecode(np.fromfile(str(template), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return 0
    result = cv2.matchTemplate(screen, image, cv2.TM_CCOEFF_NORMED)
    threshold = float(config.get("ORDER_CARD_MATCH_THRESHOLD", 0.68))
    ys, xs = np.where(result >= threshold)
    if len(xs) == 0:
        return 0
    points = sorted((int(x), int(y), float(result[y, x])) for x, y in zip(xs, ys))
    h, w = image.shape[:2]
    selected: list[tuple[int, int, float]] = []
    for x, y, score in points:
        if any(abs(x - sx) < w // 2 and abs(y - sy) < h // 2 for sx, sy, _s in selected):
            continue
        selected.append((x, y, score))
    return len(selected)


def wait_for_item_card_count_increase(name: str, before: int, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        count = count_visible_item_cards(name)
        if count > before:
            log("物品订单卡数量增加:", name, before, "->", count)
            return True
        time.sleep(0.3)
    log("物品订单卡数量未增加:", name, "之前=", before, "之后=", count_visible_item_cards(name))
    return False


def visible_sell_order_count(name: str) -> int | None:
    base = str(config["BASE_CURRENCY"])
    if not select_currency(base, name):
        log("启动卖单扫描选择交易对失败:", name)
        close_currency_selector_if_open()
        ensure_trade_ui_open()
        return None
    time.sleep(0.25)
    count = count_visible_item_cards(name)
    sell_count = max(0, count - 1)
    log("启动卖单扫描:", name, "可见卡片=", count, "卖单=", sell_count)
    return sell_count


def competition_price_for_current_pair(item_name: str, target_side: str) -> float | None:
    pair_cfg = chaos_flip_pair_config(item_name) or {}
    expected_key = "buy_c" if target_side == "left" else "sell_c"
    expected_price = pair_cfg.get(expected_key)
    expected = float(expected_price) if expected_price is not None else None
    ratio = read_first_competition_ratio(expected_price=expected, target_side=target_side)
    if not ratio:
        log("竞争价格读取失败:", item_name, target_side)
        return None
    left, right = ratio
    if target_side == "left":
        return right / left
    return left / right


def place_board_buy_order(name: str, data: dict[str, Any]) -> bool:
    base = str(config["BASE_CURRENCY"])
    order_size = int(config.get("ORDER_BOARD_ORDER_SIZE", 1))
    if not ensure_trade_and_inventory_open():
        log("跳过买入，交易界面和背包必须都打开:", name)
        return False
    session = ensure_ledger_session(data)
    if not session.get("ready"):
        log("跳过买入，初始混沌/金币尚未初始化:", session)
        return False
    if not select_currency(name, base):
        return False
    price = competition_price_for_current_pair(name, target_side="left")
    if price is None:
        return False
    cost = max(1, int(math.ceil(price * order_size)))
    available_chaos = order_board_available_chaos(data)
    if available_chaos is None:
        log("跳过买入，当前混沌余额未知:", name)
        return False
    if cost > available_chaos:
        log("跳过买入，混沌石不足:", name, "需要=", cost, "可用=", available_chaos)
        return False
    log("订单板挂买单:", name, order_size, "个 =", cost, base, "竞争单价=", f"{price:.4f}")
    if not place_order(order_size, cost, verify_item_name=name):
        return False
    data.setdefault("orders", {})[name] = {
        "state": "buy_order",
        "item_qty": order_size,
        "cost": cost,
        "competition_price": price,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    update_order_board_balance(data)
    append_ledger(
        "buy_order_placed",
        item=name,
        item_qty=order_size,
        chaos_cost=cost,
        competition_price=price,
        balance=order_board_balance_summary(data, refresh_gold=False),
    )
    return True


def place_board_sell_order(name: str, data: dict[str, Any]) -> bool:
    base = str(config["BASE_CURRENCY"])
    order_size = int(config.get("ORDER_BOARD_ORDER_SIZE", 1))
    previous = data.get("orders", {}).get(name)
    cost = int(previous.get("cost") or 0) if isinstance(previous, dict) else 0
    if not ensure_trade_and_inventory_open():
        log("跳过卖出，交易界面和背包必须都打开:", name)
        return False
    if not select_currency_side(base, "left"):
        return False
    if not put_inventory_item_to_sell_slot(name):
        log("跳过卖出，无法从背包放入出售商品:", name)
        return False
    price = competition_price_for_current_pair(name, target_side="right")
    if price is None:
        return_sell_slot_item_to_inventory(name)
        return False
    chaos = max(1, int(math.floor(price * order_size)))
    log("订单板挂卖单:", name, order_size, "个 ->", chaos, base, "竞争单价=", f"{price:.4f}")
    if not place_order(chaos, order_size, verify_item_name=name, verify_item_side="right"):
        return_sell_slot_item_to_inventory(name)
        return False
    data.setdefault("orders", {})[name] = {
        "state": "sell_order",
        "item_qty": order_size,
        "chaos": chaos,
        "cost": cost,
        "competition_price": price,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    update_order_board_balance(data)
    append_ledger(
        "sell_order_placed",
        item=name,
        item_qty=order_size,
        chaos_receive=chaos,
        competition_price=price,
        balance=order_board_balance_summary(data, refresh_gold=False),
    )
    return True


def check_board_order_done(name: str, data: dict[str, Any]) -> bool:
    order = data.get("orders", {}).get(name)
    if not isinstance(order, dict):
        return False
    state = order.get("state")
    base = str(config["BASE_CURRENCY"])
    if state == "buy_order":
        if not select_currency(name, base):
            return False
        time.sleep(0.3)
        done = find_done_anchor()
        if not done:
            if board_order_timed_out(order):
                log("买单超时，取消并重新挂单:", name)
                if cancel_visible_board_order(name, "buy_order"):
                    data.get("orders", {}).pop(name, None)
                    update_order_board_balance(data)
                    place_board_buy_order(name, data)
                    return True
                return False
            return False
        log("买单已成交:", name)
        stock_in(done, "buy")
        time.sleep(0.5)
        data["orders"][name] = {
            "state": "holding",
            "bought_at": datetime.now().isoformat(timespec="seconds"),
            "source": "buy_done_stock_in",
            "item_qty": int(order.get("item_qty") or config.get("ORDER_BOARD_ORDER_SIZE", 1)),
            "cost": int(order.get("cost") or 0),
            "buy_competition_price": order.get("competition_price"),
        }
        update_order_board_balance(data)
        append_ledger(
            "buy_order_completed",
            item=name,
            item_qty=int(order.get("item_qty") or config.get("ORDER_BOARD_ORDER_SIZE", 1)),
            chaos_cost=int(order.get("cost") or 0),
            balance=order_board_balance_summary(data, refresh_gold=False),
        )
        return True
    if state == "sell_order":
        if not select_currency(base, name):
            return False
        time.sleep(0.3)
        done = find_done_anchor()
        if not done:
            if board_order_timed_out(order):
                log("卖单超时，取消并重新挂单:", name)
                if cancel_visible_board_order(name, "sell_order"):
                    data.get("orders", {}).pop(name, None)
                    data.setdefault("orders", {})[name] = {
                        "state": "holding",
                        "source": "sell_timeout_recovered_inventory",
                        "timeout_recovered_at": datetime.now().isoformat(timespec="seconds"),
                        "item_qty": int(order.get("item_qty") or config.get("ORDER_BOARD_ORDER_SIZE", 1)),
                        "cost": int(order.get("cost") or 0),
                    }
                    update_order_board_balance(data)
                    place_board_sell_order(name, data)
                    return True
                return False
            return False
        log("卖单已成交:", name)
        stock_in(done, "sell")
        spent = int(order.get("cost") or 0)
        received = int(order.get("chaos") or 0)
        data.get("orders", {}).pop(name, None)
        update_order_board_balance(data)
        if spent > 0 and received > 0:
            add_realized_trade(data, name, spent=spent, received=received)
        else:
            append_ledger(
                "sell_order_completed_unknown_profit",
                item=name,
                order=order,
                reason="missing_cost_or_expected_chaos",
            )
        save_order_board(data)
        append_ledger("sell_order_stocked", item=name, balance=order_board_balance_summary(data, refresh_gold=False))
        return True
    if state == "holding":
        source = str(order.get("source") or "")
        if source not in {"buy_done_stock_in", "startup_inventory_reconcile", "sell_timeout_recovered_inventory"}:
            log("跳过持货卖出，物品尚未确认入背包:", name, "来源=", source)
            return False
        if not ensure_trade_and_inventory_open():
            log("跳过持货卖出，交易界面和背包没有同时打开:", name)
            return False
        log("持货状态，物品已在背包，准备挂卖单:", name)
        if place_board_sell_order(name, data):
            log("持货商品已挂卖单:", name)
            return True
    return False


def reconcile_order_board_startup(data: dict[str, Any], names: list[str]) -> bool:
    if not bool(config.get("ORDER_BOARD_STARTUP_RECONCILE", True)):
        return False
    if data.get("startup_reconciled_at"):
        return False

    log("启动检查: 扫描订单页面和背包")
    changed = False
    for name in names:
        orders = data.setdefault("orders", {})
        if name in orders:
            continue

        inventory_found = inventory_has_item(name)
        if not inventory_found:
            log("启动检查: 背包没有该物品，跳过卖单恢复:", name)
            continue

        sell_orders = visible_sell_order_count(name)
        if sell_orders is None:
            continue
        if sell_orders > 0:
            orders[name] = {
                "state": "sell_order",
                "source": "startup_order_page_reconcile",
                "item_qty": int(config.get("ORDER_BOARD_ORDER_SIZE", 1)),
                "cost": 0,
                "chaos": 0,
                "detected_sell_orders": sell_orders,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            update_order_board_balance(data)
            append_ledger("startup_sell_order_detected", item=name, sell_orders=sell_orders)
            changed = True
            continue

        orders[name] = {
            "state": "holding",
            "source": "startup_inventory_reconcile",
            "item_qty": int(config.get("ORDER_BOARD_ORDER_SIZE", 1)),
            "cost": 0,
            "detected_at": datetime.now().isoformat(timespec="seconds"),
        }
        update_order_board_balance(data)
        append_ledger("startup_inventory_holding_detected", item=name)
        if place_board_sell_order(name, data):
            append_ledger("startup_sell_order_placed_from_inventory", item=name)
            changed = True

    data["startup_reconciled_at"] = datetime.now().isoformat(timespec="seconds")
    update_order_board_balance(data)
    append_ledger("startup_reconcile_complete", active_orders=active_board_order_count(data))
    return changed


def startup_reconcile_check() -> int:
    print("Startup reconcile check (dry-run)")
    if not ensure_trade_and_inventory_open():
        print("Ensure trade+inventory: failed")
        return 1
    print("Ensure trade+inventory: ok")
    for name in order_board_pairs():
        icon_count, icon_raw = inventory_item_icon_count(name)
        inventory_found = icon_count > 0
        if icon_raw == "backpack_icon_template_missing":
            inventory_found = inventory_has_item(name)
            icon_raw = "fallback_ocr=" + str(inventory_found)
        sell_orders: int | str | None = "skipped_no_inventory"
        if inventory_found:
            sell_orders = visible_sell_order_count(name)
        print(
            name.encode("unicode_escape").decode(),
            "sell_orders=", sell_orders,
            "inventory_icon_count=", icon_count,
            "inventory_found=", inventory_found,
            "inventory_raw=", icon_raw.encode("unicode_escape", errors="replace").decode(),
        )
    return 0


def board_order_timed_out(order: dict[str, Any]) -> bool:
    created = order.get("created_at")
    if not created:
        return False
    try:
        created_at = datetime.fromisoformat(str(created))
    except ValueError:
        return False
    return (datetime.now() - created_at).total_seconds() >= float(config.get("ORDER_TIMEOUT_SECONDS", 600))


def cancel_visible_board_order(name: str, state: str = "buy_order") -> bool:
    # Select the pair first so the relevant order cards are visible, then click
    # the small cancel button to the right of the visible card anchored by item art.
    base = str(config["BASE_CURRENCY"])
    if state == "sell_order":
        selected = select_currency(base, name)
    else:
        selected = select_currency(name, base)
    if not selected:
        log("取消订单前选择交易对失败:", name, state)
        return False
    time.sleep(0.4)
    match = find_image(f"{name}.png", threshold=0.65, quiet=True)
    if not match:
        match = find_visible_currency_text(name)
    if not match:
        log("未找到取消订单锚点:", name, state)
        save_debug_snapshot(f"cancel_anchor_not_found_{state}_{name}")
        return False
    cancel_x = match.x + int(config.get("ORDER_CARD_CANCEL_OFFSET_X", 216))
    cancel_y = match.y + int(config.get("ORDER_CARD_CANCEL_OFFSET_Y", 0))
    click_xy(cancel_x, cancel_y)
    time.sleep(0.5)
    log("已点击取消订单:", name, (cancel_x, cancel_y))
    return True


def find_done_anchor() -> Match | None:
    done = find_image("done.png", threshold=0.8, quiet=True)
    if done:
        return done
    match = ocr_find_text_center(["完成", "已完成", "交易完成", "取回", "收取"])
    if not match:
        return None
    x, y, text = match
    log("通过 OCR 找到完成订单锚点:", (x, y), repr(text))
    return Match(x=x, y=y, score=1.0)


def handle_order_board_once() -> None:
    check_stop()
    win = find_window()
    if not win:
        log("订单板跳过: 未找到游戏窗口")
        if input_backend is not None:
            input_backend.release_all()
        return
    check_stop()
    layout = current_trade_layout()
    if layout.window:
        log("已绑定窗口:", layout.window.title, f"{layout.window.width}x{layout.window.height}", f"({layout.window.left},{layout.window.top})")
    close_order_confirmation_if_visible()
    if not ensure_trade_and_inventory_open():
        log("交易界面或背包未就绪，等待")
        return

    data = load_order_board()
    session = ensure_ledger_session(data)
    if not session.get("ready"):
        log("订单板跳过: 初始背包余额尚未就绪:", session)
        return
    names = order_board_pairs()
    max_orders = int(config.get("ORDER_BOARD_MAX_ORDERS", 10))
    log("订单板模式:", ", ".join(names), "活跃订单=", active_board_order_count(data))

    if reconcile_order_board_startup(data, names):
        data = load_order_board()

    # 主流程：
    # 1. 先检查交易栏已有订单，完成的全部 Ctrl+右键取回背包。
    # 2. 再把背包里 holding 的商品 Ctrl+左键放入出售栏，按竞争价挂卖单。
    # 3. 最后才用剩余混沌石挂买单；混沌不足时只等待卖单成交。
    for name in names:
        check_stop()
        order = data.get("orders", {}).get(name)
        if not isinstance(order, dict) or order.get("state") not in {"buy_order", "sell_order"}:
            continue
        if check_board_order_done(name, data):
            data = load_order_board()

    for name in names:
        check_stop()
        order = data.get("orders", {}).get(name)
        if not isinstance(order, dict) or order.get("state") != "holding":
            continue
        if active_board_order_count(data) >= max_orders:
            break
        if check_board_order_done(name, data):
            data = load_order_board()
            return
        data = load_order_board()

    for name in names:
        check_stop()
        if active_board_order_count(data) >= max_orders:
            break
        if name in data.get("orders", {}):
            continue
        available_chaos = order_board_available_chaos(data)
        if available_chaos is not None and available_chaos <= 0:
            log("当前没有可用混沌石，等待卖单成交")
            break
        if place_board_buy_order(name, data):
            return
        data = load_order_board()

def add_gold_cost_model(row: dict[str, Any]) -> None:
    buy_price = float(row.get("buy_price") or 0.0)
    sell_price = float(row.get("sell_price") or 0.0)
    spread = float(row.get("spread") or 0.0)
    if buy_price <= 0 or sell_price <= 0 or spread <= 0:
        return

    pair_cfg = chaos_flip_pair_config(str(row["name"])) or {}
    gold_per_chaos = float(pair_cfg.get("gold_per_chaos", config.get("GOLD_FEE_PER_CHAOS", 160)))
    divine_to_chaos = float(config.get("CHAOS_FLIP_DIVINE_TO_CHAOS", 10.14))
    gold_per_round = (buy_price + sell_price) * gold_per_chaos
    profit_d_per_round = spread / divine_to_chaos if divine_to_chaos > 0 else 0.0
    if profit_d_per_round <= 0:
        return

    row.update(
        {
            "gold_per_chaos": gold_per_chaos,
            "gold_per_round": gold_per_round,
            "profit_d_per_round": profit_d_per_round,
            "gold_per_divine_profit": gold_per_round / profit_d_per_round,
        }
    )


def enrich_arbitrage_row(row: dict[str, Any]) -> None:
    buy = row.get("buy") or {}
    sell = row.get("sell") or {}
    if not (buy.get("ok") and sell.get("ok")):
        return

    buy_price = float(buy["price_per_item"])
    sell_price = float(sell["price_per_item"])
    spread = sell_price - buy_price
    spread_pct = spread / buy_price * 100 if buy_price > 0 else 0.0
    row.update(
        {
            "buy_price": buy_price,
            "sell_price": sell_price,
            "spread": spread,
            "spread_pct": spread_pct,
            "profitable": spread > 0,
        }
    )
    add_gold_cost_model(row)


def prices_match(first: dict[str, Any], second: dict[str, Any]) -> tuple[bool, str]:
    tolerance_pct = float(config.get("INGAME_CONFIRM_PRICE_TOLERANCE_PCT", 0.75))
    for key in ("buy_price", "sell_price"):
        a = float(first.get(key) or 0.0)
        b = float(second.get(key) or 0.0)
        if a <= 0 or b <= 0:
            return False, f"{key} missing"
        diff_pct = abs(a - b) / max(a, b) * 100
        if diff_pct > tolerance_pct:
            return False, f"{key} mismatch {a:.4f} vs {b:.4f} ({diff_pct:.2f}%)"
    return True, "ok"


def confirm_ingame_arbitrage_row(row: dict[str, Any]) -> None:
    min_spread_pct = float(config.get("INGAME_SCAN_MIN_SPREAD_PCT", 1.0))
    if not config.get("INGAME_CONFIRM_ENABLED", True):
        row["confirmed"] = True
        row["confirm_reason"] = "disabled"
        return
    if float(row.get("spread") or 0.0) <= 0 or float(row.get("spread_pct") or 0.0) < min_spread_pct:
        row["confirmed"] = False
        row["confirm_reason"] = "not_profitable"
        return

    name = str(row["name"])
    base = str(row["base"])
    log("二次确认价差:", name)
    buy2 = competition_price_per_item(name, base, target_side="left")
    sell2 = competition_price_per_item(base, name, target_side="right")
    confirm_row: dict[str, Any] = {
        "name": name,
        "base": base,
        "buy": buy2,
        "sell": sell2,
    }
    enrich_arbitrage_row(confirm_row)
    row["confirmation"] = confirm_row
    if confirm_row.get("buy_price") is None or confirm_row.get("sell_price") is None:
        row["confirmed"] = False
        row["confirm_reason"] = "confirm_scan_failed"
        log("二次确认失败:", name, row["confirm_reason"], confirm_row)
        return

    ok, reason = prices_match(row, confirm_row)
    row["confirmed"] = ok
    row["confirm_reason"] = reason
    if ok:
        log("二次确认通过:", name)
    else:
        log("二次确认拒绝:", name, reason)


def scan_ingame_arbitrage(limit: int | None = None) -> list[dict[str, Any]]:
    if not find_image("need.png", threshold=0.8, quiet=True):
        raise RuntimeError("未检测到通货交易界面，请先打开通货交易窗口")

    base = str(config["BASE_CURRENCY"])
    limit = int(limit if limit is not None else config.get("INGAME_SCAN_LIMIT", 4))
    names = available_ingame_scan_pairs()
    if limit > 0:
        names = names[:limit]

    results: list[dict[str, Any]] = []
    for name in names:
        log("游戏内价差扫描:", name)
        buy = competition_price_per_item(name, base, target_side="left")
        sell = competition_price_per_item(base, name, target_side="right")
        row: dict[str, Any] = {
            "name": name,
            "base": base,
            "buy": buy,
            "sell": sell,
        }
        enrich_arbitrage_row(row)
        if row.get("buy_price") is not None and row.get("sell_price") is not None:
            confirm_ingame_arbitrage_row(row)
            log(
                "价差:",
                name,
                f"买 {float(row['buy_price']):.4f} {base}",
                f"卖 {float(row['sell_price']):.4f} {base}",
                f"差 {float(row['spread']):.4f} ({float(row['spread_pct']):.2f}%)",
                f"确认={row.get('confirmed')}",
            )
        else:
            log("价差扫描失败:", name, "buy=", buy, "sell=", sell)

        results.append(row)

    results.sort(key=lambda item: float(item.get("spread_pct") or -999999), reverse=True)
    INGAME_SCAN_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log("游戏内价差扫描完成，结果:", INGAME_SCAN_FILE)
    return results


def ingame_arbitrage_scan_report(limit: int | None = None) -> str:
    results = scan_ingame_arbitrage(limit=limit)
    lines = ["游戏内价差扫描结果:"]
    if not results:
        lines.append("无可扫描商品")
        return "\n".join(lines)
    for i, row in enumerate(results, 1):
        if row.get("buy_price") is None or row.get("sell_price") is None:
            lines.append(f"{i}. {row['name']}: 扫描失败 buy={row.get('buy')} sell={row.get('sell')}")
            continue
        gold_text = ""
        if row.get("gold_per_divine_profit") is not None:
            gold_text = (
                f"，每赚 1D 约 {row['gold_per_divine_profit']:.0f} 金币"
                f"（单轮 {row['gold_per_round']:.0f} 金币，利润 {row['profit_d_per_round']:.4f}D）"
            )
        confirm_text = ""
        if row.get("confirmed") is not None:
            confirm_text = "，已二次确认" if row.get("confirmed") else f"，未通过二次确认: {row.get('confirm_reason')}"
        lines.append(
            f"{i}. {row['name']}: 买 {row['buy_price']:.4f} {row['base']} -> 卖 {row['sell_price']:.4f} {row['base']}, "
            f"价差 {row['spread']:.4f} ({row['spread_pct']:.2f}%){gold_text}{confirm_text}"
        )
    lines.append(f"结果文件: {INGAME_SCAN_FILE}")
    return "\n".join(lines)


def profitable_ingame_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    min_spread_pct = float(config.get("INGAME_SCAN_MIN_SPREAD_PCT", 1.0))
    return [
        row
        for row in results
        if float(row.get("spread_pct") or 0.0) >= min_spread_pct and float(row.get("spread") or 0.0) > 0
        and row.get("confirmed") is True
    ]


def best_chaos_flip_candidate(budget_chaos: float | None = None) -> dict[str, Any] | None:
    results = scan_ingame_arbitrage()
    hits = profitable_ingame_rows(results)
    if budget_chaos is not None:
        hits = [row for row in hits if float(row.get("buy_price") or 0.0) <= budget_chaos]
    if not hits:
        return None
    hits.sort(key=lambda row: (float(row.get("gold_per_divine_profit") or 1e18), -float(row.get("spread_pct") or 0.0)))
    return hits[0]


def watch_ingame_arbitrage() -> None:
    interval = float(config.get("INGAME_SCAN_INTERVAL", 60))
    limit = int(config.get("INGAME_SCAN_LIMIT", 4))
    min_spread_pct = float(config.get("INGAME_SCAN_MIN_SPREAD_PCT", 1.0))
    log(
        "游戏内价差自动检查启动:",
        f"interval={interval}s",
        f"limit={limit}",
        f"min_spread={min_spread_pct:.2f}%",
    )
    while True:
        try:
            if not find_image("need.png", threshold=0.8, quiet=True):
                log("未检测到通货交易界面，等待打开...")
                time.sleep(interval)
                continue

            results = scan_ingame_arbitrage(limit=limit)
            hits = profitable_ingame_rows(results)
            if hits:
                log("发现可套利商品:", len(hits))
                for row in hits:
                    log(
                        "套利候选:",
                        row["name"],
                        f"买 {float(row['buy_price']):.4f} {row['base']}",
                        f"卖 {float(row['sell_price']):.4f} {row['base']}",
                        f"价差 {float(row['spread']):.4f} ({float(row['spread_pct']):.2f}%)",
                    )
            else:
                log(f"未发现超过 {min_spread_pct:.2f}% 的游戏内价差")
        except KeyboardInterrupt:
            raise
        except Exception:
            log("游戏内价差自动检查异常:", traceback.format_exc())
            if input_backend is not None:
                input_backend.release_all()
        time.sleep(interval)



def read_trade_order_count() -> tuple[int | None, int | None, str]:
    layout = current_trade_layout()
    if not layout.need:
        return None, None, ""
    if layout.have:
        rel_x = layout.have.x - layout.origin[0] + int(config.get("ORDER_COUNT_HAVE_OFFSET_X", 64))
        rel_y = layout.have.y - layout.origin[1] + int(config.get("ORDER_COUNT_HAVE_OFFSET_Y", 76))
    else:
        rel_x = layout.need.x - layout.origin[0] + int(config.get("ORDER_COUNT_OFFSET_X", 392))
        rel_y = layout.need.y - layout.origin[1] + int(config.get("ORDER_COUNT_OFFSET_Y", 87))
    w = int(config.get("ORDER_COUNT_W", 54))
    h = int(config.get("ORDER_COUNT_H", 24))
    crop = layout.screen[max(0, rel_y):rel_y + h, max(0, rel_x):rel_x + w]
    if crop.size == 0:
        return None, None, ""
    up = cv2.resize(crop, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    _, bright = cv2.threshold(gray, 90, 255, cv2.THRESH_BINARY)
    raw_result = get_ocr()(bright, use_det=False, use_cls=False)
    raw = "".join(getattr(raw_result, "txts", ()) or ())
    text = raw.replace(" ", "")
    match = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if not match:
        digits = re.findall(r"\d+", text)
        if len(digits) >= 2:
            return int(digits[0]), int(digits[1]), raw
        centers = ocr_text_centers(layout.screen, layout.origin)
        joined = " ".join(text for text, _x, _y in centers)
        full_match = re.search(r"(\d+)\s*/\s*(\d+)", joined)
        if full_match:
            return int(full_match.group(1)), int(full_match.group(2)), joined
        return None, None, raw
    return int(match.group(1)), int(match.group(2)), raw


def order_confirmation_text() -> str | None:
    win = find_window()
    if not win:
        return None
    screen, origin, _win = capture_target_screen()
    rel_x = int(config.get("ORDER_CONFIRM_WINDOW_X", 330))
    rel_y = int(config.get("ORDER_CONFIRM_WINDOW_Y", 350))
    w = int(config.get("ORDER_CONFIRM_WINDOW_W", 650))
    h = int(config.get("ORDER_CONFIRM_WINDOW_H", 310))
    centers = ocr_text_centers_region(screen, origin, rel_x, rel_y, w, h)
    joined = "".join(text for text, _x, _y in centers)
    markers = ["不利", "交易比率", "取消", "下订单", "下訂單"]
    if not any(marker in joined for marker in markers):
        return None
    return joined


def click_order_confirmation_if_visible() -> bool:
    win = find_window()
    if not win:
        return False
    joined = order_confirmation_text()
    if not joined:
        return False
    confirm_x = win.left + int(config.get("ORDER_CONFIRM_SUBMIT_X", 453))
    confirm_y = win.top + int(config.get("ORDER_CONFIRM_SUBMIT_Y", 578))
    log("检测到订单确认弹窗，点击确认:", repr(joined), (confirm_x, confirm_y))
    click_xy(confirm_x, confirm_y, duration=0.12)
    time.sleep(0.8)
    return True


def close_order_confirmation_if_visible() -> bool:
    win = find_window()
    if not win:
        return False
    joined = order_confirmation_text()
    if not joined:
        return False
    cancel_x = win.left + int(config.get("ORDER_CONFIRM_CANCEL_X", 832))
    cancel_y = win.top + int(config.get("ORDER_CONFIRM_CANCEL_Y", 578))
    log("检测到残留订单确认弹窗，点击取消:", repr(joined), (cancel_x, cancel_y))
    click_xy(cancel_x, cancel_y, duration=0.12)
    time.sleep(0.5)
    return True


def click_order_button(previous_count: int | None = None) -> bool:
    order = find_image("order.png", threshold=0.8)
    if not order:
        layout = current_trade_layout()
        order = layout.order
        if not order and layout.window:
            order = Match(
                layout.window.left + int(config.get("TRADE_FALLBACK_ORDER_X", 354)),
                layout.window.top + int(config.get("TRADE_FALLBACK_ORDER_Y", 293)),
                0.0,
            )
        if not order:
            log("未找到下订单按钮锚点，未点击下订单")
            return False
        log("未找到 order.png，使用布局里的下订单锚点:", order)

    offset_y = int(config.get("ORDER_BUTTON_SINGLE_CLICK_OFFSET_Y", 8))
    click_xy(order.x, order.y + offset_y, duration=0.12)
    log("已点击下订单按钮一次:", (order.x, order.y + offset_y))
    time.sleep(float(config.get("ORDER_AFTER_CLICK_VERIFY_DELAY", 0.55)))
    click_order_confirmation_if_visible()
    time.sleep(float(config.get("ORDER_AFTER_CONFIRM_VERIFY_DELAY", 0.45)))
    current, total, raw = read_trade_order_count()
    if previous_count is not None and current is not None and current > previous_count:
        log("订单数量增加:", previous_count, "->", current, "OCR=", raw)
        return True
    if previous_count is None:
        log("已点击下订单按钮，但未做数量验证")
        return True
    log("点击一次后订单数量未增加，不再重复点击，交给后续状态验证")
    return True


def place_order(
    left_value: int,
    right_value: int,
    verify_template: str | None = None,
    verify_item_name: str | None = None,
    verify_item_side: str = "left",
) -> bool:
    layout = current_trade_layout()
    left_xy = layout.input_left()
    right_xy = layout.input_right()
    if not left_xy or not right_xy:
        log("未找到 need.png，跳过下单")
        return False

    if verify_item_name and not selected_currency_matches(verify_item_name, verify_item_side, layout):
        log("下单前栏位不是目标商品，已中止:", verify_item_side, verify_item_name)
        save_debug_snapshot(f"selected_item_mismatch_{verify_item_name}")
        return False

    log("填写下单数量:", "左侧=", left_value, left_xy, "右侧=", right_value, right_xy)
    click_xy(*left_xy)
    if not input_and_verify(str(left_value)):
        log("左侧数量输入失败")
        return False

    click_xy(*right_xy)
    if not input_and_verify(str(right_value)):
        log("右侧数量输入失败")
        return False

    previous_cards = count_visible_item_cards(verify_item_name) if verify_item_name else None
    previous_count, _total, count_raw = read_trade_order_count()
    log("点击前订单数量:", previous_count, "OCR=", count_raw)
    if not click_order_button(previous_count=previous_count):
        if verify_item_name and previous_cards is not None:
            return wait_for_item_card_count_increase(verify_item_name, previous_cards)
        return False

    if verify_item_name and previous_cards is not None:
        if wait_for_item_card_count_increase(verify_item_name, previous_cards):
            return True
        current_count, _total, count_raw_after = read_trade_order_count()
        if current_count is not None and (previous_count is None or current_count > previous_count):
            log("通过订单数量变化确认下单成功:", previous_count, "->", current_count, "OCR=", count_raw_after)
            return True
        log("未通过卡片或数量变化确认下单:", verify_item_name, "数量OCR=", count_raw_after)
        return False

    if verify_template and template_path(verify_template).exists():
        if wait_for_image(verify_template, timeout=3.0, threshold=0.8):
            log("订单状态模板验证成功:", verify_template)
            return True
        log("订单点击未通过模板验证:", verify_template)
        return False

    return True

def buy_pair(pair_name: str, budget_chaos: float | None = None) -> bool:
    base = str(config["BASE_CURRENCY"])
    log("市场直买:", pair_name)
    if not select_currency(pair_name, base):
        return False

    ratio, raw_text = read_market_ratio()
    if not ratio:
        return False

    selected = choose_buy_price(*ratio, max_cost=budget_chaos)
    if not selected:
        log("没有合适的市场直买报价:", pair_name, "市场=", raw_text, "预算=", budget_chaos)
        return False

    got, cost, reduce_pct = selected
    if budget_chaos is not None and cost > budget_chaos:
        log("买入报价超过预算:", pair_name, cost, ">", budget_chaos)
        return False

    log("市场直买报价:", got, pair_name, "=", cost, base, "压价:", f"{reduce_pct:.2f}%", "市场:", raw_text)
    save_pair_price(pair_name, {"got": got, "cost": cost, "source": "market_direct", "market": raw_text})
    ok = place_order(got, cost, verify_template=f"正在买{pair_name}.png", verify_item_name=pair_name)
    if ok:
        save_order_state(pair_name, "buy", {"got": got, "cost": cost, "source": "market_direct", "market": raw_text})
    else:
        remove_pair_price(pair_name)
    return ok


def buy_pair_from_candidate(row: dict[str, Any], budget_chaos: float | None = None) -> bool:
    pair_name = str(row["name"])
    base = str(row["base"])
    ratio = row.get("buy", {}).get("ratio")
    if not ratio or len(ratio) != 2:
        log("候选缺少买入比例，跳过:", pair_name)
        return False

    selected = choose_buy_price_from_competition_ratio((float(ratio[0]), float(ratio[1])))
    if not selected:
        log("候选无合适买入报价:", pair_name)
        return False

    got, cost, reduce_pct = selected
    if budget_chaos is not None and cost > budget_chaos:
        log("候选买入超过预算，跳过:", pair_name, cost, ">", budget_chaos)
        return False

    log(
        "按扫描候选买入:",
        pair_name,
        "方向: 混沌石 -> 商品",
        got,
        pair_name,
        "=",
        cost,
        base,
        "压价:",
        f"{reduce_pct:.2f}%",
    )
    if not select_currency(pair_name, base):
        return False

    save_pair_price(
        pair_name,
        {
            "got": got,
            "cost": cost,
            "source": "confirmed_ingame_scan",
            "buy_price": row.get("buy_price"),
            "sell_price": row.get("sell_price"),
            "spread": row.get("spread"),
            "spread_pct": row.get("spread_pct"),
            "gold_per_divine_profit": row.get("gold_per_divine_profit"),
        },
    )
    ok = place_order(got, cost, verify_template=f"正在买{pair_name}.png", verify_item_name=pair_name)
    if ok:
        save_order_state(
            pair_name,
            "buy",
            {
                "got": got,
                "cost": cost,
                "source": "confirmed_ingame_scan",
                "chain": "chaos_to_item_to_chaos",
            },
        )
    else:
        remove_pair_price(pair_name)
    return ok


def sell_pair(pair_name: str) -> bool:
    base = str(config["BASE_CURRENCY"])
    bought = get_pair_price(pair_name)
    if not bought:
        log("没有买入记录，跳过卖出:", pair_name)
        return False

    if not select_currency(base, pair_name):
        return False

    ratio, raw_text = read_market_ratio()
    if not ratio:
        return False

    selected = choose_sell_price_from_market(*ratio, item_count=int(bought["got"]))
    if not selected:
        log("没有合适的市场直卖报价:", pair_name, "市场=", raw_text)
        return False

    sell_items, sell_chaos, profit_pct = selected
    log("市场直卖报价:", sell_items, pair_name, "->", sell_chaos, base, "溢价:", f"{profit_pct:.2f}%", "市场:", raw_text)

    ok = place_order(
        sell_chaos,
        sell_items,
        verify_template=f"正在卖{pair_name}.png",
        verify_item_name=pair_name,
        verify_item_side="right",
    )
    if ok:
        save_order_state(
            pair_name,
            "sell",
            {
                "sell_items": sell_items,
                "sell_chaos": sell_chaos,
                "profit_pct": profit_pct,
                "source": "market_direct",
                "market": raw_text,
            },
        )
        remove_pair_price(pair_name)
        with TRADE_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {pair_name}: "
                f"buy {bought}, sell {sell_items}->{sell_chaos}, market {raw_text}, premium {profit_pct:.2f}%\n"
            )
    return ok


def stock_in(done: Match, action: str) -> None:
    check_stop()
    # Completed order cards can be collected directly by Ctrl+right-clicking
    # the item/currency icon on the card. This keeps the flow deterministic:
    # no backpack slot scan is needed after a successful stock-in click.
    if action == "buy":
        offsets = (
            (int(config["STOCK_BUY_X1"]), int(config.get("STOCK_BUY_Y1", 0))),
            (int(config["STOCK_BUY_X2"]), int(config.get("STOCK_BUY_Y2", 0))),
        )
    else:
        offsets = (
            (int(config["STOCK_SELL_X1"]), int(config.get("STOCK_SELL_Y1", 0))),
            (int(config["STOCK_SELL_X2"]), int(config.get("STOCK_SELL_Y2", 0))),
        )

    clicked: set[tuple[int, int]] = set()
    for offset_x, offset_y in offsets:
        check_stop()
        x = done.x + offset_x
        y = done.y + offset_y
        if (x, y) in clicked:
            continue
        clicked.add((x, y))
        if config.get("DRY_RUN"):
            log("[DRY_RUN] 入库点击", action, x, y)
            continue
        check_stop()
        get_input().mouse_move(x, y, duration_ms=80)
        try:
            check_stop()
            get_input().key_down("ctrl")
            check_stop()
            get_input().mouse_click(button="right", hold_ms=45)
        finally:
            get_input().key_up("ctrl")
        time.sleep(0.2)
    log("已通过 Ctrl+右键入库完成订单:", action)


def configured_pairs() -> list[str]:
    return [str(pair["name"]) for pair in config.get("TRADING_PAIRS", []) if pair.get("name")]


def active_pair_names() -> list[str]:
    if bool(config.get("ORDER_BOARD_MODE", True)):
        return order_board_pairs()
    return configured_pairs()


def handle_once() -> None:
    check_stop()
    if game_unavailable_guard():
        return
    check_stop()
    if bool(config.get("ORDER_BOARD_MODE", True)):
        handle_order_board_once()
        return

    screen, origin, win = capture_target_screen()
    if win:
        log("绑定窗口:", win.title, f"{win.width}x{win.height}", f"({win.left},{win.top})")
    done = find_image("done.png", screen=screen, screen_origin=origin, quiet=True)
    trade_ui = find_image("need.png", screen=screen, screen_origin=origin, threshold=0.8, quiet=True)
    if not trade_ui and not done:
        log("未检测到通货交易界面，等待")
        return

    active_order = load_order_state()
    if active_order:
        name = str(active_order["name"])
        action = str(active_order["action"])
        if done:
            log("检测到当前订单完成:", name, action)
            stock_in(done, action)
            clear_order_state()
            return
        log("当前订单未完成，等待:", name, action)
        return

    for name in available_ingame_scan_pairs():
        if get_pair_price(name):
            log("已有买入成本，准备把商品卖回混沌石:", name)
            sell_pair(name)
            return

    if trade_ui:
        budget = float(config.get("AUTO_TRADE_CHAOS_BUDGET", 420))
        names = available_ingame_scan_pairs()
        log("没有持货，进入市场直买模式，预算:", budget, "交易对:", ", ".join(names))
        for name in names:
            if buy_pair(name, budget_chaos=budget):
                return
        log("没有挂出市场直买订单")
        return

def key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def main() -> None:
    log("OwnCurrencyBot 启动")
    log("配置:", CONFIG_FILE)
    log("图片:", IMAGES_DIR)
    log("交易对:", ", ".join(active_pair_names()) or "无")
    log("输入后端:", config.get("INPUT_BACKEND", "pydm_driver"))
    log("F1 启动，F2 暂停，F3 停止退出，Ctrl+C 退出")

    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    )

    running = False
    f1_prev = False
    f2_prev = False
    f3_prev = False
    while True:
        f1 = key_down(VK_F1)
        f2 = key_down(VK_F2)
        f3 = key_down(VK_F3)
        if f1 and not f1_prev:
            clear_stop_request()
            running = True
            log("状态: 运行中")
        if f2 and not f2_prev:
            running = False
            request_stop()
            log("状态: 已暂停")
        if f3 and not f3_prev:
            running = False
            request_stop()
            log("状态: 已停止，退出")
            break
        f1_prev = f1
        f2_prev = f2
        f3_prev = f3

        if running:
            try:
                handle_once()
            except BotStopRequested:
                running = False
                log("当前操作已停止")
            except Exception:
                err = traceback.format_exc()
                log("运行异常:", err)
                if input_backend is not None:
                    input_backend.release_all()
                (LOG_DIR / "crash.log").write_text(err, encoding="utf-8")
                time.sleep(1)
        time.sleep(float(config["LOOP_DELAY"]))



def order_board_validate(dry_select: bool = True) -> int:
    reason = game_unavailable_reason()
    print("Game unavailable:", reason)
    print("Order board pairs:", [name.encode("unicode_escape").decode() for name in order_board_pairs()])
    win = find_window()
    print("Window:", win)
    if reason:
        return 2
    ok = ensure_trade_and_inventory_open()
    print("Ensure trade+inventory:", ok)
    layout = current_trade_layout()
    print("Layout open:", layout.is_open)
    print("Anchors:", "title=", layout.title, "need=", layout.need, "have=", layout.have, "order=", layout.order)
    print("Slots:", "left=", layout.left_slot(), "right=", layout.right_slot(), "input_left=", layout.input_left(), "input_right=", layout.input_right())
    slots = inventory_slot_centers()
    print("Inventory slots sample:", slots[:3], "count=", len(slots))
    print("Selector open:", selector_is_open())
    if not ok or not dry_select:
        return 0 if ok else 1
    pairs = order_board_pairs()
    if not pairs:
        print("No order board pairs")
        return 1
    name = pairs[0]
    base = str(config["BASE_CURRENCY"])
    print("Dry select:", name, "/", base)
    selected = select_currency(name, base)
    print("Select result:", selected)
    layout = current_trade_layout()
    print("After select layout:", layout.is_open, layout.need, layout.have, layout.order)
    return 0 if selected else 1


def order_board_status_report() -> int:
    data = load_order_board()
    print("Order board file:", ORDER_BOARD_FILE)
    print("Trade ledger file:", TRADE_LEDGER_FILE)
    if not isinstance(data.get("session"), dict):
        print("Session: not started; first order-board loop will record initial chaos/gold")
    print("Pairs:", [name.encode("unicode_escape").decode() for name in order_board_pairs()])
    print("Active orders:", active_board_order_count(data))
    print("Totals:", ledger_totals(data))
    for name, order in data.get("orders", {}).items():
        if not isinstance(order, dict):
            print(name, order)
            continue
        state = order.get("state")
        timed_out = board_order_timed_out(order) if state in {"buy_order", "sell_order"} else False
        print(name.encode("unicode_escape").decode(), "state=", state, "timed_out=", timed_out, "data=", order)
    return 0


def order_board_preflight_report() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    names = order_board_pairs()
    expected = order_board_default_pairs()

    print("Project:", PROJECT_DIR)
    print("Config:", CONFIG_FILE, "exists=", CONFIG_FILE.exists())
    print("Images:", IMAGES_DIR, "exists=", IMAGES_DIR.exists())
    print("Logs:", LOG_DIR)
    print("Input backend:", config.get("INPUT_BACKEND", "pydm_driver"))
    print("Base currency:", config.get("BASE_CURRENCY"))
    print("Order board mode:", bool(config.get("ORDER_BOARD_MODE", True)))
    print("Order board pairs:", names)
    print("Max orders:", config.get("ORDER_BOARD_MAX_ORDERS"))
    print("Order size:", config.get("ORDER_BOARD_ORDER_SIZE"))
    print("Timeout seconds:", config.get("ORDER_TIMEOUT_SECONDS"))
    print("Unavailable cooldown threshold:", config.get("GAME_UNAVAILABLE_COOLDOWN_THRESHOLD"))
    print("Unavailable cooldown seconds:", config.get("GAME_UNAVAILABLE_COOLDOWN_SECONDS"))

    if not bool(config.get("ORDER_BOARD_MODE", True)):
        failures.append("ORDER_BOARD_MODE is disabled")
    if str(config.get("BASE_CURRENCY")) != "混沌石":
        warnings.append("BASE_CURRENCY is not 混沌石")
    if int(config.get("ORDER_BOARD_MAX_ORDERS", 0)) != 10:
        warnings.append("ORDER_BOARD_MAX_ORDERS is not 10")
    if int(config.get("ORDER_BOARD_ORDER_SIZE", 0)) != 1:
        failures.append("ORDER_BOARD_ORDER_SIZE must be 1")
    if int(config.get("ORDER_TIMEOUT_SECONDS", 0)) != 600:
        warnings.append("ORDER_TIMEOUT_SECONDS is not 600")
    if [normalize_currency_text(x) for x in names] != [normalize_currency_text(x) for x in expected]:
        failures.append("ORDER_BOARD_PAIRS does not exactly match the five target items")

    required_templates = ["title.png", "need.png", "order.png", "done.png", *[f"{name}.png" for name in names]]
    for image_name in required_templates:
        path = template_path(image_name)
        status = "ok" if path.exists() else "missing"
        print("Template:", image_name, status, path)
        if not path.exists():
            failures.append(f"missing template: {image_name}")

    for name in names:
        image_name = f"{name}_背包图标.png"
        path = template_path(image_name)
        status = "ok" if path.exists() else "missing"
        print("Backpack template:", image_name, status, path)
        if not path.exists():
            warnings.append(f"missing backpack template: {image_name}")

    win = find_window()
    print("Window:", win)
    reason = game_unavailable_reason() if win else "window not found"
    print("Game unavailable:", reason)
    if reason:
        warnings.append(f"game not ready: {reason}")

    data = load_order_board()
    print("Active order count:", active_board_order_count(data))
    print("Order board file:", ORDER_BOARD_FILE)

    for warning in warnings:
        print("WARN:", warning)
    for failure in failures:
        print("FAIL:", failure)

    if failures:
        print("Preflight: failed")
        return 1
    print("Preflight: ok")
    return 0


def inventory_scan_check() -> int:
    reason = game_unavailable_reason()
    print("Game unavailable:", reason)
    win = find_window()
    print("Window:", win)
    if reason:
        return 2

    ok = ensure_trade_and_inventory_open()
    print("Ensure trade+inventory:", ok)
    layout = current_trade_layout()
    print("Layout open:", layout.is_open)
    print("Anchors:", "title=", layout.title, "need=", layout.need, "have=", layout.have, "order=", layout.order)
    print("Done anchor:", find_done_anchor())
    amount, raw = read_gold_amount()
    print("Gold:", amount, "raw=", repr(raw))
    print("Inventory grid origin:", inventory_grid_origin())
    slots = inventory_slot_centers()
    print("Inventory grid count:", len(slots))
    print("Inventory grid sample:", slots[:12])
    if not ok:
        return 1

    for name in order_board_pairs():
        found = inventory_has_item(name)
        print(name.encode("unicode_escape").decode(), "in_inventory=", found)
    return 0


def initial_inventory_check() -> int:
    reason = game_unavailable_reason()
    print("Game unavailable:", reason)
    win = find_window()
    print("Window:", win)
    if reason:
        return 2

    ok = ensure_inventory_open()
    print("Ensure inventory:", ok)
    if not ok:
        return 1

    snapshot = save_inventory_session_snapshot("manual_initial_inventory_check")
    gold, raw_gold = read_gold_amount()
    base = str(config.get("BASE_CURRENCY", "混沌石"))
    base_amount, raw_base = read_inventory_currency_amount(base)
    print("Inventory snapshot:", snapshot.get("image"))
    print("Inventory OCR dump:", snapshot.get("ocr"))
    print("Gold OCR raw:", repr(raw_gold))
    print("Gold amount:", gold)
    print("Base currency:", base.encode("unicode_escape").decode())
    print("Base currency amount:", base_amount)
    print("Base currency raw:", raw_base.encode("unicode_escape", errors="replace").decode())
    return 0 if gold is not None and base_amount is not None else 1


def layout_dump() -> int:
    screen, origin, win = capture_target_screen()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_path = LOG_DIR / f"layout_{stamp}.png"
    text_path = LOG_DIR / f"layout_{stamp}_ocr.txt"
    ok, encoded = cv2.imencode(".png", screen)
    if ok:
        encoded.tofile(str(image_path))
    centers = ocr_text_centers(screen, origin)
    layout = current_trade_layout()
    lines = [
        f"window={win}",
        f"origin={origin}",
        f"inventory_grid_origin={inventory_grid_origin()}",
        f"trade_layout_open={layout.is_open}",
        f"trade_anchors title={layout.title} need={layout.need} have={layout.have} order={layout.order}",
        f"trade_slots left={layout.left_slot()} right={layout.right_slot()} input_left={layout.input_left()} input_right={layout.input_right()}",
        "",
    ]
    for text, x, y in centers:
        lines.append(f"{x},{y}\t{text}")
    text_path.write_text("\n".join(lines), encoding="utf-8")
    print("Layout screenshot:", image_path)
    print("Layout OCR dump:", text_path)
    print("OCR count:", len(centers))
    return 0


def save_debug_snapshot(reason: str) -> tuple[Path | None, Path | None]:
    if not bool(config.get("DEBUG_SNAPSHOT_ON_FAILURE", True)):
        return None, None
    safe_reason = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", reason).strip("_")[:80] or "debug"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screen, origin, win = capture_target_screen()
    image_path = LOG_DIR / f"debug_{stamp}_{safe_reason}.png"
    text_path = LOG_DIR / f"debug_{stamp}_{safe_reason}_ocr.txt"
    try:
        ok, encoded = cv2.imencode(".png", screen)
        if ok:
            encoded.tofile(str(image_path))
        centers = ocr_text_centers(screen, origin)
        layout = current_trade_layout()
        lines = [
            f"reason={reason}",
            f"window={win}",
            f"origin={origin}",
            f"game_unavailable={game_unavailable_reason()}",
            f"inventory_grid_origin={inventory_grid_origin()}",
            f"trade_layout_open={layout.is_open}",
            f"trade_anchors title={layout.title} need={layout.need} have={layout.have} order={layout.order}",
            f"trade_slots left={layout.left_slot()} right={layout.right_slot()} input_left={layout.input_left()} input_right={layout.input_right()}",
            f"selector_open={selector_is_open()}",
            f"done_anchor={find_done_anchor()}",
            "",
        ]
        for text, x, y in centers:
            lines.append(f"{x},{y}\t{text}")
        text_path.write_text("\n".join(lines), encoding="utf-8")
        log("调试截图已保存:", reason, image_path, text_path)
        return image_path, text_path
    except Exception as exc:
        log("调试截图失败:", reason, exc)
        return None, None


def input_self_test() -> bool:
    """Open a small local window and verify real keyboard and mouse output."""
    import tkinter as tk

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    backend = get_input()
    root = tk.Tk()
    root.title("OwnCurrencyBot Input Test")
    root.geometry("360x180+200+200")
    root.attributes("-topmost", True)

    status = tk.StringVar(value="Preparing...")
    clicked = {"ok": False}

    tk.Label(root, text="Input backend self-test").pack(pady=8)
    entry = tk.Entry(root, width=24)
    entry.pack(pady=8)

    def on_click() -> None:
        clicked["ok"] = True
        status.set("Button clicked")

    button = tk.Button(root, text="Click Target", command=on_click, width=18)
    button.pack(pady=8)
    tk.Label(root, textvariable=status).pack(pady=4)

    root.update()
    root.lift()
    root.focus_force()
    entry.focus_force()
    root.update()
    time.sleep(0.6)

    ex = entry.winfo_rootx() + entry.winfo_width() // 2
    ey = entry.winfo_rooty() + entry.winfo_height() // 2
    backend.mouse_click(ex, ey, button="left", hold_ms=80)
    time.sleep(0.4)
    root.update()

    for key in "abc":
        backend.key_tap(key, hold_ms=45)
        time.sleep(0.16)
        root.update()
    time.sleep(0.4)
    root.update()
    typed = entry.get()
    keyboard_ok = typed == "abc"

    bx = button.winfo_rootx() + button.winfo_width() // 2
    by = button.winfo_rooty() + button.winfo_height() // 2
    backend.mouse_click(bx, by, button="left", hold_ms=90)
    root.update()
    time.sleep(0.5)
    root.update()
    mouse_ok = clicked["ok"]

    status.set(f"keyboard={keyboard_ok}, mouse={mouse_ok}")
    root.update()
    time.sleep(0.8)
    root.destroy()
    backend.release_all()

    print("Input self-test typed:", typed)
    print("Input self-test keyboard:", "ok" if keyboard_ok else "failed")
    print("Input self-test mouse:", "ok" if mouse_ok else "failed")
    return keyboard_ok and mouse_ok


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            print("OwnCurrencyBot check")
            print("APP_DIR:", APP_DIR)
            print("BUNDLE_DIR:", BUNDLE_DIR)
            print("PROJECT_DIR:", PROJECT_DIR)
            print("CONFIG_FILE:", CONFIG_FILE, "exists=", CONFIG_FILE.exists())
            print("IMAGES_DIR:", IMAGES_DIR, "exists=", IMAGES_DIR.exists())
            print("LOG_DIR:", LOG_DIR)
            print("ORDER_BOARD_MODE:", bool(config.get("ORDER_BOARD_MODE", True)))
            print("ACTIVE_PAIRS:", active_pair_names())
            print("TRADING_PAIRS:", configured_pairs())
            print("ORDER_BOARD_PAIRS:", order_board_pairs())
            print("INPUT_BACKEND:", config.get("INPUT_BACKEND", "pydm_driver"))
            sys.exit(0)
        if "--window-check" in sys.argv:
            win = find_window()
            if not win:
                print("Window not found:", config.get("GAME_WINDOW_TITLE", ""))
                sys.exit(1)
            print("Window ok:")
            print("  hwnd:", hex(win.hwnd))
            print("  title:", win.title)
            print("  rect:", (win.left, win.top, win.right, win.bottom))
            print("  size:", (win.width, win.height))
            print("  center:", win.center)
            sys.exit(0)
        if "--window-mouse-test" in sys.argv:
            win = find_window()
            if not win:
                print("Window not found:", config.get("GAME_WINDOW_TITLE", ""))
                sys.exit(1)
            activate_window(win)
            backend = get_input()
            x, y = win.center
            backend.mouse_move(x, y, duration_ms=200)
            time.sleep(0.2)

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            backend.release_all()
            ok = abs(pt.x - x) <= 5 and abs(pt.y - y) <= 5
            print("Window mouse test target:", (x, y))
            print("Window mouse test cursor:", (pt.x, pt.y))
            print("Window mouse test:", "ok" if ok else "failed")
            sys.exit(0 if ok else 1)
        if "--gold-check" in sys.argv:
            amount, raw = open_inventory_and_read_gold()
            print("Gold OCR raw:", repr(raw))
            print("Gold amount:", amount)
            sys.exit(0 if amount is not None else 1)
        if "--competition-check" in sys.argv:
            ratio = read_first_competition_ratio()
            selected = choose_buy_price_from_competition_ratio(ratio) if ratio else None
            print("Competition ratio:", ratio)
            print("Competition buy quote:", selected)
            sys.exit(0 if selected else 1)
        if "--strategy-check" in sys.argv:
            import poe2scout_strategy

            print(poe2scout_strategy.strategy_report())
            sys.exit(0)
        if "--strategy-scan" in sys.argv:
            import poe2scout_strategy

            print(poe2scout_strategy.scan_report())
            sys.exit(0)
        if "--strategy-chain" in sys.argv:
            import poe2scout_strategy

            print(poe2scout_strategy.chain_report())
            sys.exit(0)
        if "--ingame-arbitrage-scan" in sys.argv:
            print(ingame_arbitrage_scan_report())
            sys.exit(0)
        if "--watch-ingame-arbitrage" in sys.argv:
            watch_ingame_arbitrage()
            sys.exit(0)
        if "--order-board-validate" in sys.argv:
            sys.exit(order_board_validate(dry_select=True))
        if "--order-board-status" in sys.argv:
            sys.exit(order_board_status_report())
        if "--order-board-preflight" in sys.argv:
            sys.exit(order_board_preflight_report())
        if "--startup-reconcile-check" in sys.argv:
            sys.exit(startup_reconcile_check())
        if "--inventory-scan-check" in sys.argv:
            sys.exit(inventory_scan_check())
        if "--initial-inventory-check" in sys.argv:
            sys.exit(initial_inventory_check())
        if "--layout-dump" in sys.argv:
            sys.exit(layout_dump())
        if "--input-check" in sys.argv:
            backend = get_input()
            print("Input check ok:", backend.__class__.__name__)
            backend.release_all()
            sys.exit(0)
        if "--input-self-test" in sys.argv:
            ok = input_self_test()
            sys.exit(0 if ok else 1)
        if "--ocr-check" in sys.argv:
            img = np.full((40, 180), 255, dtype=np.uint8)
            result = get_ocr()(img, use_det=False, use_cls=False)
            print("OCR check ok:", result)
            sys.exit(0)
        main()
    except KeyboardInterrupt:
        log("已退出")
    finally:
        if input_backend is not None:
            input_backend.release_all()
