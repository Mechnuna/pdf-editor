"""Модель документа: строки текста PDF, правки и их применение.

Редактируемая единица — строка (line из PyMuPDF), которая может состоять
из нескольких span с разными шрифтами. При правке сохраняются точные
позиции неизменённых символов (кернинг, межсловные интервалы), а новые
символы набираются шрифтом соседнего символа.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

import pymupdf

from .fonts import FontResolver

pymupdf.TOOLS.set_small_glyph_heights(True)  # bbox символов без лишних полей

NBSP = "\xa0"


@dataclass
class Span:
    font: str
    size: float
    flags: int
    color: int
    ascender: float
    descender: float

    @property
    def rgb(self) -> tuple[float, float, float]:
        c = self.color
        return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


@dataclass
class Char:
    c: str
    origin: tuple[float, float]
    bbox: tuple[float, float, float, float]
    span: Span


@dataclass
class TextLine:
    id: str
    page: int
    chars: list[Char]
    bbox: pymupdf.Rect

    @property
    def text(self) -> str:
        return "".join(ch.c for ch in self.chars)

    @property
    def main_span(self) -> Span:
        counts: dict[int, int] = {}
        for ch in self.chars:
            counts[id(ch.span)] = counts.get(id(ch.span), 0) + 1
        best = max(counts, key=counts.get)
        return next(ch.span for ch in self.chars if id(ch.span) == best)


@dataclass
class LineEdit:
    text: str | None = None     # None — текст не менялся
    dx: float = 0.0
    dy: float = 0.0

    @property
    def is_noop(self) -> bool:
        return self.text is None and self.dx == 0 and self.dy == 0


@dataclass
class _Run:
    """Кусок для записи: один шрифт, одна стартовая точка."""
    text: str
    origin: tuple[float, float]
    span: Span


def _tokens(text: str) -> tuple[list[str], list[int]]:
    """Слова и пробелы + индексы их начала (последний элемент — длина текста)."""
    toks, pos = [], []
    for m in re.finditer(r"\s+|[^\s]+", text):
        toks.append(m.group())
        pos.append(m.start())
    pos.append(len(text))
    return toks, pos


class PdfDocument:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.src = pymupdf.open(stream=self.data, filetype="pdf")
        if self.src.needs_pass:
            raise ValueError("Документ защищён паролем")
        self.fonts = FontResolver(self.src)
        self._lines: dict[int, list[TextLine]] = {}
        self.edits: dict[str, LineEdit] = {}
        self.font_report: dict[str, str] = {}  # line_id → какой шрифт использован

    @property
    def page_count(self) -> int:
        return len(self.src)

    def page_rect(self, page_no: int) -> pymupdf.Rect:
        return self.src[page_no].rect

    def is_editable_page(self, page_no: int) -> bool:
        return self.src[page_no].rotation == 0

    # -- извлечение ---------------------------------------------------------

    def lines(self, page_no: int) -> list[TextLine]:
        if page_no not in self._lines:
            self._lines[page_no] = self._extract(page_no)
        return self._lines[page_no]

    def line(self, line_id: str) -> TextLine:
        page_no = int(line_id.split(":")[0])
        return next(l for l in self.lines(page_no) if l.id == line_id)

    def _extract(self, page_no: int) -> list[TextLine]:
        page = self.src[page_no]
        if page.rotation != 0:
            return []
        raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES)
        result = []
        for b_i, block in enumerate(raw["blocks"]):
            for l_i, line in enumerate(block.get("lines", [])):
                dx, dy = line["dir"]
                if abs(dy) > 1e-3 or dx < 0:
                    continue  # повёрнутый текст не редактируем
                chars = []
                for s in line["spans"]:
                    span = Span(s["font"], s["size"], s["flags"], s["color"],
                                s.get("ascender", 0.9), s.get("descender", -0.2))
                    for ch in s["chars"]:
                        c = " " if ch["c"] == NBSP else ch["c"]
                        chars.append(Char(c, tuple(ch["origin"]), tuple(ch["bbox"]), span))
                if not "".join(c.c for c in chars).strip():
                    continue
                result.append(TextLine(f"{page_no}:{b_i}:{l_i}", page_no, chars, pymupdf.Rect(line["bbox"])))
        return result

    # -- правки -------------------------------------------------------------

    def edit_for(self, line_id: str) -> LineEdit:
        return self.edits.get(line_id, LineEdit())

    def set_edit(self, line_id: str, edit: LineEdit):
        line = self.line(line_id)
        if edit.text is not None and edit.text == line.text:
            edit = LineEdit(None, edit.dx, edit.dy)
        if edit.is_noop:
            self.edits.pop(line_id, None)
            self.font_report.pop(line_id, None)
        else:
            self.edits[line_id] = edit

    def current_text(self, line_id: str) -> str:
        e = self.edit_for(line_id)
        return e.text if e.text is not None else self.line(line_id).text

    def has_changes(self) -> bool:
        return bool(self.edits)

    def page_edits(self, page_no: int) -> dict[str, LineEdit]:
        return {k: v for k, v in self.edits.items() if int(k.split(":")[0]) == page_no}

    # -- применение ---------------------------------------------------------

    def _runs_for(self, line: TextLine, edit: LineEdit) -> list[_Run]:
        """Разбивает строку на куски с точными позициями.

        Старый и новый текст сравниваются по словам. Неизменённые слова
        остаются на своих местах (со сдвигом на накопленную разницу ширины)
        и сохраняют исходный кернинг. Заменённое слово набирается стилем
        слова, которое оно заменило, вставленное — стилем соседа.
        """
        old = line.chars
        new = edit.text if edit.text is not None else line.text
        runs: list[_Run] = []
        shift = 0.0

        def x_at(i: int) -> float:
            """x начала i-го старого символа (или конец строки)."""
            return old[i].origin[0] if i < len(old) else old[-1].bbox[2]

        def put_new(text: str, i1: int, i2: int, span: Span):
            nonlocal shift
            if text:
                runs.append(_Run(text, (x_at(i1) + shift, old[min(i1, len(old) - 1)].origin[1]), span))
            width = self._measure(line.page, span, text) if text else 0.0
            shift += width - (x_at(i2) - x_at(i1))

        old_tok, old_pos = _tokens(line.text)
        new_tok, new_pos = _tokens(new)
        sm = difflib.SequenceMatcher(None, old_tok, new_tok, autojunk=False)
        for op, a1, a2, b1, b2 in sm.get_opcodes():
            i1, i2 = old_pos[a1], old_pos[a2]
            if op == "equal":
                for ch in old[i1:i2]:
                    runs.append(_Run(ch.c, (ch.origin[0] + shift, ch.origin[1]), ch.span))
            elif op == "delete":
                put_new("", i1, i2, old[i1].span)
            elif op == "insert":
                span = old[i1 - 1].span if i1 > 0 else old[0].span
                put_new(new[new_pos[b1]:new_pos[b2]], i1, i1, span)
            elif a2 - a1 == b2 - b1:  # слово в слово — каждое со своим стилем
                for k in range(a2 - a1):
                    put_new(new_tok[b1 + k], old_pos[a1 + k], old_pos[a1 + k + 1], old[old_pos[a1 + k]].span)
            else:
                put_new(new[new_pos[b1]:new_pos[b2]], i1, i2, old[i1].span)

        return [_Run(r.text, (r.origin[0] + edit.dx, r.origin[1] + edit.dy), r.span) for r in runs]

    def current_bbox(self, line_id: str) -> pymupdf.Rect:
        """Примерная рамка строки с учётом правок (для выделения в UI)."""
        line = self.line(line_id)
        edit = self.edit_for(line_id)
        if edit.is_noop:
            return pymupdf.Rect(line.bbox)
        r = pymupdf.Rect(line.bbox) + (edit.dx, edit.dy, edit.dx, edit.dy)
        runs = self._runs_for(line, edit)
        if runs:
            r.x0 = min(run.origin[0] for run in runs)
            r.x1 = max(run.origin[0] + self._measure(line.page, run.span, run.text) for run in runs)
        else:
            r.x1 = r.x0 + 4
        return r

    def _measure(self, page_no: int, span: Span, text: str) -> float:
        font = self.fonts.resolve(page_no, span.font, span.flags, text).font
        return font.text_length(text, fontsize=span.size)

    def apply_to_page(self, page: pymupdf.Page, page_no: int):
        edits = self.page_edits(page_no)
        if not edits:
            return
        # 1. стираем только текст исходных строк (графика и картинки остаются)
        for line_id in edits:
            for ch in self.line(line_id).chars:
                x0, y0, x1, y1 = ch.bbox
                h = y1 - y0
                r = pymupdf.Rect(x0 + 0.1, y0 + h * 0.3, x1 - 0.1, y1 - h * 0.3)
                if r.is_valid and not r.is_empty:
                    page.add_redact_annot(r, fill=False, cross_out=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        # 2. пишем новый текст
        for line_id, edit in edits.items():
            line = self.line(line_id)
            used = set()
            writers: dict[tuple, pymupdf.TextWriter] = {}  # по одному на цвет
            for run in self._runs_for(line, edit):
                resolved = self.fonts.resolve(page_no, run.span.font, run.span.flags, run.text)
                used.add(resolved.label)
                tw = writers.setdefault(run.span.rgb, pymupdf.TextWriter(page.rect))
                tw.append(run.origin, run.text, font=resolved.font, fontsize=run.span.size)
            for rgb, tw in writers.items():
                tw.write_text(page, color=rgb)
            self.font_report[line_id] = ", ".join(sorted(used))

    def render_page(self, page_no: int, zoom: float) -> pymupdf.Pixmap:
        if self.page_edits(page_no):
            work = pymupdf.open(stream=self.data, filetype="pdf")
            page = work[page_no]
            self.apply_to_page(page, page_no)
        else:
            page = self.src[page_no]
        return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)

    def save(self, path: str):
        out = pymupdf.open(stream=self.data, filetype="pdf")
        touched = sorted({int(k.split(":")[0]) for k in self.edits})
        for page_no in touched:
            self.apply_to_page(out[page_no], page_no)
        if touched:
            try:
                out.subset_fonts()  # системные шрифты встраиваются целиком — урезаем
            except Exception:
                pass
        out.save(path, garbage=3, deflate=True)
        out.close()
