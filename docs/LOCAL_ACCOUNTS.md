# Local Accounts — feature contract (v1)

Superadmin-managed local accounts for the framework host (blank_app), with permission grants
that reference the same permission registry exposed to EasyAuth (the app manifest /
permission catalog — "EasyAuth permission templates").

## Concepts

- **Local account** = `platform_accounts` row with `external_source IS NULL`. External
  (EasyAuth/OIDC-projected) accounts are NOT managed by this surface; their permissions keep
  coming from EasyAuth snapshots.
- **Admin** (`is_admin=true`, local, local auth enabled) keeps `ALL_PERMISSIONS` — unchanged.
- **Non-admin local account** permission resolution becomes:
  `BASELINE_SELF_SERVICE ∪ (stored local grants ∩ active catalog codes)`.
  - `BASELINE_SELF_SERVICE` (implicit, not stored): `auth.totp.create`, `auth.totp.advance`,
    `auth.passkey.view`, `auth.passkey.create`, `notification.center.view` — every active local
    account can manage its own password / 2FA / passkeys and see its notifications.
- **Grant storage**: JSON list column `local_permissions` on `platform_accounts`
  (default `[]`); filtered against the active permission catalog at resolution time.
- New permission codes in `FRAMEWORK_PERMISSIONS` (registered to EasyAuth manifest as well):
  - `accounts.local.view` (domain `accounts`, resource `accounts.local`, scope ALL, standard)
  - `accounts.local.manage` (same, risk `high`)

## Backend API (all under `/api/v1`, bearer auth)

Gating: list/detail need `accounts.local.view`; mutations need `accounts.local.manage`.
(Admins have both via ALL_PERMISSIONS; codes can also be granted to non-admins.)

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/local-accounts` | — | Local accounts only. Response `{data: LocalAccountSummary[], meta: {total}}`; supports `?search=` (username/email substring). |
| POST | `/local-accounts` | `{username, email?, password, mustChangePassword?=true, isAdmin?=false, permissions?=[]}` | 201 → detail. 409 on duplicate username. Password validated by the same shared password policy used by the self-service password change route. |
| GET | `/local-accounts/{id}` | — | Detail incl. `permissions` (stored grants), `baselinePermissions` (implicit). |
| PATCH | `/local-accounts/{id}` | `{email?, active?, isAdmin?, uiLocale?}` | Guards: cannot deactivate or demote yourself; cannot deactivate/demote the **last active local admin**. |
| DELETE | `/local-accounts/{id}` | — | Guards: not self, not last active local admin. Deletes dependent passkeys/notifications rows. |
| POST | `/local-accounts/{id}/password` | `{password, mustChangePassword?=true}` | Admin reset; revokes sessions (`sessions_revoked_at`). |
| PUT | `/local-accounts/{id}/permissions` | `{permissions: [codes]}` | Codes must be ⊆ active catalog codes, else 422. 422 for admin accounts (they hold ALL; storing grants would mislead). `BASELINE_SELF_SERVICE` codes are **stripped before persistence** (implicit-only invariant — never stored as explicit grants); the same stripping applies to `permissions` on create. The picker shows baseline codes as locked/implied, not as selectable grants. |
| DELETE | `/local-accounts/{id}/totp` | — | Admin rescue for lost 2FA: disables TOTP + clears secrets. |
| GET | `/local-accounts/permission-catalog` | — | Grantable catalog for the permission picker, gated on `accounts.local.view` (so delegated managers don't need `authz.integration.*`). Full shape: `{data: [{code, domain, resource, riskLevel, active}]}`, active codes only. |

`LocalAccountSummary`: `id, username, email, active, isAdmin, totpEnabled, passkeyCount,
mustChangePassword, permissionCount, createdAt`. Detail adds `uiLocale, permissions,
baselinePermissions`.

Every mutation writes a `PlatformAuditLog` entry (actor, action, target account id, changed keys —
never passwords/secrets).

Random password generation is **frontend-side** (crypto-strong, shown once with copy button);
backend only enforces the password policy.

Login: `authenticate_password` must accept any **active** local account (admin or not) under the
existing `BLANK_LOCAL_AUTH_MODE` rules; inactive accounts are rejected. `must_change_password`
forced-change flow and TOTP second factor apply as today.

## Frontend

- Blank host page `/[locale]/app/settings/accounts`, nav entry in the settings frame gated on
  `accounts.local.view` (label: 本地账户 / Local accounts).
- Surface implemented in EasyUI as a **new subpath export** `@easy-enterprise/ui/enterprise-local-accounts`
  (`EnterpriseLocalAccountsSurface`), **using antd** (`antd` becomes an optional peerDependency of
  EasyUI and a direct dependency of the blank app, version `^6.5.2`). It must NOT be re-exported from
  `./enterprise` (hosts without antd must not pull it into their bundles).
- Surface props follow the existing adapter/labels/permissions pattern
  (see `access-settings-surface.tsx`): `adapter` (API calls), `labels` (i18n strings from host),
  `permissions: {view, manage}`, `locale`.
- Features: antd Table list (badges for admin/2FA/disabled/must-change-password); create modal
  (antd Form: username/email/password with 随机生成 button + copy, mustChangePassword switch,
  isAdmin switch, permission picker); edit drawer (profile + activate/deactivate + delete confirm +
  password reset + TOTP rescue + permission editor). Permission picker groups catalog codes by
  domain (data from `GET /api/v1/local-accounts/permission-catalog` via adapter) — the same
  registry EasyAuth sees.
- Self-service password change & 2FA UIs already exist (`settings/security`) and apply to all local
  accounts unchanged.
