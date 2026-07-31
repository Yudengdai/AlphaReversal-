# AlphaReversal-Deploy v2 — 文件替换说明

## 📁 本次需要替换的文件（共 2 个）

| 文件路径 | 说明 |
|---|---|
| `.github/workflows/daily.yml` | GitHub Actions 工作流 |
| `feishu_notify.py` | 飞书推送脚本（仓库根目录） |

## 🔧 替换步骤

### 方式一：直接覆盖（推荐）

```bash
# 1. 进入你的 AlphaReversal 仓库
cd /path/to/your/AlphaReversal-repo

# 2. 覆盖两个文件
cp /path/to/AlphaReversal-Deploy-v2/daily.yml .github/workflows/daily.yml
cp /path/to/AlphaReversal-Deploy-v2/feishu_notify.py ./feishu_notify.py

# 3. 提交并推送
git add .github/workflows/daily.yml feishu_notify.py
git commit -m "fix: replace daily.yml & feishu_notify.py (v2)"
git push
```

### 方式二：手动复制粘贴

1. 打开本目录下的 `daily.yml`，**全选复制**
2. 在 GitHub 网页上，进入 `.github/workflows/daily.yml`，点铅笔图标编辑，**全选替换**
3. 同样操作 `feishu_notify.py`
4. 分别 Commit changes

## 🧪 测试

推送后，进入 GitHub → **Actions** → 左侧选 **AlphaReversal Daily Signal** → 点 **Run workflow** → 确认。

预期日志：
```
⚠️ TUSHARE_TOKEN 未设置，使用示例数据模拟
✅ 示例数据已生成
📤 推送飞书...
✅ 飞书推送成功
```

## ⚙️ 后续接入真实数据

等你有 tushare token 后：
1. 进入仓库 **Settings → Secrets and variables → Actions**
2. 添加 `TUSHARE_TOKEN`，值为你的 token
3. 下次运行时，Workflow 会自动检测并使用真实数据
4. 无需修改任何文件

## 📌 注意事项

- `daily.yml` 中的 `on` 字段加了引号（`"on":`）是因为 YAML 把 `on` 当作布尔关键字
- `feishu_notify.py` 使用 `interactive` 卡片模式，飞书卡片必须用 `"card"` 字段（不是 `"content"`）
- 无 Token 时只生成 3 只模拟信号 + 2 只模拟持仓，用于测试推送链路
- 飞书机器人需开启"自定义关键词"：`AlphaReversal` 或 `📊`
