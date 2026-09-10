# Build from the repository root so the committed workspace lockfile is the
# only dependency authority: docker build -f services/gateway/Dockerfile .
FROM oven/bun:1.4.0-alpine AS dependencies

WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY services/gateway/package.json ./services/gateway/package.json
COPY services/web/package.json ./services/web/package.json
RUN bun install --frozen-lockfile --filter gateway

FROM dependencies AS builder
COPY services/gateway/tsconfig.json ./services/gateway/tsconfig.json
COPY services/gateway/src/ ./services/gateway/src/
RUN bun run --filter gateway build

FROM oven/bun:1.4.0-alpine AS production-dependencies
WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
COPY services/gateway/package.json ./services/gateway/package.json
COPY services/web/package.json ./services/web/package.json
RUN bun install --frozen-lockfile --production --filter gateway

FROM oven/bun:1.4.0-alpine AS runner

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=4000

RUN addgroup -S certus && adduser -S -G certus certus

COPY --from=production-dependencies --chown=certus:certus /app/node_modules ./node_modules
COPY --from=builder --chown=certus:certus /app/services/gateway/package.json ./services/gateway/package.json
COPY --from=builder --chown=certus:certus /app/services/gateway/dist ./services/gateway/dist

EXPOSE 4000

USER certus
CMD ["bun", "services/gateway/dist/server.js"]
