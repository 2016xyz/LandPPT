"""One conversion at a time; run with network=none and a shared /spool volume."""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

SPOOL = Path("/spool")
fonts = sorted(
    set(subprocess.check_output(["fc-list", ":", "family"], text=True).splitlines())
)


def convert(job):
    result = {}
    try:
        with tempfile.TemporaryDirectory(prefix="lo-") as profile:
            user = Path(profile) / "user"
            user.mkdir()
            (user / "registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                "</item></oor:items>",
                encoding="utf-8",
            )
            command = [
                "soffice",
                f"-env:UserInstallation={Path(profile).as_uri()}",
                "--headless",
                "--nologo",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}',
                "--outdir",
                str(job),
                str(job / "input.pptx"),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.wait(timeout=100)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise ValueError("PPTX 渲染超过 100 秒")
            if process.returncode or not (job / "input.pdf").exists():
                raise ValueError("LibreOffice 无法渲染此文件")
            result["fonts"] = fonts
    except Exception as exc:
        result = {"error": str(exc)}
    if job.exists():
        staging = job / "result.tmp"
        staging.write_text(json.dumps(result), encoding="utf-8")
        staging.replace(job / "result.json")


while True:
    for job in sorted(SPOOL.glob("pptx-*")):
        ready = job / "ready"
        if job.is_symlink() or not ready.is_file():
            continue
        try:
            ready.rename(job / "running")
        except FileNotFoundError:
            continue
        convert(job)
    time.sleep(0.25)
