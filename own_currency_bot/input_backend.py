from __future__ import annotations

import ctypes
import struct
import sys
import time
from pathlib import Path
from typing import Any, Protocol

import pyautogui


MOUSE_BUTTONS = frozenset({"left", "right", "middle"})


class InputBackend(Protocol):
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def key_tap(self, key: str, hold_ms: int = 30) -> None: ...
    def hotkey(self, *keys: str, hold_ms: int = 30) -> None: ...
    def mouse_move(self, x: int, y: int, duration_ms: int = 80) -> None: ...
    def mouse_down(self, button: str = "left") -> None: ...
    def mouse_up(self, button: str = "left") -> None: ...
    def mouse_click(self, x: int | None = None, y: int | None = None, button: str = "left", hold_ms: int = 30) -> None: ...
    def release_all(self) -> None: ...


_ALIASES = {
    "control": "ctrl",
    "escape": "esc",
    "return": "enter",
    "mouse_left": "left",
    "mouse_right": "right",
    "mouse_middle": "middle",
}


def norm_key(key: str) -> str:
    k = str(key).strip().lower()
    return _ALIASES.get(k, k)


def is_mouse_button(key: str) -> bool:
    return norm_key(key) in MOUSE_BUTTONS


class PyAutoGuiBackend:
    def __init__(self) -> None:
        pyautogui.PAUSE = 0.0
        pyautogui.FAILSAFE = True
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()

    def key_down(self, key: str) -> None:
        k = norm_key(key)
        if k in MOUSE_BUTTONS:
            self.mouse_down(k)
            return
        pyautogui.keyDown(k)
        self._pressed_keys.add(k)

    def key_up(self, key: str) -> None:
        k = norm_key(key)
        if k in MOUSE_BUTTONS:
            self.mouse_up(k)
            return
        pyautogui.keyUp(k)
        self._pressed_keys.discard(k)

    def key_tap(self, key: str, hold_ms: int = 30) -> None:
        self.key_down(key)
        time.sleep(max(0, hold_ms) / 1000.0)
        self.key_up(key)

    def hotkey(self, *keys: str, hold_ms: int = 30) -> None:
        normalized = [norm_key(k) for k in keys]
        for key in normalized:
            self.key_down(key)
        time.sleep(max(0, hold_ms) / 1000.0)
        for key in reversed(normalized):
            self.key_up(key)

    def mouse_move(self, x: int, y: int, duration_ms: int = 80) -> None:
        pyautogui.moveTo(int(x), int(y), duration=max(0, duration_ms) / 1000.0)

    def mouse_down(self, button: str = "left") -> None:
        b = norm_key(button)
        if b not in MOUSE_BUTTONS:
            raise ValueError(f"invalid mouse button: {button!r}")
        pyautogui.mouseDown(button=b)
        self._pressed_buttons.add(b)

    def mouse_up(self, button: str = "left") -> None:
        b = norm_key(button)
        if b not in MOUSE_BUTTONS:
            raise ValueError(f"invalid mouse button: {button!r}")
        pyautogui.mouseUp(button=b)
        self._pressed_buttons.discard(b)

    def mouse_click(self, x: int | None = None, y: int | None = None, button: str = "left", hold_ms: int = 30) -> None:
        if x is not None and y is not None:
            self.mouse_move(int(x), int(y), duration_ms=0)
        self.mouse_down(button)
        time.sleep(max(0, hold_ms) / 1000.0)
        self.mouse_up(button)

    def release_all(self) -> None:
        for key in list(self._pressed_keys):
            try:
                self.key_up(key)
            except Exception:
                pass
        for button in list(self._pressed_buttons):
            try:
                self.mouse_up(button)
            except Exception:
                pass


_VK_CODES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left_arrow": 0x25,
    "up_arrow": 0x26,
    "right_arrow": 0x27,
    "down_arrow": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
}
_VK_CODES.update({str(i): 0x30 + i for i in range(10)})
_VK_CODES.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
_VK_CODES.update({f"f{i}": 0x6F + i for i in range(1, 25)})


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> tuple[int, int]:
    pt = _Point()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return max(1, int(user32.GetSystemMetrics(0))), max(1, int(user32.GetSystemMetrics(1)))


def _parse_u16(value: int | str, field: str) -> int:
    try:
        parsed = int(value) if isinstance(value, int) else int(str(value), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer or hex string, got {value!r}") from exc
    if not 0 <= parsed <= 0xFFFF:
        raise ValueError(f"{field} must be in [0, 0xffff], got {value!r}")
    return parsed


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


def _resolve_msdk_dll(explicit_path: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    here = Path(__file__).resolve().parent
    bundle = _bundle_dir()
    candidates.extend(
        [
            here / "drivers" / "msdk.dll",
            bundle / "drivers" / "msdk.dll",
            Path.cwd() / "drivers" / "msdk.dll",
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("msdk.dll not found. Put it under own_currency_bot\\drivers.")


class PyDmGameDriverBackend:
    def __init__(
        self,
        *,
        vid: int | str = "0xC216",
        pid: int | str = "0x0301",
        mouse_enabled: bool = True,
        move_mode: str = "instant",
        move_step_ms: int = 10,
        dll_path: str | Path | None = None,
    ) -> None:
        if move_mode not in {"instant", "smooth"}:
            raise ValueError("move_mode must be 'instant' or 'smooth'")
        self._vid = _parse_u16(vid, "vid")
        self._pid = _parse_u16(pid, "pid")
        self._mouse_enabled = bool(mouse_enabled)
        self._move_mode = move_mode
        self._move_step_ms = max(1, int(move_step_ms))
        self._pressed_keys: set[str] = set()
        self._pressed_buttons: set[str] = set()
        self.dll_path = _resolve_msdk_dll(dll_path)
        self._lib = ctypes.windll.LoadLibrary(str(self.dll_path))
        self._configure_library(self._lib)
        self.hdl = self._call("M_Open_VidPid", self._vid, self._pid)
        if self.hdl in (None, 0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            raise RuntimeError(f"YJS device open failed: VID=0x{self._vid:04X}, PID=0x{self._pid:04X}")
        self._set_resolution()

    @staticmethod
    def _configure_library(lib: Any) -> None:
        open_fn = getattr(lib, "M_Open_VidPid", None)
        if open_fn is not None:
            open_fn.argtypes = [ctypes.c_uint16, ctypes.c_uint16]
            open_fn.restype = ctypes.c_void_p
        signatures = {
            "M_ResolutionUsed": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "M_KeyDown2": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "M_KeyUp2": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "M_LeftDown": ([ctypes.c_void_p], ctypes.c_int),
            "M_LeftUp": ([ctypes.c_void_p], ctypes.c_int),
            "M_RightDown": ([ctypes.c_void_p], ctypes.c_int),
            "M_RightUp": ([ctypes.c_void_p], ctypes.c_int),
            "M_MiddleDown": ([ctypes.c_void_p], ctypes.c_int),
            "M_MiddleUp": ([ctypes.c_void_p], ctypes.c_int),
            "M_MoveTo3_D": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "M_MoveTo3": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
            "M_ReleaseAllKey": ([ctypes.c_void_p], ctypes.c_int),
        }
        for name, (argtypes, restype) in signatures.items():
            fn = getattr(lib, name, None)
            if fn is not None:
                fn.argtypes = argtypes
                fn.restype = restype

    def _call(self, name: str, *args: Any) -> Any:
        fn = getattr(self._lib, name, None)
        if not callable(fn):
            raise RuntimeError(f"msdk.dll missing function: {name}")
        return fn(*args)

    def _set_resolution(self) -> None:
        fn = getattr(self._lib, "M_ResolutionUsed", None)
        if callable(fn):
            width, height = _screen_size()
            fn(self.hdl, width, height)

    def _key_code(self, key: str) -> int:
        k = norm_key(key)
        if k in _VK_CODES:
            return int(_VK_CODES[k])
        raise ValueError(f"YJS backend does not support key {key!r}")

    def key_down(self, key: str) -> None:
        k = norm_key(key)
        if k in MOUSE_BUTTONS:
            self.mouse_down(k)
            return
        self._call("M_KeyDown2", self.hdl, self._key_code(k), 1)
        self._pressed_keys.add(k)

    def key_up(self, key: str) -> None:
        k = norm_key(key)
        if k in MOUSE_BUTTONS:
            self.mouse_up(k)
            return
        self._call("M_KeyUp2", self.hdl, self._key_code(k), 1)
        self._pressed_keys.discard(k)

    def key_tap(self, key: str, hold_ms: int = 30) -> None:
        self.key_down(key)
        time.sleep(max(0, hold_ms) / 1000.0)
        self.key_up(key)

    def hotkey(self, *keys: str, hold_ms: int = 30) -> None:
        normalized = [norm_key(k) for k in keys]
        for key in normalized:
            self.key_down(key)
            time.sleep(0.015)
        time.sleep(max(0, hold_ms) / 1000.0)
        for key in reversed(normalized):
            self.key_up(key)
            time.sleep(0.015)

    def mouse_move(self, x: int, y: int, duration_ms: int = 80) -> None:
        if not self._mouse_enabled:
            return
        target_x = int(x)
        target_y = int(y)
        if duration_ms <= 0:
            self._move_to(target_x, target_y)
            return
        try:
            start_x, start_y = _cursor_pos()
        except Exception:
            start_x, start_y = target_x, target_y
        steps = max(2, int(duration_ms / self._move_step_ms))
        sleep_s = max(0, duration_ms) / 1000.0 / steps
        for i in range(1, steps + 1):
            t = i / steps
            self._move_to(int(start_x + (target_x - start_x) * t), int(start_y + (target_y - start_y) * t))
            time.sleep(sleep_s)

    def _move_to(self, x: int, y: int) -> None:
        fn_name = "M_MoveTo3" if self._move_mode == "smooth" else "M_MoveTo3_D"
        self._call(fn_name, self.hdl, int(x), int(y))

    def mouse_down(self, button: str = "left") -> None:
        if not self._mouse_enabled:
            return
        b = norm_key(button)
        self._call({"left": "M_LeftDown", "right": "M_RightDown", "middle": "M_MiddleDown"}[b], self.hdl)
        self._pressed_buttons.add(b)

    def mouse_up(self, button: str = "left") -> None:
        if not self._mouse_enabled:
            return
        b = norm_key(button)
        self._call({"left": "M_LeftUp", "right": "M_RightUp", "middle": "M_MiddleUp"}[b], self.hdl)
        self._pressed_buttons.discard(b)

    def mouse_click(self, x: int | None = None, y: int | None = None, button: str = "left", hold_ms: int = 30) -> None:
        if x is not None and y is not None:
            self.mouse_move(int(x), int(y), duration_ms=0)
        self.mouse_down(button)
        time.sleep(max(0, hold_ms) / 1000.0)
        self.mouse_up(button)

    def release_all(self) -> None:
        for key in list(self._pressed_keys):
            try:
                self.key_up(key)
            except Exception:
                pass
        self._pressed_keys.clear()
        for button in list(self._pressed_buttons):
            try:
                self.mouse_up(button)
            except Exception:
                pass
        self._pressed_buttons.clear()
        release_all = getattr(self._lib, "M_ReleaseAllKey", None)
        if callable(release_all):
            try:
                release_all(self.hdl)
            except Exception:
                pass

    def __del__(self) -> None:
        try:
            self.release_all()
        except Exception:
            pass


def create_input_backend(config: dict[str, Any]) -> InputBackend:
    backend = str(config.get("INPUT_BACKEND", "pydm_driver")).strip().lower()
    if backend == "pyautogui":
        return PyAutoGuiBackend()
    if backend in {"pydm", "pydm_driver", "yjs", "driver"}:
        return PyDmGameDriverBackend(
            vid=config.get("PYDM_VID", "0xC216"),
            pid=config.get("PYDM_PID", "0x0301"),
            mouse_enabled=bool(config.get("PYDM_MOUSE_ENABLED", True)),
            move_mode=str(config.get("PYDM_MOVE_MODE", "instant")),
            move_step_ms=int(config.get("PYDM_MOVE_STEP_MS", 10)),
            dll_path=config.get("PYDM_DLL_PATH") or None,
        )
    raise ValueError(f"unknown INPUT_BACKEND: {backend!r}")
