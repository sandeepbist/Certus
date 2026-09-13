import { describe, expect, test } from 'bun:test';

import {
  EmbeddingGeneration,
  embeddingGenerationActions,
  embeddingGenerationProgress,
  embeddingProfileLabel,
} from '../../services/web/src/lib/embedding-generations';

function generation(
  status: EmbeddingGeneration['status'],
  overrides: Partial<EmbeddingGeneration> = {},
): EmbeddingGeneration {
  return {
    id: '10000000-0000-4000-8000-000000000001',
    embedding_profile: 'embedding-space:v1:local:local-lexical-v2:1536',
    creation_reason: 'operator',
    status,
    corpus: { snapshot_revision: 2, current_revision: 2, is_current: true },
    progress: { expected: 10, embedded: 4, failed: 0, remaining: 6 },
    previous_generation_id: null,
    evaluation: null,
    has_error: false,
    created_at: '2026-09-13T00:00:00Z',
    updated_at: '2026-09-13T00:00:00Z',
    rollback_until: null,
    last_paused_at: null,
    last_resumed_at: null,
    last_pause_reason: null,
    ...overrides,
  };
}

describe('embedding generation workspace controls', () => {
  test('maps only valid operator transitions', () => {
    expect(embeddingGenerationActions(generation('building'), true))
      .toEqual(['pause', 'cancel']);
    expect(embeddingGenerationActions(generation('paused'), true))
      .toEqual(['resume', 'cancel']);
    expect(embeddingGenerationActions(generation('ready'), true))
      .toEqual(['activate', 'cancel']);
    expect(embeddingGenerationActions(generation('building'), false)).toEqual([]);
    expect(embeddingGenerationActions(generation('failed'), true)).toEqual([]);
  });

  test('offers rollback only inside a retained predecessor window', () => {
    const active = generation('active', {
      previous_generation_id: '20000000-0000-4000-8000-000000000001',
      rollback_until: '2026-09-14T00:00:00Z',
    });
    expect(embeddingGenerationActions(active, true, Date.parse('2026-09-13T00:00:00Z')))
      .toEqual(['rollback']);
    expect(embeddingGenerationActions(active, true, Date.parse('2026-09-15T00:00:00Z')))
      .toEqual([]);
  });

  test('bounds progress and labels canonical profiles', () => {
    expect(embeddingGenerationProgress(generation('building'))).toBe(40);
    expect(embeddingGenerationProgress(generation('ready', {
      progress: { expected: 0, embedded: 0, failed: 0, remaining: 0 },
    }))).toBe(100);
    expect(embeddingProfileLabel('embedding-space:v1:local:local-lexical-v2:1536'))
      .toBe('Local lexical fallback');
    expect(embeddingProfileLabel('future-profile')).toBe('future-profile');
  });
});
