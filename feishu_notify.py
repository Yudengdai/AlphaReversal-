#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
feishu_notify.py — AlphaReversal 每日信号飞书推送
================================================================
功能：
  1. 读取 output/ 目录下的回测结果
  2. 解析交易记录、权益曲线、市场状态
  3. 组装成结构化的飞书消息（支持 text / interactive 两种模式）
  4. POST 到飞书自定义机器人 webhook

用法：
  python feishu_notify.py                  # 推送完整日报
  python feishu_notify.py --mode brief    # 只推摘要
  python feishu_notify.py --no-image      # 不附带图表

环境变量：
  FEISHU_WEBHOOK  飞书机器人 webhook 地址（必填）
  TUSHARE_TOKEN   tushare API token（策略运行需要）
"""

import os
import sys
import json
import base64
import io
from pathlib import Path
from datetime import datetime, timedelta
import argparse

import pandas as pd
import numpy as np
import requests

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "output"
RESULTS_DIR = ROOT / "results"

# 历史基线（用于对比）
BASELINE = {
    "回测收益":  "+5.94%",
    "最大回撤":  "-3.43%",
    "胜率":      "71.86%",
    "盈利因子":  "1.274",
}


# ─────────────────────────────────────────────
# 数据读取
# ─────────────────────────────────────────────
def find_latest_dir(base_dir: Path, pattern: str = "*") -> Path | None:
    """找到目录下最新的子目录"""
    if not base_dir.exists():
        return None
    dirs = [d for d in base_dir.glob(pattern) if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def load_trades() -> pd.DataFrame | None:
    """加载交易记录"""
    # 优先从 output/ 读
    for path in [OUTPUT_DIR / "trades.csv", RESULTS_DIR / "trades.csv"]:
        if path.exists():
            df = pd.read_csv(path)
            df["buy_date"] = pd.to_datetime(df["buy_date"])
            df["sell_date"] = pd.to_datetime(df["sell_date"])
            return df
    # 搜索 results/日期目录/
    latest = find_latest_dir(RESULTS_DIR)
    if latest:
        p = latest / "trades.csv"
        if p.exists():
            df = pd.read_csv(p)
            df["buy_date"] = pd.to_datetime(df["buy_date"])
            df["sell_date"] = pd.to_datetime(df["sell_date"])
            return df
    return None


def load_equity() -> pd.DataFrame | None:
    """加载每日权益曲线"""
    for path in [OUTPUT_DIR / "daily_equity.csv", RESULTS_DIR / "daily_equity.csv"]:
        if path.exists():
            df = pd.read_csv(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            return df
    latest = find_latest_dir(RESULTS_DIR)
    if latest:
        p = latest / "daily_equity.csv"
        if p.exists():
            df = pd.read_csv(p)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            return df
    return None


def find_latest_image() -> Path | None:
    """找最新的回测图表"""
    candidates = [
        OUTPUT_DIR / "backtest_report.png",
        ROOT / "output" / "backtest_report.png",
    ]
    for c in candidates:
        if c.exists():
            return c
    # 搜索 results 子目录
    latest = find_latest_dir(RESULTS_DIR)
    if latest:
        p = latest / "backtest_report.png"
        if p.exists():
            return p
    return None


# ─────────────────────────────────────────────
# 指标计算
# ─────────────────────────────────────────────
def calc_recent_metrics(trades: pd.DataFrame, equity: pd.DataFrame | None,
                        ref_date: pd.Timestamp) -> dict:
    """计算最近一段时间的策略指标"""
    # 最近 30 个自然日内的平仓交易
    window_start = ref_date - timedelta(days=30)
    recent = trades[(trades["sell_date"] >= window_start) &
                    (trades["sell_date"] <= ref_date)]
    if len(recent) == 0:
        return {"trades_30d": 0}

    wins = recent[recent["pnl"] > 0]
    win_rate = len(wins) / len(recent) * 100
    total_pnl = recent["pnl"].sum()
    avg_pnl_pct = recent["pnl_pct"].mean()

    # 当前持仓（未平仓 = buy_date 在窗口内且无对应 sell）
    open_positions = trades[trades["buy_date"] >= window_start]
    open_positions = open_positions[open_positions["sell_date"] >= ref_date]

    metrics = {
        "trades_30d":     len(recent),
        "win_rate_30d":   win_rate,
        "total_pnl_30d":  total_pnl,
        "avg_pnl_pct_30d": avg_pnl_pct,
        "open_positions": len(open_positions),
    }

    # 从权益曲线算当前回撤
    if equity is not None and "equity" in equity.columns:
        eq = equity[equity["date"] <= ref_date]["equity"]
        if len(eq) > 0:
            peak = eq.cummax()
            drawdown = (eq - peak) / peak * 100
            metrics["current_equity"] = eq.iloc[-1]
            metrics["current_dd_pct"] = drawdown.iloc[-1]
            # 区间收益
            start_eq = eq.iloc[0] if len(eq) > 1 else eq.iloc[0]
            metrics["period_return_pct"] = (eq.iloc[-1] / start_eq - 1) * 100

    return metrics


def get_today_signals(trades: pd.DataFrame, ref_date: pd.Timestamp) -> pd.DataFrame:
    """获取"今日信号"= 今日新开的仓位"""
    today = trades[trades["buy_date"] == ref_date]
    return today


def get_open_positions(trades: pd.DataFrame, ref_date: pd.Timestamp) -> pd.DataFrame:
    """获取当前仍持有的仓位（未平仓）"""
    open_pos = trades[(trades["buy_date"] <= ref_date) &
                      (trades["sell_date"] >= ref_date)]
    return open_pos


# ─────────────────────────────────────────────
# 飞书消息组装
# ─────────────────────────────────────────────
def build_text_message(ref_date: pd.Timestamp, trades: pd.DataFrame,
                       equity: pd.DataFrame | None, metrics: dict,
                       today_signals: pd.DataFrame,
                       open_positions: pd.DataFrame,
                       mode: str = "full") -> str:
    """构建纯文本消息"""
    lines = []
    bar = "━" * 32

    # 标题
    lines.append(f"📊 AlphaReversal 每日报告")
    lines.append(f"📅 {ref_date.strftime('%Y-%m-%d')} (交易日)")
    lines.append(bar)

    # 市场状态（如果有）
    if "market_state" in metrics:
        lines.append(f"🎯 市场状态：{metrics['market_state']}")
        lines.append(bar)

    # 近 30 日绩效
    if metrics.get("trades_30d", 0) > 0:
        lines.append("📈 近 30 日绩效")
        lines.append(f"  交易笔数：{metrics['trades_30d']}")
        lines.append(f"  胜率：{metrics['win_rate_30d']:.1f}%")
        lines.append(f"  总盈亏：{metrics['total_pnl_30d']:+.0f} 元")
        lines.append(f"  平均收益率：{metrics['avg_pnl_pct_30d']:+.2f}%")
        if "current_dd_pct" in metrics:
            dd = metrics["current_dd_pct"]
            dd_icon = "🟢" if dd > -3 else ("🟡" if dd > -6 else "🔴")
            lines.append(f"  当前回撤：{dd_icon} {dd:.2f}%")
        if "period_return_pct" in metrics:
            r = metrics["period_return_pct"]
            lines.append(f"  区间收益：{'🟢' if r >= 0 else '🔴'} {r:+.2f}%")
    else:
        lines.append("📈 近 30 日：无平仓交易")
    lines.append(bar)

    # 今日新信号
    if len(today_signals) > 0:
        lines.append(f"🎯 今日新信号（{len(today_signals)} 只）")
        for _, row in today_signals.head(15).iterrows():
            code = row["code"]
            price = row["buy_price"]
            shares = int(row["shares"])
            lines.append(f"  ▸ {code}  买入价 {price:.2f}  数量 {shares}")
        if len(today_signals) > 15:
            lines.append(f"  ... 还有 {len(today_signals)-15} 只未显示")
    else:
        lines.append("🎯 今日新信号：无（市场状态不满足或择时空仓）")
    lines.append(bar)

    # 当前持仓
    if len(open_positions) > 0:
        lines.append(f"📂 当前持仓（{len(open_positions)} 只）")
        for _, row in open_positions.head(10).iterrows():
            code = row["code"]
            bprice = row["buy_price"]
            bdate = pd.Timestamp(row["buy_date"]).strftime("%m-%d")
            lines.append(f"  ▸ {code}  买入 {bprice:.2f} ({bdate})")
        if len(open_positions) > 10:
            lines.append(f"  ... 还有 {len(open_positions)-10} 只未显示")
    lines.append(bar)

    # 历史基线对比
    lines.append("📌 历史基线（2020-04 ~ 2025-12）")
    lines.append(f"  回测收益：{BASELINE['回测收益']} | 沪深300 +10.22%")
    lines.append(f"  最大回撤：{BASELINE['最大回撤']} | 沪深300 -45.60%")
    lines.append(f"  胜率：{BASELINE['胜率']} | 盈利因子 {BASELINE['盈利因子']}")
    lines.append(bar)

    # 风控提示
    dd = metrics.get("current_dd_pct", 0)
    if dd < -6:
        lines.append("⚠️ 风控提示：当前回撤已超过 -6%，建议降仓观察")
    elif dd < -3:
        lines.append("⚠️ 风控提示：当前回撤 -3%~-6%，处于观察区")
    else:
        lines.append("✅ 风控提示：回撤在安全区间内")

    lines.append("")
    lines.append("⏰ 生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("⚠️ 仅供研究学习，不构成投资建议")

    return "\n".join(lines)


def build_interactive_card(ref_date: pd.Timestamp, trades: pd.DataFrame,
                           equity: pd.DataFrame | None, metrics: dict,
                           today_signals: pd.DataFrame,
                           open_positions: pd.DataFrame) -> dict:
    """构建飞书 interactive 卡片消息（更美观）"""
    # 根据回撤选颜色
    dd = metrics.get("current_dd_pct", 0)
    if dd > -3:
        dd_color = "green"
        dd_emoji = "🟢"
    elif dd > -6:
        dd_color = "orange"
        dd_emoji = "🟡"
    else:
        dd_color = "red"
        dd_emoji = "🔴"

    # 是否有新信号
    has_signal = len(today_signals) > 0

    # 信号列表 → markdown 子元素
    if has_signal:
        signal_items = []
        for _, row in today_signals.head(10).iterrows():
            signal_items.append({
                "type": "div",
                "text": {
                    "type": "lark_md",
                    "content": f"**{row['code']}**  买入价 `{row['buy_price']:.2f}`  数量 `{int(row['shares'])}`"
                }
            })
        if len(today_signals) > 10:
            signal_items.append({
                "type": "div",
                "text": {"type": "lark_md", "content": f"... 还有 **{len(today_signals)-10}** 只未显示"}
            })
    else:
        signal_items = [{
            "type": "div",
            "text": {"type": "lark_md", "content": "今日无新信号（市场状态不满足或择时空仓）"}
        }]

    # 持仓列表
    if len(open_positions) > 0:
        pos_items = []
        for _, row in open_positions.head(8).iterrows():
            bdate = pd.Timestamp(row["buy_date"]).strftime("%m-%d")
            pos_items.append({
                "type": "div",
                "text": {
                    "type": "lark_md",
                    "content": f"**{row['code']}**  买入 `{row['buy_price']:.2f}` ({bdate})"
                }
            })
        if len(open_positions) > 8:
            pos_items.append({
                "type": "div",
                "text": {"type": "lark_md", "content": f"... 还有 **{len(open_positions)-8}** 只"}
            })
    else:
        pos_items = [{
            "type": "div",
            "text": {"type": "lark_md", "content": "当前无持仓"}
        }]

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "type": "plain_text",
                    "content": f"📊 AlphaReversal 日报 · {ref_date.strftime('%Y-%m-%d')}"
                },
                "template": "blue"
            },
            "elements": [
                # ── 近 30 日绩效 ──
                {
                    "type": "div",
                    "text": {"type": "lark_md", "content": "**📈 近 30 日绩效**"}
                },
                {
                    "type": "action",
                    "actions": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "content": f"交易 {metrics.get('trades_30d', 0)} 笔"},
                            "url": "https://github.com/CANGLIN123/AlphaReversal-"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text",
                                     "content": f"胜率 {metrics.get('win_rate_30d', 0):.0f}%"},
                            "url": "https://github.com/CANGLIN123/AlphaReversal-"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text",
                                     "content": f"盈亏 {metrics.get('total_pnl_30d', 0):+.0f}"},
                            "url": "https://github.com/CANGLIN123/AlphaReversal-"
                        },
                    ]
                },
                {"type": "hr"},

                # ── 回撤 & 区间收益 ──
                {
                    "type": "div",
                    "text": {
                        "type": "lark_md",
                        "content": (f"**{dd_emoji} 当前回撤** `{dd:.2f}%`    "
                                   f"**区间收益** `{metrics.get('period_return_pct', 0):+.2f}%`")
                    }
                },
                {"type": "hr"},

                # ── 今日信号 ──
                {
                    "type": "div",
                    "text": {"type": "lark_md",
                             "content": f"**🎯 今日新信号（{len(today_signals)} 只）**"}
                },
                *signal_items,
                {"type": "hr"},

                # ── 当前持仓 ──
                {
                    "type": "div",
                    "text": {"type": "lark_md",
                             "content": f"**📂 当前持仓（{len(open_positions)} 只）**"}
                },
                *pos_items,
                {"type": "hr"},

                # ── 历史基线 ──
                {
                    "type": "div",
                    "text": {
                        "type": "lark_md",
                        "content": ("**📌 历史基线（2020-04~2025-12）**\n"
                                   f"收益 `{BASELINE['回测收益']}` | 回撤 `{BASELINE['最大回撤']}` | "
                                   f"胜率 `{BASELINE['胜率']}` | 盈利因子 `{BASELINE['盈利因子']}`")
                    }
                },
                {"type": "hr"},

                # ── 风控提示 ──
                {
                    "type": "note",
                    "elements": [{
                        "type": "plain_text",
                        "content": ("⚠️ 仅供研究学习，不构成投资建议 | "
                                    f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                    }]
                }
            ]
        }
    }
    return card


# ─────────────────────────────────────────────
# 推送
# ─────────────────────────────────────────────
def push_text(webhook: str, content: str) -> bool:
    """推送 text 消息"""
    payload = {"msg_type": "text", "content": {"text": content}}
    try:
        resp = requests.post(webhook, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ 飞书 text 推送成功")
            return True
        else:
            print(f"⚠️ 飞书推送返回异常: {result}")
            return False
    except Exception as e:
        print(f"❌ 飞书推送失败: {e}")
        return False


def push_interactive(webhook: str, card: dict) -> bool:
    """推送 interactive 卡片"""
    try:
        resp = requests.post(webhook, json=card, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ 飞书卡片推送成功")
            return True
        else:
            print(f"⚠️ 飞书卡片返回异常: {result}")
            return False
    except Exception as e:
        print(f"❌ 飞书卡片推送失败: {e}")
        return False


def push_image(webhook: str, image_path: Path) -> bool:
    """推送图片（将图片作为 file 类型发送）"""
    try:
        # 飞书机器人发送图片需要先上传到飞书获取 image_key
        # 简单方案：用 base64 编码后通过 rich_text 发送
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        # 注意：自定义机器人 webhook 通常不支持直接发图
        # 这里用 text 消息附带图片链接（如果图片有公网 URL）
        # 或者降级为不上传图片
        print(f"ℹ️ 图片大小: {len(img_b64)//1024}KB（webhook 模式不支持直接发图，已跳过）")
        return True
    except Exception as e:
        print(f"⚠️ 图片处理失败: {e}")
        return False


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AlphaReversal 飞书推送")
    parser.add_argument("--mode", choices=["full", "brief"], default="full",
                        help="full=完整卡片, brief=纯文本摘要")
    parser.add_argument("--no-image", action="store_true", help="不处理图表")
    args = parser.parse_args()

    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("❌ 未设置 FEISHU_WEBHOOK 环境变量")
        print("   请在 GitHub Secrets 中配置，或本地 export FEISHU_WEBHOOK=...")
        sys.exit(1)

    # 1. 加载数据
    print("[1/4] 加载交易记录...")
    trades = load_trades()
    if trades is None or len(trades) == 0:
        print("⚠️ 未找到交易记录，请先运行 run.py 生成结果")
        # 仍然推送一条提示消息
        push_text(webhook, "⚠️ AlphaReversal 今日未产生交易记录\n请检查策略是否正常运行")
        sys.exit(0)
    print(f"   加载 {len(trades)} 条交易记录")

    print("[2/4] 加载权益曲线...")
    equity = load_equity()
    if equity is not None:
        print(f"   加载 {len(equity)} 条权益数据")
    else:
        print("   未找到权益曲线文件")

    # 2. 确定参考日期（最后一个交易日）
    ref_date = trades["sell_date"].max()
    # 如果当天数据还没生成，用最新 buy_date
    latest_buy = trades["buy_date"].max()
    ref_date = max(ref_date, latest_buy)
    print(f"   参考日期: {ref_date.strftime('%Y-%m-%d')}")

    # 3. 计算指标
    print("[3/4] 计算指标...")
    metrics = calc_recent_metrics(trades, equity, ref_date)
    today_signals = get_today_signals(trades, ref_date)
    open_positions = get_open_positions(trades, ref_date)
    print(f"   近30日交易 {metrics.get('trades_30d', 0)} 笔, "
          f"胜率 {metrics.get('win_rate_30d', 0):.0f}%, "
          f"今日信号 {len(today_signals)} 只, "
          f"持仓 {len(open_positions)} 只")

    # 4. 推送
    print("[4/4] 推送到飞书...")
    if args.mode == "brief":
        text = build_text_message(ref_date, trades, equity, metrics,
                                 today_signals, open_positions, mode="brief")
        ok = push_text(webhook, text)
    else:
        card = build_interactive_card(ref_date, trades, equity, metrics,
                                      today_signals, open_positions)
        ok = push_interactive(webhook, card)

    # 图片（可选）
    if not args.no_image:
        img_path = find_latest_image()
        if img_path:
            push_image(webhook, img_path)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
