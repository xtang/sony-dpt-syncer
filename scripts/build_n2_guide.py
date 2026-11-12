#!/usr/bin/env python3
"""Create an embedded-font, monochrome A4 N2 study PDF."""
import html
import json
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from n2_guide_content import make_pages

OUT = Path("output/pdf")
TMP = Path("tmp/pdfs/n2-guide")
DEST = OUT / "N2高频考点与易错辨析_SonyDPT.pdf"
W, H = A4
LEFT, RIGHT, BOTTOM = 40, 40, 48
WIDTH = W - LEFT - RIGHT


def escape(text):
    return html.escape(str(text)).replace("\n", "<br/>")


class Painter:
    def __init__(self, target, page, number, total):
        self.c, self.page, self.number, self.total = target, page, number, total
        self.y = H - 85
        self.styles = {
            "body": ParagraphStyle("body", fontName="Body", fontSize=13.4, leading=20.2, wordWrap="CJK"),
            "jp": ParagraphStyle("jp", fontName="Strong", fontSize=14.3, leading=21.5, wordWrap="CJK"),
            "small": ParagraphStyle("small", fontName="Body", fontSize=11.6, leading=17, wordWrap="CJK"),
            "card": ParagraphStyle("card", fontName="Strong", fontSize=15.5, leading=22, wordWrap="CJK"),
            "table": ParagraphStyle("table", fontName="Body", fontSize=12.8, leading=18.6, wordWrap="CJK"),
            "tablehead": ParagraphStyle("tablehead", fontName="Strong", fontSize=12.7, leading=18.5, wordWrap="CJK"),
        }

    def paragraph(self, text, style="body", after=6, indent=0):
        p = Paragraph(escape(text), self.styles[style])
        width, height = p.wrap(WIDTH - indent, H)
        if self.y - height < BOTTOM:
            raise ValueError(f"Page {self.number} overflow ({self.page['title']}): {text[:55]} at {self.y:.1f}, needs {height:.1f}")
        p.drawOn(self.c, LEFT + indent, self.y - height)
        self.y -= height + after

    def table(self, headers, rows, widths=None, font_size=None, row_heights=None):
        ratios = widths or [1] * len(headers)
        column_widths = [WIDTH * x / sum(ratios) for x in ratios]
        values = [[Paragraph(escape(x), self.styles["tablehead"]) for x in headers]]
        values.extend([[Paragraph(escape(x), self.styles["table"]) for x in row] for row in rows])
        t = Table(values, colWidths=column_widths, rowHeights=row_heights, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEEEEE")),
            ("FONTNAME", (0, 0), (-1, -1), "Body"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), .8, colors.black),
            ("LINEBELOW", (0, 1), (-1, -1), .3, colors.HexColor("#BBBBBB")),
        ]))
        tw, th = t.wrap(WIDTH, H)
        if self.y - th < BOTTOM:
            raise ValueError(f"Page {self.number} table overflow: {self.page['title']}, needs {th:.1f}, available {self.y - BOTTOM:.1f}")
        t.drawOn(self.c, LEFT, self.y - th)
        self.y -= th + 10

    def card(self, item):
        self.paragraph(item["title"], "card", 4)
        if item.get("form"):
            self.paragraph("接续：" + item["form"], "small", 4)
        self.paragraph(item["point"], "body", 5)
        if item.get("jp"):
            self.paragraph(item["jp"], "jp", 2, 10)
        if item.get("cn"):
            self.paragraph(item["cn"], "body", 4, 10)
        if item.get("trap"):
            self.paragraph("排错：" + item["trap"], "small", 9)
        else:
            self.y -= 5
        self.c.setStrokeColor(colors.HexColor("#BBBBBB"))
        self.c.setLineWidth(.4)
        self.c.line(LEFT, self.y + 2, W - RIGHT, self.y + 2)
        self.y -= 8

    def draw(self):
        c, page = self.c, self.page
        c.setFillColor(colors.black)
        c.setFont("Body", 10.5)
        c.drawString(LEFT, H - 29, "N2 高频考点与易错辨析")
        c.drawRightString(W - RIGHT, H - 29, page["section"])
        c.setLineWidth(.7)
        c.line(LEFT, H - 37, W - RIGHT, H - 37)
        c.setFont("Strong", 21)
        c.drawString(LEFT, H - 66, page["title"])
        if page.get("lead"):
            self.paragraph(page["lead"], "small", 11)
        for item in page.get("items", []):
            kind = item["kind"]
            if kind == "card":
                self.card(item)
            elif kind == "table":
                self.table(item["headers"], item["rows"], item.get("widths"))
            elif kind == "text":
                self.paragraph(item["text"], item.get("style", "body"), item.get("after", 7))
            elif kind == "space":
                self.y -= item["height"]
            elif kind == "toc":
                top = self.y
                self.table(["内容", "页码"], item["rows"], [6, .7], row_heights=30)
                for index, (label, target) in enumerate(item["rows"]):
                    c.linkAbsolute(str(label), f"p{target}",
                                   Rect=(LEFT, top - 30 * (index + 2), W - RIGHT, top - 30 * (index + 1)),
                                   thickness=0)
            else:
                raise ValueError(kind)
        if self.y > BOTTOM + 85 and page["section"] not in ("开篇", "练习", "答案", "词汇速查", "资料"):
            c.setFont("Body", 10.5)
            c.setFillColor(colors.HexColor("#555555"))
            c.drawString(LEFT, BOTTOM + 53, "笔记")
            c.setStrokeColor(colors.HexColor("#CCCCCC"))
            for y in [BOTTOM + 38, BOTTOM + 15]:
                c.line(LEFT, y, W - RIGHT, y)
        c.setFillColor(colors.black)
        c.setFont("Body", 10.5)
        c.drawString(LEFT, 23, "A4 黑白版 · 例题为编写练习")
        c.drawRightString(W - RIGHT, 23, f"{self.number} / {self.total}")
        return self.y


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from all_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from all_strings(v)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont("Body", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont("Strong", "/System/Library/Fonts/STHeiti Medium.ttc", subfontIndex=0))
    vocab = json.loads(Path("output/vocabulary/vocabulary-150.json").read_text())["entries"]
    pages, questions = make_pages(vocab)
    missing = sorted({ch for text in all_strings(pages) for ch in text
                      if not ch.isspace() and ord(ch) not in pdfmetrics.getFont("Body").face.charToGlyph})
    if missing:
        raise ValueError("Missing glyphs: " + "".join(missing))
    c = canvas.Canvas(str(DEST), pagesize=A4, pageCompression=1, pdfVersion=(1, 4),
                      initialFontName="Body", initialFontSize=13.4, lang="zh-CN")
    c.setTitle("N2 高频考点与易错辨析")
    c.setAuthor("日语 N2 学习资料")
    c.setSubject("语法接续与辨析、词汇搭配、听力判断、练习解析；A4 黑白阅读版")
    minimum_y = H
    prior_section = None
    bottoms = []
    for i, page in enumerate(pages, 1):
        c.bookmarkPage(f"p{i}")
        if page["section"] != prior_section:
            c.addOutlineEntry(page["section"], f"p{i}", level=0, closed=False)
            prior_section = page["section"]
        c.addOutlineEntry(page["title"], f"p{i}", level=1, closed=False)
        bottom = Painter(c, page, i, len(pages)).draw()
        minimum_y = min(minimum_y, bottom)
        bottoms.append(round(bottom, 2))
        c.showPage()
    c.save()
    manifest = {
        "file": str(DEST), "pages": len(pages), "page_size": "A4 portrait",
        "pdf_version": "1.4", "fonts_embedded": True, "body_font_size_pt": 13.4,
        "japanese_font_size_pt": 14.3, "monochrome": True,
        "practice_questions": len(questions), "minimum_content_y_pt": round(minimum_y, 2),
        "outline": [{"page": i, "section": p["section"], "title": p["title"], "content_bottom_pt": bottoms[i - 1]} for i, p in enumerate(pages, 1)],
    }
    (TMP / "guide-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"file": str(DEST), "pages": len(pages), "questions": len(questions),
                      "bytes": DEST.stat().st_size, "minimum_content_y_pt": round(minimum_y, 2)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
