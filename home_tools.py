# -*- coding: utf-8 -*-
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone


TOOL_DEFS = [
    {'key': 'W', 'name': '精選W', 'page': '精選W頁'},
    {'key': 'V1', 'name': '精V1', 'page': '舊版V1/V2頁'},
    {'key': 'V1Z', 'name': '精V1Z', 'page': '舊版V1/V2頁'},
    {'key': 'V2', 'name': '精V2', 'page': '舊版V1/V2頁'},
    {'key': 'V2Z', 'name': '精V2Z', 'page': '舊版V1/V2頁'},
    {'key': 'F7', 'name': '精選7', 'page': '精選7頁'},
    {'key': 'F8', 'name': '精選8', 'page': '精選8頁'},
    {'key': 'F12', 'name': '精選12', 'page': '精選12頁'},
    {'key': 'F8S', 'name': '格8s', 'page': '精選8s頁'},
    {'key': 'S8', 'name': '精選8s', 'page': '精選8s頁'},
    {'key': 'X8', 'name': '8X', 'page': '8X頁'},
    {'key': 'W88', 'name': '88W', 'page': '88W頁'},
    {'key': 'JX', 'name': '新精選', 'page': '新精選頁'},
    {'key': 'HB', 'name': '好波', 'page': '好波頁'},
    {'key': 'K3', 'name': 'K3（舊紀錄）', 'page': '88W頁'},
    {'key': 'OU', 'name': '大小方法', 'page': '大小方法頁'},
    {'key': 'HTFT', 'name': '半全場方法', 'page': '半全場方法頁'},
]

_SPECS = [
    {'key': 'W', 'table': 'v3_featured', 'direction': 't.direction',
     'payload': 't.detail', 'kind': 'w'},
    {'key': 'V1', 'table': 'v1_featured', 'direction': 't.direction',
     'payload': 'x.letters', 'kind': 'letters',
     'join': 'LEFT JOIN featured x ON x.match_id=t.match_id'},
    {'key': 'V1Z', 'table': 'v1_featured_z', 'direction': 't.direction',
     'payload': 'x.letters', 'kind': 'letters',
     'join': 'LEFT JOIN featured_z x ON x.match_id=t.match_id'},
    {'key': 'V2', 'table': 'featured', 'direction': 't.direction',
     'payload': 't.letters', 'kind': 'letters'},
    {'key': 'V2Z', 'table': 'featured_z', 'direction': 't.direction',
     'payload': 't.letters', 'kind': 'letters'},
    {'key': 'F7', 'table': 'fw_grid_featured', 'direction': 't.direction',
     'payload': 't.detail', 'kind': 'grid', 'where': "t.grid='7'"},
    {'key': 'F8', 'table': 'fw_grid_featured', 'direction': 't.direction',
     'payload': 't.detail', 'kind': 'grid', 'where': "t.grid='8'"},
    {'key': 'F12', 'table': 'fw_grid_featured', 'direction': 't.direction',
     'payload': 't.detail', 'kind': 'grid', 'where': "t.grid='12'"},
    {'key': 'F8S', 'table': 'fw_grid_featured', 'direction': 't.direction',
     'payload': 't.detail', 'kind': 'grid', 'where': "t.grid='8s'"},
    {'key': 'S8', 'table': 'fw8s_log', 'direction': 't.direction',
     'payload': 't.conds', 'kind': 'array'},
    {'key': 'X8', 'table': 'fw8x_log', 'direction': 't.direction',
     'payload': 't.preds', 'kind': 'array'},
    {'key': 'W88', 'table': 'w88_log',
     'direction': "COALESCE(t.label,t.market,'組合')", 'payload': 't.conds',
     'kind': 'w88', 'extra': ',t.market AS market'},
    {'key': 'JX', 'table': 'newjx_log', 'direction': 't.dir',
     'payload': 't.conds', 'kind': 'array'},
    {'key': 'HB', 'table': 'hb_pick_log', 'direction': 't.side',
     'payload': 't.rule', 'kind': 'hb'},
    {'key': 'K3', 'table': 'k3_log', 'direction': "'雙數和波'",
     'payload': 't.conds', 'kind': 'array'},
]
_DEF_BY_KEY = {d['key']: d for d in TOOL_DEFS}
_CACHE = {'ts': 0.0, 'db': None, 'data': None}
_CACHE_TTL = 60
_DIR = {'up': '上盤', 'down': '下盤', 'over': '大球', 'under': '細球'}


def invalidate():
    _CACHE.update(ts=0.0, db=None, data=None)


def _decode(payload):
    if payload is None:
        return None
    if not isinstance(payload, str):
        return payload
    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return payload


def _strings(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(str(x) for x in value if x is not None and str(x))
    if value:
        return [str(value)]
    return []


def _filters(spec, row):
    kind = spec['kind']
    data = _decode(row.get('payload'))
    if kind == 'w':
        conds = data.get('conds') if isinstance(data, dict) else []
        return sorted(str(c.get('note') or c.get('name') or c.get('no') or '')
                      for c in (conds or []) if isinstance(c, dict))
    if kind == 'letters':
        letters = data.get('letters') if isinstance(data, dict) else data
        if isinstance(letters, dict):
            letters = letters.get('letters')
        return _strings(letters)
    if kind == 'grid':
        cells = data.get('cells') if isinstance(data, dict) else []
        return sorted(str(c.get('cell') or c.get('name') or '')
                      for c in (cells or []) if isinstance(c, dict))
    if kind == 'array':
        return _strings(data)
    if kind == 'hb':
        if data is None:
            return []
        return [str(data).split('｜', 1)[0].strip()]
    if kind == 'w88':
        return [str(row.get('market') or '')] + _strings(data)
    return _strings(data)


def _selection(spec, row):
    key = spec['key']
    raw_dir = str(row.get('direction') or '未標方向')
    direction = _DIR.get(raw_dir, raw_dir)
    filters = [x for x in _filters(spec, row) if x]
    signature = json.dumps([key, raw_dir, filters], ensure_ascii=False,
                           sort_keys=True, separators=(',', ':'))
    definition = _DEF_BY_KEY[key]
    return {'key': key, 'name': definition['name'], 'page': definition['page'],
            'direction': direction, 'filter': '＋'.join(filters),
            'result': row.get('result'), '_signature': signature}


def _rows(conn, spec, ids=None, settled=False):
    select = ('SELECT t.match_id AS match_id,%s AS direction,%s AS payload,'
              't.result AS result%s' %
              (spec['direction'], spec['payload'], spec.get('extra', '')))
    if settled:
        select += ',m.kickoff AS kickoff'
    sql = select + ' FROM %s t ' % spec['table'] + (spec.get('join') or '')
    if settled:
        sql += ' JOIN matches m ON m.id=t.match_id '
    where = []
    args = []
    if spec.get('where'):
        where.append(spec['where'])
    if settled:
        where.append("t.result IN ('W','L','P')")
    if ids is not None:
        if not ids:
            return []
        where.append('t.match_id IN (%s)' % ','.join('?' * len(ids)))
        args.extend(ids)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    try:
        cur = conn.execute(sql, args)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return []
        raise


def _empty():
    return {'w': 0, 'l': 0, 'p': 0, 'n': 0, 'hit': None}


def _bump(bucket, result):
    if result == 'W':
        bucket['w'] += 1
    elif result == 'L':
        bucket['l'] += 1
    elif result == 'P':
        bucket['p'] += 1


def _pack(bucket):
    n = bucket['w'] + bucket['l'] + bucket['p']
    return {'w': bucket['w'], 'l': bucket['l'], 'p': bucket['p'], 'n': n,
            'hit': round((bucket['w'] + 0.5 * bucket['p']) / n * 100, 1)
            if n else None}


def _db_path(conn):
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        return row[2] if row else ''
    except sqlite3.Error:
        return ''


def _dynamic_selection(key, card):
    definition = _DEF_BY_KEY[key]
    raw_dir = str(card.get('direction') or '')
    filters = _strings(card.get('filter'))
    return {'key': key, 'name': definition['name'], 'page': definition['page'],
            'direction': _DIR.get(raw_dir, raw_dir),
            'filter': '＋'.join(filters), 'result': card.get('result'),
            '_signature': json.dumps([key, raw_dir, filters], ensure_ascii=False,
                                     sort_keys=True, separators=(',', ':')),
            '_card_id': card.get('id')}


def _build_stats(conn, cutoff, dynamic=None):
    all_buckets = {}
    recent_buckets = {}
    for spec in _SPECS:
        for row in _rows(conn, spec, settled=True):
            selection = _selection(spec, row)
            sig = selection['_signature']
            _bump(all_buckets.setdefault(sig, {'w': 0, 'l': 0, 'p': 0}),
                  row.get('result'))
            if (row.get('kickoff') or '') >= cutoff:
                _bump(recent_buckets.setdefault(sig, {'w': 0, 'l': 0, 'p': 0}),
                      row.get('result'))
    for key, cards in (dynamic or {}).items():
        for card in cards:
            if card.get('result') not in ('W', 'L', 'P'):
                continue
            sig = _dynamic_selection(key, card)['_signature']
            _bump(all_buckets.setdefault(sig, {'w': 0, 'l': 0, 'p': 0}),
                  card['result'])
            if (card.get('kickoff') or '') >= cutoff:
                _bump(recent_buckets.setdefault(sig, {'w': 0, 'l': 0, 'p': 0}),
                      card['result'])
    return {'all': all_buckets, 'recent': recent_buckets}


def _stats(conn, cutoff, use_cache, dynamic=None):
    path = _db_path(conn)
    now = time.time()
    if use_cache and _CACHE['data'] is not None \
            and _CACHE['db'] == path and now - _CACHE['ts'] < _CACHE_TTL:
        return _CACHE['data']
    data = _build_stats(conn, cutoff, dynamic)
    if use_cache:
        _CACHE.update(ts=now, db=path, data=data)
    return data


def attach_home_tools(conn, matches, now=None, dynamic=None):
    ids = sorted({int(m['id']) for m in matches if m.get('id') is not None})
    if now is None:
        now = datetime.now(timezone.utc) + timedelta(hours=8)
        use_cache = True
    else:
        use_cache = False
    cutoff = (now - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S')
    stats = _stats(conn, cutoff, use_cache, dynamic)
    grouped = {mid: [] for mid in ids}
    for spec in _SPECS:
        for start in range(0, len(ids), 300):
            chunk = ids[start:start + 300]
            for row in _rows(conn, spec, ids=chunk):
                selection = _selection(spec, row)
                sig = selection.pop('_signature')
                selection['history'] = _pack(stats['all'].get(
                    sig, {'w': 0, 'l': 0, 'p': 0}))
                selection['recent14'] = _pack(stats['recent'].get(
                    sig, {'w': 0, 'l': 0, 'p': 0}))
                grouped.setdefault(row['match_id'], []).append(selection)
    for key, cards in (dynamic or {}).items():
        for card in cards:
            if not card.get('direction') or not card.get('filter'):
                continue
            mid = card.get('id')
            if mid not in grouped:
                continue
            selection = _dynamic_selection(key, card)
            sig = selection.pop('_signature')
            selection.pop('_card_id', None)
            selection['history'] = _pack(stats['all'].get(
                sig, {'w': 0, 'l': 0, 'p': 0}))
            selection['recent14'] = _pack(stats['recent'].get(
                sig, {'w': 0, 'l': 0, 'p': 0}))
            grouped[mid].append(selection)
    for match in matches:
        match['tool_picks'] = grouped.get(match.get('id'), [])
    return TOOL_DEFS
