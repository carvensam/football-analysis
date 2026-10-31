# -*- coding: utf-8 -*-
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone


_LOCAL = threading.local()


def hk_now_str():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S')


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS work_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        status TEXT NOT NULL DEFAULT 'running')''')
    conn.execute('''CREATE TABLE IF NOT EXISTS work_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        league TEXT,
        match_text TEXT,
        tool TEXT,
        direction TEXT,
        done INTEGER,
        total INTEGER,
        markets TEXT,
        pages TEXT,
        status TEXT,
        message TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_work_log_job '
                 'ON work_log(job_id, id)')


def interrupt_running(conn):
    cur = conn.execute(
        "UPDATE work_jobs SET status='interrupted', ended_at=? "
        "WHERE status='running'", (hk_now_str(),))
    return cur.rowcount


def begin(conn, source):
    migrate(conn)
    cur = conn.execute(
        "INSERT INTO work_jobs(source,started_at,status) VALUES(?,?,'running')",
        (str(source or '未命名工作')[:120], hk_now_str()))
    jid = cur.lastrowid
    conn.execute('DELETE FROM work_log WHERE job_id NOT IN '
                 '(SELECT id FROM work_jobs ORDER BY id DESC LIMIT 20)')
    conn.execute('DELETE FROM work_jobs WHERE id NOT IN '
                 '(SELECT id FROM work_jobs ORDER BY id DESC LIMIT 20)')
    conn.commit()
    _LOCAL.job_id = jid
    return jid


def current_job_id():
    return getattr(_LOCAL, 'job_id', None)


def event(conn, kind, league=None, match_text=None, tool=None, direction=None,
          done=None, total=None, markets=None, pages=None, status=None,
          message=None, job_id=None, commit=False):
    jid = job_id if job_id is not None else current_job_id()
    if jid is None:
        return None
    cur = conn.execute(
        'INSERT INTO work_log(job_id,ts,kind,league,match_text,tool,direction,'
        'done,total,markets,pages,status,message) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (jid, hk_now_str(), str(kind)[:30], league, match_text, tool, direction,
         done, total,
         json.dumps(markets or [], ensure_ascii=False) if markets is not None else None,
         json.dumps(pages or [], ensure_ascii=False) if pages is not None else None,
         status, message))
    if commit:
        conn.commit()
    return cur.lastrowid


def end(conn, status='done', message=None):
    jid = current_job_id()
    if jid is None:
        return
    if message:
        event(conn, 'status', status=status, message=message, job_id=jid)
    conn.execute('UPDATE work_jobs SET ended_at=?, status=? WHERE id=?',
                 (hk_now_str(), status, jid))
    conn.commit()
    _LOCAL.job_id = None


def _json_list(value):
    if not value:
        return []
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def tool_progress(events, job_status=None):
    states = {}
    for event in events:
        if event.get('kind') != 'recalc':
            continue
        job_id = event.get('job_id')
        for name in str(event.get('tool') or '全部工具').split('／'):
            state = states.setdefault((job_id, name), {
                'job_id': job_id, 'tool': name, 'done': 0, 'total': None,
                'status': '等待', 'match': None, 'ts': None})
            if event.get('done') is not None:
                state['done'] = max(state['done'], event['done'])
            if event.get('total') is not None:
                state['total'] = event['total']
            state['status'] = event.get('status') or state['status']
            state['match'] = event.get('match_text')
            state['ts'] = event.get('ts')
    job_status = job_status or {}
    for state in states.values():
        if (state['status'] == '完成' and state['total'] is not None
                and state['done'] < state['total']):
            status = job_status.get(state['job_id'])
            state['status'] = ('進行中' if status == 'running' else
                               '已結束（未完成全部）' if status == 'done' else
                               '未完成全部')
    return list(states.values())


def latest(conn):
    job_row = conn.execute(
        'SELECT id,source,started_at,ended_at,status FROM work_jobs '
        'ORDER BY id DESC LIMIT 1').fetchone()
    if not job_row:
        return {'job': None, 'events': []}
    job = dict(zip(('id', 'source', 'started_at', 'ended_at', 'status'), job_row))
    job_rows = conn.execute(
        'SELECT id,source,started_at,ended_at,status FROM work_jobs '
        "WHERE status='running' OR id=? ORDER BY id DESC", (job['id'],)).fetchall()
    ids = [row[0] for row in job_rows]
    rows = conn.execute(
        'SELECT id,job_id,ts,kind,league,match_text,tool,direction,done,total,'
        'markets,pages,status,message FROM work_log WHERE job_id IN (%s) ORDER BY id'
        % ','.join('?' * len(ids)), ids).fetchall()
    keys = ('id', 'job_id', 'ts', 'kind', 'league', 'match_text', 'tool', 'direction',
            'done', 'total', 'markets', 'pages', 'status', 'message')
    events = []
    for row in rows:
        item = dict(zip(keys, row))
        item['markets'] = _json_list(item['markets'])
        item['pages'] = _json_list(item['pages'])
        events.append(item)
    jobs = [dict(zip(('id', 'source', 'started_at', 'ended_at', 'status'), row))
            for row in job_rows]
    sources = {row['id']: row['source'] for row in jobs}
    tools = tool_progress(events, {row['id']: row['status'] for row in jobs})
    for state in tools:
        state['source'] = sources.get(state['job_id'], '')
    return {'job': job, 'jobs': jobs, 'events': events, 'tools': tools}
