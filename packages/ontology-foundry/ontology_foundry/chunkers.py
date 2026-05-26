from __future__ import annotations

import hashlib
import re
from typing import Protocol

from ontology_foundry.models import ChunkMetadata, Document, DocumentChunk


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class DocumentChunker(Protocol):
    def chunk(self, document: Document) -> list[DocumentChunk]:
        ...


class MarkdownHeaderChunker:
    """
    Splits markdown on ATX headings (# .. ## ..); fallback single chunk.
    Emits chunk metadata with heading_path and adjacency (§3.5).
    """

    def __init__(self, max_chars_soft: int = 12000) -> None:
        self.max_chars_soft = max_chars_soft

    def chunk(self, document: Document) -> list[DocumentChunk]:
        text = document.text
        pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if not matches:
            return [_single_chunk(document, text, heading_path="")]

        pieces: list[tuple[str, str]] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            title = m.group(2).strip()
            section = text[start:end].strip()
            pieces.append((title, section))
            if len(section) > self.max_chars_soft:
                # Oversized section: delegate split inside RecursiveTextChunker would duplicate logic;
                # keep one chunk but production may subdivide further.
                pass

        out: list[DocumentChunk] = []
        for idx, (title, section_text) in enumerate(pieces):
            chunk_id = f"{document.doc_id}/md/{idx}"
            prev_id = f"{document.doc_id}/md/{idx - 1}" if idx > 0 else None
            next_id = f"{document.doc_id}/md/{idx + 1}" if idx + 1 < len(pieces) else None
            approx_tokens = max(1, len(section_text.split()))
            meta = ChunkMetadata(
                chunk_id=chunk_id,
                parent_doc_id=document.doc_id,
                heading_path=title,
                prev_chunk_id=prev_id,
                next_chunk_id=next_id,
                token_count=approx_tokens,
                content_hash=_hash_text(section_text),
            )
            out.append(DocumentChunk(metadata=meta, text=section_text))

        return out


class RecursiveTextChunker:
    """
    Generic fallback: split into overlapping windows by character budget
    (~800–1200 tokens approximated by chars when tokenizer unavailable).
    """

    def __init__(self, max_chars: int = 3200, overlap_chars: int = 200) -> None:
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def chunk(self, document: Document) -> list[DocumentChunk]:
        text = document.text
        if not text:
            return []

        chunks: list[DocumentChunk] = []
        start = 0
        idx = 0
        n = len(text)
        while start < n:
            end = min(n, start + self.max_chars)
            piece = text[start:end]
            chunk_id = f"{document.doc_id}/rt/{idx}"
            prev_id = f"{document.doc_id}/rt/{idx - 1}" if idx > 0 else None
            next_end = min(n, end + self.max_chars - self.overlap_chars)
            next_id = f"{document.doc_id}/rt/{idx + 1}" if next_end > end else None
            meta = ChunkMetadata(
                chunk_id=chunk_id,
                parent_doc_id=document.doc_id,
                heading_path="",
                prev_chunk_id=prev_id,
                next_chunk_id=next_id,
                token_count=max(1, len(piece.split())),
                content_hash=_hash_text(piece),
            )
            chunks.append(DocumentChunk(metadata=meta, text=piece))
            if end >= n:
                break
            start = max(0, end - self.overlap_chars)
            idx += 1

        return chunks


def _single_chunk(document: Document, text: str, heading_path: str) -> DocumentChunk:
    chunk_id = f"{document.doc_id}/full/0"
    meta = ChunkMetadata(
        chunk_id=chunk_id,
        parent_doc_id=document.doc_id,
        heading_path=heading_path,
        prev_chunk_id=None,
        next_chunk_id=None,
        token_count=max(1, len(text.split())),
        content_hash=_hash_text(text),
    )
    return DocumentChunk(metadata=meta, text=text)
