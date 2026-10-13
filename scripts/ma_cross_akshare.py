# -*- coding: utf-8 -*-
import backtrader as bt
import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime

class MA_Cross(bt.Strategy):
    """双均线交叉策略"""
    
    params = (
        ('pfast', 5),   # 快线周期
        ('pslow', 10),  # 慢线周期
    )

    def log(self, txt, dt=None):
        """日志函数"""
        dt = dt or self.datas[0].datetime.date(0)
        print(f"{dt.isoformat()} {txt}")

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.volume = self.datas[0].volume
        self.order = None
        self.buyprice = None
        self.buycomm = None

        # 计算均线
        self.sma_fast = bt.ind.SMA(period=self.p.pfast)
        self.sma_slow = bt.ind.SMA(period=self.p.pslow)
        self.crossover = bt.ind.CrossOver(self.sma_fast, self.sma_slow)

    def next(self):
        """每天执行"""
        self.log(f"收盘价: {self.dataclose[0]:.2f}")

        if self.order:
            return

        # 空仓 - 寻找买入机会
        if not self.position:
            if self.crossover > 0:  # 金叉
                self.log(f"买入信号出现 - 价格: {self.dataclose[0]:.2f}")
                # 用95%资金买入
                size = int(0.95 * self.broker.cash / self.dataclose[0])
                self.order = self.buy(size=size)

        # 持仓 - 寻找卖出机会
        else:
            if self.crossover < 0:  # 死叉
                self.log(f"卖出信号出现 - 价格: {self.dataclose[0]:.2f}")
                self.order = self.sell(size=self.position.size)

    def notify_order(self, order):
        """订单状态通知"""
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f"买入执行 - 价格: {order.executed.price:.2f}, "
                        f"数量: {order.executed.size}")
            else:
                self.log(f"卖出执行 - 价格: {order.executed.price:.2f}, "
                        f"数量: {order.executed.size}")

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log("订单取消/保证金不足/拒绝")

        self.order = None

    def notify_trade(self, trade):
        """交易完成通知"""
        if not trade.isclosed:
            return
        self.log(f"交易利润 - 毛利润: {trade.pnl:.2f}, 净利润: {trade.pnlcomm:.2f}")


def get_data_akshare(code, start_date, end_date):
    """
    用 AKShare 获取 A 股数据
    code: 股票代码（如 '000333'）
    """
    print(f"正在下载 {code} 数据...")
    
    # 格式化日期（AKShare 需要 YYYYMMDD 格式）
    start = start_date.replace('-', '')
    end = end_date.replace('-', '')
    
    # 获取数据
    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start,
        end_date=end,
        adjust="qfq"  # 前复权
    )
    
    if df.empty:
        raise ValueError(f"未获取到 {code} 的数据")
    
    print(f"获取到 {len(df)} 条数据")
    
    # 重命名列
    df.rename(columns={
        '日期': 'date',
        '开盘': 'open',
        '收盘': 'close',
        '最高': 'high',
        '最低': 'low',
        '成交量': 'volume'
    }, inplace=True)
    
    # 处理日期
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    
    # 确保数据类型
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])
    
    # backtrader 需要的字段
    df['openinterest'] = 0
    
    return df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]


def get_index_data_akshare(symbol="sh000001", start_date="20240101", end_date="20241231"):
    """
    获取指数数据
    symbol: 指数代码（sh000001 上证指数，sh000300 沪深300）
    """
    df = ak.stock_zh_index_daily(symbol=symbol)
    
    # 根据日期筛选
    df['date'] = pd.to_datetime(df['date'])
    df.set_index('date', inplace=True)
    df = df[start_date:end_date]
    
    # 重命名列以适配 backtrader
    df.rename(columns={
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'volume': 'volume'
    }, inplace=True)
    
    df['openinterest'] = 0
    return df[['open', 'high', 'low', 'close', 'volume', 'openinterest']]


if __name__ == "__main__":
    
    # ========== 1. 设置回测参数 ==========
    code = "000333"           # 美的集团
    start_date = "2024-01-01"
    end_date = "2024-12-31"
    initial_cash = 10000
    
    print("="*50)
    print("回测配置")
    print(f"股票代码: {code}")
    print(f"回测区间: {start_date} 至 {end_date}")
    print(f"初始资金: {initial_cash}")
    print("="*50)
    
    # ========== 2. 获取数据 ==========
    try:
        dataframe = get_data_akshare(code, start_date, end_date)
        print(f"数据预览:")
        print(dataframe.head())
        print(f"数据区间: {dataframe.index[0]} 至 {dataframe.index[-1]}")
    except Exception as e:
        print(f"数据获取失败: {e}")
        exit(1)
    
    # ========== 3. 设置回测引擎 ==========
    cerebro = bt.Cerebro()
    
    # 添加数据
    data = bt.feeds.PandasData(dataname=dataframe)
    cerebro.adddata(data)
    
    # 添加策略
    cerebro.addstrategy(MA_Cross)
    
    # 设置资金
    cerebro.broker.setcash(initial_cash)
    
    # 设置手续费（万五）
    cerebro.broker.setcommission(commission=0.0005)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    
    print(f"\n期初总资产: {cerebro.broker.getvalue():.2f}")
    
    # ========== 4. 运行回测 ==========
    print("\n开始回测...")
    results = cerebro.run()
    strat = results[0]
    
    # ========== 5. 输出结果 ==========
    final_value = cerebro.broker.getvalue()
    print(f"\n期末总资产: {final_value:.2f}")
    print(f"总收益率: {(final_value/initial_cash - 1)*100:.2f}%")
    
    # 获取分析结果
    print("\n=== 绩效指标 ===")
    try:
        sharpe = strat.analyzers.sharpe.get_analysis()
        print(f"夏普比率: {sharpe.get('sharperatio', 'N/A')}")
    except:
        print("夏普比率: 计算失败")
    
    try:
        returns = strat.analyzers.returns.get_analysis()
        print(f"年化收益率: {returns.get('rnorm100', 'N/A')}%")
    except:
        print("年化收益率: 计算失败")
    
    try:
        drawdown = strat.analyzers.drawdown.get_analysis()
        print(f"最大回撤: {drawdown.get('max', {}).get('drawdown', 'N/A')}%")
    except:
        print("最大回撤: 计算失败")
    
    # ========== 6. 画图 ==========
    print("\n正在生成图表...")
    cerebro.plot(style='candlestick')
    
    print("\n回测完成！")