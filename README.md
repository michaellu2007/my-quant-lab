# my-quant-lab

基于开源量化框架 [Qbot](https://github.com/UFund-Me/Qbot) 搭建的个人量化工作区：自研的回测 / 条件选股 / 模拟盘面板，配合若干独立策略脚本和每日收盘任务。本仓库**只收录自己写的部分**，Qbot 框架本体不入库。

## 功能一览

| 模块 | 说明 |
|---|---|
| `qbot_bt.py` | 独立启动器：一个窗口挂「我的免费回测 + 条件选股 + 版本回退 + 模拟盘」四个 tab，不改 Qbot 源码 |
| `packs/Qbot/qbot/gui/panels/panel_my_backtest*.py` | 自研免费回测面板及因子面板 |
| `packs/Qbot/qbot/gui/panels/panel_stock_picker.py` | 条件选股面板 |
| `packs/Qbot/qbot/gui/panels/panel_paper_trading.py` | 模拟盘交易面板 |
| `packs/Qbot/qbot/gui/panels/panel_versions.py` / `ui_theme.py` | 版本回退面板 / UI 主题 |
| `scripts/daily_close.py` | 收盘后无 GUI 跑持仓决策清单（卖出条件、仓位建议、事件提示），产出 txt 报告 |
| `scripts/ma_cross_akshare.py`、`scripts/my_cross_tushare.py` | backtrader 双均线策略示例（数据源分别为 akshare / tushare） |
| `run_qbot_bt.bat`、`run_daily_close.bat` | Windows 一键启动 / 定时任务入口 |

## 环境要求

- Windows 10/11（面板是 wxPython GUI，`.bat` 也是 Windows 脚本）
- Python 3.9（对应 Qbot 框架的 `venv39` 虚拟环境；面板里部分脚本引用 `venv38`，两套环境按需安装）
- 依赖（由 Qbot 框架提供）：wxPython、backtrader、pandas、numpy、akshare、baostock、tushare 等，建议直接按 Qbot 官方 README 安装其 requirements

## 部署步骤

1. 安装 Python 3.9，并在 `E:\QUANT` 下创建两个虚拟环境（或按自己路径调整，脚本里的路径硬编码为 `E:\QUANT`，换目录需要同步修改 `qbot_bt.py` 里的 `ROOT` 和各 `.bat`）：

   ```
   cd E:\QUANT
   python -m venv qbot          # 旧环境
   py -3.9 -m venv packs\Qbot\venv39
   ```

2. 克隆 Qbot 框架到 `packs\Qbot`（本仓库不含框架代码）：

   ```
   git clone https://github.com/UFund-Me/Qbot packs\Qbot
   ```

3. 安装依赖：

   ```
   packs\Qbot\venv39\Scripts\pip install -r packs\Qbot\requirements.txt
   packs\Qbot\venv39\Scripts\pip install wxPython backtrader akshare baostock tushare
   ```

4. 用本仓库的面板文件覆盖框架同名目录下的文件（`qbot/gui/panels/` 下的 6 个文件），或直接以本仓库为工作区、把框架克隆进来互补。

## 使用方法

**图形界面**

```
run_qbot_bt.bat        # 或：packs\Qbot\venv39\Scripts\python.exe qbot_bt.py
```

打开后共 4 个 tab：我的免费回测、条件选股、版本与回退、模拟盘交易。

**每日收盘清单（可选，挂 Windows 任务计划）**

```
schtasks /create /tn "Qbot收盘清单" /tr "E:\QUANT\run_daily_close.bat" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:30
```

产出在 `packs\Qbot\qbot\gui\bkt_result\` 下：`signal_report.txt`（持仓明细、仓位占比、卖出条件、技术状态）和 `event_report.txt`（新闻/财报事件，需联网）。

**策略回测脚本**

```
packs\Qbot\venv39\Scripts\python.exe scripts\ma_cross_akshare.py
```

## API Key

本仓库**不存储任何 API key**。tushare token 请自行通过环境变量或在本地未入库的配置文件中设置，例如：

```
set TS_TOKEN=你的token     # 或在脚本中 ts.set_token(os.environ["TS_TOKEN"])
```

数据源：[AKShare](https://github.com/akfamily/akshare)（MIT 协议，免费）、[BaoStock](https://www.baostock.com)（免费，但非标准开源协议，数据版权归其数据来源方，商用需自行确认条款）。

## 目录结构

```
E:\QUANT
├── qbot_bt.py                  # 独立启动器
├── run_qbot_bt.bat             # GUI 一键启动
├── run_daily_close.bat         # 收盘任务入口
├── test_strategy_loading.py    # 策略加载自测
├── scripts/                    # 独立策略与任务脚本
└── packs/Qbot/                 # Qbot 框架（不入库，仅上面板文件）
```

## 协议

本仓库中自己编写的代码以 **MIT License** 发布。`packs/Qbot` 下的面板文件基于 [Qbot](https://github.com/UFund-Me/Qbot)（Apache-2.0）二次开发，同样以 MIT 发布；如需使用请一并遵守上游协议。数据来源的授权见上文「API Key」一节。
