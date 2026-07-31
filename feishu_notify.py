#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
feishu_notify.py — AlphaReversal 飞书推送
================================================================
功能：
  读取 output/ 目录下真实回测结果（trades.csv / daily_equity.csv）
  派生当日信号与持仓，计算绩效指标，组装飞书卡片推送
  （兼容旧版手写 results/signals.csv / results/positions.csv）

用法：
  python feishu_notify.py           # 推送卡片（默认）
  python feishu_notify.py --text   # 推送纯文本

环境变量：
  FEISHU_WEBHOOK  飞书机器人 webhook 地址（必填）
"""

import os
import sys
import argparse
from datetime import datetime
from typing import List, Dict, Optional

import requests
import pandas as pd
import numpy as np

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK", "")

# 历史基线（用于对比）
BASELINE = {
    "return":        "5.94",
    "dd":            "3.43",
    "win_rate":      "71.86",
    "profit_factor": "1.274",
}


# ─────────────────────────────────────────────
# 推送函数
# ─────────────────────────────────────────────
def push_text(text: str) -> bool:
    """发送纯文本消息"""
    if not WEBHOOK_URL:
        print("⚠️ 未配置 FEISHU_WEBHOOK")
        return False
    payload = {
        "msg_type": "text",
        "content": {"text": text}
    }
    return _do_push(payload)


def push_card(card: dict) -> bool:
    """发送 interactive 卡片消息"""
    if not WEBHOOK_URL:
        print("⚠️ 未配置 FEISHU_WEBHOOK")
        return False
    # 注意：interactive 类型必须用 "card" 字段，不能用 "content"
    payload = {
        "msg_type": "interactive",
        "card": card
    }
    return _do_push(payload)


def _do_push(payload: dict) -> bool:
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print("✅ 飞书推送成功")
            return True
        else:
            print(f"⚠️ 飞书推送返回异常: {result}")
            return False
    except Exception as e:
        print(f"❌ 飞书推送异常: {e}")
        return False


# ─────────────────────────────────────────────
# 卡片构建（已验证的飞书规范结构）
# ─────────────────────────────────────────────
def build_card(
    date_str: str,
    signals: List[Dict],
    positions: List[Dict],
    metrics: Dict
) -> dict:
    """
    构建符合飞书规范的 interactive 卡片
    """
    # 回撤颜色判断
    risk_note = "✅ 回撤在安全区间内"
    dd_raw = str(metrics.get("drawdown", "-"))
    try:
        dd_val = float(dd_raw.replace("%", "").replace("−", "-"))
        if dd_val <= -6:
            risk_note = "🔴 回撤超 -6%，建议降仓"
        elif dd_val <= -3:
            risk_note = "⚠️ 回撤接近警戒线"
    except:
        pass

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📊 AlphaReversal 日报 · {date_str}"
            },
            "template": "blue"
        },
        "elements": []
    }

    # ── 绩效概览 ──
    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"**📈 近30日绩效**\n"
                f"交易 {metrics.get('trade_count', '?')} 笔 | "
                f"胜率 {metrics.get('win_rate', '?')}% | "
                f"盈亏 {metrics.get('profit', '?')}"
            )
        }
    })

    # ── 回撤与区间收益 ──
    dd_val = metrics.get('drawdown', '?')
    ret_val = metrics.get('interval_return', '?')
    # 避免双百分号：值已含 % 就不再追加
    dd_str = dd_val if '%' in str(dd_val) else f"{dd_val}%"
    ret_str = ret_val if '%' in str(ret_val) else f"{ret_val}%"
    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"🟢 当前回撤 {dd_str}  "
                f"区间收益 {ret_str}"
            )
        }
    })

    # ── 分割线 ──
    card["elements"].append({"tag": "hr"})

    # ── 今日新信号 ──
    if signals:
        lines = [f"**🎯 今日新信号（{len(signals)} 只）**"]
        for s in signals[:10]:
            lines.append(
                f"▸ {s.get('code','?')}  "
                f"买入价 {s.get('price','?')}  "
                f"数量 {s.get('qty','?')}"
            )
        if len(signals) > 10:
            lines.append(f"... 还有 {len(signals)-10} 只未显示")
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)}
        })
    else:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**🎯 今日无新信号**"}
        })

    # ── 当前持仓 ──
    if positions:
        lines = [f"**📂 当前持仓（{len(positions)} 只）**"]
        for p in positions[:10]:
            lines.append(
                f"▸ {p.get('code','?')}  "
                f"买入 {p.get('buy_price','?')} "
                f"({p.get('buy_date','?')})"
            )
        if len(positions) > 10:
            lines.append(f"... 还有 {len(positions)-10} 只未显示")
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)}
        })
    else:
        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**📂 当前无持仓**"}
        })

    # ── 分割线 ──
    card["elements"].append({"tag": "hr"})

    # ── 历史基线 ──
    card["elements"].append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": (
                f"📌 **历史基线（2020-04~2025-12）**\n"
                f"收益 +{BASELINE['return']}% | "
                f"回撤 -{BASELINE['dd']}% | "
                f"胜率 {BASELINE['win_rate']}% | "
                f"盈利因子 {BASELINE['profit_factor']}"
            )
        }
    })

    # ── 备注（风控提示）──
    card["elements"].append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"{risk_note} | 仅供研究学习，不构成投资建议"
            }
        ]
    })

    return card


# ─────────────────────────────────────────────
# 纯文本消息构建
# ─────────────────────────────────────────────
def build_text(
    date_str: str,
    signals: List[Dict],
    positions: List[Dict],
    metrics: Dict
) -> str:
    """构建纯文本消息"""
    bar = "━" * 32
    lines = []
    lines.append(f"📊 AlphaReversal 每日报告")
    lines.append(f"📅 {date_str}")
    lines.append(bar)

    lines.append("📈 近 30 日绩效")
    lines.append(f"  交易 {metrics.get('trade_count','?')} 笔")
    lines.append(f"  胜率 {metrics.get('win_rate','?')}%")
    lines.append(f"  盈亏 {metrics.get('profit','?')}")
    dd_val = metrics.get('drawdown', '?')
    ret_val = metrics.get('interval_return', '?')
    dd_str = dd_val if '%' in str(dd_val) else f"{dd_val}%"
    ret_str = ret_val if '%' in str(ret_val) else f"{ret_val}%"
    lines.append(f"  回撤 {dd_str}")
    lines.append(f"  区间收益 {ret_str}")
    lines.append(bar)

    if signals:
        lines.append(f"🎯 今日新信号（{len(signals)} 只）")
        for s in signals[:15]:
            lines.append(f"  ▸ {s.get('code','?')}  买入价 {s.get('price','?')}  数量 {s.get('qty','?')}")
        if len(signals) > 15:
            lines.append(f"  ... 还有 {len(signals)-15} 只")
    else:
        lines.append("🎯 今日无新信号")
    lines.append(bar)

    if positions:
        lines.append(f"📂 当前持仓（{len(positions)} 只）")
        for p in positions[:10]:
            lines.append(f"  ▸ {p.get('code','?')}  买入 {p.get('buy_price','?')} ({p.get('buy_date','?')})")
    else:
        lines.append("📂 当前无持仓")
    lines.append(bar)

    lines.append("📌 历史基线: +5.94% / -3.43% / 胜率71.86%")
    lines.append(bar)
    lines.append("⚠️ 仅供研究学习，不构成投资建议")
    lines.append(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# 数据读取（真实回测结果 output/ 目录）
# ─────────────────────────────────────────────
def _find_csv(name: str) -> Optional[str]:
    """按优先级查找文件：output/ → results/ → results/YYYYMMDD/ 最新"""
    for base in ["output", "results"]:
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    # 在 results 子目录中找最新的
    if os.path.isdir("results"):
        subdirs = sorted(
            [d for d in os.listdir("results") if os.path.isdir(os.path.join("results", d))],
            reverse=True
        )
        for d in subdirs:
            p = os.path.join("results", d, name)
            if os.path.exists(p):
                return p
    return None


def load_trades() -> Optional[pd.DataFrame]:
    """读取最新回测交易记录 output/trades.csv"""
    path = _find_csv("trades.csv")
    if not path:
        print(f"  ℹ️ trades.csv 不存在（output/ 或 results/），返回空")
        return None
    df = pd.read_csv(path)
    print(f"  📄 读取交易记录: {path} ({len(df)} 笔)")
    return df


def load_daily() -> Optional[pd.DataFrame]:
    """读取每日净值 output/daily_equity.csv"""
    path = _find_csv("daily_equity.csv")
    if not path:
        print(f"  ℹ️ daily_equity.csv 不存在，返回空")
        return None
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    print(f"  📄 读取净值曲线: {path} ({len(df)} 行, {df['date'].min().date()} ~ {df['date'].max().date()})")
    return df


def load_signals(trades: Optional[pd.DataFrame] = None) -> List[Dict]:
    """
    获取信号列表：
    1. 优先读手写维护的 results/signals.csv（兼容旧用法）
    2. 否则从最新回测 trades.csv 提取最近 5 笔买入作为信号
    """
    legacy = "results/signals.csv"
    if os.path.exists(legacy):
        signals = []
        with open(legacy, "r", encoding="utf-8") as f:
            f.readline()  # 跳过标题
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) >= 3:
                    signals.append({
                        "code":  parts[0].strip(),
                        "price": parts[1].strip(),
                        "qty":   parts[2].strip(),
                    })
        print(f"  📄 读取 {len(signals)} 条信号（results/signals.csv）")
        return signals

    # 从 trades.csv 派生最近买入
    if trades is not None and not trades.empty:
        df = trades.sort_values("buy_date", ascending=False).head(5)
        signals = []
        for _, row in df.iterrows():
            pnl_pct = row.get("pnl_pct", 0)
            signals.append({
                "code":  row["code"],
                "price": f"{row['buy_price']:.2f}",
                "qty":   str(int(row.get("shares", 0))),
                "pnl_pct": f"{pnl_pct:+.2f}%",
            })
        print(f"  📄 从 trades.csv 派生 {len(signals)} 条最近买入")
        return signals

    print(f"  ℹ️ 无信号数据")
    return []


def load_positions(trades: Optional[pd.DataFrame] = None) -> List[Dict]:
    """
    获取持仓列表：
    1. 优先读手写维护的 results/positions.csv
    2. 否则从 trades.csv 找未平仓（sell_date 为空/NaN）记录
    """
    legacy = "results/positions.csv"
    if os.path.exists(legacy):
        positions = []
        with open(legacy, "r", encoding="utf-8") as f:
            f.readline()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) >= 3:
                    positions.append({
                        "code":      parts[0].strip(),
                        "buy_price": parts[1].strip(),
                        "buy_date":  parts[2].strip(),
                    })
        print(f"  📄 读取 {len(positions)} 条持仓（results/positions.csv）")
        return positions

    # 从 trades.csv 找未平仓持仓
    if trades is not None and not trades.empty:
        open_df = trades[trades["sell_date"].isna() | (trades["sell_date"].astype(str).str.strip() == "")]
        if not open_df.empty:
            positions = []
            for _, row in open_df.iterrows():
                positions.append({
                    "code":      row["code"],
                    "buy_price": f"{row['buy_price']:.2f}",
                    "buy_date":  str(row["buy_date"]),
                })
            print(f"  📄 从 trades.csv 提取 {len(positions)} 只未平仓持仓")
            return positions

    print(f"  ℹ️ 无持仓数据（全部已平仓）")
    return []


def load_metrics(trades: Optional[pd.DataFrame] = None,
                 daily: Optional[pd.DataFrame] = None) -> Dict:
    """
    从真实回测结果计算绩效指标：
    - 交易笔数 / 胜率 / 总盈亏 ← trades.csv
    - 当前回撤 ← daily_equity.csv 最后一行 drawdown
    - 近30日区间收益 ← daily_equity.csv 最近30日 equity 变化
    """
    metrics = {
        "trade_count":     "?",
        "win_rate":        "?",
        "profit":          "?",
        "drawdown":        "?",
        "interval_return": "?",
    }

    if trades is not None and not trades.empty:
        metrics["trade_count"] = len(trades)
        if "pnl" in trades.columns:
            total_pnl = trades["pnl"].sum()
            metrics["profit"] = f"{total_pnl:+,.0f}"
        if "pnl" in trades.columns and len(trades) > 0:
            win_rate = (trades["pnl"] > 0).mean() * 100
            metrics["win_rate"] = f"{win_rate:.0f}"

    if daily is not None and not daily.empty:
        # 当前回撤（最后一行，小数转百分比）
        last_dd = daily.iloc[-1].get("drawdown", 0)
        if pd.notna(last_dd):
            metrics["drawdown"] = f"{float(last_dd) * 100:.2f}%"
        # 近30日区间收益
        recent = daily.tail(30)
        if len(recent) >= 2 and "equity" in recent.columns:
            ret = recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1
            metrics["interval_return"] = f"{ret * 100:+.2f}%"

    print(f"  📄 绩效: {metrics['trade_count']}笔 | 胜率{metrics['win_rate']}% | "
          f"盈亏{metrics['profit']} | 回撤{metrics['drawdown']}")
    return metrics


# ─────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AlphaReversal 飞书推送")
    parser.add_argument("--text", action="store_true",
                        help="使用纯文本模式（默认 interactive 卡片）")
    parser.add_argument("--no-image", action="store_true",
                        help="不附带图表（保留参数兼容）")
    args = parser.parse_args()

    # 检查 webhook
    if not WEBHOOK_URL:
        print("❌ 未设置 FEISHU_WEBHOOK 环境变量")
        print("   请在 GitHub Secrets 中配置 FEISHU_WEBHOOK")
        sys.exit(1)

    # 读取数据
    print("[1/4] 加载回测结果...")
    trades = load_trades()
    daily = load_daily()

    print("[2/4] 加载信号...")
    signals = load_signals(trades)

    print("[3/4] 加载持仓...")
    positions = load_positions(trades)

    print("[3.5/4] 计算绩效指标...")
    metrics = load_metrics(trades, daily)

    date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"   日期: {date_str}")
    print(f"   信号: {len(signals)} 只 | 持仓: {len(positions)} 只")

    # 推送
    print("[4/4] 推送到飞书...")
    if args.text:
        text = build_text(date_str, signals, positions, metrics)
        print("   模式: 纯文本")
        success = push_text(text)
    else:
        print("   模式: interactive 卡片")
        card = build_card(date_str, signals, positions, metrics)
        success = push_card(card)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
