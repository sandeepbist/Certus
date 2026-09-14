import { describe, expect, test } from 'bun:test';

import {
  normalizeAnswerStatus,
  normalizeCitations,
  normalizeClaims,
  normalizeRunProvenance,
} from '../../services/web/src/hooks/useStreamingChat';


describe('chat grounding projection', () => {
  test('accepts a complete mechanically validated claim and selected citation', () => {
    const claims = normalizeClaims([{
      claim_id: 'C1',
      text: 'The approved budget is 42 credits.',
      source_refs: [{
        source_id: 'D1',
        source_kind: 'document',
        content_sha256: 'a'.repeat(64),
        claim_span_sha256: 'd'.repeat(64),
      }],
      mechanical_validation: {
        status: 'passed',
        exact_anchors: ['42'],
        significant_term_coverage: 1,
        semantic_entailment_checked: false,
      },
      semantic_support_status: 'not_evaluated',
    }]);
    const citations = normalizeCitations([{
      evidence_id: 'D1',
      claim_ids: ['C1'],
      chunk_id: 'chunk-1',
      document_id: 'document-1',
      document_title: 'Budget',
      quote: 'The approved budget is 42 credits.',
      support_scope: 'atomic_claim_selected',
      verification_status: 'mechanical_checks_passed_semantic_not_evaluated',
      claim_span_profile: 'certus_claim_aligned_sentence_span:unicode_code_point:v1',
      claim_spans: [{
        claim_id: 'C1',
        profile: 'certus_claim_aligned_sentence_span:unicode_code_point:v1',
        selection_status: 'claim_aligned',
        quote: 'The approved budget is 42 credits.',
        quote_sha256: 'd'.repeat(64),
        relative_start_char: 12,
        relative_end_char: 46,
        start_char: 112,
        end_char: 146,
        text_locator_status: 'exact',
        semantic_entailment_checked: false,
      }],
    }]);

    expect(claims).toHaveLength(1);
    expect(claims[0]?.sourceRefs[0]?.sourceId).toBe('D1');
    expect(citations).toHaveLength(1);
    expect(citations[0]?.claimIds).toEqual(['C1']);
    expect(citations[0]?.claimSpans?.[0]?.startChar).toBe(112);
    expect(claims[0]?.sourceRefs[0]?.claimSpanSha256).toBe('d'.repeat(64));
    expect(normalizeAnswerStatus('answered')).toBe('answered');
    expect(normalizeRunProvenance({
      evidence_manifest_profile: 'certus_typed_evidence_manifest:v1',
      evidence_manifest_sha256: 'b'.repeat(64),
      evidence_source_count: 1,
      generation_profile: 'certus_grounded_generation:v1',
      generation_profile_sha256: 'c'.repeat(64),
      execution_mode: 'local_extractive',
      provider: 'certus_local',
      requested_model: 'local-extractive',
      returned_model: 'local-extractive',
      model_revision_locked: false,
      prompt_profile: 'certus_atomic_claim_prompt:v1',
      validator_profile: 'certus_mechanical_claim_validator:v1',
      conversation_context_profile: 'certus_conversation_context:v1',
      conversation_context_sha256: 'd'.repeat(64),
      conversation_context_db_sha256: 'e'.repeat(64),
      conversation_turn_count: 2,
    })?.conversationTurnCount).toBe(2);
  });

  test('drops malformed hashes, dangling citation metadata, and invented statuses', () => {
    expect(normalizeClaims([{
      claim_id: 'C1',
      text: 'Claim',
      source_refs: [{
        source_id: 'D1',
        source_kind: 'document',
        content_sha256: 'not-a-sha256',
      }],
      mechanical_validation: {
        status: 'passed',
        semantic_entailment_checked: false,
      },
      semantic_support_status: 'not_evaluated',
    }])).toEqual([]);
    expect(normalizeCitations([{
      chunk_id: 'chunk-1',
      document_id: 'document-1',
      document_title: 'Budget',
      support_scope: 'atomic_claim_selected',
    }])).toEqual([]);
    expect(normalizeCitations([{
      evidence_id: 'D1',
      claim_ids: ['C1'],
      chunk_id: 'chunk-1',
      document_id: 'document-1',
      document_title: 'Budget',
      quote: 'Chunk',
      support_scope: 'atomic_claim_selected',
      verification_status: 'mechanical_checks_passed_semantic_not_evaluated',
      claim_span_profile: 'certus_claim_aligned_sentence_span:unicode_code_point:v1',
      claim_spans: [{
        claim_id: 'C2',
        profile: 'certus_claim_aligned_sentence_span:unicode_code_point:v1',
        selection_status: 'claim_aligned',
        quote: 'Wrong claim',
        quote_sha256: 'd'.repeat(64),
        relative_start_char: 0,
        relative_end_char: 11,
        text_locator_status: 'unavailable',
        semantic_entailment_checked: false,
      }],
    }])).toEqual([]);
    expect(normalizeAnswerStatus('semantically_verified')).toBeUndefined();
    expect(normalizeRunProvenance({
      evidence_manifest_profile: 'certus_typed_evidence_manifest:v1',
      evidence_manifest_sha256: 'not-a-hash',
    })).toBeUndefined();
  });
});
