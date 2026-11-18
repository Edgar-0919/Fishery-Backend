"""Embedding 模型管理 —— 加载 BAAI/bge-small-zh-v1.5，提供文本向量化接口

关键设计：
- 启动时加载一次，全局单例，避免重复加载模型（~100MB 内存）
- encode_kwargs={'normalize_embeddings': True} 确保向量归一化（Cosine 距离前提条件）
- 支持单文本和批量文本两种向量化方式
"""

from __future__ import annotations

import logging
import os

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# 在线模型名称（本地模型缺失时的回退下载源，可配合 HF_ENDPOINT 镜像）
_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# 本地模型路径（优先使用，避免联网下载）
_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOCAL_MODEL_PATH = os.path.join(_backend_dir, "models", "bge-small-zh-v1.5")

# 全局单例，启动时加载一次
_embedding_model: SentenceTransformer | None = None


def _resolve_model_path() -> str:
    """确定模型加载路径：本地 models/ 目录存在则优先使用，否则回退在线下载"""
    if os.path.isdir(_LOCAL_MODEL_PATH):
        return _LOCAL_MODEL_PATH
    return _MODEL_NAME


def get_embedding_model() -> SentenceTransformer:
    """获取全局单例 Embedding 模型，首次调用时自动加载

    Returns:
        SentenceTransformer: 已加载并配置好的 embedding 模型实例
    """
    global _embedding_model
    if _embedding_model is None:
        model_path = _resolve_model_path()
        logger.info(f"[RAG] 正在加载 Embedding 模型：{model_path} ...")
        _embedding_model = SentenceTransformer(model_path)
        # 归一化是 Cosine 距离的前提，必须开启
        _embedding_model.encode_kwargs = {'normalize_embeddings': True}
        logger.info(f"[RAG] Embedding 模型加载完成，向量维度：{_embedding_model.get_sentence_embedding_dimension()}")
    return _embedding_model


def embed_text(text: str) -> list[float]:
    """将单段文本转换为向量

    Args:
        text: 待向量化的文本

    Returns:
        list[float]: 归一化后的向量（维度 = 512）
    """
    model = get_embedding_model()
    embedding = model.encode(text, normalize_embeddings=True)
    return embedding.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """批量将文本转换为向量（比逐条调用快 2-3 倍）

    Args:
        texts: 待向量化的文本列表

    Returns:
        list[list[float]]: 归一化后的向量列表
    """
    model = get_embedding_model()
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return embeddings.tolist()


def get_embedding_dimension() -> int:
    """获取当前 embedding 模型的向量维度"""
    model = get_embedding_model()
    return model.get_sentence_embedding_dimension()