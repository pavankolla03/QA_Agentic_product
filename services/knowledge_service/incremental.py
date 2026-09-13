"""Incremental indexing — embed only what changed.

Embeddings are the second-largest recurring cost after generation, and almost
all of it is waste: a repository of 400 files rarely changes more than a handful
between runs. This orchestrates the Repository Map's file-level diff into
chunk-level embedding work, so a no-op run costs nothing and a one-file change
costs one file's worth of embeddings.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from packages.aiqa_types.models import RepoProfile, new_id
from services.knowledge_service.code_parser import chunk_text
from services.knowledge_service.indexer import _chunk_kind
from services.knowledge_service.repository_map import (
    IndexDelta,
    RepositoryMap,
    RepositoryMapper,
)
from services.observability.db import session_scope
from services.observability.models import KnowledgeChunkRow

log = logging.getLogger("aiqa.incremental")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]


class IncrementalIndexer:
    """Keeps the Repository Map and the embedded chunk store in step."""

    def __init__(self, project_id: str, project_root: str | Path) -> None:
        self.project_id = project_id
        self.root = Path(project_root).resolve()
        self.mapper = RepositoryMapper(project_id, self.root)

    # ------------------------------------------------------------------ #
    async def sync(
        self,
        router: Any = None,
        *,
        force: bool = False,
        scope: str = "repository",
        budget: Any = None,
    ) -> tuple[RepoProfile, IndexDelta, RepositoryMap]:
        """Bring the map and the chunk index up to date. Returns what changed."""
        previous = RepositoryMap.load(self.root)
        repo_map, delta = self.mapper.build(previous=previous, force=force)

        if delta.reused_from_cache:
            # Nothing to do at all — the cheapest possible path.
            existing = self._count_chunks(scope)
            if existing:
                profile = repo_map.to_profile()
                profile.indexed_chunks = existing
                if budget is not None and hasattr(budget, "record_saving"):
                    budget.record_saving(self._estimate_tokens(repo_map))
                return profile, delta, repo_map
            # Map cached but chunks were wiped: fall through and re-embed.
            delta.reused_from_cache = False
            delta.modified = list(repo_map.files)

        # ---- 1. drop chunks for removed and modified files ------------- #
        stale = set(delta.removed) | set(delta.modified)
        if stale:
            with session_scope() as session:
                session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.project_id == self.project_id,
                        KnowledgeChunkRow.scope == scope,
                        KnowledgeChunkRow.file_path.in_(list(stale)),
                    )
                )

        # ---- 2. chunk only the files that changed ---------------------- #
        pending: list[dict[str, Any]] = []
        for rel in delta.changed:
            path = self.root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entry = repo_map.files.get(rel, {})
            primary = (entry.get("symbols") or [{}])[0].get("name", "") if entry.get("symbols") else ""
            for start, end, content in chunk_text(text):
                if not content.strip():
                    continue
                pending.append(
                    {
                        "file_path": rel,
                        "symbol": primary,
                        "start_line": start,
                        "end_line": end,
                        "content": content,
                        "kind": _chunk_kind(rel),
                        "hash": _hash(content),
                    }
                )

        # ---- 3. reuse embeddings for chunks whose text is unchanged ---- #
        # A moved or reformatted file often yields chunks we have already paid
        # to embed; content-hash lookup recovers those for free.
        reusable: dict[str, tuple[list[float], str]] = {}
        if pending:
            hashes = [c["hash"] for c in pending]
            with session_scope() as session:
                rows = session.execute(
                    select(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.project_id == self.project_id,
                        KnowledgeChunkRow.content_hash.in_(hashes),
                    )
                ).scalars()
                for row in rows:
                    if row.embedding:
                        reusable[row.content_hash] = (row.embedding, row.embedding_model)

        to_embed = [c for c in pending if c["hash"] not in reusable]
        delta.embeddings_skipped = len(pending) - len(to_embed)

        vectors: list[list[float]] = []
        model_name = "none"
        if to_embed and router is not None:
            texts = [f"{c['file_path']}\n{c['content']}" for c in to_embed]
            try:
                vectors = await router.embed(texts, budget=budget)
                resolved = await router.resolve("embedding")
                model_name = resolved.model if resolved else "unknown"
                delta.embeddings_computed = len(vectors)
            except Exception as exc:  # noqa: BLE001 - indexing must not fail a run
                log.warning("embedding failed; storing chunks without vectors: %s", exc)
                vectors = []

        vector_by_hash = {
            chunk["hash"]: vectors[index] for index, chunk in enumerate(to_embed) if index < len(vectors)
        }

        # ---- 4. persist ------------------------------------------------ #
        if pending:
            with session_scope() as session:
                for chunk in pending:
                    cached = reusable.get(chunk["hash"])
                    embedding = vector_by_hash.get(chunk["hash"], cached[0] if cached else [])
                    session.add(
                        KnowledgeChunkRow(
                            id=new_id("chk"),
                            project_id=self.project_id,
                            scope=scope,
                            kind=chunk["kind"],
                            file_path=chunk["file_path"],
                            symbol=chunk["symbol"],
                            start_line=chunk["start_line"],
                            end_line=chunk["end_line"],
                            content=chunk["content"],
                            summary=f"{chunk['file_path']}:{chunk['start_line']}-{chunk['end_line']}",
                            content_hash=chunk["hash"],
                            embedding=embedding,
                            embedding_model=model_name if chunk["hash"] in vector_by_hash
                            else (cached[1] if cached else ""),
                            tokens=max(1, len(chunk["content"]) // 4),
                        )
                    )

        repo_map.save(self.root)

        profile = repo_map.to_profile()
        profile.indexed_chunks = self._count_chunks(scope)

        if budget is not None and hasattr(budget, "record_saving") and delta.unchanged:
            budget.record_saving(self._estimate_tokens(repo_map, only_unchanged=delta.unchanged))

        log.info("index sync for %s: %s", self.project_id, delta.summary())
        return profile, delta, repo_map

    # ------------------------------------------------------------------ #
    def _count_chunks(self, scope: str) -> int:
        from sqlalchemy import func

        with session_scope() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(KnowledgeChunkRow)
                    .where(
                        KnowledgeChunkRow.project_id == self.project_id,
                        KnowledgeChunkRow.scope == scope,
                    )
                ).scalar_one()
                or 0
            )

    @staticmethod
    def _estimate_tokens(repo_map: RepositoryMap, only_unchanged: int = 0) -> int:
        """Tokens we did not have to send because the map was reused."""
        entries = list(repo_map.files.values())
        if only_unchanged:
            entries = entries[:only_unchanged]
        return sum(max(1, int(entry.get("size", 0)) // 4) for entry in entries)
