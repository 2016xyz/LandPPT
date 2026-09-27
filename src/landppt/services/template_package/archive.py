"""Portable packages: JSON for v1, a bounded manifest + raster archive for v2."""

import base64
import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from .schemas import TemplatePackage

MAX_ARCHIVE_BYTES = 65 * 1024 * 1024


def export_archive(package):
    manifest = package.model_dump(mode="json")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for asset in manifest["assets"]:
            path = f"assets/{asset['id']}"
            archive.writestr(path, base64.b64decode(asset.pop("data")))
            asset["path"] = path
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    return output.getvalue()


def import_archive(data):
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("模板包文件不能超过 65 MB")
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if len(entries) > 121 or len(names) != len(set(names)):
                raise ValueError("模板包包含重复或过多文件")
            if sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError("模板包解压大小超过限制")
            if (
                "manifest.json" not in names
                or archive.getinfo("manifest.json").file_size > 10_000_000
            ):
                raise ValueError("缺少 manifest.json 或文件过大")
            manifest = json.loads(archive.read("manifest.json"))
            expected = {"manifest.json"}
            for asset in manifest.get("assets", []):
                path = f"assets/{asset['id']}"
                if asset.pop("path", None) != path or "data" in asset:
                    raise ValueError("无效的素材路径")
                if path not in names or archive.getinfo(path).file_size > 9_000_000:
                    raise ValueError("素材缺失或过大")
                asset["data"] = base64.b64encode(archive.read(path)).decode("ascii")
                expected.add(path)
            if set(names) != expected:
                raise ValueError("模板包包含未声明文件")
            return TemplatePackage.model_validate(manifest)
    except (BadZipFile, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("模板包 ZIP 无效") from exc
