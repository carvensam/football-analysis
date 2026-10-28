# -*- coding: utf-8 -*-
import math
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone


DAY_START = (12, 1)


def hk_now_str():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime(
        '%Y-%m-%d %H:%M:%S')


def migrate(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS user_bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER NOT NULL,
        choice TEXT NOT NULL CHECK(choice IN ('up','down')),
        home TEXT NOT NULL,
        away TEXT NOT NULL,
        league TEXT,
        kickoff TEXT,
        line REAL NOT NULL,
        odds REAL NOT NULL,
        stake REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','confirmed')),
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        updated_at TEXT,
        result TEXT,
        profit REAL,
        settled_at TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_user_bets_status '
                 'ON user_bets(status, confirmed_at)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_user_bets_match '
                 'ON user_bets(match_id)')


def _now(value=None):
    if value is None:
        return hk_now_str()
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)[:19]


def _finite(value, name, minimum=None, allow_zero=True):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError('%s格式無效' % name)
    if not math.isfinite(number):
        raise ValueError('%s格式無效' % name)
    if minimum is not None:
        if allow_zero and number < minimum:
            raise ValueError('%s不可少於 %s' % (name, minimum))
        if not allow_zero and number <= minimum:
            raise ValueError('%s必須大於 %s' % (name, minimum))
    return number


def _line_text(value):
    v = float(value)
    if v == 0:
        return '平手'
    names = {0.25: '平/半', 0.5: '半球', 0.75: '半/一', 1: '一球',
             1.25: '一/球半', 1.5: '球半', 1.75: '球半/兩', 2: '兩球',
             2.25: '兩/兩球半', 2.5: '兩球半', 2.75: '兩球半/三', 3: '三球'}
    amount = abs(v)
    text = names.get(amount) or ('%.2f球' % amount).replace('.00球', '球')
    return ('主讓' if v < 0 else '客讓') + text


def _choice_team(choice, line, home, away):
    if choice == 'up':
        return home if line <= 0 else away
    return away if line <= 0 else home


def _split_line(value):
    value = float(value)
    if round(value * 4) % 2 == 0:
        return [value]
    return [math.floor(value * 2) / 2, math.ceil(value * 2) / 2]


def settle_values(choice, line, odds, stake, home_score, away_score):
    if choice not in ('up', 'down'):
        raise ValueError('方向無效')
    odds = _finite(odds, '賠率', 0, False)
    stake = _finite(stake, '本金', 0, False)
    if home_score is None or away_score is None:
        return None, None
    parts = []
    profits = []
    for part_line in _split_line(line):
        margin = home_score - away_score + part_line
        if choice == 'up':
            margin = margin if part_line <= 0 else -margin
        else:
            margin = -margin if part_line <= 0 else margin
        result = 'W' if margin > 0 else ('L' if margin < 0 else 'P')
        part_stake = stake / len(_split_line(line))
        profits.append(part_stake * odds if result == 'W'
                       else (-part_stake if result == 'L' else 0))
        parts.append(result)
    if len(set(parts)) == 1:
        result = parts[0]
    elif set(parts) == {'W', 'P'}:
        result = 'HW'
    elif set(parts) == {'L', 'P'}:
        result = 'HL'
    else:
        result = 'X'
    return result, round(sum(profits), 2)


def cycle_day(value):
    dt = datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S')
    boundary = dt.replace(hour=DAY_START[0], minute=DAY_START[1], second=0,
                          microsecond=0)
    if dt < boundary:
        dt -= timedelta(days=1)
    return dt.strftime('%Y-%m-%d')


def cycle_week(value):
    day = datetime.strptime(cycle_day(value), '%Y-%m-%d')
    day -= timedelta(days=day.weekday())
    return day.strftime('%Y-%m-%d')


def create(conn, match_id, choice, line, odds, stake, now=None):
    if choice not in ('up', 'down'):
        raise ValueError('方向無效')
    line = _finite(line, '盤口')
    odds = _finite(odds, '賠率', 0, False)
    stake = _finite(stake, '本金', 0)
    if abs(line) > 20 or round(line * 4) != line * 4:
        raise ValueError('盤口必須是 0.25 的倍數及不可超過 20 球')
    row = conn.execute(
        'SELECT c.req_name,m.kickoff,ht.name_tc,at.name_tc FROM matches m '
        'JOIN seasons s ON s.id=m.season_id '
        'JOIN competitions c ON c.titan_id=s.titan_id '
        'JOIN teams ht ON ht.titan_id=m.home_id '
        'JOIN teams at ON at.titan_id=m.away_id WHERE m.id=?',
        (match_id,)).fetchone()
    if not row:
        raise ValueError('找不到賽事')
    now = _now(now)
    cur = conn.execute(
        'INSERT INTO user_bets(match_id,choice,home,away,league,kickoff,line,'
        'odds,stake,status,created_at,updated_at) '
        "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
        (match_id, choice, row[2], row[3], row[0], row[1], line, odds, stake,
         now, now))
    conn.commit()
    return {'ok': True, 'id': cur.lastrowid, 'match_id': match_id}


def _bet(conn, bet_id):
    row = conn.execute('SELECT * FROM user_bets WHERE id=?', (bet_id,)).fetchone()
    if not row:
        raise ValueError('找不到投注紀錄')
    cols = [r[1] for r in conn.execute('PRAGMA table_info(user_bets)')]
    return dict(zip(cols, row))


def confirm(conn, bet_id, now=None):
    bet = _bet(conn, bet_id)
    if bet['status'] != 'pending':
        raise ValueError('這場已確認')
    if bet['stake'] <= 0:
        raise ValueError('確認前請先輸入本金')
    now = _now(now)
    conn.execute("UPDATE user_bets SET status='confirmed',confirmed_at=?,"
                 'updated_at=?,result=NULL,profit=NULL,settled_at=NULL WHERE id=?',
                 (now, now, bet_id))
    conn.commit()
    settle(conn)
    return {'ok': True, 'id': bet_id, 'confirmed_at': now}


def edit(conn, bet_id, home, away, line, odds, stake, now=None, choice=None):
    bet = _bet(conn, bet_id)
    home = str(home or '').strip()
    away = str(away or '').strip()
    if not home or not away:
        raise ValueError('主隊及客隊名稱不可留空')
    choice = choice or bet['choice']
    if choice not in ('up', 'down'):
        raise ValueError('方向無效')
    line = _finite(line, '盤口')
    odds = _finite(odds, '賠率', 0, False)
    stake = _finite(stake, '本金', 0)
    if abs(line) > 20 or round(line * 4) != line * 4:
        raise ValueError('盤口必須是 0.25 的倍數及不可超過 20 球')
    if bet['status'] == 'confirmed' and stake <= 0:
        raise ValueError('已確認投注的本金必須大於 0')
    now = _now(now)
    conn.execute('UPDATE user_bets SET home=?,away=?,choice=?,line=?,odds=?,stake=?,'
                 'updated_at=?,result=NULL,profit=NULL,settled_at=NULL WHERE id=?',
                 (home, away, choice, line, odds, stake, now, bet_id))
    conn.commit()
    if bet['status'] == 'confirmed':
        settle(conn)
    return {'ok': True, 'id': bet_id}


def settle(conn):
    rows = conn.execute(
        'SELECT b.id,b.choice,b.line,b.odds,b.stake,m.home_score,m.away_score '
        'FROM user_bets b JOIN matches m ON m.id=b.match_id '
        "WHERE b.status='confirmed' AND m.home_score IS NOT NULL "
        'AND m.away_score IS NOT NULL').fetchall()
    changed = 0
    now = _now()
    for bet_id, choice, line, odds, stake, hs, aws in rows:
        result, profit = settle_values(choice, line, odds, stake, hs, aws)
        cur = conn.execute('UPDATE user_bets SET result=?,profit=?,settled_at=? '
                           'WHERE id=? AND (result IS NOT ? OR profit IS NOT ? '
                           'OR settled_at IS NULL)',
                           (result, profit, now, bet_id, result, profit))
        changed += cur.rowcount
    conn.commit()
    return changed


def delete_pending(conn, bet_id=None, match_id=None):
    if bet_id is not None:
        sql = "DELETE FROM user_bets WHERE id=? AND status='pending'"
        args = (bet_id,)
    else:
        sql = "DELETE FROM user_bets WHERE match_id=? AND status='pending'"
        args = (match_id,)
    conn.execute('PRAGMA busy_timeout=5000')
    for attempt in range(4):
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return {'ok': True, 'deleted': cur.rowcount}
        except sqlite3.OperationalError as exc:
            conn.rollback()
            locked = 'locked' in str(exc).lower() or 'busy' in str(exc).lower()
            if not locked or attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))


def delete_period(conn, period, key):
    if period not in ('day', 'week'):
        raise ValueError('刪除範圍無效')
    datetime.strptime(key, '%Y-%m-%d')
    cycle = cycle_day if period == 'day' else cycle_week
    ids = [r[0] for r in conn.execute(
        "SELECT id,confirmed_at FROM user_bets WHERE status='confirmed'")
        if cycle(r[1]) == key]
    if ids:
        conn.execute('DELETE FROM user_bets WHERE id IN (%s)' %
                     ','.join('?' * len(ids)), ids)
    conn.commit()
    return {'ok': True, 'deleted': len(ids), 'period': period, 'key': key}


def _record(row):
    (bet_id, mid, choice, home, away, league, ko, line, odds, stake, status,
     created, confirmed, updated, result, profit, settled, hs, aws) = row
    return {'id': mid, 'bet_id': bet_id, 'choice': choice, 'home': home,
            'away': away, 'league': league, 'kickoff': ko, 'line': line,
            'line_text': _line_text(line), 'choice_team': _choice_team(
                choice, line, home, away),
            'odds': odds, 'stake': stake, 'status': status,
            'created_at': created, 'confirmed_at': confirmed, 'updated_at': updated,
            'result': result, 'profit': profit, 'settled_at': settled,
            'played': hs is not None and aws is not None,
            'score': None if hs is None or aws is None else '%s-%s' % (hs, aws)}


def _rows(conn, status=None):
    sql = ('SELECT b.id,b.match_id,b.choice,b.home,b.away,b.league,b.kickoff,'
           'b.line,b.odds,b.stake,b.status,b.created_at,b.confirmed_at,'
           'b.updated_at,b.result,b.profit,b.settled_at,m.home_score,m.away_score '
           'FROM user_bets b JOIN matches m ON m.id=b.match_id')
    args = ()
    if status:
        sql += ' WHERE b.status=?'
        args = (status,)
    if status == 'pending':
        sql += ' ORDER BY b.kickoff,b.id'
    elif status == 'confirmed':
        sql += ' ORDER BY b.confirmed_at DESC,b.id DESC'
    else:
        sql += " ORDER BY CASE b.status WHEN 'pending' THEN 0 ELSE 1 END,b.id"
    return [_record(r) for r in conn.execute(sql, args)]


def summary(conn):
    pending = _rows(conn, 'pending')
    return {'stats': {'total': len(pending), 'pending': len(pending),
                      'wins': 0, 'losses': 0, 'pushes': 0, 'win_rate': None},
            'picks': pending}


def full(conn):
    settle(conn)
    pending = _rows(conn, 'pending')
    confirmed = _rows(conn, 'confirmed')
    days = {}
    weeks = {}
    for rec in confirmed:
        day = cycle_day(rec['confirmed_at'])
        week = cycle_week(rec['confirmed_at'])
        rec['day'] = day
        rec['week'] = week
        for key, bucket in ((day, days), (week, weeks)):
            item = bucket.setdefault(key, {'n': 0, 'profit': 0.0,
                                          'pending_settlement': 0})
            item['n'] += 1
            if rec['profit'] is None:
                item['pending_settlement'] += 1
            else:
                item['profit'] = round(item['profit'] + rec['profit'], 2)
    day_list = [dict(day=key, **value) for key, value in days.items()]
    week_list = [dict(week=key, **value) for key, value in weeks.items()]
    day_list.sort(key=lambda x: x['day'], reverse=True)
    week_list.sort(key=lambda x: x['week'], reverse=True)
    known = [r['profit'] for r in confirmed if r['profit'] is not None]
    stats = {'total': len(pending) + len(confirmed), 'pending': len(pending),
             'confirmed': len(confirmed),
             'wins': sum(r['result'] == 'W' for r in confirmed),
             'losses': sum(r['result'] == 'L' for r in confirmed),
             'pushes': sum(r['result'] == 'P' for r in confirmed),
             'half_wins': sum(r['result'] == 'HW' for r in confirmed),
             'half_losses': sum(r['result'] == 'HL' for r in confirmed),
             'profit': round(sum(known), 2)}
    return {'stats': stats, 'pending': pending, 'confirmed': confirmed,
            'days': day_list, 'weeks': week_list}
