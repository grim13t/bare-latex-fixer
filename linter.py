#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linter.py - report LaTeX in Markdown that is missing math delimiters.

Usage:
    python3 linter.py <file-or-dir> [...]       # report only
    python3 linter.py --json <path>             # machine readable

Exit code: 0 when clean, 1 when problems are found, so it can act as a gate.
"""
import json
import re
import sys
from pathlib import Path

# ---- Rule tables come from rules.json (shared with the fixer) ----
RULES_PATH = Path(__file__).parent / "rules.json"
try:
    with open(RULES_PATH, encoding="utf-8") as _fp:
        R = json.load(_fp)
except FileNotFoundError:
    raise SystemExit(f"rules file not found: {RULES_PATH}")
except json.JSONDecodeError as e:
    raise SystemExit(f"invalid rules JSON: {e}")

_CMD_LIST = sorted(set(R["commands"]["trigger"]), key=len, reverse=True)
CMDS = r"\\(?:" + "|".join(_CMD_LIST) + r")\b"

# Single-token math signals
RE_CMD = re.compile(CMDS)
RE_SUBSUP = re.compile(R["structural"]["sub_superscript"]["trigger"])
RE_GREEK = re.compile(r"[αβγδεζηθικλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ]")  # Literal Greek letters
_REL_ALT = "|".join(re.escape(x) for x in R["structural"]["relations"]["ascii"])
RE_COMPARE = re.compile(r"[A-Za-z0-9)\]\}]\s*(?:\\(?:" + R["structural"]["relations"]["continue"] + r")\b|" + _REL_ALT + r"|≠|≤|≥)\s*")
RE_FUNC = re.compile(R["structural"]["function_notation"]["trigger"] + r"[^()]*\)")  # f(x), F_Y(x)

# Code blocks and block quotes are skipped
SKIP_PREFIX = ("```", "    ", "\t", "|", "> ")

# Inline code, links, images and HTML tags are not math.
RE_NONMATH = re.compile(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|<[^>]+>|https?://\S+")


def strip_math_spans(line: str):
    """Mask delimited math, links and code, leaving only the bare remainder."""
    line = RE_NONMATH.sub(lambda m: " " * len(m.group(0)), line)
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i] == "\\":           # escape pair, skip both
            out.append("  ")
            i += 2
            continue
        if line[i] == "$":
            # find the closing delimiter
            if line.startswith("$$", i):
                j = line.find("$$", i + 2)
                end = (j + 2) if j != -1 else n
            else:
                j = i + 1
                while j < n:
                    if line[j] == "\\":
                        j += 2
                        continue
                    if line[j] == "$":
                        break
                    j += 1
                end = (j + 1) if j < n else n
            out.append(" " * (end - i))
            i = end
            continue
        out.append(line[i])
        i += 1
    return "".join(out)


def is_formula_candidate(seg: str) -> bool:
    """Whether a bare span contains real math rather than ordinary words."""
    # Strip CJK and whitespace noise before deciding
    if RE_CMD.search(seg):
        return True
    if RE_SUBSUP.search(seg):
        return True
    if RE_COMPARE.search(seg):
        return True
    if "\\(" in seg or "\\[" in seg:
        return True
    return False


def scan_line(line: str, no: int):
    """Return the bare-math anchors found on this line."""
    if line.lstrip().startswith(SKIP_PREFIX):
        return []
    bare = strip_math_spans(line)
    if not bare.strip():
        return []
    findings = []
    # Anchor on math signals, then grow the fragment outward
    for m in RE_CMD.finditer(bare):
        findings.append((bare, m.start()))
    for m in RE_SUBSUP.finditer(bare):
        # A lone "letter+subscript" reference such as f_X is not bare math to wrap
        seg = _around(bare, m.start())
        if re.fullmatch(r"\s*[A-Za-z]\s*[_^]\s*\{?[A-Za-z0-9]+\}?\s*", seg):
            continue
        findings.append((bare, m.start()))
    for m in RE_COMPARE.finditer(bare):
        findings.append((bare, m.start()))
    return findings


def _around(s: str, pos: int, span: int = 40) -> str:
    """A slice around pos, used to tell whether an anchor is just a lone variable."""
    a = max(0, pos - span)
    b = min(len(s), pos + span)
    return s[a:b]


def _has_bad_cjk(body: str) -> str:
    """CJK text inside a formula body that should not be there.

    Text-like commands such as \\text{...} legitimately contain prose, so their
    arguments are stripped before the check.
    """
    t = re.sub(r"\\(?:text|textrm|textsf|texttt|mathrm|mathbf|mathit|operatorname)\s*\{[^{}]*\}", "", body)
    m = re.search(r"[\u4e00-\u9fff]", t)
    return t[max(0, m.start() - 15):m.start() + 25] if m else ""


def check_structure(text: str):
    """Structural checks: delimiter pairing, block closure, prose inside math."""
    issues = []
    in_fence = False
    lines = text.split("\n")
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # Mask out escapes and non-math regions
        masked = RE_NONMATH.sub(lambda m: " " * len(m.group(0)), line)
        # Walk character by character, checking $ and $$ pairing
        j, n = 0, len(masked)
        while j < n:
            if masked[j] == "\\":
                j += 2
                continue
            if masked[j] == "$":
                if masked.startswith("$$", j):
                    k = masked.find("$$", j + 2)
                    if k == -1:
                        issues.append((i, "unclosed $$ block"))
                        break
                    body = masked[j + 2:k]
                    bad = _has_bad_cjk(body)
                    if bad:
                        issues.append((i, f"prose inside $$ block: {bad}"))
                    j = k + 2
                else:
                    k = j + 1
                    while k < n:
                        if masked[k] == "\\":
                            k += 2
                            continue
                        if masked[k] == "$":
                            break
                        if masked[k] == "\n":
                            break
                        k += 1
                    if k >= n or masked[k] != "$":
                        issues.append((i, f"unclosed inline $: {masked[j:j+40]}"))
                        break
                    body = masked[j + 1:k]
                    bad = _has_bad_cjk(body)
                    if bad:
                        issues.append((i, f"prose inside $...$: {bad}"))
                    j = k + 1
                continue
            j += 1
    return issues


def check_file(path: Path):
    problems = []
    text = path.read_text(encoding="utf-8")
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        hits = scan_line(line, i)
        if hits:
            problems.append((i, line))
    return problems, check_structure(text)


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if not args:
        args = ["."]
    targets = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            targets += sorted(p.glob("*.md"))
        elif p.is_file():
            targets.append(p)
        else:
            print(f"skipping (not found): {a}", file=sys.stderr)

    total = 0
    report = {}
    struct_total = 0
    struct_report = {}
    for f in targets:
        probs, struct = check_file(f)
        if probs:
            total += len(probs)
            report[str(f)] = [{"line": n, "text": t} for n, t in probs]
        if struct:
            struct_total += len(struct)
            struct_report[str(f)] = [{"line": n, "issue": m} for n, m in struct]
    if as_json:
        print(json.dumps({"bare": report, "structure": struct_report},
                         ensure_ascii=False, indent=2))
    else:
        if not report:
            print("OK: no bare LaTeX found.")
        for f, probs in report.items():
            print(f"\n--- {f}  ({len(probs)} line(s) with bare LaTeX)")
            for p in probs[:40]:
                print(f"  L{p['line']}: {p['text'][:110]}")
            if len(probs) > 40:
                print(f"  ... and {len(probs)-40} more")
        if not struct_report:
            print("OK: structural checks passed (delimiters paired, blocks closed, no prose inside math).")
        for f, issues in struct_report.items():
            print(f"\n!! {f}  ({len(issues)} structural issue(s))")
            for it in issues[:30]:
                print(f"  L{it['line']}: {it['issue'][:110]}")
        print(f"\ntotal: bare LaTeX in {len(report)} file(s)/{total} line(s); "
              f"structural issues in {len(struct_report)} file(s)/{struct_total}")
    return 1 if (report or struct_report) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
