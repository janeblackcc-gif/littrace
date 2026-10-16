# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""
Embedding 服务 - 使用阿里 DashScope

功能：
1. generate_embedding - 使用 text-embedding-v4 生成向量
2. rerank_similarity - 使用 DashScope Rerank 重排序

模型迁移（2026-09-20）：
  由 `text-embedding-v4` 换为 `qwen3.7-text-embedding-flash`。
  - 新模型支持 256/512/768/1024 维自定义维度，默认 1024 维，
    与 `milvus_service.py` 的 `vector_dim` 及既有集合 schema **一致**，无需改 Milvus schema。
  - 新模型上下文 128K（旧 v4 为 32K），多语言 100→201 种。
  - ⚠️ 语义上「基本持平」但**不是同一个向量空间**：换模型后【已入库的旧向量不可复用】，
    必须对既有知识库重建索引（`vector_collection` 置空 → 走 requires_reindex 流程）。
  - 模型名可用 `EMBEDDING_MODEL` 覆盖，维度用 `EMBEDDING_DIMENSIONS` 覆盖。
"""

import os
from typing import List, Optional, Tuple
import numpy as np
from openai import OpenAI
from llama_index.core.data_structs import Node
from llama_index.core.schema import NodeWithScore
from llama_index.postprocessor.dashscope_rerank import DashScopeRerank

from dotenv import load_dotenv
load_dotenv()

DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"


def generate_embedding(
    text: str | List[str],
    api_key: str = None,
    base_url: str = None,
    model_name: str = None,
    dimensions: int = None,
    encoding_format: str = "float",
    max_batch_size: int = 10
) -> Optional[List[float] | List[List[float]]]:
    """
    生成文本的向量嵌入（使用阿里 qwen3.7-text-embedding-flash）

    Args:
        text: 单个文本或文本列表
        api_key: API密钥（默认从环境变量获取）
        base_url: API基础URL（默认从环境变量获取）
        model_name: 模型名称（默认 EMBEDDING_MODEL，即 qwen3.7-text-embedding-flash）
        dimensions: 向量维度（默认 EMBEDDING_DIMENSIONS 或 1024，**必须与 Milvus 集合维度一致**）
        encoding_format: 编码格式
        max_batch_size: 最大批量大小（阿里云限制为10）

    Returns:
        单个文本时返回向量，文本列表时返回向量列表
    """
    api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    base_url = base_url or os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    # 默认值放在运行时解析（而非形参默认值），便于用环境变量覆盖而不改代码
    model_name = model_name or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    dimensions = dimensions or int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

    if not api_key:
        print("错误: 缺少 DASHSCOPE_API_KEY 环境变量")
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception as e:
        print(f"初始化 OpenAI 客户端失败: {e}")
        return None

    # 单个文本
    if isinstance(text, str):
        try:
            completion = client.embeddings.create(
                model=model_name,
                input=text,
                dimensions=dimensions,
                encoding_format=encoding_format
            )
            return completion.data[0].embedding
        except Exception as e:
            print(f"Embedding 请求失败: {e}")
            return None

    # 文本列表 - 分批处理
    if isinstance(text, list):
        all_embeddings = []

        for i in range(0, len(text), max_batch_size):
            batch = text[i:i + max_batch_size]

            try:
                completion = client.embeddings.create(
                    model=model_name,
                    input=batch,
                    dimensions=dimensions,
                    encoding_format=encoding_format
                )
                batch_embeddings = [item.embedding for item in completion.data]
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                print(f"Embedding 批量请求失败 (batch {i // max_batch_size + 1}): {e}")
                all_embeddings.extend([None] * len(batch))

        return all_embeddings

    return None


def rerank_similarity(
    query: str,
    texts: List[str],
    top_n: int = None
) -> Tuple[np.ndarray, None]:
    """
    使用 DashScope Rerank 对文本进行重排序

    Args:
        query: 查询文本
        texts: 待排序的文本列表
        top_n: 返回前N个结果（默认返回全部）

    Returns:
        (scores, None) - 分数数组和占位符
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")

    if not api_key:
        print("错误: 缺少 DASHSCOPE_API_KEY 环境变量")
        return np.array([]), None

    top_n = top_n or len(texts)

    # 创建节点列表
    nodes = [NodeWithScore(node=Node(text=text), score=1.0) for text in texts]

    # 初始化 DashScopeRerank
    dashscope_rerank = DashScopeRerank(top_n=top_n, api_key=api_key)

    # 执行重排序
    results = dashscope_rerank.postprocess_nodes(nodes, query_str=query)

    # 提取分数
    scores = np.array([res.score for res in results])

    return scores, None
