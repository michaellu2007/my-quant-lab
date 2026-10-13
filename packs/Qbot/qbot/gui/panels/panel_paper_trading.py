"""
Author: ZCode
Date: 2026-08-31
Description: 模拟盘交易面板 - 登录/注册 + 下单撮合 + 持仓/成交/净值展示。
             撮合价来自本地行情缓存，断网可用。
"""

import wx
import pandas as pd

from qbot.common.logging.logger import LOGGER as logger
from qbot.core.paper_broker import PaperBroker, latest_price, normalize_code


class EquityChartPanel(wx.Panel):
    """账户净值曲线（matplotlib 本地渲染）"""

    BG = '#131722'
    FG = '#d1d4dc'
    GRID = '#2B2B43'
    ACCENT = '#2962ff'

    def __init__(self, parent, height=2.0):
        super().__init__(parent)
        self._canvas = None
        self._fig = None
        self._height = height
        self.SetSizer(wx.BoxSizer(wx.VERTICAL))

    def _ensure_canvas(self):
        import matplotlib
        matplotlib.use('WxAgg')
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg
        import matplotlib.font_manager as fm
        for name in ('Microsoft YaHei', 'SimHei', 'PingFang SC', 'Noto Sans CJK SC'):
            if any(name in f.name for f in fm.fontManager.ttflist):
                matplotlib.rcParams['font.sans-serif'] = [name]
                break
        matplotlib.rcParams['axes.unicode_minus'] = False
        if self._canvas is not None:
            return self._canvas
        self._fig = Figure(figsize=(6, self._height), dpi=100, facecolor=self.BG)
        self._canvas = FigureCanvasWxAgg(self, -1, self._fig)
        self.GetSizer().Add(self._canvas, 1, wx.EXPAND)
        self.Layout()
        return self._canvas

    def show_curve(self, curve, init_cash):
        self._ensure_canvas()
        self._fig.clear()
        ax = self._fig.add_subplot(111)
        ax.set_facecolor(self.BG)
        if curve:
            xs = [p[0] for p in curve]
            ys = [p[1] for p in curve]
            ax.plot(range(len(ys)), ys, color=self.ACCENT, linewidth=1.4)
            ax.axhline(init_cash, color='#787b86', linewidth=0.8,
                       linestyle='--', label=f'初始资金 {init_cash:,.0f}')
            ax.legend(fontsize=8, facecolor=self.BG, edgecolor=self.GRID, labelcolor=self.FG)
            step = max(1, len(xs) // 8)
            ax.set_xticks(range(0, len(xs), step))
            ax.set_xticklabels([xs[i][5:] for i in range(0, len(xs), step)], fontsize=7)
        else:
            ax.text(0.5, 0.5, '暂无净值记录（点击「刷新对账」生成）',
                    ha='center', va='center', color=self.FG, transform=ax.transAxes)
        ax.set_title('账户净值', color=self.FG, fontsize=10)
        ax.tick_params(colors=self.FG, labelsize=8)
        for s in ax.spines.values():
            s.set_color(self.GRID)
        ax.grid(True, color=self.GRID, linewidth=0.4, alpha=0.6)
        self._fig.tight_layout()
        self._canvas.draw()


class PaperTradingPanel(wx.Panel):
    """模拟盘交易面板"""

    def __init__(self, parent):
        super().__init__(parent)
        self.broker = PaperBroker()
        self._init_cash = 100000.0

        root = wx.BoxSizer(wx.VERTICAL)

        # ===== 登录区 =====
        login_box = wx.StaticBox(self, -1, "账户登录（本机加密存储，不联网）")
        lb = wx.StaticBoxSizer(login_box, wx.HORIZONTAL)
        lb.Add(wx.StaticText(login_box, label="用户名:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self.user_input = wx.TextCtrl(login_box, size=(120, -1))
        lb.Add(self.user_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        lb.Add(wx.StaticText(login_box, label="密码:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        self.pwd_input = wx.TextCtrl(login_box, size=(120, -1), style=wx.TE_PASSWORD)
        lb.Add(self.pwd_input, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        self.login_but = wx.Button(login_box, -1, "登录")
        self.reg_but = wx.Button(login_box, -1, "注册(初始10万)")
        self.logout_but = wx.Button(login_box, -1, "登出")
        self.logout_but.Enable(False)
        self.login_but.Bind(wx.EVT_BUTTON, self.on_login)
        self.reg_but.Bind(wx.EVT_BUTTON, self.on_register)
        self.logout_but.Bind(wx.EVT_BUTTON, self.on_logout)
        lb.Add(self.login_but, 0, wx.LEFT, 10)
        lb.Add(self.reg_but, 0, wx.LEFT, 4)
        lb.Add(self.logout_but, 0, wx.LEFT, 4)
        self.user_status = wx.StaticText(login_box, label="未登录")
        self.user_status.SetForegroundColour(wx.Colour(150, 60, 60))
        lb.Add(self.user_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        root.Add(lb, 0, wx.EXPAND | wx.ALL, 5)

        main = wx.BoxSizer(wx.HORIZONTAL)

        # ===== 左：下单区 =====
        order_box = wx.StaticBox(self, -1, "委托下单（市价=本地缓存最新收盘±0.1%滑点）")
        ob = wx.StaticBoxSizer(order_box, wx.VERTICAL)
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(order_box, label="代码:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.code_input = wx.TextCtrl(order_box, value="600000", size=(100, -1))
        row1.Add(self.code_input, 0)
        row1.Add(wx.StaticText(order_box, label="股数:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 4)
        self.shares_input = wx.TextCtrl(order_box, value="100", size=(70, -1))
        row1.Add(self.shares_input, 0)
        ob.Add(row1, 0, wx.ALL, 5)

        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(order_box, label="限价(留空=市价):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.price_input = wx.TextCtrl(order_box, value="", size=(90, -1))
        row2.Add(self.price_input, 0)
        self.price_hint = wx.StaticText(order_box, label="行情: -")
        self.price_hint.SetForegroundColour(wx.Colour(100, 100, 100))
        row2.Add(self.price_hint, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        ob.Add(row2, 0, wx.ALL, 5)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.buy_but = wx.Button(order_box, -1, "买入")
        self.buy_but.SetBackgroundColour(wx.Colour(200, 60, 60))
        self.buy_but.SetForegroundColour(wx.Colour(255, 255, 255))
        self.sell_but = wx.Button(order_box, -1, "卖出")
        self.sell_but.SetBackgroundColour(wx.Colour(30, 140, 90))
        self.sell_but.SetForegroundColour(wx.Colour(255, 255, 255))
        self.buy_but.Bind(wx.EVT_BUTTON, lambda e: self.on_order('buy'))
        self.sell_but.Bind(wx.EVT_BUTTON, lambda e: self.on_order('sell'))
        btn_row.Add(self.buy_but, 1, wx.RIGHT, 4)
        btn_row.Add(self.sell_but, 1)
        ob.Add(btn_row, 0, wx.EXPAND | wx.ALL, 5)

        self.order_status = wx.StaticText(order_box, label="（登录后可下单）")
        self.order_status.SetForegroundColour(wx.Colour(100, 100, 100))
        self.order_status.Wrap(250)
        ob.Add(self.order_status, 0, wx.ALL, 5)

        self.refresh_but = wx.Button(order_box, -1, "刷新对账（按最新缓存行情估值+记录净值）")
        self.refresh_but.Bind(wx.EVT_BUTTON, lambda e: self.refresh_all())
        ob.Add(self.refresh_but, 0, wx.EXPAND | wx.ALL, 5)

        main.Add(ob, 0, wx.EXPAND | wx.ALL, 5)

        # ===== 右：资产/持仓/成交/净值 =====
        right = wx.BoxSizer(wx.VERTICAL)

        self.asset_label = wx.StaticText(self, label="总资产: --   可用现金: --   持仓市值: --")
        f = self.asset_label.GetFont()
        f.SetPointSize(f.GetPointSize() + 1)
        f = f.Bold()
        self.asset_label.SetFont(f)
        right.Add(self.asset_label, 0, wx.ALL, 6)

        right.Add(wx.StaticText(self, label="持仓:"), 0, wx.LEFT, 6)
        self.pos_list = wx.ListCtrl(self, style=wx.LC_REPORT, size=(-1, 110))
        for i, (col, w) in enumerate([("代码", 95), ("股数", 70), ("成本", 70),
                                      ("现价", 70), ("市值", 95), ("盈亏%", 70)]):
            self.pos_list.InsertColumn(i, col, width=w)
        right.Add(self.pos_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        right.Add(wx.StaticText(self, label="成交记录（最近 50 笔）:"), 0, wx.LEFT, 6)
        self.trade_list = wx.ListCtrl(self, style=wx.LC_REPORT, size=(-1, 150))
        for i, (col, w) in enumerate([("时间", 130), ("代码", 90), ("方向", 50),
                                      ("股数", 60), ("价格", 70), ("金额", 90),
                                      ("费用", 70), ("余现金", 95)]):
            self.trade_list.InsertColumn(i, col, width=w)
        right.Add(self.trade_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        self.equity_chart = EquityChartPanel(self, height=1.8)
        right.Add(self.equity_chart, 1, wx.EXPAND | wx.ALL, 6)

        main.Add(right, 1, wx.EXPAND)
        root.Add(main, 1, wx.EXPAND)
        self.SetSizer(root)

        self._set_order_enable(False)
        self.refresh_all()

    # ===== 登录相关 =====
    def on_register(self, event):
        dlg = wx.TextEntryDialog(self, "设置初始资金（元）:", "注册", "100000")
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        try:
            init_cash = float(dlg.GetValue())
        except ValueError:
            init_cash = 100000.0
        dlg.Destroy()
        ok, msg = self.broker.register(self.user_input.GetValue(),
                                       self.pwd_input.GetValue(), init_cash)
        self._flash(msg, ok)
        if ok:
            self._init_cash = init_cash

    def on_login(self, event):
        ok, msg = self.broker.login(self.user_input.GetValue(),
                                    self.pwd_input.GetValue())
        self._flash(msg, ok)
        if ok:
            self.user_status.SetLabel(f"已登录: {self.broker.current_user}")
            self.user_status.SetForegroundColour(wx.Colour(30, 140, 90))
            self._set_order_enable(True)
            self.logout_but.Enable(True)
            self.login_but.Enable(False)
            self.reg_but.Enable(False)
            # 初始资金从曲线首点取
            curve = self.broker.account.get('equity_curve') if self.broker.account else None
            if curve:
                self._init_cash = curve[0][1]
            self.refresh_all()

    def on_logout(self, event):
        self.broker.logout()
        self.user_status.SetLabel("未登录")
        self.user_status.SetForegroundColour(wx.Colour(150, 60, 60))
        self._set_order_enable(False)
        self.logout_but.Enable(False)
        self.login_but.Enable(True)
        self.reg_but.Enable(True)
        self.refresh_all()

    def _set_order_enable(self, enabled):
        for c in (self.code_input, self.shares_input, self.price_input,
                  self.buy_but, self.sell_but, self.refresh_but):
            c.Enable(enabled)

    def _flash(self, msg, ok=True):
        self.order_status.SetLabel(msg)
        self.order_status.SetForegroundColour(
            wx.Colour(30, 140, 90) if ok else wx.Colour(200, 60, 60))

    # ===== 下单 =====
    def on_order(self, side):
        code = self.code_input.GetValue().strip()
        try:
            shares = int(self.shares_input.GetValue())
        except ValueError:
            self._flash("股数必须是整数", False)
            return
        price_text = self.price_input.GetValue().strip()
        try:
            price = float(price_text) if price_text else None
        except ValueError:
            self._flash("限价必须是数字（留空=市价）", False)
            return
        ok, msg = self.broker.place_order(code, side, shares, price)
        self._flash(msg, ok)
        if ok:
            self.refresh_all()
            # 更新行情提示
            p, d = latest_price(code)
            if p:
                self.price_hint.SetLabel(f"行情: {p:.2f} ({d})")

    # ===== 刷新展示 =====
    def refresh_all(self):
        acc = self.broker.account
        # 行情提示
        code = normalize_code(self.code_input.GetValue()) if self.code_input.GetValue().strip() else None
        if code:
            p, d = latest_price(code)
            self.price_hint.SetLabel(f"行情: {p:.2f} ({d})" if p else "行情: 本地无缓存")
        if not acc:
            self.asset_label.SetLabel("总资产: --   可用现金: --   持仓市值: --")
            self.pos_list.DeleteAllItems()
            self.trade_list.DeleteAllItems()
            self.equity_chart.show_curve([], self._init_cash)
            return

        mv, prices = self.broker.positions_value()
        total = acc['cash'] + mv
        self.asset_label.SetLabel(
            f"总资产: ¥{total:,.2f}   可用现金: ¥{acc['cash']:,.2f}   持仓市值: ¥{mv:,.2f}")

        # 持仓表
        self.pos_list.DeleteAllItems()
        for code_, pos in acc['positions'].items():
            if pos['shares'] <= 0:
                continue
            price = prices.get(code_, pos['avg_cost'])
            value = pos['shares'] * price
            pnl = (price / pos['avg_cost'] - 1) * 100 if pos['avg_cost'] else 0
            idx = self.pos_list.InsertItem(self.pos_list.GetItemCount(), code_)
            self.pos_list.SetItem(idx, 1, str(pos['shares']))
            self.pos_list.SetItem(idx, 2, f"{pos['avg_cost']:.2f}")
            self.pos_list.SetItem(idx, 3, f"{price:.2f}")
            self.pos_list.SetItem(idx, 4, f"{value:,.0f}")
            self.pos_list.SetItem(idx, 5, f"{pnl:+.1f}%")

        # 成交表（最近 50 笔倒序）
        self.trade_list.DeleteAllItems()
        for t in reversed(acc['trades'][-50:]):
            idx = self.trade_list.InsertItem(self.trade_list.GetItemCount(), t['time'])
            self.trade_list.SetItem(idx, 1, t['code'])
            self.trade_list.SetItem(idx, 2, "买入" if t['side'] == 'buy' else "卖出")
            self.trade_list.SetItem(idx, 3, str(t['shares']))
            self.trade_list.SetItem(idx, 4, f"{t['price']:.2f}")
            self.trade_list.SetItem(idx, 5, f"{t['amount']:,.0f}")
            self.trade_list.SetItem(idx, 6, f"{t['fee']:.2f}")
            self.trade_list.SetItem(idx, 7, f"{t['cash_after']:,.0f}")

        # 净值曲线（含今日）
        eq = self.broker.mark_and_settle()
        self.equity_chart.show_curve(acc['equity_curve'], self._init_cash)
        self.Layout()
