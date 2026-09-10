'use client';

import React, { useCallback, useEffect, useState, Suspense } from 'react';
import {
  Plus,
  Search,
  CheckCircle2,
  Trash2,
} from 'lucide-react';
import { gatewayFetch } from '@/lib/gateway-client';

interface TaskItem {
  id: string;
  title: string;
  description: string;
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
  priority: 'low' | 'medium' | 'high' | 'urgent';
  tags: string[];
  due_date?: string | null;
  version: number;
  created_at: string;
}

interface TaskStatusCounts {
  pending: number;
  in_progress: number;
  completed: number;
  cancelled: number;
}

function TasksContent() {
  const [tasks, setTasks] = useState<TaskItem[]>([]);

  const [filterStatus, setFilterStatus] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newDescription, setNewDescription] = useState('');
  const [newPriority, setNewPriority] = useState('medium');
  const [newTags, setNewTags] = useState('');
  const [newDueDate, setNewDueDate] = useState('');
  const [showAddModal, setShowAddModal] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [statusCounts, setStatusCounts] = useState<TaskStatusCounts>({
    pending: 0,
    in_progress: 0,
    completed: 0,
    cancelled: 0,
  });
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const loadTasks = useCallback(async (pageCursor?: string, signal?: AbortSignal) => {
    if (pageCursor) setIsLoadingMore(true);
    else setIsLoading(true);
    try {
      const query = new URLSearchParams({ limit: '50' });
      if (filterStatus !== 'all') query.set('status', filterStatus);
      if (searchQuery.trim()) query.set('q', searchQuery.trim());
      if (pageCursor) query.set('cursor', pageCursor);
      const response = await gatewayFetch(`/tasks?${query}`, { signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || data.detail || 'Tasks could not be loaded.');
      const page = Array.isArray(data.tasks) ? data.tasks as TaskItem[] : [];
      setTasks((current) => {
        if (!pageCursor) return page;
        const existingIds = new Set(current.map((task) => task.id));
        return [...current, ...page.filter((task) => !existingIds.has(task.id))];
      });
      setStatusCounts(data.status_counts || {
        pending: 0,
        in_progress: 0,
        completed: 0,
        cancelled: 0,
      });
      setNextCursor(data.pagination?.next_cursor || null);
      setErrorMessage(null);
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setErrorMessage(error instanceof Error ? error.message : 'Tasks could not be loaded.');
    } finally {
      if (pageCursor) setIsLoadingMore(false);
      else setIsLoading(false);
    }
  }, [filterStatus, searchQuery]);

  useEffect(() => {
    const controller = new AbortController();
    const debounce = window.setTimeout(() => {
      void loadTasks(undefined, controller.signal);
    }, 200);
    return () => {
      window.clearTimeout(debounce);
      controller.abort();
    };
  }, [loadTasks]);

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim()) return;

    const tagList = newTags.split(',').map((t) => t.trim()).filter(Boolean);

    try {
      setErrorMessage(null);
      const res = await gatewayFetch('/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: newTitle,
          description: newDescription,
          priority: newPriority,
          due_date: newDueDate ? new Date(newDueDate).toISOString() : null,
          tags: tagList,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.detail || 'Task could not be created.');
      await loadTasks();
      setNewTitle('');
      setNewDescription('');
      setNewTags('');
      setNewDueDate('');
      setShowAddModal(false);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Task could not be created.');
    }
  };

  const toggleTaskStatus = async (task: TaskItem) => {
    const nextStatus: TaskItem['status'] =
      task.status === 'completed'
        ? 'pending'
        : task.status === 'pending'
        ? 'in_progress'
        : 'completed';
    try {
      setErrorMessage(null);
      const response = await gatewayFetch(`/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: nextStatus, version: task.version }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || 'Task could not be updated.');
      await loadTasks();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Task could not be updated.');
    }
  };

  const deleteTask = async (task: TaskItem) => {
    try {
      setErrorMessage(null);
      const response = await gatewayFetch(`/tasks/${task.id}`, { method: 'DELETE' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Task could not be deleted.');
      await loadTasks();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'Task could not be deleted.');
    }
  };

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-4 border-b border-zinc-800">
        <div>
          <h1 className="text-xl font-semibold text-white tracking-tight">
            Tasks
          </h1>
          <p className="text-xs text-zinc-400 mt-0.5">
            Agent workflows and tasks created dynamically or via MCP Tools.
          </p>
        </div>

        <button
          onClick={() => setShowAddModal(true)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-white text-black hover:bg-zinc-200 text-xs font-medium transition-colors shrink-0"
        >
          <Plus className="w-3.5 h-3.5" />
          <span>New Task</span>
        </button>
      </div>

      {errorMessage && (
        <div className="rounded-lg border border-red-900/60 bg-red-950/30 px-3 py-2 text-xs text-red-300">
          {errorMessage}
        </div>
      )}

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Pending</span>
          <div className="text-xl font-semibold text-zinc-200 mt-1">
            {statusCounts.pending}
          </div>
        </div>
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">In Progress</span>
          <div className="text-xl font-semibold text-zinc-200 mt-1">
            {statusCounts.in_progress}
          </div>
        </div>
        <div className="p-4 rounded-xl bg-zinc-950 border border-zinc-800/80">
          <span className="text-xs text-zinc-400">Completed</span>
          <div className="text-xl font-semibold text-emerald-400 mt-1">
            {statusCounts.completed}
          </div>
        </div>
      </div>

      {/* Filter & Search */}
      <div className="flex flex-col sm:flex-row gap-2.5">
        <div className="relative flex-1">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search task text or exact tag..."
            className="w-full pl-9 pr-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
          />
        </div>

        <select
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
          className="bg-zinc-900 border border-zinc-800 text-xs text-zinc-300 rounded-lg px-2.5 py-2 focus:outline-none cursor-pointer"
        >
          <option value="all">All Statuses</option>
          <option value="pending">Pending</option>
          <option value="in_progress">In Progress</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
        </select>
      </div>

      {/* Task List */}
      <div className="space-y-2.5">
        {isLoading ? (
          <div className="p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            Loading tasks...
          </div>
        ) : tasks.length === 0 ? (
          <div className="p-6 text-center text-xs text-zinc-500 border border-dashed border-zinc-800 rounded-xl">
            No tasks found matching filter.
          </div>
        ) : (
          tasks.map((t) => (
            <div
              key={t.id}
              className={`p-3.5 rounded-xl border transition-colors flex items-center justify-between gap-3 ${
                t.status === 'completed'
                  ? 'bg-zinc-950/60 border-zinc-850 opacity-60'
                  : 'bg-zinc-950 border-zinc-800/80 hover:border-zinc-700'
              }`}
            >
              <div className="flex items-start gap-3 min-w-0">
                <button
                  onClick={() => toggleTaskStatus(t)}
                  className={`w-4 h-4 rounded border mt-0.5 flex items-center justify-center transition-colors shrink-0 ${
                    t.status === 'completed'
                      ? 'bg-emerald-500 border-emerald-500 text-black'
                      : 'border-zinc-700 hover:border-zinc-500 bg-zinc-900'
                  }`}
                >
                  {t.status === 'completed' && <CheckCircle2 className="w-3.5 h-3.5" />}
                </button>
                <div className="min-w-0">
                  <h3
                    className={`text-xs font-medium text-white ${
                      t.status === 'completed' ? 'line-through text-zinc-500' : ''
                    }`}
                  >
                    {t.title}
                  </h3>
                  {t.description && (
                    <p className="text-[11px] text-zinc-400 mt-0.5 line-clamp-1">{t.description}</p>
                  )}
                  {t.due_date && (
                    <p className="text-[10px] text-zinc-500 mt-1">
                      Due {new Date(t.due_date).toLocaleString()}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
                    <span className="px-1.5 py-0.2 rounded text-[10px] font-mono uppercase bg-zinc-900 border border-zinc-800 text-zinc-400">
                      {t.priority}
                    </span>
                    {t.tags.map((tag, idx) => (
                      <span
                        key={idx}
                        className="px-1.5 py-0.2 rounded bg-zinc-900 text-[10px] text-zinc-400 border border-zinc-800"
                      >
                        #{tag}
                      </span>
                    ))}
                  </div>
                </div>
              </div>

              <div className="text-right shrink-0 flex items-center gap-2">
                <span className="text-[10px] font-mono capitalize text-zinc-400">
                  {t.status.replace('_', ' ')}
                </span>
                <button
                  onClick={() => deleteTask(t)}
                  className="p-1 rounded text-zinc-500 hover:text-red-400 transition-colors"
                  title="Delete task"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      {nextCursor && !isLoading && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void loadTasks(nextCursor)}
            disabled={isLoadingMore}
            className="px-3 py-1.5 rounded-lg border border-zinc-800 bg-zinc-900 text-xs text-zinc-300 hover:text-white disabled:opacity-50"
          >
            {isLoadingMore ? 'Loading...' : 'Load more'}
          </button>
        </div>
      )}

      {/* Add Task Modal */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="w-full max-w-md bg-zinc-950 border border-zinc-800 rounded-xl p-5 space-y-4">
            <h2 className="text-sm font-semibold text-white">
              Create New Task
            </h2>
            <form onSubmit={handleCreateTask} className="space-y-3">
              <div>
                <label className="text-xs text-zinc-400">Task Title</label>
                <input
                  type="text"
                  value={newTitle}
                  onChange={(e) => setNewTitle(e.target.value)}
                  placeholder="e.g. Optimize HNSW index parameters"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
                  required
                />
              </div>

              <div>
                <label className="text-xs text-zinc-400">Description</label>
                <textarea
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none resize-none h-20"
                  maxLength={5000}
                />
              </div>

              <div>
                <label className="text-xs text-zinc-400">Priority</label>
                <select
                  value={newPriority}
                  onChange={(e) => setNewPriority(e.target.value)}
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none cursor-pointer"
                >
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                  <option value="urgent">Urgent</option>
                </select>
              </div>

              <div>
                <label className="text-xs text-zinc-400">Due Date (optional)</label>
                <input
                  type="datetime-local"
                  value={newDueDate}
                  onChange={(e) => setNewDueDate(e.target.value)}
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 focus:outline-none"
                />
              </div>

              <div>
                <label className="text-xs text-zinc-400">Tags (comma-separated)</label>
                <input
                  type="text"
                  value={newTags}
                  onChange={(e) => setNewTags(e.target.value)}
                  placeholder="e.g. pgvector, storage, architecture"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-600"
                />
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowAddModal(false)}
                  className="px-3 py-1.5 rounded-lg bg-zinc-900 hover:bg-zinc-800 text-zinc-300 text-xs transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1.5 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium transition-colors"
                >
                  Create Task
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default function TasksPage() {
  return (
    <Suspense fallback={<div className="p-8 text-center text-xs text-zinc-500">Loading Tasks...</div>}>
      <TasksContent />
    </Suspense>
  );
}
