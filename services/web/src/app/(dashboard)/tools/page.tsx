'use client';

import React, { Suspense, useEffect, useState } from 'react';
import { AlertTriangle, Play, Terminal, Wrench } from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  items?: JsonSchema;
  anyOf?: JsonSchema[];
}

interface ToolAnnotations {
  title?: string;
  readOnlyHint?: boolean;
  destructiveHint?: boolean;
  idempotentHint?: boolean;
}

interface ToolSchema {
  name: string;
  description?: string;
  inputSchema: {
    type: string;
    properties?: Record<string, JsonSchema>;
    required?: string[];
  };
  annotations?: ToolAnnotations;
}

function initialInputs(tool: ToolSchema): Record<string, string> {
  return Object.fromEntries(
    Object.entries(tool.inputSchema.properties || {}).map(([name, schema]) => [
      name,
      schema.default === undefined || schema.default === null
        ? ''
        : typeof schema.default === 'string'
          ? schema.default
          : JSON.stringify(schema.default),
    ]),
  );
}

function concreteSchema(schema: JsonSchema): JsonSchema {
  return schema.type
    ? schema
    : schema.anyOf?.find((candidate) => candidate.type && candidate.type !== 'null') || schema;
}

function parseInput(name: string, rawValue: string, schema: JsonSchema): unknown {
  const resolvedSchema = concreteSchema(schema);
  const value = rawValue.trim();
  if (resolvedSchema.type === 'integer') {
    const parsed = Number(value);
    if (!Number.isInteger(parsed)) throw new Error(`${name} must be an integer.`);
    return parsed;
  }
  if (resolvedSchema.type === 'number') {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error(`${name} must be a number.`);
    return parsed;
  }
  if (resolvedSchema.type === 'boolean') return value === 'true';
  if (resolvedSchema.type === 'array') {
    if (value.startsWith('[')) {
      const parsed: unknown = JSON.parse(value);
      if (!Array.isArray(parsed)) throw new Error(`${name} must be a JSON array.`);
      return parsed;
    }
    return value.split(',').map((item) => item.trim()).filter(Boolean);
  }
  if (resolvedSchema.type === 'object') return JSON.parse(value);
  return rawValue;
}

function ToolsContent() {
  const [tools, setTools] = useState<ToolSchema[]>([]);
  const [selectedTool, setSelectedTool] = useState<ToolSchema | null>(null);
  const [paramInputs, setParamInputs] = useState<Record<string, string>>({});
  const [executionResult, setExecutionResult] = useState<unknown>(null);
  const [isExecuting, setIsExecuting] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [protocol, setProtocol] = useState('MCP');

  useEffect(() => {
    async function fetchTools() {
      try {
        const response = await gatewayFetch('/tools/list');
        const data: {
          tools?: ToolSchema[];
          protocol?: string;
          detail?: string;
          message?: string;
        } = await response.json();
        if (!response.ok) throw new Error(data.detail || data.message || 'Tools could not be loaded.');
        const loadedTools = data.tools || [];
        setTools(loadedTools);
        setProtocol(data.protocol || 'MCP');
        if (loadedTools.length > 0) {
          setSelectedTool(loadedTools[0]);
          setParamInputs(initialInputs(loadedTools[0]));
        }
      } catch (error) {
        setLoadError(error instanceof Error ? error.message : 'Tools could not be loaded.');
      } finally {
        setIsLoading(false);
      }
    }
    void fetchTools();
  }, []);

  const selectTool = (tool: ToolSchema) => {
    setSelectedTool(tool);
    setParamInputs(initialInputs(tool));
    setExecutionResult(null);
  };

  const handleExecute = async () => {
    if (!selectedTool) return;
    setIsExecuting(true);
    setExecutionResult(null);

    try {
      const required = new Set(selectedTool.inputSchema.required || []);
      const argumentsPayload: Record<string, unknown> = {};
      for (const [name, schema] of Object.entries(selectedTool.inputSchema.properties || {})) {
        const rawValue = paramInputs[name] || '';
        if (!rawValue.trim()) {
          if (required.has(name)) throw new Error(`${name} is required.`);
          continue;
        }
        argumentsPayload[name] = parseInput(name, rawValue, schema);
      }

      const response = await gatewayFetch('/tools/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tool_name: selectedTool.name,
          arguments: argumentsPayload,
        }),
      }, { profile: 'processing' });
      const data: unknown = await response.json();
      if (!response.ok) {
        const errorData = data as { detail?: unknown; message?: string };
        const detail = typeof errorData.detail === 'string'
          ? errorData.detail
          : errorData.message || 'Tool execution failed.';
        throw new Error(detail);
      }
      setExecutionResult(data);
    } catch (error) {
      setExecutionResult({ error: error instanceof Error ? error.message : 'Tool execution failed.' });
    } finally {
      setIsExecuting(false);
    }
  };

  const selectedProperties = selectedTool?.inputSchema.properties || {};
  const selectedRequired = new Set(selectedTool?.inputSchema.required || []);
  const selectedWritesData = selectedTool?.annotations?.readOnlyHint === false;

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">Tools</h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Authenticated workspace tools backed by the standard Streamable HTTP protocol.
          </p>
        </div>

        <span className="px-2.5 py-1 rounded-md bg-zinc-900 border border-zinc-800 text-zinc-300 text-xs font-mono flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${loadError ? 'bg-red-500' : 'bg-emerald-500'}`} />
          {protocol}
        </span>
      </div>

      {loadError && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {loadError}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="space-y-2">
          <h2 className="text-[11px] font-medium text-zinc-500 uppercase tracking-wider font-mono">
            Registry ({tools.length})
          </h2>
          <div className="space-y-1.5">
            {isLoading && (
              <div className="p-5 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
                Loading registered tools...
              </div>
            )}
            {!isLoading && !loadError && tools.length === 0 && (
              <div className="p-5 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
                No tools are registered.
              </div>
            )}
            {tools.map((tool) => (
              <button
                type="button"
                key={tool.name}
                onClick={() => selectTool(tool)}
                className={`w-full text-left p-3 rounded-lg border transition-colors ${
                  selectedTool?.name === tool.name
                    ? 'bg-zinc-900 border-zinc-700 text-white font-medium'
                    : 'bg-zinc-950 border-zinc-800 text-zinc-300 hover:bg-zinc-900/50'
                }`}
              >
                <div className="flex items-center gap-2">
                  <Wrench className="w-3.5 h-3.5 text-zinc-400 shrink-0" />
                  <div className="min-w-0">
                    <h3 className="text-xs font-mono text-zinc-100 truncate">{tool.name}</h3>
                    <p className="text-[11px] text-zinc-400 mt-0.5 truncate">{tool.description}</p>
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>

        <div className="lg:col-span-2 space-y-4">
          {selectedTool ? (
            <div className="p-5 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-4">
              <div>
                <div className="flex items-center gap-2">
                  <span className="px-1.5 py-0.5 rounded bg-zinc-900 border border-zinc-800 text-zinc-400 text-[10px] font-mono">
                    MCP Tool
                  </span>
                  <span className={`text-[10px] font-mono ${selectedWritesData ? 'text-amber-400' : 'text-emerald-400'}`}>
                    {selectedWritesData ? 'writes workspace data' : 'read-only'}
                  </span>
                </div>
                <h2 className="text-base font-semibold text-white mt-1.5 font-mono">{selectedTool.name}</h2>
                <p className="text-xs text-zinc-400 mt-0.5">{selectedTool.description}</p>
              </div>

              {selectedWritesData && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-900/50 bg-amber-950/20 px-3 py-2 text-[11px] text-amber-300">
                  <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                  Running this tool creates persistent workspace data. Review the arguments before executing it.
                </div>
              )}

              <div className="space-y-2.5">
                <h3 className="text-xs font-medium text-zinc-300">Arguments</h3>
                <div className="space-y-2.5">
                  {Object.entries(selectedProperties).map(([paramName, schema]) => {
                    const resolvedSchema = concreteSchema(schema);
                    return <div key={paramName}>
                      <div className="flex items-center justify-between mb-1">
                        <label className="text-xs font-mono text-zinc-300">
                          {paramName}
                          {selectedRequired.has(paramName) && <span className="text-red-400 ml-0.5">*</span>}
                        </label>
                        <span className="text-[10px] text-zinc-500 font-mono">{resolvedSchema.type || 'string'}</span>
                      </div>
                      {resolvedSchema.enum ? (
                        <select
                          value={paramInputs[paramName] || ''}
                          onChange={(event) => setParamInputs({ ...paramInputs, [paramName]: event.target.value })}
                          className="w-full px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none focus:border-zinc-600 font-mono"
                        >
                          {resolvedSchema.enum.map((option) => (
                            <option key={String(option)} value={String(option)}>{String(option)}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type="text"
                          value={paramInputs[paramName] || ''}
                          onChange={(event) => setParamInputs({ ...paramInputs, [paramName]: event.target.value })}
                          placeholder={resolvedSchema.type === 'array' ? 'comma-separated values or JSON array' : `Enter ${paramName}...`}
                          className="w-full px-3 py-1.5 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 font-mono"
                        />
                      )}
                      {resolvedSchema.description && <p className="text-[10px] text-zinc-500 mt-1">{resolvedSchema.description}</p>}
                    </div>
                  })}
                </div>

                <button
                  type="button"
                  onClick={handleExecute}
                  disabled={isExecuting}
                  className="w-full py-2 rounded-lg bg-white hover:bg-zinc-200 disabled:opacity-40 text-black text-xs font-medium transition-colors flex items-center justify-center gap-1.5 mt-2"
                >
                  {isExecuting ? 'Executing tool...' : (
                    <><Play className="w-3 h-3" />Execute Tool</>
                  )}
                </button>
              </div>

              {executionResult !== null && (
                <div className="space-y-1.5 pt-2 border-t border-zinc-800">
                  <h3 className="text-xs font-medium text-zinc-400 flex items-center gap-1">
                    <Terminal className="w-3.5 h-3.5 text-zinc-400" />
                    Result
                  </h3>
                  <pre className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 text-[11px] font-mono text-emerald-400 overflow-x-auto max-h-72">
                    {JSON.stringify(executionResult, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          ) : !isLoading && (
            <div className="p-8 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
              Select a registered tool to inspect its schema.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ToolsPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading tools...</div>}>
      <ToolsContent />
    </Suspense>
  );
}
