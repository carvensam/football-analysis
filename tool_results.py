import datetime as dt
import sqlite3

try:
    from . import home_tools
except ImportError:
    import home_tools


_ALIASES = {key.lower(): key for key in home_tools._DEF_BY_KEY}
_ALIASES.update({
    'w': 'W', 'g7': 'F7', 'g8': 'F8', 'g12': 'F12', 'g8s': 'F8S',
    'grid8s': 'F8S', 'fw8s': 'S8', '8s': 'S8', '88w': 'W88',
    'jx': 'JX', 'hb': 'HB', 'k3': 'K3', 'ou': 'OU', 'htft': 'HTFT',
})
_RESULT = {'W': 'W', 'L': 'L', 'P': 'P', '贏': 'W', '輸': 'L', '走': 'P'}


def _result(value):
    if value is None:
        return None
    return _RESULT.get(str(value).strip(), str(value).strip().upper())


def _month_range(month):
    if month == 'all':
        return None, None
    try:
        year, mon = (int(x) for x in month.split('-'))
        if not 1 <= mon <= 12:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError('月份格式必須是 YYYY-MM')
    first = dt.date(year, mon, 1)
    last = dt.date(year + (mon == 12), mon % 12 + 1, 1)
    return first.isoformat(), last.isoformat()


def summarize(rows):
    selected = len(rows)
    ids = {row.get('id', row.get('match_id')) for row in rows}
    counts = {'W': 0, 'L': 0, 'P': 0}
    pending = 0
    for row in rows:
        result = _result(row.get('result', row.get('settle')))
        if result in counts:
            counts[result] += 1
        else:
            pending += 1
    w, l, p = counts['W'], counts['L'], counts['P']
    settled = w + l + p
    return {
        'selected': selected, 'match_count': len(ids),
        'hit': w, 'miss': l, 'push': p, 'pending': pending,
        'settled': settled,
        'hit_rate': round((w + 0.5 * p) / settled * 100, 1) if settled else None,
    }


def _spec(tool):
    key = _ALIASES.get(str(tool).strip().lower())
    if key in ('OU', 'HTFT'):
        return key, key
    for spec in home_tools._SPECS:
        if spec['key'] == key:
            return key, spec
    raise ValueError('未知工具：%s' % tool)


def _empty(key, name, month, offset, limit, missing=None):
    return {
        'ok': True, 'tool': key, 'name': name, 'month': month,
        'stats': summarize([]), 'rows': [],
        'page': {'offset': offset, 'limit': limit,
                 'next_offset': offset, 'has_more': False},
        'missing_source': missing,
    }


def _table_page(conn, key, spec, month, offset, limit):
    start, end = _month_range(month)
    join = spec.get('join') or ''
    base = ('FROM %s t ' % spec['table'] + join +
            ' JOIN matches m ON m.id=t.match_id '
            'JOIN seasons s ON s.id=m.season_id '
            'JOIN competitions c ON c.titan_id=s.titan_id '
            'JOIN teams ht ON ht.titan_id=m.home_id '
            'JOIN teams at ON at.titan_id=m.away_id ' +
            ('WHERE 1=1 ' if start is None
             else 'WHERE m.kickoff >= ? AND m.kickoff < ? '))
    if spec.get('where'):
        base += 'AND ' + spec['where'] + ' '
    args = [] if start is None else [start, end]
    if key == 'K3':
        try:
            from . import w88_engine
        except ImportError:
            import w88_engine
        w88_engine.settle_k3(conn)
    try:
        result_rows = conn.execute(
            'SELECT t.result, COUNT(*) ' + base + 'GROUP BY t.result', args).fetchall()
        match_count = conn.execute(
            'SELECT COUNT(DISTINCT t.match_id) ' + base, args).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return _empty(key, home_tools._DEF_BY_KEY[key]['name'], month,
                          offset, limit, spec['table'])
        raise
    stats = summarize([{'result': row[0]} for row in result_rows
                       for _ in range(row[1])])
    stats['match_count'] = match_count
    cursor = conn.execute(
        'SELECT t.match_id AS match_id,%s AS direction,%s AS payload,'
        't.result AS result%s,m.kickoff,m.home_score,m.away_score,'
        'c.req_name AS league,ht.name_tc AS home,at.name_tc AS away ' % (
            spec['direction'], spec['payload'], spec.get('extra', '')) +
        base + 'ORDER BY m.kickoff DESC,t.match_id DESC,t.rowid DESC LIMIT ? OFFSET ?',
        args + [limit + 1, offset])
    names = [column[0] for column in cursor.description]
    rows = cursor.fetchall()
    extra_names = ['kickoff', 'home_score', 'away_score', 'league', 'home', 'away']
    out = []
    for values in rows[:limit]:
        row = dict(zip(names + extra_names, values))
        selection = home_tools._selection(spec, row)
        selection.pop('_signature', None)
        selection.update({
            'id': row['match_id'], 'kickoff': row['kickoff'],
            'league': row['league'], 'home': row['home'], 'away': row['away'],
            'score': ('%s-%s' % (row['home_score'], row['away_score'])
                      if row['home_score'] is not None else None),
            'source': spec['table'],
        })
        out.append(selection)
    return {
        'ok': True, 'tool': key, 'name': home_tools._DEF_BY_KEY[key]['name'],
        'month': month, 'stats': stats, 'rows': out,
        'page': {'offset': offset, 'limit': limit,
                 'next_offset': offset + len(out), 'has_more': len(rows) > limit},
    }


def _dynamic_engine(key):
    try:
        if key == 'OU':
            from . import ou_rules_engine as engine
        else:
            from . import htft_rules_engine as engine
    except ImportError:
        if key == 'OU':
            import ou_rules_engine as engine
        else:
            import htft_rules_engine as engine
    return engine


def _dynamic_scan(conn, key, start, end, include_upcoming, progress=None):
    engine = _dynamic_engine(key)
    previous_db = engine.DB
    try:
        engine.DB = home_tools._db_path(conn)
        data = engine.scan(hours=None if include_upcoming else 0,
                           played_from=start or '0001-01-01',
                           played_to=end or '9999-12-31',
                           include_upcoming=include_upcoming,
                           progress=progress)
    finally:
        engine.DB = previous_db
    return engine, data


def _dynamic_card(engine, card):
    home_score = card.get('home_score')
    away_score = card.get('away_score')
    return {
        'id': card.get('id', card.get('match_id')),
        'kickoff': card.get('kickoff', card.get('ko')),
        'league': card.get('league', card.get('lg')),
        'home': card.get('home', card.get('h')),
        'away': card.get('away', card.get('a')),
        'score': card.get('score') or (
            '%s-%s' % (home_score, away_score)
            if home_score is not None else None),
        'direction': (card.get('direction') or card.get('side') or
                      card.get('target_name') or card.get('target')),
        'filter': (card.get('rule_text') or card.get('cond_text') or
                   card.get('method') or card.get('label') or
                   card.get('tier') or card.get('line')),
        'result': _result(card.get('result', card.get('settle'))),
        'source': engine.__name__.rsplit('.', 1)[-1],
    }


def _dynamic_cards(conn, key, start, end, include_upcoming):
    engine, data = _dynamic_scan(conn, key, start, end, include_upcoming)
    cards = [_dynamic_card(engine, card) for card in data['played']]
    if include_upcoming:
        cards.extend(_dynamic_card(engine, card)
                     for card in data.get('upcoming') or [])
    cards.sort(key=lambda row: (row['kickoff'] or '', row['id'] or 0), reverse=True)
    return data, cards


def dynamic_home_cards(conn, key):
    return _dynamic_cards(conn, key, None, None, True)[1]


def dynamic_recompute(conn, key, progress=None):
    return _dynamic_scan(conn, key, None, None, True, progress)[0]


def dynamic_league_report(conn, key, now=None):
    key = _spec(key)[0]
    data, cards = _dynamic_cards(conn, key, '2026-01-01', '2027-01-01', False)
    current = (now or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8))).replace(tzinfo=None)
    cutoff = current - dt.timedelta(days=7)
    groups = {}
    for card in cards:
        kickoff = str(card.get('kickoff') or '')
        result = _result(card.get('result'))
        if result not in ('W', 'L', 'P') or not kickoff:
            continue
        ko = dt.datetime.strptime(kickoff[:19], '%Y-%m-%d %H:%M:%S') \
            if len(kickoff) >= 19 else dt.datetime.strptime(kickoff[:16], '%Y-%m-%d %H:%M')
        if not (dt.datetime(2026, 1, 1) <= ko <= current):
            continue
        league = card.get('league') or '（未知）'
        bucket = groups.setdefault(league, {
            'n': 0, 'w': 0, 'l': 0, 'p': 0,
            'n7': 0, 'w7': 0, 'l7': 0, 'p7': 0,
        })
        bucket['n'] += 1
        bucket[result.lower()] += 1
        if ko >= cutoff:
            bucket['n7'] += 1
            bucket[result.lower() + '7'] += 1
    rows = []
    for league, bucket in groups.items():
        row = {'league': league, **bucket}
        row['hit'] = round((bucket['w'] + 0.5 * bucket['p']) / bucket['n'] * 100, 1)
        row['hit7'] = (round((bucket['w7'] + 0.5 * bucket['p7']) / bucket['n7'] * 100, 1)
                       if bucket['n7'] else None)
        if not bucket['n7']:
            row['n7'] = row['w7'] = row['l7'] = row['p7'] = None
        row['warn'] = row['hit'] < 60 and row['hit7'] is not None and row['hit7'] < 60
        rows.append(row)
    rows.sort(key=lambda row: (row['hit7'] is None, -(row['hit7'] or 0),
                               -row['hit'], row['league']))
    return {'ok': True, 'tool': key.lower(), 'rows': rows,
            'errors': data['stats'].get('errors', 0),
            'scanned': data['stats'].get('scanned', 0),
            'now': current.strftime('%Y-%m-%d %H:%M')}


def _dynamic_page(conn, key, month, offset, limit):
    start, end = _month_range(month)
    name = '大小方法' if key == 'OU' else '半全場方法'
    data, cards = _dynamic_cards(conn, key, start, end, False)
    stats = summarize(cards)
    stats['errors'] = data['stats'].get('errors', 0)
    stats['scanned'] = data['stats'].get('scanned', 0)
    return {
        'ok': True, 'tool': key, 'name': name, 'month': month,
        'stats': stats, 'rows': cards[offset:offset + limit],
        'page': {'offset': offset, 'limit': limit,
                 'next_offset': offset + len(cards[offset:offset + limit]),
                 'has_more': offset + limit < len(cards)},
    }


def result_page(conn, tool, month='2026-10', offset=0, limit=50):
    key, spec = _spec(tool)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 100))
    if key in ('OU', 'HTFT'):
        return _dynamic_page(conn, key, month, offset, limit)
    return _table_page(conn, key, spec, month, offset, limit)
