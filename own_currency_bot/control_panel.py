from __future__ import annotations

import queue
import re
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

import currency_bot as bot


class BotConsole(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OwnCurrencyBot 中控台")
        self.geometry("1040x760")
        self.minsize(900, 680)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.running = threading.Event()
        self.worker_stop = threading.Event()
        self.arb_watch_stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.arb_watcher: threading.Thread | None = None
        self.hotkey_prev = {"F1": False, "F2": False, "F3": False}
        self.status_var = tk.StringVar(value="已暂停")
        self.backend_var = tk.StringVar(value=str(bot.config.get("INPUT_BACKEND", "pydm_driver")))
        self.pairs_value_var = tk.StringVar(value="")
        self.initial_chaos_var = tk.StringVar(value="未初始化")
        self.current_chaos_var = tk.StringVar(value="未初始化")
        self.initial_gold_var = tk.StringVar(value="未初始化")
        self.current_gold_var = tk.StringVar(value="未初始化")
        self.gold_used_var = tk.StringVar(value="未初始化")

        self._install_log_hook()
        self._build_ui()
        self._poll_logs()
        self._poll_hotkeys()
        self._poll_balance()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<F1>", lambda _event: self.start_running())
        self.bind_all("<F2>", lambda _event: self.pause_running())
        self.bind_all("<F3>", lambda _event: self.stop_running())

        self.gui_log("中控台已启动")
        self.gui_log("配置:", bot.CONFIG_FILE)
        self.gui_log("图片:", bot.IMAGES_DIR)
        self.gui_log("输入后端:", self.backend_var.get())

    def _install_log_hook(self) -> None:
        self._original_log = bot.log

        def hooked_log(*parts: object) -> None:
            text = " ".join(str(p) for p in parts)
            self.log_queue.put(text)
            self._original_log(*parts)

        bot.log = hooked_log

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")

        ttk.Label(header, text="OwnCurrencyBot", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status_var, font=("Microsoft YaHei UI", 12)).pack(side="right")

        controls = ttk.LabelFrame(root, text="控制", padding=10)
        controls.pack(fill="x", pady=(12, 8))

        self.start_btn = ttk.Button(controls, text="启动", command=self.toggle_running)
        self.start_btn.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="单次执行", command=self.run_once).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="释放按键", command=self.release_input).grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="清空日志", command=self.clear_logs).grid(row=0, column=3, padx=4, pady=4, sticky="ew")

        ttk.Button(controls, text="资源检查", command=self.check_resources).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="驱动键鼠自测", command=self.input_self_test).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="OCR 自测", command=self.ocr_check).grid(row=1, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="退出", command=self.on_close).grid(row=1, column=3, padx=4, pady=4, sticky="ew")

        ttk.Button(controls, text="窗口检查", command=self.window_check).grid(row=2, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="激活窗口", command=self.activate_game_window).grid(row=2, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="鼠标移到窗口中心", command=self.move_mouse_to_window_center).grid(row=2, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="读取金币", command=self.read_gold).grid(row=2, column=3, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="游戏内价差扫描", command=self.ingame_arbitrage_scan).grid(row=3, column=0, padx=4, pady=4, sticky="ew")
        self.arb_watch_btn = ttk.Button(controls, text="自动检查价差", command=self.toggle_arbitrage_watch)
        self.arb_watch_btn.grid(row=3, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="清除订单状态", command=self.clear_order_state).grid(row=3, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="订单板状态", command=self.order_board_status).grid(row=3, column=3, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="定位检查", command=self.inventory_scan_check).grid(row=4, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="布局Dump", command=self.layout_dump).grid(row=4, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="订单板验证", command=self.order_board_validate).grid(row=4, column=2, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="清订单板", command=self.clear_order_board_state).grid(row=4, column=3, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="启动前预检", command=self.order_board_preflight).grid(row=5, column=0, padx=4, pady=4, sticky="ew")
        ttk.Button(controls, text="初始化余额", command=self.initialize_balance).grid(row=5, column=1, padx=4, pady=4, sticky="ew")

        for i in range(4):
            controls.columnconfigure(i, weight=1)

        info = ttk.LabelFrame(root, text="当前配置", padding=10)
        info.pack(fill="x", pady=(0, 8))

        pairs = ", ".join(bot.active_pair_names()) or "无"
        self.pairs_value_var.set(pairs)
        rows = [
            ("实际交易对", self.pairs_value_var),
            ("基准通货", bot.config.get("BASE_CURRENCY", "")),
            ("输入后端", self.backend_var.get()),
            ("VID/PID", f"{bot.config.get('PYDM_VID', '')} / {bot.config.get('PYDM_PID', '')}"),
            ("匹配阈值", bot.config.get("FIND_THRESHOLD", "")),
            ("DRY_RUN", bot.config.get("DRY_RUN", False)),
            ("初始混沌", self.initial_chaos_var),
            ("当前混沌", self.current_chaos_var),
            ("初始金币", self.initial_gold_var),
            ("当前金币", self.current_gold_var),
            ("已用金币", self.gold_used_var),
        ]
        for i, (k, v) in enumerate(rows):
            ttk.Label(info, text=k + ":").grid(row=i // 3, column=(i % 3) * 2, sticky="w", padx=(0, 4), pady=3)
            if isinstance(v, tk.StringVar):
                ttk.Label(info, textvariable=v).grid(row=i // 3, column=(i % 3) * 2 + 1, sticky="w", padx=(0, 24), pady=3)
            else:
                ttk.Label(info, text=str(v)).grid(row=i // 3, column=(i % 3) * 2 + 1, sticky="w", padx=(0, 24), pady=3)

        flip_frame = ttk.LabelFrame(root, text="混沌石低买高卖配置", padding=8)
        flip_frame.pack(fill="both", expand=False, pady=(0, 8))

        columns = ("name", "buy", "sell", "spread", "gold", "gold_per_d", "template")
        self.flip_tree = ttk.Treeview(flip_frame, columns=columns, show="headings", height=8, selectmode="extended")
        headings = {
            "name": "商品",
            "buy": "买入C",
            "sell": "卖出C",
            "spread": "价差C",
            "gold": "金币/个",
            "gold_per_d": "金币/1D",
            "template": "模板",
        }
        widths = {
            "name": 180,
            "buy": 70,
            "sell": 70,
            "spread": 70,
            "gold": 80,
            "gold_per_d": 90,
            "template": 70,
        }
        for col in columns:
            self.flip_tree.heading(col, text=headings[col])
            self.flip_tree.column(col, width=widths[col], anchor="center")
        flip_scroll = ttk.Scrollbar(flip_frame, orient="vertical", command=self.flip_tree.yview)
        self.flip_tree.configure(yscrollcommand=flip_scroll.set)
        self.flip_tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        flip_scroll.grid(row=0, column=4, sticky="ns")

        ttk.Button(flip_frame, text="应用选中为交易对", command=self.apply_selected_flip_pairs).grid(row=1, column=0, padx=4, pady=(8, 0), sticky="ew")
        ttk.Button(flip_frame, text="应用有模板为交易对", command=self.apply_templated_flip_pairs).grid(row=1, column=1, padx=4, pady=(8, 0), sticky="ew")
        ttk.Button(flip_frame, text="应用全部为交易对", command=self.apply_all_flip_pairs).grid(row=1, column=2, padx=4, pady=(8, 0), sticky="ew")
        ttk.Button(flip_frame, text="刷新列表", command=self.populate_flip_pairs).grid(row=1, column=3, padx=4, pady=(8, 0), sticky="ew")
        ttk.Button(flip_frame, text="扫描这些模板", command=self.ingame_arbitrage_scan).grid(row=1, column=4, padx=4, pady=(8, 0), sticky="ew")
        for i in range(5):
            flip_frame.columnconfigure(i, weight=1)
        flip_frame.rowconfigure(0, weight=1)
        self.populate_flip_pairs()

        log_frame = ttk.LabelFrame(root, text="日志", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, wrap="word", height=18, state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        footer = ttk.Label(root, text="提示：启动后会循环执行 Bot 逻辑；鼠标移动到屏幕角落仍可触发 pyautogui failsafe。")
        footer.pack(fill="x", pady=(8, 0))

    def populate_flip_pairs(self) -> None:
        for item in self.flip_tree.get_children():
            self.flip_tree.delete(item)
        d2c = float(bot.config.get("CHAOS_FLIP_DIVINE_TO_CHAOS", 10.14))
        for pair in bot.config.get("CHAOS_FLIP_PAIRS", []):
            if not isinstance(pair, dict):
                continue
            name = str(pair.get("name", ""))
            buy = float(pair.get("buy_c", 0) or 0)
            sell = float(pair.get("sell_c", 0) or 0)
            gold = int(pair.get("gold", 0) or 0)
            spread = sell - buy
            gold_per_d = "n/a"
            if spread > 0:
                gold_per_d = f"{((gold + sell * 160) / (spread / d2c)):,.0f}"
            template = "有" if bot.template_path(f"{name}.png").exists() else "缺"
            self.flip_tree.insert(
                "",
                "end",
                iid=name,
                values=(
                    name,
                    f"{buy:g}",
                    f"{sell:g}",
                    f"{spread:.2f}",
                    f"{gold:,}",
                    gold_per_d,
                    template,
                ),
            )

    def apply_selected_flip_pairs(self) -> None:
        names = list(self.flip_tree.selection())
        if not names:
            messagebox.showwarning("应用交易对", "请先在表格里选择商品。")
            return
        self.apply_trading_pairs(names)

    def apply_all_flip_pairs(self) -> None:
        names = [str(pair.get("name")) for pair in bot.config.get("CHAOS_FLIP_PAIRS", []) if isinstance(pair, dict) and pair.get("name")]
        self.apply_trading_pairs(names)

    def apply_templated_flip_pairs(self) -> None:
        names = [
            str(pair.get("name"))
            for pair in bot.config.get("CHAOS_FLIP_PAIRS", [])
            if isinstance(pair, dict)
            and pair.get("name")
            and bot.template_path(f"{pair.get('name')}.png").exists()
        ]
        self.apply_trading_pairs(names)

    def apply_trading_pairs(self, names: list[str]) -> None:
        unique = []
        for name in names:
            if name and name not in unique:
                unique.append(name)
        if bool(bot.config.get("ORDER_BOARD_MODE", True)):
            bot.config["ORDER_BOARD_PAIRS"] = [{"name": name} for name in unique]
            self.write_pair_block("ORDER_BOARD_PAIRS", unique)
        else:
            bot.config["TRADING_PAIRS"] = [{"name": name} for name in unique]
            self.write_pair_block("TRADING_PAIRS", unique)
        self.pairs_value_var.set(", ".join(unique) or "无")
        missing = [name for name in unique if not bot.template_path(f"{name}.png").exists()]
        self.gui_log("已应用交易对:", ", ".join(unique) or "无")
        if missing:
            self.gui_log("以下商品缺少模板，实际操作前需要裁图:", ", ".join(missing))
            messagebox.showwarning("已应用交易对", "已写入配置，但这些商品缺少模板：\n" + "\n".join(missing))
        else:
            messagebox.showinfo("已应用交易对", "已写入配置：\n" + "\n".join(unique))

    def write_pair_block(self, key: str, names: list[str]) -> None:
        block = key + " = [\n" + "".join(f'  {{name = "{name}"}},\n' for name in names) + "]"
        text = bot.CONFIG_FILE.read_text(encoding="utf-8")
        new_text, count = re.subn(rf"{re.escape(key)}\s*=\s*\[.*?\]", block, text, count=1, flags=re.S)
        if count == 0:
            new_text = block + "\n\n" + text
        bot.CONFIG_FILE.write_text(new_text, encoding="utf-8")

    def gui_log(self, *parts: object) -> None:
        self.log_queue.put(" ".join(str(p) for p in parts))

    def _poll_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_logs)

    def _poll_hotkeys(self) -> None:
        keys = {
            "F1": bot.key_down(bot.VK_F1),
            "F2": bot.key_down(bot.VK_F2),
            "F3": bot.key_down(bot.VK_F3),
        }
        if keys["F1"] and not self.hotkey_prev["F1"]:
            self.start_running()
        if keys["F2"] and not self.hotkey_prev["F2"]:
            self.pause_running()
        if keys["F3"] and not self.hotkey_prev["F3"]:
            self.stop_running()
        self.hotkey_prev = keys
        self.after(100, self._poll_hotkeys)

    @staticmethod
    def _fmt_amount(value: object) -> str:
        try:
            if value is None:
                return "未知"
            return f"{int(float(value)):,}"
        except (TypeError, ValueError):
            return "未知"

    def refresh_balance_panel(self) -> None:
        data = bot.load_order_board()
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        balance = data.get("last_balance") if isinstance(data.get("last_balance"), dict) else {}
        if not balance:
            balance = bot.order_board_balance_summary(data, refresh_gold=False)
        self.initial_chaos_var.set(self._fmt_amount(session.get("initial_chaos") if isinstance(session, dict) else None))
        self.current_chaos_var.set(self._fmt_amount(balance.get("current_chaos_available")))
        self.initial_gold_var.set(self._fmt_amount(session.get("initial_gold") if isinstance(session, dict) else None))
        self.current_gold_var.set(self._fmt_amount(balance.get("current_gold")))
        self.gold_used_var.set(self._fmt_amount(balance.get("gold_used")))

    def _poll_balance(self) -> None:
        try:
            self.refresh_balance_panel()
        except Exception:
            pass
        self.after(1000, self._poll_balance)

    def _append_log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {line}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def ensure_worker(self) -> None:
        if self.worker and self.worker.is_alive():
            if self.worker_stop.is_set():
                self.worker.join(timeout=0.2)
            if self.worker.is_alive():
                return
        self.worker_stop.clear()
        self.worker = threading.Thread(target=self.worker_loop, name="bot-worker", daemon=True)
        self.worker.start()

    def worker_loop(self) -> None:
        self.gui_log("后台循环线程已启动")
        while not self.worker_stop.is_set():
            if self.running.is_set():
                try:
                    bot.handle_once()
                except bot.BotStopRequested:
                    self.running.clear()
                    self.gui_log("当前操作已停止")
                except Exception:
                    err = traceback.format_exc()
                    self.gui_log("运行异常:", err)
                    try:
                        if bot.input_backend is not None:
                            bot.input_backend.release_all()
                    except Exception:
                        pass
                    time.sleep(1)
            time.sleep(float(bot.config.get("LOOP_DELAY", 0.25)))
        self.gui_log("后台循环线程已停止")

    def toggle_running(self) -> None:
        if self.running.is_set():
            self.pause_running()
            return
        self.start_running()

    def start_running(self) -> None:
        bot.clear_stop_request()
        self.ensure_worker()
        self.running.set()
        self.status_var.set("运行中")
        self.start_btn.configure(text="暂停")
        self.gui_log("已启动 (F1)")

    def pause_running(self) -> None:
        self.running.clear()
        bot.request_stop()
        self.status_var.set("已暂停")
        self.start_btn.configure(text="启动")
        self.release_input()
        self.gui_log("已暂停 (F2)")

    def stop_running(self) -> None:
        self.running.clear()
        bot.request_stop()
        self.worker_stop.set()
        self.status_var.set("已停止")
        self.start_btn.configure(text="启动")
        self.release_input()
        self.gui_log("已停止 (F3)")

    def run_once(self) -> None:
        def task() -> None:
            try:
                self.gui_log("单次执行开始")
                bot.clear_stop_request()
                bot.handle_once()
                self.gui_log("单次执行完成")
            except Exception:
                self.gui_log("单次执行异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def release_input(self) -> None:
        try:
            if bot.input_backend is not None:
                bot.input_backend.release_all()
            self.gui_log("已释放输入")
        except Exception as exc:
            self.gui_log("释放输入失败:", exc)

    def check_resources(self) -> None:
        lines = [
            f"CONFIG_FILE: {bot.CONFIG_FILE} exists={bot.CONFIG_FILE.exists()}",
            f"IMAGES_DIR: {bot.IMAGES_DIR} exists={bot.IMAGES_DIR.exists()}",
            f"LOG_DIR: {bot.LOG_DIR}",
            f"ORDER_BOARD_MODE: {bool(bot.config.get('ORDER_BOARD_MODE', True))}",
            f"ACTIVE_PAIRS: {bot.active_pair_names()}",
            f"ORDER_BOARD_PAIRS: {bot.order_board_pairs()}",
            f"TRADING_PAIRS: {bot.configured_pairs()}",
            f"INPUT_BACKEND: {bot.config.get('INPUT_BACKEND', 'pydm_driver')}",
        ]
        for line in lines:
            self.gui_log(line)
        messagebox.showinfo("资源检查", "\n".join(lines))

    def window_check(self) -> None:
        win = bot.find_window()
        if not win:
            msg = f"未找到窗口: {bot.config.get('GAME_WINDOW_TITLE', '')}"
            self.gui_log(msg)
            messagebox.showwarning("窗口检查", msg)
            return
        lines = [
            f"标题: {win.title}",
            f"句柄: {hex(win.hwnd)}",
            f"矩形: ({win.left}, {win.top}) - ({win.right}, {win.bottom})",
            f"尺寸: {win.width} x {win.height}",
            f"中心: {win.center}",
        ]
        for line in lines:
            self.gui_log(line)
        messagebox.showinfo("窗口检查", "\n".join(lines))

    def activate_game_window(self) -> None:
        win = bot.find_window()
        if not win:
            self.gui_log("激活失败，未找到窗口")
            return
        ok = bot.activate_window(win)
        self.gui_log("激活窗口:", "成功" if ok else "已请求激活/可能被系统限制", win.title)

    def move_mouse_to_window_center(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再做窗口鼠标测试。")
            return
        win = bot.find_window()
        if not win:
            self.gui_log("鼠标测试失败，未找到窗口")
            return
        try:
            bot.activate_window(win)
            x, y = win.center
            bot.get_input().mouse_move(x, y, duration_ms=200)
            self.gui_log("驱动鼠标已移动到窗口中心:", (x, y))
        except Exception:
            self.gui_log("窗口鼠标测试异常:", traceback.format_exc())

    def read_gold(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再读取金币。")
            return

        def task() -> None:
            try:
                self.gui_log("读取金币开始")
                amount, raw = bot.open_inventory_and_read_gold()
                self.gui_log("金币 OCR:", repr(raw))
                self.gui_log("金币数量:", amount)
                if amount is None:
                    messagebox.showwarning("读取金币", f"读取失败，OCR={raw!r}")
                else:
                    data = bot.load_order_board()
                    session = data.get("session") if isinstance(data.get("session"), dict) else {}
                    balance = data.get("last_balance") if isinstance(data.get("last_balance"), dict) else {}
                    initial_gold = session.get("initial_gold") if isinstance(session, dict) else None
                    data["last_balance"] = {
                        **balance,
                        "current_gold": amount,
                        "gold_used": (int(initial_gold) - amount) if isinstance(initial_gold, int) else None,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    bot.save_order_board(data)
                    self.after(0, self.refresh_balance_panel)
                    messagebox.showinfo("读取金币", f"金币数量: {amount:,}")
            except Exception:
                self.gui_log("读取金币异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def initialize_balance(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再初始化余额。")
            return

        def task() -> None:
            try:
                self.gui_log("初始化余额开始")
                data = bot.load_order_board()
                if not bot.ensure_trade_and_inventory_open():
                    self.gui_log("初始化余额失败: 交易界面或背包界面未打开")
                    messagebox.showwarning("初始化余额", "交易界面或背包界面未打开")
                    return
                session = bot.ensure_ledger_session(data)
                if not session.get("ready"):
                    localized = bot.localize_log_data(session)
                    self.gui_log("初始化余额失败:", localized)
                    messagebox.showwarning("初始化余额", f"读取失败: {localized}")
                    return
                totals = bot.refresh_order_board_gold(data)
                self.gui_log(
                    "初始化余额完成:",
                    "混沌=", totals.get("initial_chaos"),
                    "金币=", session.get("initial_gold"),
                )
                self.after(0, self.refresh_balance_panel)
                messagebox.showinfo(
                    "初始化余额",
                    f"初始混沌: {self._fmt_amount(totals.get('initial_chaos'))}\n"
                    f"初始金币: {self._fmt_amount(session.get('initial_gold'))}",
                )
            except Exception:
                self.gui_log("初始化余额异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def order_board_status(self) -> None:
        data = bot.load_order_board()
        self.gui_log("订单板文件:", bot.ORDER_BOARD_FILE)
        self.gui_log("订单板商品:", ", ".join(bot.order_board_pairs()))
        self.gui_log("活跃订单:", bot.active_board_order_count(data))
        self.gui_log("余额:", bot.localize_log_data(bot.order_board_balance_summary(data, refresh_gold=False)))
        for name, order in data.get("orders", {}).items():
            self.gui_log(name, bot.localize_log_data(order))
        messagebox.showinfo("订单板状态", f"活跃订单: {bot.active_board_order_count(data)}")

    def order_board_preflight(self) -> None:
        def task() -> None:
            try:
                self.gui_log("订单板启动前预检开始")
                code = bot.order_board_preflight_report()
                self.gui_log("订单板启动前预检结束:", code)
                messagebox.showinfo("启动前预检", f"完成，返回码: {code}")
            except Exception:
                self.gui_log("订单板启动前预检异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def inventory_scan_check(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再做定位检查。")
            return

        def task() -> None:
            try:
                self.gui_log("定位检查开始")
                code = bot.inventory_scan_check()
                self.gui_log("定位检查结束:", code)
                messagebox.showinfo("定位检查", f"完成，返回码: {code}")
            except Exception:
                self.gui_log("定位检查异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def layout_dump(self) -> None:
        def task() -> None:
            try:
                self.gui_log("布局 dump 开始")
                code = bot.layout_dump()
                self.gui_log("布局 dump 完成:", code)
                messagebox.showinfo("布局Dump", f"已写入 logs，返回码: {code}")
            except Exception:
                self.gui_log("布局 dump 异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def order_board_validate(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再做订单板验证。")
            return

        def task() -> None:
            try:
                self.gui_log("订单板验证开始")
                code = bot.order_board_validate(dry_select=True)
                self.gui_log("订单板验证结束:", code)
                messagebox.showinfo("订单板验证", f"完成，返回码: {code}")
            except Exception:
                self.gui_log("订单板验证异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def ingame_arbitrage_scan(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再扫描游戏内价差。")
            return

        def task() -> None:
            try:
                self.gui_log("游戏内价差扫描开始")
                report = bot.ingame_arbitrage_scan_report()
                for line in report.splitlines():
                    self.gui_log(line)
                messagebox.showinfo("游戏内价差扫描", report)
            except Exception:
                self.gui_log("游戏内价差扫描异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def toggle_arbitrage_watch(self) -> None:
        if self.arb_watcher and self.arb_watcher.is_alive() and not self.arb_watch_stop.is_set():
            self.arb_watch_stop.set()
            self.arb_watch_btn.configure(text="自动检查价差")
            self.gui_log("游戏内价差自动检查已停止")
            return

        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再启动价差自动检查。")
            return

        self.arb_watch_stop.clear()
        self.arb_watch_btn.configure(text="停止价差检查")
        self.arb_watcher = threading.Thread(target=self.arbitrage_watch_loop, daemon=True)
        self.arb_watcher.start()

    def arbitrage_watch_loop(self) -> None:
        interval = float(bot.config.get("INGAME_SCAN_INTERVAL", 60))
        limit = int(bot.config.get("INGAME_SCAN_LIMIT", 4))
        min_spread_pct = float(bot.config.get("INGAME_SCAN_MIN_SPREAD_PCT", 1.0))
        self.gui_log(
            f"游戏内价差自动检查启动 interval={interval}s limit={limit} min_spread={min_spread_pct:.2f}%"
        )
        while not self.arb_watch_stop.is_set():
            if self.running.is_set():
                self.gui_log("Bot 正在运行，价差自动检查等待")
            else:
                try:
                    if not bot.find_image("need.png", threshold=0.8, quiet=True):
                        self.gui_log("未检测到通货交易界面，等待打开...")
                    else:
                        results = bot.scan_ingame_arbitrage(limit=limit)
                        hits = bot.profitable_ingame_rows(results)
                        if hits:
                            self.gui_log(f"发现可套利商品: {len(hits)}")
                            for row in hits:
                                self.gui_log(
                                    f"套利候选 {row['name']}: 买 {float(row['buy_price']):.4f} {row['base']} -> "
                                    f"卖 {float(row['sell_price']):.4f} {row['base']}, "
                                    f"价差 {float(row['spread']):.4f} ({float(row['spread_pct']):.2f}%)"
                                )
                        else:
                            self.gui_log(f"未发现超过 {min_spread_pct:.2f}% 的游戏内价差")
                except Exception:
                    self.gui_log("游戏内价差自动检查异常:", traceback.format_exc())
                    try:
                        if bot.input_backend is not None:
                            bot.input_backend.release_all()
                    except Exception:
                        pass

            self.arb_watch_stop.wait(interval)

        self.arb_watch_btn.configure(text="自动检查价差")

    def input_self_test(self) -> None:
        if self.running.is_set():
            messagebox.showwarning("正在运行", "请先暂停 Bot，再做驱动键鼠自测。")
            return

        def task() -> None:
            try:
                self.gui_log("驱动键鼠自测开始")
                ok = bot.input_self_test()
                self.gui_log("驱动键鼠自测:", "通过" if ok else "失败")
            except Exception:
                self.gui_log("驱动键鼠自测异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def ocr_check(self) -> None:
        def task() -> None:
            try:
                import numpy as np

                self.gui_log("OCR 自测开始")
                img = np.full((40, 180), 255, dtype=np.uint8)
                result = bot.get_ocr()(img, use_det=False, use_cls=False)
                self.gui_log("OCR 自测通过:", result)
            except Exception:
                self.gui_log("OCR 自测异常:", traceback.format_exc())

        threading.Thread(target=task, daemon=True).start()

    def clear_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def clear_order_state(self) -> None:
        bot.clear_order_state()
        self.gui_log("已清除当前订单状态")
        messagebox.showinfo("清除订单状态", "已清除 logs/order_state.json")

    def clear_order_board_state(self) -> None:
        bot.clear_order_board_state()
        self.gui_log("已清除订单板状态")
        messagebox.showinfo("清订单板", "已清除 logs/order_board.json")

    def on_close(self) -> None:
        self.running.clear()
        bot.request_stop()
        self.worker_stop.set()
        self.arb_watch_stop.set()
        self.release_input()
        self.after(150, self.destroy)


def main() -> None:
    app = BotConsole()
    app.mainloop()


if __name__ == "__main__":
    main()
