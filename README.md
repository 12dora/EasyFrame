# EasyFrame

企业应用框架站(原 EasyTrade monorepo 的 blank 宿主 + 共享企业平台内核),自 EasyTrade `bd8616a3` 剥离为独立仓。

## 组成

- `backend/enterprise_platform/` — 共享企业平台内核(认证/授权/OIDC/passkey/通知/健康/限流),被 EasyTrade、EasyCustoms 以 git submodule 方式消费
- `backend/blank_app/` — 框架站宿主(FastAPI + 独立 alembic 迁移),同时是新宿主的复制模板
- `backend/platform_tests/` — 平台契约测试(消费方镜像也会打包运行,作为接收框架更新时的安全门禁)
- `frontend/apps/blank/` — 框架站前端(Next.js);`frontend/packages/easy-enterprise` 为 [EasyUI](https://github.com/12dora/EasyUI) submodule
- `docker-compose.yml` — 框架站独立栈(项目名保持 `easytrade-blank`,沿用既有数据卷)

## 消费方接线(EasyTrade / EasyCustoms)

宿主仓在 `backend/easyframe` 挂载本仓 submodule,Dockerfile 从
`easyframe/backend/{enterprise_platform,blank_app,platform_tests}` COPY 到镜像内**原路径**
(`/app/enterprise_platform` 等),因此 python import、alembic 路径、测试脚本全部不变。

接收框架更新:宿主仓执行
`git submodule update --remote backend/easyframe && git submodule update --remote frontend/packages/easy-enterprise`,
跑门禁后提交指针。

## 常用命令

```bash
git submodule update --init            # 拉取 EasyUI
make blank-up                          # 起框架站栈(需 .env.blank)
make blank-check                       # 全量门禁:隔离栈 + 镜像审计 + platform_tests + typecheck + e2e
pnpm --dir frontend blank:typecheck
```
