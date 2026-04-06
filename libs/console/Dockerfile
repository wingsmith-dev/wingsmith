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

# Vite substitutes these at build time, not at container start: the output is a bundle of static
# files with the values already inlined, so an image built against one API is an image that only
# ever talks to that API. Changing where the console points is a rebuild, which is why they are
# build args rather than the environment entries they look like they should be.
ARG VITE_API_BASE_URL
ARG VITE_API_MODE
ARG VITE_API_PROJECT
ARG VITE_READ_ONLY
RUN pnpm run build

FROM nginx:1.27-alpine

# The router is client-side, so every path below / exists only once the bundle is running. Without
# the fallback, a reload on /clusters/abc is a 404 from nginx for a page the console can render
# perfectly well — the server is being asked for a file that was never meant to exist.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 8080
