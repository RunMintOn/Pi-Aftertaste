# Pi Distill Blindspot 路线

这是 Pi Distill 里专门负责 **用户协作盲区 / blindspot** 分析的路线。

当前实现仍然是一个**无 LLM 的轻量版本**，核心目标是：

- 识别用户重复表达习惯
- 识别后置约束 / 纠偏链
- 生成 blindspot profile 和改进建议

## 当前方法

1. 高频短语
2. TF-IDF + KMeans
3. TF-IDF + DBSCAN
4. 规则标签
5. correction chain
6. profile 汇总

## 运行方式

### 运行时模式（默认）

```bash
python3 pi-distill/blindspot/blindspot_nlp.py \
  --project-cwd /home/lee/11MyProjrct/34-pi-coding-agent/TMP
```

默认会写到：

```text
~/.pi-distill/projects/<project-id>/latest/blindspot/
```

并更新：

- `state.json`
- `session_index.json`
- `history/<run-id>/run_manifest.json`
- `latest/blindspot/sessions/*.json` session 级缓存

### 仓库样本模式

```bash
python3 pi-distill/blindspot/blindspot_nlp.py \
  --project-cwd /home/lee/11MyProjrct/34-pi-coding-agent/TMP \
  --out pi-distill/runs/tmp/blindspot
```

当前默认增量策略：

- 没变化时复用 `latest`
- 有变化时只重算受影响 session 的缓存，再重做项目级聚合

如需样本输出同时写入运行时状态，可额外加：

```bash
--record-state
```
