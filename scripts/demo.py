#!/usr/bin/env python3
"""Serve the real UI with fictional, local-only notes for documentation screenshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont
from app.server import Handler
from app.service import Service, document_id
from app.store import encode


def font_path(override=None):
    candidates=[override] if override else [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/System/Library/Fonts/Supplemental/Songti.ttc',
        '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for path in candidates:
        if path and Path(path).is_file(): return str(path)
    raise ValueError('A CJK font is required. Install Noto CJK or pass --font /path/to/font.ttc.')


def preview(font, section, title, lines, question, kind='underline'):
    image=Image.new('RGB',(1080,620),'#fffefa')
    draw=ImageDraw.Draw(image)
    serif=lambda size:ImageFont.truetype(font,size)
    draw.text((68,43),section,font=serif(23),fill='#87847b')
    draw.line((68,88,1012,88),fill='#d9d5c9',width=2)
    draw.text((68,118),title,font=serif(39),fill='#252521')
    for i,line in enumerate(lines):
        draw.text((68,210+i*70),line,font=serif(31),fill='#393934')
    blue='#285f9d'
    if kind=='circle':
        x=68+draw.textlength(lines[2].split('3 背')[0],font=serif(31))
        draw.arc((x-12,342,x+70,395),5,359,fill=blue,width=5)
        draw.arc((x-15,345,x+73,398),160,340,fill=blue,width=3)
    else:
        draw.line([(68,258),(240,260),(430,257),(635,259)],fill=blue,width=4)
        draw.line([(72,264),(360,265),(628,263)],fill=blue,width=2)
    draw.text((155,475),question,font=serif(36),fill=blue)
    draw.line([(136,503),(93,453),(115,464),(93,453),(95,475)],fill=blue,width=3)
    draw.text((68,575),'墨记  /  演示文档',font=serif(20),fill='#b0aaa0')
    return image


def seed(data, font):
    data=Path(data).resolve()
    if data.exists() and any(data.iterdir()):
        raise ValueError('Choose an empty --data directory; demo creation never overwrites existing data.')
    service=Service(data,data/'unused-auth')
    service.config.update(device_address='',model_base_url='',model_name='',model_api_key='',
                          auto_sync=False,auto_analyze=False,sync_folders=['Document/日语学习','Document/阅读札记'])
    service._write_settings()
    base=datetime.now(timezone.utc).replace(second=0,microsecond=0)
    stamp=lambda minutes:(base-timedelta(minutes=minutes)).isoformat()
    service.config['last_sync']=stamp(3);service._write_settings()
    for folder in ['Document','Document/日语学习','Document/阅读札记','Document/稍后阅读']:
        service.store.execute('INSERT INTO folders(path,entry_id,last_seen) VALUES(?,?,?)',(folder,folder,stamp(3)))
    images={
        'direction':preview(font,'GRAMMAR NOTES  /  01','変化の方向をとらえる',[
            'これからも、日本語を勉強していきたい。',
            '毎日練習して、少しずつ話せるようになってきた。',
            '「ていく」と「てくる」は、時間の向きを表す。'],'这两个表达的时间方向有什么区别？'),
        'uchi':preview(font,'GRAMMAR NOTES  /  02','「うちに」の使い方',[
            '温かいうちに、スープを飲んでください。',
            '忘れないうちに、メモしておこう。'],'和「あいだに」有什么区别？'),
        'habit':preview(font,'GRAMMAR NOTES  /  03','変化と習慣',[
            '最近、日本語の新聞が読めるようになった。',
            '毎朝、新聞を読むようにしている。'],'能力的变化？还是有意识养成习惯？'),
        'exercise':preview(font,'VOCABULARY PRACTICE  /  08','下線のことばを漢字で書きましょう。',[
            '08   重い荷物を持って、かたが痛くなった。',
            '         正しい漢字を一つ選んでください。',
            '         1 腰          2 腹          3 背          4 肩'],'选了 3，身体部位的汉字再核对一下。','circle'),
        'positive':preview(font,'VOCABULARY PRACTICE  /  09','ことばの意味を考える',[
            '新しい活動に、積極的に参加してみよう。',
            '「積極的」は、自分から進んで行動する様子。'],'積極的：主动、积极'),
        'retry':preview(font,'SYSTEM DESIGN NOTES  /  01','重试与指数退避',[
            '失败后立即重试，可能让已经拥塞的服务更繁忙。',
            '逐步延长等待时间，并加入随机抖动。',
            '为重试设置次数上限，以及整体超时预算。'],'为什么还要加入随机抖动？'),
    }
    docs=[('grammar','Document/日语学习/语法阅读笔记.pdf','日语阅读 · 变化与习惯',['direction','uchi','habit']),
          ('exercise','Document/日语学习/汉字与词汇练习.pdf','日语练习 · 汉字与词汇',['exercise','positive']),
          ('reading','Document/阅读札记/系统设计札记.pdf','系统设计 · 阅读札记',['retry'])]
    docids={};snapshots={}
    for key,path,title,pages in docs:
        identifier=document_id(path);docids[key]=identifier
        target=data/'documents'/identifier/'demo.pdf';target.parent.mkdir(parents=True)
        images[pages[0]].save(target,'PDF',resolution=120,save_all=True,append_images=[images[p] for p in pages[1:]])
        sha=hashlib.sha256(target.read_bytes()).hexdigest()
        service.store.execute('INSERT INTO documents(id,remote_path,name,title,pages,tracked,created_at,device_modified,file_size) VALUES(?,?,?,?,?,1,?,?,?)',
                              (identifier,path,Path(path).name,title,len(pages),stamp(60),stamp(3),target.stat().st_size))
        snapshots[key]=service.store.execute('INSERT INTO snapshots(document_id,sha256,pdf_path,captured_at) VALUES(?,?,?,?)',(identifier,sha,str(target),stamp(3)))
        service.store.execute('UPDATE documents SET snapshot_id=? WHERE id=?',(snapshots[key],identifier))
    def note(key,page,title,transcript,question,image,minutes,tags,answer=None,**flags):
        picture=data/'previews'/(image+'.png');picture.parent.mkdir(exist_ok=True);images[image].save(picture)
        identifier=service.store.execute('''INSERT INTO notes(document_id,page,group_key,content_hash,annotation_ids,snapshot_id,
            preview_path,source_text,title,transcript,question,kind,status,confidence,tags,device_created,device_modified,
            time_trusted,first_seen,last_seen,activity_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)''',
            (docids[key],page,'demo-'+title,hashlib.sha256(title.encode()).hexdigest(),'[]',snapshots[key],str(picture),'',
             title,transcript,question,flags.get('kind','question'),'pending',.98,encode(tags),stamp(minutes+20),stamp(minutes),stamp(minutes+20),stamp(minutes),stamp(minutes)))
        if answer:
            service.store.save_answer(identifier,dict(answer,tags=tags,sources=[docs[[d[0] for d in docs].index(key)][2]+f'，第{page}页']), '演示解答')
        updates=dict(flags,activity_at=stamp(minutes),unread=0)
        service.store.execute('UPDATE notes SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),identifier))
        return identifier
    main=note('grammar',1,'ていく・てくる：变化的方向','这两个表达的时间方向有什么区别？','为什么一个面向未来，一个回看过去？','direction',5,['日语语法','表达辨析'],dict(
        summary='把现在当作观察点：ていく看向之后，てくる回顾到现在。',
        explanation='「〜ていく」描述从现在延续到未来的变化或行动。「勉強していきたい」强调今后还想继续学习。\n\n「〜てくる」描述过去到现在的变化。「話せるようになってきた」强调练习积累到现在，逐渐能开口表达。\n\n先找到说话人的观察时间，再判断变化是从这里继续，还是向这里累积。',
        examples=[dict(text='これからも練習を続けていく。',explanation='从现在开始，今后也会继续练习。'),dict(text='少しずつ自信がついてきた。',explanation='经过积累，到现在逐渐有了自信。')]))
    note('grammar',2,'うちに：趁状态还没改变','和「あいだに」有什么区别？','对比两种时间表达。','uchi',14,['日语语法','时间表达'],dict(summary='うちに强调机会窗口；あいだに强调某段时间内。',explanation='「温かいうちに」含有趁还热着、不要等到变凉的意味。あいだに主要标示事情发生在哪段时间里。'))
    stale=note('grammar',3,'ようになる・ようにする：变化与习惯','能力的变化？','这句话表达了什么变化？','habit',22,['日语语法','习惯'],dict(summary='ようになる描述状态或能力发生变化。',explanation='「読めるようになった」表示从以前读不了，到现在能读了。'),manual_transcript=1)
    service.store.capture_revision(service.store.note(stale),'handwriting_changed')
    service.store.execute("UPDATE notes SET confirmation_stale=1,status='needs_review' WHERE id=?",(stale,))
    note('grammar',1,'页边的一笔标记','','','direction',50,['阅读标记'],ignored=1,kind='mark')
    exercise=note('exercise',1,'肩与背：身体部位的汉字','第8题：3 背','核对「かた」对应的汉字。','exercise',8,['日语词汇','汉字'],dict(
        summary='第8题需要订正：かた应写作「肩」，选4。',explanation='结合读音与语境区分身体部位，详见逐题解析。',exercise_reviews=[dict(question_ref='8',question='重い荷物を持って、かたが痛くなった。',user_answer='3 背',suggested_answer='4 肩',verdict='incorrect',basis='reasoning',confidence=.99,reference_quote='',explanation='「肩」读作かた，意思是肩膀。「背」通常读作せ，指背部；两者部位和读音都不同。',knowledge_points=['肩（かた）：肩膀；背（せ）：背部。','腰（こし）：腰；腹（はら）：腹部。'])]),kind='answer',scope_status='known',scope_version=1,question_refs='["8"]')
    note('exercise',2,'積極的：主动参与的态度','積極的：主动、积极','理解例句中的用法。','positive',18,['日语词汇'],dict(summary='積極的表示主动、积极地采取行动。',explanation='「積極的に参加する」表示主动参与，与消极、被动相对。'),kind='mark')
    note('exercise',2,'圈选有擦改，请核对最终答案','圈选位置不够清楚。','请核对最终选择。','positive',30,['作答核对'],status='needs_review',confidence=.62,kind='answer')
    note('reading',1,'重试为什么需要随机抖动','为什么还要加入随机抖动？','避免多个客户端一起重试。','retry',35,['系统设计','可靠性'],dict(summary='随机抖动把重试时间分散开，减少同步重试造成的流量尖峰。',explanation='指数退避逐步增加等待时间，但同一时刻失败的客户端仍可能一起重试。加入随机性可以把重试摊开；同时还应限制重试次数和总耗时。'))
    note('reading',1,'幂等与重试的边界','如何判断一个操作能不能安全重试？','区分读取、重复提交与幂等键。','retry',42,['系统设计'],status='pending',confidence=None)
    service.store.execute("UPDATE notes SET unread=1 WHERE id IN (2,6,8)")
    service.store.execute('DELETE FROM events')
    service.store.event('sync','演示数据：整理了 3 份文档与 9 条笔记')
    (data/'demo-fixture.json').write_text(encode(dict(version=1,fictional=True,main_note=main,exercise_note=exercise,revision_note=stale)))
    return service


class DemoHandler(Handler):
    def post(self,path):
        if re.fullmatch(r'/api/(?:notes/\d+/(?:read|edit|ignore|view)|documents/[a-f0-9]{24}/read)',path):
            return super().post(path)
        self.json_body()
        return self.json({'error':'演示模式：设备连接、上传和模型调用已禁用。'},400)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=Path('output/demo'))
    parser.add_argument('--font',help='Path to a CJK TrueType/OpenType font')
    parser.add_argument('--port',type=int,default=4391)
    args=parser.parse_args()
    if (args.data/'demo-fixture.json').is_file():
        marker=json.loads((args.data/'demo-fixture.json').read_text())
        if marker.get('fictional') is not True or marker.get('version')!=1:
            raise ValueError('This directory is not a supported demo fixture.')
        service=Service(args.data,args.data/'unused-auth')
    else:
        service=seed(args.data,font_path(args.font))
    # Never load project .env, pair with a reader, or start background workers.
    service.config.update(device_address='',model_base_url='',model_name='',model_api_key='',auto_sync=False,auto_analyze=False)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),DemoHandler)
    server.service=service;server.allowed_hosts=set();server.web_auth=None
    print(f'Fictional demo: http://127.0.0.1:{args.port} (no device or model connections)',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=='__main__': main()
