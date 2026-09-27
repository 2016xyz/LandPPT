"""Submit render jobs to the network-isolated LibreOffice worker over a spool."""

import json
import os
import tempfile
import time
from pathlib import Path


def render_pdf(pptx_bytes):
    spool = os.environ.get("LANDPPT_PPTX_RENDER_SPOOL", "")
    if not spool or not Path(spool).is_dir():
        raise ValueError(
            "PPTX 渲染服务未配置，请按模板包 README 启动隔离渲染容器并设置 LANDPPT_PPTX_RENDER_SPOOL"
        )
    # Random job directories contain no user-chosen paths. The worker writes only here.
    with tempfile.TemporaryDirectory(prefix="pptx-", dir=spool) as folder:
        job = Path(folder)
        (job / "input.pptx").write_bytes(pptx_bytes)
        (job / "ready").touch()
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            status = job / "result.json"
            if status.exists():
                result = json.loads(status.read_text(encoding="utf-8"))
                if result.get("error"):
                    raise ValueError(result["error"])
                pdf = job / "input.pdf"
                if not pdf.exists() or pdf.stat().st_size > 80_000_000:
                    raise ValueError("渲染结果缺失或过大")
                return pdf.read_bytes(), result.get("fonts", [])
            time.sleep(0.25)
        raise ValueError("PPTX 渲染超时，请检查渲染服务后重试")
