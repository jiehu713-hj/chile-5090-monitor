# 5090 Atlas · 全球 RTX 5090 观察站

固定服务：https://chile-5090-monitor.onrender.com 。重用原 Render 服务与仓库，不创建新付费资源。

## 能力与真实边界

- 27 个国家和地区的来源目录；39 个来源入口。目录覆盖不等于每个市场已成功采集库存或价格。
- 公开网页采集遵守 robots.txt；超时、验证码、HTTP 错误、跳转与拒绝访问分别保留原因，不视为缺货。不绕过登录与访问限制。
- 只从主商品对应的单一 JSON-LD Product/Offer 提取原币种价格与 availability。网页结构化信息属于 **公开网页线索**，不是已验证 API。搜索页、比价、整机、限整机、5090 D、笔记本与歧义推荐不生成单卡报价。
- 尚无已接入的库存 API，因此已验证 API 数量为 0。数量、采购资格、可批量供货、结账、税费和运费都需要另行确认。不同币种不混排。
- Reddit：尝试公开 Atom/RSS 消息源，保存相关标题、原帖 URL、发布时间和采集时间，URL 哈希去重；访问被拒时显示抓取失败。不推断国家、情绪分数或供货可信度。
- X、Instagram、Facebook：显示 **需要授权** 和公开入口。没有授权适配器，不会冒充已接入。平台授权范围、审核和 API 费用需确认后才能开发启用。不调用付费接口。
- 国家、地区、商家、来源类型、状态筛选；CSV 导出；30 天原币种价格散点图与明细；最多保留 90 天、每次最多返回 2,000 点。
- 站内变动事件；浏览器保存原币种阈值规则与去重提醒。关闭网页没有后台手机、邮件推送。首次成功采集建立基线；仅连续成功报价的降价/到货线索生成变动事件，避免网络恢复误报。

## 数据模型

| 表 | 用途 |
| --- | --- |
| monitor_state | source_id 主键；最近一次检查、证据等级、当前价格、上次有效价格及各自时间 |
| observations | 仅成功提取的单卡价格，按 source_id/at 索引；失败不回填旧价格 |
| alerts | 变动事件，包含来源、时间、类型和需要核实的说明 |
| posts | 原链接哈希主键、平台、标题、发布时间、采集时间、证据类型 |

v2 默认使用新的 `monitor-v2.db`，不迁移旧程序可能重复写入的价格历史；不删除旧 monitor.db。状态与历史分开，超过 3 个采集间隔标记过期。定时任务在每轮结束后等待 CHECK_MINUTES，单进程顺序采集、按 robots crawl-delay 退让。网页每 60 秒读取结果，不触发采集。每次状态落库清理 90 天以外记录。

默认 SQLite 仅保证当前实例运行期。Render 免费服务空闲休眠时停止采集，休眠/重启/部署会丢失本地文件，**不能承诺全天候或长期历史**。没有用保活请求绕过免费套餐规则。长期保存可配置用户已有 PostgreSQL 的 `DATABASE_URL`；单实例结构与 PostgreSQL 兼容。此分支未自动创建数据库、磁盘或付费定时任务，PostgreSQL 需在实际环境补充集成验证。

## 本地运行与测试

需要 Python 3.12+：

```sh
pip install -r requirements.txt
python -m unittest -v test_monitor
node --check static/app.js
uvicorn app:app --host 0.0.0.0 --port 8090
```

离线测试使用临时数据库，任何夹具报价都不写入部署数据。GitHub Actions 在 push/PR 执行相同检查。

| 环境变量 | 默认 / 行为 |
| --- | --- |
| CHECK_MINUTES | 30，最少 10；每轮完成后等待 |
| COLLECTOR_ENABLED | 1；本地纯界面测试可设为 0 |
| DB_PATH | 项目目录 monitor-v2.db；临时 SQLite |
| DATABASE_URL | 可选；已有 PostgreSQL 连接串，保密配置，不写入仓库 |

只运行 **一个** Uvicorn worker，防止重复采集。POST 管理接口与公开手工抓取接口均未开放。

## 原服务发布与验证

原 Render 服务 `srv-db8usi4s728c73b632sg`，main 分支；构建 `pip install -r requirements.txt`，启动 `uvicorn app:app --host 0.0.0.0 --port $PORT`。无需改变运行命令或升级套餐。原 render.yaml 保留作配置参考，不申请新 Blueprint。控制台 health check 当前采用平台默认 TCP；应用另提供 `/api/health` 数据库可读检查。

发布后必须确认具体提交的 Render deploy 为 live，并验证 `/api/health` 返回 `version: 2.0.0`，`/api/data` 包含国家目录、平台状态和真实检查时间；再核对网页及静态资源。首次采集允许为空，不能用演示报价掩盖失败。回退代码时使用已知旧提交；回退不会恢复临时 SQLite 历史。

## 扩展来源

编辑 sources.json；每个来源固定 id、国家、币种、类型与 HTTPS 链接。先核对主商品入口，再增加精确单卡来源，不把分类页改成 product。未知结构应增加有夹具测试的专用适配器，不能通过全页最低价格猜测报价。

来源与运行参考：
- [Render 免费实例限制](https://render.com/docs/free)
- [Render 部署流程](https://render.com/docs/deploys)
- [Render 健康检查](https://render.com/docs/health-checks)
- [Schema.org Offer](https://schema.org/Offer)
- [Instagram Platform](https://developers.facebook.com/docs/instagram-platform/)
- [X API](https://docs.x.com/)

本次工作中 Meta 文档返回限流、X 文档不可访问，因此未据此声称任何具体套餐价格或授权能力，社交授权适配保持未接入。
