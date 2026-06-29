FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable

# Install web app dependencies (monorepo-aware, repo root as build context)
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/
RUN pnpm install --filter @schurfer/web --frozen-lockfile

COPY apps/web/ ./apps/web/
RUN pnpm --filter @schurfer/web build

FROM nginx:1.27-alpine
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -q -O /dev/null http://localhost/ || exit 1
