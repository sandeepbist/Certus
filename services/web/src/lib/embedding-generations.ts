export type EmbeddingGenerationStatus =
  | 'building'
  | 'paused'
  | 'ready'
  | 'active'
  | 'retired'
  | 'stale'
  | 'failed'
  | 'cancelled'
  | 'rolled_back';

export type EmbeddingGenerationAction =
  | 'pause'
  | 'resume'
  | 'cancel'
  | 'activate'
  | 'rollback';

export type EmbeddingGeneration = {
  id: string;
  embedding_profile: string;
  creation_reason: 'operator' | 'corpus_refresh';
  status: EmbeddingGenerationStatus;
  corpus: {
    snapshot_revision: number;
    current_revision: number;
    is_current: boolean;
  };
  progress: {
    expected: number;
    embedded: number;
    failed: number;
    remaining: number;
  };
  previous_generation_id: string | null;
  evaluation: {
    decision?: string | null;
    gates_passed?: boolean | null;
    quality_claim?: string | null;
  } | null;
  has_error: boolean;
  created_at: string;
  updated_at: string;
  rollback_until: string | null;
  last_paused_at: string | null;
  last_resumed_at: string | null;
  last_pause_reason: string | null;
};

const profileLabels: Record<string, string> = {
  'embedding-space:v1:local:local-lexical-v2:1536': 'Local lexical fallback',
  'embedding-space:v1:openai:text-embedding-3-small:1536': 'OpenAI text-embedding-3-small',
  'embedding-space:v1:openai:text-embedding-3-large:1536': 'OpenAI text-embedding-3-large',
};

export function embeddingProfileLabel(profile: string): string {
  return profileLabels[profile] || profile;
}

export function embeddingGenerationActions(
  generation: EmbeddingGeneration,
  operator: boolean,
  now = Date.now(),
): EmbeddingGenerationAction[] {
  if (!operator) return [];
  if (generation.status === 'building') return ['pause', 'cancel'];
  if (generation.status === 'paused') return ['resume', 'cancel'];
  if (generation.status === 'ready') return ['activate', 'cancel'];
  if (
    generation.status === 'active'
    && generation.previous_generation_id
    && generation.rollback_until
    && Date.parse(generation.rollback_until) > now
  ) {
    return ['rollback'];
  }
  return [];
}

export function embeddingGenerationProgress(generation: EmbeddingGeneration): number {
  if (generation.progress.expected === 0) return 100;
  return Math.max(
    0,
    Math.min(100, Math.round(
      (generation.progress.embedded / generation.progress.expected) * 100,
    )),
  );
}
