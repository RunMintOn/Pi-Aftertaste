# Project Distill Pipeline

## 目标

把一个项目的 Pi session 历史转成三份可直接放在项目根目录的高质量文档：

- `project_context.md`：项目事实、边界、架构决策、验证方式、已知坑。
- `collaboration_guide.md`：用户下次如何更高效地和 agent 协作。
- `patterns.md`：从该项目中抽出的可迁移工作模式和经验。

核心不是多 agent，而是一个可追溯、可缓存、可验证的流水线。

## MVP 流程

```text
Pi sessions
→ Pi-VCC compile
→ deterministic chunker
→ session_chunks/
→ chunk analysis
→ chunk_reports/
→ synthesis
→ project_context.md / collaboration_guide.md / patterns.md
→ validator
→ state/cache
```

## 目录约定

最终产物放目标项目根目录：

```text
<project-root>/
  project_context.md
  collaboration_guide.md
  patterns.md
```

过程产物放项目内隐藏目录：

```text
<project-root>/.pi-distill/
  state.json
  runs/<run-id>/
    manifest.json
    session_chunks/
      chunk_001.md
    chunk_reports/
      chunk_001.report.md
    synthesis/
      project_context.md
      collaboration_guide.md
      patterns.md
    validation/
      score.json
      critique.md
```

## Chunk 规则

Chunker 必须是确定性脚本，不由 LLM 自由切分。

原则：

- 短 session 可合并。
- 长 session 按自然边界拆分。
- 不破坏 user / assistant / tool call 的原始顺序。
- 每个 chunk 保留来源信息：session id、源文件、行号范围、turn range、内容 hash。
- 目标大小：约 2-3 万 tokens。
- 硬上限：约 5 万 tokens。

Pi-VCC 产物使用协议：

- 默认读 `*.min.txt`。
- 需要核对细节时，按 `min` 中的 `.txt:行号范围` 回跳同名 `*.txt` 的最小必要行段。
- MVP 不依赖 `*.view.txt` 或 `--grep`。

## LLM 节点

### 1. Chunk analysis

每个 chunk 独立运行一次 session/chunk analysis prompt，输出 `chunk_XXX.report.md`。

报告只做中间材料，不直接写最终 memory 或资产。

### 2. Synthesis

读取全部 chunk reports，生成：

- `project_context.md`
- `collaboration_guide.md`
- `patterns.md`

三份文档职责必须分流：

- 项目事实进入 `project_context.md`。
- 协作建议进入 `collaboration_guide.md`。
- 可迁移模式进入 `patterns.md`。

### 3. Validator

Validator 按 rubric 对三份文档评分。低于阈值时，把 critique 交回 generator 重写。

建议：

- 阈值：8/10。
- 最多重试：2 次。
- 仍不合格：保留产物，但标记 `needs_review`。

评分维度：

- groundedness：是否有证据支撑。
- specificity：是否具体到该项目。
- routing：三类文档是否分流正确。
- actionability：下次是否能直接使用。
- non-overgeneralization：是否避免过度泛化。

## State / Cache

至少记录：

```json
{
  "project_root": "...",
  "prompt_version": "...",
  "model": "...",
  "sessions": {
    "session_id": {
      "fingerprint": "content_hash",
      "compiled": true,
      "chunks": ["chunk_001"],
      "status": "done"
    }
  },
  "chunks": {
    "chunk_001": {
      "source_sessions": ["session_id"],
      "fingerprint": "content_hash",
      "report_path": "...",
      "status": "done"
    }
  },
  "synthesis": {
    "inputs": ["chunk_001.report.md"],
    "status": "done",
    "score": 8.4
  }
}
```

Fingerprint 优先用内容 hash，不只依赖 mtime/size。

## AGENTS.md 集成

不要把 `project_context.md` 全量 append 到 `AGENTS.md`。

推荐只加短引用：

```markdown
## Project context
Before working on this project, read:
- `project_context.md`
```

如果未来支持 include，再改成 include 机制。

## MVP / Later 分界

MVP：

- 单 orchestrator。
- 串行 chunk analysis。
- 确定性 chunker。
- state/cache。
- synthesis + validator。

Later：

- chunk analysis 并发。
- 真正多 agent 调度。
- 模型/思考等级/提示词版本策略。
- 更强的失败恢复和成本控制。
- `--grep` / `view` 参与跨 session 搜索。
