from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import re
import threading

import numpy as np

from .paths import guarded
from .types import MemoryFault


class HashEmbedding:
    """Deterministic synthetic-test provider; not a substitute for semantic embeddings."""
    name = "synthetic-hash-v1"
    dimension = 384

    def encode(self, texts):
        result = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for index, text in enumerate(texts):
            tokens = re.findall(r"[a-zA-Z0-9]+|[^\W\d_]", text.casefold())
            tokens += [a+b for a, b in zip(tokens, tokens[1:])]
            for token in tokens:
                digest = hashlib.sha256(token.encode()).digest()
                result[index, int.from_bytes(digest[:4], "little") % self.dimension] += 1
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        return result / np.maximum(norms, 1e-12)


class LocalEmbedding:
    dimension = 1024

    def __init__(self, project: Path, model_path: Path, revision: str, device="cpu"):
        self.project = project
        self.path = guarded(project, str(model_path.relative_to(project)))
        self.name = f"BAAI/bge-m3@{revision}:dense:512:{device}"
        self.model = None
        self.device = device
        self.lock = threading.Lock()

    def encode(self, texts):
        with self.lock:
            if self.model is None:
                if not self.path.is_dir() or not (self.path / "config.json").exists():
                    raise MemoryFault("本地 embedding 尚未准备；当前使用全文与结构化检索", 503)
                lock_path = guarded(self.path, "model-lock.json")
                if not lock_path.is_file():
                    raise MemoryFault("本地模型缺少完整准备清单", 503)
                locked = json.loads(lock_path.read_text(encoding="utf-8"))
                if not any(f["path"] in ("pytorch_model.bin", "model.safetensors") for f in locked.get("files", [])):
                    raise MemoryFault("本地模型准备清单缺少权重文件", 503)
                for entry in locked["files"]:
                    path = guarded(self.path, entry["path"])
                    if not path.is_file() or path.stat().st_size != entry["size"]:
                        raise MemoryFault("本地模型文件不完整，请重新准备", 503)
                    if entry.get("sha256"):
                        digest = hashlib.sha256()
                        with path.open("rb") as stream:
                            for block in iter(lambda: stream.read(1024*1024), b""):
                                digest.update(block)
                        if digest.hexdigest() != entry["sha256"]:
                            raise MemoryFault("本地模型权重校验失败", 503)
                try:
                    import torch
                    from sentence_transformers import SentenceTransformer
                    torch.set_num_threads(min(8, max(1, torch.get_num_threads())))
                    self.model = SentenceTransformer(str(self.path), device=self.device, local_files_only=True)
                    self.model.max_seq_length = 512
                except (ImportError, OSError, RuntimeError) as error:
                    raise MemoryFault("本地 embedding 加载失败，请检查项目依赖与模型文件", 503) from error
            return self.model.encode(texts, batch_size=8, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False).astype(np.float32)


class VectorIndex:
    """Immutable bounded shards, loaded one at a time; DB revalidation is mandatory."""
    shard_rows = 65536
    def __init__(self, root: Path, embedding):
        self.root = root
        self.embedding = embedding
        self.generation = hashlib.sha256(embedding.name.encode()).hexdigest()[:24]
        self.directory = guarded(root, self.generation)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.last_error = None

    def clear_memory(self):
        # No long-lived mmap handles: files can be purged on Windows.
        self.directory = guarded(self.root, self.generation)

    def add(self, entries):
        if not entries:
            return
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            ids = [entry[0] for entry in entries]
            matrix = self.embedding.encode([entry[1] for entry in entries])
            self.write(ids, matrix)

    def write(self, ids, matrix):
        matrix = np.asarray(matrix, dtype=np.float32)
        if matrix.shape != (len(ids), self.embedding.dimension) or not np.isfinite(matrix).all():
            raise MemoryFault("embedding 返回维度或数值无效", 503)
        matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        self.directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256("|".join(ids).encode()).hexdigest()[:32]
        temp = guarded(self.directory, f"{name}.tmp")
        with temp.open("wb") as stream:
            np.save(stream, matrix, allow_pickle=False)
        temp.replace(guarded(self.directory, f"{name}.npy"))
        guarded(self.directory, f"{name}.json").write_text(json.dumps({"generation": self.generation, "ids": ids}), encoding="utf-8")

    def search(self, query, limit=50):
        with self.lock:
            vector = self.embedding.encode([query])[0]
            best = {}
            if not self.directory.is_dir():
                return []
            for meta_file in self.directory.glob("*.json"):
                meta = json.loads(guarded(self.directory, meta_file.name).read_text(encoding="utf-8"))
                if meta.get("generation") != self.generation:
                    continue
                matrix_path = guarded(self.directory, meta_file.with_suffix(".npy").name)
                if not matrix_path.is_file():
                    continue  # Incomplete/crashed shard is rebuilt by jobs.
                matrix = np.load(matrix_path, allow_pickle=False)
                try:
                    if matrix.shape != (len(meta["ids"]), self.embedding.dimension):
                        continue
                    for start in range(0, len(matrix), 4096):
                        # This is a streaming matrix-vector dot, not a large
                        # matrix multiply. BLAS spawning many threads per shard
                        # dominates latency on million-message indexes.
                        scores = np.einsum("ij,j->i", matrix[start:start+4096], vector, optimize=False)
                        count = min(limit, len(scores))
                        if not count:
                            continue
                        indexes = np.argpartition(scores, -count)[-count:]
                        for index in indexes:
                            chunk_id = meta["ids"][start+int(index)]
                            best[chunk_id] = max(best.get(chunk_id, -math.inf), float(scores[index]))
                finally:
                    del matrix
            return sorted(best.items(), key=lambda pair: pair[1], reverse=True)[:limit]

    def compact(self, active_ids):
        with self.lock:
            if not self.directory.exists():
                return
            files = list(self.directory.glob("*.json"))
            if len(files) < 16:
                return
            live, vectors = [], []
            seen = set()
            old_files = []
            metadata = [(path, json.loads(guarded(self.directory, path.name).read_text(encoding="utf-8"))) for path in files]
            for meta_file, meta in sorted(metadata, key=lambda pair: len(pair[1]["ids"])):
                # Keep full shards immutable. Re-merging them on every job both
                # wastes I/O and prevents small shards from being consolidated.
                if len(meta["ids"]) >= self.shard_rows:
                    continue
                if len(live) + len(meta["ids"]) > self.shard_rows:
                    break
                matrix_path = guarded(self.directory, meta_file.with_suffix(".npy").name)
                if not matrix_path.exists():
                    continue
                matrix = np.load(matrix_path, mmap_mode="r", allow_pickle=False)
                try:
                    for i, chunk_id in enumerate(meta["ids"]):
                        if chunk_id in active_ids and chunk_id not in seen:
                            live.append(chunk_id)
                            vectors.append(np.array(matrix[i]))
                            seen.add(chunk_id)
                finally:
                    matrix._mmap.close()
                old_files.extend([meta_file, matrix_path])
                if len(live) >= self.shard_rows:
                    break
            if not live or len(old_files) < 4:
                return
            new_name = hashlib.sha256("|".join(live).encode()).hexdigest()[:32]
            self.write(live, np.asarray(vectors))
            for path in old_files:
                if path.stem != new_name:
                    guarded(self.directory, path.name).unlink(missing_ok=True)


def hybrid_search(service, scope, query, limit=20, filters=None, *, focused=False, exclude_ids=()):
    filters = filters or {}
    exact = service.search_messages(scope, query, filters, limit=min(limit*2, 100))
    index = service.vector_index
    degraded = None
    vector_hits = []
    # Vector scanning neither reads nor holds the authoritative DB. Overlap it
    # with bounded lexical queries, then revalidate every source under the DB
    # lock. One helper per request; the index serializes its own file access.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-vector") as pool:
        vector_task = pool.submit(index.search, query, limit=100) if index else None
        relaxed = service.search_messages(scope, query, filters, limit=min(limit*2, 100), relaxed=True) if len(exact) < limit else []
        recent = service.search_messages(scope, query, filters, limit=min(limit*2, 100), relaxed=True, newest=True) if focused else []
        if vector_task:
            try:
                vector_hits = vector_task.result()
            except (MemoryFault, OSError, ValueError, RuntimeError) as error:
                degraded = "语义检索暂不可用，已使用原文和结构化查询"
                index.last_error = type(error).__name__
        else:
            degraded = "尚未启用本地语义模型，当前使用原文和结构化查询"
    candidates, ranks = {}, {}
    excluded = set(exclude_ids)
    for weight, rows in [(1.3, exact), (0.6, relaxed), (1.3, recent)]:
        for rank, row in enumerate(rows):
            if row["id"] in excluded:
                continue
            candidates[row["id"]] = row
            ranks[row["id"]] = ranks.get(row["id"], 0)+weight/(rank+20)
    with service.lock:
        service.check(scope)
        service.barrier()
        if service.vector_index is not index:
            vector_hits = []
            degraded = "语义索引已变化，当前使用原文和结构化查询"
        best_scoped_score = None
        for rank, (chunk_id, score) in enumerate(vector_hits):
            chunk = service.db.execute("SELECT * FROM ltm_chunks WHERE id=? AND user_id=? AND status='active'", (chunk_id, scope.user_id)).fetchone()
            if not chunk or chunk["generation"] != index.generation:
                continue
            refs, versions = json.loads(chunk["source_ids"]), json.loads(chunk["source_versions"])
            try:
                rows = service.read_messages(scope, refs)
            except MemoryFault:
                continue
            if any(row["revision"] != versions.get(row["id"]) for row in rows):
                continue
            # Search screens show broad candidates; automatic recall must not
            # inject the nearest arbitrary item when every match is weak.
            if focused:
                if not math.isfinite(score) or score < .35:
                    continue
                scoped = [row for row in rows if row["id"] not in excluded
                          and (not filters.get("role") or row["role"] == filters["role"])
                          and (not filters.get("session_id") or row["session_id"] == filters["session_id"])]
                if not scoped:
                    continue
                best_scoped_score = max(best_scoped_score or score, score)
                if score < best_scoped_score - .18:
                    continue
            for row in rows:
                if row["id"] in excluded:
                    continue
                if filters.get("session_id") and row["session_id"] != filters["session_id"]:
                    continue
                if filters.get("role") and row["role"] != filters["role"]:
                    continue
                if filters.get("since") is not None and row["created"] < filters["since"]:
                    continue
                if filters.get("until") is not None and row["created"] > filters["until"]:
                    continue
                candidates[row["id"]] = row
                ranks[row["id"]] = ranks.get(row["id"], 0)+1/(rank+20)
        # Sources can have changed while embedding ran; re-read every selected hit.
        rows = []
        for mid in sorted(ranks, key=ranks.get, reverse=True):
            try:
                current = service.read_messages(scope, [mid])[0]
            except MemoryFault:
                continue
            rows.append(current)
            if len(rows) == limit:
                break
        return {"messages": rows, "degraded": degraded}
