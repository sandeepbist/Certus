# Build from the repository root so the committed workspace lockfile is the
# only dependency authority: docker build -f services/gateway/Dockerfile .
FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS dependencies

WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY services/gateway/package.json ./services/gateway/package.json
COPY services/web/package.json ./services/web/package.json
RUN bun install --frozen-lockfile --filter gateway

FROM dependencies AS builder
COPY services/gateway/tsconfig.json ./services/gateway/tsconfig.json
COPY services/gateway/src/ ./services/gateway/src/
RUN bun run --filter gateway build

FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS production-dependencies
WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY services/gateway/package.json ./services/gateway/package.json
COPY services/web/package.json ./services/web/package.json
RUN bun install --frozen-lockfile --production --filter gateway

FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8 AS runner

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=4000

COPY --from=dependencies /usr/local/bin/bun /usr/local/bin/bun
COPY --from=dependencies /usr/lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1
COPY --from=dependencies /usr/lib/libstdc++.so.6.0.33 /usr/lib/libstdc++.so.6.0.33
RUN ln -s libstdc++.so.6.0.33 /usr/lib/libstdc++.so.6 \
    && addgroup -S certus \
    && adduser -S -G certus certus

COPY --from=production-dependencies --chown=certus:certus /app/node_modules ./node_modules
COPY --from=builder --chown=certus:certus /app/services/gateway/package.json ./services/gateway/package.json
COPY --from=builder --chown=certus:certus /app/services/gateway/dist ./services/gateway/dist

EXPOSE 4000

USER certus
CMD ["bun", "services/gateway/dist/server.js"]
