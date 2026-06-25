"""RAG 检索模块 — 共享 RAGRetriever 与 Evidence 结构。"""

from app.rag.retriever import RAGRetriever
from app.rag.schemas import (
    Evidence,
    FulltextHit,
    MergedHit,
    RAGError,
    RAGUnavailableError,
    VectorHit,
)

__all__ = [
    "RAGRetriever",
    "Evidence",
    "FulltextHit",
    "MergedHit",
    "RAGError",
    "RAGUnavailableError",
    "VectorHit",
]
