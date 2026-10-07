# 淘票票路演监控

淘票票（大麦系）影院路演 / 见面会场次全天监控工具。发现目标影片在指定城市放出见面会标签场次（或接近白名单票价场次）时，立即推送到企业微信群。

无需登录 Cookie，程序自动完成 mtop H5 握手与令牌续期。

## 功能

- **全天监控**：按「ShowID + 城市 + 日期」循环扫描，命中即推企业微信群
- **不限城市搜片**：ShowID 全网唯一，输入片名关键词即可查出 ShowID（自动扫 12 个热门票仓城市）
- **见面会场次识别**：自动识别「明星见面会 / 映后见面会 / 主创见面会 / 路演」标签
- **票价白名单兜底**：没标签的影院按票价接近度兜底（误差 1 元内）
- **Web 控制台**：浏览器里配置目标、看扫描状态、查命中记录
- **数据持久化**：命中记录落 SQLite，重启不丢
- **守护自拉起**：程序挂了自动重启（VBS 守护循环 / Windows 计划任务两种方案）

## 界面预览

（此处放界面截图，见 `docs/` 目录）

## 快速开始

### 环境要求

- Windows（macOS / Linux 需自行调整启动脚本）
- Python 3.11+

### 安装与启动

```bash
git clone https://github.com/<你的用户名>/taopiaopiao-monitor.git
cd taopiaopiao-monitor

# 双击 start.bat（自动建虚拟环境、装依赖、启动服务、打开浏览器）
# 或手动：
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml
.venv\Scripts\python -m monitor
```

启动后浏览器自动打开 `http://127.0.0.1:8787`。

### 配置

复制 `config.example.yaml` 为 `config.yaml`：

```yaml
listen_port: 8787          # Web 端口
interval_seconds: 10       # 扫描间隔（秒），仅后台可改
price_whitelist_yuan:      # 票价白名单（元），仅后台可改
  - 69
  - 199
wecom:
  webhooks:                # 企业微信群机器人 Webhook，仅后台可改
    - https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的KEY
  wechat_id: ""            # 你的微信号（推送文案里引导加好友）
targets: []                # 监控目标，也可在网页里加
```

### 使用流程

1. **查 ShowID**：网页顶部选「全部城市」，输片名关键词（如「上海女儿」），点查询 → 点「使用」自动填入
2. **选城市和日期**：下拉选城市，日期选择器选日期
3. **保存**：下一轮扫描生效
4. **等推送**：命中后企业微信群收到通知，含影院、场次、票价

### 常用命令

| 操作 | 命令 |
|---|---|
| 启动 | 双击 `start.bat` |
| 停止 | 双击 `stop.bat` |
| 重启 | 双击 `restart.bat` |
| 查看状态 | `start.bat status` |

## 文档

- [扫描原理与接口说明](docs/扫描原理.md)：mtop H5 握手、签名算法、影院/排期接口字段、过滤逻辑

## 技术栈

- Python 3.11+ / asyncio / httpx
- 内置 HTTP 服务（无 Flask 依赖）
- SQLite 持久化
- 淘票票 mtop H5 开放接口（无需登录）

## 加群交流 & 赞助

这个工具完全免费开源。如果对你有帮助，欢迎：

- **加微信进通知群**：扫描下方二维码加我个人微信，拉你进「路演监控通知群」，群里会同步工具更新、共享热门影片 ShowID、解答使用问题

  ![微信二维码](assets/wechat-qr.png)

- **求赞助**：如果工具帮你抢到了心仪的路演票，欢迎请我喝杯咖啡 ☕

  ![收款码](assets/pay-qr.png)

## Star 历史

如果觉得有用，点个 Star ⭐ 是对我最大的鼓励。

## 许可证

[MIT](LICENSE) — 可自由使用、修改、商用，保留署名即可。
