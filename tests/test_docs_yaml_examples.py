from __future__ import annotations

import re
import unittest
from pathlib import Path

from tess_core import ProviderRegistry
from tess_core.config import parse_yaml_subset

ROOT = Path(__file__).resolve().parents[1]


class DocsYamlExampleTests(unittest.TestCase):
    """Every YAML example in the README and guides must load in the runtime's
    own parser, which accepts a subset of YAML (no flow-style collections)."""

    def test_yaml_examples_parse_and_registries_validate(self) -> None:
        registries = 0
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
            for index, block in enumerate(re.findall(r"```yaml\n(.*?)```", path.read_text(encoding="utf-8"), flags=re.S)):
                with self.subTest(file=str(path.relative_to(ROOT)), block=index):
                    document = parse_yaml_subset(block)
                    if isinstance(document, dict) and "providerProfiles" in document:
                        ProviderRegistry.from_document(document)
                        registries += 1
        self.assertGreaterEqual(registries, 2)


if __name__ == "__main__":
    unittest.main()
