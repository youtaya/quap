# Quap Platform Expert · 衡工

QUAP（知衡量化）A 股研究工作台的平台工程专家。擅长 Qlib 不可变代次、训练/推理流水线排障、Docker 单机部署与 PostgreSQL 备份恢复，并能对流水线阻塞与故障做根因定位，给出可在真实栈上验证的修复方案。

## 类型

Agent 型（单个 AI 专家）

## 功能

- **平台架构与契约解读**：四源降级链（通达信 → 东方财富 → 新浪 → 腾讯）、canonical 与 provider 标识、复权因子与 `qfactor` 换算、日频/五分钟代次互不混用、完整血缘链。
- **Qlib 流水线排障**：`origin=scheduled` 与 `origin=pipeline` 代次的用途区别、就绪阻塞原因逐条解读、训练会话数与影子资格门槛、模型时效、`blocked`/`failed` 运行的重试语义。
- **Docker 单机部署运维**：Compose 分层与 project `quant-platform`、只读根文件系统、`migrate` 退出码语义、北京时间运维节奏、改源码必须重建镜像。
- **备份与恢复**：`quap-snapshot-v1` tar 包结构、`restore-check` 流程与 RPO/RTO、保留策略的真实含义、异地副本与失败域边界。
- **故障根因定位**：区分「设计行为 / 数据限制 / 代码缺陷」；熟悉几类反直觉规律（派生状态有自己的写入者、终态任务不会被重试、裸异常名是有意的、`${VAR:-}` 空串会变成 `Path('.')`、多个 checkout 各有一份互不相同的 `deploy/secrets/`）。
- **上线就绪度评估**：按 P0–P3 分级说明阻塞项与影响面，坚持「夹具测试 ≠ 生产验收」。

## 使用示例

- 带我过一遍 QUAP 平台「数据采集 → Qlib 代次 → 训练推理 → 建议发布」的完整链路，并列出当前有哪些阻塞项。
- QUAP 的 backup_overdue 告警一直不消失，帮我定位根因并验证修复。
- 改了源码后容器里还是旧逻辑，或者 migrate 报密码认证失败，怎么排查？

## 头像

头像已自动生成在 `avatars/` 目录下。如需替换为自定义头像，要求：
- 格式：PNG（推荐）或 JPG
- 尺寸：512×512 px
- 大小：单张不超过 500KB

## 安装

将专家包目录放到专家目录下：

```
/Users/jinxiaoping/.workbuddy-ai/plugins/marketplaces/my-experts/plugins/quap-platform-expert/
```

然后运行注册命令使其可见：

```bash
python3 scripts/register_expert.py <expert-dir>
```

## 打包分享

```bash
python3 scripts/package_expert.py <expert-dir>
```
