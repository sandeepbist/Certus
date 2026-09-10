'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { gatewayFetch, gatewayWebSocketUrl } from '@/lib/gateway-client';

export interface Citation {
  evidenceId?: string;
  claimIds?: string[];
  chunkId: string;
  documentId: string;
  documentVersionId?: string;
  derivationId?: string;
  parsedArtifactId?: string;
  versionNumber?: number;
  documentTitle: string;
  quote: string;
  contentHash?: string;
  sourceTime?: string | null;
  recordedAt?: string;
  isCurrentVersion?: boolean;
  score?: number;
  pageNumber?: number;
  startChar?: number;
  endChar?: number;
  textLocatorStatus?: 'exact' | 'unavailable';
  textLocatorProfile?: string;
  quoteSha256?: string;
  supportScope?: 'retrieved_context_not_claim_aligned' | 'atomic_claim_selected';
  verificationStatus?: 'mechanical_checks_passed_semantic_not_evaluated';
}

export interface ClaimEvidence {
  claimId: string;
  text: string;
  sourceRefs: Array<{
    sourceId: string;
    sourceKind: 'document' | 'memory' | 'graph' | 'tool';
    contentSha256: string;
  }>;
  mechanicalValidation: {
    status: 'passed';
    exactAnchors: string[];
    significantTermCoverage?: number;
    semanticEntailmentChecked: false;
  };
  semanticSupportStatus: 'not_evaluated';
}

export interface RunProvenance {
  evidenceManifestProfile: 'certus_typed_evidence_manifest:v1';
  evidenceManifestSha256: string;
  evidenceManifestDbSha256?: string;
  evidenceSourceCount: number;
  generationProfile: 'certus_grounded_generation:v1';
  generationProfileSha256: string;
  generationProfileDbSha256?: string;
  executionMode: 'openai_structured' | 'local_extractive' | 'local_extractive_fallback' | 'local_action';
  provider: 'openai' | 'certus_local';
  requestedModel: string;
  returnedModel?: string;
  modelRevisionLocked: boolean;
  promptProfile: 'certus_atomic_claim_prompt:v1';
  validatorProfile: 'certus_mechanical_claim_validator:v1';
  conversationContextProfile?: 'certus_conversation_context:v1';
  conversationContextSha256?: string;
  conversationContextDbSha256?: string;
  conversationTurnCount?: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  citations?: Citation[];
  claims?: ClaimEvidence[];
  answerStatus?: 'answered' | 'extractive' | 'insufficient_evidence' | 'conflicting_evidence' | 'action_completed' | 'legacy_unavailable';
  groundingProfile?: string;
  provenance?: RunProvenance;
  agentEvents?: string[];
  modelUsed?: string;
  evalScore?: number;
  selectedDocumentIds?: string[];
  selectedVersionScope?: ChatVersionScope;
  timestamp: string;
}

export type ChatVersionScope = 'auto' | 'all_history' | 'current_only';

export interface AgentEvent {
  agent: 'router' | 'planner' | 'researcher' | 'executor' | 'critic' | 'tool_dispatcher';
  action: string;
  status: 'pending' | 'started' | 'in_progress' | 'completed' | 'degraded' | 'warning' | 'failed';
  details?: Record<string, unknown>;
  timestamp: number;
}

type ConnectionState = 'connecting' | 'connected' | 'disconnected';

type StreamingChatOptions = {
  initialSessionId?: string;
  welcomeMessage?: string;
};

type PendingRequest = {
  requestId: string;
  assistantMessageId: string;
  query: string;
  sessionId: string;
  model: string;
  documentIds: string[];
  versionScope: ChatVersionScope;
  transport: 'socket' | 'rest';
  recovering: boolean;
};

const DEFAULT_WELCOME = 'Welcome to Certus. Ask a question about your workspace documents, or request an available tool action.';

function timestamp() {
  return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function normalizeCitations(value: unknown): Citation[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const citation = asRecord(item);
    if (!citation) return [];
    const chunkId = citation.chunk_id ?? citation.chunkId;
    const documentId = citation.document_id ?? citation.documentId;
    const documentVersionId = citation.document_version_id ?? citation.documentVersionId;
    const derivationId = citation.derivation_id ?? citation.derivationId;
    const parsedArtifactId = citation.parsed_artifact_id ?? citation.parsedArtifactId;
    const versionNumber = citation.version_number ?? citation.versionNumber;
    const documentTitle = citation.document_title ?? citation.documentTitle;
    const contentHash = citation.content_hash ?? citation.contentHash;
    const isCurrentVersion = citation.is_current_version ?? citation.isCurrentVersion;
    const startChar = citation.start_char ?? citation.startChar;
    const endChar = citation.end_char ?? citation.endChar;
    const textLocatorStatus = citation.text_locator_status ?? citation.textLocatorStatus;
    const textLocatorProfile = citation.text_locator_profile ?? citation.textLocatorProfile;
    const quoteSha256 = citation.quote_sha256 ?? citation.quoteSha256;
    const supportScope = citation.support_scope ?? citation.supportScope;
    const evidenceId = citation.evidence_id ?? citation.evidenceId;
    const claimIds = citation.claim_ids ?? citation.claimIds;
    const verificationStatus = citation.verification_status ?? citation.verificationStatus;
    if (
      typeof chunkId !== 'string'
      || typeof documentId !== 'string'
      || typeof documentTitle !== 'string'
    ) return [];
    if (
      supportScope === 'atomic_claim_selected'
      && (
        typeof evidenceId !== 'string'
        || !/^D[1-9][0-9]{0,2}$/.test(evidenceId)
        || !Array.isArray(claimIds)
        || claimIds.length === 0
        || !claimIds.every((claimId) => typeof claimId === 'string' && /^C[1-9][0-9]{0,2}$/.test(claimId))
        || verificationStatus !== 'mechanical_checks_passed_semantic_not_evaluated'
      )
    ) return [];
    return [{
      chunkId,
      documentId,
      documentTitle,
      quote: typeof citation.quote === 'string' ? citation.quote : '',
      ...(typeof evidenceId === 'string' ? { evidenceId } : {}),
      ...(Array.isArray(claimIds) && claimIds.every((claimId) => typeof claimId === 'string')
        ? { claimIds: claimIds as string[] }
        : {}),
      ...(typeof documentVersionId === 'string' ? { documentVersionId } : {}),
      ...(typeof derivationId === 'string' ? { derivationId } : {}),
      ...(typeof parsedArtifactId === 'string' ? { parsedArtifactId } : {}),
      ...(typeof versionNumber === 'number' ? { versionNumber } : {}),
      ...(typeof contentHash === 'string' ? { contentHash } : {}),
      ...(typeof isCurrentVersion === 'boolean' ? { isCurrentVersion } : {}),
      ...(typeof (citation.source_time ?? citation.sourceTime) === 'string'
        ? { sourceTime: (citation.source_time ?? citation.sourceTime) as string }
        : {}),
      ...(typeof (citation.recorded_at ?? citation.recordedAt) === 'string'
        ? { recordedAt: (citation.recorded_at ?? citation.recordedAt) as string }
        : {}),
      ...(typeof citation.score === 'number' ? { score: citation.score } : {}),
      ...(typeof (citation.page_number ?? citation.pageNumber) === 'number'
        ? { pageNumber: (citation.page_number ?? citation.pageNumber) as number }
        : {}),
      ...(typeof startChar === 'number' ? { startChar } : {}),
      ...(typeof endChar === 'number' ? { endChar } : {}),
      ...(textLocatorStatus === 'exact' || textLocatorStatus === 'unavailable'
        ? { textLocatorStatus }
        : {}),
      ...(typeof textLocatorProfile === 'string' ? { textLocatorProfile } : {}),
      ...(typeof quoteSha256 === 'string' ? { quoteSha256 } : {}),
      ...(supportScope === 'retrieved_context_not_claim_aligned' || supportScope === 'atomic_claim_selected'
        ? { supportScope }
        : {}),
      ...(verificationStatus === 'mechanical_checks_passed_semantic_not_evaluated'
        ? { verificationStatus }
        : {}),
    }];
  });
}

export function normalizeClaims(value: unknown): ClaimEvidence[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const claim = asRecord(item);
    if (!claim) return [];
    const claimId = claim.claim_id ?? claim.claimId;
    const sourceRefsValue = claim.source_refs ?? claim.sourceRefs;
    const mechanicalValue = asRecord(claim.mechanical_validation ?? claim.mechanicalValidation);
    const semanticStatus = claim.semantic_support_status ?? claim.semanticSupportStatus;
    if (
      typeof claimId !== 'string'
      || !/^C[1-9][0-9]{0,2}$/.test(claimId)
      || typeof claim.text !== 'string'
      || claim.text.length === 0
      || claim.text.length > 1_000
      || !Array.isArray(sourceRefsValue)
      || sourceRefsValue.length === 0
      || sourceRefsValue.length > 5
      || mechanicalValue?.status !== 'passed'
      || semanticStatus !== 'not_evaluated'
    ) return [];
    const sourceRefs = sourceRefsValue.flatMap((sourceRefValue) => {
      const sourceRef = asRecord(sourceRefValue);
      const sourceId = sourceRef?.source_id ?? sourceRef?.sourceId;
      const sourceKind = sourceRef?.source_kind ?? sourceRef?.sourceKind;
      const contentSha256 = sourceRef?.content_sha256 ?? sourceRef?.contentSha256;
      if (
        typeof sourceId !== 'string'
        || !/^[DMGT][1-9][0-9]{0,2}$/.test(sourceId)
        || !['document', 'memory', 'graph', 'tool'].includes(String(sourceKind))
        || typeof contentSha256 !== 'string'
        || !/^[0-9a-f]{64}$/.test(contentSha256)
      ) return [];
      return [{
        sourceId,
        sourceKind: sourceKind as 'document' | 'memory' | 'graph' | 'tool',
        contentSha256,
      }];
    });
    if (sourceRefs.length !== sourceRefsValue.length) return [];
    const exactAnchors = mechanicalValue.exact_anchors ?? mechanicalValue.exactAnchors;
    const significantTermCoverage = mechanicalValue.significant_term_coverage
      ?? mechanicalValue.significantTermCoverage;
    const semanticEntailmentChecked = mechanicalValue.semantic_entailment_checked
      ?? mechanicalValue.semanticEntailmentChecked;
    if (
      semanticEntailmentChecked !== false
      || (significantTermCoverage !== undefined
        && (typeof significantTermCoverage !== 'number'
          || significantTermCoverage < 0
          || significantTermCoverage > 1))
    ) return [];
    return [{
      claimId,
      text: claim.text,
      sourceRefs,
      mechanicalValidation: {
        status: 'passed',
        exactAnchors: Array.isArray(exactAnchors)
          ? exactAnchors.filter((anchor): anchor is string => typeof anchor === 'string')
          : [],
        ...(typeof significantTermCoverage === 'number' ? { significantTermCoverage } : {}),
        semanticEntailmentChecked: false,
      },
      semanticSupportStatus: 'not_evaluated',
    }];
  });
}

export function normalizeAnswerStatus(value: unknown): ChatMessage['answerStatus'] {
  return [
    'answered',
    'extractive',
    'insufficient_evidence',
    'conflicting_evidence',
    'action_completed',
    'legacy_unavailable',
  ].includes(String(value)) ? value as ChatMessage['answerStatus'] : undefined;
}

export function normalizeRunProvenance(value: unknown): RunProvenance | undefined {
  const provenance = asRecord(value);
  if (!provenance) return undefined;
  const evidenceManifestSha256 = provenance.evidence_manifest_sha256
    ?? provenance.evidenceManifestSha256;
  const evidenceManifestDbSha256 = provenance.evidence_manifest_db_sha256
    ?? provenance.evidenceManifestDbSha256;
  const generationProfileSha256 = provenance.generation_profile_sha256
    ?? provenance.generationProfileSha256;
  const generationProfileDbSha256 = provenance.generation_profile_db_sha256
    ?? provenance.generationProfileDbSha256;
  const evidenceSourceCount = provenance.evidence_source_count
    ?? provenance.evidenceSourceCount;
  const executionMode = provenance.execution_mode ?? provenance.executionMode;
  const requestedModel = provenance.requested_model ?? provenance.requestedModel;
  const returnedModel = provenance.returned_model ?? provenance.returnedModel;
  const modelRevisionLocked = provenance.model_revision_locked
    ?? provenance.modelRevisionLocked;
  const promptProfile = provenance.prompt_profile ?? provenance.promptProfile;
  const validatorProfile = provenance.validator_profile ?? provenance.validatorProfile;
  const conversationContextProfile = provenance.conversation_context_profile
    ?? provenance.conversationContextProfile;
  const conversationContextSha256 = provenance.conversation_context_sha256
    ?? provenance.conversationContextSha256;
  const conversationContextDbSha256 = provenance.conversation_context_db_sha256
    ?? provenance.conversationContextDbSha256;
  const conversationTurnCount = provenance.conversation_turn_count
    ?? provenance.conversationTurnCount;
  const hasConversationContext = conversationContextProfile != null
    || conversationContextSha256 != null
    || conversationContextDbSha256 != null
    || conversationTurnCount != null;
  const sha256 = (candidate: unknown) => (
    typeof candidate === 'string' && /^[0-9a-f]{64}$/.test(candidate)
  );
  if (
    provenance.evidence_manifest_profile !== 'certus_typed_evidence_manifest:v1'
    || provenance.generation_profile !== 'certus_grounded_generation:v1'
    || !sha256(evidenceManifestSha256)
    || !sha256(generationProfileSha256)
    || (evidenceManifestDbSha256 != null && !sha256(evidenceManifestDbSha256))
    || (generationProfileDbSha256 != null && !sha256(generationProfileDbSha256))
    || typeof evidenceSourceCount !== 'number'
    || !Number.isInteger(evidenceSourceCount)
    || evidenceSourceCount < 0
    || evidenceSourceCount > 64
    || !['openai_structured', 'local_extractive', 'local_extractive_fallback', 'local_action'].includes(String(executionMode))
    || !['openai', 'certus_local'].includes(String(provenance.provider))
    || typeof requestedModel !== 'string'
    || requestedModel.length === 0
    || (returnedModel != null && typeof returnedModel !== 'string')
    || typeof modelRevisionLocked !== 'boolean'
    || promptProfile !== 'certus_atomic_claim_prompt:v1'
    || validatorProfile !== 'certus_mechanical_claim_validator:v1'
    || (hasConversationContext && (
      conversationContextProfile !== 'certus_conversation_context:v1'
      || !sha256(conversationContextSha256)
      || (conversationContextDbSha256 != null && !sha256(conversationContextDbSha256))
      || !Number.isInteger(conversationTurnCount)
      || Number(conversationTurnCount) < 0
      || Number(conversationTurnCount) > 4
    ))
  ) return undefined;
  return {
    evidenceManifestProfile: 'certus_typed_evidence_manifest:v1',
    evidenceManifestSha256: evidenceManifestSha256 as string,
    ...(typeof evidenceManifestDbSha256 === 'string' ? { evidenceManifestDbSha256 } : {}),
    evidenceSourceCount,
    generationProfile: 'certus_grounded_generation:v1',
    generationProfileSha256: generationProfileSha256 as string,
    ...(typeof generationProfileDbSha256 === 'string' ? { generationProfileDbSha256 } : {}),
    executionMode: executionMode as RunProvenance['executionMode'],
    provider: provenance.provider as RunProvenance['provider'],
    requestedModel,
    ...(typeof returnedModel === 'string' ? { returnedModel } : {}),
    modelRevisionLocked,
    promptProfile: 'certus_atomic_claim_prompt:v1',
    validatorProfile: 'certus_mechanical_claim_validator:v1',
    ...(hasConversationContext ? {
      conversationContextProfile: 'certus_conversation_context:v1' as const,
      conversationContextSha256: conversationContextSha256 as string,
      ...(typeof conversationContextDbSha256 === 'string' ? { conversationContextDbSha256 } : {}),
      conversationTurnCount: conversationTurnCount as number,
    } : {}),
  };
}

function normalizeAgentEvent(value: Record<string, unknown>): AgentEvent | null {
  if (
    typeof value.agent !== 'string'
    || typeof value.action !== 'string'
    || typeof value.status !== 'string'
    || typeof value.timestamp !== 'number'
  ) return null;
  return value as unknown as AgentEvent;
}

export function useStreamingChat(options: StreamingChatOptions = {}) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      content: options.welcomeMessage || DEFAULT_WELCOME,
      timestamp: timestamp(),
    },
  ]);
  const [agentEvents, setAgentEvents] = useState<AgentEvent[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [selectedModel, setSelectedModel] = useState('auto');
  const [sessionId, setSessionId] = useState(
    () => options.initialSessionId || crypto.randomUUID(),
  );
  const [connectionState, setConnectionState] = useState<ConnectionState>('connecting');
  const [completedRunId, setCompletedRunId] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef<PendingRequest | null>(null);
  const restControllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);

  const updateAssistant = useCallback(
    (assistantMessageId: string, update: (message: ChatMessage) => ChatMessage) => {
      setMessages((previous) => previous.map((message) => (
        message.id === assistantMessageId ? update(message) : message
      )));
    },
    [],
  );

  const finishPending = useCallback((requestId: string) => {
    if (pendingRef.current?.requestId !== requestId) return false;
    pendingRef.current = null;
    restControllerRef.current = null;
    setIsStreaming(false);
    return true;
  }, []);

  const failPending = useCallback((message: string, requestId?: string) => {
    const pending = pendingRef.current;
    if (!pending || (requestId && requestId !== pending.requestId)) return;
    updateAssistant(pending.assistantMessageId, (assistant) => ({
      ...assistant,
      content: assistant.content
        ? `${assistant.content}\n\n[Stream stopped: ${message}]`
        : `Error: ${message}`,
    }));
    finishPending(pending.requestId);
  }, [finishPending, updateAssistant]);

  const handleSocketMessage = useCallback((rawMessage: unknown) => {
    const data = asRecord(rawMessage);
    if (!data || typeof data.type !== 'string') return;
    const requestId = typeof data.request_id === 'string' ? data.request_id : undefined;
    const pending = pendingRef.current;
    if (requestId && pending && requestId !== pending.requestId) return;

    if (data.type === 'agent_event') {
      const agentEvent = normalizeAgentEvent(data);
      if (agentEvent) setAgentEvents((previous) => [...previous, agentEvent]);
      return;
    }

    if (data.type === 'token' && pending && typeof data.content === 'string') {
      updateAssistant(pending.assistantMessageId, (assistant) => ({
        ...assistant,
        content: assistant.content + data.content,
      }));
      return;
    }

    if (data.type === 'citations' && pending) {
      updateAssistant(pending.assistantMessageId, (assistant) => ({
        ...assistant,
        citations: normalizeCitations(data.citations),
      }));
      return;
    }

    if (data.type === 'done' && pending) {
      updateAssistant(pending.assistantMessageId, (assistant) => ({
        ...assistant,
        modelUsed: typeof data.model_used === 'string' ? data.model_used : undefined,
        evalScore: typeof data.eval_score === 'number' ? data.eval_score : undefined,
        citations: normalizeCitations(data.citations),
        claims: normalizeClaims(data.claims),
        answerStatus: normalizeAnswerStatus(data.answer_status),
        groundingProfile: typeof data.grounding_profile === 'string'
          ? data.grounding_profile
          : undefined,
        provenance: normalizeRunProvenance(data.provenance),
      }));
      if (finishPending(pending.requestId)) {
        setCompletedRunId(typeof data.run_id === 'string' ? data.run_id : pending.requestId);
      }
      return;
    }

    if ((data.type === 'error' || data.type === 'stream.cancelled') && pending) {
      failPending(
        typeof data.message === 'string' ? data.message : 'The workflow failed.',
        requestId,
      );
    }
  }, [failPending, finishPending, updateAssistant]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let stopped = false;
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;

    mountedRef.current = true;

    const scheduleReconnect = () => {
      if (stopped || !navigator.onLine || reconnectTimer !== null) return;
      const baseDelay = Math.min(30_000, 1_000 * (2 ** reconnectAttempt));
      const delay = Math.round(baseDelay * (0.8 + Math.random() * 0.4));
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    const connect = () => {
      if (
        stopped
        || !navigator.onLine
        || socket?.readyState === WebSocket.OPEN
        || socket?.readyState === WebSocket.CONNECTING
      ) return;
      setConnectionState('connecting');
      try {
        const nextSocket = new WebSocket(gatewayWebSocketUrl());
        socket = nextSocket;
        wsRef.current = nextSocket;
        nextSocket.onopen = () => {
          reconnectAttempt = 0;
          setConnectionState('connected');
          const pending = pendingRef.current;
          if (pending?.transport === 'socket' && pending.recovering) {
            pending.recovering = false;
            setAgentEvents([]);
            updateAssistant(pending.assistantMessageId, (assistant) => ({
              ...assistant,
              content: '',
              citations: undefined,
              modelUsed: undefined,
              evalScore: undefined,
              provenance: undefined,
            }));
            nextSocket.send(JSON.stringify({
              type: 'query',
              content: pending.query,
              sessionId: pending.sessionId,
              model: pending.model,
              requestId: pending.requestId,
              documentIds: pending.documentIds,
              versionScope: pending.versionScope,
            }));
          }
        };
        nextSocket.onmessage = (event) => {
          try {
            handleSocketMessage(JSON.parse(String(event.data)));
          } catch {
            // Ignore malformed server messages; the connection remains usable.
          }
        };
        nextSocket.onerror = () => nextSocket.close();
        nextSocket.onclose = () => {
          if (socket === nextSocket) socket = null;
          if (wsRef.current === nextSocket) wsRef.current = null;
          if (stopped) return;
          setConnectionState('disconnected');
          if (pendingRef.current?.transport === 'socket') {
            pendingRef.current.recovering = true;
          }
          scheduleReconnect();
        };
      } catch {
        socket = null;
        wsRef.current = null;
        setConnectionState('disconnected');
        scheduleReconnect();
      }
    };

    const onOnline = () => connect();
    const onFocus = () => connect();
    const onVisibility = () => {
      if (document.visibilityState === 'visible') connect();
    };
    const onOffline = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      socket?.close();
      socket = null;
      wsRef.current = null;
      setConnectionState('disconnected');
    };

    connect();
    window.addEventListener('online', onOnline);
    window.addEventListener('offline', onOffline);
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      stopped = true;
      mountedRef.current = false;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      restControllerRef.current?.abort();
      restControllerRef.current = null;
      socket?.close();
      socket = null;
      wsRef.current = null;
      window.removeEventListener('online', onOnline);
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [handleSocketMessage, updateAssistant]);

  const sendMessage = useCallback(async (
    queryText: string,
    selectedDocumentIds: string[] = [],
    selectedVersionScope: ChatVersionScope = 'auto',
  ) => {
    const query = queryText.trim();
    if (!query || pendingRef.current) return;

    const documentIds = [...new Set(
      selectedDocumentIds.slice(0, 10).map((documentId) => documentId.toLowerCase()),
    )];

    const requestId = crypto.randomUUID();
    const assistantMessageId = `asst_${requestId}`;
    const socketIsOpen = wsRef.current?.readyState === WebSocket.OPEN;
    pendingRef.current = {
      requestId,
      assistantMessageId,
      query,
      sessionId,
      model: selectedModel,
      documentIds,
      versionScope: selectedVersionScope,
      transport: socketIsOpen ? 'socket' : 'rest',
      recovering: false,
    };
    setMessages((previous) => [
      ...previous,
      {
        id: `user_${requestId}`,
        role: 'user',
        content: query,
        selectedDocumentIds: documentIds,
        selectedVersionScope,
        timestamp: timestamp(),
      },
      {
        id: assistantMessageId,
        role: 'assistant',
        content: '',
        timestamp: timestamp(),
      },
    ]);
    setIsStreaming(true);
    setAgentEvents([]);
    setCompletedRunId(null);

    if (socketIsOpen && wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'query',
        content: query,
        sessionId,
        model: selectedModel,
        requestId,
        documentIds,
        versionScope: selectedVersionScope,
      }));
      return;
    }

    const controller = new AbortController();
    restControllerRef.current = controller;
    try {
      const response = await gatewayFetch('/chat/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          sessionId,
          model: selectedModel,
          requestId,
          documentIds,
          versionScope: selectedVersionScope,
        }),
        signal: controller.signal,
      }, { profile: 'stream' });
      const data = asRecord(await response.json().catch(() => null));
      if (!response.ok) {
        throw new Error(
          (typeof data?.message === 'string' && data.message)
          || (typeof data?.detail === 'string' && data.detail)
          || 'The query could not be processed.',
        );
      }
      if (!data) throw new Error('The query service returned an invalid response.');
      if (!mountedRef.current || pendingRef.current?.requestId !== requestId) return;
      setAgentEvents(Array.isArray(data.agent_events)
        ? data.agent_events.map(asRecord).filter((item): item is Record<string, unknown> => item !== null)
          .map(normalizeAgentEvent).filter((item): item is AgentEvent => item !== null)
        : []);
      updateAssistant(assistantMessageId, (assistant) => ({
        ...assistant,
        content: typeof data.response === 'string' ? data.response : '',
        modelUsed: typeof data.model_used === 'string' ? data.model_used : undefined,
        evalScore: typeof data.eval_score === 'number' ? data.eval_score : undefined,
        citations: normalizeCitations(data.citations),
        claims: normalizeClaims(data.claims),
        answerStatus: normalizeAnswerStatus(data.answer_status),
        groundingProfile: typeof data.grounding_profile === 'string'
          ? data.grounding_profile
          : undefined,
        provenance: normalizeRunProvenance(data.provenance),
      }));
      if (finishPending(requestId)) {
        setCompletedRunId(typeof data.run_id === 'string' ? data.run_id : requestId);
      }
    } catch (error) {
      if (!mountedRef.current || controller.signal.aborted) return;
      failPending(
        error instanceof Error ? error.message : 'The query could not be processed.',
        requestId,
      );
    } finally {
      if (restControllerRef.current === controller) restControllerRef.current = null;
    }
  }, [failPending, finishPending, selectedModel, sessionId, updateAssistant]);

  const cancel = useCallback(() => {
    const pending = pendingRef.current;
    if (!pending) return;
    restControllerRef.current?.abort();
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'cancel', requestId: pending.requestId }));
    }
    failPending('The request was cancelled.', pending.requestId);
  }, [failPending]);

  return {
    messages,
    agentEvents,
    isStreaming,
    selectedModel,
    setSelectedModel,
    sendMessage,
    cancel,
    sessionId,
    setSessionId,
    connectionState,
    completedRunId,
  };
}
