# Class2Cal

把学校课表从「网上办事服务大厅」同步到 Apple 日历（iCloud），iPhone 自动跟着更新。

学校分批排课、还会调课，所以这不是一次性导入，而是可以反复跑的增量同步。

## 为什么只抓移动端

门户的 PC 端和移动端是后台各自独立配置的两套「展示方案」，调的是不同卡片、不同接口。
桌面端会漏课，而且只给「第几节」；移动端才给具体时间（08:20 这种）。所以本工具全程
以 iPhone UA 走移动端链路。

## 安装

```bash
uv sync
```

## 用法

```bash
uv run class2cal setup            # 存凭据（进 Keychain，不落盘）
uv run class2cal login            # 验证统一认证能登上
uv run class2cal probe            # 探测课表卡片坐标（关键一步）
uv run class2cal probe --pick 1   # 确认后写入配置
uv run class2cal fetch            # 打印抓到的课，人工核对
uv run class2cal sync --dry-run   # 看 diff，不改日历
uv run class2cal sync --yes       # 实际写入
uv run class2cal status           # 看配置与同步状态
```

`probe` 这一步需要人工确认：课表卡片的 `cardWid`/`cardId` 只能登录后现场探，
自动识别失配时可以翻 `var/probe/*.json` 手工找，填进 `config.toml` 的 `[schedule]`。

## 不会误删你手动加的事件

目标日历里可能已有你自己录的事件。工具给自己建的事件统一打 UID 前缀 `c2c.`
（形如 `c2c.20260921.0820.ab12cd34@class2cal`），对账只在这个集合内做增删改，
其余一律不动。另有四道闸：

1. **非空守卫** —— 某周原本有课、这次抓到 0 条时跳过该周并告警，不删。防的是
   会话静默失效返回空数据把日历清空。
2. **未抓取周免疫** —— 只对账本次抓取成功的周，学校还没排的周完全不碰。
3. **前缀隔离** —— 删除/更新前硬校验 UID 前缀，缺失即跳过。
4. **dry-run 优先** —— 首次同步默认只看不改，确认后才写。

## 凭据

统一认证密码和 Apple 应用专用密码都只存 macOS Keychain，不进 `config.toml`、
不进日志。Apple 专用密码到 [appleid.apple.com](https://appleid.apple.com) 生成
（不是 Apple ID 账户密码）。`var/` 全目录不入版本库。

## 测试

```bash
uv run pytest
```
