# Copyright © 2026 深圳市深维智见教育科技有限公司 版权所有
# 未经授权，禁止转售或仿制。

"""Milvus 向量存储服务"""
import json
import time
import os
from typing import List, Dict, Any, Optional
from pymilvus import (
    connections,
    Collection,
    CollectionSchema,
    FieldSchema,
    DataType,
    utility,
)


class MilvusService:
    """Milvus 向量存储服务"""

    def __init__(self):
        self.host = os.getenv("MILVUS_HOST", "localhost")
        self.port = int(os.getenv("MILVUS_PORT", "19530"))
        # 必须与 embedding 模型输出维度一致（qwen3.7-text-embedding-flash 默认 1024 维，
        # 亦支持 256/512/768）。改动此值等同于改动集合 schema：既有集合并存不同维度会查询失败，
        # 必须重建索引，故用环境变量显式暴露而不是埋在代码里。
        self.vector_dim = int(os.getenv("MILVUS_VECTOR_DIM", "1024"))
        self._connect()

    def _connect(self):
        """连接到 Milvus"""
        try:
            connections.connect(
                alias="default",
                host=self.host,
                port=self.port,
            )
            print(f"已连接到 Milvus: {self.host}:{self.port}")
        except Exception as e:
            print(f"连接 Milvus 失败: {e}")
            raise

    def create_collection(self, collection_name: str) -> Collection:
        """
        创建集合（如果不存在）

        Args:
            collection_name: 集合名称

        Returns:
            Collection 对象
        """
        # 检查集合是否存在
        if utility.has_collection(collection_name):
            print(f"集合 {collection_name} 已存在")
            collection = Collection(collection_name)
            collection.load()
            return collection

        # 定义字段
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="filename", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.vector_dim),
        ]

        schema = CollectionSchema(fields=fields, description=f"Knowledge base: {collection_name}")
        collection = Collection(name=collection_name, schema=schema)

        # 创建索引
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        collection.create_index(field_name="vector", index_params=index_params)

        # 加载集合到内存
        collection.load()

        print(f"集合 {collection_name} 创建成功")
        return collection

    def insert_documents(
        self,
        collection_name: str,
        documents: List[Dict[str, Any]],
    ) -> int:
        """
        插入文档

        Args:
            collection_name: 集合名称
            documents: 文档列表，每个文档包含:
                - id: 文档ID
                - doc_id: 原始文档ID
                - kb_id: 知识库ID
                - filename: 文件名
                - content: 文本内容
                - chunk_index: 切片索引
                - vector: 向量

        Returns:
            插入的文档数量
        """
        collection = self.create_collection(collection_name)

        # 准备数据
        ids = [doc["id"] for doc in documents]
        doc_ids = [doc["doc_id"] for doc in documents]
        kb_ids = [doc["kb_id"] for doc in documents]
        filenames = [doc["filename"] for doc in documents]
        contents = [doc["content"][:65535] for doc in documents]  # 截断过长内容
        chunk_indices = [doc["chunk_index"] for doc in documents]
        vectors = [doc["vector"] for doc in documents]

        # 插入数据
        data = [ids, doc_ids, kb_ids, filenames, contents, chunk_indices, vectors]
        collection.insert(data)
        collection.flush()

        print(f"成功插入 {len(documents)} 条文档到 {collection_name}")
        return len(documents)

    def search(
        self,
        collection_name: str,
        query_vector: List[float],
        top_k: int = 5,
        kb_id: Optional[str] = None,
        doc_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量搜索

        Args:
            collection_name: 集合名称
            query_vector: 查询向量
            top_k: 返回结果数量
            kb_id: 知识库ID（可选，用于过滤）

        Returns:
            搜索结果列表
        """
        if not utility.has_collection(collection_name):
            print(f"集合 {collection_name} 不存在")
            return []

        collection = Collection(collection_name)
        collection.load()

        # 构建过滤表达式
        clauses = ["kb_id == " + json.dumps(kb_id)] if kb_id else []
        if doc_ids is not None:
            if not doc_ids:
                return []
            clauses.append("doc_id in " + json.dumps(doc_ids))
        expr = " and ".join(clauses) or None

        # 搜索参数
        search_params = {
            "metric_type": "COSINE",
            "params": {"nprobe": 10},
        }

        results = collection.search(
            data=[query_vector],
            anns_field="vector",
            param=search_params,
            limit=top_k,
            expr=expr,
            output_fields=["id", "doc_id", "kb_id", "filename", "content", "chunk_index"],
            consistency_level="Strong",
            timeout=15,
        )

        # 格式化结果
        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append({
                    "id": hit.entity.get("id"),
                    "doc_id": hit.entity.get("doc_id"),
                    "kb_id": hit.entity.get("kb_id"),
                    "filename": hit.entity.get("filename"),
                    "content": hit.entity.get("content"),
                    "chunk_index": hit.entity.get("chunk_index"),
                    "score": hit.score,
                })

        return formatted_results

    def delete_by_doc_id(self, collection_name: str, doc_id: str) -> bool:
        # Query primary keys first: avoid relying on non-primary-key delete expression support.
        try:
            if not utility.has_collection(collection_name):
                return True
            collection = Collection(collection_name)
            collection.load()
            expr = "doc_id == " + json.dumps(str(doc_id))
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                rows = collection.query(expr=expr, output_fields=["id"], limit=256,
                                        consistency_level="Strong", timeout=10)
                if not rows:
                    return True
                ids = [r["id"] for r in rows]
                collection.delete("id in " + json.dumps(ids), timeout=10)
                # Strong re-read verifies visibility; delete_count/num_entities are not the oracle.
            return False
        except Exception:
            return False

    def delete_collection(self, collection_name: str) -> bool:
        try:
            if utility.has_collection(collection_name):
                utility.drop_collection(collection_name, timeout=15)
            return not utility.has_collection(collection_name)
        except Exception:
            return False

    def get_collection_stats(self, collection_name: str) -> Dict[str, Any]:
        """
        获取集合统计信息

        Args:
            collection_name: 集合名称

        Returns:
            统计信息
        """
        if not utility.has_collection(collection_name):
            return {"exists": False}

        collection = Collection(collection_name)
        return {
            "exists": True,
            "name": collection_name,
            "num_entities": collection.num_entities,
        }

    def get_chunks_by_filename(
        self,
        collection_name: str,
        filename: str,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        根据文件名获取所有切片

        Args:
            collection_name: 集合名称
            filename: 文件名
            limit: 最大返回数量

        Returns:
            切片列表
        """
        if not utility.has_collection(collection_name):
            print(f"集合 {collection_name} 不存在")
            return []

        try:
            collection = Collection(collection_name)
            collection.load()

            # 查询表达式
            expr = f'filename == "{filename}"'

            results = collection.query(
                expr=expr,
                output_fields=["id", "doc_id", "kb_id", "filename", "content", "chunk_index"],
                limit=limit,
            )

            # 按 chunk_index 排序
            results.sort(key=lambda x: x.get("chunk_index", 0))

            return results
        except Exception as e:
            print(f"查询切片失败: {e}")
            return []

    def get_chunks_by_doc_id(self, collection_name: str, doc_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        if not utility.has_collection(collection_name):
            return []
        collection = Collection(collection_name)
        collection.load()
        rows = collection.query(expr="doc_id == " + json.dumps(str(doc_id)),
            output_fields=["id", "doc_id", "kb_id", "filename", "content", "chunk_index"],
            limit=limit, consistency_level="Strong", timeout=15)
        return sorted(rows, key=lambda row: row.get("chunk_index", 0))


# 单例实例
_milvus_service: Optional[MilvusService] = None


def get_milvus_service() -> MilvusService:
    """获取 Milvus 服务单例"""
    global _milvus_service
    if _milvus_service is None:
        _milvus_service = MilvusService()
    return _milvus_service
