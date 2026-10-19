"""知识库 API 的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeHitOut(BaseModel):
    content: str
    distance: float | None = Field(
        None, description="向量距离，**越小越相关**（不同 embedding 模型的绝对值不可跨模型比较）"
    )
    source_file: str = Field(..., description="来源文件名")
    doc_title: str
    doc_type: str = Field(..., description="device_manual / emergency_plan")
    section: str = Field(..., description="章节标题 —— 引用时可以精确到节")
    device_types: list[str] = Field(default_factory=list)


class KnowledgeSearchOut(BaseModel):
    query: str
    k: int
    count: int
    elapsed_ms: float
    collection_size: int = Field(..., description="向量库当前块数")
    hits: list[KnowledgeHitOut] = Field(default_factory=list)
    note: str = Field(
        "",
        description="可选提示（如向量库为空）；没有要提示的就留空",
    )


class IndexedDocumentOut(BaseModel):
    source_file: str
    doc_title: str
    doc_type: str
    sections: list[str] = Field(default_factory=list)
    chunks: int


class KnowledgeStatusOut(BaseModel):
    collection: str
    chunks: int
    embedding_model: str
    chroma_dir: str
    raw_dir: str
    raw_files: int = Field(..., description="data/raw 下的源文件数")
    indexed_documents: int
    documents: list[IndexedDocumentOut] = Field(default_factory=list)
    note: str = ""
