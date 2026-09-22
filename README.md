# Class2Cal

把学校课表从「网上办事服务大厅」自动同步到 Apple 日历（iCloud），iPhone 自动跟着更新。

学校分批排课、还会调课、撤课，所以这不是一次性导入，而是**可以反复跑的增量同步** — 学校调整什么，日历就跟着变什么，你手动加的事件不会被动。

## 适用范围

本工具适用于使用**金智教务管理系统**（金智教育 Wisedu）的高校。该系统特征：
- 统一认证地址通常为 `sso.xxx.edu.cn`（金智 esc-sso v3）
- 门户地址通常为 `all.xxx.edu.cn`（网上办事服务大厅）
- 登录时可能需要拖动滑块验证码

如果你的学校使用其他教务系统，本工具可能无法直接使用。

## 支持的系统

- **操作系统**：macOS（依赖 macOS Keychain 存储凭据）
- **Python 版本**：3.10+
- **浏览器**：Microsoft Edge 或 Google Chrome（用于辅助登录和接口探测）

## 特性

- ✅ **真实时间，不是节次** — 直接用移动端接口给的具体时刻（08:20-09:45），而非「第 1-2 节」
- ✅ **增量同步** — 自动识别新增、更新、删除，只改变化的部分
- ✅ **保护手录事件** — UID 前缀隔离 + 四道安全闸，不会误删你自己加的课外活动、考试安排
- ✅ **分批排课友好** — 学校后续批次排出来的课会自动接住，已排的周不受影响
- ✅ **调课自动跟随** — 学校改教室、换老师、调时间，下次同步自动反映
- ✅ **一键探测接口** — `probe --browser` 开浏览器让你点进课表页，工具自动抓出接口坐标，不用翻抓包
- ✅ **凭据安全** — 密码只存 macOS Keychain，不落盘、不进日志、不入版本库

## 为什么只走移动端

门户的 PC 端和移动端后台各自独立配置，调的是不同卡片、不同接口。实测发现：

- **桌面端会漏课** — 某些课在移动端有，桌面端看不到
- **桌面端只给节次** — 「第 7-8 节」，没有 08:20 这种具体时刻
- **移动端给完整数据** — 时间、教室、教师齐全，连堂课（1-4 节 = 08:20-11:25）的起止是服务端算好的

所以本工具全程以 iPhone UA 走移动端链路，拿到的就是 iPhone 日历 App 该显示的那个时间。

## 安装

需要 Python 3.10+ 和 [uv](https://github.com/astral-sh/uv)（推荐）或 pip。

```bash
# 克隆仓库
git clone https://github.com/postyizhan/Class2Cal.git
cd Class2Cal

# 安装依赖（uv 会自动创建虚拟环境）
uv sync

# 或用 pip
pip install -e .
```

装完后 `class2cal` 命令即可用（`uv run class2cal` 或直接 `class2cal`，取决于你的环境）。

## 快速开始

### 1. 初次配置

```bash
# 存凭据（学校域名、学号、密码、Apple 专用密码）
class2cal setup
```

配置时需要输入：
- **统一认证地址（SSO）**：如 `https://sso.example.edu.cn`
- **门户地址**：如 `https://all.example.edu.cn`
- **学号**和**统一认证密码**
- **Apple ID** 和 **Apple 专用密码**

**Apple 专用密码**不是你的 Apple ID 账户密码，而是到 [appleid.apple.com](https://appleid.apple.com) → 登录与安全 → App 专用密码 生成的 16 位密码（格式 `xxxx-xxxx-xxxx-xxxx`）。

配置完成后验证登录：

```bash
class2cal login --browser
```

### 2. 探测课表接口

```bash
# 推荐方式：开浏览器让你点进课表页，工具自动抓出接口
class2cal probe --browser
```

会弹出一个 iPhone 模拟的 Edge 窗口（已带上你的登录状态），你在里面：
1. 点进能看到课程表的页面（通常在「服务大厅」或「全部应用」里搜「课表」）
2. 等课表显示出来
3. 关掉窗口

工具会列出候选接口并按可能性排序：

```
找到 15 个候选接口（按可能性排序）：
  [1] https://all.example.edu.cn/execCardMethod/2373030345076692/CUS_CARD_CALENDAR_DETAIL
      命中信号：课、日历  得分 22
  [2] https://all.example.edu.cn/execCardMethod/5071153494847371/SYS_CARD_TODOTASK
      命中信号：(无)  得分 10
  ...

确认哪个是课表后，跑 `class2cal probe --browser --pick N` 写入配置。
```

确认后：

```bash
class2cal probe --browser --pick 1
```

**如果自动识别没找到课表**，可以查看 `var/probe/network.json` 中的完整请求记录，手动找到课表接口后填写到 `config.toml` 的 `[schedule]` 部分：

```toml
[schedule]
card_wid = "课表卡片的 wid"
card_id = "课表卡片的 id"
cal_wid = "课表日历的 wid（可选，用于过滤）"
```

### 3. 验证抓取

```bash
# 抓取并打印课表（不写日历）
class2cal fetch

# 输出示例：
# 2026-09-21 ~ 2026-09-27（9 节）
#   2026-09-21 08:20-09:45  高等数学 @善思楼204 奚敏 [1-2节]
#   2026-09-21 10:00-11:25  思想道德与法治 @善学楼201 衡朝阳 [3-4节]
#   ...
# 共 12 节课，覆盖 4 周
```

人工核对一下课程名、时间、教室是否正确。

### 4. 同步到日历

```bash
# 试运行：看 diff，不改日历
class2cal sync --dry-run

# 输出示例：
# [试运行] 2026-09-21 ~ 2026-09-27
#   + 新增  2026-09-21 08:20-09:45 高等数学
#   + 新增  2026-09-21 10:00-11:25 思想道德与法治
#   ...
#   · 跳过 3 个非工具事件（你手动加的，不动）
#
# 将新增 12、更新 0、删除 0；保护 3 个手录事件

# 确认无误后实际写入
class2cal sync --yes
```

首次同步后，定期（每天或每周）跑 `class2cal sync --yes` 就能保持日历与学校排课同步。

**重要提示**：首次同步前，**删掉「学校课程」日历里你之前手录的课** — 工具不会删它们（UID 不同），会和抓回来的重复。手录的考试、活动之类不受影响，可以放心留着。

### 5. 查看状态

```bash
class2cal status

# 输出示例：
# 配置
#   学号:       22610627
#   门户:       https://all.example.edu.cn
#   课表卡片:    2373030345076692 / CUS_CARD_CALENDAR_DETAIL
#   日历:       我的课表 (calWid=1046441504596287488)
#   抓取窗口:    当前周 + 后 4 周
#
# 同步状态
#   上次同步:    2026-09-21 15:30 (3 周)
#   已同步周:    2026-09-21 (9 节)
#               2026-10-05 (3 节)
#               2026-10-12 (0 节，学校未排)
```

## 命令速查

```bash
class2cal setup              # 存凭据（学号、密码）
class2cal login --browser    # 浏览器登录（推荐）
class2cal login --manual     # 手动粘贴 cookie（备用）

class2cal probe --browser         # 浏览器抓包，自动找课表接口
class2cal probe --browser --pick 1  # 确认第 1 个候选并写入配置

class2cal fetch              # 抓取并打印课表，不写日历
class2cal fetch --weeks 6    # 抓取 6 周（默认 4）

class2cal sync --dry-run     # 看 diff，不改日历
class2cal sync --yes         # 实际写入日历

class2cal status             # 查看配置与同步状态
```

## 安全保护机制

目标日历里可能已有你自己录的事件。工具给自己建的事件统一打 UID 前缀 `c2c.`（形如 `c2c.20260921.0820.ab12cd34@class2cal`），对账只在这个集合内做增删改，其余一律不动。另有四道闸：

### 1. 非空守卫

某周原本有课、这次抓到 0 条时**跳过该周并告警，不删**。防的是会话静默失效返回空数据把日历清空。

```
WARNING 2026-09-21 原有 9 节课，本次抓到 0 条 —— 接口可能失效了。
        跳过该周，不删现有事件。请检查会话或手动 fetch 确认。
```

### 2. 未抓取周免疫

只对账**本次抓取成功的周**，学校还没排的周（返回空数组是正常状态）完全不碰。

比如抓取失败导致某周没拿到数据，该周的现有事件不会被删除，也不会被标记为「需删除」。

### 3. 前缀隔离

删除/更新前硬校验 UID 前缀，缺 `c2c.` 的事件即跳过。代码层面保证不会误碰你手录的事件。

### 4. dry-run 优先

首次同步默认只看不改（`--dry-run`），确认 diff 无误后才 `--yes` 写入。

## 凭据存储

- **统一认证密码** 和 **Apple 专用密码** 只存 macOS Keychain，不进 `config.toml`、不进日志
- **会话 cookie** 存 `var/session.json`（权限 600），有效期几小时，过期自动重登
- `var/` 全目录不入版本库（`.gitignore` 已配置）

## 目录结构

```
.
├── config.toml           # 配置（学号、接口坐标、同步窗口），不含密码
├── var/
│   ├── session.json      # 会话 cookie（自动维护）
│   ├── state.json        # 同步状态（哪些周已同步、UID 清单）
│   ├── raw/              # 原始响应存档（排查用）
│   └── probe/            # 探测阶段的抓包数据
├── src/class2cal/        # 源码
└── tests/                # 测试（44 项）
```

## 故障排查

### 登录失败：需要验证码

学校开着滑块验证码，账密无法静默登录。用浏览器方式：

```bash
class2cal login --browser
```

会开一个 Edge 窗口，你在里面拖滑块登录，工具自动提取 cookie。

### 会话过期

会话 cookie 有效期几小时。过期时 `sync`/`fetch` 会自动尝试重登，如果失败会提示：

```
会话已过期，需要重新登录一次：
    class2cal login --browser
```

重新登录一次即可。

### probe 没找到课表接口

可能是：
1. **没点进课表页** — 确保在浏览器窗口里真的看到了课程表界面，不只是门户首页
2. **课表走了其他接口** — 把 `var/probe/network.json` 发我，我帮你找

### fetch 抓到的课不对

1. **缺课** — 检查 `config.toml` 的 `[schedule] cal_wid` 是否正确，可能过滤掉了
2. **多了无关事件** — 同上，`cal_wid` 没配或配错，把「假期」等其他日历也抓进来了
3. **时间不对** — 对照 `src/class2cal/timetable.py` 里的作息表，确认学校没改过

### sync 提示「日历不存在」

Apple 日历名默认是「学校课程」。如果你改了名字或删了，到「日历」App 里新建一个同名日历（iCloud 账户下）。

或者改 `config.toml` 的 `[calendar] name`。

## 技术架构

```
┌─────────────┐
│   CLI       │  命令行入口（setup / login / probe / fetch / sync / status）
└──────┬──────┘
       │
       ├───► SSO 登录        统一认证 CAS 登录，密码加密（RSA + Base64）
       │                     会话存 var/session.json，自动重登
       │
       ├───► 门户接口         execCardMethod 调用课表卡片
       │                     移动端 UA，拿具体时间而非节次
       │
       ├───► 课表解析         日历卡片 renderData → Lesson 对象
       │                     按 calName/calWid 过滤，排除假期等非课程日历
       │
       ├───► CalDAV 同步      增量对账（新增/更新/删除）
       │                     UID 前缀隔离 + 四道安全闸
       │
       └───► 浏览器探测       Playwright 开 Edge，用户点课表页
                             记录所有请求，自动识别课表接口
```

**核心模块**：

- `sso.py` — 统一认证登录，密码加密（学校用 JSEncrypt RSA），会话管理
- `portal.py` — 门户 API 封装，`execCardMethod` 调卡片方法
- `schedule.py` — 课表解析，日历卡片响应 → `Lesson` 对象
- `calendar_sync.py` — CalDAV 增量同步，对账逻辑 + 安全闸
- `probe_browser.py` — 浏览器抓包，自动识别课表接口
- `cli.py` — 命令行入口

**为什么用移动端接口**：门户的 PC / 移动端后台独立配置，移动端数据更完整（具体时间、不漏课）。

## 测试

```bash
uv run pytest           # 运行全部测试（44 项）
uv run pytest -v        # 详细输出
uv run pytest -k sync   # 只跑同步相关的
```

测试覆盖：
- 课表解析（真实响应结构，日历过滤，UID 稳定性）
- 增量同步（新增/更新/删除，安全闸，手录事件保护）
- 配置加载（TOML 解析，凭据读取）

## 许可

MIT

## 致谢

本工具的开发过程由 [Claude](https://claude.ai) 全程协助完成 — 从需求分析、接口探测、代码实现到测试编写，Claude 提供了完整的技术方案与实现。
