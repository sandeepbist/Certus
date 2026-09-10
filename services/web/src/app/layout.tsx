import type { Metadata, Viewport } from 'next';
import '../styles/globals.css';

export const metadata: Metadata = {
  title: 'Certus — Verifiable Intelligence & Intent-Assured Autonomous Execution',
  description:
    'Certus is a multi-tenant cognitive operating system uniting hybrid RAG, Neo4j GraphRAG, persistent memory, and verifiable intent contracts.',
  keywords: ['Certus', 'AI Operating System', 'GraphRAG', 'pgvector', 'LangGraph', 'Temporal', 'Intent Assurance', 'MCP'],
  authors: [{ name: 'Certus Core Team' }],
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <body className="antialiased bg-black text-zinc-100 selection:bg-white/20 selection:text-white min-h-screen">
        {children}
      </body>
    </html>
  );
}
