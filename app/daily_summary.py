"""Calendar-day learning summaries; persisted results are never overwritten."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import encode, now


def summary_zone(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('请填写有效的时区，例如 Asia/Shanghai')
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('请填写有效的时区，例如 Asia/Shanghai') from None


def study_day(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError('日期需要使用 YYYY-MM-DD 格式') from None


class DailySummaries:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def today(self, zone=None, instant=None):
        return (instant or datetime.now(timezone.utc)).astimezone(
            summary_zone(zone or self.service.settings()['daily_summary_timezone'])).date()

    def validate(self, day, zone):
        summary_zone(zone)
        if study_day(day) >= self.today(zone):
            raise ValueError('请选择昨天或更早的日期，完整一天结束后才能生成总结')
        return day

    def record(self, day, zone):
        return self.store.one('SELECT * FROM daily_summaries WHERE study_date=? AND timezone=?', (day, zone))

    def tick(self, instant=None):
        config = self.service.settings()
        if not config['daily_summary_enabled']:
            return
        local = (instant or datetime.now(timezone.utc)).astimezone(summary_zone(config['daily_summary_timezone']))
        if local.strftime('%H:%M') < config['daily_summary_time']:
            return
        day = (local.date()-timedelta(days=1)).isoformat()
        row = self.record(day, config['daily_summary_timezone'])
        if row:
            if row['status'] != 'error' or row['attempts'] >= 3:
                return
            if local-datetime.fromisoformat(row['updated_at']) < timedelta(hours=1):
                return
        self.service.enqueue('daily_summary', dict(day=day, zone=config['daily_summary_timezone'], automatic=True))

    def sources(self, day, zone):
        start = datetime.combine(study_day(day), time.min, summary_zone(zone))
        end = datetime.combine(study_day(day)+timedelta(days=1), time.min, summary_zone(zone))
        viewed = {row['note_id'] for row in self.store.query(
            'SELECT DISTINCT note_id FROM note_views WHERE julianday(viewed_at)>=julianday(?) AND julianday(viewed_at)<julianday(?)',
            (start.isoformat(), end.isoformat()))}
        sources = []
        for note in self.store.notes():
            if not self.service.in_scope(note['remote_path']):
                continue
            reasons = ['网页查看'] if note['id'] in viewed else []
            if note['time_trusted'] and note['device_modified']:
                try:
                    modified = datetime.fromisoformat(note['device_modified'].replace('Z', '+00:00'))
                    if modified.tzinfo and start <= modified < end:
                        reasons.append('批注修改')
                except ValueError:
                    pass
            if not reasons:
                continue
            sources.append(dict(id=note['id'], document=note['document_title'], page=note['page'],
                title=note['title'], evidence=reasons, transcript=note['transcript'], question=note['question'],
                anchor=note['anchor'], answer=note['answer'], status=note['status'],
                confirmation_stale=bool(note['confirmation_stale'])))
        return sorted(sources, key=lambda item: item['id'])

    def generate(self, day, zone, automatic=False):
        self.validate(day, zone)
        if automatic:
            config = self.service.settings()
            if not config['daily_summary_enabled'] or zone != config['daily_summary_timezone']:
                return {'message': '每日总结已关闭或时区已改变，跳过排队任务'}
        with self.store.transaction():
            old = self.record(day, zone)
            if old and old['status'] in ('ready', 'empty', 'running'):
                return {'message': '这一天已有总结或正在生成', 'summary_id': old['id']}
            self.store.execute('''INSERT INTO daily_summaries(study_date,timezone,status,attempts,created_at,updated_at)
                VALUES(?,?,'running',1,?,?) ON CONFLICT(study_date,timezone) DO UPDATE SET
                status='running',attempts=attempts+1,error='',updated_at=excluded.updated_at''', (day, zone, now(), now()))
            identifier = self.record(day, zone)['id']
        try:
            sources = self.sources(day, zone)
            model = self.service.settings()['model_name'] if sources else ''
            self.store.execute('UPDATE daily_summaries SET sources=?,note_count=?,model=? WHERE id=?',
                               (encode(sources), len(sources), model, identifier))
            body = self.service.model.summarize_day(day, zone, sources) if sources else '当天没有找到符合范围的笔记记录，未调用 AI。'
            header = f'# {day} 学习总结\n\n时区：{zone} · {len(sources)} 条笔记\n\n'
            limitation = '依据：当前同步范围内，当天有可信批注修改时间或网页查看记录的笔记（去重）。这不代表阅读时长或知识掌握程度；内容采用生成时保存的转写与解答。\n\n'
            references = '\n\n## 来源笔记\n\n' + '\n'.join(
                f"- #{item['id']} · {item['document'].replace(chr(10), ' ')} · P{item['page']} · {item['title'].replace(chr(10), ' ')}（{'、'.join(item['evidence'])}）" for item in sources) if sources else ''
            self.store.execute('UPDATE daily_summaries SET status=?,markdown=?,updated_at=? WHERE id=?',
                               ('ready' if sources else 'empty', header+limitation+body+references, now(), identifier))
            self.store.event('daily_summary', f'{day} 的学习总结已保存，共 {len(sources)} 条笔记')
            return {'message': f'{day} 的学习总结已保存', 'summary_id': identifier}
        except Exception as error:
            self.store.execute("UPDATE daily_summaries SET status='error',error=?,updated_at=? WHERE id=?",
                               (self.service.safe_error(error), now(), identifier))
            raise

    def listing(self, day=None):
        zone = self.service.settings()['daily_summary_timezone']
        yesterday = (self.today(zone)-timedelta(days=1)).isoformat()
        day = day or yesterday
        self.validate(day, zone)
        items = self.store.query('''SELECT id,study_date,timezone,status,note_count,updated_at FROM daily_summaries
            ORDER BY study_date DESC,id DESC LIMIT 60''')
        return dict(items=items, selected=self.public(self.record(day, zone)), yesterday=yesterday, timezone=zone)

    @staticmethod
    def public(row):
        if row is None:
            return None
        row = dict(row)
        row['sources'] = [dict(id=item['id'], document=item['document'], page=item['page'], title=item['title'])
                          for item in json.loads(row['sources'])]
        return row
