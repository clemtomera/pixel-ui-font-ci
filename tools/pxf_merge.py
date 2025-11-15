#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge sémantique pour .pxf (PixelForge) au format:
  - header jusqu'à "glyphs:\n"
  - blocs glyphes:
        \t<codepoint>:
        \t\tadvance: ...
        \t\t...
        \t\tpixels: x y, x y, ...

3-way merge par glyphe:
 - Si un seul côté a modifié le glyphe -> on prend la version modifiée.
 - Si les deux ont modifié -> choix explicite via merge-choices.json ("ours"/"theirs"), sinon last-writer-wins (theirs).
 - L'en-tête est conservé. 'num_glyphs' est recalculé à la fin.

Usage:
  pxf_merge.py BASE OURS THEIRS OUT [CHOICES_JSON]
"""

import sys, re, json
from pathlib import Path

GLYPHS_ANCHOR_RE = re.compile(r'(?m)^\s*glyphs:\s*\n')
# Un bloc glyphe commence par: ^\t<nombre>:
GLYPH_START_RE   = re.compile(r'(?m)^\t(\d+):\s*$')

def split_header_and_body(text):
    m = GLYPHS_ANCHOR_RE.search(text)
    if not m:
        # Pas de section glyphs, tout est header
        return text, ""
    idx = m.end()
    return text[:idx], text[idx:]

def parse_glyph_blocks(body):
    """
    Retourne: (ordered_keys, dict[int]->block_text)
    body commence après 'glyphs:\n'
    """
    blocks = {}
    order = []
    starts = [m.start() for m in GLYPH_START_RE.finditer(body)]
    starts.append(len(body))
    for i in range(len(starts)-1):
        start = starts[i]
        end   = starts[i+1]
        chunk = body[start:end]
        m = GLYPH_START_RE.match(chunk)
        if not m:
            continue
        key = int(m.group(1))
        blocks[key] = chunk.rstrip("\n") + "\n"
        order.append(key)
    return order, blocks

def normalize_line_endings(s):  # pour minimiser les diffs
    return s.replace("\r\n", "\n").replace("\r", "\n")

def changed(base_blk, side_blk):
    return (base_blk or "") != (side_blk or "")

def load_choices(path):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def rebuild_num_glyphs(header_text, count):
    """
    Remplace/insère la ligne num_glyphs: <n> dans l'en-tête.
    On laisse les autres clés telles quelles.
    """
    lines = header_text.splitlines(True)
    num_re = re.compile(r'(?m)^num_glyphs:\s*\d+\s*\n')
    if any(num_re.match(l) for l in lines):
        new = []
        replaced = False
        for l in lines:
            if num_re.match(l) and not replaced:
                new.append(f"num_glyphs: {count}\n")
                replaced = True
            else:
                new.append(l)
        return "".join(new)
    else:
        # Insérer juste avant 'glyphs:' s'il n'existe pas
        out = []
        for l in lines:
            if l.strip() == "glyphs:":
                out.append(f"num_glyphs: {count}\n")
            out.append(l)
        return "".join(out)

def merge_three(base_text, ours_text, theirs_text, choices):
    base_text   = normalize_line_endings(base_text)
    ours_text   = normalize_line_endings(ours_text)
    theirs_text = normalize_line_endings(theirs_text)

    b_header, b_body = split_header_and_body(base_text)
    o_header, o_body = split_header_and_body(ours_text)
    t_header, t_body = split_header_and_body(theirs_text)

    b_order, b = parse_glyph_blocks(b_body)
    o_order, o = parse_glyph_blocks(o_body)
    t_order, t = parse_glyph_blocks(t_body)

    # Header : on garde celui d'ours par défaut (on a ouvert & édité),
    # mais on revalide num_glyphs après reconstruction.
    header = o_header if o_header else (t_header or b_header)

    all_keys = set(b) | set(o) | set(t)
    merged = {}
    # last-writer-wins par défaut -> "theirs"
    # choices peut contenir: { "33": "ours", "34": "theirs" } ou { 33: "ours"... }
    normalized_choices = {int(k): v for k, v in ((k, choices[k]) for k in choices)}
    for k in sorted(all_keys):
        bb = b.get(k)
        oo = o.get(k)
        tt = t.get(k)

        o_changed = changed(bb, oo)
        t_changed = changed(bb, tt)

        if not o_changed and not t_changed:
            merged[k] = bb or oo or tt
            continue

        if o_changed and t_changed:
            sel = normalized_choices.get(k)
            if sel == "ours" and oo:
                merged[k] = oo
            elif sel == "theirs" and tt:
                merged[k] = tt
            else:
                merged[k] = tt  # défaut: theirs = "celui de la branche cible"
            continue

        merged[k] = oo if o_changed else tt

    # Reconstruction finale
    # 1) header (avec num_glyphs recalculé)
    header_final = rebuild_num_glyphs(header, len(merged))

    # 2) section glyphs (le header inclut déjà "glyphs:\n")
    parts = [header_final]
    for k in sorted(merged.keys()):
        parts.append(merged[k] if merged[k].endswith("\n") else merged[k] + "\n")
    return "".join(parts)

def main():
    if len(sys.argv) < 5:
        print("Usage: pxf_merge.py BASE OURS THEIRS OUT [CHOICES_JSON]", file=sys.stderr)
        sys.exit(2)
    base, ours, theirs, out = sys.argv[1:5]
    choices = load_choices(sys.argv[5]) if len(sys.argv) > 5 else {}

    def readp(p):
        try:
            return Path(p).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    merged = merge_three(readp(base), readp(ours), readp(theirs), choices)
    Path(out).write_text(merged, encoding="utf-8")

if __name__ == "__main__":
    main()
