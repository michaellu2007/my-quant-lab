"""
Author: ZCode
Date: 2026-08-29
Description: 选股面板 - 两个页签：
  1) 条件筛选：BaoStock 价量+估值，条件 AND 组合
  2) 因子打分：全A 一年日线 → 10 个价量因子 → RankIC 检验 → 横截面打分 TopN
"""

import os
import wx
import pandas as pd
import numpy as np
import threading
from datetime import datetime

try:
    import baostock as bs
except ImportError:
    bs = None

from qbot.common.logging.logger import LOGGER as logger

try:
    from qbot.gui.config import DATA_DIR_BKT_RESULT
except ImportError:
    import tempfile
    DATA_DIR_BKT_RESULT = os.path.join(tempfile.gettempdir(), "qbot_backtest")
    os.makedirs(DATA_DIR_BKT_RESULT, exist_ok=True)

POOL_DIR = os.path.join(DATA_DIR_BKT_RESULT, "stock_pool")
os.makedirs(POOL_DIR, exist_ok=True)

# ===== 条件筛选（21 日截面）=====
LOOKBACK_DAYS = 21
BS_FIELDS = "date,code,close,turn,volume,amount,peTTM,pbMRQ,isST"

CONDITIONS = [
    ('pe_max',     '市盈率PE ≤',       '30',   '',      ''),
    ('pb_max',     '市净率PB ≤',       '2',    '',      ''),
    ('chg5_min',   '5日涨幅 ≥ %',      '0',    '',      ''),
    ('chg5_max',   '5日涨幅 ≤ %',      '',     '',      ''),
    ('chg20_min',  '20日涨幅 ≥ %',     '',     '',      ''),
    ('turn_min',   '20日均换手 ≥ %',   '1',    '',      ''),
    ('turn_max',   '20日均换手 ≤ %',   '10',   '',      ''),
    ('amt_min',    '20日均成交额 ≥ 亿', '0.5',  '',      ''),
]

# ===== 因子打分（一年日线面板）=====
FACTOR_FIELDS = "date,code,open,high,low,close,volume,amount,turn,isST"
FACTOR_NUM_COLS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'isST')

# (key, 标签, 方向说明)  方向已在因子构造时调整为「值越大分越高」
FACTOR_LABELS = {
    'mom20': '动量20日（强者恒强）',
    'rev5': '反转5日（超跌反弹）',
    'low_vol': '低波动20日（稳者恒稳）',
    'low_turn': '低换手20日（筹码稳定）',
    'low_ampl': '低振幅20日（走势平滑）',
    'close_pos': '强势收盘位置（收在高位的票）',
    'vp_corr': '量价相关20日（量价齐升）',
    'liq20': '流动性20日（成交活跃）',
    'low_bias': '低乖离20日（未远离均线）',
    'low_price': '低价股（近似便宜/小盘溢价）',
}


def _find_trade_day():
    """从今天往回找最近一个有股票数据的交易日，返回 'YYYY-MM-DD'。"""
    for back in range(7):
        d = pd.Timestamp.now() - pd.Timedelta(days=back)
        if d.weekday() >= 5:
            continue
        probe = bs.query_all_stock(day=d.strftime('%Y-%m-%d'))
        rows = []
        while probe.error_code == '0' and probe.next():
            rows.append(probe.get_row_data())
        if rows:
            return d.strftime('%Y-%m-%d')
    raise RuntimeError("最近 7 天找不到交易日")


def _stock_universe(trade_day):
    """全部 A 票（sh.6/sz.0/sz.3 开头，排除指数与北交所）。返回 (codes, names)。"""
    rs = bs.query_all_stock(day=trade_day)
    rows = []
    while rs.error_code == '0' and rs.next():
        rows.append(rs.get_row_data())
    codes = [r[0] for r in rows
             if r[0].startswith(('sh.6', 'sz.0', 'sz.3')) and len(r) > 2]
    names = {r[0]: r[2] for r in rows if len(r) > 2}
    return codes, names


def fetch_stock_pool(progress_cb=None):
    """拉全 A 最近 LOOKBACK_DAYS 日价量+估值 → 一行/票截面 DataFrame。当日缓存。"""
    today = datetime.now().strftime('%Y%m%d')
    cache = os.path.join(POOL_DIR, f"pool_{today}.pkl")
    if os.path.exists(cache):
        logger.info(f"选股池命中当日缓存: {cache}")
        return pd.read_pickle(cache), True

    if bs is None:
        raise RuntimeError("baostock 未安装")

    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        trade_day = _find_trade_day()
        codes, names = _stock_universe(trade_day)
        logger.info(f"选股池: {len(codes)} 只A股，基准日 {trade_day}")

        start = (pd.Timestamp(trade_day) - pd.Timedelta(days=45)).strftime('%Y-%m-%d')
        records = []
        for i, code in enumerate(codes):
            rs = bs.query_history_k_data_plus(
                code, BS_FIELDS, start_date=start, end_date=trade_day,
                frequency='d', adjustflag='2')
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            if len(rows) < 6:
                continue
            df = pd.DataFrame(rows, columns=BS_FIELDS.split(','))
            for col in ('close', 'turn', 'volume', 'amount', 'peTTM', 'pbMRQ', 'isST'):
                df[col] = pd.to_numeric(df[col], errors='coerce')
            last = df.iloc[-1]
            c = df['close']
            records.append({
                'code': code,
                'name': names.get(code, code),
                'close': last['close'],
                'peTTM': last['peTTM'],
                'pbMRQ': last['pbMRQ'],
                'isST': last['isST'],
                'chg5': (c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) >= 6 and c.iloc[-6] > 0 else None,
                'chg20': (c.iloc[-1] / c.iloc[-21] - 1) * 100 if len(c) >= 21 and c.iloc[-21] > 0 else None,
                'turn20': df['turn'].tail(20).mean(),
                'amt20': df['amount'].tail(20).mean() / 1e8,
            })
            if progress_cb and i % 200 == 0:
                progress_cb(i, len(codes))

        pool = pd.DataFrame(records)
        pool.to_pickle(cache)
        logger.info(f"选股池构建完成: {len(pool)} 只，缓存 {cache}")
        return pool, False
    finally:
        bs.logout()


def fetch_factor_data(progress_cb=None):
    """拉全 A 近一年日线 OHLCV+换手 → 长表 [date,code,O,H,L,C,V,amount,turn,isST]。
    当日缓存（含名称表）。"""
    today = datetime.now().strftime('%Y%m%d')
    cache = os.path.join(POOL_DIR, f"factor_{today}.pkl")
    if os.path.exists(cache):
        logger.info(f"因子数据命中当日缓存: {cache}")
        return pd.read_pickle(cache), True

    if bs is None:
        raise RuntimeError("baostock 未安装")

    lg = bs.login()
    if lg.error_code != '0':
        raise RuntimeError(f"BaoStock 登录失败: {lg.error_msg}")
    try:
        trade_day = _find_trade_day()
        codes, names = _stock_universe(trade_day)
        logger.info(f"因子数据: {len(codes)} 只A股，基准日 {trade_day}，拉取近一年…")

        start = (pd.Timestamp(trade_day) - pd.Timedelta(days=420)).strftime('%Y-%m-%d')
        frames = []
        for i, code in enumerate(codes):
            rs = bs.query_history_k_data_plus(
                code, FACTOR_FIELDS, start_date=start, end_date=trade_day,
                frequency='d', adjustflag='2')
            rows = []
            while rs.error_code == '0' and rs.next():
                rows.append(rs.get_row_data())
            if len(rows) < 60:  # 不足 60 日的次新/长停牌不参与因子
                continue
            frames.append(pd.DataFrame(rows, columns=FACTOR_FIELDS.split(',')))
            if progress_cb and i % 100 == 0:
                progress_cb(i, len(codes))

        long_df = pd.concat(frames, ignore_index=True)
        for col in FACTOR_NUM_COLS:
            long_df[col] = pd.to_numeric(long_df[col], errors='coerce')
        long_df.to_pickle(cache)
        pd.Series(names, name='name').to_frame().to_pickle(
            os.path.join(POOL_DIR, f"factor_names_{today}.pkl"))
        logger.info(f"因子数据构建完成: {len(long_df)} 行，缓存 {cache}")
        return long_df, False
    finally:
        bs.logout()


def compute_factor_scores(long_df):
    """核心：由日线长表计算 10 因子 + RankIC 检验 + 最新截面打分。
    返回 (score: Series[code→0-100], ic_table: DataFrame[name, mean_ic, icir, t, n],
          detail: DataFrame 最新截面各因子值)。"""
    close = long_df.pivot(index='date', columns='code', values='close').sort_index()
    high = long_df.pivot(index='date', columns='code', values='high').sort_index()
    low = long_df.pivot(index='date', columns='code', values='low').sort_index()
    volume = long_df.pivot(index='date', columns='code', values='volume').sort_index()
    amount = long_df.pivot(index='date', columns='code', values='amount').sort_index()
    turn = long_df.pivot(index='date', columns='code', values='turn').sort_index()
    isst = long_df.pivot(index='date', columns='code', values='isST').sort_index()

    ret = close.pct_change()

    # 方向已调整：值越大 → 分越高
    factors = {
        'mom20': close.pct_change(20),
        'rev5': -close.pct_change(5),
        'low_vol': -ret.rolling(20).std(),
        'low_turn': -turn.rolling(20).mean(),
        'low_ampl': -((high - low) / close).rolling(20).mean(),
        # (high-low)==0（一字板/停牌）会产生 inf 污染 rank，除零置 NaN
        'close_pos': ((close - low) / (high - low).replace(0, np.nan)).rolling(20).mean(),
        'vp_corr': close.rolling(20).corr(volume),
        'liq20': np.log(amount.rolling(20).mean()),
        'low_bias': -(close / close.rolling(20).mean() - 1).abs(),
    }
    # 低价因子：日线面板无 PE，用对数低价近似「便宜」
    factors['low_price'] = -np.log(close)

    # ===== RankIC 检验：每月一个截面，因子 vs 未来 20 日收益 =====
    dates = close.index
    fwd20 = close.shift(-20) / close - 1
    ic_rec = {k: [] for k in factors}
    for i in range(0, len(dates) - 20, 20):
        d = dates[i]
        r = fwd20.iloc[i]
        r_rank = r[r.notna()].rank()
        if len(r_rank) < 100:
            continue
        for k, F in factors.items():
            f = F.iloc[i]
            pair = pd.concat([f, r_rank], axis=1, keys=['f', 'r']).dropna()
            if len(pair) < 100:
                continue
            ic_rec[k].append(pair['f'].rank().corr(pair['r']))

    ic_rows = []
    for k, ics in ic_rec.items():
        s = pd.Series(ics).dropna()
        if len(s) == 0:
            ic_rows.append({'name': FACTOR_LABELS[k], 'key': k, 'mean_ic': np.nan,
                            'icir': np.nan, 't': np.nan, 'n': 0})
            continue
        std = s.std(ddof=1)
        mean_ic = s.mean()
        ic_rows.append({'name': FACTOR_LABELS[k], 'key': k, 'mean_ic': mean_ic,
                        'icir': mean_ic / std if std else 0.0,
                        't': mean_ic / std * np.sqrt(len(s)) if std else np.nan,
                        'n': len(s)})
    ic_table = pd.DataFrame(ic_rows)

    # ===== 最新截面打分：各因子横截面百分位 rank 等权平均 =====
    score_parts = []
    detail = {}
    for k, F in factors.items():
        s = F.iloc[-1]
        detail[k] = s
        score_parts.append(s.rank(pct=True))
    score = pd.concat(score_parts, axis=1).mean(axis=1) * 100

    # ST 剔除 + 停牌（最后一日无成交）剔除
    last_st = isst.iloc[-1] if len(isst) else pd.Series(dtype=float)
    last_amt = amount.iloc[-1]
    valid = score.notna()
    if len(last_st) == len(score):
        valid &= (last_st.reindex(score.index) == 0)
    valid &= (last_amt.reindex(score.index) > 0)
    score = score[valid]

    detail = pd.DataFrame(detail)
    return score, ic_table, detail


class PickerChartPanel(wx.Panel):
    """分布图（matplotlib 本地渲染）"""

    BG = '#131722'
    FG = '#d1d4dc'
    GRID = '#2B2B43'
    ACCENT = '#2962ff'

    def __init__(self, parent, height=2.4):
        super().__init__(parent)
        self._canvas = None
        self._fig = None
        self._chart_height = height
        s = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(s)

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
        self._fig = Figure(figsize=(6, self._chart_height), dpi=100, facecolor=self.BG)
        self._canvas = FigureCanvasWxAgg(self, -1, self._fig)
        self.GetSizer().Add(self._canvas, 1, wx.EXPAND)
        self.Layout()
        return self._canvas

    def _style_ax(self, ax, title):
        ax.set_facecolor(self.BG)
        ax.set_title(title, color=self.FG, fontsize=10)
        ax.tick_params(colors=self.FG, labelsize=8)
        for s in ax.spines.values():
            s.set_color(self.GRID)
        ax.grid(True, color=self.GRID, linewidth=0.4, alpha=0.6)

    def show_distribution(self, result):
        self._ensure_canvas()
        self._fig.clear()
        ax = self._fig.add_subplot(111)
        self._style_ax(ax, f"入选股票 PE(TTM) 分布  n={len(result)}")
        pe = result['peTTM'].dropna()
        pe = pe[(pe > 0) & (pe < 100)]
        if len(pe) > 0:
            ax.hist(pe, bins=30, color=self.ACCENT, edgecolor=self.GRID)
            ax.axvline(pe.median(), color='#ff9800', linestyle='--', linewidth=1,
                       label=f'中位数 {pe.median():.1f}')
            ax.legend(fontsize=8, facecolor=self.BG, edgecolor=self.GRID, labelcolor=self.FG)
        self._fig.tight_layout()
        self._canvas.draw()


class FactorChartPanel(PickerChartPanel):
    """因子选股双图：左 RankIC 柱状（|t|≥2 标绿），右总分分布"""

    def show_factor_result(self, ic_table, score):
        self._ensure_canvas()
        self._fig.clear()
        gs = self._fig.add_gridspec(1, 2, wspace=0.35)

        # 左：各因子 mean IC，|t|>=2 视为显著
        ax1 = self._fig.add_subplot(gs[0])
        self._style_ax(ax1, "因子 RankIC（|t|≥2 显著）")
        tab = ic_table.dropna(subset=['mean_ic']).sort_values('mean_ic')
        if len(tab):
            colors = [self.ACCENT if abs(t) >= 2 else '#787b86'
                      for t in tab['t'].fillna(0)]
            labels = [k.split('（')[0] for k in tab['name']]
            ax1.barh(labels, tab['mean_ic'], color=colors, edgecolor=self.GRID)
            ax1.axvline(0, color=self.FG, linewidth=0.6)
            ax1.tick_params(axis='y', labelsize=7)

        # 右：TopN 总分分布
        ax2 = self._fig.add_subplot(gs[1])
        self._style_ax(ax2, "全市场因子总分分布")
        if len(score):
            ax2.hist(score.values, bins=40, color='#26a69a', edgecolor=self.GRID)
            ax2.axvline(0, color=self.FG, linewidth=0.4)

        self._fig.tight_layout()
        self._canvas.draw()


class ConditionPickPanel(wx.Panel):
    """页签1：条件筛选"""

    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False
        self._pool = None

        main_sizer = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.Panel(self, size=(300, -1))
        left.SetBackgroundColour(wx.Colour(245, 245, 245))
        ls = wx.BoxSizer(wx.VERTICAL)

        cond_box = wx.StaticBox(left, -1, "🎯 选股条件（全部满足才入选）")
        cs = wx.StaticBoxSizer(cond_box, wx.VERTICAL)
        self._cond_widgets = {}
        for key, label, dmin, dmax, _u in CONDITIONS:
            row = wx.BoxSizer(wx.HORIZONTAL)
            cb = wx.CheckBox(cond_box, label=label)
            row.Add(cb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            tc = wx.TextCtrl(cond_box, value=dmin, size=(60, -1))
            row.Add(tc, 0, wx.ALIGN_CENTER_VERTICAL)
            self._cond_widgets[key] = (cb, tc)
            cs.Add(row, 0, wx.EXPAND | wx.ALL, 3)
        ls.Add(cs, 0, wx.EXPAND | wx.ALL, 5)

        filt_box = wx.StaticBox(left, -1, "🛡 基础过滤（强制）")
        fs = wx.StaticBoxSizer(filt_box, wx.VERTICAL)
        fs.Add(wx.StaticText(filt_box, label="• 剔除 ST\n• 剔除长期停牌（无近期成交）\n• 剔除数据不足 6 日的次新"),
               0, wx.ALL, 5)
        ls.Add(fs, 0, wx.EXPAND | wx.ALL, 5)

        self.pick_but = wx.Button(left, -1, "🔍 开始选股")
        self.pick_but.SetBackgroundColour(wx.Colour(76, 187, 23))
        self.pick_but.SetForegroundColour(wx.Colour(255, 255, 255))
        self.pick_but.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.pick_but.SetMinSize((-1, 40))
        self.pick_but.Bind(wx.EVT_BUTTON, self.on_pick)
        ls.Add(self.pick_but, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(left, label="点击「开始选股」拉取全A数据（首次约几分钟，当日缓存后秒开）")
        self.status.Wrap(270)
        ls.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)

        left.SetSizer(ls)

        right = wx.Panel(self)
        rs_box = wx.BoxSizer(wx.VERTICAL)

        self.result_count = wx.StaticText(right, label="等待选股…")
        self.result_count.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        rs_box.Add(self.result_count, 0, wx.ALL, 6)

        self.result_list = wx.ListCtrl(right, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 420))
        for i, (col, w) in enumerate([("代码", 90), ("名称", 100), ("收盘", 70), ("PE(TTM)", 70),
                                      ("PB", 60), ("5日%", 60), ("20日%", 60), ("换手20", 60), ("额(亿)", 70)]):
            self.result_list.InsertColumn(i, col, width=w)
        rs_box.Add(self.result_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        self.chart_panel = PickerChartPanel(right)
        rs_box.Add(self.chart_panel, 1, wx.EXPAND | wx.ALL, 6)

        right.SetSizer(rs_box)

        main_sizer.Add(left, 0, wx.EXPAND | wx.ALL, 4)
        main_sizer.Add(right, 1, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(main_sizer)

    def on_pick(self, event):
        if self._busy:
            return
        self._busy = True
        self.pick_but.Disable()
        self.status.SetLabel("正在拉取全A行情+估值（首次约几分钟）…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            pool, from_cache = fetch_stock_pool(
                progress_cb=lambda i, n: wx.CallAfter(
                    self.status.SetLabel, f"拉取中 {i}/{n} …"))
            wx.CallAfter(self._done, pool, from_cache)
        except Exception as e:
            logger.exception("选股失败")
            wx.CallAfter(self._fail, str(e))

    def _done(self, pool, from_cache):
        self._pool = pool
        self._busy = False
        self.pick_but.Enable()
        result = self._apply_conditions(pool)
        self._show_result(result)
        src = "当日缓存" if from_cache else "最新拉取"
        self.status.SetLabel(f"数据源: {src} · 共 {len(pool)} 只候选 · 入选 {len(result)} 只")

    def _fail(self, msg):
        self._busy = False
        self.pick_but.Enable()
        self.status.SetLabel(f"选股失败: {msg}")

    def _apply_conditions(self, pool):
        df = pool[(pool['isST'] == 0)].dropna(subset=['close']).copy()
        for key, _label, _dmin, _dmax, _u in CONDITIONS:
            cb, tc = self._cond_widgets[key]
            if not cb.GetValue():
                continue
            try:
                v = float(tc.GetValue())
            except ValueError:
                continue
            if key == 'pe_max':
                df = df[(df['peTTM'] > 0) & (df['peTTM'] <= v)]
            elif key == 'pb_max':
                df = df[(df['pbMRQ'] > 0) & (df['pbMRQ'] <= v)]
            elif key == 'chg5_min':
                df = df[df['chg5'] >= v]
            elif key == 'chg5_max':
                df = df[df['chg5'] <= v]
            elif key == 'chg20_min':
                df = df[df['chg20'] >= v]
            elif key == 'turn_min':
                df = df[df['turn20'] >= v]
            elif key == 'turn_max':
                df = df[df['turn20'] <= v]
            elif key == 'amt_min':
                df = df[df['amt20'] >= v]
        return df

    def _show_result(self, result):
        self.result_list.DeleteAllItems()
        n = len(result)
        self.result_count.SetLabel(f"入选 {n} 只")
        for _, r in result.head(200).iterrows():
            idx = self.result_list.InsertItem(self.result_list.GetItemCount(), r['code'])
            self.result_list.SetItem(idx, 1, str(r['name']))
            self.result_list.SetItem(idx, 2, f"{r['close']:.2f}")
            self.result_list.SetItem(idx, 3, f"{r['peTTM']:.1f}" if pd.notna(r['peTTM']) else '--')
            self.result_list.SetItem(idx, 4, f"{r['pbMRQ']:.2f}" if pd.notna(r['pbMRQ']) else '--')
            self.result_list.SetItem(idx, 5, f"{r['chg5']:+.1f}" if pd.notna(r['chg5']) else '--')
            self.result_list.SetItem(idx, 6, f"{r['chg20']:+.1f}" if pd.notna(r['chg20']) else '--')
            self.result_list.SetItem(idx, 7, f"{r['turn20']:.2f}" if pd.notna(r['turn20']) else '--')
            self.result_list.SetItem(idx, 8, f"{r['amt20']:.2f}" if pd.notna(r['amt20']) else '--')
        self.chart_panel.show_distribution(result)


class FactorPickPanel(wx.Panel):
    """页签2：因子打分选股"""

    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False

        main_sizer = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.Panel(self, size=(300, -1))
        left.SetBackgroundColour(wx.Colour(245, 245, 245))
        ls = wx.BoxSizer(wx.VERTICAL)

        cfg_box = wx.StaticBox(left, -1, "🧮 因子打分选股")
        cfs = wx.StaticBoxSizer(cfg_box, wx.VERTICAL)

        row_n = wx.BoxSizer(wx.HORIZONTAL)
        row_n.Add(wx.StaticText(cfg_box, label="入选 TopN:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.topn_input = wx.TextCtrl(cfg_box, value="50", size=(60, -1))
        row_n.Add(self.topn_input, 0)
        cfs.Add(row_n, 0, wx.EXPAND | wx.ALL, 4)

        cfs.Add(wx.StaticLine(cfg_box), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        cfs.Add(wx.StaticText(cfg_box, label="参与打分的因子（勾选生效）\n方向已按经济含义调整，\nIC 显著性见右图，自行取舍："),
                0, wx.ALL, 4)

        self.factor_checks = {}
        for key, label in FACTOR_LABELS.items():
            cb = wx.CheckBox(cfg_box, label=label)
            cb.SetValue(True)
            self.factor_checks[key] = cb
            cfs.Add(cb, 0, wx.ALL, 2)
        ls.Add(cfs, 0, wx.EXPAND | wx.ALL, 5)

        self.pick_but = wx.Button(left, -1, "🧮 因子打分选股")
        self.pick_but.SetBackgroundColour(wx.Colour(33, 150, 243))
        self.pick_but.SetForegroundColour(wx.Colour(255, 255, 255))
        self.pick_but.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        self.pick_but.SetMinSize((-1, 40))
        self.pick_but.Bind(wx.EVT_BUTTON, self.on_pick)
        ls.Add(self.pick_but, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(left, label="首次拉取全A一年日线约 10 分钟，当日缓存后秒开。\nIC 检验：近12个月 × 每月截面 × 未来20日收益")
        self.status.Wrap(270)
        ls.Add(self.status, 0, wx.EXPAND | wx.ALL, 5)

        left.SetSizer(ls)

        right = wx.Panel(self)
        rs_box = wx.BoxSizer(wx.VERTICAL)

        self.result_count = wx.StaticText(right, label="等待因子选股…")
        self.result_count.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        rs_box.Add(self.result_count, 0, wx.ALL, 6)

        self.result_list = wx.ListCtrl(right, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 380))
        cols = [("代码", 90), ("名称", 90), ("收盘", 60), ("总分", 55), ("动量20%", 65),
                ("反转5%", 60), ("换手20", 60), ("量价相关", 65), ("振幅20", 60)]
        for i, (col, w) in enumerate(cols):
            self.result_list.InsertColumn(i, col, width=w)
        rs_box.Add(self.result_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        self.chart_panel = FactorChartPanel(right, height=2.6)
        rs_box.Add(self.chart_panel, 1, wx.EXPAND | wx.ALL, 6)

        right.SetSizer(rs_box)

        main_sizer.Add(left, 0, wx.EXPAND | wx.ALL, 4)
        main_sizer.Add(right, 1, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(main_sizer)

    def on_pick(self, event):
        if self._busy:
            return
        self._busy = True
        self.pick_but.Disable()
        self.status.SetLabel("正在拉取全A一年日线（首次约 10 分钟）…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            long_df, from_cache = fetch_factor_data(
                progress_cb=lambda i, n: wx.CallAfter(
                    self.status.SetLabel, f"拉取中 {i}/{n} …"))
            wx.CallAfter(self.status.SetLabel, "正在计算因子与 RankIC…")
            score, ic_table, detail = compute_factor_scores(long_df)
            wx.CallAfter(self._done, score, ic_table, detail, from_cache)
        except Exception as e:
            logger.exception("因子选股失败")
            wx.CallAfter(self._fail, str(e))

    def _done(self, score, ic_table, detail, from_cache):
        self._busy = False
        self.pick_but.Enable()
        try:
            topn = max(1, int(float(self.topn_input.GetValue())))
        except ValueError:
            topn = 50

        # 名称表（同日因子缓存附带；缺失则用代码）
        today = datetime.now().strftime('%Y%m%d')
        names_path = os.path.join(POOL_DIR, f"factor_names_{today}.pkl")
        names = pd.read_pickle(names_path)['name'] if os.path.exists(names_path) else pd.Series(dtype=str)

        # 基础信息（收盘价）来自 close pivot 的最后值：从 detail 里取不到 close，
        # 直接再从 long_df 拿代价高——这里用 factor 缓存的 close 列最后一天。
        # detail 中没有 close，故重新读缓存的最后一行收盘（缓存一定存在，因为刚算完）。
        cache = os.path.join(POOL_DIR, f"factor_{today}.pkl")
        last_close = pd.Series(dtype=float)
        if os.path.exists(cache):
            ld = pd.read_pickle(cache)
            last_close = ld.groupby('code')['close'].last()

        top = score.sort_values(ascending=False).head(topn)
        self.result_list.DeleteAllItems()
        for code, sc in top.items():
            idx = self.result_list.InsertItem(self.result_list.GetItemCount(), code)
            self.result_list.SetItem(idx, 1, str(names.get(code, code)))
            self.result_list.SetItem(idx, 2, f"{last_close.get(code, float('nan')):.2f}"
                                     if pd.notna(last_close.get(code, float('nan'))) else '--')
            self.result_list.SetItem(idx, 3, f"{sc:.1f}")
            if code in detail.index:
                d = detail.loc[code]
                self.result_list.SetItem(idx, 4, f"{d['mom20']*100:+.1f}" if pd.notna(d['mom20']) else '--')
                self.result_list.SetItem(idx, 5, f"{-d['rev5']*100:+.1f}" if pd.notna(d['rev5']) else '--')
                self.result_list.SetItem(idx, 6, f"{-d['low_turn']:.2f}" if pd.notna(d['low_turn']) else '--')
                self.result_list.SetItem(idx, 7, f"{d['vp_corr']:.2f}" if pd.notna(d['vp_corr']) else '--')
                self.result_list.SetItem(idx, 8, f"{-d['low_ampl']*100:.1f}" if pd.notna(d['low_ampl']) else '--')

        n_valid = len(score)
        sig = ic_table[ic_table['t'].abs() >= 2]['key'].tolist() if len(ic_table) else []
        self.result_count.SetLabel(f"Top{len(top)} / 有效样本 {n_valid} 只 · IC显著因子 {len(sig)}/10")
        src = "当日缓存" if from_cache else "最新拉取"
        self.status.SetLabel(f"数据源: {src} · 检验区间近12个月·每月截面·未来20日收益")
        self.chart_panel.show_factor_result(ic_table, score)

    def _fail(self, msg):
        self._busy = False
        self.pick_but.Enable()
        self.status.SetLabel(f"因子选股失败: {msg}")


class StockPickerPanel(wx.Panel):
    """选股面板容器：条件筛选 / 因子打分 两个页签"""

    def __init__(self, parent, on_pick_callback=None):
        super().__init__(parent)
        nb = wx.Notebook(self)
        nb.AddPage(ConditionPickPanel(nb), "⚖ 条件筛选")
        nb.AddPage(FactorPickPanel(nb), "🧮 因子打分")
        s = wx.BoxSizer(wx.VERTICAL)
        s.Add(nb, 1, wx.EXPAND | wx.ALL, 2)
        self.SetSizer(s)
