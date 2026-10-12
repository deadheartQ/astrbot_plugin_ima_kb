"""把 ima 返回的 markdown 洗成适合 QQ 直接发的纯文本。

QQ 不渲染 markdown：表格会糊成一坨、`**` 和 `[1](@index-ref?id=1)` 会原样显示。
这里只做"能读懂"的降级，不做重新排版。
"""

from __future__ import annotations

import re

_REF = re.compile(r"\[\d+\]\(@index-ref\?id=\d+\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)
_MULTI_BLANK = re.compile(r"\n{3,}")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL = re.compile(r"^[\s:|-]+$")


def _convert_tables(text: str) -> str:
    """把 markdown 表格转成「· 列名：值；列名：值」的行。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TABLE_ROW.match(line):
            block: list[str] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                block.append(lines[i])
                i += 1
            rows: list[list[str]] = []
            for raw in block:
                cells = [c.strip() for c in raw.strip().strip("|").split("|")]
                if cells and all(_TABLE_SEP_CELL.match(c) for c in cells if c):
                    continue  # 表头下的分隔行
                rows.append(cells)
            if rows:
                body = rows[1:]
                width = max(len(r) for r in rows)
                if body and width == 2:
                    # 两列表：第一列本身就是属性名，表头（常是「项目 / 内容」）加了反而绕
                    for r in body:
                        if len(r) >= 2 and (r[0] or r[1]):
                            out.append(f"· {r[0]}：{r[1]}".rstrip("："))
                elif body and width >= 3:
                    # 多列表：保持列序，用 ｜ 分隔，比硬套表头好读
                    for r in body:
                        kept = [c for c in r if c]
                        if kept:
                            out.append("· " + "｜".join(kept))
                else:
                    for r in rows:
                        kept = [c for c in r if c]
                        if kept:
                            out.append("· " + "｜".join(kept))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _truncate(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    cut = text[:max_len]
    for mark in ("\n", "。", "！", "？", "；", ". "):
        pos = cut.rfind(mark)
        if pos > max_len * 0.6:
            cut = cut[: pos + len(mark)]
            break
    return cut.rstrip() + "\n…（内容较长，已截断）"


def clean(text: str, *, max_len: int = 0, keep_refs: bool = False) -> str:
    """markdown → 纯文本。

    max_len=0 表示不限长；keep_refs=True 时保留 [1] 这类角标（去掉链接部分）。
    """
    if not text:
        return ""
    s = text.strip()
    s = _REF.sub(lambda m: m.group(0).split("]")[0] + "]" if keep_refs else "", s)
    s = _convert_tables(s)
    s = _HEADING.sub("", s)
    s = _QUOTE.sub("", s)
    s = _BOLD.sub(r"\1", s)
    s = _HR.sub("", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = _MULTI_BLANK.sub("\n\n", s)
    s = "\n".join(ln.rstrip() for ln in s.split("\n")).strip()
    return _truncate(s, max_len)
