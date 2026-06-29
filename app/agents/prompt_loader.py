"""Prompt manifest 加载器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.agents.errors import PromptManifestError
from app.core.config import get_settings


@dataclass(frozen=True)
class PromptTemplate:
    """已解析的 Prompt 模板。"""

    agent_name: str
    prompt_version: str
    path: Path
    content: str


class PromptLoader:
    """加载 app/agents/prompts/manifest.yaml 中声明的 Prompt 版本。"""

    def __init__(self, manifest_path: str | Path | None = None) -> None:
        if manifest_path is None:
            manifest_path = get_settings().prompt_manifest_path
        self._manifest_path = Path(manifest_path)

    @property
    def manifest_path(self) -> Path:
        """manifest 文件路径。"""
        return self._manifest_path

    def load(self, agent_name: str) -> PromptTemplate:
        """加载指定 Agent 当前启用的 Prompt 模板。"""
        manifest = self._read_manifest()
        prompt_version = manifest.get(agent_name)
        if not prompt_version:
            raise PromptManifestError(f"manifest 未配置 agent={agent_name}")

        base_dir = self._manifest_path.parent.resolve()
        prompt_path = (base_dir / prompt_version).resolve()
        if not prompt_path.is_relative_to(base_dir):
            raise PromptManifestError(f"prompt path 越界 agent={agent_name}")
        if not prompt_path.exists():
            raise PromptManifestError(f"prompt 文件不存在 agent={agent_name} version={prompt_version}")

        return PromptTemplate(
            agent_name=agent_name,
            prompt_version=prompt_version,
            path=prompt_path,
            content=prompt_path.read_text(encoding="utf-8"),
        )

    def _read_manifest(self) -> dict[str, str]:
        if not self._manifest_path.exists():
            raise PromptManifestError(f"manifest 文件不存在 path={self._manifest_path}")

        entries: dict[str, str] = {}
        for line_no, raw_line in enumerate(self._manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise PromptManifestError(f"manifest 第 {line_no} 行格式错误")
            key, value = line.split(":", 1)
            agent_name = key.strip()
            prompt_file = value.strip().strip("\"'")
            if not agent_name or not prompt_file:
                raise PromptManifestError(f"manifest 第 {line_no} 行缺少 agent 或 prompt 文件")
            entries[agent_name] = prompt_file
        return entries
