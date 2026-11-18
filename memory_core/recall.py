"""Bounded conversational recall, distinct from a broad history search."""
from __future__ import annotations

import re
import math

from .retrieval import hybrid_search
from .types import MemoryFault


# These remove question scaffolding, never rewrite the stored originals.
QUESTION_WORDS = re.compile(
    r"请问|请告诉我|告诉我|我想知道|你还记得|你记得|还记得|记不记得|"
    r"我的|我们之前|现在|目前|到底|在哪里|在哪儿|在哪|哪里|是什么|什么|"
    r"怎么样|怎样|有没有|是否|一下|吗|呢|[？?，,。！!：:；;]"
)
FOLLOWUP = re.compile(
    r"^(?:后来|之后|随后|现在|目前|其实|不对|不是|更正|纠正|补充|改成|改到|"
    r"移到|搬到|它|那个|那把|对了|不，|不,|刚才说错|我刚才说错)"
)
MEDIA_REQUEST = re.compile(r"图片|照片|相片|截图|图里|图中|那张|这张|看图|语音|录音|音频|听听|photo|image|audio", re.I)
ASSISTANT_RECALL = re.compile(
    r"(?:你|您)(?:(?:之前|以前|上次|当时|曾经|刚才)(?:给我|向我)?(?:说|建议|推荐|回复|回答|提议|告诉)|"
    r"说过|说的|建议的|推荐的|回复的|回答的|提议的|(?:给我的|的)(?:建议|推荐|回复|回答|提议))"
)
QUOTATION_SCAFFOLD = re.compile(
    r"(?:请)?核对(?:一下)?(?:助手)?(?:原话|原文)|(?:你|您)(?:之前|以前|上次|当时|曾经|刚才)"
)


def topic_overlap(query, row):
    text = row["body"] + " " + " ".join(a.get("recognition") or "" for a in row.get("attachments", []))
    text += " " + " ".join(r["correction"] for r in row.get("corrections", []))
    # Cross-session media needs a topic link in its text/recognition or an
    # explicit media request. Generic neighbouring chunk membership is not one.
    noise = set("我你他她它们的了是在有也就都很和与及把这那张个一上次以前说过请问能否还记得")
    meaningful = {c for c in query if "\u4e00" <= c <= "\u9fff" and c not in noise}
    words = re.findall(r"[a-z0-9_]{2,}", query.casefold())
    return bool(meaningful.intersection(text)) or any(word in text.casefold() for word in words)


def semantic_text(row):
    return "\n".join([row["body"], *[r["correction"] for r in row.get("corrections", [])],
                      *[a["recognition"] for a in row.get("attachments", []) if a.get("recognition")]])


def rerank(service, query, rows):
    index = service.vector_index
    if not rows or not index or not hasattr(index, "embedding"):
        return rows
    try:
        # Repeated utterances retain separate identities, but encoding identical
        # candidate text once per request avoids redundant model work. Include
        # corrections and recognition in the key; retain no cross-request cache.
        texts = [semantic_text(row) for row in rows]
        unique = list(dict.fromkeys([query, *texts]))
        vectors = index.embedding.encode(unique)
        positions = {text: i for i, text in enumerate(unique)}
        unique_scores = vectors @ vectors[positions[query]]
        scores = [unique_scores[positions[text]] for text in texts]
        peak = max(float(score) for score in scores)
        # Calibrated by the fixed paraphrase+distractor corpus. Whole-exchange
        # vector scores cannot establish that every message in a chunk is relevant.
        return [row for row, score in zip(rows, scores)
                if math.isfinite(float(score)) and float(score) >= max(.35, peak-.03)
                or query.casefold() in semantic_text(row).casefold()]
    except (MemoryFault, OSError, ValueError, RuntimeError):
        # Fulltext remains available when the semantic provider is unavailable.
        return [row for row in rows if topic_overlap(query, row)]


def recall_for_chat(service, scope, current, limit=12):
    asks_assistant = bool(ASSISTANT_RECALL.search(current["body"]))
    question = current["body"][:500]
    if asks_assistant:
        # Citation instructions describe how to answer, not the remembered
        # subject. Keep the substantive phrase for exact and semantic lookup.
        question = QUOTATION_SCAFFOLD.sub(" ", question)
    query = QUESTION_WORDS.sub(" ", question).strip()
    if not query:
        return {"messages": [], "degraded": None}
    # Assistant words can establish what the assistant previously said. Default
    # fact recall remains user-only; render_source labels assistant quotations.
    filters = {"role": "assistant" if asks_assistant else "user"}
    result = hybrid_search(service, scope, query, min(limit*3, 50), filters,
                           focused=True, exclude_ids=[current["id"]])
    selected = {row["id"]: row for row in rerank(service, query, result["messages"])
                if not row.get("attachments") or MEDIA_REQUEST.search(current["body"]) or topic_overlap(query, row)}
    with service.lock:
        # A hit is an entry point to an exchange, not a complete current fact.
        # Read nearby follow-ups directly, including messages not indexed yet.
        for hit in list(selected.values()):
            nearby = service.read_messages(scope, [hit["id"]], before=2, after=6)
            for row in nearby:
                if row["ordinal"] <= hit["ordinal"] or row["role"] != "user":
                    continue
                if row["id"] == current["id"]:
                    break
                followup=row["body"].strip()
                if row.get("attachments"):
                    audio=row["attachments"]
                    # A recognized voice update is a user follow-up too. Never
                    # infer continuity from an image or unrecognized recording.
                    if any(not a["mime"].startswith("audio/") or not a.get("recognition") for a in audio):
                        break
                    if followup in ("", "【语音】", "[语音]"):
                        followup="\n".join(a["recognition"] for a in audio).strip()
                if not FOLLOWUP.match(followup):
                    break
                selected[row["id"]] = row
    # New changes get budget priority; presentation is chronological in chat.
    result["messages"] = sorted(selected.values(), key=lambda row: (row["created"], row.get("pk", 0)), reverse=True)[:limit]
    return result
