from abc import ABC, abstractmethod
import re
from typing import Any, Dict, List, Optional


TextSpan = tuple[int, int]


def trim_text_span(text: str, start: int = 0, end: Optional[int] = None) -> TextSpan:
    """Return non-whitespace bounds without changing text inside the span."""

    bounded_end = len(text) if end is None else end
    if start < 0 or bounded_end < start or bounded_end > len(text):
        raise ValueError("text span is outside the source text")
    while start < bounded_end and text[start].isspace():
        start += 1
    while bounded_end > start and text[bounded_end - 1].isspace():
        bounded_end -= 1
    return start, bounded_end


def _sentence_spans(text: str) -> List[TextSpan]:
    spans: List[TextSpan] = []
    cursor = 0
    for separator in re.finditer(r"(?<=[.!?])\s+", text):
        start, end = trim_text_span(text, cursor, separator.start())
        if start < end:
            spans.append((start, end))
        cursor = separator.end()
    start, end = trim_text_span(text, cursor, len(text))
    if start < end:
        spans.append((start, end))
    return spans


def _oversized_sentence_spans(
    text: str,
    sentence_span: TextSpan,
    target_tokens: int,
    overlap_tokens: int,
) -> List[TextSpan]:
    start, end = sentence_span
    if estimate_tokens(text[start:end]) <= target_tokens:
        return [sentence_span]

    words = list(re.finditer(r"\S+", text[start:end]))
    available_tokens = max(1, target_tokens - overlap_tokens)
    words_per_piece = max(1, int(available_tokens / 1.3))
    spans: List[TextSpan] = []
    for index in range(0, len(words), words_per_piece):
        group = words[index:index + words_per_piece]
        spans.append((start + group[0].start(), start + group[-1].end()))
    return spans


def _paragraph_spans(text: str) -> List[TextSpan]:
    spans: List[TextSpan] = []
    cursor = 0
    for separator in re.finditer(r"\n[ \t]*\n+", text):
        start, end = trim_text_span(text, cursor, separator.start())
        if start < end:
            spans.append((start, end))
        cursor = separator.end()
    start, end = trim_text_span(text, cursor, len(text))
    if start < end:
        spans.append((start, end))
    return spans

class Chunk:
    def __init__(
        self,
        content: str,
        contextualized_content: str,
        chunk_index: int,
        token_count: int,
        section_title: str = "",
        page_number: Optional[int] = None,
        start_char: int = 0,
        end_char: int = 0
    ):
        self.content = content
        self.contextualized_content = contextualized_content.strip()
        self.chunk_index = chunk_index
        self.token_count = token_count
        self.section_title = section_title
        self.page_number = page_number
        self.start_char = start_char
        self.end_char = end_char

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "contextualized_content": self.contextualized_content,
            "chunk_index": self.chunk_index,
            "token_count": self.token_count,
            "section_title": self.section_title,
            "page_number": self.page_number,
            "start_char": self.start_char,
            "end_char": self.end_char
        }

def estimate_tokens(text: str) -> int:
    # Heuristic: 1 token ≈ 4 characters or ~0.75 words
    words = text.split()
    return max(1, int(len(words) * 1.3))

def split_into_sentences(text: str) -> List[str]:
    return [text[start:end] for start, end in _sentence_spans(text)]


def split_oversized_sentence(
    sentence: str,
    target_tokens: int,
    overlap_tokens: int,
) -> List[str]:
    return [
        sentence[start:end]
        for start, end in _oversized_sentence_spans(
            sentence,
            trim_text_span(sentence),
            target_tokens,
            overlap_tokens,
        )
    ]

class BaseChunker(ABC):
    @abstractmethod
    def chunk(self, text: str, document_title: str, section_title: str = "", page_number: Optional[int] = None) -> List[Chunk]:
        pass

class TokenChunker(BaseChunker):
    def __init__(self, target_chunk_tokens: int = 400, overlap_tokens: int = 50):
        self.target_chunk_tokens = target_chunk_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, text: str, document_title: str, section_title: str = "", page_number: Optional[int] = None) -> List[Chunk]:
        sentence_spans = [
            piece_span
            for sentence_span in _sentence_spans(text)
            for piece_span in _oversized_sentence_spans(
                text,
                sentence_span,
                self.target_chunk_tokens,
                self.overlap_tokens,
            )
        ]
        if not sentence_spans:
            return []

        chunks: List[Chunk] = []
        current_sentences: List[TextSpan] = []
        current_tokens = 0
        chunk_idx = 0

        for sentence_span in sentence_spans:
            sentence = text[sentence_span[0]:sentence_span[1]]
            stokens = estimate_tokens(sentence)
            if current_tokens + stokens > self.target_chunk_tokens and current_sentences:
                chunk_start = current_sentences[0][0]
                chunk_end = current_sentences[-1][1]
                chunk_raw = text[chunk_start:chunk_end]
                ctx_header = f"{document_title} > {section_title}: " if section_title else f"{document_title}: "
                contextualized = f"{ctx_header}{chunk_raw}"
                
                chunks.append(Chunk(
                    content=chunk_raw,
                    contextualized_content=contextualized,
                    chunk_index=chunk_idx,
                    token_count=estimate_tokens(chunk_raw),
                    section_title=section_title,
                    page_number=page_number,
                    start_char=chunk_start,
                    end_char=chunk_end,
                ))
                chunk_idx += 1

                # Retain overlap sentences
                overlap_kept: List[TextSpan] = []
                overlap_toks = 0
                for retained_span in reversed(current_sentences):
                    t = estimate_tokens(text[retained_span[0]:retained_span[1]])
                    if overlap_toks + t <= self.overlap_tokens:
                        overlap_kept.insert(0, retained_span)
                        overlap_toks += t
                    else:
                        break
                current_sentences = overlap_kept
                current_tokens = overlap_toks

                while (
                    current_sentences
                    and current_tokens + stokens > self.target_chunk_tokens
                ):
                    removed_span = current_sentences.pop(0)
                    current_tokens -= estimate_tokens(
                        text[removed_span[0]:removed_span[1]]
                    )

            current_sentences.append(sentence_span)
            current_tokens += stokens

        if current_sentences:
            chunk_start = current_sentences[0][0]
            chunk_end = current_sentences[-1][1]
            chunk_raw = text[chunk_start:chunk_end]
            ctx_header = f"{document_title} > {section_title}: " if section_title else f"{document_title}: "
            contextualized = f"{ctx_header}{chunk_raw}"
            chunks.append(Chunk(
                content=chunk_raw,
                contextualized_content=contextualized,
                chunk_index=chunk_idx,
                token_count=estimate_tokens(chunk_raw),
                section_title=section_title,
                page_number=page_number,
                start_char=chunk_start,
                end_char=chunk_end,
            ))

        return chunks

class RecursiveChunker(BaseChunker):
    def __init__(self, max_tokens: int = 400, overlap_tokens: int = 50):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def chunk(self, text: str, document_title: str, section_title: str = "", page_number: Optional[int] = None) -> List[Chunk]:
        chunks: List[Chunk] = []
        chunk_idx = 0

        for paragraph_start, paragraph_end in _paragraph_spans(text):
            clean_p = text[paragraph_start:paragraph_end]
            p_tokens = estimate_tokens(clean_p)
            
            if p_tokens <= self.max_tokens:
                ctx_header = f"{document_title} > {section_title}: " if section_title else f"{document_title}: "
                chunks.append(Chunk(
                    content=clean_p,
                    contextualized_content=f"{ctx_header}{clean_p}",
                    chunk_index=chunk_idx,
                    token_count=p_tokens,
                    section_title=section_title,
                    page_number=page_number,
                    start_char=paragraph_start,
                    end_char=paragraph_end,
                ))
                chunk_idx += 1
            else:
                # Sub-chunk with TokenChunker
                token_chunker = TokenChunker(target_chunk_tokens=self.max_tokens, overlap_tokens=self.overlap_tokens)
                sub_chunks = token_chunker.chunk(clean_p, document_title, section_title, page_number)
                for sc in sub_chunks:
                    sc.chunk_index = chunk_idx
                    sc.start_char += paragraph_start
                    sc.end_char += paragraph_start
                    chunks.append(sc)
                    chunk_idx += 1

        return chunks

class SentenceWindowChunker(BaseChunker):
    """Token-bounded windows that preserve sentence boundaries when possible."""

    def __init__(self, max_tokens: int = 450):
        self.max_tokens = max_tokens

    def chunk(self, text: str, document_title: str, section_title: str = "", page_number: Optional[int] = None) -> List[Chunk]:
        return TokenChunker(target_chunk_tokens=self.max_tokens, overlap_tokens=40).chunk(
            text, document_title, section_title, page_number
        )

class ChunkerFactory:
    SUPPORTED_STRATEGIES = {"token", "sentence", "recursive"}

    @staticmethod
    def normalize_strategy(strategy: str = "token") -> str:
        normalized = (strategy or "token").strip().lower()
        # Compatibility for upload clients created before migration 028. The
        # behavior was always sentence-window chunking; only the name changes.
        if normalized == "semantic":
            normalized = "sentence"
        if normalized not in ChunkerFactory.SUPPORTED_STRATEGIES:
            supported = ", ".join(sorted(ChunkerFactory.SUPPORTED_STRATEGIES))
            raise ValueError(f"Unsupported chunk strategy. Choose one of: {supported}.")
        return normalized

    @staticmethod
    def get_chunker(strategy: str = "token") -> BaseChunker:
        strat = ChunkerFactory.normalize_strategy(strategy)
        if strat == "sentence":
            return SentenceWindowChunker()
        if strat == "recursive":
            return RecursiveChunker()
        return TokenChunker()
