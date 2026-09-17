# EasyFrame

企业应用框架站(原 EasyTrade monorepo 的 blank 宿主 + 共享企业平台内核),自 EasyTrade `bd8616a3` 剥离为独立仓。

本仓有两个用途:

1. **可直接跑起来的框架站**(blank 宿主):一套自带认证/授权/设置页的最小企业应用,也是开新宿主时的复制模板。
2. **共享内核的发布源**:EasyTrade、EasyCustoms 以 git submodule 消费这里的 `backend/` 与 EasyUI。

技术栈:FastAPI(Python ≥ 3.11)+ PostgreSQL 16 + Next.js 16 / React 19,全部通过 docker compose 起。

## 组成

- `backend/enterprise_platform/` — 共享企业平台内核(认证/授权/OIDC/passkey/通知/健康/限流/拼音人名检索),被 EasyTrade、EasyCustoms 以 git submodule 方式消费
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

## 拼音人名检索

`enterprise_platform.pinyin` 把目录人员的全拼 / 首字母检索做成框架能力,公开名与 EasyLearning 本地实现相同,宿主可改成再导出。

宿主接入三步:

1. **模型混入** `PinyinNameMixin`(声明顺序 `class DirectoryUser(PinyinNameMixin, HostBase)`)。模型必须已有 `name` 列;`@validates("name")` 会在构造和后续赋值时写入 `name_pinyin` / `name_pinyin_initials`。
2. **迁移加列**:`name_pinyin`、`name_pinyin_initials` 均为 `VARCHAR(128) NOT NULL DEFAULT ''`,并建索引(mixin 声明了 `index=True`)。已有行按 `pinyin_full` / `pinyin_initials` 回填。
3. **列表查询**用 `person_name_match(keyword, Model.name, Model.name_pinyin, Model.name_pinyin_initials)`:姓名列始终做转义 ILIKE 子串;仅纯 ASCII 字母数字查询才再匹配两列拼音。其它关键字过滤可复用 `ilike_contains`;内存过滤用 `matches_person_query`。

运行时依赖 `pypinyin==0.55.0`(已写入 `backend/pyproject.toml` 与 `requirements.blank*.txt`)。

## 表格(列表页)

列表页的所有约定都在 EasyUI 里,宿主**不自己接** antd `Table`、`ConfigProvider`、URL 查询状态
与列装饰器,只提供路由、文案与行内操作。完整 API 与契约见
[EasyUI `src/README.md` 的「表格」一节](frontend/packages/easy-enterprise/src/README.md)。

blank 模板已经把三处接线做好,复制成新宿主时原样保留:

- `frontend/apps/blank/app/globals.css` — 导入顺序 `tailwindcss` → `@source` EasyUI src →
  `@easy-enterprise/ui/theme.css` → `@easy-enterprise/ui/table.css`(表头不折行等 token 表达不了的规则)
- `frontend/apps/blank/components/antd-provider.tsx` — antd 环境只在 `BlankShell` 里包一层,
  按站点语言 `next/dynamic` 加载 EasyUI 的 `provider-zh` / `provider-en`(`ssr: true`);
  页面里**不要**再嵌套带 `cssVar` 的 `ConfigProvider`,页面级主题走 `token` / `components`
- `frontend/apps/blank/lib/table-query.ts` — 唯一的 Next 适配层:
  `useTableQuery(config)` = `useTableQueryWith(config, { pathname, search, replace })`,
  并把 `useLocalTableQuery`(对话框里不写地址栏的表格)与 kit 类型一并转出,页面统一从这里 import

写一张表:`const CONFIG = {...} as const satisfies TableQueryConfig`(必须是模块常量或
`useMemo`,否则每渲染都会重建列、把用户刚打开的漏斗关掉)→ `useTableQuery(CONFIG)` →
`searchColumn` / `filterColumn` / `sortColumn` / `withEllipsis` 装饰列 → `DataTable`。
可运行的样板在 `frontend/apps/blank/components/examples/table-example.tsx`
(路由 `/[locale]/app/examples/table`,导航里的「示例」项),**新宿主接入真实列表后请连同
`app/[locale]/app/examples`、`components/examples`、`lib/messages.ts` 的 `examples` 文案块与
导航里的 examples 分组一起删掉**。

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

- 上文「拼音人名检索」— 宿主目录模型混入 `PinyinNameMixin`、迁移两列、列表查询走 `person_name_match`
- [docs/LOCAL_ACCOUNTS.md](docs/LOCAL_ACCOUNTS.md) — 本地账户管理(超管建号、权限授予、2FA 救援)的接口与行为契约
- [docs/EASYAUTH_EVENTS.md](docs/EASYAUTH_EVENTS.md) — EasyAuth 授权变更 webhook、快照拉取规则与宿主镜像清单
- [docs/GENERAL_SETTINGS.md](docs/GENERAL_SETTINGS.md) — 通用设置（名称/副标题/页脚/标志）线合同、页脚 shim 与宿主镜像清单
- [docs/PERMISSION_ONBOARDING.md](docs/PERMISSION_ONBOARDING.md) — 零授权账号引导页、`GET /auth/session` 与宿主接线清单
- [docs/SHELL_PERCEIVED_LOADING.md](docs/SHELL_PERCEIVED_LOADING.md) — 外壳身份的感知加载（只等 `/auth/me`、本标签页快照、空闲复查）与宿主接入清单
- [docs/SHELL_MOBILE.md](docs/SHELL_MOBILE.md) — 外壳的手机形态（一条头部栏、页脚传两遍、列表换卡片）与宿主镜像清单

## 许可

[Apache License 2.0](LICENSE)。可自由使用、修改、商用与再分发,含显式专利授权;分发时需保留
许可证与版权声明,并标注你所做的修改。子模块 [EasyUI](https://github.com/12dora/EasyUI) 同样以
Apache-2.0 授权。
