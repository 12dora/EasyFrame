FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/easy-enterprise/package.json ./packages/easy-enterprise/package.json
COPY apps/blank/package.json ./apps/blank/package.json
RUN corepack enable pnpm && \
    pnpm install --frozen-lockfile --ignore-scripts && \
    pnpm approve-builds sharp @swc/core @parcel/watcher unrs-resolver && \
    pnpm install --frozen-lockfile

FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=deps /app/apps/blank/node_modules ./apps/blank/node_modules
COPY --from=deps /app/packages/easy-enterprise/node_modules ./packages/easy-enterprise/node_modules
COPY . .
ARG INTERNAL_BACKEND_ORIGIN=http://blank-backend:8000
ENV INTERNAL_BACKEND_ORIGIN=$INTERNAL_BACKEND_ORIGIN
ENV NEXT_PUBLIC_ENTERPRISE_API_BASE_URL=""
ENV NEXT_PUBLIC_ENTERPRISE_DEMO_AUTH=false
RUN corepack enable pnpm && pnpm --dir apps/blank build

FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2 AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# 固定 UID/GID，避免宿主间用户名映射差异；standalone cache 也由该用户持有。
RUN addgroup --gid 10001 blank \
    && adduser --uid 10001 --ingroup blank --home /app --shell /sbin/nologin --disabled-password blank
COPY --chown=10001:10001 --from=builder /app/apps/blank/.next/standalone ./
COPY --chown=10001:10001 --from=builder /app/apps/blank/.next/static ./apps/blank/.next/static
EXPOSE 3000

USER 10001:10001

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=10 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(response => { if (!response.ok) process.exit(1) }).catch(() => process.exit(1))"]

CMD ["node", "apps/blank/server.js"]
