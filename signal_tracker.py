#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
signal_tracker.py — 样本外追踪日志
================================================================
功能：
  维护一份 signal_log.csv，记录每日信号及未来 1/3/5/10 日收益
  用于长期追踪策略的样本外真实表现

用法：
  python signal_tracker.py update   # 更新日志（每天跑完策略后调用）
  python signal_tracker.py report  # 生成绩效报告
  python signal_tracker.py check   # 检查今日信号状态
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
import argparse

import pandas as pd
import numpy as np
import requests

ROOT = Path(__file__).parent
LOG_FILE = ROOT / "signal_log.csv"
DATA_CACHE = ROOT / "data_cache"
RESULTS_DIR = ROOT / "results"
OUTPUT_DIR = ROOT / "output"

# 需要 tushare 获取最新价格来验证信号
try:
    import tushare as ts
    TUSHARE_AVAILABLE = True
except ImportError:
    TUSHARE_AVAILABLE = False


def load_trades() -> pd.DataFrame | None:
    """加载最新交易记录"""
    for path in [OUTPUT_DIR / "trades.csv", RESULTS_DIR / "trades.csv"]:
        if path.exists():
            df = pd.read_csv(path)
            df["buy_date"] = pd.to_datetime(df["buy_date"])
            df["sell_date"] = pd.to_datetime(df["sell_date"])
            return df
    # 搜索 results 子目录
    for d in sorted(RESULTS_DIR.glob("*")) if RESULTS_DIR.exists() else []:
        if d.is_dir():
            p = d / "trades.csv"
            if p.exists():
                df = pd.read_csv(p)
                df["buy_date"] = pd.to_datetime(df["buy_date"])
                df["sell_date"] = pd.to_datetime(df["sell_date"])
                return df
    return None


def get_latest_signal_date(trades: pd.DataFrame) -> pd.Timestamp:
    """获取最新信号日期"""
    return trades["buy_date"].max()


def fetch_recent_prices(codes: list[str], start: str, end: str) -> pd.DataFrame:
    """用 tushare 获取最新价格"""
    if not TUSHARE_AVAILABLE:
        print("⚠️ tushare 未安装，无法获取最新价格")
        return pd.DataFrame()

    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        ts.set_token(token)
    else:
        # 尝试从 config 读
        try:
            import config
            ts.set_token(config.TUSHARE_TOKEN)
        except:
            print("⚠️ 未找到 TUSHARE_TOKEN")
            return pd.DataFrame()

    pro = ts.pro_api()
    all_data = []
    for code in codes:
        try:
            df = pro.daily(ts_code=code, start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                all_data.append(df[["ts_code", "trade_date", "close"]])
        except Exception as e:
            print(f"  ⚠️ {code} 获取失败: {e}")

    if all_data:
        return pd.concat(all_data, ignore_index=True)
    return pd.DataFrame()


def update_log():
    """更新 signal_log.csv"""
    print("📝 更新信号日志...")
    trades = load_trades()
    if trades is None or len(trades) == 0:
        print("⚠️ 未找到交易记录")
        return

    latest_date = get_latest_signal_date(trades)
    print(f"   最新信号日期: {latest_date.strftime('%Y-%m-%d')}")

    # 获取最近 30 天买入的信号
    cutoff = latest_date - timedelta(days=30)
    recent = trades[trades["buy_date"] >= cutoff].copy()

    if len(recent) == 0:
        print("   近 30 天无新信号")
        return

    # 用 tushare 获取最新价格，计算未平仓收益
    open_pos = recent[recent["sell_date"] >= latest_date]
    if len(open_pos) > 0 and TUSHARE_AVAILABLE:
        codes = open_pos["code"].unique().tolist()
        start_str = cutoff.strftime("%Y%m%d")
        end_str = datetime.now().strftime("%Y%m%d")
        print(f"   📡 获取 {len(codes)} 只股票最新价格...")
        prices = fetch_recent_prices(codes, start_str, end_str)

        if len(prices) > 0:
            # 计算每只股票的最新价
            latest_prices = prices.groupby("ts_code")["close"].last().reset_index()
            latest_prices.columns = ["code", "current_price"]

            open_pos = open_pos.merge(latest_prices, on="code", how="left")
            open_pos["unrealized_pnl_pct"] = (
                (open_pos["current_price"] - open_pos["buy_price"])
                / open_pos["buy_price"] * 100
            )

            print(f"\n   📊 未平仓持仓当前浮盈亏:")
            for _, row in open_pos.iterrows():
                pct = row.get("unrealized_pnl_pct", 0)
                emoji = "🟢" if pct >= 0 else "🔴"
                print(f"      {emoji} {row['code']}  买入 {row['buy_price']:.2f}  "
                      f"当前 {row.get('current_price', 0):.2f}  ({pct:+.2f}%)")

    # 追加到日志
    log_exists = LOG_FILE.exists()
    if not log_exists:
        log_df = pd.DataFrame(columns=[
            "signal_date", "code", "buy_price", "sell_date",
            "sell_price", "pnl", "pnl_pct", "reason"
        ])
    else:
        log_df = pd.read_csv(LOG_FILE)
        log_df["signal_date"] = pd.to_datetime(log_df["signal_date"])
        log_df["sell_date"] = pd.to_datetime(log_df["sell_date"])

    # 去重：只添加日志中不存在的记录
    new_records = recent.copy()
    new_records["signal_date"] = new_records["buy_date"]
    new_records = new_records.rename(columns={"buy_date": "signal_date_temp"})
    # 用 signal_date + code 做去重键
    dedup_key = ["signal_date", "code"]
    existing_keys = set(zip(log_df["signal_date"].dt.strftime("%Y-%m-%d"),
                           log_df["code"])) if log_exists else set()

    to_add = []
    for _, row in new_records.iterrows():
        key = (row["signal_date"].strftime("%Y-%m-%d"), row["code"])
        if key not in existing_keys:
            to_add.append({
                "signal_date": row["signal_date"].strftime("%Y-%m-%d"),
                "code": row["code"],
                "buy_price": row["buy_price"],
                "sell_date": row["sell_date"].strftime("%Y-%m-%d") if pd.notna(row["sell_date"]) else "",
                "sell_price": row.get("sell_price", 0),
                "pnl": row.get("pnl", 0),
                "pnl_pct": row.get("pnl_pct", 0),
                "reason": row.get("reason", ""),
            })

    if to_add:
        add_df = pd.DataFrame(to_add)
        log_df = pd.concat([log_df, add_df], ignore_index=True)
        log_df.to_csv(LOG_FILE, index=False)
        print(f"\n   ✅ 新增 {len(to_add)} 条记录到 signal_log.csv")
    else:
        print(f"\n   ℹ️ 无新增记录（已存在 {len(log_df)} 条）")

    print(f"   📄 日志文件: {LOG_FILE}")


def generate_report():
    """生成绩效报告"""
    if not LOG_FILE.exists():
        print("⚠️ signal_log.csv 不存在，请先运行 update")
        return

    df = pd.read_csv(LOG_FILE)
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["sell_date"] = pd.to_datetime(df["sell_date"], errors="coerce")

    print("=" * 55)
    print("  AlphaReversal 样本外追踪报告")
    print(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 55)

    # 总体统计
    total = len(df)
    closed = df[df["sell_date"].notna() & (df["pnl"] != 0)]
    wins = closed[closed["pnl"] > 0]
    win_rate = len(wins) / len(closed) * 100 if len(closed) > 0 else 0
    total_pnl = closed["pnl"].sum()
    avg_pnl_pct = closed["pnl_pct"].mean() if len(closed) > 0 else 0

    print(f"\n📊 总体表现")
    print(f"   总信号数: {total}")
    print(f"   已平仓:   {len(closed)}")
    print(f"   胜率:     {win_rate:.1f}%")
    print(f"   总盈亏:   {total_pnl:+.0f}")
    print(f"   平均收益: {avg_pnl_pct:+.2f}%")

    # 近 30 天
    cutoff = pd.Timestamp.now() - timedelta(days=30)
    recent = df[df["signal_date"] >= cutoff]
    recent_closed = recent[recent["sell_date"].notna() & (recent["pnl"] != 0)]
    if len(recent_closed) > 0:
        rw = len(recent_closed[recent_closed["pnl"] > 0]) / len(recent_closed) * 100
        rp = recent_closed["pnl"].sum()
        print(f"\n📈 近 30 天")
        print(f"   信号数: {len(recent)}")
        print(f"   已平仓: {len(recent_closed)}")
        print(f"   胜率:   {rw:.1f}%")
        print(f"   盈亏:   {rp:+.0f}")

    # 对比历史基线
    print(f"\n📌 历史基线对比")
    print(f"   基线胜率: 71.86%  |  当前: {win_rate:.1f}%  "
          f"{'✅' if win_rate >= 65 else '⚠️'}")
    print(f"   基线回撤: -3.43%  |  需持续监控")

    # 止损原因分析
    if len(closed) > 0:
        print(f"\n🔍 平仓原因分布")
        reasons = closed["reason"].value_counts()
        for reason, count in reasons.head(5).items():
            print(f"   {reason}: {count} ({count/len(closed)*100:.0f}%)")

    print(f"\n{'=' * 55}")
    print("⚠️ 仅供研究学习，不构成投资建议")


def check_status():
    """检查今日信号状态"""
    trades = load_trades()
    if trades is None:
        print("⚠️ 未找到交易记录")
        return

    latest = get_latest_signal_date(trades)
    today = pd.Timestamp.now().normalize()
    days_diff = (today - latest).days

    print(f"📅 最新信号日期: {latest.strftime('%Y-%m-%d')}")
    print(f"   距今天数: {days_diff}")

    if days_diff <= 1:
        today_signals = trades[trades["buy_date"] == latest]
        print(f"   今日信号: {len(today_signals)} 只")
        for _, row in today_signals.iterrows():
            print(f"      ▸ {row['code']}  买入价 {row['buy_price']:.2f}")
    elif days_diff <= 3:
        print("   ⚠️ 信号略有延迟，检查数据源是否正常")
    else:
        print("   🔴 信号严重滞后，请检查策略运行状态")

    # 检查日志文件
    if LOG_FILE.exists():
        log = pd.read_csv(LOG_FILE)
        print(f"\n📄 信号日志: {len(log)} 条记录")
    else:
        print(f"\n📄 信号日志: 尚未创建")


def main():
    parser = argparse.ArgumentParser(description="AlphaReversal 信号追踪器")
    parser.add_argument("action", choices=["update", "report", "check"],
                        help="update=更新日志, report=生成报告, check=检查状态")
    args = parser.parse_args()

    if args.action == "update":
        update_log()
    elif args.action == "report":
        generate_report()
    elif args.action == "check":
        check_status()


if __name__ == "__main__":
    main()
