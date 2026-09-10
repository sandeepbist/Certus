'use client';

import React, { useEffect, useState } from 'react';
import {
  AlertCircle,
  Building,
  CheckCircle2,
  Copy,
  Download,
  Key,
  Laptop,
  RefreshCw,
  Shield,
  Trash2,
  User,
  WandSparkles,
  Webhook,
  Send,
  Power,
} from 'lucide-react';

import { authClient, useSession } from '@/lib/auth-client';
import { gatewayFetch } from '@/lib/gateway-client';

type Tab = 'profile' | 'workspace' | 'api-keys' | 'security' | 'preferences' | 'webhooks' | 'data';

type SettingsRecord = {
  organization_id: string;
  organization_name: string;
  organization_slug: string;
  role: string;
  plan: string | null;
  max_documents: number | null;
  max_storage_bytes: string | null;
  committed_documents: string;
  reserved_documents: string;
  committed_original_bytes: string;
  reserved_original_bytes: string;
  max_token_budget_daily: number | null;
  theme: string;
  default_model: string;
  default_chunk_strategy: string;
  notification_email: boolean;
  notification_in_app: boolean;
  notification_digest: boolean;
};

type ManagedApiKey = {
  id: string;
  name: string | null;
  start: string | null;
  enabled: boolean;
  request_count: number;
  last_request: string | null;
  expires_at: string | null;
  created_at: string;
  scopes: string[];
};

type ManagedSession = {
  id: string;
  token: string;
  userAgent?: string | null;
  ipAddress?: string | null;
  createdAt: string | Date;
  updatedAt: string | Date;
  expiresAt: string | Date;
};

type ExportNotice = {
  export_id: string;
  status: 'ready';
  file_name: string;
  archive_size_bytes: number;
  record_counts: Record<string, number>;
  expires_at: string;
  download_url: string;
};

type Enrollment = {
  totpURI: string;
  backupCodes: string[];
};

type ManagedWebhook = {
  id: string;
  name: string;
  url: string;
  events: string[];
  secret_hint: string | null;
  is_enabled: boolean;
  last_triggered_at: string | null;
  failure_count: number;
  last_error: string | null;
  version: number;
  delivery_count: number;
  success_count: number;
};

type WebhookDelivery = {
  id: string;
  event_type: string;
  response_status: number | null;
  success: boolean;
  error_message: string | null;
  duration_ms: number | null;
  attempt_number: number;
  attempted_at: string;
};

const inputClass = 'w-full mt-1 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-800 text-xs text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-600';
const panelClass = 'p-5 rounded-xl bg-zinc-950 border border-zinc-800/80 space-y-4';

function SettingsContent() {
  const { data: session, refetch: refetchSession } = useSession();
  const [activeTab, setActiveTab] = useState<Tab>('profile');
  const [settings, setSettings] = useState<SettingsRecord | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [generativeProviderConfigured, setGenerativeProviderConfigured] = useState(false);
  const [apiKeys, setApiKeys] = useState<ManagedApiKey[]>([]);
  const [sessions, setSessions] = useState<ManagedSession[]>([]);
  const [webhooks, setWebhooks] = useState<ManagedWebhook[]>([]);
  const [supportedWebhookEvents, setSupportedWebhookEvents] = useState<string[]>([]);
  const [webhookDeliveries, setWebhookDeliveries] = useState<Record<string, WebhookDelivery[]>>({});
  const [webhookDeliveryCursors, setWebhookDeliveryCursors] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState('');
  const [newKeyName, setNewKeyName] = useState('');
  const [newKeyScope, setNewKeyScope] = useState('read');
  const [newKeyExpiry, setNewKeyExpiry] = useState(90);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [keyBusy, setKeyBusy] = useState(false);

  const [securityPassword, setSecurityPassword] = useState('');
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [totpCode, setTotpCode] = useState('');
  const [securityBusy, setSecurityBusy] = useState(false);

  const [exportNotice, setExportNotice] = useState<ExportNotice | null>(null);
  const [isExporting, setIsExporting] = useState(false);

  const [newWebhookName, setNewWebhookName] = useState('');
  const [newWebhookUrl, setNewWebhookUrl] = useState('');
  const [newWebhookEvents, setNewWebhookEvents] = useState<string[]>(['document_ready']);
  const [createdWebhookSecret, setCreatedWebhookSecret] = useState<string | null>(null);
  const [webhookBusy, setWebhookBusy] = useState(false);

  const loadSettings = async () => {
    const response = await fetch('/api/settings', { credentials: 'same-origin' });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.message || 'Settings could not be loaded.');
    setSettings(payload.settings as SettingsRecord);
    setAvailableModels(payload.available_models as string[]);
    setGenerativeProviderConfigured(Boolean(payload.generative_provider_configured));
  };

  const loadApiKeys = async () => {
    const response = await fetch('/api/api-keys', { credentials: 'same-origin' });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.message || 'API keys could not be loaded.');
    setApiKeys(payload.api_keys as ManagedApiKey[]);
  };

  const loadSessions = async () => {
    const result = await authClient.listSessions();
    if (result.error) throw new Error(result.error.message || 'Sessions could not be loaded.');
    setSessions((result.data || []) as ManagedSession[]);
  };

  const loadWebhooks = async () => {
    const response = await gatewayFetch('/webhooks');
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Webhooks could not be loaded.');
    setWebhooks(payload.webhooks || []);
    setSupportedWebhookEvents(payload.supported_events || []);
  };

  const loadWebhookDeliveries = async (webhookId: string, pageCursor?: string) => {
    const query = new URLSearchParams({ limit: '10' });
    if (pageCursor) query.set('cursor', pageCursor);
    const response = await gatewayFetch(`/webhooks/${webhookId}/deliveries?${query}`);
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Delivery history could not be loaded.');
    const page = payload.deliveries || [];
    setWebhookDeliveries((previous) => {
      if (!pageCursor) return { ...previous, [webhookId]: page };
      const current = previous[webhookId] || [];
      const ids = new Set(current.map((delivery) => delivery.id));
      return { ...previous, [webhookId]: [...current, ...page.filter((delivery: WebhookDelivery) => !ids.has(delivery.id))] };
    });
    setWebhookDeliveryCursors((previous) => ({
      ...previous,
      [webhookId]: payload.pagination?.next_cursor || null,
    }));
  };

  useEffect(() => {
    setDisplayName(session?.user.name || '');
  }, [session?.user.name]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([loadSettings(), loadApiKeys(), loadSessions(), loadWebhooks()])
      .catch((error) => {
        if (!cancelled) setPageError(error instanceof Error ? error.message : 'Settings could not be loaded.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const showNotice = (message: string) => {
    setNotice(message);
    setPageError(null);
  };

  const showError = (error: unknown, fallback: string) => {
    setPageError(error instanceof Error ? error.message : fallback);
    setNotice(null);
  };

  const saveProfile = async (event: React.FormEvent) => {
    event.preventDefault();
    const name = displayName.trim();
    if (!name || name.length > 100) {
      setPageError('Display name must contain 1 to 100 characters.');
      return;
    }
    const result = await authClient.updateUser({ name });
    if (result.error) {
      showError(new Error(result.error.message), 'Profile could not be updated.');
      return;
    }
    await refetchSession();
    showNotice('Profile updated.');
  };

  const savePreferences = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!settings) return;
    try {
      const response = await fetch('/api/settings', {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          theme: settings.theme,
          default_model: settings.default_model,
          default_chunk_strategy: settings.default_chunk_strategy,
          notification_email: settings.notification_email,
          notification_in_app: settings.notification_in_app,
          notification_digest: settings.notification_digest,
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || 'Preferences could not be updated.');
      setSettings(payload.settings as SettingsRecord);
      showNotice('Runtime preferences updated.');
    } catch (error) {
      showError(error, 'Preferences could not be updated.');
    }
  };

  const createApiKey = async (event: React.FormEvent) => {
    event.preventDefault();
    setKeyBusy(true);
    setCreatedSecret(null);
    try {
      const response = await fetch('/api/api-keys', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newKeyName,
          scopes: [newKeyScope],
          expires_in_days: newKeyExpiry,
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || 'API key could not be created.');
      setCreatedSecret(payload.api_key.key);
      setNewKeyName('');
      await loadApiKeys();
      showNotice('API key created. Copy it now; it cannot be recovered.');
    } catch (error) {
      showError(error, 'API key could not be created.');
    } finally {
      setKeyBusy(false);
    }
  };

  const revokeApiKey = async (keyId: string) => {
    setKeyBusy(true);
    try {
      const response = await fetch('/api/api-keys/' + encodeURIComponent(keyId), {
        method: 'DELETE',
        credentials: 'same-origin',
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.message || 'API key could not be revoked.');
      }
      await loadApiKeys();
      showNotice('API key revoked.');
    } catch (error) {
      showError(error, 'API key could not be revoked.');
    } finally {
      setKeyBusy(false);
    }
  };

  const beginTwoFactorEnrollment = async () => {
    setSecurityBusy(true);
    try {
      const result = await authClient.twoFactor.enable({
        password: securityPassword,
        method: 'totp',
      });
      if (result.error) throw new Error(result.error.message);
      if (!result.data || result.data.method !== 'totp') {
        throw new Error('Authenticator enrollment did not return setup details.');
      }
      setEnrollment({
        totpURI: result.data.totpURI,
        backupCodes: result.data.backupCodes,
      });
      setSecurityPassword('');
      showNotice('Scan or copy the setup URI, then verify one authenticator code.');
    } catch (error) {
      showError(error, 'Two-factor enrollment could not begin.');
    } finally {
      setSecurityBusy(false);
    }
  };

  const verifyTwoFactorEnrollment = async () => {
    setSecurityBusy(true);
    try {
      const result = await authClient.twoFactor.verifyTotp({ code: totpCode, trustDevice: true });
      if (result.error) throw new Error(result.error.message);
      setTotpCode('');
      setEnrollment(null);
      await refetchSession();
      showNotice('Two-factor authentication enabled.');
    } catch (error) {
      showError(error, 'Authenticator code could not be verified.');
    } finally {
      setSecurityBusy(false);
    }
  };

  const disableTwoFactor = async () => {
    setSecurityBusy(true);
    try {
      const result = await authClient.twoFactor.disable({ password: securityPassword });
      if (result.error) throw new Error(result.error.message);
      setSecurityPassword('');
      await refetchSession();
      showNotice('Two-factor authentication disabled.');
    } catch (error) {
      showError(error, 'Two-factor authentication could not be disabled.');
    } finally {
      setSecurityBusy(false);
    }
  };

  const revokeOtherSessions = async () => {
    setSecurityBusy(true);
    try {
      const result = await authClient.revokeOtherSessions();
      if (result.error) throw new Error(result.error.message);
      await loadSessions();
      showNotice('Other sessions revoked.');
    } catch (error) {
      showError(error, 'Other sessions could not be revoked.');
    } finally {
      setSecurityBusy(false);
    }
  };

  const revokeSession = async (token: string) => {
    setSecurityBusy(true);
    try {
      const result = await authClient.revokeSession({ token });
      if (result.error) throw new Error(result.error.message);
      await loadSessions();
      showNotice('Session revoked.');
    } catch (error) {
      showError(error, 'Session could not be revoked.');
    } finally {
      setSecurityBusy(false);
    }
  };

  const createWebhook = async (event: React.FormEvent) => {
    event.preventDefault();
    if (newWebhookEvents.length === 0) {
      setPageError('Select at least one webhook event.');
      return;
    }
    setWebhookBusy(true);
    setCreatedWebhookSecret(null);
    try {
      const response = await gatewayFetch('/webhooks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newWebhookName, url: newWebhookUrl, events: newWebhookEvents }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Webhook could not be created.');
      setCreatedWebhookSecret(payload.signing_secret);
      setNewWebhookName('');
      setNewWebhookUrl('');
      await loadWebhooks();
      showNotice('Webhook created. Copy the signing secret now; it cannot be recovered.');
    } catch (error) {
      showError(error, 'Webhook could not be created.');
    } finally {
      setWebhookBusy(false);
    }
  };

  const toggleWebhook = async (webhook: ManagedWebhook) => {
    setWebhookBusy(true);
    try {
      const response = await gatewayFetch(`/webhooks/${webhook.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: webhook.version, is_enabled: !webhook.is_enabled }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail?.code || payload?.detail || 'Webhook could not be updated.');
      await loadWebhooks();
      showNotice(`Webhook ${payload.webhook.is_enabled ? 'enabled' : 'disabled'}.`);
    } catch (error) {
      showError(error, 'Webhook could not be updated.');
    } finally {
      setWebhookBusy(false);
    }
  };

  const testWebhook = async (webhookId: string) => {
    setWebhookBusy(true);
    try {
      const response = await gatewayFetch(`/webhooks/${webhookId}/test`, { method: 'POST' });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Test delivery could not be sent.');
      await Promise.all([loadWebhooks(), loadWebhookDeliveries(webhookId)]);
      if (!payload.delivery.success) {
        throw new Error(payload.delivery.error_message || `Endpoint returned HTTP ${payload.delivery.response_status || 'unknown'}.`);
      }
      showNotice(`Test webhook delivered in ${payload.delivery.duration_ms}ms.`);
    } catch (error) {
      showError(error, 'Test delivery could not be sent.');
    } finally {
      setWebhookBusy(false);
    }
  };

  const rotateWebhookSecret = async (webhookId: string) => {
    setWebhookBusy(true);
    setCreatedWebhookSecret(null);
    try {
      const response = await gatewayFetch(`/webhooks/${webhookId}/rotate-secret`, { method: 'POST' });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Signing secret could not be rotated.');
      setCreatedWebhookSecret(payload.signing_secret);
      await loadWebhooks();
      showNotice('Signing secret rotated. Update the receiving endpoint before sending more events.');
    } catch (error) {
      showError(error, 'Signing secret could not be rotated.');
    } finally {
      setWebhookBusy(false);
    }
  };

  const deleteWebhook = async (webhookId: string) => {
    setWebhookBusy(true);
    try {
      const response = await gatewayFetch(`/webhooks/${webhookId}`, { method: 'DELETE' });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.message || payload?.detail || 'Webhook could not be deleted.');
      await loadWebhooks();
      setWebhookDeliveries((previous) => {
        const next = { ...previous };
        delete next[webhookId];
        return next;
      });
      showNotice('Webhook and its delivery history deleted.');
    } catch (error) {
      showError(error, 'Webhook could not be deleted.');
    } finally {
      setWebhookBusy(false);
    }
  };

  const handleExportData = async () => {
    setIsExporting(true);
    try {
      const response = await gatewayFetch(
        '/export',
        { method: 'POST' },
        { profile: 'processing' },
      );
      const data = await response.json().catch(() => null);
      if (!response.ok || !data) throw new Error(data?.message || data?.detail || 'The archive could not be generated.');
      const download = await gatewayFetch(
        data.download_url,
        {},
        { profile: 'processing' },
      );
      if (!download.ok) {
        const failure = await download.json().catch(() => null);
        throw new Error(failure?.message || failure?.detail || 'The archive could not be downloaded.');
      }
      const objectUrl = URL.createObjectURL(await download.blob());
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = data.file_name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setExportNotice(data as ExportNotice);
      showNotice('Data archive generated and downloaded.');
    } catch (error) {
      showError(error, 'The archive could not be generated.');
    } finally {
      setIsExporting(false);
    }
  };

  const tabs: Array<{ id: Tab; label: string; icon: React.ComponentType<{ className?: string }> }> = [
    { id: 'profile', label: 'Profile', icon: User },
    { id: 'workspace', label: 'Workspace', icon: Building },
    { id: 'api-keys', label: 'API Keys', icon: Key },
    { id: 'security', label: 'Security', icon: Shield },
    { id: 'preferences', label: 'Preferences', icon: WandSparkles },
    { id: 'webhooks', label: 'Webhooks', icon: Webhook },
    { id: 'data', label: 'Data Export', icon: Download },
  ];

  return (
    <div className="p-6 max-w-5xl mx-auto space-y-6">
      <div className="pb-4 border-b border-zinc-800">
        <h1 className="text-xl font-semibold text-white tracking-tight">Settings</h1>
        <p className="text-xs text-zinc-400 mt-0.5">Account, workspace, security, API access, and runtime defaults.</p>
      </div>

      {pageError && <StatusBanner error message={pageError} />}
      {notice && <StatusBanner message={notice} />}

      <div className="flex items-center gap-1 border-b border-zinc-800 pb-2 overflow-x-auto">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className={
                'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors ' +
                (activeTab === tab.id
                  ? 'bg-zinc-900 text-white border border-zinc-800'
                  : 'text-zinc-400 hover:text-white hover:bg-zinc-900/50')
              }
            >
              <Icon className="w-3.5 h-3.5" />
              {tab.label}
            </button>
          );
        })}
      </div>

      {loading ? (
        <div className={panelClass + ' text-center text-xs text-zinc-500'}>Loading settings…</div>
      ) : (
        <>
          {activeTab === 'profile' && (
            <form onSubmit={saveProfile} className={panelClass}>
              <h2 className="text-sm font-semibold text-white">Profile</h2>
              <div className="max-w-md space-y-3">
                <label className="block text-xs text-zinc-400">
                  Display name
                  <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} className={inputClass} maxLength={100} />
                </label>
                <label className="block text-xs text-zinc-400">
                  Email
                  <input value={session?.user.email || ''} readOnly className={inputClass + ' opacity-70 cursor-not-allowed'} />
                </label>
                <button className="px-3.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium">Save profile</button>
              </div>
            </form>
          )}

          {activeTab === 'workspace' && settings && (
            <div className={panelClass}>
              <h2 className="text-sm font-semibold text-white">Workspace</h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Fact label="Name" value={settings.organization_name} />
                <Fact label="Your role" value={settings.role} />
                <Fact label="Plan configuration" value={settings.plan || 'free'} />
                <Fact
                  label="Documents used / limit"
                  value={`${formatCountSum(
                    settings.committed_documents,
                    settings.reserved_documents,
                  )} / ${settings.max_documents === null ? 'Unlimited' : settings.max_documents.toLocaleString()}`}
                />
                <Fact
                  label="Original storage used / limit"
                  value={`${formatByteSum(
                    settings.committed_original_bytes,
                    settings.reserved_original_bytes,
                  )} / ${settings.max_storage_bytes === null ? 'Unlimited' : formatBytes(settings.max_storage_bytes)}`}
                />
                <Fact label="Enforced daily agent budget" value={(settings.max_token_budget_daily ?? 100000).toLocaleString()} />
              </div>
              <p className="text-[11px] text-zinc-500">Document and retained-original limits are enforced atomically before upload. In-flight reservations are included in usage.</p>
              <div>
                <p className="text-[10px] text-zinc-500">Workspace ID</p>
                <p className="mt-1 text-[11px] font-mono text-zinc-400 break-all">{settings.organization_id}</p>
              </div>
            </div>
          )}

          {activeTab === 'api-keys' && (
            <div className={panelClass}>
              <div>
                <h2 className="text-sm font-semibold text-white">Programmatic API keys</h2>
                <p className="mt-1 text-[11px] text-zinc-500">Keys are workspace-scoped, hashed at rest, rate-limited, and expire automatically.</p>
              </div>
              <form onSubmit={createApiKey} className="grid grid-cols-1 sm:grid-cols-[1fr_auto_auto_auto] gap-2">
                <input value={newKeyName} onChange={(event) => setNewKeyName(event.target.value)} className={inputClass + ' mt-0'} placeholder="Key name" maxLength={32} required />
                <select value={newKeyScope} onChange={(event) => setNewKeyScope(event.target.value)} className={inputClass + ' mt-0'}>
                  <option value="read">Read</option>
                  <option value="write">Read + write</option>
                  <option value="admin">Admin</option>
                </select>
                <select value={newKeyExpiry} onChange={(event) => setNewKeyExpiry(Number(event.target.value))} className={inputClass + ' mt-0'}>
                  <option value={30}>30 days</option>
                  <option value={90}>90 days</option>
                  <option value={365}>365 days</option>
                </select>
                <button disabled={keyBusy} className="px-3 py-2 rounded-lg bg-white disabled:opacity-40 text-black text-xs font-medium">Generate</button>
              </form>

              {createdSecret && (
                <div className="p-3 rounded-lg border border-amber-800/60 bg-amber-950/20 space-y-2">
                  <p className="text-xs font-medium text-amber-300">Copy this key now. It will not be shown again.</p>
                  <div className="flex gap-2">
                    <code className="flex-1 p-2 rounded bg-black text-[11px] text-zinc-300 break-all">{createdSecret}</code>
                    <button type="button" onClick={() => navigator.clipboard.writeText(createdSecret)} className="p-2 rounded bg-zinc-800 text-zinc-200" aria-label="Copy API key"><Copy className="w-4 h-4" /></button>
                  </div>
                  <button type="button" onClick={() => setCreatedSecret(null)} className="text-[11px] text-zinc-400 hover:text-white">I saved it — hide key</button>
                </div>
              )}

              <div className="space-y-2">
                {apiKeys.length === 0 ? (
                  <p className="py-5 text-center text-xs text-zinc-500">No active API keys.</p>
                ) : apiKeys.map((key) => (
                  <div key={key.id} className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-xs font-medium text-white truncate">{key.name || 'Unnamed key'}</p>
                      <p className="mt-0.5 text-[10px] text-zinc-500 font-mono">{key.start || 'certus_…'} · {key.scopes.join(', ')} · {key.request_count} requests</p>
                      <p className="mt-0.5 text-[10px] text-zinc-600">Expires {key.expires_at ? new Date(key.expires_at).toLocaleDateString() : 'never'}</p>
                    </div>
                    <button type="button" disabled={keyBusy} onClick={() => revokeApiKey(key.id)} className="p-1.5 rounded bg-zinc-800 text-zinc-400 hover:text-red-400 disabled:opacity-40" aria-label={'Revoke ' + (key.name || 'API key')}><Trash2 className="w-3.5 h-3.5" /></button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'security' && (
            <div className={panelClass}>
              <div>
                <h2 className="text-sm font-semibold text-white">Two-factor authentication</h2>
                <p className="mt-1 text-[11px] text-zinc-500">Authenticator-based TOTP with one-time recovery codes.</p>
              </div>

              {enrollment ? (
                <div className="space-y-3 p-3 rounded-lg bg-zinc-900 border border-zinc-800">
                  <p className="text-xs text-zinc-300">Add this URI to your authenticator app, save the recovery codes, then enter a current six-digit code.</p>
                  <div className="flex gap-2">
                    <code className="flex-1 p-2 rounded bg-black text-[10px] text-zinc-400 break-all">{enrollment.totpURI}</code>
                    <button type="button" onClick={() => navigator.clipboard.writeText(enrollment.totpURI)} className="p-2 rounded bg-zinc-800" aria-label="Copy authenticator setup URI"><Copy className="w-4 h-4" /></button>
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-1">
                    {enrollment.backupCodes.map((code) => <code key={code} className="p-1.5 rounded bg-black text-center text-[10px] text-amber-300">{code}</code>)}
                  </div>
                  <div className="flex gap-2">
                    <input value={totpCode} onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, '').slice(0, 6))} className={inputClass + ' mt-0 max-w-36'} placeholder="123456" inputMode="numeric" />
                    <button type="button" onClick={verifyTwoFactorEnrollment} disabled={securityBusy || totpCode.length !== 6} className="px-3 py-2 rounded-lg bg-white disabled:opacity-40 text-black text-xs font-medium">Verify and enable</button>
                  </div>
                </div>
              ) : (
                <div className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 flex flex-col sm:flex-row sm:items-end gap-2">
                  <label className="flex-1 text-xs text-zinc-400">
                    Current password
                    <input type="password" value={securityPassword} onChange={(event) => setSecurityPassword(event.target.value)} className={inputClass} autoComplete="current-password" />
                  </label>
                  {(session?.user as { twoFactorEnabled?: boolean } | undefined)?.twoFactorEnabled ? (
                    <button type="button" onClick={disableTwoFactor} disabled={securityBusy || !securityPassword} className="px-3 py-2 rounded-lg bg-red-950 text-red-300 border border-red-900/50 disabled:opacity-40 text-xs">Disable 2FA</button>
                  ) : (
                    <button type="button" onClick={beginTwoFactorEnrollment} disabled={securityBusy || !securityPassword} className="px-3 py-2 rounded-lg bg-white text-black disabled:opacity-40 text-xs font-medium">Set up authenticator</button>
                  )}
                </div>
              )}

              <div className="pt-4 border-t border-zinc-800 space-y-3">
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-xs font-semibold text-white">Active sessions</h3>
                    <p className="text-[10px] text-zinc-500">{sessions.length} active session{sessions.length === 1 ? '' : 's'}</p>
                  </div>
                  <button type="button" onClick={revokeOtherSessions} disabled={securityBusy || sessions.length <= 1} className="px-2.5 py-1.5 rounded bg-zinc-900 border border-zinc-800 disabled:opacity-40 text-xs text-zinc-300">Revoke others</button>
                </div>
                <div className="space-y-2">
                  {sessions.map((activeSession) => {
                    const current = activeSession.token === session?.session.token;
                    return (
                      <div key={activeSession.id} className="p-3 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-between gap-3">
                        <div className="flex items-start gap-2 min-w-0">
                          <Laptop className="w-4 h-4 text-zinc-500 mt-0.5 shrink-0" />
                          <div className="min-w-0">
                            <p className="text-xs text-zinc-200 truncate">{formatUserAgent(activeSession.userAgent)}</p>
                            <p className="text-[10px] text-zinc-500">{activeSession.ipAddress || 'IP unavailable'} · active {new Date(activeSession.updatedAt).toLocaleString()}</p>
                          </div>
                        </div>
                        {current ? (
                          <span className="text-[10px] text-emerald-400">Current</span>
                        ) : (
                          <button type="button" onClick={() => revokeSession(activeSession.token)} disabled={securityBusy} className="text-[10px] text-red-400 hover:text-red-300 disabled:opacity-40">Revoke</button>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}

          {activeTab === 'preferences' && settings && (
            <form onSubmit={savePreferences} className={panelClass}>
              <div>
                <h2 className="text-sm font-semibold text-white">Runtime defaults</h2>
                <p className="mt-1 text-[11px] text-zinc-500">Applied when chat and uploads do not explicitly choose an override.</p>
                <p className="mt-1 text-[11px] text-zinc-500">
                  {generativeProviderConfigured
                    ? 'Provider-backed generation is configured; each completed run reports the model actually used.'
                    : 'No generative provider key is configured. Chat uses deterministic local extractive output, not a local generative LLM.'}
                </p>
              </div>
              <div className="max-w-md space-y-3">
                <label className="block text-xs text-zinc-400">
                  Default model routing
                  <select value={settings.default_model} onChange={(event) => setSettings({ ...settings, default_model: event.target.value })} className={inputClass}>
                    {availableModels.map((model) => <option key={model} value={model}>{model === 'auto' ? 'Auto router' : model}</option>)}
                  </select>
                </label>
                <label className="block text-xs text-zinc-400">
                  Default chunking strategy
                  <select value={settings.default_chunk_strategy} onChange={(event) => setSettings({ ...settings, default_chunk_strategy: event.target.value })} className={inputClass}>
                    <option value="token">Token windows</option>
                    <option value="sentence">Sentence-boundary windows</option>
                    <option value="recursive">Recursive paragraphs</option>
                  </select>
                </label>
                <button className="px-3.5 py-2 rounded-lg bg-white hover:bg-zinc-200 text-black text-xs font-medium">Save preferences</button>
              </div>
            </form>
          )}

          {activeTab === 'webhooks' && (
            <div className={panelClass}>
              <div>
                <h2 className="text-sm font-semibold text-white">Outbound webhooks</h2>
                <p className="mt-1 text-[11px] text-zinc-500">HTTPS-only signed event delivery. Secrets are encrypted at rest and shown only after creation or rotation.</p>
              </div>

              <form onSubmit={createWebhook} className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label className="text-xs text-zinc-400">
                    Name
                    <input required maxLength={100} value={newWebhookName} onChange={(event) => setNewWebhookName(event.target.value)} className={inputClass} placeholder="Production event receiver" />
                  </label>
                  <label className="text-xs text-zinc-400">
                    HTTPS endpoint
                    <input required type="url" maxLength={500} value={newWebhookUrl} onChange={(event) => setNewWebhookUrl(event.target.value)} className={inputClass} placeholder="https://example.com/certus/events" />
                  </label>
                </div>
                <fieldset>
                  <legend className="text-xs text-zinc-400">Subscribed events</legend>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {supportedWebhookEvents.map((eventName) => (
                      <label key={eventName} className="flex items-center gap-1.5 rounded-md border border-zinc-800 bg-black px-2 py-1.5 text-[10px] text-zinc-400">
                        <input
                          type="checkbox"
                          checked={newWebhookEvents.includes(eventName)}
                          onChange={(event) => setNewWebhookEvents((previous) => (
                            event.target.checked
                              ? [...previous, eventName]
                              : previous.filter((item) => item !== eventName)
                          ))}
                          className="accent-white"
                        />
                        {displayWebhookEvent(eventName)}
                      </label>
                    ))}
                  </div>
                </fieldset>
                <button disabled={webhookBusy || newWebhookEvents.length === 0} className="rounded-lg bg-white px-3.5 py-2 text-xs font-medium text-black disabled:opacity-40">Add webhook</button>
              </form>

              {createdWebhookSecret && (
                <div className="space-y-2 rounded-lg border border-amber-800/60 bg-amber-950/20 p-3">
                  <p className="text-xs font-medium text-amber-300">Copy this signing secret now. It will not be shown again.</p>
                  <div className="flex gap-2">
                    <code className="flex-1 break-all rounded bg-black p-2 text-[11px] text-zinc-300">{createdWebhookSecret}</code>
                    <button type="button" onClick={() => navigator.clipboard.writeText(createdWebhookSecret)} className="rounded bg-zinc-800 p-2 text-zinc-200" aria-label="Copy webhook signing secret"><Copy className="w-4 h-4" /></button>
                  </div>
                  <button type="button" onClick={() => setCreatedWebhookSecret(null)} className="text-[11px] text-zinc-400 hover:text-white">I saved it — hide secret</button>
                </div>
              )}

              <div className="space-y-3">
                {webhooks.length === 0 ? (
                  <p className="py-6 text-center text-xs text-zinc-500">No webhook endpoints configured.</p>
                ) : webhooks.map((webhook) => (
                  <div key={webhook.id} className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="truncate text-xs font-medium text-white">{webhook.name}</p>
                          <span className={`text-[10px] ${webhook.is_enabled ? 'text-emerald-400' : 'text-zinc-600'}`}>{webhook.is_enabled ? 'Enabled' : 'Disabled'}</span>
                        </div>
                        <p className="mt-1 break-all font-mono text-[10px] text-zinc-500">{webhook.url}</p>
                        <p className="mt-1 text-[10px] text-zinc-600">Secret ending {webhook.secret_hint || 'unknown'} · {webhook.success_count}/{webhook.delivery_count} successful deliveries</p>
                        <div className="mt-2 flex flex-wrap gap-1">
                          {webhook.events.map((eventName) => <span key={eventName} className="rounded border border-zinc-800 bg-black px-1.5 py-0.5 text-[9px] text-zinc-500">{displayWebhookEvent(eventName)}</span>)}
                        </div>
                        {webhook.last_error && <p className="mt-2 text-[10px] text-red-400">Last failure: {webhook.last_error}</p>}
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        <button type="button" disabled={webhookBusy} onClick={() => toggleWebhook(webhook)} title={webhook.is_enabled ? 'Disable webhook' : 'Enable webhook'} className="rounded bg-zinc-800 p-1.5 text-zinc-400 hover:text-white disabled:opacity-40"><Power className="w-3.5 h-3.5" /></button>
                        <button type="button" disabled={webhookBusy || !webhook.is_enabled} onClick={() => testWebhook(webhook.id)} title="Send test event" className="rounded bg-zinc-800 p-1.5 text-zinc-400 hover:text-white disabled:opacity-40"><Send className="w-3.5 h-3.5" /></button>
                        <button type="button" disabled={webhookBusy} onClick={() => rotateWebhookSecret(webhook.id)} title="Rotate signing secret" className="rounded bg-zinc-800 p-1.5 text-zinc-400 hover:text-white disabled:opacity-40"><RefreshCw className="w-3.5 h-3.5" /></button>
                        <button type="button" disabled={webhookBusy} onClick={() => deleteWebhook(webhook.id)} title="Delete webhook" className="rounded bg-zinc-800 p-1.5 text-zinc-400 hover:text-red-400 disabled:opacity-40"><Trash2 className="w-3.5 h-3.5" /></button>
                      </div>
                    </div>

                    <button type="button" onClick={() => loadWebhookDeliveries(webhook.id).catch((error) => showError(error, 'Delivery history could not be loaded.'))} className="text-[10px] text-zinc-400 hover:text-white">Refresh delivery history</button>
                    {webhookDeliveries[webhook.id]?.length > 0 && (
                      <div className="overflow-x-auto rounded border border-zinc-800 bg-black">
                        <table className="w-full text-left text-[10px]">
                          <thead className="text-zinc-600"><tr><th className="p-2">Event</th><th className="p-2">Result</th><th className="p-2">Latency</th><th className="p-2">Attempted</th></tr></thead>
                          <tbody className="divide-y divide-zinc-900">
                            {webhookDeliveries[webhook.id].map((delivery) => (
                              <tr key={delivery.id} className="text-zinc-400">
                                <td className="p-2">{displayWebhookEvent(delivery.event_type)}</td>
                                <td className={`p-2 ${delivery.success ? 'text-emerald-400' : 'text-red-400'}`}>{delivery.success ? `HTTP ${delivery.response_status}` : delivery.error_message || `HTTP ${delivery.response_status || '—'}`}</td>
                                <td className="p-2 font-mono">{delivery.duration_ms === null ? '—' : `${delivery.duration_ms}ms`}</td>
                                <td className="p-2">{new Date(delivery.attempted_at).toLocaleString()}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    {webhookDeliveryCursors[webhook.id] && (
                      <button type="button" onClick={() => loadWebhookDeliveries(webhook.id, webhookDeliveryCursors[webhook.id] || undefined).catch((error) => showError(error, 'Delivery history could not be loaded.'))} className="text-[10px] text-zinc-400 hover:text-white">Load older deliveries</button>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {activeTab === 'data' && (
            <div className={panelClass}>
              <div>
                <h2 className="text-sm font-semibold text-white">Data portability</h2>
                <p className="mt-1 text-xs text-zinc-400">Download a tenant-scoped ZIP containing your stored application data. Credential material is excluded.</p>
              </div>
              <button type="button" onClick={handleExportData} disabled={isExporting} className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg bg-white disabled:opacity-40 text-black text-xs font-medium">
                {isExporting ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                {isExporting ? 'Generating archive…' : 'Export data archive'}
              </button>
              {exportNotice && (
                <div className="p-3.5 rounded-lg bg-zinc-900 border border-zinc-800 space-y-2 text-xs">
                  <span className="font-medium text-emerald-400 flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" /> {exportNotice.file_name} downloaded</span>
                  <p className="text-zinc-500">{(exportNotice.archive_size_bytes / 1024).toFixed(1)} KB · server copy expires {new Date(exportNotice.expires_at).toLocaleString()}</p>
                </div>
              )}
              <div className="pt-4 border-t border-zinc-800">
                <h3 className="text-xs font-semibold text-zinc-300">Account deletion</h3>
                <p className="mt-1 text-[11px] text-zinc-500">Cross-database deletion is not enabled yet. The product will not claim deletion until PostgreSQL, Neo4j, Temporal, and retained archives can be erased atomically and audited.</p>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function StatusBanner({ message, error = false }: { message: string; error?: boolean }) {
  const Icon = error ? AlertCircle : CheckCircle2;
  return (
    <div role={error ? 'alert' : 'status'} className={'p-3 rounded-lg border text-xs flex gap-2 ' + (error ? 'border-red-900/60 bg-red-950/30 text-red-300' : 'border-emerald-900/50 bg-emerald-950/20 text-emerald-300')}>
      <Icon className="w-4 h-4 shrink-0" />
      {message}
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="p-3.5 rounded-lg bg-zinc-900 border border-zinc-800">
      <span className="text-[10px] text-zinc-500">{label}</span>
      <p className="text-xs font-medium text-zinc-100 mt-1 break-all">{value}</p>
    </div>
  );
}

function formatCountSum(first: string, second: string) {
  return (BigInt(first) + BigInt(second)).toLocaleString();
}

function formatByteSum(first: string, second: string) {
  return formatBytes(BigInt(first) + BigInt(second));
}

function formatBytes(value: string | bigint) {
  const bytes = BigInt(value);
  const kibibyte = 1024n;
  const mebibyte = kibibyte * 1024n;
  const gibibyte = mebibyte * 1024n;
  if (bytes < mebibyte) {
    return ((bytes + kibibyte / 2n) / kibibyte).toLocaleString() + ' KB';
  }
  if (bytes < gibibyte) {
    return ((bytes + mebibyte / 2n) / mebibyte).toLocaleString() + ' MB';
  }
  const tenths = (bytes * 10n + gibibyte / 2n) / gibibyte;
  return `${tenths / 10n}.${tenths % 10n} GB`;
}

function formatUserAgent(value?: string | null) {
  if (!value) return 'Unknown device';
  if (value.length <= 90) return value;
  return value.slice(0, 87) + '…';
}

function displayWebhookEvent(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export default function SettingsPage() {
  return <SettingsContent />;
}
