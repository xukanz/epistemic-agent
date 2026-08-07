# 架构

## 分层

```
┌──────────────────────────────────────────────────────────────┐
│  实例（instances/*）                                          │
│    config/project.yaml   唯一适配面：路径、词表、合并策略      │
│    vocabulary/           组织专属词表分片                      │
│    scripts/bootstrap.py  确定性冷启动                          │
│    CLAUDE.md + skills/   工作流提示词                          │
├──────────────────────────────────────────────────────────────┤
│  框架（epistemic_agent）                                     │
│    ingest → merge → health → analysis                        │
│    review（人机协作）  ·  llm（可选抽取路径）                  │
├──────────────────────────────────────────────────────────────┤
│  词表（onto/）                                                │
│    12 个通用分片 + 实例分片，find_concepts + 三档路由          │
│    后端可换（local | mcp）                                     │
├──────────────────────────────────────────────────────────────┤
│  存储（kg/store.py）                                          │
│    单个 JSON 文件，节点/边为朴素 dict                          │
└──────────────────────────────────────────────────────────────┘
```

词表被设计成包内可替换的模块层（`onto/`），而不是独立服务——理由见 README「词表层不做成独立 MCP 服务」。

## 数据形状

一个 JSON 文件，节点与边都是朴素 dict：

```json
{
  "nodes": [{"id": "...", "type": "...", "properties": {...}}],
  "edges": [{"id": "...", "type": "...", "source": "...", "target": "...",
             "properties": {...}}]
}
```

边 ID 由内容派生（`source__TYPE__target`），所以摄取与合并天然幂等，重跑不会产生重复边。保存时按 `(type, id)` 排序 —— 这个文件进 git，diff 必须可读。

Schema 校验是**建议性**的：未知类型记为警告，从不拒绝。schema 跟随数据演化。

## 四个不变量

这四条是让图谱可信的东西，任何改动都不能破坏：

1. **单一写入口。** 所有变更走 `ingest_entities`。agent 从不直接改 JSON。
2. **来源可追。** 每个节点带 `_sources`，每次运行追加 `kg/changelog.md`。
3. **不确定性显式化。** 接地置信度分三档路由；落在中间带的进审查队列，不猜。
4. **软删除。** `remove` 与 `deprecate` 打标记，不物理删除。否则会丢掉"这个组织曾经具备什么能力"这个信息，而那恰恰是以后有人要问的。

## 接地：三档路由

```
find_concepts(label, target_type) → ConceptCluster[]

score ≥ 0.70   → 自动设 term_id / ontology_id / _grounding_score
0.50 ≤ s < 0.70 → 设 _grounding_candidates，发 ReviewItem 进队列
score < 0.50   → 保持未接地，标签写入 kg/vocabulary-suggestions.md
```

阈值的**实现位置**在代码里：`ground_payload` 负责判档，提示词只描述策略、不实现策略。策略在提示词里、实现在代码里，两者不能颠倒——否则每个调用方都可能把 0.70/0.50 的判断写错。

### 按目标类型分流

词表分片声明 `target_type`（`TechStack` 或 `Pattern`），接地时按类型过滤。这不是洁癖 —— `react` 对 TechStack 是 React 框架，对 Pattern 是 ReAct 循环，不分流就会互相污染。精确匹配表也按 `(target_type, 归一化文本)` 建键。

### 归一化 vs 语义折叠

刻意分开：

- **`merge/canonical.py` 做通用归一化** —— 大小写、空白、分隔符、版本后缀、多值拆分、括号剥离。不知道任何一个具体技术的名字。
- **词表分片做语义折叠** —— `portkey-ai` → Portkey、`k8s` → Kubernetes。这是数据，跟实例走。

混在一起的坏处是：把某个组织/地域特定的知识写死进了看似通用的代码，换一个组织就不通用了。本项目的通用层里没有任何一个技术名。

## 合并

`build_merge_map` / `apply_merges` 是通用的组合器，四个策略由 `config.merge.strategies` 构造：

| 策略 | 做什么 |
|---|---|
| `AliasMerge` | 按 `term_id` 折叠。规范 ID 从 **term_id** 派生（不是标签，中文标签 slugify 后为空；也不是"组内最短 id"，那会把 `mongodb` 并进 `beanie`） |
| `FuzzyLabelMerge` | 未接地节点 → 已接地节点的有向模糊匹配。按首字符分块 |
| `NaturalKeyGuard` | 自然键冲突**只报告不合并**。合并会掩盖摄取 bug |
| `MultiTokenFlag` | 多值标签隔离到 `-multi`，实际拆分交人工 |

## 分析视图

五个确定性视图（`analysis/views.py`），都返回朴素数据，渲染分离，可以从 notebook 或 agent 里直接调：

| 视图 | 回答 |
|---|---|
| `coverage` | 这个组织实际做什么，按词表分片或业务领域 |
| `duplicates` | 同领域、跨团队、栈高度重合的仓库对，附 `relation` 分类 |
| `gaps` | 词表里有但没人做 / 只有一个仓库 |
| `experts` | 给定技术，谁做过（按最后提交排序） |
| `risk` | bus factor = 1、内部系统依赖集中度、僵尸仓库 |

### 两条硬规则

**重复是问题不是结论。** 栈重合不等于目的重复。视图产出候选，`emit_duplicate_effort` 发给人，只有人工确认才写 `DUPLICATES` 边。`relation` 字段用**路径 basename**（不是显示名）区分 fork/副本 —— 五个 workshop fork 显示名是 `(Preet)` / `(Diana)` / `(workshop)`，路径全是 `github-copilot-hackathon-ssf`。

**没有证据 ≠ 证据表明没有。** 如果某个 `target_type` 在图里一个节点都没有，该类型的全部词表术语都会报"缺口"——那不是缺口，是抽取那一步没跑。`view_gaps` 检测这种情况并整体排除该类型，同时明确说明原因。

## 审查队列

`review/`（models + queue + emitters + tui）是通用的人机协作队列，能力地图专属的 emitter 有三个：`emit_duplicate_effort`、`emit_multi_token`、`emit_internal_lockin`。

只追加的 JSONL：`items.jsonl` 记 agent 的不确定，`decisions.jsonl` 记人的判断。**决策不会自动改图** —— 应用决策是一次正常的、被记录的摄取。这个分离是刻意的：队列是人说过什么的不可变记录。

## 依赖

`pydantic` / `typer` / `rich` / `jsonlines` / `rapidfuzz` / `pyyaml`。TUI 要 `textual`，LLM 路径要 `openai` 或 `anthropic`。

没有 ORM，没有 networkx，没有数据库。
