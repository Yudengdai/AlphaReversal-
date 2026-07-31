# AlphaReversal-Deploy — 每日自动运行 + 飞书推送

基于 [CANGLIN123/AlphaReversal-](https://github.com/CANGLIN123/AlphaReversal-) 的部署增强包。

## 📁 文件结构

```
AlphaReversal-Deploy/
├── .github/
│   └── workflows/
│       └── daily.yml          # GitHub Actions 每日自动运行
├── feishu_notify.py           # 飞书推送（text + interactive 卡片）
├── signal_tracker.py          # 样本外追踪日志
├── run_daily.sh               # 本地一键运行脚本
├── README.md
└── (将 AlphaReversal 原仓库文件放在同目录)
```

## 🚀 快速开始

### 方式一：GitHub Actions（推荐）

1. **Fork** AlphaReversal 仓库到你的 GitHub
2. 将本目录下的 `.github/` 和 `feishu_notify.py`、`signal_tracker.py`、`run_daily.sh` 放入仓库
3. 进入仓库 **Settings → Secrets and variables → Actions**
4. 添加两个 Secret：
   - `TUSHARE_TOKEN` — 你的 tushare token
   - `FEISHU_WEBHOOK` — 飞书机器人 webhook 地址
5. 手动触发一次测试：**Actions → AlphaReversal Daily Signal → Run workflow**

### 方式二：本地运行

```bash
# 1. 克隆 AlphaReversal 仓库
git clone https://github.com/CANGLIN123/AlphaReversal-.git
cd AlphaReversal-

# 2. 把本目录的文件复制到仓库根目录
cp /path/to/AlphaReversal-Deploy/feishu_notify.py .
cp /path/to/AlphaReversal-Deploy/signal_tracker.py .
cp /path/to/AlphaReversal-Deploy/run_daily.sh .
mkdir -p .github/workflows
cp /path/to/AlphaReversal-Deploy/.github/workflows/daily.yml .github/workflows/

# 3. 设置环境变量
export TUSHARE_TOKEN="你的tushare_token"
export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"

# 4. 运行
bash run_daily.sh
```

### 方式三：crontab 定时

```bash
# 每天 16:30 运行
30 16 * * 1-5 cd /path/to/AlphaReversal- && bash run_daily.sh >> daily.log 2>&1
```

## 📊 飞书推送内容

每日推送包含以下板块：

| 板块 | 内容 |
|------|------|
| 标题 | 日期 + 策略名称 |
| 近30日绩效 | 交易笔数、胜率、总盈亏、平均收益 |
| 回撤监控 | 当前回撤百分比 + 颜色预警 |
| 今日新信号 | 当日买入的股票代码、价格、数量 |
| 当前持仓 | 尚未平仓的仓位列表 |
| 历史基线 | 71.86% 胜率 / -3.43% 回撤 等基准数据 |
| 风控提示 | 根据回撤自动给出建议 |

## 🔬 样本外追踪

```bash
# 更新信号日志（每天跑完策略后调用）
python signal_tracker.py update

# 查看绩效报告
python signal_tracker.py report

# 检查今日信号状态
python signal_tracker.py check
```

`signal_log.csv` 会持续累积，3-6 个月后你就有了一份属于自己的**样本外验证数据**，可以回答：
- "在我的实盘追踪里，这类信号的真实胜率是多少？"
- "策略是否已经开始漂移？"

## ⚙️ 配置说明

### GitHub Secrets

| Secret 名 | 说明 |
|-----------|------|
| `TUSHARE_TOKEN` | tushare API token，用于下载 A 股行情数据 |
| `FEISHU_WEBHOOK` | 飞书自定义机器人 webhook URL |

### 飞书机器人配置

1. 在飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人
2. 复制 webhook 地址
3. 建议开启"自定义关键词"：`AlphaReversal` 或 `📊`

### 修改推送模式

在 `daily.yml` 中修改：
```yaml
# 纯文本摘要（更简单）
python feishu_notify.py --mode brief

# 完整卡片（默认，更美观）
python feishu_notify.py --mode full
```

## ⚠️ 注意事项

1. **数据源依赖**：策略依赖 tushare，免费版有调用频率限制，大量股票池可能超时
2. **数据延迟**：GitHub Actions 定时有分钟级抖动，不影响日频策略
3. **回测 ≠ 实盘**：历史 71.86% 胜率是 2020-2025 特定窗口的统计结果
4. **不构成投资建议**：仅供研究学习

## 📜 License

MIT（与上游 AlphaReversal 一致）
