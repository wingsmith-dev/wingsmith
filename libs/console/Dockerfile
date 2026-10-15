FROM node:22.19.0-bookworm-slim AS build
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

FROM nginx:1.27-alpine

# Nothing about the plane is baked into the bundle. 40-hangar-config.sh writes config.js and the
# CSP from these at container start, so one image serves any plane. An empty base URL means the
# API answers on the console's own origin (behind the same ingress).
ENV HANGAR_API_BASE_URL="" \
    HANGAR_READ_ONLY="false"

# The router is client-side, so every path below / exists only once the bundle is running; the
# server config falls back to index.html for anything that is not a built file.
RUN rm -f /etc/nginx/conf.d/default.conf
COPY docker/default.conf.template docker/security-headers.conf.template /etc/nginx/hangar/
COPY docker/40-hangar-config.sh /docker-entrypoint.d/40-hangar-config.sh
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 8080
