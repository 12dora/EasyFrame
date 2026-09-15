# Authentik 身份变化与会话同步

EasyFrame 提供三套互补机制：RP 发起注销把浏览器送回应用登录页，后通道注销撤销已签发的本地会话，
静默复检识别上游会话消失或切换账号。
共享入口是 `enterprise_platform.oidc.create_oidc_router`，blank 宿主的实现位于
`backend/blank_app/oidc_adapter.py`。EasyTrade、EasyCustoms 更新子模块后必须完成下列宿主接线。

## RP 发起注销

浏览器登出必须先拿到 Authentik end-session 的 POST 表单字段，再 `POST /auth/logout`。
不要把 `id_token` 放进宿主 JWT 或前端存储；Authentik 校验 `id_token_hint` 时不查 `exp`，
因此登录时落下的原始 ID token 一直可用来结束上游会话。

`POST /api/v1/auth/oidc/end-session` 需要仍然有效的宿主 Bearer JWT。把 blank 的零参 callable
（`_local_accounts_current_user`，内部读 `account_adapter.current_user()`）传给
`create_oidc_router(..., current_user_dependency=...)`；框架用 `Depends(current_user_dependency)`
注册，FastAPI 会注入 `Request` 与子依赖。不要传入已经包好的 `Depends(...)` 对象，也不要把
`/auth/logout` 装配链上的 `recovery_user` 误当成「同一个对象」塞进来。依赖若抛 `AuthError`，
由宿主 app 的异常处理器转成 401（blank 已有）。JSON 体全部可选：

```json
{ "returnTo": "/zh-CN/login" }
```

`returnTo` 必须是以单个 `/` 开头的相对路径，最长 500；`//`、scheme、反斜杠、控制字符、空白
（含 U+0020 与 NBSP）、非 ASCII、百分号编码的斜杠（`%2f` / `%2F`）、`.` / `..` 路径段返回 422。
`frontend_base_url` 只填站点 origin（尾斜杠可选，不要带 `/zh-CN` 或 `/en`）。拼接
`post_logout_redirect_uri` 时会去掉尾斜杠，并剥掉末尾的 locale 段，避免 `/zh-CN/zh-CN/login`。
成功 200：

```json
{
  "url": "https://auth.example/application/o/<slug>/end-session/",
  "method": "POST",
  "fields": {
    "id_token_hint": "<raw id_token>",
    "post_logout_redirect_uri": "https://app.example/zh-CN/login"
  }
}
```

`returnTo` 缺省时省略 `post_logout_redirect_uri`。OIDC 未启用、本地账号、或从未经 OIDC 登录
（没有存储 hint）时返回 `404 {"code":"NO_END_SESSION"}`。配置不完整（缺 `frontend_base_url` 等）
返回 409，JSON 含 `detail` / `kind`，与后通道一致。响应与日志都不得打印 token。
`GET /auth/oidc/status` 仍返回无 query 的 `endSessionUrl`，供未升级的前端回退。
未实现 `store_id_token` / `end_session_hint` 的旧宿主在构造 router 时打一条 warning；回调跳过
存 token（登录仍成功），end-session 按无 hint 返回 404，前端走 GET 回落。

后通道注销只更新 `sessions_revoked_at`，**不得**清除 `oidc_id_token`。`revoke_sessions` 同样不动 hint。

每次 OIDC 回调（含静默复检）在 `upsert_identity` 之后调用 `OidcHost.store_id_token`。
超过 8 KiB 的 token 不落库。迁移前已登录的会话没有 hint，需要下一次交互登录或静默复检才会写入；
此前 end-session 返回 404，前端走 GET 回落。`end_session_hint` 对本地账号或空值返回 `None`。

### 宿主接入清单

子模块更新后，每个宿主必须镜像 blank：

- `platform_accounts.oidc_id_token`（`Text`，可空）及对应 alembic 迁移
- `OidcHost.store_id_token(account_id, id_token)` / `end_session_hint(account_id)`
- `create_oidc_router(..., current_user_dependency=_local_accounts_current_user)`：传入 **plain callable**
  （blank 的零参 `account_adapter.current_user` 包装即可）。框架以 `Depends(current_user_dependency)`
  注册，FastAPI 负责注入。不要传入 `Depends(...)` 对象。
- **先**把 CSP `form-action` 加上 Authentik 源（`form-action 'self' https://auth.jiefakj.com`），
  **然后才**升 EasyUI 指针并补适配器的 `apiUrl` / `authToken`。现有宿主都是 `form-action 'self'`；
  少这一源，浏览器会静默拦掉 end-session 隐藏表单（不抛异常、页面停在原地，本地已登出而
  Authentik 会话还活着）。接线顺序与措辞见 EasyUI
  [`docs/LOGOUT.md`](../frontend/packages/easy-enterprise/docs/LOGOUT.md)「先给 CSP 的 `form-action` 放行 Authentik 源」。
- 升 EasyUI 指针（前端在登出时先调 end-session，再 `revoke`，再 POST 表单到 Authentik）
- Authentik Provider 增加 `redirect_uri_type: logout` 的登录页 URI（regex `https://<host>/(zh-CN|en)/login`）；本仓不改线上 Provider
- `frontend_base_url` 只填 origin，不要复制带 `/zh-CN` 的登录 URL

## 后通道注销

默认接口为 `POST /api/v1/auth/oidc/backchannel-logout`，由 Authentik 服务端发送
`Content-Type: application/x-www-form-urlencoded`，表单字段为 `logout_token`。
请求不依赖浏览器登录、Authorization header 或 CSRF token。宿主或网关若统一要求登录或 CSRF 校验，
必须对这一个接口豁免；保留 JWT 验签。GET 返回 405。

框架复用登录的 JWKS 获取、缓存和未知 `kid` 刷新逻辑，仅接受 RS256 / ES256，校验签名、
`iss`、`aud`、必填 `iat` / `jti`，以及存在时的 `exp`。`events` 必须包含
`http://schemas.openid.net/event/backchannel-logout`，`nonce` 必须不存在。
无 `exp` 时，`iat` 不得早于当前时间 300 秒；未来或无效的 `iat` 被拒绝。
必须提供非空 `sub`；仅有 `sid` 的请求返回 400，说明 `sid-only logout is unsupported`。

`OidcHost` 新增**必需方法**：

```python
def revoke_sessions_by_subject(self, sub: str) -> int:
    ...
```

宿主必须按上游来源及 subject 查找本地账号，在数据库事务中更新 `sessions_revoked_at`，返回实际匹配并
撤销会话的**账号数**；未知 subject 返回 0。blank 宿主使用
`external_source == "authentik" AND external_user_id == sub`，与 `upsert_identity` 的映射保持一致。
不得按姓名、邮箱或单独的 `sid` 匹配，也不得撤销其他身份来源下同名 subject 的账号。

EasyFrame 宿主使用无状态 JWT，不按 `sid` 单独撤销。一次通知会撤销该 subject 对应账号的所有旧会话。
宿主必须在每次受保护 API 请求中检查账号撤销时间；blank 已使用 JWT 中的 `session_started_at`
与数据库时间比较。因此通知处理成功后，旧 JWT 的下一次 API 请求就会被拒绝，无需等待 JWT 到期。
数据库异常应暴露为服务端失败，不得吞掉异常后返回成功。

成功（含未知账号、重复通知）统一返回 `200 {}`；无效请求返回
`400 {"error":"invalid_request","error_description":"..."}`，均带 `Cache-Control: no-store`。
OIDC 未启用返回 404，配置不完整返回 409，JSON 含 `detail` / `kind`。
每次成功调用记录一行 subject 和撤销账号数，不记录令牌。

框架没有共享缓存，因此**不保存 jti 防重放**，重复的有效通知仍执行撤销并返回 200；若重复通知晚于新登录，
新签发的会话也可能被撤销。不要把进程内集合当作多实例的重放保护。
验证规则参考 [OpenID Connect Back-Channel Logout](https://openid.net/specs/openid-connect-backchannel-1_0.html#Validation)。
本实现按共享契约允许近期且不带 `exp` 的令牌，并要求 `sub`。

## 静默身份复检

`GET /api/v1/auth/oidc/status` 新增 `silentAuthorizePath`，其值为 `authorizePath + "?silent=1"`。
宿主应读取返回路径，不硬编码 API 前缀。静默入口沿用授权码、PKCE、state、nonce 校验，
在签名状态 cookie 中记录 `silent: true`，向 Authentik 的授权请求追加 `prompt=none`。
静默状态使用独立的 `<state_cookie_name>_silent` cookie；`locale`、`next`、cookie 路径、安全属性和 TTL 遵循普通登录规则。

宿主前端必须新增公开页面 `/login/oidc-silent`，按现有语言路由挂载，例如
`/zh-CN/login/oidc-silent` 和 `/en/login/oidc-silent`。可通过
`OidcRouteConfig.frontend_silent_path` 修改页面路径；其仍会添加语言前缀。
完成地址使用配置的 `frontend_base_url`，结果只放在 URL fragment：

| 条件 | 完成片段 |
|---|---|
| 上游当前账号登录成功 | `#outcome=authenticated&token=<本地会话JWT>&account=<本地账号ID>` |
| `login_required`（无上游会话）、`access_denied`（上游策略拒绝应用） | `#outcome=logged_out&kind=<上述错误>` |
| `interaction_required`、`consent_required`（上游已登录但需要交互，保留本地会话）、其他 provider 错误、授权码交换失败、ID token 无效、账号停用等 | `#outcome=error&kind=<错误种类>` |

所有片段值均经过 URL 编码。不要把 `error_description` 当成前端错误类型；框架不向静默页面透传它。
回调优先选择验签后标记为静默且 state 匹配的 cookie，否则检查交互 cookie；仅删除匹配事务的 cookie。
两者均不匹配时，有可信静默 cookie 则返回 `outcome=error`，保留未完成事务的 cookie。有效签名的过期 cookie 仅用于选择静默错误页，不能通过有效期校验完成登录。
缺失、签名错误或用途错误的 cookie 无法证明是静默流程，会走普通登录错误页，父页面按超时处理。
OIDC 未配置时回调返回 404/409 JSON，并仅清除匹配事务的 cookie。普通登录继续使用原来的完成页和 `next` 片段。

静默页面读取 fragment 后，使用 `window.parent.postMessage(payload, window.location.origin)`
通知父窗口；消息结构由宿主前端统一定义。页面不得把 token 写入日志或分析服务。
父窗口必须同时验证 `event.origin === window.location.origin`、`event.source === iframe.contentWindow`
和消息字段，再移除 iframe。不要接受其他窗口或跨域消息。

父页面处理规则：

- `authenticated`：比较 `account` 与当前本地用户 ID；保存新 token。相同账号视为未变化；
  账号变化时清理原用户状态并刷新页面，使权限与数据重新加载。
- `logged_out`：清除本地会话，跳转登录页，`next` 使用当前站内路径。
- `error` 或 15 秒超时：恢复既有行为，不展示新的错误提示；由 401 触发时回到既有会话过期处理。

只对经 Authentik 建立的会话运行检查，本地密码或本地管理员会话跳过。
触发时机为 SPA 启动、页面重新可见（最多每 60 秒一次）、页面可见期间每 5 分钟一次，
以及 API 返回 401 时（在弹出会话过期提示前检查）。同一时刻只能有一次检查；
静默流程与普通交互登录使用独立 cookie，可并发进行。多个标签页的静默检查仍可能互相覆盖，
此时返回 `error`，下次定时检查重试。401 检查中若身份变化则刷新，
若已退出则跳登录，其余情况交还既有 UI。

## 页面与网关响应头

仅对实际的静默完成页路径（含语言前缀）允许同源嵌入，并设置：

```http
X-Frame-Options: SAMEORIGIN
Content-Security-Policy: frame-ancestors 'self'
Cache-Control: no-store
```

若站点已有 CSP，合并该路径的 `frame-ancestors` 指令，不能同时遗留会阻止 iframe 的
`DENY` 或 `frame-ancestors 'none'`。不要全站放宽 frame 策略。
RP 发起注销还会向 Authentik origin POST 隐藏表单：全站 CSP 的 `form-action` 必须在 `'self'`
之外包含该源（生产是 `https://auth.jiefakj.com`），且必须排在启用 EasyUI 登出适配器成员之前。
只加这一个源，不要写 `form-action *`。详见 EasyUI
[`docs/LOGOUT.md`](../frontend/packages/easy-enterprise/docs/LOGOUT.md)。
后端静默重定向已设置 `no-store`；宿主仍须为最终 HTML 页面独立配置这些响应头。
还需确认 Authentik 授权导航可在宿主 iframe 内完成；若上游响应头阻止嵌入，检查会超时。
当前部署的应用与 `auth.jiefakj.com` 同属 HTTPS 的 `jiefakj.com` 站点，使用 SameSite=Lax；
换到跨站部署后须重新验证浏览器 cookie 和 iframe 限制，不能假定本契约自动适用。

## Authentik 运维配置与验收

在每个应用的 Authentik OAuth2/OIDC Provider 中设置：

- Logout method：`backchannel`。
- Logout URI：`<公开 API base>/auth/oidc/backchannel-logout`。API base 必须包含宿主挂载前缀，
  默认是 `https://<API域名>/api/v1`；不能填写前端静默页。
- 原有登录 Redirect URI 继续使用 `<公开 API base>/auth/oidc/callback`，静默流程复用它。

若 API 通过对应站点的 `/api/v1` 同源发布，配置值为：

| 应用站点 | Logout URI |
|---|---|
| `etrade.jiefakj.com` | `https://etrade.jiefakj.com/api/v1/auth/oidc/backchannel-logout` |
| `tradedata.jiefakj.com` | `https://tradedata.jiefakj.com/api/v1/auth/oidc/backchannel-logout` |

使用独立 API 域名的宿主必须替换成实际公开 API 域名。确保 Authentik 服务端可访问这些地址，
网关不会将请求重定向到登录页或要求浏览器 CSRF cookie。保持现有 issuer、client ID 和 JWKS 配置一致。
本仓代码提交不会修改线上 Provider 设置。

验收时先用账号 A 登录应用，在 Authentik 注销后确认后通道响应 200，旧 JWT 的下一次 API 请求为 401；
再于 Authentik 登录账号 B，刷新应用或触发复检，确认父页面切换到 B 的 token、账号和权限。
同时验证本地管理员会话不会触发复检、无效 logout_token 不撤销会话，以及两个语言版本的完成页都可同源嵌入。
