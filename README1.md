# AlphaReversal-Cached — 全A股 + 增量缓存版

## 核心改造（v4）

| 特性 | 说明 |
|---|---|
| 🌐 全A股 | 默认 `all_filtered` ≈ 5000 只（沪/深/创业板/科创板/北交所，剔除ST/退市） |
| 📦 分片缓存 | 按交易所分子目录：`cache/stocks/{sh,sz,bj}/` |
| 🔄 增量更新 | 每只股票独立 CSV，只拉"上次末尾 → 今天"的增量 |
| 🐢 自适应限速 | 基础 0.5s/请求，遇错自动退避到 4.0s，成功后缓慢恢复 |
| 🔁 指数退避重试 | 失败自动重试 3 次（1s→2s→4s） |
| 💾 断点续传 | 进度存 `cache/progress.json`，中断后从断点继续 |
| 🛡 降级策略 | API 失败 → 返回过期缓存（宁可旧数据也不空） |
| 🚪 显式登出 | `atexit` 注册 `bs.logout()` |

## 缓存目录结构

```
cache/
├── stocks/
│   ├── sh/      # 沪市主板(6开头) + 科创板(688/689开头)
│   ├── sz/      # 深市主板(0/2开头) + 创业板(3开头)
│   └── bj/      # 北交所(4/8开头)
├── index/        # 指数日线: 000001_SH.csv
├── pool/         # 股票池快照: all_filtered_20260731.csv
├── funda/        # 基本面快照: 20260731.csv
└── progress.json # 断点续传进度
```

## 股票池选项

| 选项 | 数量 | 说明 |
|---|---|---|
| `all_filtered` ★默认 | ~5000 | 全A股剔除ST/退市/停牌 |
| `all` | ~5400 | 全A股（含ST） |
| `hs300` | ~300 | 沪深300 |
| `zz500` | ~500 | 中证500 |
| `top1500` | 1500 | 按市值取前1500 |
| `top20` | 20 | 快速调试 |

## 耗时预估（自适应限速下）

| 场景 | API 调用 | 预计耗时 |
|---|---|---|
| 首次全A股（无缓存） | ~5000 | 40-60 分钟 ⚠️ |
| 次日增量（1 天） | ~5000 | 30-45 分钟 |
| 周末后（3 天增量） | ~5000 | 35-50 分钟 |
| 全量命中缓存 | 0 | <30 秒 |
| hs300 次日增量 | ~300 | 2-4 分钟 |

> ⚠️ **首次运行全A股约 40-60 分钟**，GitHub Actions 免费版单次最多 6 小时，完全够用。后续增量每次 30-45 分钟。
>
> 💡 **建议**：首次先在本地跑（更稳定），或先用 `hs300` 跑通再切全A股。

## 部署步骤

```bash
# 1. 进入你的 AlphaReversal 仓库
cd /path/to/AlphaReversal-repo

# 2. 覆盖文件
cp -r /path/to/AlphaReversal-Cached/src/ ./
cp -r /path/to/AlphaReversal-Cached/scripts/ ./
cp -r /path/to/AlphaReversal-Cached/.github/ ./
cp /path/to/AlphaReversal-Cached/feishu_notify.py ./
cp /path/to/AlphaReversal-Cached/requirements.txt ./

# 3. 提交推送
git add .
git commit -m "feat: 全A股 + 增量缓存 + 自适应限速"
git push
```

## 首次运行建议

由于全A股首次下载量大，**强烈建议先用小池子验证管道通畅**：

```bash
# 手动触发时指定 pool 参数
# 先在 Actions 页面用 hs300 跑一次（~5分钟）
# 确认飞书推送正常后，再切到 all_filtered
```

或者修改 `daily.yml` 中的默认值：

```yaml
# .github/workflows/daily.yml
# 把 pool 默认值先改成 hs300
python scripts/run.py --pool hs300 ...
```

## 本地测试

```bash
# 安装依赖
pip install -r requirements.txt

# 快速测试（20只）
python scripts/run.py --pool top20 --start 20250101 --end $(date +%Y%m%d) --no-plot

# 全A股测试（首次会下载约40-60分钟）
python scripts/run.py --pool all_filtered --start 20250101 --end $(date +%Y%m%d) --no-plot

# 次日增量（应只需2-5分钟）
python scripts/run.py --pool all_filtered --no-plot
```

## 速率控制参数（可调整）

在 `src/data_engine.py` 顶部：

```python
REQUEST_SLEEP = 0.5    # 基础请求间隔（秒）
BATCH_SIZE = 300       # 每 N 只额外休眠
BATCH_SLEEP = 2.0      # 批次间休眠（秒）
MAX_RETRIES = 3        # 最大重试
RETRY_BACKOFF = [1, 2, 4]  # 退避间隔

# 自适应（自动调整，无需手动改）
# 连续成功 → 恢复到 0.5s
# 连续失败 → 退避到 2.0s → 4.0s（封顶）
```

> 如果 baostock 仍然限流，把 `REQUEST_SLEEP` 调到 `0.8-1.0`，`BATCH_SLEEP` 调到 `5.0`。

## 断点续传

进度自动保存到 `cache/progress.json`：

```json
{
  "all_filtered": {
    "completed": ["000001.SZ", "000002.SZ", ...],
    "last_index": 1234,
    "updated": "2026-07-31 16:35:22",
    "total": 5120
  }
}
```

中断后重新运行，会自动跳过已完成的股票，从中断处继续。

## 降级行为

| 场景 | 行为 |
|---|---|
| 单只股票 API 失败 | 返回已有缓存（即使过期） |
| 缓存不存在 + API 失败 | 返回空 DataFrame（该股票跳过） |
| 指数下载失败 | 跳过择时模块，继续回测 |
| 基本面获取失败 | 跳过基本面因子，继续 |

## 注意事项

1. **首次全A股很慢**：建议先在本地或 GitHub Actions 上用 `hs300` 验证
2. **缓存目录会很大**：5000 只股票 × 每只 ~100KB ≈ 500MB，注意磁盘空间
3. **GitHub Artifacts 限制**：单文件 ≤ 500MB，分片上传（sh/sz/bj 分开）避免超限
4. **Actions 超时**：全A股首次可能 60 分钟，已设 `timeout-minutes: 120`
5. **baostock 服务器**：偶尔不稳定，重试机制会自动处理

## 性能优化路线图

| 阶段 | 方法 | 预期提速 |
|---|---|---|
| 当前 | 单线程 + 0.5s sleep | 基准 |
| 短期 | 减少 sleep 到 0.3s + 更激进重试 | ~30% |
| 中期 | 多进程(4 worker) + 每进程独立 IP 连接池 | ~3x |
| 长期 | 本地 baostock 镜像 / 改用 akshare 批量接口 | ~10x |

> 多进程方案需要自托管 runner（固定 IP），否则会被 baostock 限流更严重。
