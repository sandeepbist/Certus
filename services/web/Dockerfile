# Build from the repository root so the committed workspace lockfile is the
# only dependency authority: docker build -f services/web/Dockerfile .
FROM oven/bun:1.4.2-alpine@sha256:d888c0ae6c86d7866ff10c5aafdd9077b36aee6455b33dd270fb93c0dd5cef6f AS dependencies

WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY services/gateway/package.json ./services/gateway/package.json
COPY services/web/package.json ./services/web/package.json
RUN bun install --frozen-lockfile --filter web

FROM dependencies AS builder

WORKDIR /app
COPY services/web/ ./services/web/

ENV NEXT_TELEMETRY_DISABLED=1
RUN bun run --filter web build

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6 AS runner

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

COPY --from=dependencies /usr/local/bin/bun /usr/local/bin/bun
COPY --from=dependencies /usr/lib/libgcc_s.so.1 /usr/lib/libgcc_s.so.1
COPY --from=dependencies /usr/lib/libstdc++.so.6.0.33 /usr/lib/libstdc++.so.6.0.33
RUN ln -s libstdc++.so.6.0.33 /usr/lib/libstdc++.so.6 \
    && addgroup -S certus \
    && adduser -S -G certus certus

WORKDIR /app
COPY --from=builder --chown=certus:certus /app/services/web/.next/standalone ./
COPY --from=builder --chown=certus:certus /app/services/web/.next/static ./services/web/.next/static

EXPOSE 3000

USER certus
WORKDIR /app/services/web
CMD ["bun", "server.js"]
