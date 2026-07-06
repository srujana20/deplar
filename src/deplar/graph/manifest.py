from datetime import datetime
from typing import List

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    file: str
    line: int = 0
    snippet: str = ""


class DependencyItem(BaseModel):
    from_repo: str = Field(alias="from")
    to_repo: str = Field(alias="to")
    types: List[str]
    confidence: float
    evidence: List[EvidenceItem] = []
    metadata: dict = {}

    model_config = {"populate_by_name": True}


class RepoMeta(BaseModel):
    name: str
    path: str = ""
    primary_language: str = ""


class SummaryMeta(BaseModel):
    total_deps: int = 0
    high_confidence: int = 0
    services_called: List[str] = []
    services_calling_me: List[str] = []


class DepsManifest(BaseModel):
    version: str
    scanned_at: datetime
    repo: RepoMeta
    dependencies: List[DependencyItem]
    summary: SummaryMeta

    @classmethod
    def load(cls, path) -> "DepsManifest":
        import json
        from pathlib import Path
        data = json.loads(Path(path).read_text())
        return cls.model_validate(data)