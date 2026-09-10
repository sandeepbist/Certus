import './config/environment.js';

import fastify from 'fastify';
import websocket from '@fastify/websocket';
import multipart from '@fastify/multipart';
import corsPlugin from './plugins/cors.js';
import authPlugin from './plugins/auth.js';
import rateLimiterPlugin from './plugins/rateLimiter.js';
import { healthRoutes } from './routes/health.js';
import { chatRoutes } from './routes/chat.js';
import { documentRoutes } from './routes/documents.js';
import { traceRoutes } from './routes/traces.js';
import { automationRoutes } from './routes/automations.js';
import { memoryRoutes } from './routes/memories.js';
import { analyticsRoutes } from './routes/analytics.js';
import { toolRoutes } from './routes/tools.js';
import { exportRoutes } from './routes/export.js';
import { taskRoutes } from './routes/tasks.js';
import { graphRoutes } from './routes/graph.js';
import { dashboardRoutes } from './routes/dashboard.js';
import { notificationRoutes } from './routes/notifications.js';
import { searchRoutes } from './routes/search.js';
import { webhookRoutes } from './routes/webhooks.js';
import { embeddingGenerationRoutes } from './routes/embeddingGenerations.js';
import { handleWebSocketConnection } from './websocket/streamHandler.js';
import { NotificationBroker } from './websocket/notificationBroker.js';
import { handleNotificationConnection } from './websocket/notificationHandler.js';
import { RealtimeBroker } from './websocket/realtimeBroker.js';
import { handleRealtimeConnection } from './websocket/realtimeHandler.js';
import { assertInternalServiceConfiguration } from './utils/internalService.js';

assertInternalServiceConfiguration();

const port = Number(process.env.GATEWAY_PORT || process.env.PORT || 4000);
const host = '0.0.0.0';

export async function buildServer() {
  const app = fastify({
    logger: {
      level: process.env.LOG_LEVEL || 'info',
      transport:
        process.env.NODE_ENV !== 'production'
          ? {
              target: 'pino-pretty',
              options: {
                translateTime: 'HH:MM:ss Z',
                ignore: 'pid,hostname',
              },
            }
          : undefined,
    },
    requestIdHeader: 'x-request-id',
  });

  // Core Plugins
  await app.register(corsPlugin);
  await app.register(websocket, {
    options: {
      maxPayload: 64 * 1024,
      perMessageDeflate: false,
    },
  });
  await app.register(multipart, {
    limits: {
      files: 1,
      fields: 4,
      parts: 5,
      fileSize: 50 * 1024 * 1024,
      fieldSize: 8 * 1024,
    },
  });
  await app.register(authPlugin);
  await app.register(rateLimiterPlugin);

  const notificationBroker = new NotificationBroker(
    process.env.REDIS_URL || 'redis://localhost:6379',
    app.log,
  );
  const realtimeBroker = new RealtimeBroker(
    process.env.REDIS_URL || 'redis://localhost:6379',
    app.log,
  );
  app.addHook('onReady', async () => {
    notificationBroker.start();
    realtimeBroker.start();
  });
  app.addHook('onClose', async () => {
    await Promise.all([
      notificationBroker.stop(),
      realtimeBroker.stop(),
    ]);
  });

  // REST Routes
  await app.register(healthRoutes);
  await app.register(chatRoutes);
  await app.register(documentRoutes);
  await app.register(traceRoutes);
  await app.register(automationRoutes);
  await app.register(memoryRoutes);
  await app.register(analyticsRoutes);
  await app.register(toolRoutes);
  await app.register(exportRoutes);
  await app.register(taskRoutes);
  await app.register(graphRoutes);
  await app.register(dashboardRoutes);
  await app.register(notificationRoutes);
  await app.register(searchRoutes);
  await app.register(webhookRoutes);
  await app.register(embeddingGenerationRoutes);

  // WebSocket Streaming Route
  app.register(async (fastifyInstance) => {
    fastifyInstance.get('/ws/chat', { websocket: true }, (connection, req) => {
      const socket = (connection as any).socket || connection;
      handleWebSocketConnection(socket, req);
    });
    fastifyInstance.get('/ws/notifications', { websocket: true }, (connection, req) => {
      const socket = (connection as any).socket || connection;
      handleNotificationConnection(socket, req, notificationBroker);
    });
    fastifyInstance.get('/ws/realtime', { websocket: true }, (connection, req) => {
      const socket = (connection as any).socket || connection;
      handleRealtimeConnection(socket, req, realtimeBroker);
    });
  });

  return app;
}

async function start() {
  try {
    const server = await buildServer();
    await server.listen({ port, host });
    server.log.info(`🚀 Certus Gateway running on http://${host}:${port}`);
    server.log.info(`🔌 WebSocket streaming available on ws://${host}:${port}/ws/chat`);
    server.log.info(`🔔 Notification push available on ws://${host}:${port}/ws/notifications`);
    server.log.info(`📡 Live trace and document updates available on ws://${host}:${port}/ws/realtime`);

    const shutdown = async (signal: string) => {
      server.log.info(`Received ${signal}. Gracefully shutting down...`);
      await server.close();
      process.exit(0);
    };

    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('SIGTERM', () => shutdown('SIGTERM'));
  } catch (err) {
    console.error('Fatal error during Gateway startup:', err);
    process.exit(1);
  }
}

if (process.env.NODE_ENV !== 'test') {
  start();
}
