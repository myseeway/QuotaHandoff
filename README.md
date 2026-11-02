# QuotaHandoff · 额度接力

QuotaHandoff 是一款 macOS 菜单栏工具，用于查看多个 ChatGPT / Codex 账号的剩余额度、重置时间和重置卡，并在账号之间切换。它也提供实验性的自动调度功能，在额度不足时暂停本地聊天，切换账号并继续符合恢复条件的任务。

<p align="center">
  <img src="docs/images/dashboard.jpg" width="380" alt="主面板：账号额度与调度状态">
  <img src="docs/images/reset-cards.jpg" width="380" alt="重置卡详情：卡片状态与到期日期">
</p>

<p align="center">主面板与重置卡详情，使用内置演示数据。</p>

## 安装

需要 macOS 13 或更新版本，以及 Apple Command Line Tools。当前已在 Apple Silicon Mac 上验证。后端使用 `/usr/bin/python3` 和 Python 标准库。

使用前，请安装并登录支持 Work 本地聊天的桌面客户端。当前桌面控制适配 `/Applications/ChatGPT.app`。

如尚未安装编译工具，先执行：

```sh
xcode-select --install
```

然后获取源码并构建：

```sh
git clone https://github.com/myseeway/QuotaHandoff.git
cd QuotaHandoff
bash scripts/build.sh
```

将生成的 `dist/QuotaHandoff.app` 拖到 `~/Applications` 文件夹，在 Finder 中打开后，菜单栏会显示额度图标。当前版本以源码形式发布，构建产物采用本地签名。

升级时先退出正在运行的小组件，再替换应用。已有账号和设置会继续沿用。

## 开始使用

首次打开时，小组件处于 **仅观察** 模式，会识别桌面端当前登录的账号并查询额度。

加入其他账号有两种方式：在桌面端正常登录另一个账号，等待小组件识别；或打开设置，选择“载入另一个登录文件…”，导入自己保存的登录备份。导入过的备份路径会持续跟踪更新。

账号卡片中的“自动轮换”决定该账号是否参与调度。初始两个账号默认参与轮换，之后新增的账号默认仅监控，可按需开启。

点击右上角状态按钮，按 **仅观察 → 自动调度 → 暂停监控** 循环切换模式。

| 模式 | 用途 |
| --- | --- |
| 仅观察 | 定时查看各账号额度，手动选择使用哪个账号。 |
| 自动调度 | 根据剩余额度、重置卡和任务状态安排账号接力。 |
| 暂停监控 | 暂停定时查询和自动调度，保留手动刷新与登录变化检测。 |

## 切换账号

### 手动切换

在目标账号卡片上点击“切换到此账号”并确认，小组件会停止正在运行的聊天，重开桌面端并登录目标账号。任意已导入账号均可作为目标，切换后进入仅观察模式，由你决定何时继续聊天或使用重置卡。

### 自动调度（实验功能）

开启自动调度后，当前账号剩余额度不超过 **5%** 时，小组件会寻找额度充足的轮换账号，记录并暂停当前任务，完成切换后核对任务状态，再恢复符合条件的聊天。调度以账号各额度窗口中的最低剩余比例为准。

当所有轮换账号的额度均不足时，可以使用重置卡恢复额度。在设置中开启“允许自动使用已有重置卡”后，小组件会选择最早到期的有效卡片；卡片属于当前账号时就地重置，属于其他账号时先暂停任务并切换过去。相同到期时间优先当前账号。

没有可用额度或卡片时，任务保持暂停，等待额度刷新。兑换结果需要进一步确认时，也会等待核验后继续。

任务恢复通过向原聊天发送继续指令完成。手动停止、已进入新回合或正在等待输入的聊天会跳过。目前普通本地聊天支持自动切换，持续运行目标仍需手动处理。

### 当前兼容情况

自动调度依赖桌面端接口，客户端升级可能需要相应适配。自动用卡的完整真实兑换流程尚待验证。

遇到桌面端的退出确认提示时，需要先完成确认；登录过期时，请在桌面端重新登录。跨账号继续工作还取决于目标账号对相关云端聊天、插件和组织资源的访问权限。

## 额度刷新

各账号独立安排查询。自动调度时，当前账号的查询间隔会随剩余额度和任务状态调整。

| 情况 | 常规查询间隔 |
| --- | --- |
| 自动调度下的当前账号 | 30 秒 |
| 自动调度下，当前剩余不超过 15% 且有聊天运行 | 10 秒 |
| 备用账号或仅观察模式 | 60 秒 |
| 等待额度恢复 | 60 秒，并在预计重置时间附近复查 |
| 暂停监控 | 按需手动刷新 |

连续点击刷新会合并为一次请求。查询失败后会逐步延长重试等待，最长 30 分钟；重启后沿用原等待时间。

切号和用卡前会核验额度数据。调度依据查询结果触发，受查询间隔影响，实际执行时的剩余额度可能已低于 5%。

## 设置与数据

设置面板提供自动用卡、登录时启动、账号导入和运行记录。

<details>
<summary>查看设置界面</summary>

<p align="center"><img src="docs/images/settings.jpg" width="400" alt="设置面板：自动用卡、登录启动及账号导入"></p>

</details>

登录凭据由 macOS 钥匙串管理。首次访问时，请按系统提示完成授权；系统锁定或权限变化后，可能需要再次确认。账号显示信息、调度记录和任务恢复标记保存在本机的 `~/Library/Application Support/CodexQuota` 目录中。额度数据通过本机客户端向 OpenAI 查询。

卸载时，先在设置中关闭“登录时启动”，退出小组件后移除应用。已保存的账号凭据和状态数据默认保留。详细的数据管理说明见 [SECURITY.md](SECURITY.md)。

## 开发

界面使用 SwiftUI 与 AppKit，Python 后端负责额度查询和任务调度。

运行测试：

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q
```

需要预览界面或制作截图时，可以构建演示版：

```sh
bash scripts/build-demo.sh
```

在 Finder 中打开 `dist/QuotaHandoff Demo.app`，即可使用内置示例账号查看界面。演示版可以与日常使用的小组件同时运行。

项目结构见 [架构说明](docs/ARCHITECTURE.md)，开发约定与提交检查见 [贡献指南](CONTRIBUTING.md)。

## 参考

- [OpenAI App Server 文档](https://learn.chatgpt.com/docs/app-server)：账号、额度和重置卡接口。
- [Gaebabi/codex-switch](https://github.com/Gaebabi/codex-switch)：桌面 IPC 的发现、订阅和续跑协议参考。
- [Apple AppKit](https://developer.apple.com/documentation/appkit) 与 [Security](https://developer.apple.com/documentation/security)：原生界面与钥匙串。

## 许可证

QuotaHandoff 是独立开发的社区项目，采用 [MIT License](LICENSE)。
