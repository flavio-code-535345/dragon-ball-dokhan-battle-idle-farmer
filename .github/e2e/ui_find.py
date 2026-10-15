"""Find an element in an Android `uiautomator dump`.

ui_find.py ui.xml REGEX   prints "x y", the centre of the first element whose text or
                          description fully matches REGEX (exit 1 if there is none)
ui_find.py ui.xml --list  prints every labelled element with its bounds
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    path, pattern = sys.argv[1], sys.argv[2]
    try:
        nodes = list(ET.parse(path).getroot().iter("node"))
    except (OSError, ET.ParseError):
        return 1
    labelled = []
    for node in nodes:
        label = (node.get("text") or node.get("content-desc") or "").strip()
        if label:
            labelled.append((label, node.get("bounds", "")))
    if pattern == "--list":
        for label, bounds in labelled:
            print(f"{label!r} {bounds}")
        return 0
    regex = re.compile(pattern)
    for label, bounds in labelled:
        numbers = [int(n) for n in re.findall(r"-?\d+", bounds)]
        if regex.fullmatch(label) and len(numbers) == 4:
            print((numbers[0] + numbers[2]) // 2, (numbers[1] + numbers[3]) // 2)
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
