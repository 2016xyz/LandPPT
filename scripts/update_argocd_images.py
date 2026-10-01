"""Update the app and renderer together without discarding values-file comments."""

import argparse
import json
import re
from pathlib import Path


def update_images(text, repository, renderer_repository, tag):
    updates = {}
    for section, image in (
        (("image",), repository),
        (("pptxRenderer", "image"), renderer_repository),
    ):
        updates[(*section, "repository")] = image
        updates[(*section, "tag")] = tag
        updates[(*section, "pullPolicy")] = "IfNotPresent"
    seen, stack, lines = set(), [], text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^( *)([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not match:
            continue
        indent, key, value = len(match[1]), match[2], match[3]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (*[item[1] for item in stack], key)
        if path in updates:
            if path in seen:
                raise ValueError(f"Duplicate image setting: {'.'.join(path)}")
            seen.add(path)
            lines[index] = f"{' ' * indent}{key}: {json.dumps(updates[path])}"
        if not value or value.startswith("#"):
            stack.append((indent, key))
    missing = updates.keys() - seen
    if missing:
        raise ValueError(
            f"Missing image settings: {sorted('.'.join(path) for path in missing)}"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("values_file", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--renderer-repository", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    text = args.values_file.read_text(encoding="utf-8")
    updated = update_images(text, args.repository, args.renderer_repository, args.tag)
    args.values_file.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
