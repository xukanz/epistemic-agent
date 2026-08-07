# epistemic-agent

企业内部技术能力地图 —— 用 AI agent 构建并持续维护一张组织级能力图谱，回答四个管理层问题：

1. **这个组织实际具备哪些技术能力？**
2. **哪些团队在重复造同一个轮子？**
3. **哪些方向全公司没人覆盖？**
4. **要做某件事，该找谁？**

核心思路是把散落在几百个仓库里的信息（依赖清单、CI 配置、仓库元数据）汇总成一张结构化知识图谱：仓库、团队、技术栈、业务领域、内部系统作为节点，谁用了什么、谁属于谁作为边。图谱建好之后，"谁会什么""有没有人在重复造轮子"这类问题就是图上的查询，而不用一个个翻仓库。

---

## 现状：跑得通，有真实产出

框架在一个真实的内部工程组织上验证过：一次约 350 个代码仓库、约 200 个团队规模的 GitLab 扫描，全链路（bootstrap → ingest → merge → health → view）跑通，产出量级：

```
约 600 个原始依赖 token  →  归一化 + 接地 + 合并后收敛到约 250 个规范化技术节点
约 350 个仓库 · 约 200 个团队 · 10 个业务领域 · 约 25 个内部系统
最终图谱：约 900 个节点 / 约 2000 条边
```

产出的信号类型：

| 信号 | 含义 |
|---|---|
| bus factor = 1 | 只有一个团队掌握的技术，找到了上百个 |
| 疑似重复投入 | `duplicates` 视图能把 fork/副本噪声筛掉，剩下真正跨团队、同领域、栈高度重合的候选 |
| 能力缺口 | 词表里有、图里没有 |
| 内部系统依赖集中度 | 少数几个内部网关/平台被大部分仓库依赖 |
| 待人工审查 | 接地置信度落在 0.50–0.70 的条目，交给人判断 |

具体验证用的这份数据是某个组织的真实内部仓库清单，不随本仓库分发；`instances/` 下没有现成的已运行实例，跑 [快速开始](#快速开始) 里的 `capmap init` 可以在自己的数据上重复同一条流水线。

---

## 快速开始

```bash
uv venv .venv && uv pip install --python .venv -e ".[all]"

# 让 capmap 进 PATH，三选一：
source .venv/bin/activate                                    # 只在当前 shell 生效
ln -sf "$PWD/.venv/bin/capmap" ~/.local/bin/capmap           # 全局可用
# 或者每次写全路径 .venv/bin/capmap

# 建一个新实例
capmap init my-org
cd my-org && claude
```

`capmap init` 之后进 Claude Code，agent 会读 `CLAUDE.md` 走首次对话流程：先问清楚这张图给谁看、要回答什么，**再问两个硬前提**（词表从哪来、谁审队列），然后才提 schema 草案。写好 `scripts/bootstrap.py` 和 `vocabulary/` 之后，日常循环是：

```bash
python scripts/bootstrap.py --out payload.bootstrap.json   # 确定性冷启动，不用 LLM
capmap ingest payload.bootstrap.json                       # 唯一写入口：接地 + 三档路由 + 审计
capmap merge                                               # 按 config 里的策略去重
capmap health                                              # 质量信号 + 能力地图信号
capmap view coverage                                       # 五个分析视图
capmap view duplicates
capmap view gaps
capmap view experts -q "<某项技术>"
capmap view risk

capmap show <节点 ID 或技术名>                              # 钻取单个节点
capmap export -f html                                      # 自包含 HTML 查看器
capmap export -f graphml                                   # 拖进 Gephi / networkx
capmap export -f dot --around "<某个仓库>" --hops 1         # 邻域子图给 Graphviz
```

---

## 关键设计决策

**为什么合成层是五个确定性视图，不是叙述式文档。** 管理层问"谁会什么、有没有人在重复造轮子"要的是能查、能筛、能导出的表，不是一篇要通读的报告。所以分析层是 `coverage` / `duplicates` / `gaps` / `experts` / `risk` 五个确定性视图（见下），每个都是对图谱做一次固定的查询，不依赖 LLM 生成。

**为什么词表层不做成独立 MCP 服务。** 技术栈没有一个类似 EDAM/GO/MONDO 那样体量大、有外部权威、值得跨项目共享的本体可以依赖。这里的词表只有几百条、是组织特有的、且必须由维护这张图的同一批人维护——为几个 YAML 文件起第二个进程没有收益。接口缝隙保留着（`onto/client.py` 的 `resolve_backend` 与 `McpVocabulary`），有共享内部技术分类法的组织可以在不动摄取代码的前提下换后端。

**`DUPLICATES` 边只能由人工决策产生。** 两个仓库共享大部分技术栈不代表在做同一件事——`duplicates` 视图只产出候选，写入图谱前必须经过审查队列。

关于自然键、幂等摄取、接地三档路由这些机制为什么这样设计，见 [`docs/tutorial.md`](docs/tutorial.md) 第 7 节。

---

## 目录结构

```
epistemic_agent/
  kg/store.py              纯 JSON 图存储
  ingest/document.py       唯一写入口：接地 + 三档路由 + manifest 幂等 + changelog
  merge/
    canonical.py           规范化：版本剥离、多值拆分、路径单射、噪声判定
    strategies.py          AliasMerge / FuzzyLabelMerge / NaturalKeyGuard / MultiTokenFlag
  onto/
    client.py              find_terms / find_concepts / 三档路由 + MCP 后端缝隙
    shards/*.yaml          12 个通用技术栈分片，219 个术语
  health/manifest.py       通用质量信号 + 四个能力地图信号
  analysis/
    views.py               coverage / duplicates / gaps / experts / risk
    inspect.py             capmap show：单节点钻取
  export/
    formats.py             GraphML / GEXF / Cypher / DOT
    viewer.py + template.html  自包含 HTML 查看器（canvas + 力导向，无 CDN）
  review/                  ReviewItem 契约、JSONL 队列、Textual TUI、emitters
  llm/client.py            多提供商统一接口（LLM 路径用）
  cli.py                   capmap 命令
  template/                新实例脚手架（CLAUDE.md + 5 个 skill + schema + config）

instances/
  <your-instance>/         capmap init 生成，见下方目录结构
    vocabulary/            实例专属词表
    scripts/bootstrap.py   确定性冷启动（自己写，参考 docs/new-instance.md）
```

`review/`（models + queue + emitters + tui）和 `llm/client.py` 是真正领域无关的通用基础设施——不认识任何一个具体节点类型，任何基于这个框架的项目都能直接复用。

---

## 两条抽取路径

**确定性优先。** 依赖清单、CI 配置、仓库元数据、目录结构都是机器可读的，用代码抽取精确、免费、可重跑。`Repo` / `Team` / `Domain` / `TechStack` / `InternalSystem` 及其全部边都走这条路。

**LLM 只做代码看不见的部分。** 架构模式、这个仓库是干什么的、有什么值得拿走 —— 这些在散文里。`Capability` 和 `Pattern` 归 LLM 路径（`capmap ingest --llm`，需配 key）。

只跑确定性路径的实例里，`Capability` 和 `Pattern` 节点数会是 0。这是设计如此，不是遗漏 —— `gaps` 视图检测到某个类型一个节点都没有时，会把该类型的全部词表术语整体排除出视图并说明原因，而不是显示成"没人具备这些能力"。

---

## 这张图看不见什么

任何能力地图都有同样的盲区，写在这里以免读的人默认它们不存在：

- **没写文档的仓库被系统性低估。** 技术栈能从依赖清单抽全，能力和模式抽不到。所以这张图低估了不写 README 的团队。
- **词表覆盖决定一切。** 词表之外的东西显示为"未接地"，不是"缺口"，会进审查队列或 `kg/vocabulary-suggestions.md` 等人判断。
- **栈重合是"目的重复"的弱代理。** `duplicates` 视图的候选里，fork/副本往往占大头，真正跨团队的重复投入是少数，需要人工确认。
- **仓库不等于能力。** 有人很擅长某件事但在这里没提交过代码。
- **命名空间不一定等于团队。** 如果数据源分不清团队和个人空间（比如 GitLab 快照没带 `namespace.kind`），所有按团队计数的数字都会受影响——`bootstrap.py` 判断不了的地方应该标注出来，而不是猜。

---

## 参考

- **新手入门：[`docs/tutorial.md`](docs/tutorial.md)** — 面向初学者的完整教程，含 9 张架构/流程图、术语表、代码阅读路线、以及「哪些已实现哪些只是设计」的边界声明
- 架构说明：[`docs/architecture.md`](docs/architecture.md)
- 建新实例：[`docs/new-instance.md`](docs/new-instance.md)
