# 测试案例

下面这些句子可以直接复制到 Pi 里测试。

## 1. 设计 / 边界提醒（notify）

```text
先帮我判断这个设计路线，对不对，先别直接改代码
```

预期：

- 命中 `design-boundary-clarify`
- 给一个轻提醒
- 不阻断发送

---

## 2. README / 包页 / pi.image 范围提醒（notify）

```text
帮我改 README 和包页展示，顺便看下 pi.image
```

预期：

- 命中 `readme-display-scope-clarify`
- 可能提醒先分清：根 README、包内同步、pi.image、包页展示是否都要一起处理
- 不阻断发送

---

## 3. 先预览再发布提醒（notify）

```text
把 README 和包页一起改了，然后直接提交发布
```

预期：

- 命中 `preview-before-release`
- 提醒先预览 README / 包页效果
- 不阻断发送

---

## 4. 外部发布确认（confirm）

```text
改完直接 commit push，然后 npm publish
```

预期：

- 命中 `publish-confirm`
- 弹出两个明确选项：
  - `直接发送，不修改`
  - `取消发送，继续修改输入`
- 若取消：
  - 本次请求不进入 agent
  - 原输入会保留回编辑框

---

## 5. 网络验证分流提醒（notify）

```text
帮我测试一下网络通不通，先 ping 一下
```

预期：

- 命中 `network-validation-clarify`
- 提醒先区分：是测 HTTP/curl，还是连 ping 也要确认
- 不阻断发送

---

## 6. 沙盒模式确认提醒（notify）

```text
帮我验证一下 readonly 模式和 workspace-write 模式
```

预期：

- 命中 `sandbox-mode-clarify`
- 提醒先说明具体要测哪种模式和命令类型
- 不阻断发送

---

## 7. 反例：不应该触发 confirm

```text
这次先本地修好，不要 push，也不要 publish
```

预期：

- `publish-confirm` 不应触发
- 因为命中了 anti_patterns
