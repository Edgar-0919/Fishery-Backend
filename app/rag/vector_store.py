"""ChromaDB 向量存储操作封装

关键设计：
- 持久化存储到 Backend/chroma_data/ 目录
- 使用 Cosine 距离（metadata={"hnsw:space": "cosine"}），默认 L2 不适合文本检索
- 支持增量更新：按 source 删除旧文档 → 重新插入
- 支持元数据过滤检索（按 category 过滤）
"""

from __future__ import annotations

import logging
import os
from chromadb import PersistentClient
from chromadb.config import Settings as ChromaSettings

logger = logging.getLogger(__name__)

# Collection 名称
_COLLECTION_NAME = "fishery_knowledge"

# ChromaDB 持久化路径（相对于 Backend 目录）
_CHROMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "chroma_data")

# 全局单例
_client: PersistentClient | None = None
_collection = None


def _get_client() -> PersistentClient:
    """获取全局单例 ChromaDB 客户端"""
    global _client
    if _client is None:
        os.makedirs(_CHROMA_PATH, exist_ok=True)
        _client = PersistentClient(
            path=_CHROMA_PATH,
            settings=ChromaSettings(anonymized_telemetry=False)
        )
        logger.info(f"[RAG] ChromaDB 客户端已初始化，数据路径：{_CHROMA_PATH}")
    return _client


def get_collection():
    """获取或创建 knowledge collection（使用 Cosine 距离）

    Cosine 距离是文本检索的标准选择：
    - 关注向量方向而非大小，更适合语义相似度计算
    - 归一化后的向量集上 Cosine 距离等价于内积
    """
    global _collection
    if _collection is None:
        client = _get_client()
        _collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"}  # 关键：必须使用 Cosine 距离
        )
        logger.info(f"[RAG] Collection '{_COLLECTION_NAME}' 已就绪（Cosine 距离）")
    return _collection


def add_documents(
    chunks: list[str],
    metadatas: list[dict],
    ids: list[str],
    embeddings: list[list[float]] | None = None
) -> None:
    """批量添加文档块到向量存储

    Args:
        chunks: 文档块文本列表
        metadatas: 元数据列表（source, category, title 等）
        ids: 唯一标识符列表
        embeddings: 预计算的向量（可选，不传则由 ChromaDB 内置 embedding 生成）
    """
    collection = get_collection()
    if embeddings:
        collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids,
            embeddings=embeddings
        )
    else:
        collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids
        )
    logger.info(f"[RAG] 已添加 {len(chunks)} 个文档块到向量存储")


def search_by_embedding(
    query_embedding: list[float],
    top_k: int = 5,
    source_filter: str | None = None
) -> list[dict]:
    """通过向量检索最相似的文档块

    Args:
        query_embedding: 查询向量
        top_k: 返回结果数量
        source_filter: 按 source（文件路径）过滤，用于增量更新场景

    Returns:
        list[dict]: 检索结果 [{id, content, metadata, score}, ...]
    """
    collection = get_collection()
    where_filter = None
    if source_filter:
        where_filter = {"source": source_filter}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where=where_filter,
        include=["documents", "metadatas", "distances"]
    )

    # 将 ChromaDB 返回的列表格式转换为更易用的 dict 列表
    formatted = []
    if results["ids"] and results["ids"][0]:
        for i in range(len(results["ids"][0])):
            # Cosine 距离转相似度：distance ∈ [0, 2]，similarity = 1 - distance/2
            distance = results["distances"][0][i] if results["distances"] else 0
            similarity = 1.0 - (distance / 2.0)
            formatted.append({
                "id": results["ids"][0][i],
                "content": results["documents"][0][i] if results["documents"] else "",
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "score": round(similarity, 4)
            })
    return formatted


def delete_by_source(source: str) -> int:
    """按 source 删除文档块（用于增量更新：先删后加）

    Args:
        source: 文件路径标识

    Returns:
        int: 删除的文档块数量
    """
    collection = get_collection()
    try:
        existing = collection.get(where={"source": source})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            logger.info(f"[RAG] 已删除 source='{source}' 的 {len(existing['ids'])} 个文档块")
            return len(existing["ids"])
    except Exception as e:
        logger.warning(f"[RAG] 删除 source='{source}' 失败: {e}")
    return 0


def get_all_documents() -> list[dict]:
    """获取所有已存储的文档块（用于 BM25 关键词检索的索引重建）

    Returns:
        list[dict]: [{id, content, metadata}, ...]
    """
    collection = get_collection()
    results = collection.get(include=["documents", "metadatas"])
    formatted = []
    if results["ids"]:
        for i in range(len(results["ids"])):
            formatted.append({
                "id": results["ids"][i],
                "content": results["documents"][i] if results["documents"] else "",
                "metadata": results["metadatas"][i] if results["metadatas"] else {}
            })
    return formatted


def get_collection_count() -> int:
    """获取当前存储的文档块总数"""
    collection = get_collection()
    return collection.count()


def reset_collection() -> None:
    """重置 collection：删除并重建（用于 --reset 全量重建索引）"""
    global _collection
    client = _get_client()
    try:
        client.delete_collection(name=_COLLECTION_NAME)
        logger.info(f"[RAG] 已删除旧 Collection '{_COLLECTION_NAME}'")
    except Exception:
        pass
    _collection = None
    get_collection()
    logger.info(f"[RAG] Collection '{_COLLECTION_NAME}' 已重置")