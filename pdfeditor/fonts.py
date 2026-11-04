"""Подбор шрифта для записи текста обратно в PDF.

Порядок поиска для каждого куска текста:
  1. шрифт, встроенный в сам PDF (если в нём есть все нужные глифы);
  2. системный шрифт с тем же именем (например, тот же Times New Roman);
  3. системный шрифт того же семейства с подходящим начертанием;
  4. общий запасной шрифт по классу (serif / sans / mono).

Встроенные подмножества шрифтов (subset) часто не содержат таблицы cmap —
символы адресуются прямо номерами глифов. Для таких шрифтов cmap
восстанавливается из ToUnicode CMap документа, чтобы шрифт можно было
переиспользовать.
"""

from __future__ import annotations

import io
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pymupdf

try:
    from fontTools.ttLib import TTFont, TTCollection
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
except ImportError:  # fontTools необязателен, но без него хуже подбор
    TTFont = None

# Флаги span из PyMuPDF
FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16

SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def strip_subset(name: str) -> str:
    return SUBSET_PREFIX.sub("", name or "")


def _canon(s: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", s.lower())
    s = s.replace("regular", "")
    for suffix in ("psmt", "mt"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def style_from(name: str, flags: int) -> tuple[bool, bool]:
    low = name.lower()
    bold = bool(flags & FLAG_BOLD) or any(w in low for w in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(flags & FLAG_ITALIC) or "italic" in low or "oblique" in low
    return bold, italic


@dataclass
class ResolvedFont:
    font: pymupdf.Font
    label: str          # человекочитаемое описание для статус-бара
    embedded: bool      # True — использован исходный шрифт из PDF


# ---------------------------------------------------------------------------
# Системные шрифты
# ---------------------------------------------------------------------------

def _font_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        dirs = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
                "/Library/Fonts", home / "Library/Fonts"]
    elif sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", "C:/Windows")
        dirs = [Path(windir) / "Fonts", home / "AppData/Local/Microsoft/Windows/Fonts"]
    else:
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts", home / ".fonts", home / ".local/share/fonts"]
    return [Path(d) for d in dirs if Path(d).is_dir()]


@dataclass
class SystemFace:
    path: str
    index: int
    family: str
    subfamily: str
    keys: set
    bold: bool
    italic: bool


class SystemFonts:
    """Ленивый индекс установленных шрифтов."""

    GENERIC = {
        "serif": ["Times New Roman", "Liberation Serif", "DejaVu Serif", "Georgia"],
        "sans": ["Arial", "Helvetica", "Liberation Sans", "DejaVu Sans"],
        "mono": ["Courier New", "Liberation Mono", "DejaVu Sans Mono", "Menlo"],
        "unicode": ["Arial Unicode MS", "Arial Unicode", "DejaVu Sans", "Noto Sans"],
    }

    def __init__(self):
        self._faces: list[SystemFace] | None = None
        self._loaded: dict[tuple, pymupdf.Font] = {}

    @property
    def faces(self) -> list[SystemFace]:
        if self._faces is None:
            self._faces = self._scan()
        return self._faces

    def _scan(self) -> list[SystemFace]:
        faces = []
        if TTFont is None:
            return faces
        for d in _font_dirs():
            for path in d.rglob("*"):
                ext = path.suffix.lower()
                if ext not in (".ttf", ".otf", ".ttc"):
                    continue
                try:
                    if ext == ".ttc":
                        coll = TTCollection(str(path), lazy=True)
                        fonts = list(enumerate(coll.fonts))
                    else:
                        fonts = [(0, TTFont(str(path), lazy=True))]
                except Exception:
                    continue
                for idx, f in fonts:
                    try:
                        name = f["name"]
                    except Exception:
                        continue
                    get = lambda i: str(name.getDebugName(i) or "")
                    family = get(16) or get(1)
                    sub = get(17) or get(2)
                    full, ps = get(4), get(6)
                    keys = {_canon(x) for x in (ps, full, family + sub) if x}
                    bold, italic = style_from(sub, 0)
                    faces.append(SystemFace(str(path), idx, family, sub, keys, bold, italic))
        return faces

    def load(self, face: SystemFace) -> pymupdf.Font | None:
        key = (face.path, face.index)
        if key not in self._loaded:
            try:
                if face.path.lower().endswith(".ttc"):
                    buf = io.BytesIO()
                    TTFont(face.path, fontNumber=face.index).save(buf)
                    self._loaded[key] = pymupdf.Font(fontbuffer=buf.getvalue())
                else:
                    self._loaded[key] = pymupdf.Font(fontfile=face.path)
            except Exception:
                self._loaded[key] = None
        return self._loaded[key]

    def by_exact_name(self, name: str) -> list[SystemFace]:
        key = _canon(name)
        return [f for f in self.faces if key in f.keys]

    def by_family(self, family: str, bold: bool, italic: bool) -> list[SystemFace]:
        key = _canon(family)
        same = [f for f in self.faces if _canon(f.family) == key]
        # лучшее совпадение по начертанию — первым
        same.sort(key=lambda f: (f.bold != bold) * 2 + (f.italic != italic))
        return same


SYSTEM_FONTS = SystemFonts()


# ---------------------------------------------------------------------------
# Встроенные шрифты
# ---------------------------------------------------------------------------

def _parse_tounicode(cmap: bytes) -> dict[int, str]:
    """Разбор ToUnicode CMap: код → строка юникода."""
    text = cmap.decode("latin-1")
    result: dict[int, str] = {}

    def u(hexstr: str) -> str:
        data = bytes.fromhex(hexstr)
        try:
            return data.decode("utf-16-be")
        except UnicodeDecodeError:
            return ""

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            result[int(src, 16)] = u(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, rest in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]+>)", block):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for i, dst in enumerate(re.findall(r"<([0-9A-Fa-f]*)>", rest)):
                    result[lo + i] = u(dst)
            else:
                base = bytes.fromhex(rest[1:-1])
                start = int.from_bytes(base, "big")
                for i, code in enumerate(range(lo, hi + 1)):
                    val = (start + i).to_bytes(len(base), "big")
                    try:
                        result[code] = val.decode("utf-16-be")
                    except UnicodeDecodeError:
                        pass
    return result


def _drawable_codepoints(buffer: bytes) -> set[int] | None:
    """Символы, у которых в шрифте есть непустой глиф.

    Подмножества шрифтов нередко сохраняют все слоты глифов, но обнуляют
    контуры неиспользованных — has_glyph() для них врёт.
    """
    if TTFont is None:
        return None
    try:
        tt = TTFont(io.BytesIO(buffer))
        cmap = tt.getBestCmap() or {}
        if "glyf" in tt:
            glyf = tt["glyf"]
            return {u for u, g in cmap.items() if g in glyf and glyf[g].numberOfContours != 0}
        if "CFF " in tt:
            top = tt["CFF "].cff.topDictIndex[0]
            cs = top.CharStrings
            return {u for u, g in cmap.items() if g in cs and cs[g].calcBounds(cs) is not None}
    except Exception:
        return None
    return None


def _ref(doc: pymupdf.Document, xref: int, key: str) -> int:
    typ, val = doc.xref_get_key(xref, key)
    m = re.search(r"(\d+) 0 R", val or "")
    return int(m.group(1)) if m else 0


def _rebuild_cmap(doc: pymupdf.Document, font_xref: int, buffer: bytes) -> bytes | None:
    """Добавляет cmap в TrueType-подмножество Type0-шрифта по его ToUnicode."""
    if TTFont is None:
        return None
    tu_xref = _ref(doc, font_xref, "ToUnicode")
    desc_xref = _ref(doc, font_xref, "DescendantFonts")  # значение — массив [N 0 R]
    if not tu_xref or not desc_xref:
        return None
    if doc.xref_get_key(desc_xref, "Subtype")[1] != "/CIDFontType2":
        return None  # CFF CID-шрифты пока не поддерживаем

    to_unicode = _parse_tounicode(doc.xref_stream(tu_xref))
    cid2gid_kind, cid2gid_val = doc.xref_get_key(desc_xref, "CIDToGIDMap")
    cid2gid = None
    if cid2gid_kind == "xref":
        raw = doc.xref_stream(int(cid2gid_val.split()[0]))
        cid2gid = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]

    try:
        tt = TTFont(io.BytesIO(buffer))
        order = tt.getGlyphOrder()
    except Exception:
        return None

    mapping: dict[int, str] = {}
    for cid, uni in to_unicode.items():
        if len(uni) != 1:
            continue  # лигатуры не мапим
        gid = cid2gid[cid] if cid2gid is not None and cid < len(cid2gid) else cid
        if 0 < gid < len(order):
            mapping.setdefault(ord(uni), order[gid])
    if not mapping:
        return None
    # пробел часто размечен как U+00A0 и наоборот
    if 0x20 not in mapping and 0xA0 in mapping:
        mapping[0x20] = mapping[0xA0]
    if 0xA0 not in mapping and 0x20 in mapping:
        mapping[0xA0] = mapping[0x20]

    try:
        if "cmap" not in tt:
            from fontTools.ttLib import newTable
            tt["cmap"] = newTable("cmap")
            tt["cmap"].tableVersion = 0
            tt["cmap"].tables = []
        sub = CmapSubtable.newSubtable(4 if max(mapping) <= 0xFFFF else 12)
        sub.platformID, sub.platEncID, sub.language = 3, (1 if max(mapping) <= 0xFFFF else 10), 0
        sub.cmap = mapping
        tt["cmap"].tables = [sub]
        out = io.BytesIO()
        tt.save(out)
        return out.getvalue()
    except Exception:
        return None


class FontResolver:
    """Подбирает шрифт для записи текста; кеширует загруженные шрифты."""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self._embedded: dict[int, pymupdf.Font | None] = {}
        self._drawable: dict[int, set[int] | None] = {}  # id(font) → доступные символы
        self._page_fonts: dict[int, dict[str, int]] = {}

    def _font_xref(self, page_no: int, name: str) -> int:
        if page_no not in self._page_fonts:
            table = {}
            for xref, ext, typ, basefont, *_ in self.doc[page_no].get_fonts(full=True):
                table.setdefault(strip_subset(basefont), xref)
            self._page_fonts[page_no] = table
        return self._page_fonts[page_no].get(strip_subset(name), 0)

    def _embedded_font(self, xref: int) -> pymupdf.Font | None:
        if xref in self._embedded:
            return self._embedded[xref]
        font = None
        try:
            _, ext, typ, buf = self.doc.extract_font(xref)
            if buf and ext != "n/a":
                font = pymupdf.Font(fontbuffer=buf)
                if typ == "Type0" and not font.has_glyph(ord("a")) and not font.has_glyph(ord("а")):
                    buf = _rebuild_cmap(self.doc, xref, buf)
                    font = pymupdf.Font(fontbuffer=buf) if buf else None
                if font is not None:
                    self._drawable[id(font)] = _drawable_codepoints(buf)
        except Exception:
            font = None
        self._embedded[xref] = font
        return font

    def covers(self, font: pymupdf.Font | None, text: str) -> bool:
        if font is None:
            return False
        drawable = self._drawable.get(id(font))
        if drawable is not None:
            return all(ord(c) in drawable or (c.isspace() and font.has_glyph(ord(c))) for c in text)
        return all(font.has_glyph(ord(c)) for c in text)

    def resolve(self, page_no: int, name: str, flags: int, text: str) -> ResolvedFont:
        clean = strip_subset(name)
        font = self._embedded_font(self._font_xref(page_no, name))
        if self.covers(font, text):
            return ResolvedFont(font, f"{clean} (встроенный)", True)

        bold, italic = style_from(clean, flags)
        candidates: list[tuple[SystemFace, str]] = []
        candidates += [(f, "системный, то же имя") for f in SYSTEM_FONTS.by_exact_name(clean)]
        family = re.split(r"[-,]", clean)[0]
        family = re.sub(r"(PS|MT|PSMT)$", "", family)
        candidates += [(f, "системный, то же семейство") for f in SYSTEM_FONTS.by_family(family, bold, italic)]
        if " " not in family:  # "TimesNewRoman" → "Times New Roman"
            spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family)
            candidates += [(f, "системный, то же семейство") for f in SYSTEM_FONTS.by_family(spaced, bold, italic)]
        kind = "mono" if flags & FLAG_MONO else "serif" if flags & FLAG_SERIF else "sans"
        for generic in SystemFonts.GENERIC[kind] + SystemFonts.GENERIC["unicode"]:
            candidates += [(f, "замена") for f in SYSTEM_FONTS.by_family(generic, bold, italic)]

        seen = set()
        for face, why in candidates:
            if (face.path, face.index) in seen:
                continue
            seen.add((face.path, face.index))
            sys_font = SYSTEM_FONTS.load(face)
            if self.covers(sys_font, text):
                return ResolvedFont(sys_font, f"{face.family} {face.subfamily} ({why})", False)

        # совсем ничего — встроенный шрифт PyMuPDF
        return ResolvedFont(pymupdf.Font("helv"), "Helvetica (последняя замена)", False)
