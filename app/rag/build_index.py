"""知识库索引构建脚本

用法：
    python -m app.rag.build_index           # 增量构建（只处理新文件）
    python -m app.rag.build_index --reset   # 全量重建（清空后重新索引）

功能：
1. 遍历 knowledge/ 目录下所有 .md 文件
2. 使用 RecursiveCharacterTextSplitter 切分（chunk_size=500, overlap=50）
3. 保留元数据：source（文件路径）、category（目录名）、title（文档标题）
4. 使用 bge-small-zh-v1.5 批量向量化
5. 存入 ChromaDB（Cosine 距离）
"""

import argparse
import logging
import os
import sys
import time

# 确保 Backend 目录在 Python 路径中（支持从任意目录运行）
_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.rag.embeddings import embed_batch, get_embedding_model
from app.rag.vector_store import (
    add_documents,
    delete_by_source,
    reset_collection,
    get_collection_count
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 知识库根目录
_KNOWLEDGE_DIR = os.path.join(_backend_dir, "knowledge")

# 文本切分配置
_CHUNK_SIZE = 500    # 每个 chunk 最多 500 字符
_CHUNK_OVERLAP = 50  # 相邻 chunk 重叠 50 字符，避免关键信息被切断


def _find_markdown_files() -> list[str]:
    """递归遍历 knowledge/ 目录，返回所有 .md 文件的绝对路径"""
    md_files = []
    if not os.path.isdir(_KNOWLEDGE_DIR):
        logger.warning(f"[RAG] 知识库目录不存在: {_KNOWLEDGE_DIR}，将创建空目录")
        os.makedirs(_KNOWLEDGE_DIR, exist_ok=True)
        return md_files

    for root, dirs, files in os.walk(_KNOWLEDGE_DIR):
        for file in files:
            if file.endswith(".md"):
                md_files.append(os.path.join(root, file))
    return md_files


def _extract_metadata(file_path: str) -> dict:
    """从文件路径提取元数据：category、source、title

    Args:
        file_path: 文件绝对路径，如 knowledge/water_quality/parameters.md

    Returns:
        dict: {"source": "water_quality/parameters.md", "category": "water_quality",
               "title": "parameters"}
    """
    rel_path = os.path.relpath(file_path, _KNOWLEDGE_DIR).replace("\\", "/")
    # 第一级目录作为 category
    parts = rel_path.split("/")
    category = parts[0] if len(parts) > 1 else "general"
    # 文件名（不含扩展名）作为 title
    title = os.path.splitext(os.path.basename(file_path))[0]
    return {
        "source": rel_path,
        "category": category,
        "title": title
    }


def _read_and_split(file_path: str) -> tuple[list[str], list[dict]]:
    """读取 Markdown 文件 → 切分为 chunks → 生成元数据

    Args:
        file_path: Markdown 文件绝对路径

    Returns:
        (chunks, metadatas): 文本块列表和对应元数据列表
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        return [], []

    meta = _extract_metadata(file_path)

    # 使用 LangChain RecursiveCharacterTextSplitter，按 Markdown 标题层级切分
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        separators=["\n## ", "\n### ", "\n#### ", "\n", " ", ""],
        length_function=len,
        is_separator_regex=False,
    )

    chunks = splitter.split_text(content)
    if not chunks:
        return [], []

    # 每个 chunk 一份元数据
    metadatas = [meta.copy() for _ in chunks]
    return chunks, metadatas


def build_index(reset: bool = False) -> dict:
    """构建/重建知识库索引

    Args:
        reset: True 时清空旧数据全量重建；False 时增量更新

    Returns:
        dict: {"total_files": 处理文件数, "total_chunks": 索引块数, "elapsed_seconds": 耗时}
    """
    start_time = time.time()

    if reset:
        logger.info("[RAG] 全量重建模式：清空旧索引 ...")
        reset_collection()

    # 确保 Embedding 模型已加载（避免后续批量处理时首次加载超时）
    get_embedding_model()

    md_files = _find_markdown_files()
    logger.info(f"[RAG] 找到 {len(md_files)} 个 Markdown 文件")

    total_chunks = 0
    processed_files = 0

    for file_path in md_files:
        try:
            chunks, metadatas = _read_and_split(file_path)
            if not chunks:
                continue

            # 增量更新：先删除该文件的旧 chunks，再插入新的
            if not reset:
                rel_path = os.path.relpath(file_path, _KNOWLEDGE_DIR).replace("\\", "/")
                delete_by_source(rel_path)

            # 批量向量化（比逐条调用 embed_text 快 2-3 倍）
            embeddings = embed_batch(chunks)

            # 生成 chunk ID：{source}_{index}
            chunk_ids = [
                f"{metadatas[i]['source']}_{i}"
                for i in range(len(chunks))
            ]

            add_documents(chunks, metadatas, chunk_ids, embeddings)
            total_chunks += len(chunks)
            processed_files += 1
            logger.info(f"[RAG]   ✓ {metadatas[0]['source']} → {len(chunks)} chunks")

        except Exception as e:
            logger.error(f"[RAG]   ✗ 处理文件失败: {file_path}: {e}")

    elapsed = round(time.time() - start_time, 2)
    final_count = get_collection_count()

    logger.info(f"[RAG] 索引构建完成：{processed_files} 个文件 → {total_chunks} 个新 chunks → 总计 {final_count} chunks，耗时 {elapsed}s")

    return {
        "total_files": processed_files,
        "total_chunks": total_chunks,
        "collection_total": final_count,
        "elapsed_seconds": elapsed
    }


if __name__ == "__main__":
    from app.core.logging_config import setup_logging

    setup_logging("INFO")

    parser = argparse.ArgumentParser(description="Fishery RAG 知识库索引构建工具")
    parser.add_argument(
        "--reset", action="store_true",
        help="全量重建索引（清空 ChromaDB 后重新索引所有知识文档）"
    )
    args = parser.parse_args()

    result = build_index(reset=args.reset)
    logger.info(f"索引构建完成：{result}")