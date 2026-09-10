'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowLeft, Save } from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

export default function NewAutomationPage() {
  const router = useRouter();
  const [name, setName] = useState('');
  const [triggerType, setTriggerType] = useState('on_document_uploaded');
  const [conditionExpr, setConditionExpr] = useState("tags.includes('urgent')");
  const [actionType, setActionType] = useState('summarize_and_create_task');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    setIsSubmitting(true);
    try {
      const response = await gatewayFetch('/automations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name,
          trigger_type: triggerType,
          condition_expression: conditionExpr,
          action_type: actionType,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || 'Automation could not be saved.');
      router.push('/automations');
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Automation could not be saved.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="p-6 max-w-2xl mx-auto space-y-6">
      <div>
        <Link
          href="/automations"
          className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-white mb-2 transition-colors"
        >
          <ArrowLeft className="w-3 h-3" />
          Back to Automations
        </Link>
        <h1 className="text-xl font-semibold text-white tracking-tight">
          Create Automation Rule
        </h1>
        <p className="text-xs text-zinc-400 mt-0.5">
          Define a validated document trigger and a durable Temporal workflow action.
        </p>
      </div>

      {errorMessage && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {errorMessage}
        </div>
      )}

      <form onSubmit={handleSave} className="p-5 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-4">
        <div>
          <label className="text-xs text-zinc-300">Rule Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Ingest & Auto-Summarize Security Audits"
            className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
            required
          />
        </div>

        <div>
          <label className="text-xs text-zinc-300">Trigger Event</label>
          <select
            value={triggerType}
            onChange={(e) => setTriggerType(e.target.value)}
            className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none cursor-pointer"
          >
            <option value="on_document_uploaded">on_document_uploaded (When document uploaded)</option>
          </select>
        </div>

        <div>
          <label className="text-xs text-zinc-300">Validated Condition</label>
          <input
            type="text"
            value={conditionExpr}
            onChange={(e) => setConditionExpr(e.target.value)}
            placeholder="e.g. tags.includes('urgent')"
            className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600 font-mono"
          />
        </div>

        <div>
          <label className="text-xs text-zinc-300">Action Workflow</label>
          <select
            value={actionType}
            onChange={(e) => setActionType(e.target.value)}
            className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none cursor-pointer"
          >
            <option value="summarize_and_create_task">summarize_and_create_task (Summarization + Task)</option>
            <option value="notify">notify (In-app notification)</option>
          </select>
        </div>

        <div className="flex justify-end gap-2 pt-3 border-t border-zinc-850">
          <Link
            href="/automations"
            className="px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-300 text-xs transition-colors"
          >
            Cancel
          </Link>
          <button
            type="submit"
            disabled={isSubmitting}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-white hover:bg-zinc-200 disabled:opacity-40 text-black text-xs font-medium transition-colors"
          >
            <Save className="w-3.5 h-3.5" />
            <span>{isSubmitting ? 'Saving...' : 'Save Rule'}</span>
          </button>
        </div>
      </form>
    </div>
  );
}
