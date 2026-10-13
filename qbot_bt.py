"""只挂「我的免费回测」+ 右侧 4 个 tab 的独立启动器。
不改 Qbot 源码，复用原面板。直接 python qbot_bt.py 启动。
"""
import os
import sys

ROOT = r"E:\QUANT\packs\Qbot"
os.chdir(ROOT)  # 面板里大量相对路径（data/、results/、venv38 脚本）依赖项目根目录
sys.path.insert(0, ROOT)

import wx

from qbot.gui.panels.panel_my_backtest import MyBacktestPanel
from qbot.gui.panels.panel_paper_trading import PaperTradingPanel
from qbot.gui.panels.panel_stock_picker import StockPickerPanel
from qbot.gui.panels.panel_versions import PanelVersions
from qbot.gui.panels.ui_theme import wxs

TABS = [
    ("我的免费回测", MyBacktestPanel),
    (wxs("🔍 条件选股"), StockPickerPanel),
    (wxs("📦 版本与回退"), PanelVersions),
    (wxs("模拟盘交易"), PaperTradingPanel),
]


def main():
    app = wx.App()
    frame = wx.Frame(None, title="回测 + 选股 + 模拟盘", size=(1600, 950))
    frame.SetMinSize((1200, 700))
    nb = wx.Notebook(frame)
    for title, cls in TABS:
        nb.AddPage(cls(nb), title, True)
    frame.Show()
    # 网页式滚轮：下拉框/Spin/滑块不再吃滚轮改值，滚轮只滚页面
    from qbot.gui.gui_utils import page_scroll_only
    n = page_scroll_only(frame)
    print(f"[wheel] {n} 个输入控件的滚轮已改为冒泡到页面")
    app.MainLoop()


if __name__ == "__main__":
    main()
