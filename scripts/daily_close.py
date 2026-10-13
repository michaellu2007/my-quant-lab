"""收盘后跑一遍持仓决策清单（无 GUI，可挂 Windows 任务计划）。

用法：
    venv39\\Scripts\\python.exe E:\\QUANT\\scripts\\daily_close.py

产出（写到 packs/Qbot/qbot/gui/bkt_result/）：
    signal_report.txt   持仓明细 + 仓位占比 + 卖出条件 + 技术状态
    event_report.txt    持仓新闻/财报事件（需要联网，失败则跳过该段）

刻意不碰 wx：面板的 _start_busy/_set_load_status 都是 GUI 状态，这里复用
qbot.core.holding_advice 和 panel_positions.evaluate_long_signal 这些纯函数，
所以定时任务不需要开窗口。原 GUI 面板不受影响。
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACK = os.path.join(ROOT, "packs", "Qbot")
sys.path.insert(0, PACK)

from qbot.core.holding_advice import (pe_percentile, valuation_band,
                                      position_weights, concentration_warning,
                                      render_sell_condition,
                                      break_even_pct, drawdown_from_high,
                                      check_alert, append_history_row)
from qbot.gui.panels.panel_positions import (evaluate_long_signal, POSITIONS_FILE,
                                             HISTORY_FILE, load_portfolio)
from qbot.core.market_rules import is_hk

OUT_DIR = os.path.join(PACK, "qbot", "gui", "bkt_result")
# 持仓成本价没有复权口径时，用成本价兜底算浮盈；此处只做展示，不做记账
MARKET_WINDOW_DAYS = 900


def load_positions():
    """读持仓 + 面板填写的总资产。兼容旧裸 list schema（由 load_portfolio 处理）。"""
    try:
        positions, total = load_portfolio()
        return positions, total
    except Exception as e:
        print(f"[warn] 读不到持仓 {POSITIONS_FILE}: {e}")
        return [], None


def fetch_closes(pos):
    """取近一年日线，失败返回 None。优先用本地缓存（离线可用）。"""
    from qbot.core import market_cache
    code = str(pos["code"])
    digits = "".join(c for c in code if c.isdigit())[-5:].zfill(5)
    df = None
    if is_hk(code):
        df = market_cache.load_cached("ak", f"港股_{digits}_日线_前复权")
    else:
        norm = code if "." in code else (
            f"sh.{code}" if code[:1] in "69" else f"sz.{code}")
        df = market_cache.load_bs_cached(norm)
    if df is None or df.empty or "close" not in df.columns:
        return None
    return df["close"].dropna()


def main():
    positions, portfolio_total = load_positions()
    if not positions:
        print("持仓为空，无事可做。")
        return
    os.makedirs(OUT_DIR, exist_ok=True)

    # 现价：优先用面板上次刷新缓存的 last，没有就用本地行情缓存最新收盘
    for p in positions:
        closes = fetch_closes(p)
        if closes is not None and len(closes):
            p["last"] = float(closes.iloc[-1])
            p["_has_signal"] = len(closes) >= 200
            p["hi52"] = float(closes.tail(250).max())   # 52周高点，供回撤列
            if closes is not None:
                try:
                    st, lv, why = evaluate_long_signal(closes)
                    p["_state"], p["_why"] = st, why
                except Exception:
                    p["_state"], p["_why"] = "⚠", "信号评估失败"
        else:
            p["_has_signal"] = False

    # 估值分位：联网且有缓存才填，取不到保持 None
    try:
        from qbot.core import valuation_cache
        for p in positions:
            df = valuation_cache.fetch_pe_history(p["code"])
            if df is None or "pe" not in df.columns or len(df) < 2:
                p["pe_pctl"] = None
                continue
            cur = float(df["pe"].iloc[-1])
            p["pe_pctl"] = pe_percentile(df["pe"].iloc[:-1], cur)
    except Exception as e:
        print(f"[warn] 估值分位跳过: {e}")

    # 总资产：面板里持久化过的就用它；没填则占比退化为持仓内部相对权重
    total = portfolio_total
    weights = position_weights(positions, total)
    # 没填总资产时占比只是持仓内部相对权重，不能据此报警——否则单只持仓恒为
    # 100%，会误报成「超限」。只有填了总资产，占比才代表真实仓位。
    over = concentration_warning(weights, limit=20.0) if total else []

    path = os.path.join(OUT_DIR, "signal_report.txt")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"长线持仓信号清单  {now}\n")
        if total:
            f.write(f"总资产（面板填写）：{total:,.0f}\n")
        else:
            f.write("总资产未填写：下表「占比」为持仓内部相对权重，非绝对仓位\n")
        if over:
            f.write("⚠ 单一持仓超 20% 上限：" +
                    "、".join(f"{c} {v:.1f}%" for c, v in over) + "\n")
        f.write("=" * 60 + "\n")
        alerts_hit = []
        for p in positions:
            last = p.get("last")
            pct = weights.get(p["code"])
            band, _ = valuation_band(p.get("pe_pctl"))
            pnl = ((last / p["cost"] - 1) * 100) if (last and p["cost"]) else None
            # 警示价：定时任务里最重要的检查——触发就置顶标出
            hit = check_alert(last, p.get("alert"))
            if hit:
                alerts_hit.append(p)
                p["_state"], p["level"] = "🚨", 2
                p["_why"] = f"触发警示价{p['alert']}（现价{last:.2f}）"
            line = (f"{p.get('_state', '⚠')} {p['code']} {p.get('name', '')}  "
                    f"现价{last:.2f}" if last else
                    f"{p.get('_state', '⚠')} {p['code']} {p.get('name', '')}  现价--")
            f.write(line + f"  {p.get('_why', '行情不足200日，跳过技术信号')}\n")
            head = f"  {p['code']} {p['shares']}股  成本{p['cost']:.2f}"
            if last:
                head += f"  现价{last:.2f}"
            f.write(head)
            f.write(f"  盈亏{'%+.1f%%' % pnl if pnl is not None else '--'}")
            f.write(f"  占比{'%.1f%%' % pct if pct is not None else '--'}")
            if p.get("pe_pctl") is not None:
                f.write(f"  估值分位{band}({p['pe_pctl']*100:.0f}%)")
            be = break_even_pct(p.get("cost"), last)
            dd = drawdown_from_high(last, p.get("hi52"))
            if be is not None and be > 0:
                f.write(f"  回本需涨+{be*100:.0f}%")
            if dd is not None:
                f.write(f"  距52周高{dd*100:.0f}%")
            if p.get("alert"):
                mark = "  🚨已触发" if hit else ""
                f.write(f"  警示价{p['alert']}{mark}")
            f.write(f"\n    卖出条件：{render_sell_condition(p.get('sell'))}\n")
        if alerts_hit:
            f.write("\n🚨 今日触发警示价：" +
                    "、".join(f"{p['code']}(现价{p.get('last'):.2f} ≤ {p['alert']})"
                              for p in alerts_hit) + "\n")
    print(f"已写出: {path}")

    # 决策日记：追加到 signal_history.csv（与面板共用同一份历史）
    try:
        total_mv = sum((p.get("last") or 0) * p.get("shares", 0) for p in positions)
        t = datetime.datetime.now()
        for p in positions:
            last = p.get("last")
            append_history_row(HISTORY_FILE, {
                "date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M"),
                "code": p["code"], "name": p.get("name", ""),
                "close": last, "cost": p.get("cost"), "shares": p.get("shares"),
                "pnl_pct": (round((last / p["cost"] - 1) * 100, 2)
                            if (last and p.get("cost")) else None),
                "weight_pct": (round(weights.get(p["code"], 0.0), 2) if total else None),
                "level": p.get("level", 1), "state": p.get("_state", "⚠"),
                "pe_band": valuation_band(p.get("pe_pctl"))[0],
                "alert": p.get("alert"), "alert_hit": check_alert(last, p.get("alert")),
                "reason": p.get("_why", ""),
                "total_assets": total, "total_mv": round(total_mv, 2),
            })
        print(f"决策日记已追加: {HISTORY_FILE}")
    except Exception as e:
        print(f"[warn] 决策日记写入失败: {e}")

    # 事件扫描：纯网络功能，失败不阻断主流程
    try:
        from qbot.engine.news_events import scan_events
        rows = []
        for p in positions:
            try:
                rows.extend(scan_events(p["code"]))
            except Exception:
                continue
        if rows:
            epath = os.path.join(OUT_DIR, "event_report.txt")
            with open(epath, "w", encoding="utf-8") as f:
                f.write(f"持仓新闻/财报事件扫描  {now}\n" + "=" * 70 + "\n")
                for r in rows:
                    f.write(f"[{r['sentiment']}] {r['type']} {r['code']} "
                            f"{r['time']}  {r['title']}\n")
            print(f"已写出: {epath}")
    except Exception as e:
        print(f"[warn] 事件扫描跳过: {e}")


if __name__ == "__main__":
    main()