#!/bin/bash
# ─────────────────────────────────────────────
# run_daily.sh — 本地一键运行脚本
# 用法: bash run_daily.sh
# ─────────────────────────────────────────────
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AlphaReversal 每日运行"
echo "  时间: $(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. 更新代码（如果是在 git 仓库里）
if [ -d ".git" ]; then
    echo "📥 拉取最新代码..."
    git pull --ff-only || echo "⚠️ git pull 失败，使用本地代码继续"
    echo ""
fi

# 2. 确保虚拟环境
if [ ! -d "venv" ]; then
    echo "🔧 创建虚拟环境..."
    python3 -m venv venv
fi
source venv/bin/activate

# 3. 安装/更新依赖
echo "📦 安装依赖..."
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q requests
echo ""

# 4. 配置
if [ -f "config_template.py" ] && [ ! -f "config.py" ]; then
    cp config_template.py config.py
    echo "✅ 已生成 config.py"
fi

# 5. 运行策略
echo "🚀 运行策略..."
TODAY=$(TZ='Asia/Shanghai' date +%Y%m%d)
START=$(TZ='Asia/Shanghai' date -d '70 days ago' +%Y%m%d)

echo "📅 数据区间: $START ~ $TODAY"
echo ""

python run.py \
    --pool top1500 \
    --start $START \
    --end $TODAY \
    --no-plot \
    --execution next_open

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  策略运行完成"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 6. 推送飞书
if [ -n "$FEISHU_WEBHOOK" ]; then
    echo "📤 推送飞书..."
    python feishu_notify.py --mode full --no-image
else
    echo "⚠️ 未设置 FEISHU_WEBHOOK，跳过推送"
    echo "   请先: export FEISHU_WEBHOOK='你的webhook地址'"
fi

echo ""
echo "✅ 全部完成"
