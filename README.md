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

| 信号               | 含义                                                                                |
| ------------------ | ----------------------------------------------------------------------------------- |
| bus factor = 1     | 只有一个团队掌握的技术，找到了上百个                                                |
| 疑似重复投入       | `duplicates` 视图能把 fork/副本噪声筛掉，剩下真正跨团队、同领域、栈高度重合的候选 |
| 能力缺口           | 词表里有、图里没有                                                                  |
| 内部系统依赖集中度 | 少数几个内部网关/平台被大部分仓库依赖                                               |
| 待人工审查         | 接地置信度落在 0.50–0.70 的条目，交给人判断                                        |

具体验证用的这份数据是某个组织的真实内部仓库清单，不随本仓库分发。仓库里带的是一份跑得通的小规模示例：

```
cd instances/acme-corp
python scripts/bootstrap.py --out payload.bootstrap.json
capmap ingest payload.bootstrap.json
capmap merge && capmap health
capmap view duplicates
```

`instances/acme-corp/` 是一家虚构公司（"Acme Corp"，约 25 个仓库、10 个团队），数据全部是编造的，跑的是和真实验证完全相同的一套流水线。`data/seed.yaml` 里特意埋了几个信号，跑完能在对应视图里直接看到：一对跨团队、同领域、栈高度重合的独立重复投入（`duplicates` 视图的 `independent` 分类），一对同名 fork/副本（`same-name` 分类），几个单团队掌握的技术（`risk` 视图的 bus-factor=1），以及一个大部分仓库都依赖的虚构内部网关（`risk` 视图的内部系统集中度）。跑 [快速开始](#快速开始) 里的 `capmap init` 可以在自己的数据上重复同一条流水线。

- 在线看图谱，不用跑任何命令：**[https://xukanz.github.io/epistemic-agent/](https://xukanz.github.io/epistemic-agent/)**（`capmap export --format html` 的产物，交互式力导向图，可切换 4 个内置视图）
- 每个命令实际输出什么，逐条贴真实终端输出：[`instances/acme-corp/README.md`](instances/acme-corp/README.md)

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

`capmap init` 之后进 Claude Code，agent 会读 `CLAUDE.md` 走首次对话流程：先问清楚这张图给谁看、要回答什么，**再问两个硬前提**（词表从哪来、谁审队列），然后才提 schema 草案。

**不想开 Claude Code？** `capmap agent` 是同一套首次对话流程的独立实现——不依赖 Claude Code，直接对接任意 OpenAI 兼容端点，见下方[对话式操作](#对话式操作capmap-agent)。

写好 `scripts/bootstrap.py` 和 `vocabulary/` 之后，日常循环是：

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

## 对话式操作：capmap agent

`capmap init` 之后不一定要开 Claude Code——`capmap agent` 是一个独立的对话循环，读同一份实例 `CLAUDE.md`，自己调用下面这 13 个工具，而不是等人在终端里敲命令：

```bash
capmap agent   # 走任何 OpenAI 兼容端点——OpenAI/Portkey/LiteLLM/Azure OpenAI/Claude等
```

13 个工具：`orient_state` / `read_skill` / `run_bootstrap` / `ingest_payload` / `vocab_draft` / `merge_dry_run` / `merge_apply` / `view` / `review_status` / `read_file` / `fetch_url` / `write_file` / `export`。Guardrail 用了两种不同机制，刻意区分：

- **不存在**——写词表、解决审查队列条目、断言 `DUPLICATES` 边没有对应的工具，物理上调不到，不是靠它自觉。
- **受限**——`write_file` 是真实的写入工具，但代码里写死只放行 `config/project.yaml` / `schema/kg-schema.yaml` / `scripts/bootstrap.py` / `data/raw/*`；`vocabulary/`、`kg/`、`review/` 一律拒绝，无论怎么问都不会松口。

新团队接入不用手写任何文件：告诉它数据在哪个文件或哪个链接，它会自己 `read_file` / `fetch_url` 看真实数据、起草 `config/project.yaml` 和 `schema/kg-schema.yaml`（写之前先把内容念出来给你确认），碰到需要自定义解析代码的情况就照 `skills/bootstrap-instance.md` 写 `scripts/bootstrap.py` 自己跑。

需要设置 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`（模型 ID 按端点自己的命名，没有默认值）；如果端点要求把 key 放在标准 `Authorization: Bearer` 之外的专用 header 上（比如 Portkey 要求 `x-portkey-api-key`），额外设置 `OPENAI_EXTRA_HEADER` 就行。走的是标准 OpenAI `/chat/completions` 协议，所以 Anthropic 自己的 beta OpenAI 兼容端点也能用（`OPENAI_BASE_URL=https://api.anthropic.com/v1`，模型填 Claude 的 model ID）——但那是迁移用的子集，扩展思考等 Claude 原生能力不会暴露出来。

---

## 关键设计决策

**为什么合成层是五个确定性视图，不是叙述式文档。** 管理层问"谁会什么、有没有人在重复造轮子"要的是能查、能筛、能导出的表，不是一篇要通读的报告。所以分析层是 `coverage` / `duplicates` / `gaps` / `experts` / `risk` 五个确定性视图（见下），每个都是对图谱做一次固定的查询，不依赖 LLM 生成。

**为什么词表层不做成独立 MCP 服务。** 技术栈没有一个类似 EDAM/GO/MONDO 那样体量大、有外部权威、值得跨项目共享的本体可以依赖。这里的词表只有几百条、是组织特有的、且必须由维护这张图的同一批人维护——为几个 YAML 文件起第二个进程没有收益。接口缝隙保留着（`onto/client.py` 的 `resolve_backend` 与 `McpVocabulary`），有共享内部技术分类法的组织可以在不动摄取代码的前提下换后端。

**`DUPLICATES` 边只能由人工决策产生。** 两个仓库共享大部分技术栈不代表在做同一件事——`duplicates` 视图只产出候选，写入图谱前必须经过审查队列。`capmap agent` 没有能产生这条边的工具，跟人工用 CLI 的边界完全一致。

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
    draft.py                词表草稿聚类（capmap vocab-draft）
    shards/*.yaml          12 个通用技术栈分片，219 个术语
  bootstrap/generic.py     配置驱动的通用冷启动，扁平数据源零代码接入
  health/manifest.py       通用质量信号 + 四个能力地图信号
  analysis/
    views.py               coverage / duplicates / gaps / experts / risk
    inspect.py             capmap show：单节点钻取
  export/
    formats.py             GraphML / GEXF / Cypher / DOT
    viewer.py + template.html  自包含 HTML 查看器（canvas + 力导向，无 CDN）
  review/                  ReviewItem 契约、JSONL 队列、Textual TUI、emitters
  llm/client.py            多提供商统一接口（可选 LLM 抽取路径）
  agent/                   capmap agent：对话式操作，见上方一节
    tools.py                13 个工具的纯函数实现 + OpenAI function-calling schema 包装
    prompt.py                系统提示词（复用实例的 CLAUDE.md）
    runtime.py                加载 .env.llm，启动 openai backend
    backends/
      openai_backend.py      OpenAI 兼容端点（OpenAI 本身、Portkey、LiteLLM、Claude 等）
  project.py               Project 路径解析，cli.py 和 agent/ 共用
  cli.py                   capmap 命令
  template/                新实例脚手架（CLAUDE.md + 7 个 skill + schema + config）

instances/
  acme-corp/               跑得通的虚构示例，见上方"现状"一节
  <your-instance>/         capmap init 生成，见下方目录结构
    vocabulary/            实例专属词表
    scripts/bootstrap.py   确定性冷启动（模板自带通用版，或参考 docs/new-instance.md 自己写）
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

- 架构说明：[`docs/architecture.md`](docs/architecture.md)
- 建新实例：[`docs/new-instance.md`](docs/new-instance.md)
