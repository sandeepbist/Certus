export interface AuthUserContext {
  userId: string;
  tenantId: string;
  roles: string[];
  tokenBudget: number;
  authType: 'session' | 'api_key';
  scopes: string[];
}

export type CircuitBreakerState = 'CLOSED' | 'OPEN' | 'HALF_OPEN';

export interface CircuitBreakerConfig {
  failureThreshold: number; // e.g. 5 failures
  cooldownMs: number;       // e.g. 30,000ms
  windowMs: number;         // e.g. 60,000ms
}

export interface WsClientMessage {
  type: 'query' | 'ping' | 'cancel';
  content?: string;
  sessionId?: string;
  model?: string;
}

export interface WsServerMessage {
  type: 'token' | 'agent_event' | 'citations' | 'done' | 'error' | 'pong';
  content?: string;
  agent?: string;
  action?: string;
  citations?: Array<{
    chunk_id: string;
    document_id: string;
    document_version_id: string;
    derivation_id?: string;
    parsed_artifact_id?: string;
    version_number: number;
    document_title: string;
    quote: string;
    page_number?: number;
    start_char?: number | null;
    end_char?: number | null;
    text_locator_status?: 'exact' | 'unavailable';
    text_locator_profile?: string;
    quote_sha256?: string;
    evidence_id?: string;
    claim_ids?: string[];
    support_scope?: 'retrieved_context_not_claim_aligned' | 'atomic_claim_selected';
    verification_status?: 'mechanical_checks_passed_semantic_not_evaluated';
    content_hash: string;
    source_time?: string | null;
    recorded_at?: string;
    is_current_version: boolean;
  }>;
  claims?: Array<{
    claim_id: string;
    text: string;
    source_refs: Array<{
      source_id: string;
      source_kind: 'document' | 'memory' | 'graph' | 'tool';
      content_sha256: string;
    }>;
    mechanical_validation: {
      status: 'passed';
      exact_anchors?: string[];
      significant_term_coverage?: number;
      semantic_entailment_checked: false;
    };
    semantic_support_status: 'not_evaluated';
  }>;
  answer_status?: 'answered' | 'extractive' | 'insufficient_evidence' | 'conflicting_evidence' | 'action_completed';
  grounding_profile?: string;
  provenance?: {
    evidence_manifest_profile: 'certus_typed_evidence_manifest:v1';
    evidence_manifest_sha256: string;
    evidence_manifest_db_sha256?: string | null;
    evidence_source_count: number;
    generation_profile: 'certus_grounded_generation:v1';
    generation_profile_sha256: string;
    generation_profile_db_sha256?: string | null;
    execution_mode: 'openai_structured' | 'local_extractive' | 'local_extractive_fallback' | 'local_action';
    provider: 'openai' | 'certus_local';
    requested_model: string;
    returned_model?: string | null;
    model_revision_locked: boolean;
    prompt_profile: 'certus_atomic_claim_prompt:v1';
    validator_profile: 'certus_mechanical_claim_validator:v1';
    conversation_context_profile?: 'certus_conversation_context:v1';
    conversation_context_sha256?: string;
    conversation_context_db_sha256?: string | null;
    conversation_turn_count?: number;
  };
  replay_of_run_id?: string | null;
  replay_mode?: 'original' | 'frozen_evidence' | 'fresh_retrieval';
  evalScore?: number;
  code?: string;
  message?: string;
  retryAfter?: number;
}
