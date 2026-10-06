#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fixer.py — wrap bare LaTeX in math delimiters.

Finds LaTeX that lost its delimiters (common when AI chat output is
interrupted or truncated) and wraps it in $...$ so Markdown renderers
display it as math instead of raw source.

Strategy: mask out everything that must not be touched, then use LaTeX
command names, sub/superscripts, function notation and relation operators
as anchors. From each anchor the candidate fragment is grown left and
right over math characters only, stopping at CJK text, CJK punctuation,
delimiters and line breaks. Overlapping fragments are merged and wrapped.

Usage:
    python3 fixer.py <file-or-dir> [...]        # dry run (no files changed)
    python3 fixer.py --write <path>             # write changes in place
    python3 fixer.py --write --all              # whole tree

Exit code: 0 on success.
"""
import json
import re
import sys
from pathlib import Path

SKIP_PREFIX = ("```", "    ", "\t", "|", "> ")

# Inline code, links, images, HTML tags and bare URLs are not math.
RE_NONMATH = re.compile(
    r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|<[^>]+>|https?://\S+|\bwww\.\S+")

# ---- Characters allowed inside a math fragment ----
MATH_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "\\^_{}()[]|<>"
    "+-*/=.,;:!'\"~"
    " \t"
)

# ============ Rule tables live in rules.json, not in code ============
RULES_PATH = Path(__file__).parent / "rules.json"


def _load_rules():
    try:
        with open(RULES_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except FileNotFoundError:
        raise SystemExit(f"rules file not found: {RULES_PATH}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"invalid rules JSON: {e}")


R = _load_rules()

# ---- Commands ----
_CMDS = R["commands"]["trigger"]
RE_STRONG = re.compile(r"\\(?:" + "|".join(sorted(set(_CMDS), key=len, reverse=True)) + r")\b")
TEXT_CMDS = tuple(R["commands"]["swallow"])

# ---- Structural signals ----
RE_SUBSUP = re.compile(R["structural"]["sub_superscript"]["trigger"])
RE_FUNC_HEAD = re.compile(R["structural"]["function_notation"]["trigger"])
_ASCII_REL = "".join(re.escape(x) for x in R["structural"]["relations"]["ascii"] if x != "=")
RE_REL = re.compile(r"^\s*(?:\\(?:" + R["structural"]["relations"]["continue"] + r")\b|[" + _ASCII_REL + r"])")
RE_REL_TAIL = re.compile(r"^\s*(?:\\(?:" + R["structural"]["relations"]["continue"] + r")\b|" + "|".join(
    re.escape(x) for x in R["structural"]["relations"]["ascii"]) + r")")

# ---- Multi-line environments ----
_ENVS = "|".join(e.replace("*", r"\*") for e in R["blocks"]["environments"])
RE_ENV_BLOCK = re.compile(
    r"\\begin\{(" + _ENVS + r")\}.*?\\end\{\1\}", re.S)

# ---- Boundaries ----
MATH_CHARS = set(R["boundaries"]["math_chars"])
CN_PUNCT = R["boundaries"]["stop_chars"]["cjk_punctuation"]
TRAILING_PUNCT = R["boundaries"]["trim_trailing"]
# Paired CJK brackets: when they enclose a formula, wrap them too
BRACKET_PAIRS = {tuple(p) for p in R["boundaries"]["bracket_allowance"]["pairs"]}
BRACKET_OPEN = {o for o, _ in BRACKET_PAIRS}
BRACKET_CLOSE = {c for _, c in BRACKET_PAIRS}

# ---- Function notation (loose, whole-fragment match) ----
RE_FUNC = re.compile(r"\b([A-Za-z])_?\{?[A-Za-z0-9]*\}?\s*\(\s*[A-Za-z0-9][A-Za-z0-9\s,+\-\\{}^_]*\)")

# ---- Bare comparisons: a < b, x >= 1, r = |x-a| ----
RE_INEQ = re.compile(R["structural"]["bare_comparison"]["trigger"])

# ---- Lone variables: a math symbol standing alone in prose ----
_VARS = "".join(re.escape(v) for v in R["structural"]["lone_variable"]["letters"])
RE_ISOLATED_VAR = re.compile(r"(?<![\\A-Za-z0-9$])[" + _VARS + r"](?![A-Za-z0-9$])")


def math_char(c: str) -> bool:
    return c in MATH_CHARS


def _eat_cmd_arg(s: str, j: int) -> int:
    """s[j] is a backslash. Consume one LaTeX command; for text-like commands
    followed by {..}, consume the whole braced argument even if it contains
    text that would otherwise read as prose. Returns the new position."""
    m = re.match(r"\\([a-zA-Z]+)\*?|\\.", s[j:])
    if not m:
        return j + 1
    end = j + len(m.group(0))
    name = m.group(1) or ""
    if name in TEXT_CMDS:
        k = end
        while k < len(s) and s[k] in " \t":
            k += 1
        if k < len(s) and s[k] == "{":
            depth, t = 0, k
            while t < len(s):
                if s[t] == "\\":
                    t += 2
                    continue
                if s[t] == "{":
                    depth += 1
                elif s[t] == "}":
                    depth -= 1
                    if depth == 0:
                        return t + 1
                t += 1
            return t
    return end


def _eat_right(s: str, j: int, start: int = 0) -> int:
    """Consume math content to the right of j, returning the new position.

    A space is treated as suspicious: it is crossed only when the next token
    is itself math, so that a sentence like "see f(x) for more" is not
    swallowed whole. ``start`` is the anchor, used to tell whether the
    fragment already contains a strong math signal such as an equals sign.
    """
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j = _eat_cmd_arg(s, j)
            continue
        if c in CN_PUNCT or c == "$" or c == "\n":
            break
        if "\u4e00" <= c <= "\u9fff":
            break
        if c == " " or c == "\t":
            k = j
            while k < n and s[k] in " \t":
                k += 1
            in_math = bool(re.search(r"[=+\-*/^_<>]|\\[a-zA-Z]|\b[A-Za-z]_", s[start:j]))
            if k >= n or not _is_math_token_start(s, k, in_math):
                break
            j = k
            continue
        if c in MATH_CHARS:
            j += 1
            continue
        break
    return j


def _is_math_token_start(s: str, k: int, in_math: bool = False) -> bool:
    """Whether position k starts a math token, used for the across-space test.

    With ``in_math=True`` the fragment already carries an explicit math
    signal, so single letters and digits are accepted as well.
    """
    if k >= len(s):
        return False
    if s[k] == "\\":
        return bool(re.match(r"\\(?:[a-zA-Z]+|.)", s[k:]))
    if s[k] in "^_)]([":
        return True
    if s[k] in "=+-*/<>":
        return True
    # A letter followed by ( ^ or _ is a function or variable
    if re.match(r"[A-Za-z](?=[\^_(])", s[k:]):
        return True
    if in_math:
        # Already inside math: single letters and digits count
        if re.match(r"[A-Za-z0-9]", s[k:]):
            return True
    # Bare digits, but only before an operator or end of text
    if re.match(r"[0-9]+(?:\s*[=+\-*/<>^_)\]}]|$)", s[k:]):
        return True
    return False


def _eat_left(s: str, j: int) -> int:
    """Consume math content to the left of j, returning the new position."""
    while j > 0:
        c = s[j - 1]
        if c == "}":
            # Walk left to a matching text-like command (\\text{...}) and swallow it whole,\n            # CJK inside included
            depth, t = 0, j - 1
            while t >= 0:
                if s[t] == "}":
                    depth += 1
                elif s[t] == "{":
                    depth -= 1
                    if depth == 0:
                        break
                t -= 1
            if t > 0:
                m = re.search(r"\\([a-zA-Z]+)\*?\s*$", s[:t])
                if m and m.group(1) in TEXT_CMDS:
                    j = m.start()
                    continue
            # Ordinary brace: keep walking
            j -= 1
            continue
        if c == "\\":
            # Swallow one whole command to the left
            m = re.search(r"\\([a-zA-Z]+|.)$", s[:j])
            if m:
                j -= len(m.group(0))
                continue
            j -= 1
            continue
        if c in CN_PUNCT or c == "$" or c == "\n":
            break
        if "\u4e00" <= c <= "\u9fff":
            break
        if c in MATH_CHARS:
            j -= 1
            continue
        break
    return j


def _fix_balance_left(s: str, start: int, anchor: int) -> int:
    """If an unclosed paren remains left of the anchor, extend left until balanced."""
    for _ in range(50):
        if _balance(s[start:anchor]) >= 0:
            return start
        nxt = _eat_left(s, start)
        if nxt == start:
            return start
        start = nxt
    return start


def _fix_balance_right(s: str, anchor: int, end: int) -> int:
    """If an unclosed paren remains right of the anchor, extend right until balanced."""
    for _ in range(50):
        if _balance(s[anchor:end]) <= 0:
            return end
        nxt = _eat_right(s, end, anchor)
        if nxt == end:
            return end
        end = nxt
    return end


def expand_left(s: str, i: int) -> int:
    """Extend left from anchor i and return the fragment start."""
    j = _fix_balance_left(s, _eat_left(s, i), i)
    while j < i and s[j] in " \t":
        j += 1
    while j < i and s[j] in ",;":
        j += 1
    return j


def expand_right(s: str, i: int) -> int:
    """Extend right from anchor i and return the fragment end, exclusive."""
    j = _fix_balance_right(s, i, _eat_right(s, i, i))
    while j > i and (s[j - 1] in " \t" or s[j - 1] in TRAILING_PUNCT or s[j - 1] in ",;"):
        j -= 1
    return j


def _apply_bracket_allowance(s: str, spans):
    """Bracket allowance: absorb a paired CJK bracket only when it encloses
    the whole fragment.

    A fragment that is itself the entire bracketed group takes the brackets
    with it; a fragment that merely sits next to a bracketed group does not.
    """
    out = []
    for a, b in spans:
        inner = s[a:b]
        # Right: swallow a closing bracket only if its opening pair is inside
        if b < len(s) and s[b] in BRACKET_CLOSE:
            open_ch = next((o for o, c in BRACKET_PAIRS if c == s[b]), None)
            if open_ch and inner.count(open_ch) > inner.count(s[b]):
                b += 1
        # Left: swallow an opening bracket only if its closing pair is inside
        if a > 0 and s[a - 1] in BRACKET_OPEN:
            close_ch = next((c for o, c in BRACKET_PAIRS if o == s[a - 1]), None)
            inner2 = s[a:b]
            if close_ch and inner2.count(close_ch) > inner2.count(s[a - 1]):
                a -= 1
        out.append((a, b))
    return out


def _balance(seg: str) -> int:
    """Net bracket depth; positive means more opening brackets. \\left and \\right are ignored."""
    t = re.sub(r"\\left|\\right|\\big|\\Big|\\bigg|\\Bigg|\\middle", "", seg)
    t = re.sub(r"\\.", "", t)
    depth = 0
    for c in t:
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
    return depth


def find_bare_spans(line: str):
    """Spans on this line that need delimiters, merged where they overlap."""
    if line.lstrip().startswith(SKIP_PREFIX):
        return []
    masked = RE_NONMATH.sub(lambda m: " " * len(m.group(0)), line)
    # Mask out already-delimited $...$ spans
    work = list(masked)
    i, n = 0, len(masked)
    while i < n:
        if masked[i] == "\\":
            i += 2
            continue
        if masked[i] == "$":
            if masked.startswith("$$", i):
                k = masked.find("$$", i + 2)
                end = (k + 2) if k != -1 else n
            else:
                k = i + 1
                while k < n:
                    if masked[k] == "\\":
                        k += 2
                        continue
                    if masked[k] == "$":
                        break
                    k += 1
                end = (k + 1) if k < n else n
            for t in range(i, end):
                work[t] = " "
            i = end
            continue
        i += 1
    bare = "".join(work)

    anchors = []
    for rx in (RE_STRONG, RE_SUBSUP, RE_INEQ, RE_ISOLATED_VAR):
        anchors += [m.start() for m in rx.finditer(bare)]
    # Function head (letter + paren): triggers expansion only, not a full match
    for m in RE_FUNC_HEAD.finditer(bare):
        anchors.append(m.start())
    if not anchors:
        return []

    spans = []
    for a in sorted(set(anchors)):
        if bare[a] == " ":
            continue
        s = expand_left(bare, a)
        e = expand_right(bare, a)
        # Must be non-empty and carry a math signal
        seg = bare[s:e].strip()
        if len(seg) < 1:
            continue
        is_math = (RE_STRONG.search(seg) or re.search(r"[\^_]", seg)
                   or RE_FUNC.fullmatch(seg) or RE_FUNC_HEAD.search(seg)
                   or RE_INEQ.fullmatch(seg) or RE_INEQ.search(seg))
        # A lone variable: the whole fragment is one conventional math letter
        if not is_math and re.fullmatch(r"[A-Za-z]", seg) and RE_ISOLATED_VAR.fullmatch(seg):
            is_math = True
        if not is_math:
            continue
        # Plain English words are not math
        if re.fullmatch(r"[A-Za-z]{2,}", seg) and not RE_STRONG.search(seg):
            continue
        # Prose guard: 2+ real English words (3+ letters) means this is prose, not math.
        # Threshold must be 3+: "ax_1", "bx_1" are single-letter products, not words
        _seg_wo_cmd = re.sub(r"\\[a-zA-Z]+|\{[^{}]*\}", " ", seg)
        if len(re.findall(r"[A-Za-z]{3,}", _seg_wo_cmd)) >= 2:
            continue
        # Emoticons (T_T, Q_Q): rejected only when the letter repeats on both sides
        # f_X and p_i are math subscripts and must pass
        me = re.fullmatch(r"([A-Za-z])_([A-Za-z])", seg)
        if me and me.group(1).lower() == me.group(2).lower() and not RE_STRONG.search(seg):
            continue
        spans.append((s, e))

    if not spans:
        return []
    spans = _apply_bracket_allowance(bare, spans)
    spans.sort()
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        # Do not merge across a CJK bracket: w and (u < w < v) are siblings
        gap = bare[merged[-1][1]:s]
        if any(ch in BRACKET_OPEN or ch in BRACKET_CLOSE for ch in gap):
            merged.append([s, e])
            continue
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged if bare[m[0]:m[1]].strip()]


def fix_line(line: str) -> str:
    spans = find_bare_spans(line)
    if not spans:
        return line
    out = line
    for s, e in reversed(spans):
        seg = out[s:e]
        lead = seg[: len(seg) - len(seg.lstrip())]
        trail = seg[len(seg.rstrip()):]
        core = seg.strip()
        if not core:
            continue
        out = out[:s] + lead + f"${core}$" + trail + out[e:]
    return out


def _protect_segments(text: str):
    """Document-level state machine: mask regions that must not change.

    Returns the text with placeholders plus a list to restore afterwards.
    Regions excluded from inline repair:
      - YAML front matter
      - fenced code blocks
      - multi-line $$...$$ display blocks
      - multi-line \\[...\\] display blocks
      - \\begin{env}...\\end{env} blocks, with leading math on the same line
      - bare URLs, Markdown links, images and wiki links
    """
    holds = []  # (kind, payload)

    def hold(kind, payload):
        holds.append((kind, payload))
        return f"\x00H{len(holds)-1}\x00"

    # --- 1) YAML front matter at the top of the file ---
    m = re.match(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    body_start = 0
    if m:
        head = hold("raw", m.group(0))
        body_start = m.end()
    else:
        head = ""

    rest = text[body_start:]

    # --- 2) Fenced code blocks ---
    rest = re.sub(
        r"^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\2[ \t]*$",
        lambda mm: hold("raw", mm.group(0)),
        rest, flags=re.S | re.M)

    # --- 3) Multi-line $$...$$ display blocks ---
    rest = re.sub(r"\$\$[\s\S]*?\$\$", lambda mm: hold("raw", mm.group(0)), rest)

    # --- 4) Multi-line \[...\] display blocks ---
    rest = re.sub(r"\\\[[\s\S]*?\\\]", lambda mm: hold("raw", mm.group(0)), rest)

    # --- 5) \begin{env}...\end{env}, plus math on the same line before it ---
    pieces, last = [], 0
    for mm in RE_ENV_BLOCK.finditer(rest):
        line_start = rest.rfind("\n", 0, mm.start()) + 1
        seg = rest[line_start:mm.start()]
        cut_i = 0
        for ch_i in range(len(seg) - 1, -1, -1):
            ch = seg[ch_i]
            if ("\u4e00" <= ch <= "\u9fff") or ch in CN_PUNCT or ch == "$":
                cut_i = ch_i + 1
                break
        prefix = seg[cut_i:]
        if not (prefix.strip() and re.search(r"[=+\-*/^_]|\\[a-zA-Z]", prefix)):
            prefix = ""
            cut_i = len(seg)
        start = line_start + cut_i
        pieces.append(rest[last:start])
        lead = prefix[: len(prefix) - len(prefix.lstrip())]
        core = prefix.strip()
        pieces.append(hold("env", (lead, core, mm.group(0))))
        last = mm.end()
    pieces.append(rest[last:])
    rest = "".join(pieces)

    # --- 6) Bare URLs, links, images, wiki links ---
    rest = re.sub(r"https?://\S+|!?\[[^\]]*\]\([^)]*\)|\[\[[^\]]*\]\]",
                  lambda mm: hold("raw", mm.group(0)), rest)

    return head + rest, holds


def _restore(text: str, holds) -> str:
    for i, (kind, payload) in enumerate(holds):
        token = f"\x00H{i}\x00"
        if kind == "raw":
            text = text.replace(token, payload)
        else:
            # env: merge leading math with the environment into one $$...$$ block
            lead, core, body = payload
            inner = f"{core} {body}".strip() if core else body.strip()
            text = text.replace(token, f"{lead}$${inner}$$")
    return text


def _dollars_pair_cleanly(line: str) -> bool:
    """Whether every $ and $$ on the line pairs up cleanly.

    Distinguishes a line that genuinely contains math from prose that
    merely mentions the delimiter character. A pair whose body contains CJK
    text does not count, since prose sits between such delimiters.
    """
    n = len(line)
    i = 0
    while i < n:
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "$":
            if line.startswith("$$", i):
                k = line.find("$$", i + 2)
                if k == -1:
                    return False
                body = line[i + 2:k]
                if re.search(r"[\u4e00-\u9fff]", body):
                    return False
                i = k + 2
                continue
            k = i + 1
            while k < n:
                if line[k] == "\\":
                    k += 2
                    continue
                if line[k] == "$":
                    break
                k += 1
            if k >= n:
                return False
            body = line[i + 1:k]
            if re.search(r"[\u4e00-\u9fff]", body):
                return False
            i = k + 1
            continue
        i += 1
    return True


def _escape_stray_dollars(line: str) -> str:
    """Escape a dollar sign that is prose rather than a delimiter.

    A line that mentions the delimiter character in passing, without a
    well-formed pair around any math, would otherwise be parsed as math and
    corrupt the surrounding text. Such dollars are escaped instead.
    """
    if "$" not in line:
        return line

    # If the line already has a clean $$ block, treat it as normal math and skip
    if _dollars_pair_cleanly(line):
        return line

    # Collect every $ on the line, skipping \$
    positions, i, n = [], 0, len(line)
    while i < n:
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "$":
            positions.append(i)
        i += 1
    if not positions:
        return line

    out = list(line)
    for k in positions:
        if out[k] != "$":
            continue
        def _is_cjk(ch):
            return bool(ch) and "\u4e00" <= ch <= "\u9fff"

        # Skip spaces to find the real neighbours
        li = k - 1
        while li >= 0 and line[li] == " ":
            li -= 1
        ri = k + 1
        while ri < n and line[ri] == " ":
            ri += 1
        lch = line[li] if li >= 0 else ""
        rch = line[ri] if ri < n else ""
        l_sym = lch == "$"
        r_sym = rch == "$"
        l_cjk = _is_cjk(lch) or (lch in CN_PUNCT if lch else False)
        r_cjk = _is_cjk(rch) or (rch in CN_PUNCT if rch else False)

        # Narrative $: one side is CJK text/punctuation, the other is a $ or nothing
        narrate = (l_cjk and (r_cjk or r_sym or rch == "")) or \
                  (r_cjk and (l_cjk or l_sym or lch == ""))
        if not narrate:
            continue
        # Escape an adjacent second $ too, so $$ becomes \$\$
        out[k] = "\\$"
        if k + 1 < n and line[k + 1] == "$":
            out[k + 1] = "\\$"
    return "".join(out)


def fix_text(text: str) -> str:
    # 0) Escape narrative $ first, e.g. prose that literally mentions "$ or $$".
    #    Otherwise the $$ block protector misreads them as a display block
    #    opener and swallows the surrounding prose.
    text = "\n".join(_escape_stray_dollars(ln) for ln in text.split("\n"))

    # 1) Document-level state machine: protect regions that must not change
    protected, holds = _protect_segments(text)

    # 2) Wrap bare math line by line. An odd $ count means a broken delimiter,\n    #    so that line is left untouched
    lines = protected.split("\n")
    for idx, ln in enumerate(lines):
        if not _odd_dollars(ln):
            lines[idx] = fix_line(ln)
    out = "\n".join(lines)

    # 3) Restore protected regions
    return _restore(out, holds)


def _odd_dollars(line: str) -> bool:
    """Count $ on a line, skipping \\$; an odd count means broken delimiters."""
    n, i, L = 0, 0, len(line)
    while i < L:
        if line[i] == "\\":
            i += 2
            continue
        if line[i] == "$":
            n += 1
        i += 1
    return n % 2 == 1


def main(argv):
    write = "--write" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        args = ["."]
    targets = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            targets += sorted(p.glob("*.md"))
        elif p.is_file():
            targets.append(p)

    changed = 0
    broken = 0
    for f in targets:
        old = f.read_text(encoding="utf-8")
        new = fix_text(old)
        # Invariant: the fixer may only insert $ or escape $ as \$.
        # Stripping those out must leave the rest byte-identical
        def _norm(s):
            return re.sub(r"\\?\$", "", s)
        if _norm(old) != _norm(new):
            broken += 1
            print(f"INVARIANT VIOLATION (skipped): {f}")
            continue
        if new != old:
            changed += 1
            if write:
                f.write_text(new, encoding="utf-8")
                print(f"updated {f}")
            else:
                print(f"\n--- {f}")
                for o, n in zip(old.split("\n"), new.split("\n")):
                    if o != n:
                        print(f"  - {o[:100]}")
                        print(f"  + {n[:100]}")
    print(f"\n{'written' if write else 'dry run'}: {changed} file(s) changed"
          + (f", {broken} skipped on invariant violation" if broken else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
