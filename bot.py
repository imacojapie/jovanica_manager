"""Single-owner paid-media manager with durable broadcasts. Python standard library only."""
import json
import re
import os
import secrets
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER = int(os.environ.get('OWNER_ID', '0'))
DB = None

class APIError(Exception):
    def __init__(self, message, definite=False):
        super().__init__(message)
        self.definite = definite

def api(method, **data):
    request = urllib.request.Request('https://api.telegram.org/bot' + TOKEN + '/' + method,
        data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not log exception URLs: they contain the token.
        raise APIError('Telegram HTTP ' + str(exc.code), definite=400 <= exc.code < 500) from None
    except (urllib.error.URLError, TimeoutError):
        raise APIError('Network error; delivery may be uncertain') from None
    if not result.get('ok'):
        raise APIError('Telegram rejected request', definite=True)
    return result['result']

def get(key, default=None):
    row = DB.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default

def put(key, value):
    DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))
    DB.commit()

def tell(chat, text):
    result = None
    for start in range(0, len(text), 2000):
        result = api('sendMessage', chat_id=chat, text=text[start:start+2000])
    return result

def connection(cid):
    c = api('getBusinessConnection', business_connection_id=cid)
    if c.get('user', {}).get('id') != OWNER or not c.get('is_enabled'):
        raise ValueError('Connection is disabled or OWNER_ID is not the connected profile.')
    return c

def eligible(c, chat):
    if not c.get('rights', {}).get('can_reply'):
        raise ValueError('Enable reply permission for this bot in Chat Automation.')
    if not 0 <= time.time() - chat['date'] < 86400 - 60:
        raise ValueError('Reply window expired. The recipient must message Jovanica again.')

HELP = ('Jovanica Manager v5 — saved packs + new audiences\n'
        '/newppv — start a fresh photo/video bundle\n'
        'Upload an album or individual photos/videos (max 10), then /done.\n'
        '/caption TEXT — set bundle caption\n/price NUMBER — 1–25000 Stars for the WHOLE bundle\n'
        '/text MESSAGE — create a FREE text-only message\n'
        '/target NUMBER — choose one recipient\n/send — review single send\n'
        '/broadcast — review all eligible recipients\n/confirm CODE — approve\n'
        '/saveoffer NAME — save a NEW pack; existing chats may receive it\n'
        '/savearchive NAME — recycle a pack for FUTURE new contacts only\n'
        '/offers — list saved packs\n/offer NAME — load a pack for reuse\n'
        '/audience all|6|12|24 — unsent contacts, optionally recent arrivals\n'
        '/offerstatus — delivery history for loaded pack\n'
        '/draft — inspect content and item count\n/cancel — discard draft\n'
        '/chats — recipients\n/stats — available counts\n/id — your ID\n'
        '/buyers — saved buyers (excluded from broadcasts)\n/sales — recent purchases\n'
        '/syncsales — import/reconcile bot purchase history\n'
        '/customer ID — purchase history and notes\n/note ID TEXT — save a customer note\n'
        '/hold ID — exclude a chat from broadcasts\n/release ID — remove manual hold (buyers stay excluded)\n'
        '/status — broadcast progress\n/stopbroadcast — stop remaining sends\n'
        'Recipients can send /stop to Jovanica to opt out, /start to resume.')

def media_items(content):
    media = content.get('media', [])
    return [media] if isinstance(media, dict) else media

def validate_content(content):
    if content.get('kind') == 'text':
        if not content.get('text') or len(content['text']) > 4096:
            raise ValueError('Use /text MESSAGE with 1–4096 characters.')
        return
    if content.get('collecting'):
        raise ValueError('Bundle still open. Wait for all uploads, then send /done.')
    if not 1 <= len(media_items(content)) <= 10:
        raise ValueError('Upload 1–10 photos/videos first.')
    if not 1 <= content.get('price', 0) <= 25000:
        raise ValueError('Set /price NUMBER from 1 to 25000 Stars.')
    if len(content.get('caption', '')) > 1024:
        raise ValueError('Caption must be at most 1024 characters.')

def content_description(content):
    if content.get('kind') == 'text':
        return 'FREE TEXT MESSAGE (no paywall)\n' + content.get('text', '')
    return ('PPV bundle: ' + str(len(media_items(content))) + ' item(s)\n'
            'Price: ' + str(content.get('price', 'NOT SET')) +
            ' Stars to unlock the WHOLE bundle per recipient\nCaption: ' + content.get('caption', ''))

def offer_blocked(content, key):
    name = content.get('saved_offer')
    if not name:
        return False
    row = DB.execute('SELECT status FROM offer_delivery WHERE name=? AND chat_id=?', (name, str(key))).fetchone()
    return bool(row and row[0] != 'failed')

def audience_matches(content, key):
    hours = content.get('audience_hours', 0)
    if not hours:
        return True
    row = DB.execute('SELECT first_seen FROM arrivals WHERE chat_id=?', (str(key),)).fetchone()
    return bool(row and row[0] is not None and 0 <= time.time() - row[0] <= hours * 3600)

def delivery_status(content, key, status, message_id=None):
    if content.get('saved_offer'):
        with DB:
            DB.execute('INSERT OR REPLACE INTO offer_delivery VALUES (?,?,?,?,?)',
                (content['saved_offer'], str(key), status, int(time.time()), message_id))

def deliver(content, cid, chat_id):
    validate_content(content)
    if offer_blocked(content, chat_id):
        raise ValueError('This saved offer is already sent, excluded, or uncertain for this recipient. No duplicate sent.')
    payload = 'jm5:' + secrets.token_hex(16)
    # Persist tracking BEFORE contacting Telegram. A crash leaves a blocked sending record.
    with DB:
        if content.get('saved_offer'):
            DB.execute('INSERT OR REPLACE INTO offer_delivery VALUES (?,?,?,?,NULL)',
                (content['saved_offer'], str(chat_id), 'sending', int(time.time())))
        if content.get('kind') != 'text':
            DB.execute('INSERT INTO offers VALUES (?,?,?,?,?)',
                (payload, str(chat_id), content['price'], content.get('caption', ''), int(time.time())))
            if content.get('saved_offer'):
                DB.execute('INSERT INTO offer_links VALUES (?,?)', (payload, content['saved_offer']))
    try:
        if content.get('kind') == 'text':
            result = api('sendMessage', business_connection_id=cid, chat_id=chat_id, text=content['text'])
        else:
            result = api('sendPaidMedia', business_connection_id=cid, chat_id=chat_id, payload=payload,
                         star_count=content['price'], media=media_items(content),
                         caption=content.get('caption', ''), protect_content=True)
    except APIError as exc:
        delivery_status(content, chat_id, 'failed' if exc.definite else 'uncertain')
        raise
    delivery_status(content, chat_id, 'sent', result['message_id'])
    return result


def initialize_database(path):
    global DB
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    dbfile = path / 'manager.sqlite3'
    existing = dbfile.exists()
    DB = sqlite3.connect(dbfile)
    if existing:
        # One backup before the first v5 start, never overwrite an existing backup.
        backup = path / 'manager-before-v5.sqlite3'
        if not backup.exists():
            with sqlite3.connect(backup) as dest:
                DB.backup(dest)
    DB.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    DB.commit()
    DB.executescript("""
        CREATE TABLE IF NOT EXISTS saved_offers (
            name TEXT PRIMARY KEY, content TEXT NOT NULL, created INTEGER, archive INTEGER);
        CREATE TABLE IF NOT EXISTS offer_delivery (
            name TEXT, chat_id TEXT, status TEXT, updated INTEGER, message_id INTEGER,
            PRIMARY KEY(name,chat_id));
        CREATE TABLE IF NOT EXISTS offer_links (payload TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS arrivals (chat_id TEXT PRIMARY KEY, first_seen INTEGER);
        CREATE TABLE IF NOT EXISTS offers (
            payload TEXT PRIMARY KEY, chat_id TEXT, price INTEGER, caption TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS purchases (
            receipt TEXT PRIMARY KEY, user_id TEXT, name TEXT, username TEXT, payload TEXT, date INTEGER);
        CREATE TABLE IF NOT EXISTS star_ledger (
            txid TEXT, direction TEXT, user_id TEXT, name TEXT, username TEXT,
            amount INTEGER, date INTEGER, payload TEXT,
            PRIMARY KEY(txid, direction));
        CREATE TABLE IF NOT EXISTS customer_notes (
            user_id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', held INTEGER NOT NULL DEFAULT 0);
    """)
    DB.commit()
    # Existing chat arrival times are unknown; never invent a recent arrival date.
    with DB:
        DB.executemany('INSERT OR IGNORE INTO arrivals VALUES (?,NULL)',
                       [(key,) for key in get('chats', {})])
    # Contacts, offsets, drafts and jobs stay in the existing state table.


def active(chat):
    return (chat.get('id') != OWNER and not chat.get('opted_out', False)
            and 0 <= time.time() - chat['date'] < 86340)

def buyer_ids():
    return {r[0] for r in DB.execute("SELECT user_id FROM purchases UNION SELECT user_id FROM star_ledger WHERE direction='in'")}

def held_ids():
    return {r[0] for r in DB.execute('SELECT user_id FROM customer_notes WHERE held=1')}

def protected(key):
    return str(key) in buyer_ids() | held_ids()

def available_chats():
    connections, available, unknown = {}, [], 0
    for key, chat in get('chats', {}).items():
        if not active(chat):
            continue
        cid = chat['connection']
        if cid not in connections:
            try:
                connections[cid] = connection(cid)
            except ValueError:
                connections[cid] = {}
            except APIError:
                connections[cid] = None
        c = connections[cid]
        if c is None:
            unknown += 1
        elif c.get('rights', {}).get('can_reply'):
            available.append(key)
    return available, unknown

def sales_ready():
    return (bool(get('sales_complete', 0)) and time.time() - get('sales_complete', 0) < 120
            and not get('sales_scan', {}).get('running'))

def receipt(user, payload, date):
    # One payload per sent PPV, one record per buyer; events and ledger sync deduplicate.
    key = payload + ':' + str(user['id'])
    with DB:
        cursor = DB.execute('INSERT OR IGNORE INTO purchases VALUES (?,?,?,?,?,?)',
            (key, str(user['id']), user.get('first_name', ''), user.get('username', ''), payload, date))
    return bool(cursor.rowcount)

def purchase_event(event):
    payload, user = event['paid_media_payload'], event['from']
    offer = DB.execute('SELECT price,caption FROM offers WHERE payload=?', (payload,)).fetchone()
    if not offer:
        return
    if receipt(user, payload, int(time.time())):
        tell(OWNER, 'PPV PURCHASE: ' + user.get('first_name', str(user['id'])) +
             ' (' + str(user['id']) + ') — ' + str(offer[0]) + ' Stars\n' +
             ('@' + user['username'] + '\n' if user.get('username') else '') +
             'Offer: ' + offer[1][:300] + '\nSaved as buyer; excluded from mass sends.\n/customer ' + str(user['id']))

def sync_sales_tick():
    scan = get('sales_scan', {})
    if not scan.get('running'):
        if time.time() - get('sales_complete', 0) < 60:
            return
        scan = {'running': True, 'offset': 0, 'imported': 0}
        put('sales_scan', scan)
    if time.time() < scan.get('retry_at', 0):
        return
    try:
        rows = api('getStarTransactions', offset=scan['offset'], limit=100)['transactions']
    except APIError:
        scan['retry_at'] = time.time() + 30
        put('sales_scan', scan)
        return
    added = 0
    # Store page and its cursor together so restarts cannot lose or duplicate records.
    with DB:
        for tx in rows:
            direction = 'in' if tx.get('source') else 'out'
            partner = tx.get('source') or tx.get('receiver', {})
            if partner.get('type') != 'user' or partner.get('transaction_type') != 'paid_media_payment':
                continue
            user = partner['user']
            payload = partner.get('paid_media_payload', '')
            cur = DB.execute('INSERT OR IGNORE INTO star_ledger VALUES (?,?,?,?,?,?,?,?)',
                (tx['id'], direction, str(user['id']), user.get('first_name', ''), user.get('username', ''),
                 abs(tx['amount']), tx['date'], payload))
            added += cur.rowcount
            if direction == 'in' and payload:
                DB.execute('INSERT OR IGNORE INTO purchases VALUES (?,?,?,?,?,?)',
                    (payload + ':' + str(user['id']), str(user['id']), user.get('first_name', ''),
                     user.get('username', ''), payload, tx['date']))
        scan.update(offset=scan['offset'] + len(rows), imported=scan['imported'] + added)
        complete = len(rows) < 100
        if complete:
            scan['running'] = False
            DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('sales_complete', json.dumps(time.time())))
        DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('sales_scan', json.dumps(scan)))
    if complete and scan.get('notify'):
        tell(OWNER, 'Sales sync complete. ' + str(scan['imported']) +
             ' new transaction records; ' + str(len(buyer_ids())) + ' saved buyers. /buyers or /sales.')

def identity(key):
    row = DB.execute("SELECT name,username FROM star_ledger WHERE user_id=? ORDER BY date DESC LIMIT 1", (key,)).fetchone()
    if not row:
        row = DB.execute('SELECT name,username FROM purchases WHERE user_id=? ORDER BY date DESC LIMIT 1', (key,)).fetchone()
    chat = get('chats', {}).get(key, {})
    name, username = row if row else (chat.get('name', key), chat.get('username', ''))
    return name + (' @' + username if username else '') + ' — ' + key

def buyer_summary(key):
    count, gross = DB.execute("SELECT COUNT(*),COALESCE(SUM(amount),0) FROM star_ledger WHERE user_id=? AND direction='in'", (key,)).fetchone()
    refunds = DB.execute("SELECT COALESCE(SUM(amount),0) FROM star_ledger WHERE user_id=? AND direction='out'", (key,)).fetchone()[0]
    return identity(key) + ' — ' + str(count) + ' synced purchases, ' + str(gross) + ' Stars received, ' + str(refunds) + ' refunded'

def sales_report(key=None):
    clause, args = (' WHERE user_id=?', (key,)) if key else ('', ())
    rows = DB.execute('SELECT user_id,amount,date,payload,direction FROM star_ledger' + clause + ' ORDER BY date DESC LIMIT 20', args).fetchall()
    lines = []
    for uid, amount, date, payload, direction in rows:
        offer = DB.execute('SELECT caption FROM offers WHERE payload=?', (payload,)).fetchone()
        title = offer[0][:100] if offer and offer[0] else ('Tracked PPV' if offer else 'Older/unlabelled PPV')
        lines.append(time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(date)) + ' | ' + identity(uid) +
                     ' | ' + ('REFUND ' if direction == 'out' else '') + str(amount) + ' Stars | ' + title)
    last = get('sales_complete', 0)
    return ('Latest 20 synced transactions (received Stars, not withdrawable balance).\n' +
            ('\n'.join(lines) or 'No synced PPV transactions yet.') +
            '\nLast complete sync: ' + (time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(last)) if last else 'not yet complete') +
            '\nPurchase alerts can appear before the transaction sync. /syncsales to refresh.')


def summary(job):
    rows = job.get('recipients', [])
    counts = {k: sum(r['state'] == k for r in rows) for k in
              ('pending', 'sending', 'sent', 'skipped', 'failed', 'uncertain', 'cancelled')}
    return ('Broadcast ' + job['id'] + ' — ' + job['state'] + '\n' +
            '\n'.join(k + ': ' + str(v) for k, v in counts.items()) +
            '\nSent means Telegram accepted delivery, not purchased or read.')

def broadcast_tick():
    job = get('job')
    if not job or job['state'] != 'running':
        return
    # Buyers and manual holds are always excluded, including from older queued jobs.
    if not sales_ready():
        return
    # Any leftover sending state has an uncertain outcome after a crash/error.
    for r in job['recipients']:
        if r['state'] == 'sending':
            r['state'] = 'uncertain'
    if time.time() < job.get('next_at', 0):
        put('job', job)
        return
    row = next((r for r in job['recipients'] if r['state'] == 'pending'), None)
    if row is None:
        job['state'] = 'complete'
        put('job', job)
        tell(OWNER, summary(job))
        return
    chat = get('chats', {}).get(row['key'])
    if (not chat or not active(chat) or protected(row['key']) or chat['connection'] != row['connection']
            or offer_blocked(job, row['key']) or not audience_matches(job, row['key'])):
        row['state'] = 'skipped'
        put('job', job)
        return
    try:
        eligible(connection(chat['connection']), chat)
    except ValueError:
        row['state'] = 'skipped'
        put('job', job)
        return
    # Commit before the side effect. On restart this becomes uncertain, never resent.
    row['state'] = 'sending'
    job['next_at'] = time.time() + 1.1
    put('job', job)
    try:
        result = deliver(job, row['connection'], chat['id'])
        row['state'] = 'sent'
        row['message_id'] = result['message_id']
    except APIError as exc:
        row['state'] = 'failed' if exc.definite else 'uncertain'
        # No automatic retry. Slow down after errors, including rate limiting.
        job['next_at'] = time.time() + 30
    put('job', job)


def handle(u):
    if 'purchased_paid_media' in u:
        purchase_event(u['purchased_paid_media'])
        return
    if 'business_connection' in u:
        c = u['business_connection']
        if c.get('user', {}).get('id') == OWNER:
            put('connection', c['id'])
        return
    if 'business_message' in u:
        m = u['business_message']
        if not OWNER or m.get('from', {}).get('id') == OWNER or m.get('from', {}).get('is_bot'):
            return
        cid = m['business_connection_id']
        connection(cid)
        chats = get('chats', {})
        key = str(m['chat']['id'])
        with DB:
            DB.execute('INSERT OR IGNORE INTO arrivals VALUES (?,?)', (key, m['date']))
        chats[key] = {'id': m['chat']['id'], 'name': m['chat'].get('first_name', key),
                      'date': m['date'], 'connection': cid, 'username': m['chat'].get('username', ''),
                      'opted_out': chats.get(key, {}).get('opted_out', False)}
        command = m.get('text', '').strip().lower()
        if command == '/stop':
            chats[key]['opted_out'] = True
        elif command == '/start':
            chats[key]['opted_out'] = False
        put('chats', chats)
        return
    m = u.get('message', {})
    if m.get('chat', {}).get('type') != 'private':
        return
    uid = m.get('from', {}).get('id')
    text = m.get('text', '').strip()
    if text == '/id':
        tell(uid, 'Your Telegram ID: ' + str(uid))
        return
    if not OWNER or uid != OWNER:
        if text.startswith('/start'):
            tell(uid, 'Private manager. Send /id to see your own ID.')
        return
    try:
        process(uid, m, text)
    except (ValueError, KeyError) as exc:
        tell(uid, str(exc))

def offer_description(content):
    if not content.get('saved_offer'):
        return ''
    hours = content.get('audience_hours', 0)
    return ('Saved pack: ' + content['saved_offer'] +
            '\nAudience: not previously sent/excluded/uncertain for this pack' +
            ('; first seen within ' + str(hours) + ' hours' if hours else '; any arrival time') +
            '\nBuyers, holds and opted-out/expired chats remain excluded from broadcasts.\n')

def offer_command(uid, draft, text):
    command, _, argument = text.partition(' ')
    if command not in ('/saveoffer', '/savearchive', '/offers', '/offer', '/audience', '/offerstatus'):
        return False
    if command in ('/saveoffer', '/savearchive', '/offer'):
        name = argument.strip().lower()
        if not re.fullmatch(r'[a-z0-9_-]{1,40}', name):
            raise ValueError('Use a short name with letters/numbers/underscores, e.g. welcome.')
    if command in ('/saveoffer', '/savearchive'):
        validate_content(draft)
        if draft.get('saved_offer'):
            raise ValueError('Already a saved pack. Use /offer NAME to reuse it. /newppv starts different content.')
        if DB.execute('SELECT 1 FROM saved_offers WHERE name=?', (name,)).fetchone():
            raise ValueError('That name already exists. Use /offer ' + name + ' to reuse its delivery history.')
        content = {k: draft[k] for k in ('kind', 'media', 'caption', 'price', 'text') if k in draft}
        archive = command == '/savearchive'
        with DB:
            DB.execute('INSERT INTO saved_offers VALUES (?,?,?,?)',
                       (name, json.dumps(content), int(time.time()), int(archive)))
            if archive:
                DB.executemany('INSERT INTO offer_delivery VALUES (?,?,?,?,NULL)',
                    [(name, key, 'excluded', int(time.time())) for key in get('chats', {})])
            loaded = dict(content, saved_offer=name, audience_hours=0)
            DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('draft', json.dumps(loaded)))
        tell(uid, 'Saved and loaded: ' + name + '. Nothing sent.\n' +
             ('Existing ' + str(len(get('chats', {}))) + ' chats excluded for this pack. Future newcomers can receive it.' if archive else
              'Existing contacts may receive this pack. Earlier untracked deliveries cannot be detected; use this mode for new content.') +
             '\n/broadcast to preview. Later: /offer ' + name + ' then /broadcast. Do not re-save the same pack under new names.')
    elif command == '/offers':
        rows = DB.execute('SELECT name,content,archive FROM saved_offers ORDER BY created,name').fetchall()
        lines = []
        for name, raw, archive in rows:
            content = json.loads(raw)
            sent = DB.execute("SELECT COUNT(*) FROM offer_delivery WHERE name=? AND status='sent'", (name,)).fetchone()[0]
            lines.append(name + ' — ' + ('free text' if content.get('kind') == 'text' else str(content.get('price')) + ' Stars') +
                         ' — ' + str(sent) + ' delivered' + (' — old audience excluded' if archive else ''))
        tell(uid, '\n'.join(lines) or 'No saved packs. Prepare content, then /saveoffer NAME (new content) or /savearchive NAME (recycled content).')
    elif command == '/offer':
        row = DB.execute('SELECT content FROM saved_offers WHERE name=?', (name,)).fetchone()
        if not row:
            raise ValueError('Unknown pack. /offers lists saved names.')
        loaded = dict(json.loads(row[0]), saved_offer=name, audience_hours=0)
        put('draft', loaded)
        tell(uid, offer_description(loaded) + content_description(loaded) + '\n/broadcast to preview. Nothing sent.')
    elif command == '/audience':
        if not draft.get('saved_offer'):
            raise ValueError('Load a pack first: /offer NAME.')
        if argument.strip() not in ('all', '6', '12', '24'):
            raise ValueError('Use /audience all, /audience 6, /audience 12, or /audience 24.')
        draft['audience_hours'] = 0 if argument.strip() == 'all' else int(argument)
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, offer_description(draft) +
             'Arrival times are recorded from v5 onward; older contacts with unknown arrival times are omitted from hourly filters.\n/broadcast to preview.')
    else:
        name = draft.get('saved_offer')
        if not name:
            raise ValueError('Load a pack first: /offer NAME.')
        rows = DB.execute('SELECT status,COUNT(*) FROM offer_delivery WHERE name=? GROUP BY status', (name,)).fetchall()
        keys, unknown = available_chats()
        excluded = buyer_ids() | held_ids()
        count = sum(k not in excluded and not offer_blocked(draft,k) and audience_matches(draft,k) for k in keys)
        buyers = DB.execute("SELECT COUNT(DISTINCT p.user_id) FROM purchases p JOIN offer_links l ON l.payload=p.payload WHERE l.name=?", (name,)).fetchone()[0]
        tell(uid, offer_description(draft) + '\n'.join(status + ': ' + str(n) for status,n in rows) +
             '\nKnown buyers of this pack: ' + str(buyers) + '\nMatching recipients now: ' + str(count) +
             '\nUnavailable permission checks: ' + str(unknown) +
             '\nSending/uncertain records stay excluded to avoid duplicates. Failed deliveries can be included in a later confirmed run.')
    return True


def process(uid, m, text):
    draft = get('draft', {})
    if offer_command(uid, draft, text):
        return
    if text in ('/start', '/help'):
        tell(uid, HELP)
    elif text == '/stats':
        chats = get('chats', {})
        ready, unknown = available_chats()
        buyers, held = buyer_ids(), held_ids()
        bulk = [k for k in ready if k not in buyers | held]
        tell(uid, 'Recorded chats: ' + str(len(chats)) +
             '\nWithin reply window + reply permission: ' + str(len(ready)) +
             '\nKnown buyers (all time): ' + str(len(buyers)) +
             '\nBuyers available for individual follow-up: ' + str(sum(k in buyers for k in ready)) +
             '\nManual holds (all time): ' + str(len(held)) +
             '\nMass-send candidates, excluding buyers/holds: ' + str(len(bulk)) +
             '\nPermission checks unavailable: ' + str(unknown) +
             '\nSales sync: ' + ('current' if sales_ready() else 'pending/stale — broadcasts wait for sync') +
             '\nCounts are a snapshot, not a delivery guarantee. Expired chats remain saved. Telegram contacts are separate.')
    elif text == '/syncsales':
        scan = get('sales_scan', {})
        if scan.get('running'):
            scan['notify'] = True
        else:
            scan = {'running': True, 'offset': 0, 'imported': 0, 'notify': True}
        put('sales_scan', scan)
        tell(uid, 'Importing this bot’s PPV transaction history. Contacts stay saved. I will report when complete.')
    elif text == '/buyers':
        keys = sorted(buyer_ids())
        tell(uid, 'Saved buyers: ' + str(len(keys)) + ' — excluded from ALL broadcasts.\n' +
             ('\n'.join(buyer_summary(k) for k in keys) or 'None recorded yet. /syncsales imports earlier purchases.') +
             '\n/customer ID for notes and purchases. Pin the chat manually in your personal Telegram inbox.')
    elif text == '/sales':
        tell(uid, sales_report())
    elif text.startswith('/customer '):
        key = str(int(text.split()[1]))
        row = DB.execute('SELECT note,held FROM customer_notes WHERE user_id=?', (key,)).fetchone()
        tell(uid, buyer_summary(key) + '\nBuyer: ' + str(key in buyer_ids()) +
             '\nManual hold: ' + str(bool(row and row[1])) + '\nNote: ' + (row[0] if row else '') +
             '\n' + sales_report(key))
    elif text.startswith('/note ') or text.startswith('/hold ') or text.startswith('/release '):
        parts = text.split(maxsplit=2)
        key = str(int(parts[1]))
        if key not in get('chats', {}) and key not in buyer_ids():
            raise ValueError('Use an ID from /chats or /buyers.')
        with DB:
            DB.execute('INSERT OR IGNORE INTO customer_notes(user_id) VALUES (?)', (key,))
            if parts[0] == '/note':
                if len(parts) < 3:
                    raise ValueError('Use /note ID TEXT.')
                DB.execute('UPDATE customer_notes SET note=? WHERE user_id=?', (parts[2][:2000], key))
            else:
                DB.execute('UPDATE customer_notes SET held=? WHERE user_id=?', (int(parts[0] == '/hold'), key))
        tell(uid, 'Saved for ' + identity(key) + '. Buyers remain excluded from broadcasts; /send still supports individual follow-up.')
    elif text == '/draft':
        tell(uid, offer_description(draft) + content_description(draft) + ('\nBundle OPEN — /done when finished.' if draft.get('collecting') else ''))
    elif text == '/newppv':
        fresh = {'kind': 'ppv', 'media': [], 'caption': '', 'collecting': True}
        if 'target' in draft:
            fresh['target'] = draft['target']
        put('draft', fresh)
        tell(uid, 'New PPV bundle. Send up to 10 photos/videos as an album or separately, then /done. Your recipient database is unchanged.')
    elif text == '/done':
        if draft.get('kind') == 'text' or not media_items(draft):
            raise ValueError('Upload photos/videos first, or use /text MESSAGE.')
        draft['collecting'] = False
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Bundle closed: ' + str(len(media_items(draft))) + ' item(s). /caption TEXT, /price NUMBER, then /send or /broadcast.')
    elif text == '/text' or text.startswith('/text ') or text.startswith('/text\n'):
        body = text[5:].lstrip()
        if not body or len(body) > 4096:
            raise ValueError('Send /text followed by your message, up to 4096 characters. It will be FREE, with no photo or paywall.')
        fresh = {'kind': 'text', 'text': body}
        if 'target' in draft:
            fresh['target'] = draft['target']
        put('draft', fresh)
        tell(uid, 'Free text draft saved. /send for one recipient or /broadcast for all eligible chats. Nothing sent yet.')
    elif text == '/chats':
        chats = get('chats', {})
        lines = [str(c['id']) + ' — ' + c['name'] + (' — opted out' if c.get('opted_out') else ' — active' if active(c) else ' — expired') for c in chats.values()]
        tell(uid, '\n'.join(lines) or 'No chats recorded. Now send Hi from your second account to Jovanica.')
    elif text.startswith('/target '):
        key = str(int(text.split()[1]))
        if key not in get('chats', {}):
            raise ValueError('Use an ID listed by /chats.')
        draft['target'] = key
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Recipient selected. Prepare your content if needed, then /send to review.')
    elif m.get('photo') or m.get('video'):
        if draft.get('saved_offer'):
            raise ValueError('Saved packs keep their media. Use /newppv to create a different pack.')
        item = {'type': 'photo', 'media': m['photo'][-1]['file_id']} if m.get('photo') else {'type': 'video', 'media': m['video']['file_id']}
        group = m.get('media_group_id')
        same_group = bool(group and group == draft.get('album_group'))
        append = (draft.get('kind') != 'text' and (draft.get('collecting') or same_group))
        if not append:
            fresh = {'kind': 'ppv', 'media': [], 'caption': ''}
            if 'target' in draft:
                fresh['target'] = draft['target']
            draft = fresh
        items = media_items(draft)
        message_id = m.get('message_id')
        seen = draft.get('media_message_ids', [])
        if message_id is not None and message_id in seen:
            return
        if len(items) >= 10:
            draft.pop('confirm', None)
            draft['collecting'] = True
            put('draft', draft)
            raise ValueError('10-item limit reached. Extra item NOT added. Check /draft, then /done to approve the existing 10, or /newppv to start over.')
        caption = m.get('caption', '')
        if len(caption) > 1024:
            raise ValueError('Caption must be at most 1024 characters.')
        items.append(item)
        if message_id is not None:
            seen.append(message_id)
        draft.update(kind='ppv', media=items, media_message_ids=seen)
        if caption:
            draft['caption'] = caption
        if group:
            draft['album_group'] = group
            draft['collecting'] = True
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Saved ' + str(len(items)) + ' item(s).' +
             (' Upload remaining items, then /done.' if draft.get('collecting') else ' Set /price NUMBER, then /send or /broadcast.'))
    elif text.startswith('/price '):
        if draft.get('kind') == 'text':
            raise ValueError('Text-only messages are free. Use /newppv for a paid photo/video bundle.')
        price = int(text.split()[1])
        if not 1 <= price <= 25000:
            raise ValueError('Price must be 1–25000 Stars.')
        draft['price'] = price
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Price: ' + str(price) + ' Stars. /send for one recipient or /broadcast for all eligible chats.')
    elif text == '/caption' or text.startswith('/caption '):
        if draft.get('kind') == 'text':
            raise ValueError('Use /text MESSAGE to replace the text draft.')
        caption = text.partition(' ')[2]
        if len(caption) > 1024:
            raise ValueError('Caption must be at most 1024 characters.')
        draft['caption'] = caption
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Caption saved. /broadcast to review recipients and price.')
    elif text == '/status':
        job = get('job')
        tell(uid, summary(job) if job else 'No broadcast has been started.')
    elif text == '/stopbroadcast':
        job = get('job')
        if not job or job['state'] != 'running':
            raise ValueError('No broadcast is running.')
        for row in job['recipients']:
            if row['state'] == 'pending':
                row['state'] = 'cancelled'
            elif row['state'] == 'sending':
                row['state'] = 'uncertain'
        job['state'] = 'stopped'
        put('job', job)
        tell(uid, summary(job) + '\nAlready sent posts remain in recipient chats.')
    elif text == '/broadcast':
        if not sales_ready():
            raise ValueError('Buyer history is syncing or stale. Wait a moment, then /broadcast again. /syncsales to request a sync report.')
        if get('job', {}).get('state') == 'running':
            raise ValueError('A broadcast is running. Use /status or /stopbroadcast.')
        validate_content(draft)
        candidates = []
        connections = {}
        chats = get('chats', {})
        for key, chat in chats.items():
            if (not active(chat) or protected(key) or offer_blocked(draft, key)
                    or not audience_matches(draft, key)):
                continue
            cid = chat['connection']
            if cid not in connections:
                try:
                    connections[cid] = connection(cid)
                except ValueError:
                    connections[cid] = {}
            if connections[cid].get('rights', {}).get('can_reply'):
                candidates.append({'key': key, 'connection': cid, 'state': 'pending'})
        if not candidates:
            raise ValueError('No recipients match: reply window, buyer/hold exclusions, and any saved-pack/history/arrival filters. Nothing sent.')
        draft.update(mode='broadcast', recipients=candidates, confirm=secrets.token_hex(3), expires=time.time()+300)
        put('draft', draft)
        tell(uid, 'BROADCAST FROM JOVANICA\nRecipients: ' + str(len(candidates)) +
             ' of ' + str(len(chats)) + ' recorded chats\n' + offer_description(draft) + content_description(draft) +
             '\n\nThis ignores /target and sends only to eligible NON-BUYERS without a manual hold in this frozen list.' +
             '\nExpired/unavailable chats will be skipped or rejected.' +
             '\nConfirm within 5 minutes: /confirm ' + draft['confirm'] + '\nOr /cancel.')
    elif text == '/cancel':
        put('draft', {})
        tell(uid, 'Draft discarded.')
    elif text == '/send':
        validate_content(draft)
        if 'target' not in draft:
            raise ValueError('Choose /target NUMBER first.')
        chat = get('chats', {})[draft['target']]
        if offer_blocked(draft, draft['target']):
            raise ValueError('This saved pack is already sent, excluded, or uncertain for this person. Nothing sent.')
        eligible(connection(chat['connection']), chat)
        code = secrets.token_hex(3)
        draft['mode'] = 'single'
        draft['confirm'] = code
        draft['expires'] = time.time() + 300
        put('draft', draft)
        tell(uid, 'SEND FROM JOVANICA\nTo: ' + chat['name'] + ' (' + str(chat['id']) + ')\n' +
             offer_description(draft) + content_description(draft) + '\n\nSend /confirm ' + code + ' within 5 minutes, or /cancel.')
    elif text.startswith('/confirm '):
        if text.split()[1] != draft.get('confirm') or time.time() > draft.get('expires', 0):
            raise ValueError('Invalid/expired confirmation. Run /send or /broadcast again.')
        validate_content(draft)
        if draft.get('mode') == 'broadcast':
            if get('job', {}).get('state') == 'running':
                raise ValueError('A broadcast is already running.')
            job = {k: draft[k] for k in ('media', 'price', 'recipients', 'kind', 'text', 'saved_offer', 'audience_hours') if k in draft}
            job.update(id=secrets.token_hex(4), state='running', caption=draft.get('caption', ''), next_at=0)
            # Atomic job creation and confirmation consumption.
            with DB:
                DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('job', json.dumps(job)))
                DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('draft', '{}'))
            tell(uid, 'Broadcast queued for ' + str(len(job['recipients'])) + ' chats. /status for progress; /stopbroadcast to stop remaining sends.')
            return
        chat = get('chats', {})[draft['target']]
        if offer_blocked(draft, draft['target']):
            raise ValueError('This saved pack is already sent, excluded, or uncertain for this person. Nothing sent.')
        eligible(connection(chat['connection']), chat)
        if chat.get('opted_out'):
            raise ValueError('This recipient opted out.')
        # Consume confirmation BEFORE network call: never automatically retry a paid post.
        draft.pop('confirm', None)
        put('draft', draft)
        try:
            result = deliver(draft, chat['connection'], chat['id'])
        except APIError:
            tell(uid, 'Send failed or delivery is uncertain. Check the recipient chat BEFORE trying again. Check reply permission and have recipient send Hi again.')
            return
        put('last_send', {'message_id': result['message_id'], 'chat': chat['id'], 'price': draft.get('price', 0)})
        put('draft', {})
        tell(uid, 'Telegram accepted the message. Check the recipient chat to verify content and sender.')
    else:
        tell(uid, HELP)

def main():
    global DB
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN in Railway Variables.')
    initialize_database(os.environ.get('DATA_DIR', '/data'))
    me = api('getMe')
    print('Manager started: @' + me['username'], flush=True)
    if api('getWebhookInfo').get('url'):
        raise SystemExit('This token has a webhook. Use the new dedicated bot token; do not run two services for one bot.')
    while True:
        try:
            updates = api('getUpdates', offset=get('offset', 0), timeout=0 if get('job', {}).get('state') == 'running' else 25,
                allowed_updates=['message', 'business_connection', 'business_message', 'purchased_paid_media'])
            for update in updates:
                # Persist before processing: failure cannot replay an outbound action.
                put('offset', update['update_id'] + 1)
                try:
                    handle(update)
                except Exception as exc:
                    print('Update failed (' + type(exc).__name__ + '); no automatic send retry.', flush=True)
            try:
                sync_sales_tick()
                broadcast_tick()
            except Exception as exc:
                print('Broadcast step failed (' + type(exc).__name__ + '); check /status.', flush=True)
                time.sleep(3)
            if get('job', {}).get('state') == 'running':
                time.sleep(1.1)
        except APIError:
            print('Telegram polling unavailable; retrying.', flush=True)
            time.sleep(5)

if __name__ == '__main__':
    main()
