# 悠悠有品 求购厚度 / 大佬监控

每天扫描一批 CS2 饰品在悠悠有品上的**真实求购**，输出：

- **商品求购厚度**：出价 ≥ 在售底价 × 0.85 的求购总金额 / 件数，以及与上次的在售数、厚度变化
- **大佬排行**：按用户唯一 ID（userId）汇总有效求购总额 Top100，可展开查看其在哪些商品挂单，点击直达店铺
- 历史数据存于 SQLite（`data/yyyp.db`），可自行做更多分析

> ⚠️ **务必使用私有仓库（Private）**。数据库与报告包含大量用户挂单信息；Secrets 虽安全，但数据文件会提交进仓库。

---

## 目录结构

```
├── monitor.py            主程序（每天运行）
├── build_items.py        名单 CSV → items.txt（仅在名单变化时运行）
├── items.txt             监控清单：每行 "templateId  # 名称"
├── config.template.json  本地运行的配置模板
├── yyyp_colab.ipynb      Colab 版（手动运行）
├── data/
│   ├── yyyp.db           历史数据
│   └── report.html       最新报告
└── .github/workflows/monitor.yml   GitHub Actions
```

---

## 获取登录信息（三种方式都需要）

1. 浏览器登录 https://www.youpin898.com ，打开任意商品页
2. `F12` → 网络(Network) → 找任意 `pc-api.youpin898.com` 的请求 → 请求头(Request Headers)
3. **整段复制**「请求标头」（或右键请求 → 复制 → 复制为 cURL），程序会自动提取 `authorization`、`deviceId`、`deviceUk`、`uk` 四项

token 约 **10 天**过期，程序启动时会显示剩余天数，过期会直接报错提示。建议使用**小号**。

---

## 方式一：GitHub Actions

1. 新建 **Private** 仓库，上传本项目全部文件（含 `.github` 目录）
2. 仓库 → Settings → Secrets and variables → Actions → **New repository secret**：
   - **推荐**：只建一个 `YP_RAW_HEADERS`，值 = 整段粘贴的请求头
   - 或者分别建 `YP_AUTHORIZATION` / `YP_DEVICE_ID` / `YP_DEVICE_UK` / `YP_UK`
   - token 过期后，只需更新 `YP_RAW_HEADERS` 一个

3. Actions 页 → 「悠悠求购监控」→ **Run workflow**
4. 运行结束后：
   - `data/report.html` 与 `data/yyyp.db` 会自动提交回仓库
   - 也可在该次运行页面底部下载 **report** 附件
5. 想定时：编辑 `.github/workflows/monitor.yml`，取消 `schedule` 两行注释

**注意**
- 流程第一步是**接口预检**。GitHub 服务器在海外，若悠悠防火墙拦截会在此失败（❌），此时请改用方式二/三
- 私有仓库免费额度 2000 分钟/月，单次约 60 分钟，**每天跑会超额**，建议手动或隔天
- token 过期后到 Secrets 里更新 `YP_AUTHORIZATION` 即可

## 方式二：本地电脑（国内 IP，最稳）

```bash
pip install -r requirements.txt
# 把整段请求头粘贴保存为 headers.txt（已在 .gitignore 中，不会被提交）
# 或：cp config.template.json config.json 后手动填写
python monitor.py
```
然后打开 `data/report.html`。Windows 可用「任务计划程序」每天定时运行。

## 方式三：Google Colab

上传 `yyyp_colab.ipynb` 到 Colab，把 `items.txt`（和可选的 `yyyp.db`）放到 Google Drive 的 `MyDrive/yyyp/`，按单元格顺序运行即可。

---

## 更换监控清单

```bash
python build_items.py 你的名单.csv
```
CSV 需要含「中文名」或「名称」列（如 `AK-47 | 皇后 (崭新出厂)`）。会自动处理名称差异（如 `格洛克18型`↔`格洛克 18 型`、`CZ75自动型`↔`CZ75`），匹配不到的写入 `items_missing.txt`。

也可以直接手动编辑 `items.txt`：templateId 就是商品页网址里的 `templateId=` 数字。

---

## 🔥 异动榜（在售减少 + 求购增多）

报告最上方会列出与**上次运行**相比同时满足以下条件的商品：

| 条件 | 默认值 | 配置项 |
|---|---|---|
| 在售数减少 | ≥ 3% | `alert.sale_drop_pct` |
| 求购厚度增加 | ≥ 5% | `alert.depth_up_pct` |
| 厚度新增金额 | ≥ ¥5,000（过滤便宜货噪音） | `alert.min_depth_add` |

- **评分** = 在售降幅% + 厚度涨幅% + 新增金额(万元，上限50)，按评分排序
- **谁在加仓**：该商品上有效求购金额增加最多的 3 个用户，首次出现标「新」
- 同时显示 **7 日对比**（数据满 7 天后）、价格变化；全部商品表中异动行标红底，表头可点击排序
- 大佬排行新增「较上次」列，可看到加仓/撤单与新上榜
- Actions 使用环境变量时可在 `config.json` 不存在的情况下用默认值

## 运行机制

1. **批量取价**：先搜「探员」一次拿全部探员，其余按武器类型搜索（200 条/页），按 templateId 精确匹配，得到底价和在售数（约 20 分钟，主要是限流等待）
2. **逐个扫求购**：求购列表按价格从高到低翻页，低于 底价×0.85 即停止（约 40 分钟）
3. 汇总写库、生成报告

- 同一天重复运行 = **断点续跑**（跳过已完成商品）
- 自动处理平台限流（code 84104）：逐步延长等待
- 可在配置中调整：`ratio`（有效求购阈值，默认 0.85）、`delay_min/delay_max`（请求间隔，**不建议调小**）

## 常见问题

| 现象 | 处理 |
|---|---|
| `token 已过期` | 重新复制 authorization |
| 接口返回 403 / HTML 页面 | IP 被防火墙拦截，换国内网络运行 |
| 频繁「平台限流，等待」 | 正常保护行为，耐心等待 |
| 某商品「批量未覆盖，跳过」 | 该商品当时可能无人在售，或名称已变更 |

## 免责声明
仅供个人学习与数据分析。自动化访问可能违反平台用户协议，风险自负；请控制频率，勿公开传播他人交易数据。
