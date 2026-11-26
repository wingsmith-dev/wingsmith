FROM node:26.13.0-bookworm-slim@sha256:add5c17c14752c54d2f3d519ec46a3815684d41c33f198bcb09fb998f7dcbf68 AS build
WORKDIR /src

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable && corepack prepare pnpm@10.15.1 --activate

COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store \
    pnpm install --frozen-lockfile

COPY . .
RUN pnpm run build

FROM nginx:1.27.5-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

# Nothing about the plane is baked into the bundle. 40-wingsmith-config.sh writes config.js and the
# CSP from these at container start, so one image serves any plane. An empty base URL means the
# API answers on the console's own origin (behind the same ingress).
ENV WINGSMITH_API_BASE_URL="" \
    WINGSMITH_READ_ONLY="false" \
    WINGSMITH_DEMO_PASSWORD=""

# The router is client-side, so every path below / exists only once the bundle is running; the
# server config falls back to index.html for anything that is not a built file.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY docker/default.conf.template docker/security-headers.conf.template /etc/nginx/wingsmith/
COPY docker/40-wingsmith-config.sh /docker-entrypoint.d/40-wingsmith-config.sh
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 8080
