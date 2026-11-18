"""RAG 知识库模块 —— 提供向量检索增强生成能力

核心组件：
- embeddings.py: Embedding 模型管理（bge-small-zh-v1.5）
- vector_store.py: ChromaDB 操作封装（Cosine 距离）
- retriever.py: 混合检索（BM25 关键词 + 向量语义 + RRF 融合）
- build_index.py: 知识库索引构建脚本
"""

from app.rag.retriever import HybridRetriever, retrieve

__all__ = ["HybridRetriever", "retrieve"]