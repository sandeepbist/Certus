# Build from the repository root so the committed workspace lockfile is the
# only dependency authority: docker build -f services/web/Dockerfile .
FROM oven/bun:1.4.0-alpine@sha256:07235578f79ef8c6f97d94aee7938e76f5cdba5f21ae5dbfdd3d3d38058437eb AS dependencies

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

FROM oven/bun:1.4.0-alpine@sha256:07235578f79ef8c6f97d94aee7938e76f5cdba5f21ae5dbfdd3d3d38058437eb AS runner

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

RUN addgroup -S certus && adduser -S -G certus certus

WORKDIR /app
COPY --from=builder --chown=certus:certus /app/services/web/.next/standalone ./
COPY --from=builder --chown=certus:certus /app/services/web/.next/static ./services/web/.next/static

EXPOSE 3000

USER certus
WORKDIR /app/services/web
CMD ["bun", "server.js"]
