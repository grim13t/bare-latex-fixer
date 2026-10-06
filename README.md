# bare-latex-fixer

Repair LaTeX math in Markdown that lost its delimiters.

---

## The problem

Markdown renderers only typeset math when it is wrapped in delimiters
(`$...$` for inline, `$$...$$` for display). Text that arrives without
those delimiters is displayed verbatim — the reader sees backslashes,
braces and command names instead of a typeset formula.

This happens constantly when math is copied out of chat interfaces and
other tools that render LaTeX internally: what reaches the clipboard is
the rendered glyph in a browser, but the underlying source may be copied
unrendered, partially rendered, or with the delimiters stripped. It also
happens when generated output is truncated or interrupted partway
through — the delimiter that would have opened a block never arrives, so
everything after it stays bare.

Fixing this by hand is slow and error-prone, because the boundaries are
not obvious. A naive search for backslash commands tells you where a
fragment probably *starts*, but not where it *ends*: the trailing brace,
the following comma, the closing word of a sentence and the next line of
prose all look like plausible stopping points. Getting a boundary wrong
is worse than leaving the text alone, since a misplaced delimiter can
swallow surrounding prose or break a formula that was previously fine.

## What this does

Two command-line tools over a shared rule table.

**`fixer.py`** finds LaTeX that is missing delimiters and inserts them.

It does not simply look for "things that look like LaTeX". It works in
two stages:

1. **Protect what must never change.** YAML front matter, fenced code
   blocks, inline code, links, images, bare URLs and multi-line math
   environments are masked before any analysis. Multi-line environments
   are matched as whole units so that a `\begin{...}` block cannot be
   split across lines and incorrectly rewritten.

2. **Grow each fragment.** A set of anchors marks where math is
   plausible — command names, sub/superscripts, function notation,
   relation operators, bare comparisons. From each anchor the candidate
   fragment is extended outward over math characters only, stopping at
   prose, CJK text, CJK punctuation, delimiters and line breaks.
   Overlapping candidates are merged, and each merged span is wrapped.

Because the tools target documents where prose and math are interleaved,
most of the care goes into boundaries: not eating the word that follows a
formula, not treating a passing mention of a delimiter character as a
delimiter, not mistaking an emoticon for a subscript, and knowing that a
braced argument may legitimately contain text that would otherwise read
as prose.

**`linter.py`** reports bare LaTeX without changing anything. It also
performs structural checks — delimiter pairing, block closure, and
whether prose has leaked inside a formula. It exits non-zero when it
finds problems, so it can be used as a gate in a script or CI job.

## Why the rules are data, not code

`rules.json` holds the LaTeX command names, environment names, boundary
characters, comparison operators and bracket pairs. Both tools read it,
so the fixer and the checker can never disagree about what counts as
math — a class of bug that is otherwise very easy to introduce.

Adding support for a new LaTeX package, a new environment, or a new
convention means editing that file. The Python is only the traversal
logic.

## Safety

The fixer refuses to write a file unless it can prove it only inserted
delimiters and escaped delimiter characters, leaving every other
character byte-identical. If a rewrite would change anything else, that
file is skipped and reported. A run over an already-fixed tree makes no
changes at all, so the tool is safe to re-run and safe to point at a
large directory.

Nothing is written unless `--write` is passed; the default is a dry run
that prints the proposed changes.

## Usage

```
python3 fixer.py <file-or-dir> [...]          # dry run
python3 fixer.py --write <file-or-dir> [...]  # write changes in place
python3 linter.py <file-or-dir> [...]         # report only
python3 linter.py --json <path>               # machine-readable output
```

Requires Python 3.8 or newer and no third-party packages.

## Author

Troy

## License

MIT
