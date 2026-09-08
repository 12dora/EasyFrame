# EasyFrame

企业应用框架站(原 EasyTrade monorepo 的 blank 宿主 + 共享企业平台内核),自 EasyTrade `bd8616a3` 剥离为独立仓。

本仓有两个用途:

1. **可直接跑起来的框架站**(blank 宿主):一套自带认证/授权/设置页的最小企业应用,也是开新宿主时的复制模板。
2. **共享内核的发布源**:EasyTrade、EasyCustoms 以 git submodule 消费这里的 `backend/` 与 EasyUI。

技术栈:FastAPI(Python ≥ 3.11)+ PostgreSQL 16 + Next.js 16 / React 19,全部通过 docker compose 起。

## 组成

- `backend/enterprise_platform/` — 共享企业平台内核(认证/授权/OIDC/passkey/通知/健康/限流),被 EasyTrade、EasyCustoms 以 git submodule 方式消费
- `backend/blank_app/` — 框架站宿主(FastAPI + 独立 alembic 迁移),同时是新宿主的复制模板
- `backend/platform_tests/` — 平台契约测试(消费方镜像也会打包运行,作为接收框架更新时的安全门禁)
- `frontend/apps/blank/` — 框架站前端(Next.js);`frontend/packages/easy-enterprise` 为 [EasyUI](https://github.com/12dora/EasyUI) submodule
- `docker-compose.yml` — 框架站独立栈(项目名保持 `easytrade-blank`,沿用既有数据卷)

## 跑起来

```bash
git submodule update --init            # 拉取 EasyUI(frontend/packages/easy-enterprise)
cp .env.blank.example .env.blank       # .env.blank 已 gitignore,按注释填密钥
make blank-up                          # 构建并起栈,--wait 到健康为止
```

`.env.blank` 里至少要改这几项才起得来:`BLANK_POSTGRES_PASSWORD`(≥16 字符,不能保留
`replace-with-` 这类示例值,`blank-preflight` 会拦)、`BLANK_JWT_SECRET`(≥32 字节)、
`BLANK_ADMIN_PASSWORD`、`BLANK_INTEGRATION_ENVELOPE_KEY`(`Fernet.generate_key()` 生成)。
`BLANK_LOCAL_AUTH_MODE` 默认 `disabled`(生产 OIDC-only),本地试跑要改成 `development`。

起来之后:

- 前端 `http://localhost:3100`(`BLANK_FRONTEND_PORT`,容器内 3000),登录页在 `/zh-CN/login`
- 后端 `http://127.0.0.1:8100`(`BLANK_BACKEND_PORT`,容器内 8000),健康检查 `/health`,API 前缀 `/api/v1`

只改前端时也可以脱离容器跑 dev server:`pnpm --dir frontend blank:dev`(端口 3001,需要后端已在跑)。

## 消费方接线(EasyTrade / EasyCustoms)

宿主仓在 `backend/easyframe` 挂载本仓 submodule,Dockerfile 从
`easyframe/backend/{enterprise_platform,blank_app,platform_tests}` COPY 到镜像内**原路径**
(`/app/enterprise_platform` 等),因此 python import、alembic 路径、测试脚本全部不变。

接收框架更新:宿主仓执行
`git submodule update --remote backend/easyframe && git submodule update --remote frontend/packages/easy-enterprise`,
跑门禁后提交指针。

## 常用命令

```bash
make blank-up                          # 起框架站栈(需 .env.blank)
make blank-down                        # 停栈
make blank-check                       # 全量门禁:隔离栈 + 镜像审计 + platform_tests + typecheck + e2e
make blank-typecheck                   # = pnpm --dir frontend blank:typecheck
make blank-e2e                         # = pnpm --dir frontend blank:e2e(Playwright)
```

`make blank-check` 不碰你本地的 `.env.blank` 和数据卷:它用 `.env.blank.example` + 随机端口 +
临时 project name 起一套隔离栈,跑完 `down --volumes` 清掉。这是提交前和吸收框架更新时的门禁。

## 文档

- [docs/LOCAL_ACCOUNTS.md](docs/LOCAL_ACCOUNTS.md) — 本地账户管理(超管建号、权限授予、2FA 救援)的接口与行为契约
- [docs/EASYAUTH_EVENTS.md](docs/EASYAUTH_EVENTS.md) — EasyAuth 授权变更 webhook、快照拉取规则与宿主镜像清单
- [docs/GENERAL_SETTINGS.md](docs/GENERAL_SETTINGS.md) — 通用设置（名称/副标题/页脚/标志）线合同、页脚 shim 与宿主镜像清单

## 许可

[Apache License 2.0](LICENSE)。可自由使用、修改、商用与再分发,含显式专利授权;分发时需保留
许可证与版权声明,并标注你所做的修改。子模块 [EasyUI](https://github.com/12dora/EasyUI) 同样以
Apache-2.0 授权。
