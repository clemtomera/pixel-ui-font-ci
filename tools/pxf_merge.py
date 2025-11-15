#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PXF semantic merge (per-glyph) for PixelForge-style .pxf files.

Input format (as shown by the user sample):
  - A header (YAML-like) up to a line:   `glyphs:\n`
  - Then a sequence of glyph blocks:
        \t<decimal_codepoint>:
        \t\tadvance: ...
        \t\t...
        \t\tpixels: x y, x y, ...

This tool performs a 3-way merge (BASE / OURS / THEIRS) at the glyph level:
  - It preserves the header (preferring OURS by default) and **recomputes `num_glyphs`**.
  - For each glyph key (integer codepoint):
      * If only one side changed relative to BASE -> take that side (can be a deletion).
      * If both sides changed -> resolve using `merge-choices.json` if provided
        ("ours" | "theirs" | "base" | "keep" | "drop"), otherwise default is "theirs".
      * Deletions are allowed: if chosen block is None, the glyph is removed.
  - It produces optional machine-readable and human-readable reports listing:
      * added glyphs
      * changed glyphs (one side changed)
      * changed-both-ways (true conflicts)
      * removed glyphs
    The Markdown report includes checkboxes ("buttons") so maintainers can decide to:
      - revert conflict glyphs individually (pick ours/theirs/base),
      - **not delete** deleted glyphs (restore/keep individually).

CLI:
  pxf_merge.py BASE.pxf OURS.pxf THEIRS.pxf OUT.pxf [CHOICES_JSON] [REPORT_JSON] [REPORT_MD]

Exit codes:
  0 on success, 2 on usage error.

Notes:
  - This script is robust to missing inputs (e.g., BASE/THEIRS path not found).
  - It treats absent glyph blocks as None (supporting additions/removals cleanly).
"""

import sys
import re
import json
from pathlib import Path
from typing import Dict, Tuple, List, Optional

# --- Regexes to split header and glyph blocks -------------------------------

GLYPHS_ANCHOR_RE = re.compile(r'(?m)^\s*glyphs:\s*\n')
# A glyph block starts at a line like: "\t123:\n"
GLYPH_START_RE   = re.compile(r'(?m)^\t(\d+):\s*$')

# --- Utilities ---------------------------------------------------------------

def normalize_eol(s: str) -> str:
    """Normalize EOL to LF to minimize diffs."""
    return s.replace("\r\n", "\n").replace("\r", "\n")

def read_text_or_empty(path: str) -> str:
    """Read a text file or return empty string if missing/unreadable."""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def split_header_and_body(text: str) -> Tuple[str, str]:
    """
    Split file into (header, body) at the 'glyphs:' anchor.
    The header returned INCLUDES the 'glyphs:\n' line so reconstruction is simple.
    If no 'glyphs:' is found, we consider everything as header and body = "".
    """
    m = GLYPHS_ANCHOR_RE.search(text)
    if not m:
        return text, ""
    idx = m.end()
    return text[:idx], text[idx:]

def parse_glyph_blocks(body: str) -> Tuple[List[int], Dict[int, str]]:
    """
    Parse glyph blocks from body (text after the 'glyphs:' line).

    Returns:
      order: list of glyph keys as they appear
      blocks: dict int -> block text (including the starting "<TAB><key>:" line)
    """
    blocks: Dict[int, str] = {}
    order: List[int] = []

    # Find all start offsets, append len(body) as sentinel
    starts = [m.start() for m in GLYPH_START_RE.finditer(body)]
    starts.append(len(body))

    for i in range(len(starts) - 1):
        start = starts[i]
        end = starts[i + 1]
        chunk = body[start:end]
        m = GLYPH_START_RE.match(chunk)
        if not m:
            continue
        key = int(m.group(1))
        # keep block text with a single trailing newline
        blk = chunk.rstrip("\n") + "\n"
        blocks[key] = blk
        order.append(key)

    return order, blocks

def rebuild_num_glyphs(header_text: str, count: int) -> str:
    """
    Replace or insert the `num_glyphs: <n>` line in the header,
    preserving everything else as-is. We insert it just before `glyphs:` if absent.
    """
    lines = header_text.splitlines(True)
    num_re = re.compile(r'(?m)^num_glyphs:\s*\d+\s*\n')
    if any(num_re.match(l) for l in lines):
        out = []
        replaced = False
        for l in lines:
            if num_re.match(l) and not replaced:
                out.append(f"num_glyphs: {count}\n")
                replaced = True
            else:
                out.append(l)
        return "".join(out)
    else:
        out = []
        for l in lines:
            if l.strip() == "glyphs:":
                out.append(f"num_glyphs: {count}\n")
            out.append(l)
        return "".join(out)

def changed_block(base_blk: Optional[str], side_blk: Optional[str]) -> bool:
    """True if added/removed/modified compared to base."""
    return (base_blk or "") != (side_blk or "")

# --- Merge core --------------------------------------------------------------

def merge_three(base_text: str,
                ours_text: str,
                theirs_text: str,
                choices: Dict[int, str]) -> Tuple[str, Dict]:
    """
    Perform per-glyph 3-way merge and return:
      - merged_text (string)
      - report (dict with added/removed/changed/conflicts lists & counts)

    `choices` supports per-glyph overrides with values:
      - "ours" : pick OURS block (may be None => deletion)
      - "theirs": pick THEIRS block (may be None => deletion)
      - "base" : pick BASE block (may be None)
      - "keep" : keep any existing version (prefer OURS, then THEIRS, then BASE)
      - "drop" : force deletion (None)

    Default policy if both sides changed and no choice given: "theirs".
    """

    base_text   = normalize_eol(base_text)
    ours_text   = normalize_eol(ours_text)
    theirs_text = normalize_eol(theirs_text)

    b_header, b_body = split_header_and_body(base_text)
    o_header, o_body = split_header_and_body(ours_text)
    t_header, t_body = split_header_and_body(theirs_text)

    _, b = parse_glyph_blocks(b_body)
    _, o = parse_glyph_blocks(o_body)
    _, t = parse_glyph_blocks(t_body)

    # Prefer OURS header if present; fallback to THEIRS, then BASE.
    header = o_header or t_header or b_header

    # Normalize choices keys to int
    choices_int: Dict[int, str] = {}
    for k, v in choices.items():
        try:
            choices_int[int(k)] = str(v).strip().lower()
        except Exception:
            pass

    all_keys = set(b) | set(o) | set(t)

    merged: Dict[int, Optional[str]] = {}

    # For reporting
    added: List[int] = []
    removed: List[int] = []
    changed_single_side: List[int] = []
    changed_both_sides: List[int] = []

    def pick_from_choice(k: int,
                         bb: Optional[str],
                         oo: Optional[str],
                         tt: Optional[str],
                         choice: str) -> Optional[str]:
        if choice == "ours":
            return oo
        if choice == "theirs":
            return tt
        if choice == "base":
            return bb
        if choice == "keep":
            # keep any existing version (prefer OURS, then THEIRS, then BASE)
            return oo if oo is not None else (tt if tt is not None else bb)
        if choice == "drop":
            return None
        return None  # unknown value => treat as drop (explicit is better)

    for k in sorted(all_keys):
        bb = b.get(k)   # base block or None
        oo = o.get(k)   # ours block or None
        tt = t.get(k)   # theirs block or None

        o_changed = changed_block(bb, oo)
        t_changed = changed_block(bb, tt)

        # No change on either side vs base -> keep what's present in base (or fallback)
        if not o_changed and not t_changed:
            merged[k] = bb if bb is not None else (oo if oo is not None else tt)
            continue

        # Both sides changed -> conflict bucket (unless resolved by choice)
        if o_changed and t_changed:
            choice = choices_int.get(k, "")
            if choice:
                merged[k] = pick_from_choice(k, bb, oo, tt, choice)
            else:
                # Default: "theirs" wins for conflict
                merged[k] = tt
                changed_both_sides.append(k)
            continue

        # Exactly one side changed
        if o_changed and not t_changed:
            merged[k] = oo
            changed_single_side.append(k)
        elif t_changed and not o_changed:
            merged[k] = tt
            changed_single_side.append(k)

    # Build present_items (filter out deletions)
    present_items: Dict[int, str] = {k: v for k, v in merged.items() if v is not None}

    # Report: classify added/removed relative to BASE
    for k in sorted(all_keys):
        in_base = k in b
        in_merged = k in present_items
        if in_merged and not in_base:
            added.append(k)
        elif in_base and not in_merged:
            removed.append(k)

    # Recompute num_glyphs in header from present items
    header_final = rebuild_num_glyphs(header, len(present_items))

    # Reconstruct final text
    parts: List[str] = [header_final]
    for k in sorted(present_items.keys()):
        blk = present_items[k]
        if not blk.endswith("\n"):
            blk += "\n"
        parts.append(blk)
    merged_text = "".join(parts)

    # Machine-readable report
    report = {
        "counts": {
            "added": len(added),
            "changed_single_side": len(changed_single_side),
            "changed_both_sides": len(changed_both_sides),
            "removed": len(removed),
            "total_after_merge": len(present_items),
        },
        "added": added,
        "changed_single_side": changed_single_side,
        "changed_both_sides": changed_both_sides,
        "removed": removed,
    }
    return merged_text, report

# --- Reporting helpers -------------------------------------------------------

def load_choices(path: Optional[str]) -> Dict:
    if not path:
        return {}
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def write_text(path: Optional[str], content: str) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")

def write_json(path: Optional[str], data: dict) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def render_markdown_report(report: dict,
                           choices_path: str = "merge-choices.json") -> str:
    """
    Human-friendly Markdown summary for PR comments.

    Includes:
      - Added / Changed / Conflicted / Removed counts
      - Lists of codepoints (decimal and U+hex)
      - Checkboxes ("buttons") the maintainers can tick
      - A JSON template snippet for `merge-choices.json` per glyph
    """
    c = report["counts"]
    def fmt_list(lst: List[int]) -> str:
        if not lst: return "_(none)_"
        return ", ".join([f"{n} (U+{n:04X})" for n in lst])

    # Build per-glyph choices template (conflicts and removed glyphs are great candidates)
    template_choices = {}
    for n in report.get("changed_both_sides", []):
        template_choices[str(n)] = ""  # fill with "ours"/"theirs"/"base"
    for n in report.get("removed", []):
        template_choices[str(n)] = ""  # fill with "keep"/"drop"/"base"/"ours"/"theirs"

    lines: List[str] = []
    lines.append("### ✅ PXF semantic merge summary (per glyph)")
    lines.append("")
    lines.append(f"- **Added**: {c['added']}")
    lines.append(f"- **Changed**: {c['changed_single_side']}")
    lines.append(f"- **Changed both ways (conflicts)**: {c['changed_both_sides']}")
    lines.append(f"- **Removed**: {c['removed']}")
    lines.append("")
    lines.append("**Added glyphs:** " + fmt_list(report.get("added", [])))
    lines.append("**Changed (single side):** " + fmt_list(report.get("changed_single_side", [])))
    lines.append("**Changed both ways:** " + fmt_list(report.get("changed_both_sides", [])))
    lines.append("**Removed glyphs:** " + fmt_list(report.get("removed", [])))
    lines.append("")
    if report.get("changed_both_sides"):
        lines.append("#### Conflicts — pick a side per glyph")
        lines.append("_Tick a checkbox and push a commit updating the JSON below (CI will re-run)._")
        lines.append("")
        for n in report["changed_both_sides"]:
            lines.append(f"- Glyph **{n} (U+{n:04X})**:")
            lines.append(f"  - [ ] Use **ours**")
            lines.append(f"  - [ ] Use **theirs**")
            lines.append(f"  - [ ] Use **base**")
        lines.append("")
    if report.get("removed"):
        lines.append("#### Removed glyphs — keep or drop")
        lines.append("_Choose to keep (restore) or confirm deletion per glyph, then update the JSON._")
        lines.append("")
        for n in report["removed"]:
            lines.append(f"- Glyph **{n} (U+{n:04X})**:")
            lines.append(f"  - [ ] **Keep** (restore)")
            lines.append(f"  - [ ] **Drop** (confirm deletion)")
            lines.append(f"  - [ ] Use **base** / **ours** / **theirs** (advanced)")
        lines.append("")
    lines.append("#### Choices file template")
    lines.append(f"Update `{choices_path}` with your decisions and push to this PR. Valid values:")
    lines.append("- Conflicts: `\"ours\" | \"theirs\" | \"base\"`")
    lines.append("- Deletions: `\"keep\" | \"drop\" | \"base\" | \"ours\" | \"theirs\"`")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(template_choices, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)

# --- CLI ---------------------------------------------------------------------

def main():
    if len(sys.argv) < 5:
        print("Usage: pxf_merge.py BASE OURS THEIRS OUT [CHOICES_JSON] [REPORT_JSON] [REPORT_MD]", file=sys.stderr)
        sys.exit(2)

    base_path   = sys.argv[1]
    ours_path   = sys.argv[2]
    theirs_path = sys.argv[3]
    out_path    = sys.argv[4]
    choices_path = sys.argv[5] if len(sys.argv) > 5 else None
    report_json_path = sys.argv[6] if len(sys.argv) > 6 else None
    report_md_path   = sys.argv[7] if len(sys.argv) > 7 else None

    base_txt   = read_text_or_empty(base_path)
    ours_txt   = read_text_or_empty(ours_path)
    theirs_txt = read_text_or_empty(theirs_path)
    choices    = load_choices(choices_path)

    merged_txt, report = merge_three(base_txt, ours_txt, theirs_txt, choices)

    # Write merged .pxf
    write_text(out_path, merged_txt)

    # Optional machine-readable JSON report
    if report_json_path:
        write_json(report_json_path, report)

    # Optional human-friendly Markdown report (for PR comments)
    if report_md_path:
        md = render_markdown_report(report, choices_path or "merge-choices.json")
        write_text(report_md_path, md)

if __name__ == "__main__":
    main()
