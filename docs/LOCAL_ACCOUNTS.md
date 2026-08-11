# Local Accounts

Superadmin-managed local accounts for the framework host (blank_app), with permission grants
that reference the same permission registry exposed to EasyAuth (the app manifest /
permission catalog — "EasyAuth permission templates").

Shipped and verified against the code below. Source of truth:

| Piece | File |
|---|---|
| Router + schemas + permission codes | `backend/enterprise_platform/local_accounts.py` |
| Behavior (guards, audit, resolution) | `backend/blank_app/adapters.py` (`_local_permissions`, `_validate_grant_codes`, `_guard_local_account_*`) |
| Permission registration / manifest | `backend/blank_app/authz_api.py` (`FRAMEWORK_PERMISSIONS`) |
| Column + migration | `backend/blank_app/models.py`, `backend/blank_app/alembic/versions/0003_local_permissions.py` |
| Mount point (`/api/v1/local-accounts`) | `backend/blank_app/main.py` |
| Contract tests | `backend/platform_tests/test_local_accounts.py`, `frontend/apps/blank/tests/local-accounts.spec.ts` |

## Concepts

- **Local account** = `platform_accounts` row with `external_source IS NULL`. External
  (EasyAuth/OIDC-projected) accounts are NOT managed by this surface; their permissions keep
  coming from EasyAuth snapshots. Requests for a non-local id return 404.
- **Admin** (`is_admin=true`, local, local auth enabled) keeps `ALL_PERMISSIONS` — unchanged.
- **Non-admin local account** permission resolution is:
  `BASELINE_SELF_SERVICE ∪ (stored local grants ∩ active catalog codes)`.
  - `BASELINE_SELF_SERVICE` (implicit, not stored): `auth.totp.create`, `auth.totp.advance`,
    `auth.passkey.view`, `auth.passkey.create`, `notification.center.view` — every active local
    account can manage its own password / 2FA / passkeys and see its notifications.
- **Grant storage**: JSON list column `local_permissions` on `platform_accounts`
  (default `[]`); filtered against the active permission catalog at resolution time, so
  deactivating a catalog code silently drops it from effective permissions without rewriting rows.
- Permission codes in `FRAMEWORK_PERMISSIONS` (registered to the EasyAuth manifest as well):
  - `accounts.local.view` (domain `accounts`, resource `accounts.local`, scope ALL, risk `standard`)
  - `accounts.local.manage` (same, risk `high`)

## Backend API (all under `/api/v1`, bearer auth)

Gating: list/detail/catalog need `accounts.local.view`; mutations need `accounts.local.manage`.
(Admins have both via ALL_PERMISSIONS; codes can also be granted to non-admins.)
Every route additionally rejects a caller whose own `must_change_password` is set
(403 `PASSWORD_CHANGE_REQUIRED`) — finish your forced password change before managing accounts.

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/local-accounts` | — | Local accounts only. Response `{data: LocalAccountSummary[], meta: {total}}`; supports `?search=` (username/email substring). |
| POST | `/local-accounts` | `{username, email?, password, mustChangePassword?=true, isAdmin?=false, permissions?=[]}` | 201 → detail. 409 on duplicate username. 422 on blank username, on unknown/inactive permission codes, and on `isAdmin=true` together with a non-empty `permissions` (admins hold ALL; storing grants would mislead). Password validated by the same shared policy as the self-service change route (`PasswordValue`: 8–128 chars). |
| GET | `/local-accounts/{id}` | — | Detail incl. `permissions` (stored grants), `baselinePermissions` (implicit). |
| PATCH | `/local-accounts/{id}` | `{email?, active?, isAdmin?, uiLocale?}` | Guards: 403 on deactivating or demoting **yourself**; 422 on deactivating/demoting the **last active local admin**. Promoting to admin clears any stored grants. |
| DELETE | `/local-accounts/{id}` | — | 204. Guards: 403 on self, 422 on last active local admin. Also deletes the account's passkeys, notifications and permission-snapshot rows. |
| POST | `/local-accounts/{id}/password` | `{password, mustChangePassword?=true}` | Admin reset; revokes sessions (`sessions_revoked_at`). |
| PUT | `/local-accounts/{id}/permissions` | `{permissions: [codes]}` | Replaces the stored grant list. Codes must be ⊆ active catalog codes, else 422. 422 for admin accounts. `BASELINE_SELF_SERVICE` codes are **stripped before persistence** (implicit-only invariant — never stored as explicit grants); the same stripping applies to `permissions` on create. The picker shows baseline codes as locked/implied, not as selectable grants. |
| DELETE | `/local-accounts/{id}/totp` | — | Admin rescue for lost 2FA: disables TOTP, clears both the active and pending secrets, and revokes sessions. Returns the updated detail. |
| GET | `/local-accounts/permission-catalog` | — | Grantable catalog for the permission picker, gated on `accounts.local.view` (so delegated managers don't need `authz.integration.*`). Shape `{data: [{code, domain, resource, riskLevel, active}]}`, active codes only. |

`LocalAccountSummary`: `id, username, email, active, isAdmin, totpEnabled, passkeyCount,
mustChangePassword, permissionCount, createdAt`. Detail adds `uiLocale, permissions,
baselinePermissions`.

Every mutation writes a `PlatformAuditLog` entry (actor, action `accounts.local.*`, target account
id, changed keys — never passwords/secrets).

Random password generation is **frontend-side** (`generatePolicyPassword`, `crypto.getRandomValues`,
shown once with a copy button); the backend only enforces the password policy.

Login: `authenticate_password` accepts any **active** local account (admin or not) under the
existing `BLANK_LOCAL_AUTH_MODE` rules; inactive accounts are rejected. The
`must_change_password` forced-change flow and the TOTP second factor apply unchanged.

## Frontend

- Blank host page `/[locale]/app/settings/accounts`, nav entry in the settings frame gated on
  `accounts.local.view` (label: 本地账户 / Local accounts).
- Surface implemented in EasyUI as the subpath export `@easy-enterprise/ui/enterprise-local-accounts`
  (`EnterpriseLocalAccountsSurface`), **using antd** (`antd` is an optional peerDependency of
  EasyUI and a direct dependency of the blank app, pinned `6.5.2`). It is deliberately NOT
  re-exported from `./enterprise` (hosts without antd must not pull it into their bundles).
- Surface props follow the existing adapter/labels/permissions pattern
  (see `access-settings-surface.tsx`): `adapter` (API calls), `labels` (i18n strings from host),
  `permissions: {view, manage}`, `locale`.
- Features: antd Table list (badges for admin/2FA/disabled/must-change-password); create modal
  (antd Form: username/email/password with 随机生成 button + copy, mustChangePassword switch,
  isAdmin switch, permission picker); edit drawer (profile + activate/deactivate + delete confirm +
  password reset + TOTP rescue + permission editor). Permission picker groups catalog codes by
  domain (data from `GET /api/v1/local-accounts/permission-catalog` via adapter) — the same
  registry EasyAuth sees.
- Self-service password change & 2FA UIs live in `settings/security` and apply to all local
  accounts unchanged.
