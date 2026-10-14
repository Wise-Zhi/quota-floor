# Quota Floor

[English](README.md) | **简体中文**

**告诉 Claude Code 或 Codex：订阅额度至少要给你留多少。额度快降到这条线时，它的 `landing` 守卫会拦住后续的修改，让 agent 写好交接说明后停下。**

```text
/landing leave 70 修测试
```

这句话给 agent 一个任务和一条底线：停下时，额度至少还要剩 70%。Landing 在模型之外读取你的真实额度，并预留一小部分，保证 agent 有余量写交接说明；额度一碰到预留线，所有写操作都会被拦下。如果任务做完时还剩 91%，它就在 91% 停下。**底线是上限，不是要花完的目标。**

> **Alpha 版。** 目前只在 Windows 的真实环境里验证过，而且只覆盖
> [验证情况](#验证情况) 里列出的宿主版本。

## 安装

```bash
pipx install git+https://github.com/Wise-Zhi/quota-floor
landing install
landing doctor
```

`landing install` 是按用户账号配置 Claude Code 和 Codex 的，不针对某一个项目。它会保留你现有的设置，用 `landing uninstall` 可以完全撤销。详见 [docs/INSTALLATION.md](docs/INSTALLATION.md)（英文）。

## 为什么需要它

coding agent 会一直干到任务完成为止，哪怕把你这一周剩下的额度全搭进去。现有的几类工具都不太适合订阅用户：

- **Claude Code 的 `--max-budget-usd`** 限制的是 API 花了多少美元。而 Claude.ai 订阅的额度是按 5 小时窗口和每周窗口的百分比算的，不是美元。
- **额度耗尽拦截器** 要等某个窗口降到 0% 才会停。到那时已经没有额度来记录做到了哪一步。
- **用量查看工具** 只显示数字，什么都不拦。

Landing 让你自己定底线，预留出让 agent 体面收尾的额度，并且永远不会把未知额度当成满的。完整对比见 [docs/COMPARISON.md](docs/COMPARISON.md)（英文）。

## 运行过程

```text
你:       /landing leave 70 修测试
landing:  预算已开启 —— 所有窗口底线 70%，预留 4%
agent:    ……正常干活（NORMAL），然后谨慎收尾（CONSERVE）……
landing:  额度降到 74%（底线 70% + 预留 4%）→ LAND
agent:    试图修改 src/app.py
landing:  拒绝："已到额度边界，禁止修改；
          请把技术交接写成最后一条回复，然后停下。"
agent:    最后一条回复 = 交接说明（做了什么、改了什么、测了什么、下一步、续接提示）
landing:  保存到 ~/.landing/handoffs/<时间>-修测试.md
```

LAND 阶段会拦下所有写操作，但仍允许只读查看，这样交接说明才准确。不会往你的项目文件夹里写任何东西。

一个不消耗额度、脚本化的演示流程见 [docs/DEMO.md](docs/DEMO.md)（英文）。

## 用法

| 宿主 | 输入 |
| --- | --- |
| Claude Code | `/landing leave 70 修测试` |
| Codex | `$landing leave 70 修测试` |
| 终端 | `landing status`、`landing status --compact` |

- `leave 70`：停下时至少**剩**70%。`spend 15`：最多花 15 个百分点。这两个关键词必须写一个；只写数字会被拒绝，避免漏写关键词时被理解成相反的意思。
- 中文关键词也可以：`/landing 留 70 修测试`、`/landing 花 15 修测试`。
- 任务描述可以不写：`/landing spend 20` 单独发，就是给当前这一轮工作加上限。
- `leave` 对套餐里的**所有**额度窗口（5 小时和每周）生效，哪个窗口先碰线就按哪个算。`spend` 按 5 小时窗口计算。加上 `weekly` 或 `5h` 可以自己指定窗口，例如 `/landing leave weekly 70 修测试`。
- 一个预算只管一个任务：agent 这一轮结束，预算就自动结束。
- 支持口语化写法：`/landing leave me 70% quota`、`/landing spend at most 15% on this`。和关键词矛盾的修饰语（比如 `leave at most 30`）会被拒绝，不会去猜你的意思。
- 设置写在任务前后都行：`修测试 leave 70` 和前面的写法等价。
- 整条消息只发 `/landing cancel`（Codex 里是 `$landing cancel`），就能结束预算。agent 不能取消自己的预算，会被拦下。

指令由 `UserPromptSubmit` hook 在模型收到消息之前用代码解析。写错了会直接被拒绝，不会悄悄开始一个没有预算保护的任务。

## 工作原理

```text
当前剩余额度 - 你的底线 - 收尾预留 = 可用的工作空间
```

| 状态 | 行为 |
| --- | --- |
| **NORMAL** | 正常工作。 |
| **CONSERVE** | 接近边界：把手头的事做完，不开新任务。 |
| **LAND** | 拦下所有写操作。agent 的最后一条回复就是交接说明，由 `Stop` hook 保存。 |
| **EMERGENCY** | 额度不够再跑一轮模型：根据任务信息和 Git 状态在本地生成一份确定性的交接说明。 |
| **STOPPED** | 预算结束，直到开启新的预算。 |

- **拦截只依据真实的额度读数。** Claude Code 的读数来自状态栏，Codex 的读数来自 `account/rateLimits/read`。查额度不消耗任何模型调用。
- **未知额度永远不会被当成 100%。** 额度缺失、过期或读不出来时，进入 `CONSERVE` 并标记 `QUOTA UNKNOWN`。预算生效期间如果 hook 出错，写操作会被拒绝。
- **预留量是测出来的，不是猜的。** 取每次工具调用实际消耗的 p90，按收尾流程估算，并限制在 3–15% 之间。
- **剩余时间只是参考。** `landing status` 和仪表盘会按观测到的消耗速度估算剩余空间还能用多久，但这个估算永远不会影响拦截。

更多内容：[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md)（状态、窗口、预留、剩余时间、交接约定）和 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（组件和不变量），均为英文。

## 验证情况

| | 读取真实额度 | 守住底线 |
| --- | --- | --- |
| **Claude Code** | 仅限会运行状态栏的地方（终端 CLI） | **已验证** 2026-08-27，Windows：被拦下的写操作没有执行 |
| **Codex** | **已验证** codex-cli 0.150.0-alpha.12.2 | **已验证** 2026-08-29，Windows：LAND 阶段的代码修改被拒绝 |

- 2026-10-15，当前设计：在 Codex（ChatGPT 桌面版，cli 0.162.0-alpha.17.2）上，LAND 拦下了写操作，`Stop` hook 保存了最后一条回复形式的交接说明。在 Codex 和 Claude Code（终端 CLI 2.1.211）上，`spend 15` 的预算都开在了各自的对话上，任务提前完成，预算随这一轮自动结束。Claude Code 在当前设计下还没有实际跑到过 LAND。
- 拦截效果取决于各宿主版本如何处理 hook 的拒绝。升级 Codex 后，请先重新跑一遍集成检查再依赖它。
- Claude Code 的某些界面从不运行状态栏。在那里 Landing 读不到额度，会显示 `QUOTA UNKNOWN` 并停在 CONSERVE。这是安全的，但守不住底线。可以运行 `landing doctor` 并查看 `quota_source`。

每一项结论的完整记录和证据见 [docs/VERIFICATION.md](docs/VERIFICATION.md)（英文）。

## 已知限制

- 只在 Windows 上验证过。macOS 和 Linux 在 CI 里跑完整测试，但没有在真实宿主上检查过。
- Claude Code：只支持订阅账号（API key 会话不提供额度信息），且只支持会运行状态栏的界面。`/usage` 里按模型区分的周额度可能不会传给状态栏。Claude Code 要在第一次回复之后才提供额度，所以新对话里请先随便发一句话，再发 `/landing`。
- Codex 没有 `$landing` 自动补全，因为 Landing 有意不安装 Codex skill（[原因](docs/VERIFICATION.md#codex-gets-no-skill-on-purpose)）。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[test]"
pytest -q
landing simulate --start 100 --leave 70 --burn 0.5,0.7,0.6 --step 2
```

提交 bug 时，最好附上：宿主名称和版本、`landing doctor` 的输出，以及问题是宿主没执行 hook 的决定，还是 Landing 做了错误的决定。

## 许可证

[MIT](LICENSE)
