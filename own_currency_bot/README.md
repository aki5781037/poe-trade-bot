# OwnCurrencyBot

这是重新写的通货交易 Bot，不包含原程序的授权验证逻辑。

## 使用

已打包版本：

`own_currency_bot\dist\OwnCurrencyBot\OwnCurrencyBot.exe`

中控台版本：

`own_currency_bot\dist\OwnCurrencyBotConsole\OwnCurrencyBotConsole.exe`

双击 `OwnCurrencyBot.exe` 可运行命令行版。游戏交易界面准备好后，在控制台按提示使用：

- `F6`：启动 / 暂停
- `Ctrl+C`：退出程序

如果使用中控台版本，双击 `OwnCurrencyBotConsole.exe`，在窗口里点击“启动 / 暂停”。中控台还提供“资源检查”“驱动键鼠自测”“OCR 自测”“窗口检查”“激活窗口”“鼠标到窗口中心”“读取金币”和实时日志窗口。

源码运行版本：

双击 `run_bot.bat`。第一次运行会创建 `.venv` 并安装依赖，可能需要几分钟。

## 检查

在 `dist\OwnCurrencyBot` 目录中可以运行：

```bat
OwnCurrencyBot.exe --check
OwnCurrencyBot.exe --window-check
OwnCurrencyBot.exe --window-mouse-test
OwnCurrencyBot.exe --input-check
OwnCurrencyBot.exe --input-self-test
OwnCurrencyBot.exe --ocr-check
OwnCurrencyBot.exe --gold-check
```

`--check` 只检查配置、图片、交易对和输入后端；`--window-check` 检查游戏窗口绑定；`--window-mouse-test` 激活游戏窗口并把鼠标移动到窗口中心，不点击不按键；`--input-check` 只初始化输入后端并释放按键，不会移动或点击；`--input-self-test` 会打开本地测试窗口，使用当前输入后端输入 `abc` 并点击按钮；`--ocr-check` 只初始化 OCR，不会进入自动点击循环；`--gold-check` 会读取游戏窗口，如果金币区域不可见，会用背包键打开背包后识别金币数量。

## 背包金币读取

当前背包键和金币裁剪区域在 `config.toml` 中配置：

```toml
INVENTORY_KEY = "i"
GOLD_CROP_X = 585
GOLD_CROP_Y = 642
GOLD_CROP_W = 90
GOLD_CROP_H = 28
```

裁剪坐标相对于游戏窗口左上角。当前已在 `Path of Exile 2` 窗口测试通过，打包版输出：

```text
Gold OCR raw: '3,358,113'
Gold amount: 3358113
```

如果游戏分辨率、UI 缩放或背包位置变化，需要重新校准这组裁剪坐标。

## 输入后端

当前默认使用 PyDmGame/YJS 驱动级键鼠：

```toml
INPUT_BACKEND = "pydm_driver"
PYDM_VID = "0xC216"
PYDM_PID = "0x0301"
PYDM_MOUSE_ENABLED = true
PYDM_MOVE_MODE = "instant"
PYDM_MOVE_STEP_MS = 10
PYDM_DLL_PATH = ""
```

驱动 DLL 已放在：

`own_currency_bot\drivers\msdk.dll`

打包后的 exe 内部也会携带：

`dist\OwnCurrencyBot\_internal\drivers\msdk.dll`

如果要临时切回普通用户层输入，可以把配置改成：

```toml
INPUT_BACKEND = "pyautogui"
```

## 打包成 exe

双击 `build_exe.bat`。生成位置：

`own_currency_bot\dist\OwnCurrencyBot\OwnCurrencyBot.exe`

双击 `build_console.bat` 可打包中控台。生成位置：

`own_currency_bot\dist\OwnCurrencyBotConsole\OwnCurrencyBotConsole.exe`

打包时会把上级目录的 `config.toml` 和 `images` 一起复制进去。

## 配置

默认读取程序所在目录的 `config.toml`，其次读取打包内置配置，再其次读取上级目录的 `config.toml`。

打包后的 exe 已内置当前 `config.toml` 和 `images`。如果要改配置，可以把新的 `config.toml` 放到 `OwnCurrencyBot.exe` 同级目录，它会优先读取外部文件。

继续使用现有配置项：

- `BASE_CURRENCY`
- `TRADING_PAIRS`
- `TITLE_OFFSET_X`
- `TITLE_OFFSET_Y`
- `CROP_W`
- `CROP_H`
- `INPUT_LEFT_OFFSET_X`
- `INPUT_RIGHT_OFFSET_X`
- `INPUT_OFFSET_Y`
- `NEED_OFFSET_Y`
- `NEED_RIGHT_OFFSET_X`
- `STOCK_BUY_X1`
- `STOCK_BUY_X2`
- `STOCK_SELL_X1`
- `STOCK_SELL_X2`
- `MAX_LIMIT`
- `BUY_REDUCE_MIN`
- `BUY_REDUCE_MAX`
- `SELL_PROFIT_MIN`
- `SELL_PROFIT_MAX`
- `FIND_THRESHOLD`

运行日志会写到 `own_currency_bot\logs`。
