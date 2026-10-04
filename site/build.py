#!/usr/bin/env python3
"""Build site/forkpilot.html from site/forkpilot.src.html.

If the source contains the placeholder /*__DATA__*/null, it is replaced by the
contents of data.min.json (inline chart data). The current page needs no data,
so the placeholder is absent and the build is a plain copy.
"""
import json, pathlib
here = pathlib.Path(__file__).parent
src = (here / "forkpilot.src.html").read_text(encoding="utf-8")
if "/*__DATA__*/null" in src:
    data = json.dumps(json.loads((here / "data.min.json").read_text(encoding="utf-8")), separators=(",", ":"), ensure_ascii=False)
    src = src.replace("/*__DATA__*/null", data)
(here / "forkpilot.html").write_text(src, encoding="utf-8")
print("wrote", here / "forkpilot.html", len(src.encode()), "bytes")
