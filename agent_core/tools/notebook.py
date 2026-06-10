from __future__ import annotations

from ..contracts.context import ToolContext

import json
import uuid
from pathlib import Path
from typing import Any

from .definitions import ToolDefinition
from .paths import _authorize_path, _display_path
from .permissions import _ensure_tool_permission
from .schemas import _tool_description, _tool_schema
from .validation import _require_string


def _notebook_edit_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        notebook_path = _authorize_path(_require_string(input.get("notebook_path"), "notebook_path"), context)
        edit_mode = input.get("edit_mode") if isinstance(input.get("edit_mode"), str) else "replace"
        await _ensure_tool_permission(context, "NotebookEdit", "edit", f"{edit_mode} notebook cell in {_display_path(context, notebook_path)}")
        notebook = json.loads(Path(notebook_path).read_text(encoding="utf-8"))
        cells = notebook.get("cells") if isinstance(notebook.get("cells"), list) else []
        cell_id = input.get("cell_id") if isinstance(input.get("cell_id"), str) else None
        new_source = _require_string(input.get("new_source"), "new_source")
        cell_type = input.get("cell_type") if isinstance(input.get("cell_type"), str) else "code"
        index = next((idx for idx, cell in enumerate(cells) if isinstance(cell, dict) and cell.get("id") == cell_id), -1) if cell_id else (0 if cells else -1)
        if edit_mode == "delete":
            if index < 0:
                return {"content": "Cell not found", "isError": True}
            cells.pop(index)
        elif edit_mode == "insert":
            cell = {"id": str(uuid.uuid4()), "cell_type": cell_type, "source": [new_source], "metadata": {}, "outputs": []}
            if index < 0:
                cells.insert(0, cell)
            else:
                cells.insert(index + 1, cell)
        else:
            if index < 0:
                return {"content": "Cell not found", "isError": True}
            cells[index] = {**cells[index], "cell_type": cell_type, "source": [new_source]}
        notebook["cells"] = cells
        Path(notebook_path).write_text(f"{json.dumps(notebook, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
        return {"content": f"Updated notebook {notebook_path}"}

    return ToolDefinition("NotebookEdit", _tool_description("NotebookEdit"), _tool_schema("NotebookEdit"), handler)
