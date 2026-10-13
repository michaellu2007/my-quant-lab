"""
Author: Your Name
Date: 2026-03-12
Description: 专业回测面板 - 类似backtrader的多子图布局（K线+成交量+指标）
"""

import wx
import wx.adv
import os
import sys
import pandas as pd
import json
import backtrader as bt
try:
    import baostock as bs
except ImportError:  # BaoStock 是可选依赖：缺它只影响 BaoStock 数据源，不该让整个面板加载失败
    bs = None
import numpy as np
import time
import random
import importlib
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import requests
from datetime import datetime
import threading
import webbrowser
import importlib
from qbot.common.logging.logger import LOGGER as logger
from qbot.core.score_card import score_strategy, render_score_card_html
from qbot.core.market_rules import get_rules

try:
    from qbot.gui.panels.panel_batch_tools import OptimizerMixin, StockPoolMixin
except ImportError as e:
    logger.warning(f"批量工具模块加载失败: {e}")

try:
    from qbot.gui.panels.panel_stock_score import StockScoreMixin, WeightOptLauncherMixin
except ImportError as e:
    logger.warning(f"打分模块加载失败: {e}")
    StockScoreMixin = type('StockScoreMixin', (), {'_create_stock_score': lambda self, p, s: None})

try:
    from qbot.gui.panels.panel_positions import PositionMixin
except ImportError as e:
    logger.warning(f"持仓模块加载失败: {e}")
    PositionMixin = type('PositionMixin', (), {'_create_positions': lambda self, p, s: None})

try:
    from qbot.engine.strategy_pack import (
        RSRSMarketStrategy, MA_KDJ_Strategy, MACD_ADX_Strategy, AroonStrategy,
        ARBRStrategy, PricePercentileStrategy, SSAOptimizedMAStrategy,
        LinRegPredStrategy, SVMPredStrategy, RFPredStrategy, LGBMPredStrategy,
        LSTMPredStrategy, QLearningStrategy, _unsupported,
    )
    PAIR_UNSUPPORTED = _unsupported(
        "配对交易需要双标的数据（价差序列），当前引擎为单标的框架")
    SMALLCAP_UNSUPPORTED = _unsupported(
        "小市值策略需要全市场市值截面数据（当前仅接入单标的日线）")
    ALPHALENS_UNSUPPORTED = _unsupported(
        "alphalens 多因子交易需要 alphalens 库与多因子面板数据")
    KURTOSIS_UNSUPPORTED = _unsupported(
        "Kurtosis 组合策略需要多资产协方差优化，当前单标的引擎不支持")
except ImportError as e:
    logger.warning(f"策略包加载失败: {e}")

try:
    from qbot.gui.panels.panel_my_backtest_factors import (
        FactorCalculator, JOINQUANT_FACTORS, FactorPanelMixin, factor_meta,
        StrategyInstallerMixin,
    )
except ImportError as e:
    logger.warning(f"因子模块加载失败: {e}")
    JOINQUANT_FACTORS = {}

try:
    from qbot.gui.panels.ui_theme import COLORS, FONTS, make_button, make_label, SectionCard, wxs
except ImportError as e:
    logger.warning(f"主题模块加载失败: {e}")

try:
    from qbot.gui.config import DATA_DIR_BKT_RESULT
except ImportError:
    import tempfile
    DATA_DIR_BKT_RESULT = os.path.join(tempfile.gettempdir(), "qbot_backtest")
    os.makedirs(DATA_DIR_BKT_RESULT, exist_ok=True)


_A_NAME_MAP = None  # 进程内缓存: {code: name}，首次调用加载一次全表
_A_NAME_MAP_DISK = os.path.join(DATA_DIR_BKT_RESULT, "..", "csv", "a_name_map.json")
_HK_NAME_MAP = None
_EM_INDEX_FAILS = 0   # 东财基准接口连续失败计数（进程内熔断，见 _index_fetch）
_HK_NAME_MAP_DISK = os.path.join(DATA_DIR_BKT_RESULT, "..", "csv", "hk_name_map.json")


class _LazyModule:
    """首次真正用到某个属性时才 import 该模块。

    akshare 光 import 就要 1.4 秒，而参数寻优的子进程只跑回测、不碰行情接口，
    却因为模块级 import 每次启动都得付这笔钱。改成懒加载后，
    子进程启动省下约 1.4 秒，进程数越多省得越多。
    """

    def __init__(self, name):
        self._name = name
        self._mod = None

    def __getattr__(self, item):
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, item)


ak = _LazyModule('akshare')


def _get_a_name_map():
    """A股 代码→名称 全表：磁盘缓存 → 在线拉取(重试3次)。
    成功拉取一次后落盘，之后即使接口全挂也能离线解析名称。"""
    global _A_NAME_MAP
    if _A_NAME_MAP is not None:
        return _A_NAME_MAP
    # 1) 磁盘缓存
    try:
        import json as _json
        if os.path.exists(_A_NAME_MAP_DISK):
            with open(_A_NAME_MAP_DISK, "r", encoding="utf-8") as f:
                _A_NAME_MAP = _json.load(f)
            if _A_NAME_MAP:
                logger.info(f"A股名称表从磁盘缓存加载: {len(_A_NAME_MAP)} 只")
                return _A_NAME_MAP
    except Exception as e:
        logger.warning(f"A股名称表磁盘缓存读取失败: {e}")
    # 2) 在线拉取
    for attempt in range(3):
        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                _A_NAME_MAP = dict(zip(df['code'].astype(str), df['name'].astype(str)))
                logger.info(f"A股名称表已加载: {len(_A_NAME_MAP)} 只")
                try:
                    os.makedirs(os.path.dirname(_A_NAME_MAP_DISK), exist_ok=True)
                    import json as _json
                    with open(_A_NAME_MAP_DISK, "w", encoding="utf-8") as f:
                        _json.dump(_A_NAME_MAP, f, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"A股名称表落盘失败: {e}")
                return _A_NAME_MAP
        except Exception as e:
            logger.warning(f"A股名称表获取失败({attempt + 1}/3): {e}")
            time.sleep(1.5 * (attempt + 1))
    return None


def _get_hk_name_map():
    """港股 代码→名称 全表：磁盘缓存 → stock_hk_spot 拉一次（与 get_stock_name 港股分支同一接口）。
    ponytail: 拉取失败不重试、返回 None；名称搜索自动退化为仅A股，输5位代码不受影响。"""
    global _HK_NAME_MAP
    if _HK_NAME_MAP is not None:
        return _HK_NAME_MAP
    try:
        import json as _json
        if os.path.exists(_HK_NAME_MAP_DISK):
            with open(_HK_NAME_MAP_DISK, "r", encoding="utf-8") as f:
                _HK_NAME_MAP = _json.load(f)
            if _HK_NAME_MAP:
                logger.info(f"港股名称表从磁盘缓存加载: {len(_HK_NAME_MAP)} 只")
                return _HK_NAME_MAP
    except Exception as e:
        logger.warning(f"港股名称表磁盘缓存读取失败: {e}")
    try:
        df = ak.stock_hk_spot()
        if df is not None and not df.empty:
            col_code = '代码' if '代码' in df.columns else 'symbol'
            col_name = ('中文名称' if '中文名称' in df.columns
                        else '名称' if '名称' in df.columns else 'name')
            # 接口列名随 akshare 版本变过（曾因固定找 'name' 抛 KeyError）。
            # 兜底按位置取：第0列当代码、第1列当名称，并把真实列名记进日志。
            if col_code not in df.columns or col_name not in df.columns:
                cols = list(df.columns)
                logger.warning(f"港股spot列名变化: {cols[:6]}，按位置兜底(0=代码,1=名称)")
                col_code = col_code if col_code in df.columns else cols[0]
                col_name = col_name if col_name in df.columns else cols[1]
            # 排除人民币柜台条目（同公司的 8xxxx 重复上市，如 美团－ＷＲ/83690），
            # 否则输"美团"永远多匹配、无法自动回填主柜台 03690；柜台代码仍可直接输入
            _HK_NAME_MAP = {str(c).zfill(5): str(n)
                            for c, n in zip(df[col_code], df[col_name])
                            if not str(n).endswith(('Ｒ', 'R'))}
            logger.info(f"港股名称表已加载: {len(_HK_NAME_MAP)} 只")
            try:
                os.makedirs(os.path.dirname(_HK_NAME_MAP_DISK), exist_ok=True)
                with open(_HK_NAME_MAP_DISK, "w", encoding="utf-8") as f:
                    import json as _json
                    _json.dump(_HK_NAME_MAP, f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"港股名称表落盘失败: {e}")
            return _HK_NAME_MAP
    except Exception as e:
        logger.warning(f"港股名称表获取失败: {e}")
    return None


def _search_by_name(text, limit=4):
    """按名称搜索股票（A股+港股，反向复用代码→名称全表）：精确 > 前缀 > 包含，去重后最多 limit 条。
    A股命中排前面；名称表没加载成功的市场自动跳过。"""
    text = (text or "").strip()
    ranked = []
    for name_map in (_get_a_name_map(), _get_hk_name_map()):
        if not name_map:
            continue
        ranked += ([(c, n) for c, n in name_map.items() if n == text]
                   + [(c, n) for c, n in name_map.items() if n.startswith(text)]
                   + [(c, n) for c, n in name_map.items() if text in n])
    seen, out = set(), []
    for c, n in ranked:
        if c not in seen:
            seen.add(c)
            out.append((c, n))
            if len(out) >= limit:
                break
    return out


def get_stock_name(code, market="A股"):
    """根据股票代码和市场获取股票名称"""
    import random

    try:
        if market == "A股":
            # 方法1: 全表缓存（首次约2秒，之后即时查询）
            name_map = _get_a_name_map()
            if name_map and code in name_map:
                return name_map[code]
            # 方法2(后备): EM 单只查询；东财接口常瞬断，失败不阻塞
            try:
                time.sleep(random.uniform(0.5, 1.5))
                stock_info = ak.stock_individual_info_em(symbol=code)
                if not stock_info.empty:
                    name_row = stock_info[stock_info['item'] == '股票简称']
                    if not name_row.empty:
                        return name_row['value'].iloc[0]
            except Exception as e:
                logger.warning(f"info_em 查询名称失败({code}): {e}")
                # 方法3: 全表再试一次（可能首次加载时网络抖动）
                global _A_NAME_MAP
                _A_NAME_MAP = None
                name_map = _get_a_name_map()
                if name_map and code in name_map:
                    return name_map[code]
        elif market == "港股":
            # 港股名称获取 - 方法1: 使用 stock_hk_spot
            try:
                code_hk = code.zfill(5)
                stock_list = ak.stock_hk_spot()
                # 注意：港股 spot 接口的列名可能是 '代码' 或 'symbol'
                if '代码' in stock_list.columns:
                    match = stock_list[stock_list['代码'] == code_hk]
                elif 'symbol' in stock_list.columns:
                    match = stock_list[stock_list['symbol'] == code_hk]
                else:
                    match = stock_list[stock_list.iloc[:, 0].astype(str) == code_hk]
                
                if not match.empty:
                    if '名称' in match.columns:
                        return match['名称'].iloc[0]
                    elif 'name' in match.columns:
                        return match['name'].iloc[0]
            except Exception as e:
                logger.warning(f"港股spot接口获取名称失败: {e}")
                
            # 如果上面失败，直接返回代码
            return f"{code} (港股)"
                
        elif market == "美股":
            # 美股名称获取
            try:
                stock_list = ak.stock_us_spot()
                # 美股 spot 接口的列名可能是 '代码' 或 'symbol'
                if '代码' in stock_list.columns:
                    match = stock_list[stock_list['代码'] == code.upper()]
                elif 'symbol' in stock_list.columns:
                    match = stock_list[stock_list['symbol'] == code.upper()]
                else:
                    match = stock_list[stock_list.iloc[:, 0].astype(str).str.upper() == code.upper()]
                
                if not match.empty:
                    if '名称' in match.columns:
                        return match['名称'].iloc[0]
                    elif 'name' in match.columns:
                        return match['name'].iloc[0]
            except Exception as e:
                logger.warning(f"美股spot接口获取名称失败: {e}")
            
            # 如果上面失败，直接返回代码
            return f"{code.upper()} (美股)"
        
        return code
    except Exception as e:
        logger.warning(f"获取股票名称失败: {e}")
        return code


def detect_market(code):
    """根据股票代码自动识别市场，无法判断时返回 None"""
    if not code:
        return None
    code = code.strip().upper()

    # 带交易所后缀的代码
    if code.endswith(('.SH', '.SZ', '.BJ')):
        return "A股"
    if code.endswith('.HK'):
        return "港股"
    if code.endswith(('.US', '.NASDAQ', '.NYSE', '.AMEX')):
        return "美股"

    # 纯数字：6位为A股，5位为港股
    if code.isdigit():
        if len(code) == 6:
            return "A股"
        if len(code) == 5:
            return "港股"
        return None

    # 含字母：按美股处理
    if any(c.isalpha() for c in code):
        return "美股"

    return None


# ==================== 策略类定义（带指标计算） ====================

class AStockCommission(bt.CommInfoBase):
    """佣金模型：双边佣金，印花税按市场规则收。
    percabs=True 使费率按小数理解，避免基类 __init__ 自动 /100。

    港股印花税买卖双向（A股仅卖出），由 market 参数经 MARKET_RULES 决定，
    与 core/paper_broker.py 共用同一张规则表。
    """
    params = (
        ('percabs', True),
        ('stamp_duty', 0.0),
        ('market', 'A股'),
    )

    def _getcommission(self, size, price, pseudoexec):
        value = abs(size) * price
        comm = value * self.p.commission
        rules = get_rules(self.p.market)
        if rules['stamp_side'] == 'both':
            comm += value * self.p.stamp_duty
        elif rules['stamp_side'] == 'sell' and size < 0:
            comm += value * self.p.stamp_duty
        return comm


class BaseStrategy(bt.Strategy):
    """策略基类，自动记录交易"""
    params = (
        ('stake', 100),  # 每手股数，买入按此整手取整
        # 现实摩擦参数：默认隔日成交开（更贴近实盘），熔断默认关
        ('friction_delay', True),     # 隔日成交：信号当根不成交，下一根开盘价成交
        ('friction_circuit', False),  # 涨跌停熔断：A 股 ±10%，一字板时拒单；港股无涨跌停，自动跳过
        ('market', 'A股'),           # 见 qbot/core/market_rules.py，决定熔断与印花税方向
    )

    def __init__(self):
        self.trades = []  # [(date_str, price, 'buy'/'sell'), ...]
        self.order = None
        self._pending_action = None  # 隔日成交：暂存 ('buy'/'sell', size)，下根 next() 触发
        self.equity_curve = []  # 每根K线记录 (date, 总资产)，供资金曲线/回撤/月度收益图表使用
        # 包装 next：先 flush 隔日挂单、记录净值，再走原策略逻辑。
        # 这样 11 个子策略的 next() 不必逐个改写。
        # backtrader 通过 cerebro 调 next() 时是普通方法调用（self 已绑定），
        # 所以这里需要的是 instance method（带 self 形参）。
        from types import MethodType
        _orig = self.next
        def _next_impl(self):
            self._flush_pending()
            self._record_equity()
            if self.order:
                return
            _orig()
        self.next = MethodType(_next_impl, self)

    def _record_equity(self):
        """记录当根总资产（按当根收盘估值）"""
        d = self.datas[0].datetime.date(0).strftime('%Y-%m-%d')
        self.equity_curve.append((d, self.broker.getvalue()))

    def _circuit_breaker(self, action):
        """涨跌停熔断：基于前收计算涨跌停幅度，命中则本次信号作废。
        一字板（low==high==limit）的极端情况下，无论买卖都拒单。

        港股无涨跌停（limit_pct=None），勾了熔断也不拒单。"""
        if not self.p.friction_circuit:
            return False
        limit_pct = get_rules(getattr(self.p, 'market', 'A股'))['limit_pct']
        if limit_pct is None:
            return False
        d = self.datas[0]
        if len(d) < 2:
            return False
        prev = d.close[-1]
        if prev <= 0:
            return False
        up_limit = prev * (1 + limit_pct)
        dn_limit = prev * (1 - limit_pct)
        # 涨停：当日最低价 ≥ 涨停价（封板），买不进
        if action == 'buy' and d.low[0] >= up_limit - 1e-6:
            return True
        # 跌停：当日最高价 ≤ 跌停价（封板），卖不出
        if action == 'sell' and d.high[0] <= dn_limit + 1e-6:
            return True
        return False

    def buy_signal(self):
        """统一买入：按可用资金的95%计算股数，并按交易规模(stake)整手向下取整；
        资金不足一手时回退为全额股数，避免信号被吞。
        现实摩擦拦截：熔断命中则跳过；隔日成交则把信号压到下根。"""
        if self._circuit_breaker('buy'):
            return
        price = self.datas[0].close[0]
        raw = int(self.broker.cash * 0.95 / price)
        lot = max(1, int(self.p.stake))
        size = (raw // lot) * lot
        if size <= 0:
            size = raw
        if size <= 0:
            return
        if self.p.friction_delay:
            self._pending_action = ('buy', size)
            return
        self.order = self.buy(size=size)

    def _flush_pending(self):
        """隔日成交：把上根压下来的信号在下一根开盘价执行。
        这里仍用 close[0] 近似下一根开盘（backtrader 数据结构对策略只暴露 close）。"""
        if not self._pending_action or self.order is not None:
            return
        action, size = self._pending_action
        self._pending_action = None
        if self._circuit_breaker(action):
            return
        if action == 'buy' and not self.position:
            self.order = self.buy(size=size)
        elif action == 'sell' and self.position:
            self.order = self.sell(size=min(size, self.position.size))
    
    def notify_order(self, order):
        """订单状态变化时自动记录交易"""
        if order.status in [order.Completed]:
            exec_date = self.datas[0].datetime.date(0)
            date_str = exec_date.strftime('%Y-%m-%d')
            price = order.executed.price
            trade_type = 'buy' if order.isbuy() else 'sell'
            
            self.trades.append((date_str, price, trade_type))
            logger.info(f"交易记录: {date_str} {trade_type} @ {price:.2f}")
            
            self.order = None
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.order = None


class MA_Cross_Strategy(BaseStrategy):
    """双均线交叉策略"""
    params = (
        ('pfast', 5),
        ('pslow', 10),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma1 = bt.ind.SMA(period=self.p.pfast, subplot=False)  # 主图显示
        self.sma2 = bt.ind.SMA(period=self.p.pslow, subplot=False)  # 主图显示
        self.crossover = bt.ind.CrossOver(self.sma1, self.sma2)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.crossover > 0:
                self.buy_signal()
        else:
            if self.crossover < 0:
                self.order = self.sell(size=self.position.size)


class RSI_Strategy(BaseStrategy):
    """RSI相对强弱指数策略"""
    params = (
        ('period', 14),
        ('overbought', 70),
        ('oversold', 30),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.rsi = bt.ind.RSI(period=self.p.period, subplot=True)  # 副图显示
        self.rsi.plotinfo.plotname = 'RSI'

    def next(self):
        if self.order:
            return
        
        if not self.position:
            if self.rsi < self.p.oversold:
                self.buy_signal()
        else:
            if self.rsi > self.p.overbought:
                self.order = self.sell(size=self.position.size)


class MACD_Strategy(BaseStrategy):
    """MACD指数平滑异同移动平均线策略"""
    params = (
        ('fast', 12),
        ('slow', 26),
        ('signal', 9),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.macd = bt.ind.MACD(
            period_me1=self.p.fast,
            period_me2=self.p.slow,
            period_signal=self.p.signal,
            subplot=True  # 副图显示
        )
        self.macd.plotinfo.plotname = 'MACD'

    def next(self):
        if self.order:
            return
        
        if not self.position:
            if self.macd.macd > self.macd.signal:
                self.buy_signal()
        else:
            if self.macd.macd < self.macd.signal:
                self.order = self.sell(size=self.position.size)


class BollStrategy(BaseStrategy):
    """布林带策略"""
    params = (
        ("nk", 13),
    )

    def __init__(self):
        super().__init__()
        self.data_close = self.datas[0].close
        self.boll = bt.indicators.BollingerBands(self.datas[0], period=self.params.nk)
        self.top = self.boll.top
        self.bot = self.boll.bot
        self.sma = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.params.nk)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.data_close[0] < self.bot[0]:
                self.buy_signal()
        else:
            if self.data_close[0] > self.top[0]:
                self.order = self.sell()


class SmaCross(BaseStrategy):
    """均线交叉策略"""
    params = (
        ('pfast', 10),
        ('pslow', 30),
    )

    def __init__(self):
        super().__init__()
        sma1 = bt.ind.SMA(period=self.p.pfast)
        sma2 = bt.ind.SMA(period=self.p.pslow)
        self.crossover = bt.ind.CrossOver(sma1, sma2)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.crossover > 0:
                self.buy_signal()
        else:
            if self.crossover < 0:
                self.order = self.close()


class BiggerThanEmaStrategy(BaseStrategy):
    """大于EMA策略"""
    params = (
        ("maperiod", 15),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.params.maperiod)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.dataclose[0] > self.sma[0]:
                self.buy_signal()
        else:
            if self.dataclose[0] < self.sma[0]:
                self.order = self.sell()


class MultiStrategy(BaseStrategy):
    """多指标策略"""
    params = (
        ('exitbars', 5),
        ('maperiod', 15),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma = bt.ind.SMA(period=self.params.maperiod)
        self.rsi = bt.ind.RSI()

    def next(self):
        if self.order:
            return
        if not self.position:
            if (self.rsi[0] < 50 and self.rsi[-1] >= 50 and self.dataclose[0] > self.sma[0]):
                self.buy_signal()
        else:
            if (self.rsi[0] > 50 and self.rsi[-1] <= 50 and self.dataclose[0] < self.sma[0]):
                self.order = self.sell()


class KlinesStrategy(BaseStrategy):
    """K线策略"""
    params = (
        ('maperiod', 5),
    )

    def __init__(self):
        super().__init__()
        self.close_price = self.datas[0].close
        self.sma = bt.ind.SMA(self.datas[0], period=self.params.maperiod)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.close_price[0] > self.sma[0]:
                self.buy_signal()
        else:
            if self.close_price[0] < self.sma[0]:
                self.order = self.sell()


class LSTMStrategy(BaseStrategy):
    """趋势突破：收盘上穿 N 日均线 2% 买入，下穿均线卖出。

    不是 LSTM，也没有神经网络——日线级 LSTM 已被反复证明无可复现的 alpha，
    这里保留的只是均线突破本身。UI 标签已改为「趋势突破(原LSTM占位)」。"""
    params = (
        ('lookback', 20),
        ('threshold', 0.02),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma = bt.ind.SMA(period=self.p.lookback)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.dataclose[0] > self.sma[0] * (1 + self.p.threshold):
                self.buy_signal()
        else:
            if self.dataclose[0] < self.sma[0]:
                self.order = self.sell()


class SSAStrategy(BaseStrategy):
    """SSA策略 - 基于奇异谱分析的趋势跟踪简化版"""
    params = (
        ('window', 10),
        ('threshold', 0.01),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma = bt.ind.SMA(period=self.p.window)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.dataclose[0] > self.sma[0] * (1 + self.p.threshold):
                self.buy_signal()
        else:
            if self.dataclose[0] < self.sma[0] * (1 - self.p.threshold):
                self.order = self.sell()


class RLStrategy(BaseStrategy):
    """强化学习策略 - 基于动量策略的简化版（近似模拟RL决策）"""
    params = (
        ('momentum_period', 14),
        ('entry_threshold', 0.02),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.sma = bt.ind.SMA(period=self.p.momentum_period)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.dataclose[0] > self.sma[0] * (1 + self.p.entry_threshold):
                self.buy_signal()
        else:
            if self.dataclose[0] < self.sma[0] * (1 - self.p.entry_threshold):
                self.order = self.sell()


class MyStrategy(BaseStrategy):
    """自定义策略 - 双均线交叉"""
    params = (
        ('fast', 5),
        ('slow', 20),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.fast_ma = bt.ind.SMA(period=self.p.fast)
        self.slow_ma = bt.ind.SMA(period=self.p.slow)
        self.crossover = bt.ind.CrossOver(self.fast_ma, self.slow_ma)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.crossover[0] > 0:
                self.buy_signal()
        else:
            if self.crossover[0] < 0:
                self.order = self.sell(size=self.position.size)


# ==================== 长线策略族（低频：周/月调仓 + 冷却期） ====================
class LongTermBase(BaseStrategy):
    """长线策略基类：
    - freq: 信号只在 每周第一个交易日 / 每月第一个交易日 / 每天 生效（强制降频）
    - cooldown: 卖出后 N 个信号日内不允许再买入，防频繁进出
    子类实现 _long_entry() / _long_exit() 两个布尔方法。
    """
    params = (
        ('freq', 'weekly'),      # daily / weekly / monthly
        ('cooldown', 2),         # 冷却信号日数
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self._last_exit_bar = -10**9
        self._last_period = None

    def _signal_day(self):
        """是否为允许下单的信号日：周=每周第一个交易日，月=每月第一个交易日"""
        d = self.datas[0].datetime.date(0)
        if self.p.freq == 'weekly':
            key = d.isocalendar()[1]
        elif self.p.freq == 'monthly':
            key = d.month
        else:
            return True
        if self._last_period is None or key != self._last_period:
            self._last_period = key
            return True
        return False

    def _cooldown_ok(self):
        # 卖出后 cooldown 个交易日内禁止再买入（防频繁进出）
        return len(self) - self._last_exit_bar > self.p.cooldown

    def next(self):
        if self.order:
            return
        if not self._signal_day():
            return
        if not self.position:
            if self._cooldown_ok() and self._long_entry():
                self.buy_signal()
        else:
            if self._long_exit():
                self.order = self.sell(size=self.position.size)
                self._last_exit_bar = len(self)


class DonchianLongStrategy(LongTermBase):
    """唐奇安通道长线：收盘突破 entry_n 日最高价买入，跌破 exit_n 日最低价卖出"""
    params = (
        ('entry_n', 55),
        ('exit_n', 20),
    )

    def __init__(self):
        super().__init__()
        self.dh = bt.ind.Highest(self.datas[0].high, period=self.p.entry_n, subplot=False)
        self.dl = bt.ind.Lowest(self.datas[0].low, period=self.p.exit_n, subplot=True)
        self.dl.plotinfo.plotname = '唐奇安下轨'

    def _long_entry(self):
        return self.dataclose[0] > self.dh[-1]   # 突破昨日为止的 N 日高点

    def _long_exit(self):
        # 用截至昨日的 N 日最低：若含今日，close 永远不会低于含今日最低的窗口最小值，卖出永不触发
        return self.dataclose[0] < self.dl[-1]


class DualMALongStrategy(LongTermBase):
    """50/200 双均线长线：快线上穿慢线买入，下穿卖出（金叉持有、死叉离场）"""
    params = (
        ('fast', 50),
        ('slow', 200),
    )

    def __init__(self):
        super().__init__()
        self.ma_f = bt.ind.SMA(period=self.p.fast, subplot=False)
        self.ma_s = bt.ind.SMA(period=self.p.slow, subplot=False)
        self.cross = bt.ind.CrossOver(self.ma_f, self.ma_s, subplot=True)
        self.cross.plotinfo.plotname = '金叉死叉'

    def _long_entry(self):
        return self.cross[0] > 0

    def _long_exit(self):
        return self.cross[0] < 0


class MomentumLongStrategy(LongTermBase):
    """低频动量：价格在 slow 日均线上方且 lookback 日动量为正 → 持有；跌破均线离场"""
    params = (
        ('lookback', 120),
        ('slow', 200),
    )

    def __init__(self):
        super().__init__()
        self.ma_s = bt.ind.SMA(period=self.p.slow, subplot=False)
        self.roc = bt.ind.ROC(self.datas[0].close, period=self.p.lookback, subplot=True)
        self.roc.plotinfo.plotname = '动量%'

    def _long_entry(self):
        return self.dataclose[0] > self.ma_s[0] and self.roc[0] > 0

    def _long_exit(self):
        return self.dataclose[0] < self.ma_s[0]


class TurtleStrategy(LongTermBase):
    """海龟策略(系统1)：收盘突破 entry_n 日高点买入；止损=入场价-atr_stop×ATR；
    或收盘跌破 exit_n 日低点离场。每日看盘(经典海龟为日内系统)。"""
    params = (
        ('entry_n', 20),
        ('exit_n', 10),
        ('atr_n', 14),
        ('atr_stop', 2.0),
        ('freq', 'daily'),
        ('cooldown', 0),
    )

    def __init__(self):
        super().__init__()
        self.dh = bt.ind.Highest(self.datas[0].high, period=self.p.entry_n, subplot=False)
        self.dl = bt.ind.Lowest(self.datas[0].low, period=self.p.exit_n, subplot=True)
        self.dl.plotinfo.plotname = '海龟离场线'
        self.atr = bt.ind.ATR(period=self.p.atr_n, subplot=False)
        self._entry_price = None

    def _long_entry(self):
        if self.dataclose[0] > self.dh[-1]:
            self._entry_price = self.dataclose[0]
            return True
        return False

    def _long_exit(self):
        # 2N 止损 或 跌破 exit_n 日低点（窗口截至昨日）
        if self._entry_price is not None and self.atr[0] > 0:
            if self.dataclose[0] < self._entry_price - self.p.atr_stop * self.atr[0]:
                return True
        return self.dataclose[0] < self.dl[-1]


def is_long_term_strategy(strategy_name, strategy_map=None):
    """判断某个策略是否属于长线策略族（LongTermBase 子类）。

    长线策略按周/月才出一次信号，持仓数周以上，成交时点稍差一两天
    对结论影响有限；但回测里若用「信号当根立刻成交」这种理想时点，
    收益会被系统性高估。长线族因此默认开启延迟成交。
    用 issubclass 判定而不是硬编码策略名，新增长线策略自动生效。
    """
    if not strategy_name:
        return False
    smap = strategy_map if strategy_map is not None else STRATEGY_MAP
    cls = smap.get(strategy_name)
    if cls is None:
        return False
    try:
        return issubclass(cls, LongTermBase)
    except TypeError:
        return False


class GridStrategy(BaseStrategy):
    """网格策略：以首日收盘为锚，每跌 step% 买一份、每涨 step% 卖一份（适合震荡市）。"""
    params = (
        ('step_pct', 3.0),   # 每格涨跌幅 %
        ('n_grids', 5),      # 资金分成的份数
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self._last_price = None
        self._per_grid_cash = None

    def next(self):
        if self.order:
            return
        close = self.dataclose[0]
        if close <= 0:
            return
        if self._last_price is None:
            self._last_price = close
            self._per_grid_cash = self.broker.cash * 0.95 / max(1, self.p.n_grids)
            size = int(self._per_grid_cash / close / 100) * 100
            if size >= 100:
                self.buy(size=size)
            return
        if close <= self._last_price * (1 - self.p.step_pct / 100):
            size = int(self._per_grid_cash / close / 100) * 100
            if size >= 100 and self.broker.cash >= close * size:
                self.order = self.buy(size=size)
                self._last_price = close
        elif close >= self._last_price * (1 + self.p.step_pct / 100) and self.position:
            size = int(self._per_grid_cash / close / 100) * 100
            size = min(size, self.position.size)
            if size >= 100:
                self.order = self.sell(size=size)
                self._last_price = close


class StochRSIInd(bt.Indicator):
    """StochRSI 指标：RSI(Wilder) 在其自身 N 日区间内的随机位置(0~100)。
    全部用 next() 自算，规避 backtrader 内置 RSI/DivByZero 在数据前段
    只有单边涨跌时的零除 bug。"""
    lines = ('stochrsi',)
    params = (('rsi_n', 14), ('stoch_n', 14))
    plotinfo = dict(subplot=True, plotname='StochRSI')

    def __init__(self):
        self.addminperiod(self.p.rsi_n + self.p.stoch_n)
        self._prev_close = None
        self._avg_gain = None
        self._avg_loss = None
        self._rsi_hist = []

    def next(self):
        c = self.data.close[0]
        if self._prev_close is None:
            self._prev_close = c
            self.lines.stochrsi[0] = 50.0
            return
        chg = c - self._prev_close
        self._prev_close = c
        gain, loss = max(chg, 0.0), max(-chg, 0.0)
        if self._avg_gain is None:
            self._avg_gain, self._avg_loss = gain, loss
        else:
            n = self.p.rsi_n
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        if self._avg_loss == 0:
            rsi = 100.0
        else:
            rsi = 100.0 - 100.0 / (1.0 + self._avg_gain / self._avg_loss)
        self._rsi_hist.append(rsi)
        window = self._rsi_hist[-self.p.stoch_n:]
        lo, hi = min(window), max(window)
        if hi == lo:
            self.lines.stochrsi[0] = 50.0
        else:
            self.lines.stochrsi[0] = (rsi - lo) / (hi - lo) * 100


class StochRSIStrategy(BaseStrategy):
    """StochRSI：RSI 的随机指标。<20 超卖买入，>80 超买卖出。"""
    params = (
        ('rsi_n', 14),
        ('stoch_n', 14),
        ('oversold', 20),
        ('overbought', 80),
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        self.stoch_rsi = StochRSIInd(self.datas[0], rsi_n=self.p.rsi_n,
                                     stoch_n=self.p.stoch_n)

    def next(self):
        if self.order:
            return
        if not self.position:
            if self.stoch_rsi[0] < self.p.oversold:
                self.buy_signal()
        else:
            if self.stoch_rsi[0] > self.p.overbought:
                self.order = self.sell(size=self.position.size)


# 策略默认参数定义（供混合策略和参数调整使用）
STRATEGY_PARAMS = {
    "双均线策略": {"pfast": 5, "pslow": 10},
    "RSI策略": {"period": 14, "oversold": 30, "overbought": 70},
    "MACD策略": {"fast": 12, "slow": 26, "signal": 9},
    "布林带策略": {"nk": 13},
    "均线交叉策略": {"pfast": 10, "pslow": 30},
    "大于EMA策略": {"maperiod": 15},
    "多指标策略": {"maperiod": 15},
    "K线策略": {"maperiod": 5},
    "趋势突破(原LSTM占位)": {"lookback": 20, "threshold": 0.02},
    "SSA策略": {"window": 10, "threshold": 0.01},
    "强化学习策略": {"momentum_period": 14, "entry_threshold": 0.02},
    "MyStrategy": {"fast": 5, "slow": 20},
    "唐奇安通道长线": {"entry_n": 55, "exit_n": 20, "freq": "weekly", "cooldown": 2},
    "双均线长线": {"fast": 50, "slow": 200, "freq": "weekly", "cooldown": 2},
    "低频动量长线": {"lookback": 120, "slow": 200, "freq": "monthly", "cooldown": 1},
    "海龟策略": {"entry_n": 20, "exit_n": 10, "atr_n": 14, "atr_stop": 2.0, "cooldown": 0},
    "网格策略": {"step_pct": 3.0, "n_grids": 5},
    "StochRSI策略": {"rsi_n": 14, "stoch_n": 14, "oversold": 20, "overbought": 80},
    # ---- 与可视化回测面板同名的策略包（真实实现）----
    "单因子-相对强弱指数RSI": {"period": 14, "oversold": 30, "overbought": 70},
    "单因子-随机相对强弱指数StochRSI": {"rsi_n": 14, "stoch_n": 14, "oversold": 20, "overbought": 80},
    "单因子-RSRS择时": {"n": 18, "zwin": 250, "z_in": 0.7, "z_out": -0.5},
    "单因子-移动均线+KDJ": {"ma_n": 20, "kdj_n": 9},
    "单因子-MACD和ADX指标": {"adx_in": 20, "adx_out": 15},
    "单因子-布林线均值回归": {"nk": 13},
    "单因子-阿隆指标(趋势交易)": {"n": 25, "level": 70},
    "单因子-简单移动均线": {"maperiod": 20},
    "单因子-情绪指标ARBR": {"n": 26, "br_low": 40, "br_high": 400, "ar_high": 250},
    "单因子-市场低估值策略": {"window": 250, "low_pct": 0.30, "high_pct": 0.70},
    "机器学习-麻雀优化算法SSA": {"warmup": 250, "train_ratio": 0.7, "pop": 12, "ssa_iter": 20},
    "机器学习-支持向量机SVM": {"warmup": 250, "train_ratio": 0.7},
    "机器学习-MLP时序分类": {"warmup": 250, "train_ratio": 0.7},
    "机器学习-LGBM模型价格预测": {"warmup": 250, "train_ratio": 0.7},
    "机器学习-随机森林模型价格预测": {"warmup": 250, "train_ratio": 0.7},
    "机器学习-线性回归价格预测": {"warmup": 250, "train_ratio": 0.7},
    "强化学习-RL模型价格预测": {"warmup": 250, "train_ratio": 0.7, "eps": 0.1, "alpha": 0.2, "gamma": 0.9},
    "强化学习-Q-Leaning预测": {"warmup": 250, "train_ratio": 0.7, "eps": 0.1, "alpha": 0.2, "gamma": 0.9},
    "传统策略-海龟策略": {"entry_n": 20, "exit_n": 10, "atr_n": 14, "atr_stop": 2.0, "cooldown": 0},
    "传统策略-网格策略": {"step_pct": 3.0, "n_grids": 5},
    "多因子-ROC(20)动量信号周频Top1": {"lookback": 20, "slow": 200, "freq": "weekly", "cooldown": 2},
}


class HybridStrategy(BaseStrategy):
    """混合策略 - 自由组合多个子策略，支持AND/OR/MAJORITY组合模式"""
    params = (
        ('sub_strategies', []),  # [(name, {param: value, ...}), ...]
        ('combine_mode', 'AND'),  # AND, OR, MAJORITY
    )

    def __init__(self):
        super().__init__()
        self.dataclose = self.datas[0].close
        # 为每个子策略初始化指标
        for name, params in self.p.sub_strategies:
            self._init_sub_indicators(name, params)

    def _init_sub_indicators(self, name, params):
        if name == "双均线策略":
            self.sma_fast = bt.ind.SMA(period=params.get('pfast', 5))
            self.sma_slow = bt.ind.SMA(period=params.get('pslow', 10))
            self.cross_ma = bt.ind.CrossOver(self.sma_fast, self.sma_slow)
        elif name == "RSI策略":
            self.rsi = bt.ind.RSI(period=params.get('period', 14))
            self.rsi_ob = params.get('overbought', 70)
            self.rsi_os = params.get('oversold', 30)
        elif name == "MACD策略":
            self.macd = bt.ind.MACD(
                period_me1=params.get('fast', 12),
                period_me2=params.get('slow', 26),
                period_signal=params.get('signal', 9))
        elif name == "布林带策略":
            self.boll = bt.indicators.BollingerBands(self.datas[0], period=params.get('nk', 13))
            self.boll_top = self.boll.top
            self.boll_bot = self.boll.bot
        elif name == "均线交叉策略":
            self.sma1 = bt.ind.SMA(period=params.get('pfast', 10))
            self.sma2 = bt.ind.SMA(period=params.get('pslow', 30))
            self.cross_sma = bt.ind.CrossOver(self.sma1, self.sma2)
        elif name == "大于EMA策略":
            self.ema = bt.indicators.ExponentialMovingAverage(self.datas[0], period=params.get('maperiod', 15))
        elif name == "多指标策略":
            self.multi_sma = bt.ind.SMA(period=params.get('maperiod', 15))
            self.multi_rsi = bt.ind.RSI(period=14)
        elif name == "K线策略":
            self.kma = bt.ind.SMA(period=params.get('maperiod', 5))
        elif name == "趋势突破(原LSTM占位)":
            self.lstm_sma = bt.ind.SMA(period=params.get('lookback', 20))
            self.lstm_threshold = params.get('threshold', 0.02)
        elif name == "SSA策略":
            self.ssa_sma = bt.ind.SMA(period=params.get('window', 10))
            self.ssa_threshold = params.get('threshold', 0.01)
        elif name == "强化学习策略":
            self.rl_sma = bt.ind.SMA(period=params.get('momentum_period', 14))
            self.rl_threshold = params.get('entry_threshold', 0.02)
        elif name == "MyStrategy":
            self.my_fast = bt.ind.SMA(period=params.get('fast', 5))
            self.my_slow = bt.ind.SMA(period=params.get('slow', 20))
            self.my_cross = bt.ind.CrossOver(self.my_fast, self.my_slow)

    def _check_sub_signal(self, name):
        """检查单个子策略的买卖信号，返回 (buy, sell)"""
        try:
            if name == "双均线策略":
                return self.cross_ma[0] > 0, self.cross_ma[0] < 0
            elif name == "RSI策略":
                return self.rsi[0] < self.rsi_os, self.rsi[0] > self.rsi_ob
            elif name == "MACD策略":
                return self.macd.macd[0] > self.macd.signal[0], self.macd.macd[0] < self.macd.signal[0]
            elif name == "布林带策略":
                return self.dataclose[0] < self.boll_bot[0], self.dataclose[0] > self.boll_top[0]
            elif name == "均线交叉策略":
                return self.cross_sma[0] > 0, self.cross_sma[0] < 0
            elif name == "大于EMA策略":
                return self.dataclose[0] > self.ema[0], self.dataclose[0] < self.ema[0]
            elif name == "多指标策略":
                return (self.multi_rsi[0] < 50 and self.dataclose[0] > self.multi_sma[0]), \
                       (self.multi_rsi[0] > 50 and self.dataclose[0] < self.multi_sma[0])
            elif name == "K线策略":
                return self.dataclose[0] > self.kma[0], self.dataclose[0] < self.kma[0]
            elif name == "趋势突破(原LSTM占位)":
                return self.dataclose[0] > self.lstm_sma[0] * (1 + self.lstm_threshold), \
                       self.dataclose[0] < self.lstm_sma[0]
            elif name == "SSA策略":
                return self.dataclose[0] > self.ssa_sma[0] * (1 + self.ssa_threshold), \
                       self.dataclose[0] < self.ssa_sma[0] * (1 - self.ssa_threshold)
            elif name == "强化学习策略":
                return self.dataclose[0] > self.rl_sma[0] * (1 + self.rl_threshold), \
                       self.dataclose[0] < self.rl_sma[0] * (1 - self.rl_threshold)
            elif name == "MyStrategy":
                return self.my_cross[0] > 0, self.my_cross[0] < 0
        except Exception:
            pass
        return False, False

    def next(self):
        if self.order:
            return

        # 收集所有子策略信号
        buy_votes = []
        sell_votes = []
        for name, _ in self.p.sub_strategies:
            b, s = self._check_sub_signal(name)
            buy_votes.append(b)
            sell_votes.append(s)

        if not buy_votes:
            return

        # 根据组合模式决定最终信号
        mode = self.p.combine_mode
        if mode == 'AND':
            buy_signal = all(buy_votes)
            sell_signal = all(sell_votes)
        elif mode == 'OR':
            buy_signal = any(buy_votes)
            sell_signal = any(sell_votes)
        else:  # MAJORITY
            n = len(buy_votes)
            buy_signal = sum(buy_votes) > n / 2
            sell_signal = sum(sell_votes) > n / 2

        # 执行交易
        if not self.position:
            if buy_signal:
                self.buy_signal()
        else:
            if sell_signal:
                self.order = self.sell(size=self.position.size)


# 策略映射字典
STRATEGY_MAP = {
    "双均线策略": MA_Cross_Strategy,
    "RSI策略": RSI_Strategy,
    "MACD策略": MACD_Strategy,
    "布林带策略": BollStrategy,
    "均线交叉策略": SmaCross,
    "大于EMA策略": BiggerThanEmaStrategy,
    "多指标策略": MultiStrategy,
    "K线策略": KlinesStrategy,
    "趋势突破(原LSTM占位)": LSTMStrategy,
    "SSA策略": SSAStrategy,
    "强化学习策略": RLStrategy,
    "MyStrategy": MyStrategy,
    "唐奇安通道长线": DonchianLongStrategy,
    "双均线长线": DualMALongStrategy,
    "低频动量长线": MomentumLongStrategy,
    "海龟策略": TurtleStrategy,
    "网格策略": GridStrategy,
    "StochRSI策略": StochRSIStrategy,
    "单因子-相对强弱指数RSI": RSI_Strategy,
    "单因子-随机相对强弱指数StochRSI": StochRSIStrategy,
    "单因子-RSRS择时": RSRSMarketStrategy,
    "单因子-移动均线+KDJ": MA_KDJ_Strategy,
    "单因子-MACD和ADX指标": MACD_ADX_Strategy,
    "单因子-布林线均值回归": BollStrategy,
    "单因子-阿隆指标(趋势交易)": AroonStrategy,
    "单因子-简单移动均线": KlinesStrategy,
    "单因子-情绪指标ARBR": ARBRStrategy,
    "单因子-市场低估值策略": PricePercentileStrategy,
    "机器学习-麻雀优化算法SSA": SSAOptimizedMAStrategy,
    "机器学习-支持向量机SVM": SVMPredStrategy,
    "机器学习-MLP时序分类": LSTMPredStrategy,
    "机器学习-LGBM模型价格预测": LGBMPredStrategy,
    "机器学习-随机森林模型价格预测": RFPredStrategy,
    "机器学习-线性回归价格预测": LinRegPredStrategy,
    "强化学习-RL模型价格预测": QLearningStrategy,
    "强化学习-Q-Leaning预测": QLearningStrategy,
    "传统策略-海龟策略": TurtleStrategy,
    "传统策略-网格策略": GridStrategy,
    "多因子-ROC(20)动量信号周频Top1": MomentumLongStrategy,
    "混合策略": HybridStrategy,
}


def _akshare_fetch(code, start_date, end_date, market="A股", adjust="前复权", period="日线"):
    """AKShare 原始取数（直连网络，不含缓存）。返回 date 索引的 OHLCV + openinterest。"""
    import time
    import random

    adjust_map = {"前复权": "qfq", "后复权": "hfq", "不复权": ""}
    adjust_code = adjust_map.get(adjust, "qfq")

    # 周期 -> AKShare 接口 period 参数
    period_map = {"30分钟": "30", "60分钟": "60", "日线": "daily", "周线": "weekly"}
    period_code = period_map.get(period, "daily")

    # 根据市场调整延时（分钟线限流更严，加长）
    if period_code in ("30", "60"):
        delay = random.uniform(4, 7)
    elif market == "A股":
        delay = random.uniform(2, 4)
    elif market == "港股":
        delay = random.uniform(3, 6)
    else:
        delay = random.uniform(1, 3)

    time.sleep(delay)

    start = start_date.replace('-', '')
    end = end_date.replace('-', '')

    logger.info(f"获取{market}数据: code={code}, start={start}, end={end}, period={period}")

    try:
        if market == "A股":
            if period_code in ("30", "60"):
                # 分钟线：ak.stock_zh_a_hist_min_em
                df = ak.stock_zh_a_hist_min_em(symbol=code, period=period_code,
                                               start_date=start + " 09:30:00",
                                               end_date=end + " 15:00:00",
                                               adjust=adjust_code)
                rename_map = {
                    '时间': 'date', '开盘': 'open', '收盘': 'close',
                    '最高': 'high', '最低': 'low', '成交量': 'volume'
                }
            else:
                df = ak.stock_zh_a_hist(symbol=code, period=period_code,
                                        start_date=start, end_date=end, adjust=adjust_code)
                rename_map = {
                    '日期': 'date', '开盘': 'open', '收盘': 'close',
                    '最高': 'high', '最低': 'low', '成交量': 'volume'
                }
            
        elif market == "港股":
            code_hk = code.zfill(5)
            # 尝试多个接口
            df = None
            
            # 接口1: 带 adjust 参数
            try:
                df = ak.stock_hk_hist(symbol=code_hk, period="daily",
                                      start_date=start, end_date=end, adjust=adjust_code)
                if df is not None and not df.empty:
                    logger.info(f"港股接口1成功")
            except Exception as e:
                logger.warning(f"港股接口1失败: {e}")
            
            # 接口2: 不带 adjust 参数
            if df is None or df.empty:
                try:
                    df = ak.stock_hk_hist(symbol=code_hk, period="daily",
                                          start_date=start, end_date=end)
                    if df is not None and not df.empty:
                        logger.info(f"港股接口2成功")
                except Exception as e:
                    logger.warning(f"港股接口2失败: {e}")
            
            rename_map = {
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume'
            }
            
        elif market == "美股":
            code_us = code.upper()
            df = None
            
            # 接口1: 带 adjust 参数
            try:
                df = ak.stock_us_hist(symbol=code_us, period="daily",
                                      start_date=start, end_date=end, adjust=adjust_code)
                if df is not None and not df.empty:
                    logger.info(f"美股接口1成功")
            except Exception as e:
                logger.warning(f"美股接口1失败: {e}")
            
            # 接口2: 不带 adjust 参数
            if df is None or df.empty:
                try:
                    df = ak.stock_us_hist(symbol=code_us, period="daily",
                                          start_date=start, end_date=end)
                    if df is not None and not df.empty:
                        logger.info(f"美股接口2成功")
                except Exception as e:
                    logger.warning(f"美股接口2失败: {e}")
            
            rename_map = {
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume'
            }
        else:
            raise ValueError(f"不支持的市场: {market}")
        
        # 检查结果
        if df is None:
            logger.error(f"获取{market}数据失败: 返回 None")
            return pd.DataFrame()
        
        if df.empty:
            logger.warning(f"获取{market}数据为空: code={code}")
            return pd.DataFrame()
        
        logger.info(f"成功获取{market}数据: {len(df)} 条记录")
        
        df.rename(columns=rename_map, inplace=True)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df['openinterest'] = 0
        
        return df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]
        
    except Exception as e:
        logger.error(f"获取{market}数据失败: {e}")
        raise  # 让重试机制处理


def get_data_akshare(code, start_date, end_date, market="A股", adjust="前复权", period="日线"):
    """AKShare 取数（带本地缓存）。支持 A股、港股、美股。

    period ∈ {"30分钟", "60分钟", "日线", "周线"}；分钟/周线仅 A股支持。
    缓存命中时不联网，也就跳过了下面的限流 sleep——重复回测同一区间会快很多。
    """
    from qbot.core.market_cache import cached_ohlcv

    key = f"{market}_{code}_{period}_{adjust}"

    def _fetch(s, e):
        return _akshare_fetch(code, s, e, market=market, adjust=adjust, period=period)

    try:
        df, from_cache = cached_ohlcv('ak', key, start_date, end_date, _fetch,
                                       adjust=adjust)
    except Exception as e:
        logger.warning(f"[AKShare] 缓存路径失败({e})，回落直连:")
        df = _akshare_fetch(code, start_date, end_date, market=market,
                            adjust=adjust, period=period)
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.copy()
    df['openinterest'] = 0
    logger.info(f"[AKShare] {code} {len(df)} 条"
                f"{'（本地缓存）' if from_cache else '（实时拉取）'}")
    return df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]


def get_data_baostock(code, start_date, end_date, market="A股", adjust="前复权", period="日线"):
    """从 Baostock 获取A股K线数据。BaoStock 原生支持：日线 d / 周线 w / 月线 m / 5·15·30·60 分钟。"""
    logger.info(f"[BaoStock] 获取数据: code={code}, start={start_date}, end={end_date}, period={period}")

    if market != "A股":
        raise ValueError(f"BaoStock 仅支持A股，不支持 {market}")

    if bs is None:
        raise RuntimeError("未安装 baostock，无法使用 BaoStock 数据源；"
                           "请改用 AKShare 或执行 pip install baostock")

    # 周期 -> baostock frequency；30/60 分钟线限近两年内（baostock 分钟数据范围限制）
    freq_map = {"日线": "d", "周线": "w", "月线": "m", "30分钟": "30", "60分钟": "60",
                "5分钟": "5", "15分钟": "15"}
    if period not in freq_map:
        raise ValueError(f"BaoStock 不支持周期 {period}（可选: {list(freq_map)}）")
    frequency = freq_map[period]

    # BaoStock 日线走本地缓存：首次拉后存 qbot/data/market/bs/<code>.csv，
    # 后续只增量补缺失日。断网时回测仍可读历史缓存跑通。
    if frequency == 'd':
        try:
            from qbot.core.market_cache import fetch_with_cache as _bs_cached
            code_str = code.strip()
            bs_code = f"sh.{code_str}" if code_str.startswith(('6', '9')) else f"sz.{code_str}"
            df, from_cache = _bs_cached(bs_code, start_date, end_date, adjust=adjust)
            if df is not None and not df.empty:
                df = df[['open', 'high', 'low', 'close', 'volume']].copy()
                df['openinterest'] = 0
                logger.info(f"[BaoStock] {bs_code} {len(df)} 条"
                            f"{'（本地缓存）' if from_cache else '（实时拉取）'}")
                return df
        except Exception as e:
            logger.warning(f"[BaoStock] 缓存路径失败，回落到直连拉取: {e}")

    try:
        # 登录
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError(f"BaoStock 登录失败: {lg.error_msg}")

        # 转换股票代码格式：000001 -> sz.000001, 600000 -> sh.600000
        code_str = code.strip()
        if code_str.startswith(('6', '9')):
            bs_code = f"sh.{code_str}"
        else:
            bs_code = f"sz.{code_str}"

        # 格式化日期
        start = start_date.replace('-', '')
        end = end_date.replace('-', '')

        # 请求K线；baostock 复权标志: 2=前复权 1=后复权 3=不复权
        baostock_adjust = {"前复权": "2", "后复权": "1", "不复权": "3"}
        adjustflag = baostock_adjust.get(adjust, "2")
        fields = "date,code,open,high,low,close,volume,adjustflag"
        rs = bs.query_history_k_data_plus(
            bs_code, fields,
            start_date=start_date, end_date=end_date,
            frequency=frequency, adjustflag=adjustflag
        )

        if rs.error_code != '0':
            raise RuntimeError(f"BaoStock 查询失败: {rs.error_msg}")

        # 组装结果
        data_list = []
        while (rs.error_code == '0') & rs.next():
            row = rs.get_row_data()
            data_list.append(row)

        if not data_list:
            logger.warning(f"[BaoStock] 未获取到数据: {bs_code}")
            return pd.DataFrame()

        df = pd.DataFrame(data_list, columns=rs.fields)

        # 类型转换
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 删除空行
        df.dropna(subset=['open', 'close'], inplace=True)

        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df['openinterest'] = 0

        logger.info(f"[BaoStock] 成功获取 {len(df)} 条记录")
        # 顺手写入本地缓存（只日线），下次同区间可直接命中
        if frequency == 'd':
            try:
                from qbot.core.market_cache import save_bs_rows as _bs_save
                _bs_save(bs_code, df[['open', 'high', 'low', 'close', 'volume']].copy(),
                         adjust=adjust)
            except Exception as e:
                logger.debug(f"[BaoStock] 写缓存跳过: {e}")
        return df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]

    except Exception as e:
        logger.error(f"[BaoStock] 获取数据失败: {e}")
        raise
    finally:
        bs.logout()


BAOSTOCK_INDEX_MAP = {"000300": "sh.000300", "000016": "sh.000016",
                      "000905": "sh.000905", "000001": "sh.000001"}


def _index_fetch(index_code, start_date, end_date):
    """基准指数原始取数（直连网络）：先东财(带重试)，失败回退 BaoStock。

    ponytail: 东财在某些网络下 100% 连不上，每次还烧 2 次重试+sleep（~10s）。
    进程内熔断：连续 2 次失败后本会话直接走 BaoStock，成功一次即复位。
    """
    import time as _t
    global _EM_INDEX_FAILS
    if _EM_INDEX_FAILS < 2:
        last_err = None
        for attempt in range(2):
            try:
                df = get_index_data_akshare(index_code, start_date, end_date)
                if df is not None and not df.empty:
                    _EM_INDEX_FAILS = 0
                    return df
            except Exception as e:
                last_err = e
                logger.warning(f"[基准] 东财接口第{attempt + 1}次失败: {e}")
                _t.sleep(1.5 * (attempt + 1))
        _EM_INDEX_FAILS += 1
        if _EM_INDEX_FAILS >= 2:
            logger.warning("[基准] 东财连续失败，本次会话后续直接用 BaoStock")
    try:
        bs_code = BAOSTOCK_INDEX_MAP.get(index_code)
        if bs_code is None:
            logger.warning(f"[基准] 未知指数代码 {index_code}，BaoStock 无法兜底")
            return pd.DataFrame()
        lg = bs.login()
        if lg.error_code != '0':
            raise ConnectionError(f"BaoStock 登录失败: {lg.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                bs_code, "date,close",
                start_date=start_date, end_date=end_date,
                frequency="d")
            if rs.error_code != '0':
                raise RuntimeError(f"BaoStock 查询失败: {rs.error_msg}")
            rows = []
            while (rs.error_code == '0') and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                logger.warning(f"[基准] BaoStock 未取到数据: {bs_code}")
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=["date", "close"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df.dropna(inplace=True)
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            logger.info(f"[基准] BaoStock 兜底成功: {bs_code} {len(df)} 条")
            return df[["close"]]
        finally:
            bs.logout()
    except Exception as e:
        logger.warning(f"[基准] BaoStock 兜底也失败: {e} (东财最后错误: {last_err})")
        return pd.DataFrame()


def get_index_data(index_code, start_date, end_date):
    """获取基准指数日线（带本地缓存）。返回带 date 索引、仅含 close 列的 DataFrame。

    指数点位不存在复权问题，所以按「不复权」缓存：只增量补缺失交易日，永不过期重拉。
    每次回测都要用基准算超额，这条路径以前每次都联网，缓存后离线也能出超额曲线。
    """
    from qbot.core.market_cache import cached_ohlcv

    def _fetch(s, e):
        return _index_fetch(index_code, s, e)

    try:
        df, from_cache = cached_ohlcv('index', f"{index_code}_d", start_date, end_date,
                                       _fetch, adjust="不复权")
    except Exception as e:
        logger.warning(f"[基准] 缓存路径异常({e})，回落直连")
        df = _index_fetch(index_code, start_date, end_date)

    if df is None or len(df) == 0 or 'close' not in df.columns:
        return pd.DataFrame()
    logger.info(f"[基准] {index_code} {len(df)} 条"
                f"{'（本地缓存）' if from_cache else '（实时拉取）'}")
    return df[['close']]


def get_index_data_akshare(index_code, start_date, end_date):
    """获取A股指数日线（沪深300=000300 / 上证50=000016 / 中证500=000905），
    返回带 date 索引、仅含 close 列的 DataFrame"""
    start = start_date.replace('-', '')
    end = end_date.replace('-', '')
    df = ak.index_zh_a_hist(symbol=index_code, period="daily",
                            start_date=start, end_date=end)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={'日期': 'date', '收盘': 'close'})
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    return df[['close']]


def compute_drawdown(equity, reset_at=()):
    """equity: [(date, value)]，reset_at: 需重置峰值的段起点索引（如样本外训练/测试接缝）。
    返回 [{'time': d, 'value': 回撤%}]（值 ≤ 0）"""
    out = []
    peak = None
    reset_set = set(reset_at)
    for i, (d, v) in enumerate(equity):
        if i in reset_set or peak is None or v > peak:
            peak = v
        dd = (v / peak - 1) * 100 if peak > 0 else 0.0
        out.append({'time': d, 'value': round(dd, 2)})
    return out


def compute_monthly_returns(equity):
    """equity: [(date, value)] → 按月首尾净值算月度收益% [{'time': 'YYYY-MM-01', 'value': pct}]"""
    firsts, lasts, order = {}, {}, []
    for d, v in equity:
        key = d[:7]
        if key not in firsts:
            firsts[key] = v
            lasts[key] = v
            order.append(key)
        else:
            lasts[key] = v
    out = []
    for key in order:
        f0, l0 = firsts[key], lasts[key]
        if f0 > 0:
            out.append({'time': key + '-01', 'value': round((l0 / f0 - 1) * 100, 2)})
    return out


def compute_drawdown_recovery(equity):
    """从净值曲线算「水下时长」：从跌破前高到重新创新高，经过多少个交易日。

    equity: [(date_str, value)]，需按时间升序。
    返回 (最长修复交易日数, 期末是否仍在水下)
    回撤深度之外，修复时间同样是实盘体验的核心——回撤 20% 三个月修复和一年修复，
    是完全不同的两套策略。
    """
    if not equity or len(equity) < 2:
        return 0, False
    peak = float(equity[0][1])
    peak_i = 0
    worst = 0        # 最长水下长度（交易日）
    cur_len = 0
    for i, (_d, v) in enumerate(equity):
        v = float(v)
        if v >= peak:
            peak = v
            peak_i = i
            cur_len = 0
        else:
            cur_len = i - peak_i
            if cur_len > worst:
                worst = cur_len
    underwater = (len(equity) - 1) - peak_i > 0
    return worst, underwater


def compute_trade_pnl(trades):
    """trades: [(date, price, 'buy'/'sell')]，按顺序买卖配对计算单笔盈亏%。
    返回 (pnl_points, stats_dict)"""
    pnl = []
    entry = None
    wins = losses = 0
    sum_win = sum_loss = 0.0
    for d, price, t in trades:
        if t == 'buy':
            if entry is None:
                entry = price
        elif entry is not None and entry > 0:
            p = (price / entry - 1) * 100
            pnl.append({'time': d, 'value': round(p, 2),
                        'color': '#26a69a' if p >= 0 else '#ef5350'})
            if p >= 0:
                wins += 1
                sum_win += p
            else:
                losses += 1
                sum_loss += p
            entry = None
    n = wins + losses
    stats = {
        'count': n,
        'win_rate': f"{wins / n * 100:.1f}%" if n else '--',
        'profit_factor': (f"{sum_win / abs(sum_loss):.2f}" if sum_loss < 0
                          else ('∞' if sum_win > 0 else '--')),
        'avg_win': f"{sum_win / wins:+.2f}%" if wins else '--',
        'avg_loss': f"{sum_loss / losses:+.2f}%" if losses else '--',
        'max_win': f"{max((x['value'] for x in pnl), default=0.0):+.2f}%",
        'max_loss': f"{min((x['value'] for x in pnl), default=0.0):+.2f}%",
    }
    return pnl, stats


from qbot.gui.panels.chart_panel_web import ChartPanel  # noqa: E402  (webview+LightweightCharts 实现)


class MyBacktestPanel(wx.Panel, FactorPanelMixin, StrategyInstallerMixin, OptimizerMixin, StockPoolMixin, StockScoreMixin, PositionMixin,
                       WeightOptLauncherMixin):
    """专业回测面板 - 支持多策略"""
    
    def __init__(self, parent=None, id=-1, displaySize=(1600, 900)):
        super().__init__(parent)
        
        # 使用整合后的策略映射
        self.strategy_map = STRATEGY_MAP
        
        # 获取策略列表
        self.strategy_list = list(self.strategy_map.keys())
        
        logger.info(f"可用策略: {self.strategy_list}")
        
        self.backtest_opts = {
            "start_time": "20260101",
            "end_time": "20261231",
            "benchmark": "000300.SH",
            "code": "000001",
            "select_strategy": self.strategy_list[0] if self.strategy_list else "双均线策略",
        }
        
        self.backtest_config = {
            "init_cash": 10000,
            "commission": 0.0005,
            "slippage": 0.1,
            "stake": 100,
        }
        
        # 混合策略配置
        self.hybrid_enabled_strategies = {}  # {name: True/False}
        self.hybrid_param_widgets = {}       # {name: {param: widget}}
        self.hybrid_widget_refs = []         # 所有混合策略控件的引用，用于显示/隐藏

        # 用户是否手动改过「延迟成交」开关；改过就不再套用策略族默认值
        self._friction_delay_user_set = False
        
        self.M1_width = int(displaySize[0] * 0.23)
        self.M2_width = int(displaySize[0] * 0.8)
        
        self._data_cache = {}   # {(source, market, code, start, end, adjust): (df, name)}
        self._name_cache = {}   # {(market, code): name}
        self._cache_lock = threading.Lock()
        self._busy = False          # 加载/回测进行中标志，防止任务重入
        self.last_report_path = None
        self.chart_panel = None
        self.init_ui()
        
    def init_ui(self):
        main_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # ===== 左侧：三页签（回测/因子/策略库）+ 底部固定操作区 =====
        left_panel = wx.Panel(self, size=(self.M1_width, -1))
        left_panel.SetMinSize(wx.Size(self.M1_width, -1))
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_panel.SetSizer(left_sizer)

        self.left_book = wx.Notebook(left_panel)
        self.left_book.SetFont(FONTS.body())

        # --- 页签1: 回测 ---
        self.tab_backtest = wx.ScrolledWindow(self.left_book)
        self.tab_backtest.SetBackgroundColour(COLORS.BG)
        self.tab_backtest.SetScrollRate(0, 10)
        tab1_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tab_backtest.SetSizer(tab1_sizer)
        # 两个 *_para_sizer 各自创建并返回自己的 BoxSizer，必须挂到页签 sizer 上
        tab1_sizer.Add(self._create_stock_para_sizer(self.tab_backtest), 1, wx.EXPAND)
        tab1_sizer.Add(self._create_backtest_para_sizer(self.tab_backtest), 1, wx.EXPAND)
        self.tab_backtest.FitInside()

        # --- 页签2: 因子 ---
        self.tab_factor = wx.ScrolledWindow(self.left_book)
        self.tab_factor.SetBackgroundColour(COLORS.BG)
        self.tab_factor.SetScrollRate(0, 10)
        tab2_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tab_factor.SetSizer(tab2_sizer)
        factor_pane, factor_ps = self._add_section(self.tab_factor, tab2_sizer, "量化因子分析", icon="📊")
        self._create_factor_module(factor_pane, factor_ps)
        self.tab_factor.FitInside()

        # --- 页签3: 策略库 ---
        self.tab_installer = wx.ScrolledWindow(self.left_book)
        self.tab_installer.SetBackgroundColour(COLORS.BG)
        self.tab_installer.SetScrollRate(0, 10)
        tab3_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tab_installer.SetSizer(tab3_sizer)
        installer_pane, installer_ps = self._add_section(self.tab_installer, tab3_sizer, "拖拽安装新策略", icon="🧩")
        self._create_strategy_installer(installer_pane, installer_ps)
        self.tab_installer.FitInside()

        # --- 页签4: 股票池 ---
        self.tab_pool = wx.ScrolledWindow(self.left_book)
        self.tab_pool.SetBackgroundColour(COLORS.BG)
        self.tab_pool.SetScrollRate(0, 10)
        tab4_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tab_pool.SetSizer(tab4_sizer)
        self._create_stock_pool(self.tab_pool, tab4_sizer)
        self._create_stock_score(self.tab_pool, tab4_sizer)
        self.tab_pool.FitInside()

        # --- 页签5: 持仓与信号 ---
        self.tab_positions = wx.ScrolledWindow(self.left_book)
        self.tab_positions.SetBackgroundColour(COLORS.BG)
        self.tab_positions.SetScrollRate(0, 10)
        tab5_sizer = wx.BoxSizer(wx.VERTICAL)
        self.tab_positions.SetSizer(tab5_sizer)
        pos_pane, pos_ps = self._add_section(self.tab_positions, tab5_sizer,
                                             "持仓与信号清单", icon="☰")
        self._create_positions(pos_pane, pos_ps)
        self.tab_positions.FitInside()

        self.left_book.AddPage(self.tab_backtest, "  回测  ")
        self.left_book.AddPage(self.tab_factor, "  因子  ")
        self.left_book.AddPage(self.tab_installer, "  策略库  ")
        self.left_book.AddPage(self.tab_pool, "  股票池  ")
        self.left_book.AddPage(self.tab_positions, "  持仓  ")
        left_sizer.Add(self.left_book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)

        # --- 底部状态条（固定，含状态圆点）---
        status_strip = wx.Panel(left_panel)
        status_strip.SetBackgroundColour(COLORS.CARD)
        ss = wx.BoxSizer(wx.HORIZONTAL)
        self.status_dot = wx.Panel(status_strip, size=(10, 10))
        self.status_dot.SetBackgroundColour(COLORS.IDLE)
        ss.Add(self.status_dot, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.load_status = wx.StaticText(status_strip, label="就绪 · 请先加载数据，再开始回测")
        self.load_status.SetFont(FONTS.small())
        self.load_status.SetForegroundColour(COLORS.TEXT_SUB)
        self.load_status.Wrap(self.M1_width - 60)
        ss.Add(self.load_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        status_strip.SetSizer(ss)
        left_sizer.Add(status_strip, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)

        # --- 底部固定操作区（任何页签都可见）---
        btn_area = wx.Panel(left_panel)
        btn_area.SetBackgroundColour(COLORS.BG)
        ba = wx.BoxSizer(wx.VERTICAL)
        self.start_back_but = make_button(btn_area, "▶  开始回测", kind="success", height=40, big=True)
        self.start_back_but.Bind(wx.EVT_BUTTON, self.on_run_backtest)
        ba.Add(self.start_back_but, 0, wx.EXPAND | wx.ALL, 6)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.trade_log_but = make_button(btn_area, "📋 交易日志", kind="ghost", height=28)
        self.trade_log_but.Bind(wx.EVT_BUTTON, self.on_trade_log)
        self.report_but = make_button(btn_area, "📄 完整报告", kind="ghost", height=28)
        self.report_but.Bind(wx.EVT_BUTTON, self.on_open_report)
        row.Add(self.trade_log_but, 1, wx.RIGHT, 4)
        row.Add(self.report_but, 1)
        ba.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        btn_area.SetSizer(ba)
        left_sizer.Add(btn_area, 0, wx.EXPAND)

        # 可拖拽分割条（6px 宽）
        self.divider = wx.Panel(self, size=(6, -1))
        self.divider.SetBackgroundColour(wx.Colour(180, 188, 196))
        self.divider.SetCursor(wx.Cursor(wx.CURSOR_SIZEWE))
        self.divider.Bind(wx.EVT_LEFT_DOWN, self._on_divider_down)
        self.divider.Bind(wx.EVT_LEFT_UP, self._on_divider_up)
        self.divider.Bind(wx.EVT_MOTION, self._on_divider_motion)
        self._divider_dragging = False
        self._divider_start_x = 0
        self._left_start_width = 0

        # 右侧结果区：单页图表（顶部指标栏展示核心指标，完整报告改为浏览器打开）
        right_panel = wx.Panel(self)
        self.chart_panel = ChartPanel(right_panel)
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        right_sizer.Add(self.chart_panel, 1, wx.EXPAND)
        right_panel.SetSizer(right_sizer)

        # 水平布局：左面板 | 分割条 | 右面板
        main_sizer.Add(left_panel, 0, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.LEFT, 5)
        main_sizer.Add(self.divider, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 5)
        main_sizer.Add(right_panel, 1, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, 5)

        # 保存面板引用供分割条拖动使用
        self._left_panel = left_panel
        self._right_panel = right_panel

        self.SetSizer(main_sizer)
        self.Layout()

        # 面板初始策略也套用一次「长线族默认延迟成交」
        self._apply_friction_delay_default(self.strategy_choice.GetValue())

        self.show_welcome()
    
    def _force_relayout(self):
        """窗口完全显示后强制重新计算布局，修复初始渲染错位"""
        self.Layout()
        self.Refresh()
        # 发送窗口大小变化事件，触发 SplitterWindow 及其子面板重新计算
        self.SendSizeEvent()
    
    # ===== 可拖拽分割条事件 =====
    def _on_divider_down(self, event):
        self._divider_dragging = True
        self._divider_start_x = event.GetX() + self.divider.GetScreenRect().x
        self._left_start_width = self._left_panel.GetSize().GetWidth()
        self.divider.CaptureMouse()
        event.Skip()
    
    def _on_divider_up(self, event):
        if self._divider_dragging:
            self._divider_dragging = False
            self.divider.ReleaseMouse()
        event.Skip()
    
    def _on_divider_motion(self, event):
        if self._divider_dragging and event.Dragging():
            dx = (event.GetX() + self.divider.GetScreenRect().x) - self._divider_start_x
            new_width = max(250, self._left_start_width + dx)
            self._left_panel.SetMinSize(wx.Size(new_width, -1))
            self.Layout()
    
    def _add_section(self, parent, parent_sizer, label, expanded=True, icon=""):
        """左侧参数区的折叠卡片：返回 (内容容器, 内容垂直sizer)。"""
        card = SectionCard(parent, label, icon=icon, expanded=expanded,
                           on_toggle=self._on_card_toggled)
        parent_sizer.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        return card.body, card.body_sizer

    def _on_card_toggled(self):
        """卡片收展后刷新滚动区虚拟高度与整体布局（安全：不触碰祖先尺寸）"""
        wx.CallAfter(self._layout_full_chain)

    def _on_section_toggled(self, event):
        self._layout_full_chain()

    @staticmethod
    def _field_row(parent, pairs):
        """pairs: [(label, ctrl, proportion), ...] → 一行「标签+控件」横向布局"""
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, ctrl, prop in pairs:
            if label:
                row.Add(wx.StaticText(parent, label=label), 0,
                        wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            row.Add(ctrl, prop, wx.EXPAND)
        return row

    def _create_stock_para_sizer(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ===== 📥 行情数据（核心区，默认展开）=====
        pane, ps = self._add_section(parent, sizer, "行情数据", icon="📥")

        self.data_source_choice = wx.ComboBox(
            pane, choices=["AKShare", "BaoStock"], value="BaoStock",
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        self.market_choice = wx.ComboBox(
            pane, choices=["A股", "港股", "美股"], value="A股",
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        ps.Add(self._field_row(pane, [("数据源", self.data_source_choice, 1),
                                      ("市场", self.market_choice, 1)]),
               0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 8)

        self.stock_code_input = wx.TextCtrl(pane, -1, "000001", style=wx.TE_PROCESS_ENTER)
        self.stock_code_input.Bind(wx.EVT_TEXT, self._on_code_changed)
        self.stock_code_input.Bind(wx.EVT_TEXT_ENTER, self.on_load_data)
        code_row = self._field_row(pane, [("标的代码", self.stock_code_input, 1)])
        # 实时股票名称：输入完整代码后自动查询并显示
        self.stock_name_label = wx.StaticText(pane, label="")
        self.stock_name_label.SetFont(FONTS.small())
        self.stock_name_label.SetForegroundColour(COLORS.PRIMARY)
        code_row.Add(self.stock_name_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        ps.Add(code_row, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 8)

        # 日期选择：日历下拉控件（参照 panel_backtest.py），默认近一年
        dp_style = wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY | wx.adv.DP_ALLOWNONE
        self.start_date = wx.adv.DatePickerCtrl(pane, -1, style=dp_style)
        self.end_date = wx.adv.DatePickerCtrl(pane, -1, style=dp_style)
        now = wx.DateTime.Now()
        self.end_date.SetValue(now)
        self.start_date.SetValue(wx.DateTime.FromDMY(now.GetDay(), now.GetMonth(), now.GetYear() - 1))
        self.start_date.Bind(wx.adv.EVT_DATE_CHANGED, self._on_date_changed)
        self.end_date.Bind(wx.adv.EVT_DATE_CHANGED, self._on_date_changed)
        self._sync_backtest_date_opts()
        ps.Add(self._field_row(pane, [("开始", self.start_date, 1),
                                      ("结束", self.end_date, 1)]),
               0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 8)

        self.stock_period_cbox = wx.ComboBox(
            pane, -1, "日线", choices=["30分钟", "60分钟", "日线", "周线"],
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        self.stock_authority_cbox = wx.ComboBox(
            pane, -1, "前复权", choices=["前复权", "后复权", "不复权"],
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        ps.Add(self._field_row(pane, [("周期", self.stock_period_cbox, 1),
                                      ("复权", self.stock_authority_cbox, 1)]),
               0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 8)

        # 加载按钮（初级主按钮，位于卡片内——加载是回测前一步，引导更顺）
        self.load_data_but = make_button(pane, "📥  加载行情数据", kind="primary", height=34)
        self.load_data_but.Bind(wx.EVT_BUTTON, self.on_load_data)
        ps.Add(self.load_data_but, 0, wx.EXPAND | wx.ALL, 8)

        ps.Add(make_label(pane, "提示：输入代码或名称（A股/港股，如：茅台、小米）均可；回车或点击按钮加载",
                          size="small", color=COLORS.TEXT_HINT,
                          wrap_width=self.M1_width - 60),
               0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        return sizer
    
    def _create_backtest_para_sizer(self, parent):
        """回测参数区：策略 / 资金成本 / 进阶模拟 三张折叠卡片（因子与策略安装已移至独立页签）"""
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ===== 🎯 策略与参数（默认展开）=====
        pane, ps = self._add_section(parent, sizer, "策略与参数", icon="🎯")

        self.strategy_choice = wx.ComboBox(
            pane, choices=self.strategy_list,
            value=self.backtest_opts["select_strategy"],
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        self.strategy_choice.Bind(wx.EVT_COMBOBOX, self._on_strategy_changed)
        ps.Add(self.strategy_choice, 0, wx.EXPAND | wx.ALL, 8)

        self.strategy_desc = wx.StaticText(pane, label="双均线策略：5日线上穿10日线买入，下穿卖出")
        self.strategy_desc.SetForegroundColour(COLORS.TEXT_SUB)
        self.strategy_desc.Wrap(self.M1_width - 48)
        ps.Add(self.strategy_desc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # 参数寻优入口
        self.optimize_but = make_button(pane, "🔍 参数寻优", kind="ghost", height=30)
        self.optimize_but.Bind(wx.EVT_BUTTON, self.on_param_optimize)
        ps.Add(self.optimize_but, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # 独立策略参数（选中具体策略时显示）
        self.standalone_box = wx.StaticBox(pane, -1, "策略参数设置")
        self.standalone_sizer = wx.StaticBoxSizer(self.standalone_box, wx.VERTICAL)
        self.standalone_param_widgets = {}
        self.standalone_box.Show(False)
        ps.Add(self.standalone_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # 混合策略配置（选中「混合策略」时显示）
        self.hybrid_box = wx.StaticBox(pane, -1, "混合策略配置")
        self.hybrid_sizer = wx.StaticBoxSizer(self.hybrid_box, wx.VERTICAL)

        mode_sizer = wx.BoxSizer(wx.HORIZONTAL)
        mode_sizer.Add(wx.StaticText(self.hybrid_box, label="组合模式:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.hybrid_mode_choice = wx.ComboBox(
            self.hybrid_box, choices=["AND（全部确认）", "OR（任一确认）", "MAJORITY（多数决）"],
            value="AND（全部确认）", style=wx.CB_READONLY | wx.CB_DROPDOWN)
        mode_sizer.Add(self.hybrid_mode_choice, 1, wx.EXPAND)
        self.hybrid_sizer.Add(mode_sizer, 0, wx.EXPAND | wx.ALL, 5)

        self.hybrid_sizer.Add(wx.StaticLine(self.hybrid_box), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        hybrid_candidate_names = list(STRATEGY_PARAMS.keys())
        self.hybrid_checkboxes = {}
        self.hybrid_param_sizers = {}

        label_map = {
            'pfast': '快线', 'pslow': '慢线',
            'period': '周期', 'oversold': '超卖', 'overbought': '超买',
            'fast': '快线', 'slow': '慢线', 'signal': '信号',
            'nk': '周期', 'maperiod': '周期',
            'lookback': '回顾期', 'threshold': '阈值',
            'window': '窗口', 'momentum_period': '动量周期',
            'entry_threshold': '入场阈值',
            'freq': '调仓频率', 'cooldown': '冷却期',
            'entry_n': '突破窗口', 'exit_n': '离场窗口', 'fast': '快线', 'slow': '慢线',
            'lookback': '动量回看',
            'atr_n': 'ATR周期', 'atr_stop': 'ATR止损倍数',
            'step_pct': '每格涨幅%', 'n_grids': '资金格数',
            'rsi_n': 'RSI周期', 'stoch_n': '随机窗口',
            'oversold': '超卖', 'overbought': '超买',
            'zwin': 'z窗口', 'z_in': 'z入', 'z_out': 'z出',
            'br_low': 'BR低', 'br_high': 'BR高', 'ar_high': 'AR高',
            'low_pct': '低分位', 'high_pct': '高分位',
            'pop': '种群', 'ssa_iter': 'SSA迭代',
            'train_ratio': '训练比', 'eps': '探索率',
            'alpha': '学习率', 'gamma': '折扣',
        }
        for i, sname in enumerate(hybrid_candidate_names):
            params = STRATEGY_PARAMS[sname]

            cb = wx.CheckBox(self.hybrid_box, label=sname)
            cb.SetValue(i < 2)
            self.hybrid_checkboxes[sname] = cb
            cb.Bind(wx.EVT_CHECKBOX, self._on_hybrid_checkbox_changed)
            self.hybrid_sizer.Add(cb, 0, wx.ALL, 3)

            param_sizer = wx.BoxSizer(wx.HORIZONTAL)
            param_widgets = {}
            for pname, default in params.items():
                param_sizer.Add(wx.StaticText(self.hybrid_box, label=f"{label_map.get(pname, pname)}:"),
                                0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
                tc = wx.TextCtrl(self.hybrid_box, value=str(default), size=(45, -1))
                param_widgets[pname] = tc
                param_sizer.Add(tc, 0, wx.LEFT, 2)

            self.hybrid_param_sizers[sname] = (param_sizer, param_widgets)
            self.hybrid_sizer.Add(param_sizer, 0, wx.LEFT, 20)

        ps.Add(self.hybrid_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # 混合策略控件引用（供显示/隐藏）
        self.hybrid_widget_refs = [
            self.hybrid_box, self.hybrid_sizer, self.hybrid_mode_choice,
        ] + list(self.hybrid_checkboxes.values())
        for psizer, _ in self.hybrid_param_sizers.values():
            self.hybrid_widget_refs.append(psizer)

        self._toggle_hybrid_ui(False)

        # ===== ⚙️ 资金与成本（默认展开）=====
        pane, ps = self._add_section(parent, sizer, "资金与成本", icon="⚙️")

        self.benchmark_choice = wx.ComboBox(
            pane,
            choices=["沪深300指数(000300)", "上证50指数(000016)", "中证500指数(000905)"],
            value="沪深300指数(000300)",
            style=wx.CB_READONLY | wx.CB_DROPDOWN)
        ps.Add(self._field_row(pane, [("基准", self.benchmark_choice, 1)]),
               0, wx.EXPAND | wx.ALL, 8)

        self.init_cash = wx.TextCtrl(pane, value="10000")
        self.stake_input = wx.TextCtrl(pane, value="100")
        ps.Add(self._field_row(pane, [("初始资金", self.init_cash, 1),
                                      ("每手股数", self.stake_input, 1)]),
               0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.commission = wx.TextCtrl(pane, value="0.0005")
        self.slippage_input = wx.TextCtrl(pane, value="0.1")
        self.tax_input = wx.TextCtrl(pane, value="0.001")
        ps.Add(self._field_row(pane, [("佣金", self.commission, 1),
                                      ("滑点%", self.slippage_input, 1),
                                      ("印花税", self.tax_input, 1)]),
               0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ===== 🛡️ 进阶模拟（默认收起）=====
        pane, ps = self._add_section(parent, sizer, "进阶模拟", icon="🛡️", expanded=False)

        friction_hint = wx.StaticText(pane, label="模拟实盘摩擦：滑点/手续费放大、延迟、涨跌停，勾选后生效")
        friction_hint.SetForegroundColour(COLORS.TEXT_SUB)
        friction_hint.Wrap(self.M1_width - 48)
        ps.Add(friction_hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

        self.friction_slip_dbl = wx.CheckBox(pane, label="滑点放大 ×")
        self.friction_slip_factor = wx.TextCtrl(pane, value="2.0", size=(52, -1))
        ps.Add(self._field_row(pane, [(None, self.friction_slip_dbl, 1),
                                      (None, self.friction_slip_factor, 0)]),
               0, wx.EXPAND | wx.ALL, 4)

        self.friction_comm_dbl = wx.CheckBox(pane, label="手续费放大 ×")
        self.friction_comm_factor = wx.TextCtrl(pane, value="2.0", size=(52, -1))
        ps.Add(self._field_row(pane, [(None, self.friction_comm_dbl, 1),
                                      (None, self.friction_comm_factor, 0)]),
               0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.friction_delay_cb = wx.CheckBox(pane, label="延迟：多等一日成交（长线策略族默认开）")
        self.friction_delay_cb.Bind(wx.EVT_CHECKBOX, self._on_friction_delay_toggled)
        ps.Add(self.friction_delay_cb, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.friction_circuit_cb = wx.CheckBox(pane, label="熔断：涨跌停一字板拒单（A股±10%）")
        ps.Add(self.friction_circuit_cb, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.oos_enable_cb = wx.CheckBox(pane, label="样本外测试（训练/测试对比）")
        self.oos_train_ratio = wx.TextCtrl(pane, value="0.7", size=(52, -1))
        ps.Add(self._field_row(pane, [(None, self.oos_enable_cb, 1),
                                      ("训练比", self.oos_train_ratio, 0)]),
               0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        return sizer

    def _on_date_changed(self, event):
        """日历选择变化时同步回 backtest_opts（参照 panel_backtest.py 行为）"""
        self._sync_backtest_date_opts()
        event.Skip()

    def _sync_backtest_date_opts(self):
        self.backtest_opts["start_time"] = self._get_start_date_str().replace('-', '')
        self.backtest_opts["end_time"] = self._get_end_date_str().replace('-', '')

    def _get_start_date_str(self):
        """DatePickerCtrl 值 → 'YYYY-MM-DD'"""
        return self.start_date.GetValue().Format("%Y-%m-%d")

    def _get_end_date_str(self):
        return self.end_date.GetValue().Format("%Y-%m-%d")

    def register_user_strategy(self, display_name, cls, params):
        """注册拖拽安装的用户策略：映射表 + 参数表 + 下拉框（安装器 Mixin 调用）"""
        self.strategy_map[display_name] = cls
        STRATEGY_PARAMS[display_name] = dict(params or {})
        if display_name not in self.strategy_list:
            self.strategy_list.append(display_name)
        if self.strategy_choice.FindString(display_name) == -1:
            self.strategy_choice.Append(display_name)

    def _on_code_changed(self, event):
        """输入代码时自动识别市场并填充下拉框，并实时查询显示股票名称；输入名称时反向搜索"""
        code = self.stock_code_input.GetValue().strip()
        # 中文等非ASCII按名称处理，市场识别只对真实代码生效
        market = None if any(ord(c) > 127 for c in code) else detect_market(code)
        if market and hasattr(self, 'market_choice'):
            self.market_choice.SetValue(market)
            # BaoStock 仅支持A股：识别到港股/美股代码时自动切到 AKShare
            if market != "A股" and self.data_source_choice.GetValue() == "BaoStock":
                self.data_source_choice.SetValue("AKShare")
        self._update_name_label_async(code)

    def _name_search_async(self, text):
        """按名称后台搜索A股：唯一匹配自动回填代码，多个匹配在标签列出"""
        self._name_fetch_token = getattr(self, '_name_fetch_token', 0) + 1
        token = self._name_fetch_token

        def worker():
            matches = _search_by_name(text)
            if token != self._name_fetch_token:
                return
            if len(matches) == 1:
                c, n = matches[0]
                mkt = detect_market(c)

                def fill():
                    if mkt:
                        self._name_cache[(mkt, c)] = n  # 名称已知，回填后无需再查接口
                    self.stock_code_input.SetValue(c)
                wx.CallAfter(fill)
            else:
                tip = "未找到该名称的A股/港股" if not matches else \
                    "多个匹配: " + "、".join(f"{n}({c})" for c, n in matches)
                wx.CallAfter(self._set_stock_name_label, tip)
        threading.Thread(target=worker, daemon=True).start()

    def _update_name_label_async(self, code):
        """代码输入防抖后，后台查询股票名称并显示（缓存命中则立即显示）"""
        code = (code or "").strip()
        market = detect_market(code)
        valid = bool(market) and (
            (market == "A股" and len(code) == 6 and code.isdigit())
            or (market == "港股" and len(code) == 5 and code.isdigit())
            or (market == "美股" and 2 <= len(code) <= 6 and any(c.isalpha() for c in code)))
        if not valid:
            if code and any(ord(c) > 127 for c in code):
                self._name_search_async(code)  # 输入的是股票名称
            else:
                wx.CallAfter(self._set_stock_name_label, "")
            return

        cached = self._name_cache.get((market, code))
        if cached:
            wx.CallAfter(self._set_stock_name_label, cached)
            return

        # 合并连续键入：token 递增，只有最后一次输入的结果才上屏
        self._name_fetch_token = getattr(self, '_name_fetch_token', 0) + 1
        token = self._name_fetch_token

        def worker():
            time.sleep(0.3)
            if token != self._name_fetch_token:
                return
            name = self._get_stock_name_cached(code, market)
            if token == self._name_fetch_token:
                wx.CallAfter(self._set_stock_name_label, name)
        threading.Thread(target=worker, daemon=True).start()

    def _set_stock_name_label(self, name):
        """主线程更新代码旁的名称标签"""
        if hasattr(self, 'stock_name_label'):
            self.stock_name_label.SetLabel(name or "")
            parent = self.stock_name_label.GetParent()
            if parent is not None:
                parent.Layout()
    
    def _set_load_status(self, text, error=False, busy=False, ok=False):
        """更新底部状态条（线程安全：经事件队列回到主线程执行）"""
        wx.CallAfter(self._do_set_load_status, text, error, busy, ok)

    def _do_set_load_status(self, text, error=False, busy=False, ok=False):
        """更新底部状态条：文字 + 状态圆点颜色（灰=空闲/蓝=忙/绿=完成/红=错误）"""
        if hasattr(self, 'load_status') and hasattr(self, 'status_dot'):
            self.load_status.SetLabel(wxs(text))
            if error:
                dot, fg = COLORS.DANGER, COLORS.DANGER
            elif busy:
                dot, fg = COLORS.PRIMARY, COLORS.TEXT_SUB
            elif ok:
                dot, fg = COLORS.SUCCESS, COLORS.TEXT_SUB
            else:
                dot, fg = COLORS.IDLE, COLORS.TEXT_SUB
            self.status_dot.SetBackgroundColour(dot)
            self.load_status.SetForegroundColour(fg)
            self.load_status.Wrap(self.M1_width - 60)
            self.load_status.GetParent().Layout()
            self.Layout()

    def _start_busy(self, status_text):
        """进入忙状态：禁用操作按钮防止并发任务"""
        self._busy = True
        for b in (self.load_data_but, self.start_back_but,
                  self.trade_log_but, self.report_but,
                  getattr(self, 'optimize_but', None),
                  getattr(self, 'pool_run_but', None),
                  getattr(self, 'score_run_but', None),
                  getattr(self, 'pos_refresh_but', None),
                  getattr(self, 'sig_run_but', None)):
            if b is not None:
                b.Disable()
        self._set_load_status(status_text, busy=True)

    def _finish_busy(self):
        """任务结束（成功或失败）后恢复按钮"""
        self._busy = False
        for b in (self.load_data_but, self.start_back_but,
                  self.trade_log_but, self.report_but,
                  getattr(self, 'optimize_but', None),
                  getattr(self, 'pool_run_but', None),
                  getattr(self, 'score_run_but', None),
                  getattr(self, 'pos_refresh_but', None),
                  getattr(self, 'sig_run_but', None)):
            if b is not None:
                b.Enable()

    def on_open_report(self, event):
        """用系统默认浏览器打开最近一次生成的完整报告"""
        path = self.last_report_path
        if not path or not os.path.exists(path):
            wx.MessageBox("还没有生成的报告，请先运行一次回测", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        webbrowser.open(f"file:///{path}")

    def _on_task_error(self, msg):
        """后台任务失败回调：恢复按钮 + 状态行报错 + 弹窗 + 还原欢迎页"""
        self._finish_busy()
        self._set_load_status(msg, error=True)
        self.chart_panel.show_welcome()
        wx.MessageBox(msg, "错误", wx.OK | wx.ICON_ERROR)
    
    def _get_stock_name_cached(self, code, market):
        """带缓存的股票名称获取，避免重复请求"""
        key = (market, code)
        if key in self._name_cache:
            return self._name_cache[key]
        try:
            name = get_stock_name(code, market)
        except Exception as e:
            logger.warning(f"获取股票名称失败: {e}")
            name = code
        self._name_cache[key] = name
        return name
    
    def _ensure_stock_data(self, code, start_fmt, end_fmt, market, source, adjust="前复权", period="日线"):
        """带缓存的行情获取：同一(数据源,市场,代码,区间,复权,周期)只下载一次，返回 (df, name) 或 None"""
        key = (source, market, code, start_fmt, end_fmt, adjust, period)
        with self._cache_lock:
            if key in self._data_cache:
                logger.info(f"命中行情缓存: {key}")
                return self._data_cache[key]

        self._set_load_status(f"正在获取 {market} {period} 数据，请稍候…")
        # 行情接口偶发瞬断（RemoteDisconnected 等），自动重试 3 次
        df = pd.DataFrame()
        last_err = None
        for attempt in range(1, 4):
            try:
                df = self._get_stock_data(code, start_fmt, end_fmt, market, source, adjust, period)
            except Exception as e:
                last_err = e
                logger.warning(f"行情获取第{attempt}次失败: {e}")
                df = pd.DataFrame()
            if df is not None and not df.empty:
                break
            if attempt < 3:
                self._set_load_status(f"接口无响应，{attempt}/3 次重试…", busy=True)
                time.sleep(2 * attempt)
        if df is None or df.empty:
            msg = f"{market} {period} 数据获取失败或为空"
            if last_err is not None:
                msg += f"（已重试3次: {str(last_err)[:60]}）"
            self._set_load_status(msg, error=True)
            return None

        name = self._get_stock_name_cached(code, market)
        with self._cache_lock:
            self._data_cache[key] = (df, name)
        wx.CallAfter(self._set_stock_name_label, name)
        self._set_load_status(f"已加载 {len(df)} 条 · {name}（{market}·{period}）", ok=True)
        return df, name
    
    def _toggle_hybrid_ui(self, show):
        """显示/隐藏混合策略配置区域"""
        for widget_ref in self.hybrid_widget_refs:
            # Sizer.Show(item, show) 与 Widget.Show(show) 签名不同：
            # 对 sizer 调用单参会把 show 当索引 → 空 sizer 时越界崩溃
            if isinstance(widget_ref, (wx.Sizer, wx.StaticBoxSizer)):
                continue
            if hasattr(widget_ref, 'Show'):
                widget_ref.Show(show)
        if hasattr(self, 'hybrid_sizer'):
            self.hybrid_sizer.ShowItems(show)
        # 全链刷新布局
        self._layout_full_chain()

    def _on_hybrid_checkbox_changed(self, event):
        """勾选/取消子策略时，启用/禁用对应的参数输入"""
        cb = event.GetEventObject()
        sname = cb.GetLabel()
        if sname in self.hybrid_param_sizers:
            param_sizer, param_widgets = self.hybrid_param_sizers[sname]
            enabled = cb.GetValue()
            for tc in param_widgets.values():
                tc.Enable(enabled)

    def _get_hybrid_config(self):
        """从GUI读取混合策略配置，返回 (sub_strategies, combine_mode)"""
        sub_strategies = []
        for sname, cb in self.hybrid_checkboxes.items():
            if cb.GetValue():
                params = {}
                if sname in self.hybrid_param_sizers:
                    _, param_widgets = self.hybrid_param_sizers[sname]
                    for pname, tc in param_widgets.items():
                        try:
                            val = tc.GetValue().strip()
                            if '.' in val:
                                params[pname] = float(val)
                            else:
                                params[pname] = int(val)
                        except ValueError:
                            params[pname] = STRATEGY_PARAMS[sname][pname]
                else:
                    params = dict(STRATEGY_PARAMS[sname])
                sub_strategies.append((sname, params))
        
        mode_text = self.hybrid_mode_choice.GetValue()
        if 'AND' in mode_text:
            mode = 'AND'
        elif 'OR' in mode_text:
            mode = 'OR'
        else:
            mode = 'MAJORITY'
        
        return sub_strategies, mode

    def _on_strategy_changed(self, event):
        strategy_name = self.strategy_choice.GetValue()
        self.backtest_opts["select_strategy"] = strategy_name
        
        # 当选择/取消「混合策略」时，显示/隐藏混合配置区域
        is_hybrid = (strategy_name == "混合策略")
        self._toggle_hybrid_ui(is_hybrid)
        
        descriptions = {
            "双均线策略": "双均线策略：5日线上穿10日线买入，下穿卖出",
            "RSI策略": "RSI策略：RSI<30超卖买入，RSI>70超买卖出",
            "MACD策略": "MACD策略：MACD上穿信号线买入，下穿卖出",
            "布林带策略": "布林带策略：价格跌破下轨买入，突破上轨卖出",
            "均线交叉策略": "均线交叉策略：短期均线上穿长期均线买入，下穿卖出",
            "大于EMA策略": "大于EMA策略：收盘价大于EMA均线买入，小于EMA均线卖出",
            "多指标策略": "多指标策略：结合RSI和MA指标的复合策略",
            "趋势突破(原LSTM占位)": "LSTM预测策略：基于均线+阈值的趋势跟踪（模拟LSTM）",
            "强化学习策略": "强化学习策略：基于均线+阈值的动量跟踪（模拟RL）",
            "K线策略": "K线策略：收盘价大于均线买入，小于均线卖出",
            "SSA策略": "SSA策略：基于均线+双向阈值的趋势跟踪（模拟SSA）",
            "MyStrategy": "自定义策略：基于backtrader的自定义交易策略",
            "混合策略": "混合策略：自由组合多个子策略，支持AND/OR/MAJORITY三种组合模式",
            "唐奇安通道长线": "唐奇安通道长线：突破55日高点买入、跌破20日低点卖出；freq=weekly 每周第一个交易日才看信号",
            "双均线长线": "双均线长线：50/200日金叉买入死叉卖出，适合中长期趋势持有",
            "低频动量长线": "低频动量长线：价在200日线上且120日动量为正则持有，跌破均线离场；freq=monthly 每月看一次信号",
            "海龟策略": "海龟策略(系统1)：突破20日高点买入，2×ATR 止损或跌破10日低点离场",
            "网格策略": "网格策略：以首日收盘为锚，每跌3%买一份、每涨3%卖一份，资金分5格，适合震荡市",
            "StochRSI策略": "StochRSI策略：RSI随机值<20超卖买入、>80超买卖出",
            "单因子-RSRS择时": "RSRS择时：高低点回归斜率的z分数量化支撑强度，z>0.7买入",
            "单因子-移动均线+KDJ": "移动均线+KDJ：价在MA20上且K>D买入，跌破均线或K<D卖出",
            "单因子-MACD和ADX指标": "MACD和ADX：MACD金叉且ADX>20(趋势确认)买入",
            "单因子-阿隆指标(趋势交易)": "阿隆指标：AroonUp>70买入，AroonDown>70卖出",
            "单因子-情绪指标ARBR": "ARBR情绪指标：BR<40超卖买入，BR>400或AR>250卖出",
            "单因子-市场低估值策略": "市场低估值(近似)：价格250日分位<=30%买入，>=70%卖出",
            "机器学习-麻雀优化算法SSA": "SSA麻雀算法在前70%样本搜索双均线最优参数后交易",
            "机器学习-支持向量机SVM": "SVM：前70%样本训练分类次日涨跌，之后样本外预测",
            "机器学习-MLP时序分类": "LSTM时序预测(无torch时降级为MLP神经网络)",
            "机器学习-LGBM模型价格预测": "LGBM价格预测(lightgbm未装时用同类直方图梯度提升)",
            "机器学习-随机森林模型价格预测": "随机森林分类次日涨跌，样本外预测",
            "机器学习-线性回归价格预测": "线性回归预测次日收益方向，样本外预测",
            "强化学习-RL模型价格预测": "Q-Learning强化学习：离散状态{动量×均线}训练持仓策略",
            "强化学习-Q-Leaning预测": "Q-Learning强化学习(与RL模型同源)",
            "传统策略-海龟策略": "海龟策略：20日突破+2×ATR止损+10日离场",
            "传统策略-网格策略": "网格策略：每跌3%买一份每涨3%卖一份",
            "多因子-ROC(20)动量信号周频Top1": "ROC(20)动量周频调仓（单标的时Top1即该股）",
        }
        
        # 为动态加载的策略提供默认描述
        default_desc = f"{strategy_name}：基于backtrader框架的策略"
        self.strategy_desc.SetLabel(descriptions.get(strategy_name, default_desc))
        
        # 更新独立策略参数区域
        self._update_standalone_params(strategy_name)

        # 长线策略族默认开启「多等一日成交」
        self._apply_friction_delay_default(strategy_name)

        logger.info(f"选择策略: {strategy_name}")

    def _on_friction_delay_toggled(self, event):
        """用户手动改过延迟成交开关后，就不再被策略族默认值覆盖。"""
        self._friction_delay_user_set = True

    def _apply_friction_delay_default(self, strategy_name):
        """长线策略族默认打开延迟成交（诚实性问题：信号当根成交做不到）。

        只在用户没有手动设置过该开关时生效，避免覆盖用户的显式选择。
        非长线策略不动——短周期策略的时点假设才是收益的主要来源，
        是否延迟交给用户自己判断。
        """
        if getattr(self, '_friction_delay_user_set', False):
            return
        cb = getattr(self, 'friction_delay_cb', None)
        if cb is None:
            return
        want = is_long_term_strategy(strategy_name, self.strategy_map)
        if cb.GetValue() != want:
            cb.SetValue(want)
    
    def _update_standalone_params(self, strategy_name):
        """根据选择的独立策略，动态显示/隐藏参数输入框"""
        # 清除旧的参数控件
        for widget in self.standalone_param_widgets.values():
            widget.Destroy()
        self.standalone_param_widgets.clear()
        self.standalone_sizer.Clear()
        
        if strategy_name == "混合策略" or not STRATEGY_PARAMS.get(strategy_name):
            self.standalone_box.Show(False)
            self._layout_full_chain()
            return
        
        # 显示参数区域
        self.standalone_box.Show(True)
        
        label_map = {
            'pfast': '快线', 'pslow': '慢线',
            'period': '周期', 'oversold': '超卖', 'overbought': '超买',
            'fast': '快线', 'slow': '慢线', 'signal': '信号',
            'nk': '周期', 'maperiod': '周期',
            'lookback': '回顾期', 'threshold': '阈值',
            'window': '窗口', 'momentum_period': '动量周期',
            'entry_threshold': '入场阈值',
            'freq': '调仓频率', 'cooldown': '冷却期',
            'entry_n': '突破窗口', 'exit_n': '离场窗口', 'fast': '快线', 'slow': '慢线',
            'lookback': '动量回看',
            'atr_n': 'ATR周期', 'atr_stop': 'ATR止损倍数',
            'step_pct': '每格涨幅%', 'n_grids': '资金格数',
            'rsi_n': 'RSI周期', 'stoch_n': '随机窗口',
            'oversold': '超卖', 'overbought': '超买',
            'zwin': 'z窗口', 'z_in': 'z入', 'z_out': 'z出',
            'br_low': 'BR低', 'br_high': 'BR高', 'ar_high': 'AR高',
            'low_pct': '低分位', 'high_pct': '高分位',
            'pop': '种群', 'ssa_iter': 'SSA迭代',
            'train_ratio': '训练比', 'eps': '探索率',
            'alpha': '学习率', 'gamma': '折扣',
        }
        
        # StaticBoxSizer 内的控件 parent 用 StaticBox 本身
        sb = self.standalone_box
        
        for pname, default in STRATEGY_PARAMS.get(strategy_name, {}).items():
            row_sizer = wx.BoxSizer(wx.HORIZONTAL)
            short_label = label_map.get(pname, pname)
            row_sizer.Add(wx.StaticText(sb, label=f"{short_label}:"), 
                          0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 5)
            tc = wx.TextCtrl(sb, value=str(default), size=(55, -1))
            self.standalone_param_widgets[pname] = tc
            row_sizer.Add(tc, 0, wx.LEFT, 2)
            self.standalone_sizer.Add(row_sizer, 0, wx.EXPAND | wx.ALL, 2)
        
        # 全链刷新布局
        self._layout_full_chain()
    
    def _layout_full_chain(self):
        """从子面板到顶层全链刷新布局"""
        # 三个页签均为 ScrolledWindow，折叠/展开后需重算虚拟高度
        for tab_name in ('tab_backtest', 'tab_factor', 'tab_installer', 'tab_pool', 'tab_positions'):
            tab = getattr(self, tab_name, None)
            if tab is not None:
                tab.Layout()
                if hasattr(tab, 'FitInside'):
                    tab.FitInside()
        if hasattr(self, 'left_book'):
            cur = self.left_book.GetCurrentPage()
            if cur is not None:
                cur.Layout()
        if hasattr(self, '_left_panel'):
            self._left_panel.Layout()
        if hasattr(self, '_right_panel'):
            self._right_panel.Layout()
        self.Layout()
        self.Refresh()
        self.SendSizeEvent()
    
    def show_welcome(self):
        self.chart_panel.show_welcome()
    
    def _get_stock_data(self, code, start_fmt, end_fmt, market, source, adjust, period="日线"):
        """根据指定数据源和复权方式获取股票数据（纯函数，可在后台线程调用）"""
        logger.info(f"使用数据源: {source}, 复权: {adjust}, 周期: {period}")
        if source == "BaoStock":
            return get_data_baostock(code, start_fmt, end_fmt, market, adjust, period)
        return get_data_akshare(code, start_fmt, end_fmt, market, adjust, period)

    def _resolve_code_input(self):
        """标的输入 → 代码。输入为A股名称时解析并回填输入框（唯一匹配才回填）。
        返回 (code, None)；无法解析时返回 (None, 错误提示)。
        注意：名称表首次在线拉取约2秒（后续磁盘缓存即时），此处会阻塞主线程一次。"""
        text = self.stock_code_input.GetValue().strip()
        if not text:
            return None, "请填写标的代码或名称"
        if any(ord(c) > 127 for c in text):  # 中文等非ASCII：按名称搜索A股/港股
            matches = _search_by_name(text)
            if len(matches) == 1:
                self.stock_code_input.SetValue(matches[0][0])
                return matches[0][0], None
            if not matches:
                return None, f"未找到名称“{text}”对应的A股/港股，请直接输入代码"
            return None, ("多个匹配：" + "、".join(f"{n}({c})" for c, n in matches)
                          + "，请输入完整名称或代码")
        return text, None

    def on_load_data(self, event):
        if self._busy:
            return
        code, err = self._resolve_code_input()
        if err:
            wx.MessageBox(err, "提示", wx.OK | wx.ICON_INFORMATION)
            return
        start_fmt = self._get_start_date_str()
        end_fmt = self._get_end_date_str()
        market = self.market_choice.GetValue()
        source = self.data_source_choice.GetValue()
        adjust = self.stock_authority_cbox.GetValue()
        period = self.stock_period_cbox.GetValue() or "日线"

        # 注意：BaoStock 原生支持 日/周/月/5·15·30·60 分钟，此处无需按周期拦截数据源

        self._start_busy(f"正在获取{period}行情…")
        threading.Thread(target=self._load_worker,
                         args=(code, start_fmt, end_fmt, market, source, adjust, period),
                         daemon=True).start()

    def _load_worker(self, code, start_fmt, end_fmt, market, source, adjust, period="日线"):
        """后台线程：获取行情（图表由主线程调 matplotlib 画）"""
        try:
            result = self._ensure_stock_data(code, start_fmt, end_fmt, market, source, adjust, period)
            if result is not None:
                df, stock_name = result
                wx.CallAfter(self._on_load_done, df, stock_name, code, len(df))
            else:
                wx.CallAfter(self._finish_busy)
        except Exception as e:
            logger.exception("加载数据异常")
            wx.CallAfter(self._on_task_error, f"加载失败: {str(e)}")

    def _on_load_done(self, df, stock_name, code, row_count):
        """主线程回调：matplotlib 画预览图并提示"""
        self._finish_busy()
        self.chart_panel.show_data_preview(df, stock_name, code)
        self._set_load_status(f"已加载 {row_count} 条 · {stock_name}（{code}）", ok=True)
        wx.MessageBox(f"数据加载成功，共 {row_count} 条记录", "提示", wx.OK | wx.ICON_INFORMATION)
    
    def _collect_ctx(self):
        """收集当前面板的回测上下文（代码/区间/成本/策略参数/摩擦/样本外）。
        返回 (ctx, None) 或 (None, 错误信息)。供 主回测/参数寻优/股票池 共用。"""
        code, err = self._resolve_code_input()
        start_fmt = self._get_start_date_str()
        end_fmt = self._get_end_date_str()
        market = self.market_choice.GetValue()
        source = self.data_source_choice.GetValue()
        adjust = self.stock_authority_cbox.GetValue()
        period = self.stock_period_cbox.GetValue() or "日线"

        if err:
            return None, err

        try:
            init_cash = float(self.init_cash.GetValue())
        except:
            init_cash = 10000.0

        try:
            commission = float(self.commission.GetValue())
        except:
            commission = 0.0005

        try:
            slippage = float(self.slippage_input.GetValue())
        except:
            slippage = 0.1

        try:
            stake = int(float(self.stake_input.GetValue()))
        except:
            stake = 100

        try:
            stamp_duty = float(self.tax_input.GetValue())
        except:
            stamp_duty = 0.001

        # 现实摩擦：滑点/手续费翻倍（仅在勾选时生效；OOS 训练比例同理）
        try:
            slip_factor = float(self.friction_slip_factor.GetValue())
        except:
            slip_factor = 2.0
        try:
            comm_factor = float(self.friction_comm_factor.GetValue())
        except:
            comm_factor = 2.0
        if self.friction_slip_dbl.GetValue():
            slippage *= slip_factor
        if self.friction_comm_dbl.GetValue():
            commission *= comm_factor
        friction_delay = self.friction_delay_cb.GetValue()
        friction_circuit = self.friction_circuit_cb.GetValue()
        oos_enabled = self.oos_enable_cb.GetValue()
        try:
            oos_train_ratio = float(self.oos_train_ratio.GetValue())
            if not (0 < oos_train_ratio < 1):
                oos_train_ratio = 0.7
        except:
            oos_train_ratio = 0.7

        strategy_name = self.strategy_choice.GetValue()
        logger.info(f"执行回测，策略: {strategy_name}, 周期: {period}")

        sub_strategies = combine_mode = None
        strategy_kwargs = {}
        if strategy_name == "混合策略":
            sub_strategies, combine_mode = self._get_hybrid_config()
        else:
            defaults = STRATEGY_PARAMS.get(strategy_name, {})
            for pname, tc in self.standalone_param_widgets.items():
                val = tc.GetValue().strip()
                if isinstance(defaults.get(pname), str):
                    strategy_kwargs[pname] = val or defaults.get(pname)
                    continue
                try:
                    strategy_kwargs[pname] = float(val) if '.' in val else int(val)
                except ValueError:
                    strategy_kwargs[pname] = defaults.get(pname)
            logger.info(f"独立策略参数: {strategy_kwargs}")

        benchmark_text = self.benchmark_choice.GetValue()
        if '(' in benchmark_text and ')' in benchmark_text:
            benchmark_code = benchmark_text.split('(')[1].split(')')[0]
        else:
            benchmark_code = benchmark_text

        ctx = dict(
            code=code, start=start_fmt, end=end_fmt, market=market,
            source=source, adjust=adjust, period=period,
            init_cash=init_cash, commission=commission, slippage=slippage,
            stake=stake, stamp_duty=stamp_duty,
            strategy_name=strategy_name,
            sub_strategies=sub_strategies, combine_mode=combine_mode,
            strategy_kwargs=strategy_kwargs,
            benchmark_text=benchmark_text, benchmark_code=benchmark_code,
            friction_delay=friction_delay, friction_circuit=friction_circuit,
            oos_enabled=oos_enabled, oos_train_ratio=oos_train_ratio,
        )

        return ctx, None

    def on_run_backtest(self, event):
        if self._busy:
            return
        ctx, err = self._collect_ctx()
        if err:
            wx.MessageBox(err, "提示", wx.OK | wx.ICON_INFORMATION)
            return
        period = ctx['period']

        self._start_busy(f"正在准备回测（{period}）…")
        self._show_loading()
        threading.Thread(target=self._backtest_worker, args=(ctx,), daemon=True).start()

    def _backtest_worker(self, ctx):
        """后台线程：取数、回测、生成报告（全程不做 wx 控件调用）"""
        try:
            result = self._ensure_stock_data(ctx['code'], ctx['start'], ctx['end'],
                                             ctx['market'], ctx['source'], ctx['adjust'],
                                             ctx.get('period', '日线'))
            if result is None:
                wx.CallAfter(self._finish_busy)
                return
            dataframe, stock_name = result

            wx.CallAfter(self._set_load_status, "正在运行回测…")

            # 样本外模式才会产出这两份；非 OOS 必须先置 None，
            # 否则下面无条件的 score_strategy(..., train_metrics=train_metrics) 会 NameError
            train_metrics = None
            test_metrics = None

            # 决定是单次还是 OOS 两次：OOS 把行情按时间切两段分别跑
            if ctx.get('oos_enabled'):
                ratio = ctx.get('oos_train_ratio', 0.7)
                split = max(1, int(len(dataframe) * ratio))
                split = min(split, len(dataframe) - 1)
                wx.CallAfter(self._set_load_status,
                             f"样本外测试：训练 {split} 根 + 测试 {len(dataframe)-split} 根")
                train_metrics = self._run_single_cerebro(dataframe.iloc[:split], ctx)
                test_metrics = self._run_single_cerebro(dataframe.iloc[split:], ctx)
                eq_train = train_metrics.get('_equity', [])
                eq_test = test_metrics.get('_equity', [])
                # 主显示用测试段，交易记录也以测试段为准（更贴近「未参与决策的样本」）
                metrics = dict(test_metrics)
                metrics['train_total_return_str'] = train_metrics['total_return_str']
                metrics['train_sharpe_str'] = train_metrics['sharpe_str']
                metrics['train_drawdown_str'] = train_metrics['drawdown_str']
                # test 段已包含主指标
                trades = test_metrics.get('_trades', [])
                # 测试段 trades 的日期对应原始 dataframe 的后段，图表仍用全段
                final_value = test_metrics['_final_value']
                total_return = test_metrics['_total_return']
                sharpe_str = test_metrics['sharpe_str']
                returns_str = test_metrics['returns_str']
                drawdown_str = test_metrics['drawdown_str']
            else:
                main = self._run_single_cerebro(dataframe, ctx)
                eq_main = main.get('_equity', [])
                metrics = dict(main)
                trades = main.get('_trades', [])
                final_value = main['_final_value']
                total_return = main['_total_return']
                sharpe_str = main['sharpe_str']
                returns_str = main['returns_str']
                drawdown_str = main['drawdown_str']

            logger.info(f"找到 {len(trades)} 笔交易记录")

            # ===== 成交时点敏感性：常规 vs 多等一日 =====
            # 在同一段数据上重跑一份「关闭延迟成交」的版本做对照。
            # 注意：backtrader 的市价单本身就在信号次根开盘成交，所以「关闭延迟」
            # 已经是可实现口径；这里衡量的是再多等一根的代价，即策略对时点的敏感度。
            ideal_vs_delay = None
            if ctx.get('friction_delay'):
                try:
                    ideal_ctx = dict(ctx, friction_delay=False, friction_circuit=False)
                    ideal_metrics = self._run_single_cerebro(dataframe, ideal_ctx)
                    ideal_vs_delay = {
                        'ideal_return': ideal_metrics['total_return'],
                        'real_return': total_return,
                        'ideal_sharpe': ideal_metrics['sharpe_str'],
                        'real_sharpe': sharpe_str,
                        'ideal_dd': ideal_metrics['drawdown_str'],
                        'real_dd': drawdown_str,
                    }
                except Exception as e:
                    logger.warning(f"理想成交对照失败: {e}")

            # ===== 获取基准指数数据（失败静默降级，不影响回测）=====
            benchmark_data = None
            benchmark_ok = False
            try:
                logger.info(f"获取基准数据: {ctx['benchmark_code']}")
                bm_df = get_index_data(ctx['benchmark_code'], ctx['start'], ctx['end'])
                if not bm_df.empty:
                    # 归一化基准到股票首日价格
                    first_stock_close = dataframe['close'].iloc[0]
                    first_bm_close = bm_df['close'].iloc[0]
                    if first_bm_close > 0:
                        values = (bm_df['close'] / first_bm_close * first_stock_close).tolist()
                        dates = bm_df.index.strftime('%Y-%m-%d').tolist()
                        benchmark_data = {'dates': dates, 'values': values,
                                          'name': ctx['benchmark_text']}
                        benchmark_ok = True
            except Exception as e:
                logger.warning(f"获取基准数据失败: {e}")

            # ===== 多维度图表数据：资金曲线 / 回撤 / 月度收益 / 交易得失 =====
            bench_eq_pts = []
            if benchmark_data:
                first_bm = benchmark_data['values'][0]
                if first_bm > 0:
                    factor = ctx['init_cash'] / first_bm
                    bench_eq_pts = [{'time': d, 'value': round(v * factor, 2)}
                                    for d, v in zip(benchmark_data['dates'], benchmark_data['values'])]
            if ctx.get('oos_enabled'):
                equity = eq_train + eq_test
                dd_pts = compute_drawdown(equity, reset_at={len(eq_train)})
            else:
                equity = eq_main
                dd_pts = compute_drawdown(equity)
            monthly = compute_monthly_returns(equity)
            equity_pts = [{'time': d, 'value': round(v, 2)} for d, v in equity]
            monthly_pts = [dict(p, color='#26a69a' if p['value'] >= 0 else '#ef5350') for p in monthly]
            trade_pnl, trade_stats = compute_trade_pnl(trades)
            # 回撤修复时间：深度之外的另一半「实盘体感」
            max_recovery, still_underwater = compute_drawdown_recovery(equity)
            metrics['max_recovery_days'] = max_recovery
            metrics['still_underwater'] = still_underwater
            # ===== 策略体检评分 =====
            score, dims, flags = score_strategy(
                metrics, oos_enabled=ctx.get('oos_enabled', False),
                trade_stats=trade_stats,
                train_metrics=train_metrics, ideal_vs_delay=ideal_vs_delay)
            score_html = render_score_card_html(score, dims, flags)
            metrics['score'] = score
            extras = {'equity': equity_pts, 'drawdown': dd_pts, 'monthly': monthly_pts,
                      'benchmark_equity': bench_eq_pts,
                      'trade_pnl': trade_pnl, 'trade_stats': trade_stats,
                      'ideal_vs_delay': ideal_vs_delay}

            # 完整报告落盘（点击「打开完整报告」在浏览器查看）
            result_html = self._generate_result_html(
                stock_name, ctx['code'], ctx['start'], ctx['end'],
                ctx['init_cash'], final_value, total_return,
                sharpe_str, returns_str, drawdown_str,
                ctx['strategy_name'], len(trades),
                train_metrics=train_metrics if ctx.get('oos_enabled') else None,
                test_metrics=test_metrics if ctx.get('oos_enabled') else None,
                ideal_vs_delay=ideal_vs_delay,
                score_html=score_html,
            )
            result_path = os.path.join(DATA_DIR_BKT_RESULT, "result.html")
            with open(result_path, 'w', encoding='utf-8') as f:
                f.write(result_html)

            payload = dict(dataframe=dataframe, stock_name=stock_name, code=ctx['code'],
                           trades=trades, strategy_name=ctx['strategy_name'],
                           benchmark_data=benchmark_data, metrics=metrics,
                           extras=extras, report_path=result_path,
                           benchmark_ok=benchmark_ok)
            wx.CallAfter(self._on_backtest_done, payload)

        except Exception as e:
            logger.exception("回测异常")
            wx.CallAfter(self._on_task_error, f"回测出错: {str(e)}")

    def _run_single_cerebro(self, dataframe, ctx):
        """单次回测：在传入的 DataFrame 上跑 cerebro，返回 metrics 字典。

        真正的实现在模块级函数 run_cerebro_metrics()——参数寻优要用多进程跑同一段逻辑，
        而 Windows 的 spawn 启动方式无法 pickle 绑定方法，必须是模块级可导入的函数。
        """
        return run_cerebro_metrics(dataframe, ctx, self.strategy_map)


    def _on_backtest_done(self, p):
        """主线程回调：恢复按钮、渲染图表并提示。失败时走 _on_task_error，不让按钮卡死。"""
        self._finish_busy()
        self.last_report_path = p['report_path']
        try:
            self.chart_panel.set_data(
                p['dataframe'], p['stock_name'], p['code'], p['trades'],
                p['strategy_name'], p['benchmark_data'], p['metrics'],
                p.get('period', '日线'), extras=p.get('extras'),
            )
            self.chart_panel.create_chart()
        except Exception as e:
            logger.exception("图表渲染失败")
            self._set_load_status(f"图表渲染失败: {e}", error=True)
            self.chart_panel.show_welcome()
            return
        self._set_load_status(
            f"回测完成 · {p['strategy_name']} · {len(p['trades'])} 笔交易 · 可点击「打开完整报告」查看详情",
            ok=True)
        wx.MessageBox(f"回测完成！\n使用策略: {p['strategy_name']}\n交易次数: {len(p['trades'])}",
                      "提示", wx.OK | wx.ICON_INFORMATION)

    
    def on_trade_log(self, event):
        wx.MessageBox("交易日志功能开发中...", "提示", wx.OK | wx.ICON_INFORMATION)
    
    def _show_loading(self):
        self.chart_panel.show_loading("正在回测，请稍候…")
    
    def _generate_result_html(self, stock_name, code, start_fmt, end_fmt,
                             init_cash, final_value, total_return,
                             sharpe_str, returns_str, drawdown_str,
                             strategy_name="双均线策略", trade_count=0,
                             train_metrics=None, test_metrics=None,
                             ideal_vs_delay=None, score_html=""):
        oos_section = ""
        reality_section = ""
        if ideal_vs_delay:
            gap = ideal_vs_delay['ideal_return'] - ideal_vs_delay['real_return']
            gap_pct_str = f"{gap:+.2f}%"
            warn = "⚠ 差距大 → 策略对成交时点敏感，实盘大概率达不到左侧收益" if abs(gap) > 5 else "✅ 差距小 → 策略对成交时点不敏感，实盘较可能复现"
            reality_section = f"""
                <h2 style="margin-top:30px">⏱ 成交时点敏感性（常规 vs 多等一日）</h2>
                <div class="note">
                    <strong>差距 = {gap_pct_str}</strong> · {warn}<br>
                    • 左侧「常规」= 信号次根<strong>开盘价</strong>成交。backtrader 的市价单默认就是这个，
                      收盘后才看到信号的人次日开盘下单，属于<strong>可实现</strong>的口径<br>
                    • 右侧「多等一日」= 信号再压一根，第三根开盘才成交，用来模拟滑点变大 / 委托延迟 / 开盘跳空<br>
                    • 两侧的差值衡量的是：策略收益有多少来自「恰好在好时点成交」
                </div>
                <div class="metrics">
                    <div class="metric-card" style="border-top-color:#787b86"><div class="metric-label">常规成交收益</div><div class="metric-value {'positive' if ideal_vs_delay['ideal_return']>0 else 'negative'}">{ideal_vs_delay['ideal_return']:+.2f}%</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">多等一日收益</div><div class="metric-value {'positive' if ideal_vs_delay['real_return']>0 else 'negative'}">{ideal_vs_delay['real_return']:+.2f}%</div></div>
                    <div class="metric-card" style="border-top-color:#787b86"><div class="metric-label">常规夏普</div><div class="metric-value">{ideal_vs_delay['ideal_sharpe']}</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">多等一日夏普</div><div class="metric-value">{ideal_vs_delay['real_sharpe']}</div></div>
                    <div class="metric-card" style="border-top-color:#787b86"><div class="metric-label">常规回撤</div><div class="metric-value negative">{ideal_vs_delay['ideal_dd']}%</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">多等一日回撤</div><div class="metric-value negative">{ideal_vs_delay['real_dd']}%</div></div>
                </div>"""
        if train_metrics and test_metrics:
            tr_ret = train_metrics.get('total_return_str', '--')
            tr_sharpe = train_metrics.get('sharpe_str', '--')
            tr_dd = train_metrics.get('drawdown_str', '--')
            tr_fv = train_metrics.get('final_value_str', '--')
            te_ret = test_metrics.get('total_return_str', '--')
            te_sharpe = test_metrics.get('sharpe_str', '--')
            te_dd = test_metrics.get('drawdown_str', '--')
            te_fv = test_metrics.get('final_value_str', '--')
            oos_section = f"""
                <h2 style="margin-top:30px">🧪 样本外测试对比</h2>
                <div class="note">
                    <strong>如何读这张表</strong><br>
                    • 训练段（In-Sample）：策略见过的数据，参数在这里被「调教」<br>
                    • 测试段（Out-of-Sample）：策略从未见过的数据，更接近实盘<br>
                    • 如果测试段收益大幅低于训练段，说明策略<strong>过拟合</strong>，要警惕
                </div>
                <div class="metrics">
                    <div class="metric-card" style="border-top-color:#2962ff"><div class="metric-label">训练段收益</div><div class="metric-value {'positive' if tr_ret.startswith('+') else 'negative'}">{tr_ret}</div></div>
                    <div class="metric-card" style="border-top-color:#2962ff"><div class="metric-label">训练段夏普</div><div class="metric-value">{tr_sharpe}</div></div>
                    <div class="metric-card" style="border-top-color:#2962ff"><div class="metric-label">训练段回撤</div><div class="metric-value negative">{tr_dd}</div></div>
                    <div class="metric-card" style="border-top-color:#2962ff"><div class="metric-label">训练段终值</div><div class="metric-value">{tr_fv}</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">测试段收益</div><div class="metric-value {'positive' if te_ret.startswith('+') else 'negative'}">{te_ret}</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">测试段夏普</div><div class="metric-value">{te_sharpe}</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">测试段回撤</div><div class="metric-value negative">{te_dd}</div></div>
                    <div class="metric-card" style="border-top-color:#ff9800"><div class="metric-label">测试段终值</div><div class="metric-value">{te_fv}</div></div>
                </div>"""
        return f"""<html><head><style>
            body{{font-family:"Microsoft YaHei",Arial,sans-serif;margin:0;padding:20px;background:#f5f5f5}}
            .container{{max-width:1200px;margin:0 auto;background:white;padding:30px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,0.1)}}
            h2{{color:#2c3e50;border-bottom:3px solid #667eea;padding-bottom:10px}}
            .stock-info{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:30px;border-radius:10px;margin:20px 0;text-align:center}}
            .stock-info h1{{margin:0;font-size:32px;font-weight:300}}
            .strategy-badge{{display:inline-block;background:rgba(255,255,255,0.3);padding:5px 15px;border-radius:20px;font-size:14px;margin-top:10px}}
            .metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px;margin:30px 0}}
            .metric-card{{background:white;padding:25px;border-radius:10px;box-shadow:0 4px 6px rgba(0,0,0,0.1);text-align:center;transition:transform 0.2s;border-top:4px solid #667eea}}
            .metric-card:hover{{transform:translateY(-5px);box-shadow:0 6px 12px rgba(0,0,0,0.15)}}
            .metric-value{{font-size:28px;font-weight:bold;color:#2c3e50;margin:10px 0}}
            .metric-label{{color:#7f8c8d;font-size:14px;text-transform:uppercase;letter-spacing:1px}}
            .positive{{color:#27ae60}}
            .negative{{color:#e74c3c}}
            .note{{background:#e8f4f8;padding:20px;border-radius:8px;margin:20px 0;border-left:4px solid #3498db}}
            .note strong{{color:#2980b9}}
            .trade-count{{display:inline-block;background:#667eea;color:white;padding:5px 15px;border-radius:20px;font-size:14px;margin-left:10px}}
        </style></head><body>
            <div class="container">
                <h2>📊 回测结果报告</h2>
                <div class="stock-info">
                    <h1>{stock_name}</h1>
                    <p style="font-size:18px;margin:10px 0 0 0">{code}</p>
                    <p style="opacity:0.8;margin:5px 0 0 0">{start_fmt} 至 {end_fmt}</p>
                    <div class="strategy-badge">策略: {strategy_name}</div>
                    <div class="trade-count">交易次数: {trade_count}</div>
                </div>
                <div class="note">
                    <strong>✅ 回测完成</strong><br>
                    • 下方为策略图表，含K线主图、成交量副图、技术指标副图<br>
                    • 绿色↑=买入，红色↓=卖出，类似Backtrader专业图表
                </div>
                <div class="metrics">
                    <div class="metric-card"><div class="metric-label">初始资金</div><div class="metric-value">¥{init_cash:,.2f}</div></div>
                    <div class="metric-card"><div class="metric-label">最终资金</div><div class="metric-value">¥{final_value:,.2f}</div></div>
                    <div class="metric-card"><div class="metric-label">总收益率</div><div class="metric-value {'positive' if total_return > 0 else 'negative'}">{total_return:+.2f}%</div></div>
                    <div class="metric-card"><div class="metric-label">夏普比率</div><div class="metric-value">{sharpe_str}</div></div>
                    <div class="metric-card"><div class="metric-label">年化收益率</div><div class="metric-value">{returns_str}%</div></div>
                    <div class="metric-card"><div class="metric-label">最大回撤</div><div class="metric-value negative">{drawdown_str}%</div></div>
                </div>
                {oos_section}
                {score_html}
                {reality_section}
                <p style="text-align:center;color:#95a5a6;margin-top:30px;font-size:12px">生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>
        </body></html>"""


def run_cerebro_metrics(dataframe, ctx, strategy_map):
    """跑一次回测并返回指标字典（模块级，供主线程与寻优子进程共用）。"""
    strategy_class = strategy_map.get(ctx['strategy_name'], MA_Cross_Strategy)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=dataframe))

    friction_kw = dict(
        friction_delay=ctx.get('friction_delay', False),
        friction_circuit=ctx.get('friction_circuit', False),
        market=ctx.get('market', 'A股'),   # 决定熔断是否生效（港股无涨跌停）
    )

    # 仅当策略声明了对应参数时才注入 stake/摩擦参数，
    # 避免用户拖拽安装的第三方策略（未继承 BaseStrategy）因未知参数报错
    declared = set()
    try:
        declared = set(strategy_class.params._getkeys())
    except Exception:
        pass

    def _keep(kw_map):
        return {k: v for k, v in kw_map.items() if k in declared}

    base_kw = _keep({'stake': ctx['stake'], **friction_kw})
    if ctx['sub_strategies'] is not None:
        cerebro.addstrategy(strategy_class,
                            sub_strategies=ctx['sub_strategies'],
                            combine_mode=ctx['combine_mode'],
                            **base_kw)
        logger.info(f"混合策略配置: {ctx['sub_strategies']}, 模式: {ctx['combine_mode']}")
    else:
        cerebro.addstrategy(strategy_class,
                            **base_kw, **ctx['strategy_kwargs'])

    cerebro.broker.setcash(ctx['init_cash'])
    cerebro.broker.addcommissioninfo(AStockCommission(
        commission=ctx['commission'], stamp_duty=ctx['stamp_duty'],
        market=ctx.get('market', 'A股')))
    cerebro.broker.set_slippage_perc(perc=ctx['slippage'] / 100.0)

    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.AnnualReturn, _name='annual')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                       timeframe=bt.TimeFrame.Days,
                       compression=1,
                       riskfreerate=0.02)  # 假设无风险利率为2%
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')

    results = cerebro.run()
    strat = results[0]

    trades = list(getattr(strat, 'trades', []))
    final_value = cerebro.broker.getvalue()
    total_return = (final_value / ctx['init_cash'] - 1) * 100
    returns_a = strat.analyzers.returns.get_analysis()
    sharpe_a = strat.analyzers.sharpe.get_analysis()
    drawdown_a = strat.analyzers.drawdown.get_analysis()

    sharpe_raw = sharpe_a.get('sharperatio')
    sharpe_str = "N/A" if sharpe_raw is None else f"{sharpe_raw:.2f}"
    returns_raw = returns_a.get('rnorm100')
    returns_str = "N/A" if returns_raw is None else f"{returns_raw:.2f}"
    drawdown_raw = drawdown_a.get('max', {}).get('drawdown')
    drawdown_str = "N/A" if drawdown_raw is None else f"{drawdown_raw:.2f}"

    return {
        "init_cash_str": f"¥{ctx['init_cash']:,.2f}",
        "final_value_str": f"¥{final_value:,.2f}",
        "total_return": total_return,
        "total_return_str": f"{total_return:+.2f}%",
        "sharpe_str": sharpe_str,
        "returns_str": f"{returns_str}%" if returns_str != "N/A" else "N/A",
        "drawdown_str": f"{drawdown_str}%" if drawdown_str != "N/A" else "N/A",
        "trade_count": len(trades),
        "_trades": trades,
        "_final_value": final_value,
        "_total_return": total_return,
        "_sharpe": sharpe_raw,
        "_drawdown": drawdown_raw,
        "_equity": list(getattr(strat, 'equity_curve', [])),
    }
