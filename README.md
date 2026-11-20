# agent-skills

给编码 agent 用的 skill 集合。每个子目录是一个独立的 skill，入口是其中的 `SKILL.md`。

| Skill | 做什么 |
|---|---|
| [codex-dispatch](codex-dispatch/SKILL.md) | 把成块的后端、算法、数据处理、性能代码派给本机 Codex CLI 实现，派活方定接口、验收，合并前再用另一个模型做定向审查。附带包装脚本 `codex-worker`（Windows / macOS / Linux）。 |

## 怎么用

把仓库克隆到本机，然后让你的 agent 读对应 skill 的 `SKILL.md`，按「第一次使用：接入」一节完成安装、写入常驻规则和冒烟自检。

```bash
git clone https://github.com/Lovenndme/agent-skills.git
```

也可以把某个 skill 目录链接进 agent 的 skills 目录（例如 Claude Code 的 `~/.claude/skills/`），让它能按需加载。但 `codex-dispatch` 的规则需要常驻，链接之外仍要按它的说明写进全局指令文件。
