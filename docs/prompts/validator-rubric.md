# Validator Rubric

目标：检查 `project_context.md`、`collaboration_guide.md`、`patterns.md` 是否真的完成了跨 chunk 综合，并且能在下一次协作中发挥作用。

总分：50 分。
通过线：`{{THRESHOLD}}` 分。
优秀线：42 分。
低于 `{{THRESHOLD}}` 分：必须重写。
最多重写 `{{MAX_RETRIES}}` 次；仍低于通过线则标记 `needs_review`。

## Scoring

| Dimension             | Question                 | Points |
| --------------------- | ------------------------ | -----: |
| Routing               | 三份文档是否各司其职？              |      8 |
| Chunk coverage        | 是否吸收了每个 chunk 的高价值信号，且没有明显失衡？ |     10 |
| Cross-chunk synthesis | 是否真的综合多个 chunk，而不是拼贴？    |      8 |
| Grounding             | 重要结论是否有 chunk/report 证据？ |      8 |
| Specificity           | 是否具体到项目、文件、命令、场景？        |      6 |
| Actionability         | 下次协作或类似项目是否能直接用？         |      6 |
| Boundary control      | 是否避免过度泛化、人格判断、中间方案误写成事实？ |      4 |

## 1. Routing — 10 分

看三份文档有没有放对内容。

* `project_context.md`：只写项目事实、目标、非目标、关键决策、重要文件、验证方式、已知坑。
* `collaboration_guide.md`：写用户和 agent 怎么配合，必须包含用户可直接说的话、agent 应主动问的问题。
* `patterns.md`：写可迁移经验，必须包含适用场景和注意边界。

扣分标准：

* 项目事实跑进 collaboration guide：-2
* 用户建议跑进 project context：-2
* patterns 写成第二份项目总结：-3
* 三份文档大量重复：-3

## 2. Chunk coverage — 10 分

看它是否真的吸收了各个 chunk 的高价值信号，而不是被某一个 chunk 过度主导。

应做到：

* 识别并吸收每个 chunk 的高价值信号，尤其是 `priority_for_synthesis: high` 的信号；
* 不把最新 chunk / 最长 chunk / 最具体 chunk 自动当成整个项目；
* 不遗漏早期 chunk 中的项目主干或关键决策；
* 区分项目主干和阶段性工作；
* 如果较早 chunk 定义了项目主干，而较晚 chunk 更像近期收尾/包装/排障，则前者应更像“主干”，后者应更像“近期重点”。

扣分标准：

* 某个 chunk 明显被忽略：-4
* 最新/最长/最具体 chunk 明显过度主导：-3
* 项目主干被阶段性任务覆盖：-3
* 没有区分项目主干和近期上下文：-2

## 3. Cross-chunk synthesis — 8 分

看它是否真的综合多个 chunk。

应做到：

* 合并重复信号；
* 区分多 chunk 支撑和单 chunk 支撑；
* 区分最终事实、中间方案、一次性问题；
* 处理阶段变化或冲突。

扣分标准：

* 像是 chunk report 拼贴：-4
* 把单 chunk 现象写成项目级规律：-3
* 把早期中间方案写成最终事实：-3
* 没有处理明显冲突或阶段变化：-2

## 4. Grounding — 8 分

看重要结论是否有证据。

应做到：

* 重要结论尽量带 chunk id、文件名、命令、用户表达或具体事件；
* 不编造 packet 里没有的事实；
* 不用外部常识替代 chunk 证据。

扣分标准：

* 明显编造事实：-5
* 重要结论没有证据：-3
* 用“总是、一直、必然、极其”等强词但无证据：-2
* 引入 packet 外信息：-3

## 5. Specificity — 6 分

看内容是否足够具体。

应做到：

* 写出具体模式名、配置名、文件、平台、命令、验证动作；
* 避免“先设计再实现”“注意测试”“提高效率”这种空话；
* patterns 必须具体到适用场景。

扣分标准：

* 大量抽象口号：-4
* 没有具体文件/命令/场景：-2
* patterns 没有适用条件：-2

## 6. Actionability — 6 分

看下一次能不能直接用。

应做到：

* `project_context.md` 能帮助未来 agent 快速接手；
* `collaboration_guide.md` 有用户可复制的话术，也有 agent 可主动问的问题；
* `patterns.md` 有具体做法，不只是总结。

扣分标准：

* 只写观察，不写怎么用：-3
* 用户建议不能直接复制到下一次 prompt：-2
* agent 侧没有可执行提醒：-2
* patterns 没有落地方式：-2

## 7. Boundary control — 4 分

看它是否克制。

应做到：

* 不把单 chunk 现象写成长期规律；
* 不把用户一次行为写成人格判断；
* 不把项目选择写成普遍真理；
* 不把一次环境问题写成长期项目规律。

扣分标准：

* 过度泛化：-2
* 人格化描述用户：-1
* 忽略 caveats：-1

## Hard caps

出现以下情况，最高 34 分：

* 最终文档只是 chunk reports 拼接，没有 synthesis。
* `project_context.md` 大量写用户协作建议。
* `collaboration_guide.md` 没有用户可直接使用的话术。
* `patterns.md` 没有适用条件或注意边界。
* 三份文档超过一半内容互相重复。
* 有明显编造事实。
* 主要内容是抽象口号。
* 某个 chunk 明显包含高价值信号，但最终三份文档几乎没有体现。
* 早期 chunk 明显定义了项目主干，但最终文档几乎只像后期 chunk 的优秀总结。

## Runtime output contract

只返回一个 JSON 对象，不要输出 JSON 之外的任何内容。

字段必须至少包含：

- `raw_score`: 0-50
- `passed`: boolean
- `dimension_scores`: object
- `failed_dimensions`: string[]
- `must_fix`: string[]
- `which_chunk_underrepresented`: string[]
- `which_chunk_overweighted`: string[]
- `rewrite_direction`: object
- `summary`: string

其中：
- `rewrite_direction` 必须包含：
  - `project_context.md`: string[]
  - `collaboration_guide.md`: string[]
  - `patterns.md`: string[]

无论是否通过，只要你观察到轻微失衡，也可以在 `rewrite_direction` 里给出温和修正建议；但如果已通过，就不要把它写成必须重写。

如果你判断通过：
- `passed` 设为 `true`
- `failed_dimensions` 可以为空
- `must_fix` 可以为空

如果你判断不通过：
- `passed` 设为 `false`
- 必须明确指出 failed_dimensions
- 必须给出 must_fix
- 必须指出：
  - `which_chunk_underrepresented`
  - `which_chunk_overweighted`
- 必须给出 3 份文档各自的 rewrite_direction
