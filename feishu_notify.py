#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================
feishu_notify.py — AlphaReversal 飞书推送
================================================================
功能：
  读取 results/signals.csv 和 results/positions.csv
  组装成飞书 interactive 卡片并推送

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
from typing import List, Dict

import requests

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
# 数据读取
# ─────────────────────────────────────────────
def load_signals() -> List[Dict]:
    """
    从 results/signals.csv 读取信号
    格式：code,price,qty
    """
    path = "results/signals.csv"
    if not os.path.exists(path):
        print(f"  ℹ️ {path} 不存在，返回空列表")
        return []
    signals = []
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline()  # 跳过标题行
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                signals.append({
                    "code":   parts[0].strip(),
                    "price":  parts[1].strip(),
                    "qty":    parts[2].strip(),
                })
    print(f"  📄 读取 {len(signals)} 条信号")
    return signals


def load_positions() -> List[Dict]:
    """
    从 results/positions.csv 读取持仓
    格式：code,buy_price,buy_date
    """
    path = "results/positions.csv"
    if not os.path.exists(path):
        print(f"  ℹ️ {path} 不存在，返回空列表")
        return []
    positions = []
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline()
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                positions.append({
                    "code":       parts[0].strip(),
                    "buy_price":  parts[1].strip(),
                    "buy_date":   parts[2].strip(),
                })
    print(f"  📄 读取 {len(positions)} 条持仓")
    return positions


def load_metrics() -> Dict:
    """
    读取绩效指标
    当前为硬编码示例，后续可接入 signal_tracker.py 实时计算
    """
    return {
        "trade_count":     8,
        "win_rate":        62,
        "profit":          "+3420",
        "drawdown":        "-2.1%",
        "interval_return": "+1.8%",
    }


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
    print("[1/4] 加载信号...")
    signals = load_signals()

    print("[2/4] 加载持仓...")
    positions = load_positions()

    print("[3/4] 加载绩效指标...")
    metrics = load_metrics()

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
