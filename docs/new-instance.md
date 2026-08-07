# 建一个新实例

## 0. 先确认两个硬前提

一张技术能力地图只有两种失败方式，而且都能在写第一行代码之前看出来。

**词表从哪来？** 没有受控词表，每个团队写 `k8s` / `Kubernetes` / `K8S`，图谱就是噪声。框架自带 12 个通用分片（219 个术语），但组织专属的部分 —— 内部系统、自研框架、你们团队实际敲的缩写 —— 必须自己维护。**谁维护？**

**谁审队列？** agent 会把拿不准的东西路由到 `review/items.jsonl`。没人处理的话，几轮摄取之内质量就开始腐化。**要一个名字。**

这两个问题没有答案就先别建。硬建出来的是一个看起来很有底气、但不该被相信的产物。

## 1. 脚手架

```bash
capmap init <name>
cd <name>
claude
```

进 Claude Code 后用自己的话描述这个组织和这张图要干什么。agent 读 `CLAUDE.md` 走首次对话流程，不需要你懂框架术语。

## 2. 裁 schema

模板 schema 是起点不是规定。常见的裁剪：

| 情况 | 删掉 |
|---|---|
| 拿不到人员数据 | `Person` + `MAINTAINS` |
| 仓库不做可移植性分档 | `Repo.tier` / `reuse` / `value` |
| 全部跑公有云 | `InternalSystem` + `DEPENDS_ON_INTERNAL` |
| 没有现成的业务分类 | `Domain` + `IN_DOMAIN`（注意：`duplicates` 视图靠 Domain 过滤，删掉之后重复检测会退化成全局比对） |

只留能立刻对着 `purpose` 说清楚理由的类型。剩下的等真实数据逼出来再加 —— 加一个类型很便宜，一个没用的类型会出现在每一次健康检查和每一个视图里。

**先写类型重命名表。** 改节点类型名很容易漏改脚本里的引用，导致静默失效——脚本读到的是默认值或空值，不会报错。改名之前先把对照表写下来。

## 3. 写 bootstrap

`scripts/bootstrap.py` 读你们的仓库清单，产出摄取 payload。原则：

**能用代码抽的绝不用 LLM。** 依赖清单、CI 配置、仓库元数据都是机器可读的。参考 `instances/acme-gitlab-agents/scripts/bootstrap.py`。

**不要猜 URL 和 API 端点。** 问清楚清单从哪来。错的端点会安静地产出空图。

**把判断不了的事说出来。** 那个实例判断不了 197 个命名空间里哪些是团队、哪些是个人空间（GitLab 快照没带 `namespace.kind`），于是标 `kind: unknown` 并在运行时打出计数。猜一个会让每一个按团队计数的数字都失真且无从察觉。

## 4. 建实例词表

`vocabulary/` 下按框架分片的同样格式写：

```yaml
shard: internal-system
target_type: TechStack        # 或 Pattern
label: 内部系统
terms:
  - id: is:example
    label: Example Platform
    aliases: [example, example-platform, 内部示例平台]
```

放这里而不是改框架分片，是为了下一个组织还能复用这个框架。

**主机名不要做模糊匹配。** 主机名是标识符。`sso.acme.example` 和 `app.acme.example` 共享 acme 和 example 两段，模糊匹配会把它们判成同一个系统。要么走精确别名，要么在 bootstrap 里显式处理（参见那个实例对 `internal_terms` 的做法）。

## 5. 跑起来

```bash
python scripts/bootstrap.py --out payload.bootstrap.json
capmap ingest payload.bootstrap.json
capmap merge --dry-run     # 先看报告
capmap merge
capmap health
```

`kg/merge-report.md` 要人看过再 apply。两段都重要：

- **Merge map** —— 抽查几条。`tech-portkey-ai` → `tech-portkey` 是对的；`tech-langchain-openai` → `tech-langchain` 是粒度判断，如果你们在意这个区别，就在词表里给它单独一个术语。
- **Invariant violations** —— 自然键冲突。这些**永远不会**被自动合并，因为它意味着摄取产出错了。修摄取再重跑。

## 6. 读视图

```bash
capmap view coverage
capmap view duplicates
capmap view gaps
capmap view experts -q "<某项技术>"
capmap view risk
```

怎么读、每个视图的陷阱在哪，见 `skills/analyse-coverage.md`。

## 7. 处理队列

```bash
capmap review-status
capmap review --reviewer <name>
```

**按类型批处理，不要按优先级顺序。** 连着判二十条接地候选很快，因为上下文是共享的；在接地和重复判断之间来回切换又慢又容易判错。

队列涨得比处理得快，说明阈值对这个语料不合适，不是审的人太慢。按这个顺序调：先加词表别名（一条别名能消掉后面几十条），再调重复检测阈值，最后才考虑收窄路由范围 —— 收窄之后条目不再出现，但不确定性并没有消失。

## 常见坑

**`last_commit` 要用真人提交。** 依赖机器人会把死仓库的活动时间抬上来，而僵尸仓库本身是一个发现。那个实例的数据源里 68/392 个仓库的 `last_activity_at` 被 Renovate 污染过。

**`raw_label` 是溯源不是输入。** 多值 token 拆分后，两半都带着拆分前的原串。拿它去接地，`OpenAI/Anthropic` 里的 OpenAI 会被匹配到 Anthropic。

**中文标签 slugify 后是空的。** 框架的 `slugify` 会退化成短摘要，但如果你自己拼 ID，记得处理。

**私有优先。** 这张图给具名团队的工作打分。`deployment: private` 不要动，直到有人明确决定公开。节点属性里不要写你不愿当面对那个团队说的话。
