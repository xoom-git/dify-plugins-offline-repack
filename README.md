# Dify 插件离线重打包工具（dify-offline-repack）

解决「官方/第三方 Dify 插件拷入**零外网**的局域网 Dify（1.16+/1.17，plugin_daemon `0.6.10-local`，python 3.12）后无法安装」的问题：把插件声明的 Python 依赖**完整打进 `.difypkg`**，使安装期完全离线。

## 怎么用（两种形态）

### 1. Web 页面（推荐交付形态）—— 上传 → 重打包 → 下载

```powershell
# 启动（本仓库 dev venv 已装依赖）
& E:\DIFY_PLUGIN\.venv-dev\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8010
# 当前已在 http://127.0.0.1:8010 运行
```

页面上传官方 `.difypkg` → 选架构（amd64+arm64 单包/单架构）→ 后端依次执行
**解析 → uv 锁定(linux/cp312) → 按 PEP425 标签抓双架构 wheel(含 sha256 校验) → 注入 `wheels/`+离线依赖配置 → 重新打包 → 离线闭包自检**，
完成后页面提供下载与报告。详见 `docs/04-操作与使用指南.md`。

### 2. CLI

```powershell
& E:\DIFY_PLUGIN\.venv-dev\Scripts\python.exe -m dify_offline_repack repack 插件.difypkg `
    -o out --arch x86_64,aarch64
& E:\DIFY_PLUGIN\.venv-dev\Scripts\python.exe -m dify_offline_repack verify out\xxx-offline.difypkg
```

## 已验证结果（本机实测）

| 项 | 结果 |
|---|---|
| 官方 webscraper → 离线包（amd64） | ✅ 40 依赖锁定并内嵌，离线闭包自检通过（16.4 MB） |
| 同上（amd64 + arm64 单包） | ✅ 双架构 wheel 齐全（29.3 MB，sha256 与 Web 下载一致） |
| daemon 等价安装模拟（`uv pip install --no-index --find-links wheels … linux/cp312`） | ✅ 全部依赖离线装成 |
| Web 整链路（上传→任务→下载→报告） | ✅ e2e 通过 |
| 第二个官方插件 langgenius/agent（requirements+pyproject 并存） | ✅ 归一化打包 11.4MB，自检通过 |
| 第三个官方插件 langgenius/openai（model 类） | ✅ 打包 13.6MB；真容器断网安装通过 |
| 自签安全路径（keygen/sign/signverify） | ✅ 与官方 0.6.10 二进制**双向交叉验证通过**；可验证市场现行官方签名包 |
| 真容器离线安装（plugin_daemon:0.6.10-local 镜像内 `--network none`，webscraper/agent/openai） | ✅ 复刻 daemon 环境初始化：uv venv→`--no-index` 安装→import 冒烟全部通过 |
| 批量 `batch`（多包、失败隔离、summary） | ✅ webscraper+agent 全过（含索引抓取重试修复） |
| Web 容器化 | ✅ Dockerfile 真实构建成功，容器内上传→打包→下载 e2e 通过 |
| Web 自签开关（安全路径接入页面） | ✅ sign=true 作业自动自签，公钥验签通过 |
| 离线 sdist 构建机制（sdist-only 出路） | ✅ 容器内 `--no-index` 从 sdist+后端 wheel 离线构建验证通过（引擎集成 P2） |
| 2026 生态兼容（cp312 守卫） | ✅ 锁定后审计+自动降钉至仍发 cp312 的版本；支持 abi3 / py2.py3 通用轮子 |
| 官方插件矩阵 | ✅ 7 个官方插件（tools×4 / agent×1 / model×2 类）全部离线打包通过；duckduckgo 等真容器抽查通过 |
| sdist-only 依赖（odfpy 等） | ✅ 引擎 sdist 后备：无 wheel 依赖自动带 sdist+构建后端，真容器断网构建并 import 通过 |
| Dify 完整栈 daemon HTTP 安装编排 e2e | ⏳ 需整栈（api/postgres/redis）环境执行 |

## 目标端（离线 Dify）部署

`.env` 增加（详见 `docs/01` §FR-8 与 `docs/04`）：

```ini
FORCE_VERIFYING_SIGNATURE=false     # 或启用“自签+第三方公钥”安全路径（规划中）
PLUGIN_MAX_PACKAGE_SIZE=524288000   # 默认 50MB 限制（上传+解压双限）
NGINX_CLIENT_MAX_BODY_SIZE=500M
```
然后 `docker compose up -d plugin_daemon nginx`，在 Dify 插件页 **本地文件安装** 上传离线包。

## 目录

```
dify-offline-repack/   引擎（python 包+CLI）：解析/锁定/wheel/注入/打包/校验
dify-offline-web/      Web 应用（FastAPI + 单页）：上传/任务队列/下载/报告
docs/                  01 需求规格 · 02 技术调研与方案 · 03 验证记录 · 04 操作指南
web_001/               官方 webscraper 样例（输入/实验）
out-test/ out-dual/    CLI 产物样例；.research/ 调研与临时产物（不入库）
```

## 文档

- `docs/01-需求规格说明书.md`：背景/根因、FR 清单（含 FR-W 网页形态）、验收用例、风险
- `docs/02-技术调研与总体方案.md`：daemon 安装机制、离线钩子、方案选型（源码级证据）
- `docs/03-验证记录.md`：机制与端到端实测（V1–V14，含官方二进制签名互验、真容器离线安装）
- `docs/04-操作与使用指南.md`：Web/CLI/签名操作与目标端部署
- `docs/05-验收与部署指南.md`：自检/端到端验收步骤与用例映射
- `e2e/container-offline-check.ps1`：一键容器级离线校验（`--network none`）

## 已知限制与下一步（M3/M4）

- sdist-only 依赖需预构建 wheel（当前明确报错）；pyproject 型插件当前归一化为 requirements 安装路径；
- 自签 + 第三方公钥安全路径（`keygen`/`sign`）与批量清单在规划中；
- 真 daemon 容器 e2e 需在可访问 Docker Hub 的环境执行。
