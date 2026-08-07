# Instance vocabulary

Shards here are loaded *in addition to* the framework's own
(`epistemic_agent/onto/shards/`), via `onto.extra_shard_dirs` in
`config/project.yaml`.

This is where organisation-specific vocabulary goes:

- **internal systems** — platforms, gateways, data sources that only exist here
- **in-house frameworks** — libraries with no public equivalent
- **local naming** — the abbreviations your teams actually type

Keeping them here rather than in the framework shards is what lets the
framework be reused by the next organisation. Same format:

```yaml
shard: internal-system
target_type: TechStack        # or Pattern
label: 内部系统
terms:
  - id: is:example
    label: Example Platform
    aliases: [example, example-platform, 内部示例平台]
```
