# `session_analysis_prompt.md`

# 单 Chunk 深读分析 Prompt

你现在要做的是：**深读一个 session chunk 文件**，产出一份 `chunk report`。

这个 chunk 是 multi-chunk pipeline 的一部分。
你只负责分析当前 chunk，提取它能贡献给后续 synthesis 的信号。
不要生成最终文档，不要下项目级最终结论，不要把当前 chunk 的现象直接说成长期规律。

## 输入限制

你只允许读取当前指定的 chunk 文件：

`{{CHUNK_FILE}}`

不要读取其他 chunk、其他 session、项目文档、已有报告或最终产物。
只能基于当前 chunk 判断。
如果证据不足，直接写证据不足。

如果 chunk 中包含来源信息，如 session id、turn id、行号、时间戳、文件名、命令，请尽量保留在证据中，方便 synthesis 阶段追溯。

## Pipeline 定位

当前阶段是 map 阶段：

```text
chunk_XXX.md
→ chunk_XXX.report.md
```

后续阶段才会做 reduce / synthesis：

```text
所有 chunk reports
→ synthesis packet
→ project_context.md / collaboration_guide.md / patterns.md
```

所以你现在不要做跨 chunk 综合。
你要做的是：把当前 chunk 里有价值的信息，整理成可被后续综合的结构化信号。

如果当前 chunk 很长、包含多个阶段、或后半段比前半段更具体，不要自动让最后一段内容主导整个 report。
先找出这个 chunk 里更稳定、更改方向、更像项目主干的信号，再决定哪些近期细节值得保留。

## 最终文档归宿

每条信号最终只允许流向下面四类之一：

### 1. `project_context`

项目本身的事实、目标、非目标、架构边界、关键决策、关键文件、验证方式、环境约束、已知坑。

服务对象：未来接手本项目的 agent / 开发者。

例子：

* 本项目核心是 write-boundary protection，不是完整 workspace isolation。
* `.pi/pi-guard.json` 是策略权威来源。
* Windows 当前不支持。

### 2. `collaboration_guide`

用户和 agent 在这个项目里如何配合，才能少返工、更快收敛。

包括：

* 开始前要问清什么；
* 用户下次可以怎么说；
* agent 应该何时主动确认；
* 如何声明目标、非目标、完成标准；
* 如何分工测试；
* 哪些协作方式容易造成摩擦。

服务对象：用户 + 未来 agent。

例子：

* 长任务开始前，先确认“先审方案，还是直接执行”。
* agent 遇到边界型任务时，应主动复述目标、非目标、验证方式。

### 3. `patterns`

从当前项目中抽出的、未来类似项目也可能复用的经验。

必须有适用条件，不能写成万能经验。

服务对象：未来类似项目。

例子：

* 安全边界类功能不能只靠单测，应加入真实环境 smoke test 和绕过测试。
* 当第三方运行时库副作用影响核心边界时，可以考虑 fork/vendor，但要先确认替代成本。

### 4. `discard`

只适合作为当前 chunk 记录，不值得进入最终文档。

包括：

* 一次性细节；
* 证据弱；
* 太抽象；
* 无法影响下一次行动；
* 只描述过去，不能沉淀成项目上下文、协作指南或可迁移模式。

如果一个信号同时属于多类，请拆成多条，不要混写。

## 强信号标准

只保留强信号。强信号至少满足三项：

* 当前 chunk 中能直接看到；
* 有明确证据；
* 对任务推进、返工、成本、质量、边界判断产生实际影响；
* 后续有可能进入 `project_context.md`、`collaboration_guide.md` 或 `patterns.md`；
* 能转化成下次可用的提醒、约束、说明、流程或经验。

决定 `priority_for_synthesis` 时，优先级通常应按下面顺序判断：

1. 改变了项目定位、目标/非目标、安全边界、核心设计的信号；
2. 改变了验证方式、实现主线、依赖处理方式的信号；
3. 只是某个阶段的具体工程问题、发布细节或一次性排障；
4. 只是最新出现、但没有改变主线的局部问题。

不要因为某段内容更新、更具体、更容易展开，就自动给它最高优先级。

不要输出空泛总结，例如：

* “先设计再实现”
* “要注意测试”
* “用户重视质量”
* “agent 应该更仔细”
* “项目要保持清晰”

除非你能把它改写成具体、带场景、可执行的内容。

## 输出结构

### 1. Chunk story

用 5-8 条高密度要点说明当前 chunk 发生了什么：

* 起点是什么；
* 中间发生了哪些关键变化；
* 哪些判断改变了方向；
* 最后推进到哪里。

只写主线，不复述全文。

### 2. Distill signals

列出 5-12 条最有价值的信号。

每条按下面格式写：

#### Signal N：一句话标题

* 观察：当前 chunk 里发生了什么。
* 证据：引用或概括当前 chunk 中的具体证据，尽量带 session id / turn id / 行号 / 文件名 / 命令 / 用户原话。
* 含义：这说明什么，为什么重要。
* 归宿：`project_context` / `collaboration_guide` / `patterns` / `discard`
* scope：`chunk-local` / `likely-project-level` / `uncertain`
* cross_chunk_need：是否需要其他 chunk 验证；写“需要 / 不需要 / 不确定”，并说明原因。
* priority_for_synthesis：`high` / `medium` / `low`。
  - `high`：这个 chunk 中最值得 reduce / synthesis 阶段重点考虑的信号；如果后续最终文档没有体现，必须是因为不适合该文档，而不是被忽略。
  - `medium`：有价值，但更适合和其他 chunk 一起交叉验证或合并后再写入。
  - `low`：可作为背景，不应主导最终文档。
* 建议写入：如果进入最终文档，建议改写成什么句子。
* 置信度：高 / 中 / 低。
* 注意边界：这条结论不能被过度解释成什么。

要求：

* `project_context` 信号必须是项目事实或项目约束。
* `collaboration_guide` 信号必须能转成用户或 agent 下次的具体动作。
* `patterns` 信号必须写清适用场景，不能是泛泛经验。
* `discard` 信号要说明为什么不值得沉淀。
* `likely-project-level` 不等于最终项目结论，只表示值得 synthesis 阶段重点检查。
* 每个 chunk 至少要标出少量 `priority_for_synthesis: high` 的高价值信号；除非当前 chunk 确实几乎全是噪音，否则不要把所有 signal 都写成 `medium/low`。

### 3. User-facing opportunities

只基于当前 chunk，列出 2-4 条用户下次可以怎么做，才能让类似协作更顺。

每条包含：

* 建议：用户下次可以怎么说或怎么做。
* 依据：当前 chunk 里哪里体现出这条建议有用。
* 可直接复用的话术：给一句用户下次可以直接复制的短句。
* agent 可代劳提醒：agent 下次遇到类似场景时应该主动问什么。
* 适用场景：什么任务适用。
* 注意边界：不要上升成长期人格判断。

### 4. Not supported

列出当前 chunk 不能支持的结论。

重点写：

* 证据不够的长期偏好；
* 不能泛化到整个项目的判断；
* 不能泛化到其他项目的经验；
* 只是一次性环境问题的细节；
* 看起来重要但不该进入最终文档的内容。

## 风格要求

* 中文输出。
* 高密度短答案。
* 有判断，但不装懂。
* 具体，不说空话。
* 宁少勿多。
* 每条重要结论都必须能回到当前 chunk 证据。
* 不要写成长篇复盘。
* 不要直接生成 `project_context.md`、`collaboration_guide.md` 或 `patterns.md`。
