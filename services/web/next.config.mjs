import path from 'path';
import { fileURLToPath } from 'url';
import nextEnv from '@next/env';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const { loadEnvConfig } = nextEnv;

// Next treats services/web as its project directory; load the monorepo's single
// environment file so every service uses the same local configuration.
loadEnvConfig(
  path.join(__dirname, '../..'),
  process.env.NODE_ENV !== 'production',
  console,
  true,
);

const gatewayUrl = (process.env.GATEWAY_URL || 'http://localhost:4000').replace(/\/$/, '');
const configuredGatewayWebSocketUrl = process.env.NEXT_PUBLIC_GATEWAY_WS_URL?.replace(/\/$/, '');
const developmentGatewayWebSocketUrl = `${gatewayUrl.replace(/^http/, 'ws')}/ws`;

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  outputFileTracingRoot: path.join(__dirname, '../../'),
  env: {
    NEXT_PUBLIC_GATEWAY_WS_URL:
      configuredGatewayWebSocketUrl
      || (process.env.NODE_ENV !== 'production' ? developmentGatewayWebSocketUrl : ''),
  },
  // The repository-level ESLint command is the authoritative lint gate.
  eslint: {
    ignoreDuringBuilds: true,
  },
  async rewrites() {
    return [
      {
        source: '/api/gateway/:path*',
        destination: `${gatewayUrl}/api/:path*`,
      },
      {
        source: '/gateway/ws/:path*',
        destination: `${gatewayUrl}/ws/:path*`,
      },
    ];
  },
};

export default nextConfig;
