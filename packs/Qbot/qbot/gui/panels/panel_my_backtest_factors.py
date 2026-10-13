"""
聚宽因子库（https://www.joinquant.com/view/factorlib/list）的本地实现 + GUI 集成。

包含：
  1) JOINQUANT_FACTORS：60+ 因子，按 18 大类组织（技术面/动量/波动率/成交量/换手率/
     资金流/估值/盈利/成长/质量/规模/风格/风险/分红/预期/行业/北向/龙虎榜）
  2) FactorCalculator：基于 OHLCV 的 37 个 calc_* 方法，纯 pandas/numpy
  3) FactorPanelMixin：在面板里挂一个 wx 复选框驱动的「因子分析」子模块
  4) build_factor_html：生成因子报告 HTML

被 panel_my_backtest.py 通过 from ... import * 复用。
"""
import os
import threading
import webbrowser

import numpy as np
import pandas as pd
import wx

from qbot.gui.panels.ui_theme import wxs

try:
    from qbot.common.logging.logger import LOGGER as logger
except Exception:  # 容错
    import logging
    logger = logging.getLogger("qbot.factors")


# 回测报告目录：与主面板保持一致（qbot/gui/config 的 DATA_DIR_BKT_RESULT）
try:
    from qbot.gui.config import DATA_DIR_BKT_RESULT as _BKT_DIR
except Exception:
    _BKT_DIR = os.path.join(os.getcwd(), "qbot_backtest")


# ==================== 因子字典 ====================
JOINQUANT_FACTORS = {
    "技术面": {
        "MA":              {"name": "MA(移动均线)",         "category": "技术面", "tech": True,
                           "desc": "N日简单移动均线", "formula": "mean(close, N)",
                           "params": {"N": 5}},
        "EMA":             {"name": "EMA(指数移动均线)",     "category": "技术面", "tech": True,
                           "desc": "N日指数移动均线", "formula": "EMA(close, N)",
                           "params": {"N": 12}},
        "MACD":            {"name": "MACD",                 "category": "技术面", "tech": True,
                           "desc": "异同移动平均线，DIF-DEA", "formula": "EMA(fast)-EMA(slow); signal=EMA(DIF,signal)",
                           "params": {"fast": 12, "slow": 26, "signal": 9}},
        "RSI":             {"name": "RSI",                  "category": "技术面", "tech": True,
                           "desc": "相对强弱指标", "formula": "100 - 100/(1+mean(gain,N)/mean(loss,N))",
                           "params": {"N": 14}},
        "KDJ":             {"name": "KDJ",                  "category": "技术面", "tech": True,
                           "desc": "随机指标", "formula": "K = SMA((C-LowN)/(HighN-LowN)*100, M1); D=SMA(K, M2); J=3K-2D",
                           "params": {"N": 9, "M1": 3, "M2": 3}},
        "BOLL":            {"name": "布林带",                "category": "技术面", "tech": True,
                           "desc": "布林带上中下轨", "formula": "MID=MA(N); UP=MID+k*STD; DN=MID-k*STD",
                           "params": {"N": 20, "k": 2}},
        "BIAS":            {"name": "BIAS(乖离率)",         "category": "技术面", "tech": True,
                           "desc": "收盘价与N日均线偏离度", "formula": "(C - MA(N)) / MA(N) * 100",
                           "params": {"N": 6}},
        "CCI":             {"name": "CCI(顺势指标)",         "category": "技术面", "tech": True,
                           "desc": "典型价格相对均值的偏离", "formula": "(TP-MA(TP,N))/(0.015*MD)",
                           "params": {"N": 14}},
        "ATR":             {"name": "ATR(真实波幅)",         "category": "技术面", "tech": True,
                           "desc": "N日真实波幅均值", "formula": "MAX(H-L,|H-Cp|,|L-Cp|) 的 N 日均值",
                           "params": {"N": 14}, "dir": -1},
        "BBI":             {"name": "BBI(多空指标)",         "category": "技术面", "tech": True,
                           "desc": "多条均线平均", "formula": "(MA3+MA6+MA12+MA24)/4",
                           "params": {}},
    },
    "动量/趋势": {
        "MOM":             {"name": "MOM(动量)",            "category": "动量", "tech": True,
                           "desc": "N日价格变化", "formula": "C - C[-N]",
                           "params": {"N": 10}},
        "ROC":             {"name": "ROC(变动率)",          "category": "动量", "tech": True,
                           "desc": "N日价格变动率", "formula": "(C-C[-N])/C[-N]*100",
                           "params": {"N": 12}},
        "MOMENTUM":        {"name": "Momentum(加速度)",     "category": "动量", "tech": True,
                           "desc": "ROC 的 N 日差分", "formula": "ROC(N) - ROC(N)[-M]",
                           "params": {"N": 10, "M": 5}},
        "ADX":             {"name": "ADX(平均趋向指数)",    "category": "动量", "tech": True,
                           "desc": "趋势强度指标", "formula": "基于 +DM/-DM 与 TR 的 N 日均值",
                           "params": {"N": 14}},
        "AROON":           {"name": "Aroon(阿隆指标)",      "category": "动量", "tech": True,
                           "desc": "AroonUp/AroonDn", "formula": "(N-距N日内最高)/N, (N-距N日内最低)/N",
                           "params": {"N": 25}},
    },
    "波动率": {
        "HIST_VOL":        {"name": "历史波动率",            "category": "波动率", "tech": True,
                           "desc": "N日收益率标准差年化", "formula": "std(ret, N) * sqrt(252)",
                           "params": {"N": 20}, "dir": -1},
        "PARKINSON":       {"name": "Parkinson 波动率",     "category": "波动率", "tech": True,
                           "desc": "基于高低价的对数范围估计", "formula": "sqrt(sum(ln(H/L)^2 / (4*N*ln2)))",
                           "params": {"N": 20}, "dir": -1},
        "GK_VOL":          {"name": "Garman-Klass 波动率",  "category": "波动率", "tech": True,
                           "desc": "结合 OHLC 的高效估计", "formula": "0.5*ln(H/L)^2 - (2*ln2-1)*ln(C/O)^2",
                           "params": {"N": 20}, "dir": -1},
        "RS_VOL":          {"name": "Rogers-Satchell 波动率","category": "波动率", "tech": True,
                           "desc": "不依赖零漂移的估计", "formula": "ln(H/C)*ln(H/O)+ln(L/C)*ln(L/O)",
                           "params": {}, "dir": -1},
        "YZ_VOL":          {"name": "Yang-Zhang 波动率",    "category": "波动率", "tech": True,
                           "desc": "结合隔夜跳空与日内波动", "formula": "组合 overnight/OC/HL 波动",
                           "params": {"N": 20}, "dir": -1},
        "BOLL_WIDTH":      {"name": "布林带宽度",            "category": "波动率", "tech": True,
                           "desc": "(UP-DN)/MID", "formula": "(BOLL_UP - BOLL_DN) / BOLL_MID",
                           "params": {"N": 20, "k": 2}, "dir": -1},
    },
    "成交量": {
        "VOL_MA":          {"name": "VOL_MA",               "category": "成交量", "tech": True,
                           "desc": "成交量均线", "formula": "mean(volume, N)",
                           "params": {"N": 5}},
        "VOL_RATIO":       {"name": "量比",                  "category": "成交量", "tech": True,
                           "desc": "现量与过去 N 日均量之比", "formula": "volume / mean(volume[-(N+1):-1], N)",
                           "params": {"N": 5}},
        "OBV":             {"name": "OBV(能量潮)",           "category": "成交量", "tech": True,
                           "desc": "累积量价", "formula": "cum(sign(close-prev_close) * volume)",
                           "params": {}},
        "VWAP":            {"name": "VWAP",                 "category": "成交量", "tech": True,
                           "desc": "成交量加权均价", "formula": "sum(TP*V,N)/sum(V,N)",
                           "params": {"N": 20}},
        "MFI":             {"name": "MFI(资金流量指标)",      "category": "成交量", "tech": True,
                           "desc": "带成交量的 RSI", "formula": "100 - 100/(1+sum(PosMF,N)/sum(NegMF,N))",
                           "params": {"N": 14}},
        "VSTDDEV":         {"name": "成交量标准差",          "category": "成交量", "tech": True,
                           "desc": "成交量波动幅度", "formula": "std(volume, N)",
                           "params": {"N": 20}},
        "VR":              {"name": "VR(成交量比率)",       "category": "成交量", "tech": True,
                           "desc": "上涨日量/下跌日量", "formula": "sum(vol_up,N)/(sum(vol_down,N)+eps)",
                           "params": {"N": 26}},
    },
    "换手率": {
        "TURN":            {"name": "换手率",                "category": "换手率", "tech": True,
                           "desc": "成交量/流通股本(需流通股本)", "formula": "volume / free_float_shares",
                           "params": {}, "need_data": "free_float_shares"},
        "TURN_MA":         {"name": "换手率均线",            "category": "换手率", "tech": True,
                           "desc": "换手率 N 日均值", "formula": "mean(turn, N)",
                           "params": {"N": 5}, "need_data": "free_float_shares"},
        "ILLIQUIDITY":     {"name": "非流动性指标(Amihud)", "category": "换手率", "tech": True,
                           "desc": "|ret|/volume 的 N 日均值", "formula": "mean(|ret|/volume, N)",
                           "params": {"N": 20}},
    },
    "资金流": {
        "NET_INFLOW":      {"name": "主力净流入",            "category": "资金流", "tech": True,
                           "desc": "大单净流入(近似)", "formula": "sum((C-O)/O * V if |C-O|/O>thr)",
                           "params": {"thr": 0.02}, "need_data": "tick"},
        "SMALL_ORDER":     {"name": "小单净流入",            "category": "资金流", "tech": True,
                           "desc": "小单净流入(近似)", "formula": "需逐笔成交",
                           "params": {}, "need_data": "tick"},
        "MAIN_BUY_RATIO":  {"name": "主力买入占比",          "category": "资金流", "tech": True,
                           "desc": "主力买入金额占比(近似)", "formula": "需大单/特大单数据",
                           "params": {}, "need_data": "tick"},
        "NORTH_BOUND":     {"name": "北向资金净流入",         "category": "资金流", "tech": False,
                           "desc": "陆股通净买入额", "formula": "north_bound_holdings_change",
                           "params": {}, "need_data": "north_bound"},
    },
    "基本面-估值": {
        "PE_TTM":          {"name": "PE-TTM",               "category": "估值", "tech": False,
                           "desc": "市盈率(滚动)", "formula": "close * total_shares / net_profit_ttm",
                           "params": {}, "need_data": "财务"},
        "PB":              {"name": "PB(市净率)",           "category": "估值", "tech": False,
                           "desc": "市净率", "formula": "close * total_shares / equity",
                           "params": {}, "need_data": "财务"},
        "PS_TTM":          {"name": "PS-TTM",               "category": "估值", "tech": False,
                           "desc": "市销率(滚动)", "formula": "mcap / revenue_ttm",
                           "params": {}, "need_data": "财务"},
        "PCF":             {"name": "PCF(市现率)",          "category": "估值", "tech": False,
                           "desc": "市现率", "formula": "mcap / operating_cashflow_ttm",
                           "params": {}, "need_data": "财务"},
        "PEG":             {"name": "PEG",                  "category": "估值", "tech": False,
                           "desc": "PE / 净利润增速", "formula": "PE / (profit_growth * 100)",
                           "params": {}, "need_data": "财务"},
        "EV_EBITDA":       {"name": "EV/EBITDA",            "category": "估值", "tech": False,
                           "desc": "企业价值倍数", "formula": "(mcap + debt - cash) / EBITDA",
                           "params": {}, "need_data": "财务"},
    },
    "基本面-盈利": {
        "ROE":             {"name": "ROE(净资产收益率)",     "category": "盈利", "tech": False,
                           "desc": "净利润/股东权益", "formula": "net_profit / equity",
                           "params": {}, "need_data": "财务"},
        "ROA":             {"name": "ROA(总资产收益率)",     "category": "盈利", "tech": False,
                           "desc": "净利润/总资产", "formula": "net_profit / total_assets",
                           "params": {}, "need_data": "财务"},
        "ROIC":            {"name": "ROIC(投入资本回报率)", "category": "盈利", "tech": False,
                           "desc": "税后经营利润/投入资本", "formula": "NOPAT / invested_capital",
                           "params": {}, "need_data": "财务"},
        "GROSS_MARGIN":    {"name": "毛利率",                "category": "盈利", "tech": False,
                           "desc": "毛利/营收", "formula": "(revenue - cost) / revenue",
                           "params": {}, "need_data": "财务"},
        "NET_MARGIN":      {"name": "净利率",                "category": "盈利", "tech": False,
                           "desc": "净利润/营收", "formula": "net_profit / revenue",
                           "params": {}, "need_data": "财务"},
        "EPS":             {"name": "EPS(每股收益)",         "category": "盈利", "tech": False,
                           "desc": "归母净利润/总股本", "formula": "net_profit / total_shares",
                           "params": {}, "need_data": "财务"},
    },
    "基本面-成长": {
        "REV_GROWTH":      {"name": "营收增速",              "category": "成长", "tech": False,
                           "desc": "本期营收同比", "formula": "(rev_t - rev_t-4) / rev_t-4",
                           "params": {}, "need_data": "财务"},
        "PROFIT_GROWTH":   {"name": "净利润增速",            "category": "成长", "tech": False,
                           "desc": "本期归母净利同比", "formula": "(np_t - np_t-4) / np_t-4",
                           "params": {}, "need_data": "财务"},
        "EBITDA_GROWTH":   {"name": "EBITDA 增速",          "category": "成长", "tech": False,
                           "desc": "EBITDA 同比增速", "formula": "(ebitda_t - ebitda_t-4) / ebitda_t-4",
                           "params": {}, "need_data": "财务"},
        "ASSET_GROWTH":    {"name": "资产增速",              "category": "成长", "tech": False,
                           "desc": "总资产同比", "formula": "(assets_t - assets_t-4) / assets_t-4",
                           "params": {}, "need_data": "财务"},
    },
    "基本面-质量/偿债": {
        "DEBT_RATIO":      {"name": "资产负债率",            "category": "质量", "tech": False,
                           "desc": "总负债/总资产", "formula": "total_liab / total_assets",
                           "params": {}, "need_data": "财务"},
        "CURRENT_RATIO":   {"name": "流动比率",              "category": "质量", "tech": False,
                           "desc": "流动资产/流动负债", "formula": "ca / cl",
                           "params": {}, "need_data": "财务"},
        "QUICK_RATIO":     {"name": "速动比率",              "category": "质量", "tech": False,
                           "desc": "(流动资产-存货)/流动负债", "formula": "(ca - inv) / cl",
                           "params": {}, "need_data": "财务"},
        "INTEREST_COVER":  {"name": "利息保障倍数",          "category": "质量", "tech": False,
                           "desc": "EBIT/利息支出", "formula": "ebit / interest_expense",
                           "params": {}, "need_data": "财务"},
        "ACCRUALS":        {"name": "应计利润(质量)",        "category": "质量", "tech": False,
                           "desc": "净利润-经营现金流", "formula": "ni - ocf",
                           "params": {}, "need_data": "财务"},
    },
    "规模": {
        "MCAP":            {"name": "总市值",                "category": "规模", "tech": False,
                           "desc": "股价*总股本", "formula": "close * total_shares",
                           "params": {}, "need_data": "share"},
        "LOG_MCAP":        {"name": "对数市值",              "category": "规模", "tech": False,
                           "desc": "市值对数(规模因子)", "formula": "log(mcap)",
                           "params": {}, "need_data": "share"},
        "FREE_FLOAT_MCAP": {"name": "自由流通市值",          "category": "规模", "tech": False,
                           "desc": "自由流通股本*股价", "formula": "free_float_shares * close",
                           "params": {}, "need_data": "share"},
    },
    "风格/情绪": {
        "BETA":            {"name": "Beta",                 "category": "风格", "tech": False,
                           "desc": "相对基准的 beta", "formula": "cov(stock, bench) / var(bench)",
                           "params": {"window": 60}, "need_data": "benchmark"},
        "ALPHA":           {"name": "Alpha(年化)",           "category": "风格", "tech": False,
                           "desc": "CAPM alpha", "formula": "ret - rf - beta*(bench_ret - rf)",
                           "params": {"window": 60}, "need_data": "benchmark"},
        "RESIDUAL_VOL":    {"name": "残差波动率",            "category": "风格", "tech": False,
                           "desc": "回归残差波动", "formula": "std(residuals, N)",
                           "params": {"N": 60}, "need_data": "benchmark"},
        "EARNYIELD":       {"name": "盈利收益率",            "category": "风格", "tech": False,
                           "desc": "E/P(逆 PE)", "formula": "ep_ratio",
                           "params": {}, "need_data": "财务"},
        "BTOP":            {"name": "B/P(账面市值比)",      "category": "风格", "tech": False,
                           "desc": "权益/市值", "formula": "book_equity / mcap",
                           "params": {}, "need_data": "财务"},
    },
    "风险": {
        "MAX_DD":          {"name": "最大回撤",              "category": "风险", "tech": True,
                           "desc": "回撤序列的最大值", "formula": "max(1 - C/cummax(C))",
                           "params": {}, "dir": -1},
        "DOWNSIDE_VOL":    {"name": "下行波动率",            "category": "风险", "tech": True,
                           "desc": "负收益标准差", "formula": "std(min(ret,0), N)",
                           "params": {"N": 20}, "dir": -1},
        "VAR95":           {"name": "VaR(95%)",             "category": "风险", "tech": True,
                           "desc": "历史 VaR", "formula": "quantile(ret, 0.05)",
                           "params": {"N": 60}, "dir": -1},
        "CVAR95":          {"name": "CVaR(95%)",            "category": "风险", "tech": True,
                           "desc": "条件 VaR", "formula": "mean(ret[ret <= VaR95])",
                           "params": {"N": 60}, "dir": -1},
        "CALMAR":          {"name": "Calmar",               "category": "风险", "tech": True,
                           "desc": "年化收益/最大回撤", "formula": "annual_ret / max_drawdown",
                           "params": {}},
    },
    "分红": {
        "DIV_YIELD":       {"name": "股息率",                "category": "分红", "tech": False,
                           "desc": "近12月分红/股价", "formula": "dividend_12m / close",
                           "params": {}, "need_data": "dividend"},
        "PAYOUT_RATIO":    {"name": "分红率",                "category": "分红", "tech": False,
                           "desc": "分红/净利润", "formula": "dividend / net_profit",
                           "params": {}, "need_data": "财务"},
    },
    "分析师预期": {
        "FYPESTIM":        {"name": "一致预期净利润",         "category": "预期", "tech": False,
                           "desc": "分析师一致预期净利", "formula": "consensus_eps * total_shares",
                           "params": {}, "need_data": "consensus"},
        "RATING_AVG":      {"name": "分析师综合评级",         "category": "预期", "tech": False,
                           "desc": "1-5 分制平均评级", "formula": "mean(rating)",
                           "params": {}, "need_data": "consensus"},
        "EARNING_SURP":    {"name": "盈利超预期",             "category": "预期", "tech": False,
                           "desc": "实际 EPS - 一致预期", "formula": "eps - eps_forecast",
                           "params": {}, "need_data": "consensus"},
    },
    "行业/龙头": {
        "INDUSTRY_RET":    {"name": "行业收益率",            "category": "行业", "tech": False,
                           "desc": "申万行业 N 日收益", "formula": "mean(stock_in_industry.ret, N)",
                           "params": {"N": 20}, "need_data": "industry"},
        "IS_LEADER":       {"name": "行业龙头标记",          "category": "行业", "tech": False,
                           "desc": "市值前 20% 标记", "formula": "rank(mcap) > 0.8",
                           "params": {}, "need_data": "share+industry"},
    },
    "聪明钱/北向": {
        "HSF_HOLD":        {"name": "北向持股变化",          "category": "北向", "tech": False,
                           "desc": "北向持股变化", "formula": "holdings_t - holdings_t-1",
                           "params": {}, "need_data": "north_bound"},
        "HSF_RATIO":       {"name": "北向占流通比",          "category": "北向", "tech": False,
                           "desc": "北向持股/流通股本", "formula": "holdings / free_float_shares",
                           "params": {}, "need_data": "north_bound"},
    },
    "龙虎榜/大宗交易": {
        "LHB_NET":         {"name": "龙虎榜净买入",           "category": "龙虎榜", "tech": False,
                           "desc": "龙虎榜净买入金额", "formula": "lhb_buy - lhb_sell",
                           "params": {}, "need_data": "lhb"},
        "BLOCK_DEAL":      {"name": "大宗交易溢价",          "category": "龙虎榜", "tech": False,
                           "desc": "大宗成交价/收盘价 - 1", "formula": "block_price/close - 1",
                           "params": {}, "need_data": "block_deal"},
    },
}


# ==================== 因子计算器 ====================
class FactorCalculator:
    """基于 OHLCV 计算聚宽风格的技术类因子；非技术类因子给出占位提示。"""

    def __init__(self, df):
        if df is None or df.empty:
            raise ValueError("因子计算需要非空行情数据")
        self.df = df.copy().sort_index()
        for col in ("open", "high", "low", "close", "volume"):
            if col not in self.df.columns:
                raise ValueError(f"行情数据缺少必要列: {col}")
        self._cache = {}

    def compute(self, code, **kwargs):
        code = code.upper()
        if code in self._cache:
            return self._cache[code]
        method = getattr(self, f"calc_{code}", None)
        if method is None:
            raise NotImplementedError(
                f"因子 {code} 无计算实现（need_data="
                f"{factor_meta(code).get('need_data', '未知')}，需聚宽/Choice 等财务数据源）")
        s = method(**kwargs)
        self._cache[code] = s
        return s

    @staticmethod
    def implemented(codes):
        """过滤出真正有 calc_* 实现的因子码。

        'tech' 标志只说明因子来源是价量，不等于这里能算——估值/财务/资金流类
        因子都标 tech=False 但仍需外部数据源。用 hasattr 判定才和 compute() 一致。
        """
        return [c for c in codes if hasattr(FactorCalculator, f"calc_{c}")]

    def summary(self, codes, **kwargs):
        cols = {}
        for c in codes:
            try:
                s = self.compute(c, **kwargs)
                cols[s.name] = s
            except Exception as e:
                logger.warning(f"因子 {c} 计算失败: {e}")
        return pd.DataFrame(cols)

    # ---- 工具 ----
    def _ret(self, n=1):
        return self.df["close"].pct_change(n)

    # ---- 技术面 ----
    def calc_MA(self, N=5):
        return self.df["close"].rolling(N).mean().rename(f"MA{N}")

    def calc_EMA(self, N=12):
        return self.df["close"].ewm(span=N, adjust=False).mean().rename(f"EMA{N}")

    def calc_MACD(self, fast=12, slow=26, signal=9):
        exp1 = self.df["close"].ewm(span=fast, adjust=False).mean()
        exp2 = self.df["close"].ewm(span=slow, adjust=False).mean()
        dif = exp1 - exp2
        dea = dif.ewm(span=signal, adjust=False).mean()
        macd = (dif - dea) * 2
        return macd.rename(f"MACD({fast},{slow},{signal})")

    def calc_RSI(self, N=14):
        delta = self.df["close"].diff()
        gain = delta.clip(lower=0).rolling(N).mean()
        loss = (-delta.clip(upper=0)).rolling(N).mean()
        rs = gain / (loss.replace(0, 1e-12))
        return (100 - 100 / (1 + rs)).rename(f"RSI{N}")

    def calc_KDJ(self, N=9, M1=3, M2=3):
        low_n = self.df["low"].rolling(N).min()
        high_n = self.df["high"].rolling(N).max()
        rsv = (self.df["close"] - low_n) / (high_n - low_n).replace(0, 1e-12) * 100
        k = rsv.ewm(alpha=1 / M1, adjust=False).mean()
        d = k.ewm(alpha=1 / M2, adjust=False).mean()
        j = 3 * k - 2 * d
        return j.rename(f"KDJ({N},{M1},{M2})")

    def calc_BOLL(self, N=20, k=2):
        mid = self.df["close"].rolling(N).mean()
        std = self.df["close"].rolling(N).std()
        upper = mid + k * std
        lower = mid - k * std
        bw = (upper - lower) / mid
        return bw.rename(f"BOLL_WIDTH({N},{k})")

    def calc_BIAS(self, N=6):
        ma = self.df["close"].rolling(N).mean()
        return ((self.df["close"] - ma) / ma * 100).rename(f"BIAS{N}")

    def calc_CCI(self, N=14):
        tp = (self.df["high"] + self.df["low"] + self.df["close"]) / 3
        ma = tp.rolling(N).mean()
        md = (tp - ma).abs().rolling(N).mean()
        return ((tp - ma) / (0.015 * md.replace(0, 1e-12))).rename(f"CCI{N}")

    def calc_ATR(self, N=14):
        prev_c = self.df["close"].shift(1)
        tr = pd.concat([
            self.df["high"] - self.df["low"],
            (self.df["high"] - prev_c).abs(),
            (self.df["low"] - prev_c).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(N).mean().rename(f"ATR{N}")

    def calc_BBI(self):
        ma3 = self.df["close"].rolling(3).mean()
        ma6 = self.df["close"].rolling(6).mean()
        ma12 = self.df["close"].rolling(12).mean()
        ma24 = self.df["close"].rolling(24).mean()
        return ((ma3 + ma6 + ma12 + ma24) / 4).rename("BBI")

    # ---- 动量 ----
    def calc_MOM(self, N=10):
        return (self.df["close"] - self.df["close"].shift(N)).rename(f"MOM{N}")

    def calc_ROC(self, N=12):
        prev = self.df["close"].shift(N)
        return ((self.df["close"] - prev) / prev.replace(0, 1e-12) * 100).rename(f"ROC{N}")

    def calc_MOMENTUM(self, N=10, M=5):
        prev = self.df["close"].shift(N)
        roc = (self.df["close"] - prev) / prev.replace(0, 1e-12)
        return (roc - roc.shift(M)).rename(f"Momentum({N},{M})")

    def calc_ADX(self, N=14):
        up = self.df["high"].diff()
        down = -self.df["low"].diff()
        plus_dm = ((up > down) & (up > 0)) * up
        minus_dm = ((down > up) & (down > 0)) * down
        prev_c = self.df["close"].shift(1)
        tr = pd.concat([
            self.df["high"] - self.df["low"],
            (self.df["high"] - prev_c).abs(),
            (self.df["low"] - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(N).mean()
        plus_di = 100 * plus_dm.rolling(N).mean() / atr.replace(0, 1e-12)
        minus_di = 100 * minus_dm.rolling(N).mean() / atr.replace(0, 1e-12)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-12)
        return dx.rolling(N).mean().rename(f"ADX{N}")

    def calc_AROON(self, N=25):
        # 标准 Aroon：100*(N - 距N日极值的天数)/N，极值越新分越高。
        # 原实现写成 (N-1-argmax)/N，等于 100*距极值天数/N——整个指标镜像了
        # （今天创新高却得 0 分）。回归注册表 :85 声明的公式。
        roll_high = self.df["high"].rolling(N)
        aroon_up = roll_high.apply(
            lambda x: (N - ((len(x) - 1) - int(np.asarray(x).argmax()))) / N * 100,
            raw=False)
        roll_low = self.df["low"].rolling(N)
        aroon_dn = roll_low.apply(
            lambda x: (N - ((len(x) - 1) - int(np.asarray(x).argmin()))) / N * 100,
            raw=False)
        return (aroon_up - aroon_dn).rename(f"AROON({N})")

    # ---- 波动率 ----
    def calc_HIST_VOL(self, N=20):
        return (self._ret(1).rolling(N).std() * (252 ** 0.5)).rename(f"HIST_VOL{N}")

    def calc_PARKINSON(self, N=20):
        rs = (np.log(self.df["high"] / self.df["low"]) ** 2)
        return np.sqrt(rs.rolling(N).sum() / (4 * N * np.log(2))).rename(f"PARKINSON{N}")

    def calc_GK_VOL(self, N=20):
        u = np.log(self.df["high"] / self.df["low"]) ** 2
        c = np.log(self.df["close"] / self.df["open"]) ** 2
        val = 0.5 * u - (2 * np.log(2) - 1) * c
        return np.sqrt(val.rolling(N).mean()).rename(f"GK_VOL{N}")

    def calc_RS_VOL(self):
        u = np.log(self.df["high"] / self.df["close"]) * np.log(self.df["high"] / self.df["open"])
        d = np.log(self.df["low"] / self.df["close"]) * np.log(self.df["low"] / self.df["open"])
        return np.sqrt((u + d).rolling(20).mean()).rename("RS_VOL")

    def calc_YZ_VOL(self, N=20):
        prev_c = self.df["close"].shift(1)
        oo = (np.log(self.df["open"] / prev_c)) ** 2
        cc = (np.log(self.df["close"] / self.df["open"])) ** 2
        rs = (np.log(self.df["high"] / self.df["close"]) * np.log(self.df["high"] / self.df["open"])
              + np.log(self.df["low"] / self.df["close"]) * np.log(self.df["low"] / self.df["open"]))
        k = 0.34 / (1.34 + (N + 1) / (N - 1))
        var = oo.rolling(N).mean() + k * cc.rolling(N).mean() + (1 - k) * rs.rolling(N).mean()
        return np.sqrt(var).rename(f"YZ_VOL{N}")

    def calc_BOLL_WIDTH(self, N=20, k=2):
        return self.calc_BOLL(N=N, k=k)

    # ---- 成交量 ----
    def calc_VOL_MA(self, N=5):
        return self.df["volume"].rolling(N).mean().rename(f"VOL_MA{N}")

    def calc_VOL_RATIO(self, N=5):
        past = self.df["volume"].shift(1).rolling(N).mean()
        return (self.df["volume"] / past.replace(0, 1e-12)).rename(f"VOL_RATIO{N}")

    def calc_OBV(self):
        sign = np.sign(self.df["close"].diff().fillna(0))
        return (sign * self.df["volume"]).cumsum().rename("OBV")

    def calc_VWAP(self, N=20):
        tp = (self.df["high"] + self.df["low"] + self.df["close"]) / 3
        return ((tp * self.df["volume"]).rolling(N).sum()
                / self.df["volume"].rolling(N).sum().replace(0, 1e-12)
                ).rename(f"VWAP{N}")   # 原 bug：没 rename，summary 列名变 None

    def calc_MFI(self, N=14):
        tp = (self.df["high"] + self.df["low"] + self.df["close"]) / 3
        mf = tp * self.df["volume"]
        delta = tp.diff()
        pos = mf.where(delta > 0, 0).rolling(N).sum()
        neg = (-mf.where(delta < 0, 0)).rolling(N).sum()
        return (100 - 100 / (1 + pos / neg.replace(0, 1e-12))).rename(f"MFI{N}")

    def calc_VSTDDEV(self, N=20):
        return self.df["volume"].rolling(N).std().rename(f"VSTDDEV{N}")

    def calc_VR(self, N=26):
        ret = self._ret(1)
        up_v = self.df["volume"].where(ret > 0, 0).rolling(N).sum()
        dn_v = self.df["volume"].where(ret < 0, 0).rolling(N).sum()
        return (up_v / dn_v.replace(0, 1e-12)).rename(f"VR{N}")

    # ---- 换手率 ----
    def calc_ILLIQUIDITY(self, N=20):
        # Amihud 非流动性：经典定义用成交额（|ret|/amount）；用股数会被价格水平
        # 扭曲（同流动性下 10 元股与 1000 元股差 100 倍）。数据缺 amount 列时回落股数。
        money = self.df["amount"] if "amount" in self.df.columns else self.df["volume"]
        amihud = self._ret(1).abs() / money.replace(0, 1e-12)
        return amihud.rolling(N).mean().rename(f"ILLIQUIDITY{N}")

    # ---- 风险 ----
    def calc_MAX_DD(self):
        dd = 1 - self.df["close"] / self.df["close"].cummax()
        return dd.expanding().max().rename("MAX_DD")

    def calc_DOWNSIDE_VOL(self, N=20):
        neg = self._ret(1).clip(upper=0)
        return neg.rolling(N).std().rename(f"DOWNSIDE_VOL{N}")

    def calc_VAR95(self, N=60):
        return self._ret(1).rolling(N).quantile(0.05).rename(f"VAR95({N})")

    def calc_CVAR95(self, N=60):
        r = self._ret(1).rolling(N)
        return r.apply(lambda x: x[x <= x.quantile(0.05)].mean(), raw=False).rename(f"CVAR95({N})")

    def calc_CALMAR(self):
        ann = self._ret(1).mean() * 252
        dd = self.calc_MAX_DD().replace(0, 1e-12)
        return (ann / dd).rename("CALMAR")

    # ---- 风格 ----
    # BETA / ALPHA / RESIDUAL_VOL 不在这里实现：三者都需要基准指数序列，
    # FactorCalculator 只有单票 OHLCV。原先用自身收益率的 std/mean 冒充，
    # 与 HIST_VOL 完全共线（实测同值），在打分面板会把风险块三重计数。
    # 要做真的，先把基准收盘序列传进来再写。注册表保留条目作 need_data 占位。


# ==================== 因子报告 HTML ====================
def build_factor_html(name, key, df, results, errs):
    rows = ""
    for r in results:
        cls = "ok" if r["tech"] else "warn"
        tag = "OHLCV 可计算" if r["tech"] else f"需 {r['need_data'] or '外部数据'}"
        ic = "—" if r["ic"] is None else f"{r['ic']:+.4f}"
        ic_cls = "pos" if r["ic"] and r["ic"] > 0 else ("neg" if r["ic"] and r["ic"] < 0 else "")

        def fmt(x):
            return "—" if x is None else f"{x:.4f}"
        params = ", ".join(f"{k}={v}" for k, v in r["params"].items()) or "—"
        rows += f"""
            <tr>
              <td><b>{r['code']}</b><br><span class='muted'>{r['name']}</span></td>
              <td>{r['category']}</td>
              <td><span class='tag {cls}'>{tag}</span></td>
              <td class='num'>{fmt(r['last'])}</td>
              <td class='num'>{fmt(r['mean'])}</td>
              <td class='num'>{fmt(r['std'])}</td>
              <td class='num'>{fmt(r['min'])}</td>
              <td class='num'>{fmt(r['max'])}</td>
              <td class='num {ic_cls}'>{ic}</td>
              <td class='muted'>{r['desc']}<br><code>{r['formula']}</code><br><span class='muted'>params: {params}</span></td>
            </tr>"""
    err_block = ""
    if errs:
        err_block = "<h3>未计算的因子(依赖外部数据)</h3><ul class='err'>" + \
                    "".join(f"<li>{e}</li>" for e in errs) + "</ul>"
    return f"""<!DOCTYPE html>
<html><head><meta charset='utf-8'>
<title>因子分析报告 - {name}</title>
<style>
  body{{font-family:"Microsoft YaHei",Arial,sans-serif;margin:0;padding:20px;background:#f5f7fa;color:#2c3e50}}
  .container{{max-width:1280px;margin:0 auto;background:white;padding:28px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
  h1{{margin:0 0 6px 0;font-size:24px}}
  h3{{margin-top:24px;color:#34495e;border-left:4px solid #2196f3;padding-left:8px}}
  .muted{{color:#7f8c8d;font-size:12px}}
  .meta{{color:#7f8c8d;font-size:13px;margin-bottom:16px}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th,td{{padding:8px 10px;border-bottom:1px solid #ecf0f1;vertical-align:top;text-align:left}}
  th{{background:#f8f9fa;color:#34495e;font-weight:600}}
  td.num{{font-family:Consolas,monospace;text-align:right}}
  .tag{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px}}
  .tag.ok{{background:#e8f5e9;color:#2e7d32}}
  .tag.warn{{background:#fff3e0;color:#ef6c00}}
  .pos{{color:#27ae60;font-weight:600}}
  .neg{{color:#e74c3c;font-weight:600}}
  code{{background:#f4f6f8;padding:1px 4px;border-radius:3px;font-size:11px}}
  .err li{{color:#c0392b;font-size:13px;margin:2px 0}}
</style></head><body><div class='container'>
  <h1>📊 量化因子分析报告</h1>
  <div class='meta'>标的: <b>{name}</b> · 数据源: {key[0]} · 市场: {key[1]} · 区间: {key[3]} ~ {key[4]} · 复权: {key[5]} · 共 {len(df)} 条行情(code={key[2]})</div>
  <p class='muted'>因子库来源: <a href='https://www.joinquant.com/view/factorlib/list' target='_blank'>聚宽因子库</a>。本报告基于 OHLCV 计算可由行情推导的因子,并给出与次日收益的 Pearson IC。</p>
  <h3>因子结果</h3>
  <table>
    <thead><tr>
      <th>代码 / 名称</th><th>分类</th><th>状态</th>
      <th>最新</th><th>均值</th><th>std</th><th>min</th><th>max</th>
      <th>IC(次日)</th><th>说明 / 公式 / 参数</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {err_block}
</div></body></html>"""


# ==================== Mixin：UI 集成 ====================
def factor_meta(code):
    """根据 code 找到对应元数据；找不到时返回占位。"""
    for sub in JOINQUANT_FACTORS.values():
        if code in sub:
            return sub[code]
    return {"name": code, "desc": "", "tech": False, "formula": "", "category": "未知", "params": {}}


class FactorPanelMixin:
    """让 MyBacktestPanel 获得「📊 量化因子分析」UI + 计算流程。

    依赖宿主具备:
      - self._data_cache, self._cache_lock           (行情缓存)
      - self._set_load_status(text)                  (状态行更新)
      - self._start_busy(text) / self._finish_busy() (忙状态)
      - self._on_task_error(msg)                     (失败回调)
      - self.last_report_path                        (报告路径属性)
      - self.M1_width                                (左侧栏宽)
    """

    def _create_factor_module(self, parent, sizer):
        factor_box = wx.StaticBox(parent, -1, wxs("📊 量化因子分析（聚宽因子库）"))
        factor_sizer = wx.StaticBoxSizer(factor_box, wx.VERTICAL)

        hint = wx.StaticText(factor_box, label="基于聚宽因子库 factorlib,含 18 大类、60+ 因子\n需先加载行情数据,仅 OHLCV 可计算的因子(tech)现在可用")
        hint.SetForegroundColour(wx.Colour(120, 120, 120))
        factor_sizer.Add(hint, 0, wx.EXPAND | wx.ALL, 4)

        cat_row = wx.BoxSizer(wx.HORIZONTAL)
        cat_row.Add(wx.StaticText(factor_box, label="分类:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.factor_category_choice = wx.ComboBox(
            factor_box, choices=["全部"] + list(JOINQUANT_FACTORS.keys()),
            value="全部", style=wx.CB_READONLY | wx.CB_DROPDOWN,
        )
        self.factor_category_choice.Bind(wx.EVT_COMBOBOX, self._on_factor_category_changed)
        cat_row.Add(self.factor_category_choice, 1, wx.EXPAND)
        factor_sizer.Add(cat_row, 0, wx.EXPAND | wx.ALL, 4)

        self.factor_list = wx.CheckListBox(factor_box, choices=[], size=(-1, 160))
        self.factor_list.Bind(wx.EVT_CHECKLISTBOX, self._on_factor_check_changed)
        factor_sizer.Add(self.factor_list, 0, wx.EXPAND | wx.ALL, 4)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self.factor_select_all_but = wx.Button(factor_box, -1, "全选")
        self.factor_clear_but = wx.Button(factor_box, -1, "清空")
        self.factor_select_all_but.Bind(wx.EVT_BUTTON, lambda e: self._set_all_factors_checked(True))
        self.factor_clear_but.Bind(wx.EVT_BUTTON, lambda e: self._set_all_factors_checked(False))
        btn_row.Add(self.factor_select_all_but, 1, wx.RIGHT, 4)
        btn_row.Add(self.factor_clear_but, 1)
        factor_sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 4)

        action_row = wx.BoxSizer(wx.HORIZONTAL)
        self.calc_factor_but = wx.Button(factor_box, -1, wxs("🧮 计算并查看"))
        self.calc_factor_but.SetBackgroundColour(wx.Colour(33, 150, 243))
        self.calc_factor_but.SetForegroundColour(wx.Colour(255, 255, 255))
        self.calc_factor_but.Bind(wx.EVT_BUTTON, self.on_calc_factors)
        self.browse_factorlib_but = wx.Button(factor_box, -1, wxs("🌐 因子库说明"))
        self.browse_factorlib_but.Bind(wx.EVT_BUTTON,
            lambda e: webbrowser.open("https://www.joinquant.com/view/factorlib/list"))
        action_row.Add(self.calc_factor_but, 2, wx.RIGHT, 4)
        action_row.Add(self.browse_factorlib_but, 1)
        factor_sizer.Add(action_row, 0, wx.EXPAND | wx.ALL, 4)

        self.factor_desc = wx.StaticText(factor_box, label="勾选因子后点击「计算并查看」")
        self.factor_desc.SetForegroundColour(wx.Colour(80, 80, 80))
        self.factor_desc.Wrap(self.M1_width - 40)
        factor_sizer.Add(self.factor_desc, 0, wx.EXPAND | wx.ALL, 4)

        sizer.Add(factor_sizer, 0, wx.EXPAND | wx.ALL, 5)
        self._on_factor_category_changed(None)

    def _on_factor_category_changed(self, event):
        cat = self.factor_category_choice.GetValue()
        if cat == "全部":
            codes = [c for sub in JOINQUANT_FACTORS.values() for c in sub.keys()]
        else:
            codes = list(JOINQUANT_FACTORS[cat].keys())
        # 只列出真正能算的因子；未实现的仍保留在 JOINQUANT_FACTORS 里作为因子清单，
        # 但不该出现在可勾选列表里（勾了只会抛 NotImplementedError）
        codes = FactorCalculator.implemented(codes)
        labels = [f"✅ {c} - {factor_meta(c)['name']}" for c in codes]
        self.factor_list.Set(labels)
        self._factor_code_map = {labels[i]: codes[i] for i in range(len(codes))}
        self._set_all_factors_checked(False)

    def _on_factor_check_changed(self, event):
        sel = self.factor_list.GetCheckedStrings()
        if not sel:
            self.factor_desc.SetLabel("勾选因子后点击「计算并查看」")
            return
        lines = []
        for s in sel[:5]:
            code = self._factor_code_map.get(s, "")
            meta = factor_meta(code)
            lines.append(f"• {meta['name']}: {meta['desc']}")
        if len(sel) > 5:
            lines.append(f"... 共 {len(sel)} 个")
        self.factor_desc.SetLabel("\n".join(lines))

    def _set_all_factors_checked(self, checked):
        for i in range(self.factor_list.GetCount()):
            self.factor_list.Check(i, checked)
        self._on_factor_check_changed(None)

    def on_calc_factors(self, event):
        sel = self.factor_list.GetCheckedStrings()
        if not sel:
            wx.MessageBox("请先勾选至少一个因子", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        codes = [self._factor_code_map[s] for s in sel]

        last_key, last_val = None, None
        with self._cache_lock:
            if self._data_cache:
                last_key = list(self._data_cache.keys())[-1]
                last_val = self._data_cache[last_key]
        if last_val is None:
            wx.MessageBox("请先点击「加载行情数据」获取行情", "提示", wx.OK | wx.ICON_INFORMATION)
            return
        df, name = last_val

        self._start_busy("正在计算因子…")
        threading.Thread(target=self._factor_worker,
                         args=(df, name, last_key, codes), daemon=True).start()

    def _factor_worker(self, df, name, key, codes):
        try:
            calc = FactorCalculator(df)
            results, errs = [], []
            nxt = df["close"].pct_change().shift(-1)
            for code in codes:
                meta = factor_meta(code)
                try:
                    s = calc.compute(code)
                    aligned = pd.concat([s, nxt], axis=1).dropna()
                    aligned.columns = ["f", "r"]
                    if len(aligned) > 5 and aligned["f"].std() > 0:
                        ic = aligned["f"].corr(aligned["r"])
                    else:
                        ic = None
                    sd = s.dropna()
                    results.append({
                        "code": code, "name": meta["name"], "category": meta["category"],
                        "tech": meta.get("tech", False), "need_data": meta.get("need_data", ""),
                        "desc": meta["desc"], "formula": meta.get("formula", ""),
                        "params": meta.get("params", {}),
                        "last": float(sd.iloc[-1]) if not sd.empty else None,
                        "mean": float(sd.mean()) if not sd.empty else None,
                        "std": float(sd.std()) if not sd.empty else None,
                        "min": float(sd.min()) if not sd.empty else None,
                        "max": float(sd.max()) if not sd.empty else None,
                        "ic": ic,
                    })
                except NotImplementedError as nie:
                    errs.append(f"{code}: {str(nie)}（需 {meta.get('need_data', '外部数据')}）")
                except Exception as e:
                    errs.append(f"{code}: {e}")
            html = build_factor_html(name, key, df, results, errs)
            out_dir = _BKT_DIR
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, "factor_report.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            wx.CallAfter(self._on_factor_done, path)
        except Exception as e:
            logger.exception("因子计算异常")
            wx.CallAfter(self._on_task_error, f"因子计算失败: {e}")

    def _on_factor_done(self, path):
        self._finish_busy()
        self.last_report_path = path
        webbrowser.open(f"file:///{path}")
        self._set_load_status(f"因子报告已生成:{os.path.basename(path)}")


# ==================== 拖拽安装新策略 ====================
USER_STRATEGY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "strategies_user")
USER_STRATEGY_DIR = os.path.normpath(USER_STRATEGY_DIR)
os.makedirs(USER_STRATEGY_DIR, exist_ok=True)


class _StrategyFileDropTarget(wx.FileDropTarget):
    """拖拽文件到指定区域时触发 on_drop_files(paths)"""

    def __init__(self, on_drop_files):
        super().__init__()
        self._on_drop_files = on_drop_files

    def OnDropFiles(self, x, y, filenames):
        try:
            self._on_drop_files(list(filenames))
            return True
        except Exception as e:
            logger.exception("拖拽策略文件失败")
            wx.MessageBox(f"拖拽失败: {e}", "错误", wx.OK | wx.ICON_ERROR)
            return False


def _validate_strategy_file(src_path):
    """读取策略源文件，做基础语法与契约校验。返回 (class_name, params_dict, error_message_or_None)。

    契约（最小集）：
      - 文件可被 compile 解析
      - 至少定义了一个继承 bt.Strategy 的类
      - 顶层具有 'params' 元组（pname, default）以回填到参数 UI
    """
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            src = f.read()
    except UnicodeDecodeError:
        with open(src_path, "r", encoding="gbk", errors="ignore") as f:
            src = f.read()

    try:
        tree = __import__("ast").parse(src, filename=src_path)
    except SyntaxError as e:
        return None, None, f"语法错误: {e}"

    import re as _re
    if "import backtrader" not in src and "from backtrader" not in src:
        return None, None, "缺少 import backtrader"

    cls_name = None
    for node in tree.body:
        if isinstance(node, __import__("ast").ClassDef):
            for base in node.bases:
                base_name = getattr(base, "id", None) or getattr(base, "attr", None)
                if base_name and "Strategy" in base_name:
                    cls_name = node.name
                    break
        if cls_name:
            break
    if not cls_name:
        return None, None, "未找到继承 bt.Strategy 的类"

    # 解析 params
    params = {}
    for node in tree.body:
        if isinstance(node, __import__("ast").ClassDef) and node.name == cls_name:
            for stmt in node.body:
                if isinstance(stmt, __import__("ast").Assign):
                    for tgt in stmt.targets:
                        if getattr(tgt, "id", None) == "params" and isinstance(stmt.value, __import__("ast").Tuple):
                            for el in stmt.value.elts:
                                if isinstance(el, __import__("ast").Tuple) and len(el.elts) >= 2:
                                    pname = getattr(el.elts[0], "value", None) or getattr(el.elts[0], "id", None)
                                    if not pname:
                                        continue
                                    raw = __import__("ast").unparse(el.elts[1])
                                    try:
                                        if "." in raw or "e" in raw.lower():
                                            params[pname] = float(raw)
                                        else:
                                            params[pname] = int(raw)
                                    except Exception:
                                        try:
                                            params[pname] = float(raw)
                                        except Exception:
                                            params[pname] = raw
    return cls_name, params, None


def _install_strategy_module(src_path, target_dir=USER_STRATEGY_DIR, target_name=None):
    """把策略文件复制到 target_dir 并 importlib 加载；返回 (module, class_name, params, error)."""
    import shutil
    import time
    import importlib.util

    base = os.path.basename(src_path)
    if not base.endswith(".py"):
        return None, None, None, "仅支持 .py 文件"

    target_name = target_name or base
    target_path = os.path.join(target_dir, target_name)
    try:
        shutil.copy2(src_path, target_path)
    except Exception as e:
        return None, None, None, f"复制失败: {e}"

    spec = importlib.util.spec_from_file_location(
        f"qbot.gui.strategies_user.{os.path.splitext(target_name)[0]}",
        target_path,
    )
    if spec is None or spec.loader is None:
        return None, None, None, "无法构造模块 spec"
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        return None, None, None, f"加载失败: {e}"

    cls_name, params, err = _validate_strategy_file(target_path)
    if err:
        return mod, None, None, err
    cls = getattr(mod, cls_name, None)
    if cls is None:
        return mod, None, None, f"未找到类 {cls_name}"
    return mod, cls, params, None


def _reload_strategy_module(target_name):
    """重新加载一个已存在的用户策略模块（用于热重载）。"""
    import importlib
    import sys
    mod_name = f"qbot.gui.strategies_user.{os.path.splitext(target_name)[0]}"
    if mod_name in sys.modules:
        return importlib.reload(sys.modules[mod_name])
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(USER_STRATEGY_DIR, target_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[mod_name] = mod
    return mod


class StrategyInstallerMixin:
    """让 MyBacktestPanel 获得「拖拽 / 浏览安装新策略」能力。

    依赖宿主具备：
      - self.STRATEGY_MAP   （dict, 策略名 -> 类）
      - self.STRATEGY_PARAMS (dict, 策略名 -> {param: default})
      - self.strategy_list / self.strategy_choice / self._set_load_status
    """

    DROP_AREA_LABEL = (
        "🧩 把 .py 策略文件拖到这里\n"
        "或点击「📂 浏览文件…」选择\n\n"
        "策略类必须:\n"
        "  • 继承 bt.Strategy\n"
        "  • 在类内定义 params = (('pname', default), ...)"
    )

    def _create_strategy_installer(self, parent, sizer):
        """在 sizer 末尾插入「拖拽安装新策略」模块。"""
        box = wx.StaticBox(parent, -1, wxs("🧩 拖拽安装新策略"))
        self._installer_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        # 拖拽目标区（StaticText 模拟，背景高亮）
        self.drop_target = wx.Panel(box, -1)
        self.drop_target.SetMinSize(wx.Size(-1, 110))
        self.drop_target.SetBackgroundColour(wx.Colour(245, 245, 245))
        self.drop_target.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        dt = _StrategyFileDropTarget(self._handle_dropped_files)
        self.drop_target.SetDropTarget(dt)

        # 边框高亮
        self.drop_target.Bind(wx.EVT_PAINT, self._paint_drop_target)

        inner = wx.BoxSizer(wx.VERTICAL)
        self.drop_label = wx.StaticText(self.drop_target, label=wxs(self.DROP_AREA_LABEL))
        self.drop_label.SetForegroundColour(wx.Colour(100, 100, 100))
        self.drop_label.Wrap(240)
        inner.Add(self.drop_label, 1, wx.EXPAND | wx.ALL, 6)
        self.drop_target.SetSizer(inner)

        # 浏览 + 刷新
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        browse_btn = wx.Button(box, -1, wxs("📂 浏览文件…"))
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse_strategy_file)
        refresh_btn = wx.Button(box, -1, wxs("🔄 刷新已安装"))
        refresh_btn.Bind(wx.EVT_BUTTON, lambda e: self._refresh_installed_strategies())
        remove_btn = wx.Button(box, -1, wxs("🗑 移除选中"))
        remove_btn.Bind(wx.EVT_BUTTON, self._on_remove_selected_strategy)
        btn_row.Add(browse_btn, 1, wx.RIGHT, 4)
        btn_row.Add(refresh_btn, 1, wx.RIGHT, 4)
        btn_row.Add(remove_btn, 1)

        # 已安装策略列表
        self.installed_list = wx.ListBox(box, choices=[], size=(-1, 90),
                                          style=wx.LB_SINGLE | wx.LB_HSCROLL)
        self.installed_list.Bind(wx.EVT_LISTBOX, self._on_installed_selected)
        self.installed_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_reload_selected)

        self._installer_sizer.Add(self.drop_target, 0, wx.EXPAND | wx.ALL, 4)
        self._installer_sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, 4)
        self._installer_sizer.Add(wx.StaticText(box, label="已安装(双击=热重载):"), 0, wx.LEFT | wx.RIGHT, 6)
        self._installer_sizer.Add(self.installed_list, 1, wx.EXPAND | wx.ALL, 4)
        sizer.Add(self._installer_sizer, 0, wx.EXPAND | wx.ALL, 5)

        # 初始化一次
        self._refresh_installed_strategies()

    def _paint_drop_target(self, event):
        """给拖拽区画一个虚线边框，提示可拖入。"""
        dc = wx.PaintDC(self.drop_target)
        dc.SetPen(wx.Pen(wx.Colour(33, 150, 243), 2, wx.PENSTYLE_DOT_DASH))
        w, h = self.drop_target.GetSize()
        dc.DrawRectangle(1, 1, w - 2, h - 2)
        event.Skip()

    def _handle_dropped_files(self, paths):
        for p in paths:
            if not p.lower().endswith(".py"):
                wx.MessageBox(f"跳过非 .py 文件: {p}", "提示", wx.OK | wx.ICON_INFORMATION)
                continue
            self._install_one(p)

    def _on_browse_strategy_file(self, event):
        dlg = wx.FileDialog(self, "选择策略文件", wildcard="Python (*.py)|*.py",
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        if dlg.ShowModal() == wx.ID_OK:
            for p in dlg.GetPaths():
                self._install_one(p)
        dlg.Destroy()

    def _install_one(self, src_path):
        """复制 + 加载 + 注册一个策略。"""
        base = os.path.basename(src_path)
        # 重复时让用户决定
        target_path = os.path.join(USER_STRATEGY_DIR, base)
        if os.path.exists(target_path):
            choice = wx.MessageBox(
                f"已存在同名策略 {base}。\n\n是=覆盖,否=另存为,取消=跳过",
                "策略已存在", wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION)
            if choice == wx.ID_CANCEL:
                return
            if choice == wx.ID_NO:
                dlg = wx.FileDialog(self, "另存为", defaultDir=USER_STRATEGY_DIR,
                                    defaultFile=base, wildcard="Python (*.py)|*.py",
                                    style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
                if dlg.ShowModal() != wx.ID_OK:
                    dlg.Destroy()
                    return
                target_path = dlg.GetPath()
                dlg.Destroy()
                base = os.path.basename(target_path)

        mod, cls, params, err = _install_strategy_module(src_path, USER_STRATEGY_DIR, base)
        if err:
            wx.MessageBox(f"策略安装失败: {err}", "错误", wx.OK | wx.ICON_ERROR)
            return

        # 注册到宿主（由面板统一维护 策略映射/参数表/下拉框）
        display_name = f"[用户]{os.path.splitext(base)[0]}"
        self.register_user_strategy(display_name, cls, params or {})

        self._set_load_status(f"已安装策略: {display_name}（{len(params or {})} 个参数）")
        self._refresh_installed_strategies()
        wx.MessageBox(f"已成功安装策略: {display_name}", "完成", wx.OK | wx.ICON_INFORMATION)

    def _refresh_installed_strategies(self):
        if not os.path.isdir(USER_STRATEGY_DIR):
            return
        files = sorted([f for f in os.listdir(USER_STRATEGY_DIR) if f.endswith(".py")
                        and not f.startswith("__")])
        self.installed_list.Set(files)
        # 同步加载所有现存策略（启动时恢复）
        for f in files:
            mod_name = f"qbot.gui.strategies_user.{os.path.splitext(f)[0]}"
            if mod_name in __import__("sys").modules:
                continue
            try:
                _reload_strategy_module(f)
            except Exception as e:
                logger.warning(f"加载用户策略 {f} 失败: {e}")

    def _on_installed_selected(self, event):
        sel = self.installed_list.GetStringSelection()
        if sel:
            self._set_load_status(f"已选中用户策略: {sel}（双击可热重载）")

    def _on_reload_selected(self, event):
        sel = self.installed_list.GetStringSelection()
        if not sel:
            return
        try:
            _reload_strategy_module(sel)
        except Exception as e:
            wx.MessageBox(f"热重载失败: {e}", "错误", wx.OK | wx.ICON_ERROR)
            return
        # 用最新模块替换 STRATEGY_MAP
        mod = __import__("sys").modules.get(f"qbot.gui.strategies_user.{os.path.splitext(sel)[0]}")
        if mod is not None:
            for n in dir(mod):
                v = getattr(mod, n)
                if isinstance(v, type) and getattr(v, "__module__", "") == mod.__name__:
                    for k, existing in list(self.strategy_map.items()):
                        if k.startswith("[用户]") and type(existing).__name__ == n:
                            self.strategy_map[k] = v
        self._set_load_status(f"已热重载: {sel}")

    def _on_remove_selected_strategy(self, event):
        sel = self.installed_list.GetStringSelection()
        if not sel:
            return
        if wx.MessageBox(f"确定删除 {sel}?（仅从磁盘移除，STRATEGY_MAP 中的项需要重启）",
                         "确认", wx.YES_NO | wx.ICON_QUESTION) != wx.ID_YES:
            return
        try:
            os.remove(os.path.join(USER_STRATEGY_DIR, sel))
        except Exception as e:
            wx.MessageBox(f"删除失败: {e}", "错误", wx.OK | wx.ICON_ERROR)
            return
        self._refresh_installed_strategies()
        self._set_load_status(f"已删除: {sel}")

