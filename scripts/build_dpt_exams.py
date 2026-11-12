#!/usr/bin/env python3
"""Create faithful enlarged landscape PDFs for Sony DPT-RP1 from page scans."""
import argparse
import io
import json
import math
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf/DPT真题"
META = ROOT / "output/analysis/dpt-exams"
TEMP = ROOT / "tmp/pdfs/dpt-exams"
W, H = 2200 / 207 * 72, 1650 / 207 * 72
MARGIN, TOP, BOTTOM = 20, 48, 32
CONTENT_W, CONTENT_H = W - 2 * MARGIN, H - TOP - BOTTOM


def fonts():
    pdfmetrics.registerFont(TTFont("DPTBody", "/System/Library/Fonts/STHeiti Light.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont("DPTStrong", "/System/Library/Fonts/STHeiti Medium.ttc", subfontIndex=0))


def image_path(doc, page):
    cached = ROOT / doc["output"] / "pages" / f"page-{page:02d}.jpg"
    if cached.exists():
        return cached
    prefix = TEMP / f"{doc['id']}-extra-{page:02d}"
    path = prefix.with_suffix(".jpg")
    if not path.exists():
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                        "-r", "300", "-jpeg", "-jpegopt", "quality=94",
                        str(ROOT / doc["source"]), str(prefix)], check=True, capture_output=True)
    return path


def runs(values):
    padded = np.concatenate(([False], values, [False])).astype(np.int8)
    change = np.diff(padded)
    return list(zip(np.flatnonzero(change == 1).tolist(), np.flatnonzero(change == -1).tolist()))


def font_limit(doc,page):
    path=ROOT/doc["output"]/"ocr"/f"page-{page:02d}.json"
    if not path.exists():return .40
    data=json.loads(path.read_text())
    heights=[word["height"] for line in data["lines"] for word in line.get("words",[])
             if word["confidence"]>45 and len(word["text"])>=2
             and 20<=word["height"]<data["height"]*.05
             and .5<word["width"]/len(word["text"])/word["height"]<1.7
             and re.search("[ぁ-ヿ一-龯]",word["text"])]
    return min(.40,17/float(np.median(heights))) if len(heights)>=3 else .40


def plan(doc, page):
    path = image_path(doc, page)
    with Image.open(path) as im:
        gray = np.asarray(im.convert("L"))
    height, width = gray.shape
    layout = np.asarray(Image.fromarray(gray).filter(ImageFilter.MedianFilter(3)))
    dark = layout < 185
    # Remove isolated scan specks only from the layout mask; image pixels remain untouched.
    neighbors = np.zeros_like(gray, dtype=np.uint8)
    for dy, dx in ((-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)):
        neighbors[1:-1,1:-1] += dark[1+dy:height-1+dy,1+dx:width-1+dx]
    mask = dark & (neighbors >= 2)
    rows = mask.sum(axis=1)
    cols = mask.sum(axis=0)
    xs = np.flatnonzero(cols >= max(4, height * .001))
    ys = np.flatnonzero(rows >= max(4, width * .001))
    if not len(xs) or not len(ys):
        return dict(source_page=page, image=str(path), crop=[0,0,width,height],
                    segments=[[0,height]], scale=min(CONTENT_W/width, CONTENT_H/height),
                    blank=True, footer_removed=False)
    x0, x1 = max(0,int(xs[0])-24), min(width,int(xs[-1])+25)
    y0, y1 = max(0,int(ys[0])-16), min(height,int(ys[-1])+17)
    footer_removed = False
    # Drop only a spatially isolated, centered printed page-number footer.
    footer_start = int(height * .93)
    footer_x = np.flatnonzero(mask[footer_start:,:].sum(axis=0))
    data_path = ROOT / doc["output"] / "ocr" / f"page-{page:02d}.json"
    data=json.loads(data_path.read_text()) if data_path.exists() else None
    footer_words=[w["text"] for line in data["lines"] for w in line.get("words",[])
                  if w["y"]>=footer_start and w["confidence"]>=25] if data else []
    footer_text="".join(footer_words)
    safe_footer=not footer_text or bool(re.fullmatch(r"[0-9０-９\sー一―—\-－|｜()（）.・IiLl_]+",footer_text))
    safe_footer=safe_footer or (doc["year"]==2024 and page==2)
    if safe_footer and len(footer_x) and width*.32 < footer_x[0] < footer_x[-1] < width*.68:
        main_rows = np.flatnonzero(rows[:footer_start] > 4)
        if len(main_rows) and main_rows[-1] < height*.89:
            y1 = min(y1,int(main_rows[-1])+20)
            footer_removed = True
    # OCR geometry is used for line protection, never for the displayed text.
    protected = np.any(mask[y0:y1,x0:x1],axis=1)
    word_rows=np.zeros_like(protected)
    if data:
        for line in data["lines"]:
            for word in line.get("words",[]):
                if word["confidence"] >= 30 and word["height"] < height*.04:
                    a,b=word["y"]-y0, word["y"]+word["height"]-y0
                    protected[max(0,a):max(0,min(len(protected),b))]=True
                    if word["height"]<height*.02:
                        word_rows[max(0,a):max(0,min(len(protected),b))]=True
    ink_rows=np.flatnonzero(protected)
    # Tighten after excluding the footer. Preserve small padding on every side.
    if len(ink_rows):
        last=min(len(protected),int(ink_rows[-1])+14)
        protected=protected[:last]; y1=y0+last
        word_rows=word_rows[:last]
    scale=min(font_limit(doc,page),CONTENT_W/(x1-x0))
    capacity=max(1,int(CONTENT_H/scale))
    gaps=runs(~protected)
    candidates=[]
    for a,b in gaps:
        if b-a >= 4:
            candidates.append((a+b)//2)
    # A tall ruled table can continue across pages at a blank text row; ignore
    # only its thin vertical rules when looking for these fallback boundaries.
    table_mask=mask[y0:y1,x0:x1].copy()
    long_rules=table_mask.sum(axis=0)>table_mask.shape[0]*.35
    for a,b in runs(long_rules):
        if b-a<=12:
            table_mask[:,a:b]=False
    soft_occupied=np.any(table_mask,axis=1)|word_rows
    table_breaks=[(a+b)//2 for a,b in runs(~soft_occupied) if b-a>=5]
    question_breaks=[]
    if data:
        for line in data["lines"]:
            text=line["text"].strip()
            question=bool(re.match(r"^(?:[\[|]\s*\d{0,2}\s*[\]|]?\s*|\d{1,2}\s*[.)）]\s*|[問間岡]\s*題\s*\d)",text))
            if question:
                line_y=line["y"]-y0
                preceding=[v for v in candidates if line_y-100<=v<=line_y]
                if preceding:question_breaks.append(max(preceding))
    segments=[]; start=0; length=y1-y0
    while start < length:
        if length-start <= capacity:
            end=length
        else:
            available=[v for v in candidates if start+capacity*.42 <= v <= start+capacity]
            preferred=[v for v in question_breaks if start+capacity*.60<=v<=start+capacity]
            if preferred:
                end=max(preferred)
            elif available:
                end=max(available)
            else:
                table_available=[v for v in table_breaks if start+capacity*.55<=v<=start+capacity]
                if table_available:
                    end=max(table_available)
                else:
                    # Keep a figure whole when no safe text boundary exists.
                    later=[v for v in candidates if v>start+capacity]
                    end=min(later) if later else length
        assert end>start
        segments.append([start,end]);start=end
    assert segments[0][0]==0 and segments[-1][1]==length
    assert all(a[1]==b[0] for a,b in zip(segments,segments[1:]))
    return dict(source_page=page,image=str(path),crop=[x0,y0,x1,y1],segments=segments,
                scale=scale,footer_removed=footer_removed,blank=False,
                oversize_segments=sum(b-a>capacity for a,b in segments))


def header(c,title,right,page,total,back=False):
    c.setFillColorRGB(0,0,0);c.setFont("DPTBody",12)
    c.drawString(MARGIN,H-20,title)
    c.drawRightString(W-MARGIN,H-20,right)
    c.setStrokeGray(.55);c.setLineWidth(.4)
    c.line(MARGIN,H-29,W-MARGIN,H-29)
    c.setFont("DPTBody",10)
    c.drawString(MARGIN,14,"原页目录（点此返回）" if back else "DPT 横屏保真放大版")
    if back:c.linkRect("","index-0",(MARGIN,8,MARGIN+130,27),relative=0,thickness=0)
    c.drawRightString(W-MARGIN,14,f"{page} / {total}")


def build(doc):
    first=2 if doc["year"]==2024 else 3
    previous=META/(doc["id"]+".json")
    cached={}
    if doc.get("reuse_plan") and previous.exists():
        prior=json.loads(previous.read_text())
        assert prior["source"]==doc["source"] and prior["source_pages"]==doc["pages"]
        cached={e["source_page"]:e for e in prior["entries"]
                if e["scale"]<=font_limit(doc,e["source_page"])+1e-9}
    entries=[cached[p] if p in cached else plan(doc,p) for p in range(first,doc["pages"]+1)]
    title=f"{doc['year']} 年 {doc['month']:02d} 月 JLPT N2 真题"
    index_count=math.ceil(len(entries)/32)
    total=1+index_count+sum(len(p["segments"]) for p in entries)
    filename=f"{doc['year']}-{doc['month']:02d}_N2真题_DPT横屏大字版.pdf"
    destination=OUT/filename
    c=canvas.Canvas(str(destination),pagesize=(W,H),pageCompression=1,pdfVersion=(1,4),
                    initialFontName="DPTBody")
    c.setTitle(title+" | DPT 横屏大字版")
    c.setAuthor("本地真题阅读排版")
    c.bookmarkPage("cover");c.addOutlineEntry("考试年月与阅读说明","cover",0)
    header(c,title,"阅读说明",1,total)
    c.setFont("DPTStrong",34);c.drawString(38,H-111,f"{doc['year']} 年 {doc['month']:02d} 月")
    c.setFont("DPTStrong",28);c.drawString(38,H-156,"JLPT N2 真题")
    c.setFont("DPTBody",17);c.drawString(38,H-199,"横屏大字版 · 原版保真")
    notes=["请把阅读器横向拿着，使用单页显示。",
           "保留原文字体、题号、选项、下划线、注音与图片，不使用 OCR 文字替换题目。",
           "裁去空白边后放大；密集原页按从上到下的顺序拆页。",
           "分页尽量避开文字行；长表格分段延续，无法安全拆分的图表整体保留。",
           "点下一页的原页目录，或使用 PDF 书签，跳到对应的原 PDF 页。",
           f"原 PDF 共 {doc['pages']} 页；本版保留第 {first} 至 {doc['pages']} 页，封面以此页代替。",
           "原文件中已有的批注、答案及解析均随原页保留；本版不包含听力音频。"]
    c.setFont("DPTBody",13)
    for n,text in enumerate(notes):c.drawString(38,H-244-n*25,text)
    c.showPage()
    n=2
    for idx in range(index_count):
        key=f"index-{idx}";c.bookmarkPage(key)
        if idx==0:c.addOutlineEntry("原页跳转目录",key,0)
        header(c,title,"原页跳转目录",n,total)
        c.setFont("DPTBody",13);c.drawString(MARGIN,H-54,"点击原 PDF 页码跳转；正文页左下角可返回本目录。")
        selected=entries[idx*32:(idx+1)*32]
        for j,entry in enumerate(selected):
            x=MARGIN+(j%4)*(CONTENT_W/4);y=H-88-(j//4)*54
            c.setStrokeGray(.7);c.rect(x,y-38,CONTENT_W/4-10,44)
            c.setFont("DPTStrong",14);c.drawString(x+10,y-10,f"原 PDF 第 {entry['source_page']} 页")
            c.setFont("DPTBody",11);c.drawString(x+10,y-28,f"放大为 {len(entry['segments'])} 页")
            c.linkRect("",f"source-{entry['source_page']}",(x,y-38,x+CONTENT_W/4-10,y+6),relative=0,thickness=0)
        c.showPage();n+=1
    image_count=0
    for entry in entries:
        with Image.open(entry["image"]) as original:
            gray=original.convert("L")
        x0,y0,x1,y1=entry["crop"]
        entry["first_output_page"]=n
        c.bookmarkPage(f"source-{entry['source_page']}")
        c.addOutlineEntry(f"原 PDF 第 {entry['source_page']} 页",f"source-{entry['source_page']}",0)
        for part,(a,b) in enumerate(entry["segments"],1):
            header(c,title,f"原 PDF 第 {entry['source_page']} 页 · {part}/{len(entry['segments'])}",n,total,back=True)
            region=gray.crop((x0,y0+a,x1,y0+b))
            factor=min(entry["scale"],CONTENT_W/region.width,CONTENT_H/region.height)
            target=(max(1,round(region.width*factor*207/72)),max(1,round(region.height*factor*207/72)))
            region=region.resize(target,Image.Resampling.LANCZOS)
            payload=io.BytesIO();region.save(payload,"JPEG",quality=90,optimize=True)
            draw_w,draw_h=target[0]*72/207,target[1]*72/207
            assert draw_w<=CONTENT_W+.4 and draw_h<=CONTENT_H+.4
            c.drawImage(ImageReader(payload),MARGIN+(CONTENT_W-draw_w)/2,H-TOP-draw_h,
                        width=draw_w,height=draw_h)
            c.showPage();n+=1;image_count+=1
    assert n-1==total
    c.save()
    record=dict(id=doc["id"],title=title,source=doc["source"],pdf=str(destination.relative_to(ROOT)),
                source_pages=doc["pages"],included_source_pages=list(range(first,doc["pages"]+1)),
                output_pages=total,image_pages=image_count,bytes=destination.stat().st_size,
                page_size_points=[W,H],pixel_target=[2200,1650],dpi=207,
                original_text_preserved=True,ocr_text_used=False,segments_cover_cropped_region=True,
                entries=entries)
    (META/(doc["id"]+".json")).write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:record[k] for k in ("id","output_pages","bytes")}),flush=True)
    return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exam",action="append")
    parser.add_argument("--workers",type=int,default=2)
    parser.add_argument("--reuse-plan",action="store_true",help="Reuse already checked regions that meet the font size limit")
    args=parser.parse_args()
    for path in (OUT,META,TEMP):path.mkdir(parents=True,exist_ok=True)
    fonts()
    docs=json.loads((ROOT/"output/markdown/batch-manifest.json").read_text())["documents"]
    selected=[d for d in docs if not args.exam or d["id"] in args.exam]
    for d in selected:d["reuse_plan"]=args.reuse_plan
    if args.workers>1:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            records=list(pool.map(worker,selected))
    else:records=[build(d) for d in selected]
    if not args.exam:
        manifest=dict(documents=records,excluded_note_files=[p.name for p in (ROOT/"N2真题").glob("*Note*.pdf")],
                      total_documents=len(records),total_output_pages=sum(r["output_pages"] for r in records),
                      total_bytes=sum(r["bytes"] for r in records))
        (META/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n")


def worker(doc):
    fonts()
    return build(doc)


if __name__=="__main__":main()
