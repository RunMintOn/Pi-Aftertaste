# reminder-rules-lab

这是一个可直接进入并手工测试 reminder rules 的实验目录。

## 用法

```bash
cd pi-distill/examples/reminder-rules-lab
pi
```

首次进入若 Pi 询问信任项目，选择信任即可，这样 project-local extension 才会加载。

## 目录说明

- `.pi/extensions/`：项目级 extension 挂载点
- `.pi-distill/final/reminder_rules.json`：当前测试用规则文件
- `TEST_CASES.md`：建议直接复制测试的输入样例

## 在 Pi 里先做的两步

1. 查看是否加载成功：

```text
/reminder-rules-status
```

2. 先做静态检查：

```text
/reminder-rules-check 改完直接 commit push，然后 npm publish
```

如果命中成功，你会看到类似：

- `publish-confirm (confirm)`

## 真实交互测试

然后直接输入 `TEST_CASES.md` 里的示例句子，观察：

- `notify`：只提醒，不阻断
- `confirm`：会弹三个明确选项：
  - `直接发送，不修改`
  - `取消发送，继续修改输入`
  - `直接发送，并永久忽略此提醒`

如果你选择取消，原输入会被放回编辑框，不会直接丢失。

如果你点了“直接发送，并永久忽略此提醒”，以后当前项目里这条规则不会再弹。
要恢复测试，可在 Pi 里执行：

```text
/reminder-rules-reset-ignored
```

## 当前测试规则来源

当前规则文件来自：

- `pi-distill/.scratch/reminder-rules/sample_output.reminder_rules.json`

这里通过软链接接到本目录下，方便直接试。
