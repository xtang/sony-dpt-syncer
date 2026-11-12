#!/usr/bin/env python3
"""Build normal-size portrait exam PDFs, preserving one source page per page."""
import io
import json
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import build_dpt_exams as original

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"output/pdf/DPT真题_竖屏版"
META=ROOT/"output/analysis/dpt-exams-portrait"
TEMP=ROOT/"tmp/pdfs/dpt-exams-portrait"
W,H=1650/207*72,2200/207*72
MARGIN,TOP,BOTTOM=16,34,25
CW,CH=W-2*MARGIN,H-TOP-BOTTOM


def font_cap(doc,page):
    path=ROOT/doc["output"]/"ocr"/f"page-{page:02d}.json"
    if not path.exists():return .30
    data=json.loads(path.read_text())
    values=[w["height"] for line in data["lines"] for w in line.get("words",[])
            if w["confidence"]>45 and len(w["text"])>=2
            and 20<=w["height"]<data["height"]*.05
            and .5<w["width"]/len(w["text"])/w["height"]<1.7
            and re.search("[ぁ-ヿ一-龯]",w["text"])]
    return min(.30,10.5/float(np.median(values))) if len(values)>=3 else .30


def header(c,title,right,page,total,back=False):
    c.setFillColorRGB(0,0,0);c.setFont("DPTBody",10)
    c.drawString(MARGIN,H-17,title);c.drawRightString(W-MARGIN,H-17,right)
    c.setStrokeGray(.6);c.setLineWidth(.35);c.line(MARGIN,H-24,W-MARGIN,H-24)
    c.setFont("DPTBody",9)
    c.drawString(MARGIN,11,"原页目录（点此返回）" if back else "DPT 竖屏版 · 原页完整保留")
    c.drawRightString(W-MARGIN,11,f"{page} / {total}")
    if back:c.linkRect("","index",(MARGIN,6,MARGIN+120,24),relative=0,thickness=0)


def build(doc):
    original.fonts();original.TEMP=TEMP
    prior=json.loads((ROOT/"output/analysis/dpt-exams"/(doc["id"]+".json")).read_text())
    assert prior["source"]==doc["source"]
    entries=[]
    for old in prior["entries"]:
        page=old["source_page"]
        image=original.image_path(doc,page)
        x0,y0,x1,y1=old["crop"]
        scale=min(CW/(x1-x0),CH/(y1-y0),font_cap(doc,page))
        entries.append(dict(source_page=page,image=str(image),crop=old["crop"],
                            segments=[[0,y1-y0]],scale=scale,
                            footer_removed=old["footer_removed"],
                            first_output_page=len(entries)+2))
    total=len(entries)+1
    title=f"{doc['year']} 年 {doc['month']:02d} 月 JLPT N2 真题"
    dest=OUT/f"{doc['year']}-{doc['month']:02d}_N2真题_DPT竖屏版.pdf"
    c=canvas.Canvas(str(dest),pagesize=(W,H),pdfVersion=(1,4),pageCompression=1,initialFontName="DPTBody")
    c.setTitle(title+" | DPT 竖屏版");c.setAuthor("本地真题阅读排版")
    c.bookmarkPage("index");c.addOutlineEntry("考试年月与原页目录","index",0)
    header(c,title,"阅读说明与目录",1,total)
    c.setFont("DPTStrong",25);c.drawString(24,H-71,f"{doc['year']} 年 {doc['month']:02d} 月")
    c.setFont("DPTStrong",21);c.drawString(24,H-104,"JLPT N2 真题")
    c.setFont("DPTBody",12);c.drawString(24,H-133,"竖屏阅读 · 适中字号 · 原页不拆分")
    c.setFont("DPTBody",10.5)
    notes=["保留原字体、注音、下划线、题目选项和图表，仅裁去多余白边。",
           "每个原 PDF 页完整保留为一页，不使用 OCR 文字替换题目。",
           "点击下方页码或使用 PDF 书签，跳到对应原页。",
           f"收录原 PDF 第 {entries[0]['source_page']} 至 {doc['pages']} 页；保留原有答案和批注，不含听力音频。"]
    for i,text in enumerate(notes):c.drawString(24,H-161-i*17,text)
    for j,e in enumerate(entries):
        x=MARGIN+(j%4)*CW/4;y=H-242-(j//4)*31
        c.setStrokeGray(.7);c.rect(x,y-21,CW/4-7,27)
        c.setFont("DPTBody",10.5);c.drawString(x+7,y-11,f"原 PDF 第 {e['source_page']} 页")
        c.linkRect("",f"source-{e['source_page']}",(x,y-21,x+CW/4-7,y+6),relative=0,thickness=0)
    c.showPage()
    for number,e in enumerate(entries,2):
        c.bookmarkPage(f"source-{e['source_page']}")
        c.addOutlineEntry(f"原 PDF 第 {e['source_page']} 页",f"source-{e['source_page']}",0)
        header(c,title,f"原 PDF 第 {e['source_page']} 页",number,total,back=True)
        with Image.open(e["image"]) as im:region=im.convert("L").crop(e["crop"])
        size=(max(1,round(region.width*e["scale"]*207/72)),
              max(1,round(region.height*e["scale"]*207/72)))
        region=region.resize(size,Image.Resampling.LANCZOS)
        stream=io.BytesIO();region.save(stream,"JPEG",quality=90,optimize=True)
        dw,dh=size[0]*72/207,size[1]*72/207
        assert dw<=CW+.4 and dh<=CH+.4
        c.drawImage(ImageReader(stream),MARGIN+(CW-dw)/2,H-TOP-dh,width=dw,height=dh)
        c.showPage()
    c.save()
    record=dict(id=doc["id"],title=title,source=doc["source"],pdf=str(dest.relative_to(ROOT)),
                source_pages=doc["pages"],included_source_pages=prior["included_source_pages"],
                output_pages=total,image_pages=len(entries),bytes=dest.stat().st_size,
                page_size_points=[W,H],pixel_target=[1650,2200],dpi=207,
                original_text_preserved=True,ocr_text_used=False,
                segments_cover_cropped_region=True,one_source_page_per_page=True,entries=entries)
    (META/(doc["id"]+".json")).write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:record[k] for k in ["id","output_pages","bytes"]}),flush=True)
    return record


def main():
    for path in (OUT,META,TEMP):path.mkdir(parents=True,exist_ok=True)
    docs=json.loads((ROOT/"output/markdown/batch-manifest.json").read_text())["documents"]
    with ProcessPoolExecutor(max_workers=3) as pool:records=list(pool.map(build,docs))
    manifest=dict(documents=records,total_documents=len(records),
                  total_output_pages=sum(d["output_pages"] for d in records),
                  total_bytes=sum(d["bytes"] for d in records),
                  excluded_note_files=[p.name for p in (ROOT/"N2真题").glob("*Note*.pdf")])
    (META/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")


if __name__=="__main__":main()
