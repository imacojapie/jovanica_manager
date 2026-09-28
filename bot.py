"""Single-owner paid-media manager with durable broadcasts. Python standard library only."""
import json
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
    return api('sendMessage', chat_id=chat, text=text)

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

HELP = ('Jovanica PPV Manager v2\n'
        '/id — your Telegram ID\n/chats — recorded customer chats\n'
        '/target NUMBER — choose one recipient\n'
        'Upload ONE photo/video with a caption.\n'
        '/caption TEXT — replace caption; /caption clears it\n'
        '/price 99 — set Stars price\n'
        '/send — review single-recipient offer\n'
        '/broadcast — review offer for ALL eligible recorded chats\n'
        '/confirm CODE — approve reviewed offer\n'
        '/status — delivery summary\n/stopbroadcast — stop remaining sends\n'
        '/cancel — discard unsent draft\n'
        'Recipients can send /stop to Jovanica to opt out, /start to resume.')

def active(chat):
    return (chat.get('id') != OWNER and not chat.get('opted_out', False)
            and 0 <= time.time() - chat['date'] < 86340)

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
    if not chat or not active(chat) or chat['connection'] != row['connection']:
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
        result = api('sendPaidMedia', business_connection_id=row['connection'],
            chat_id=chat['id'], star_count=job['price'], media=[job['media']],
            caption=job['caption'], protect_content=True)
        row['state'] = 'sent'
        row['message_id'] = result['message_id']
    except APIError as exc:
        row['state'] = 'failed' if exc.definite else 'uncertain'
        # No automatic retry. Slow down after errors, including rate limiting.
        job['next_at'] = time.time() + 30
    put('job', job)


def handle(u):
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
        chats[key] = {'id': m['chat']['id'], 'name': m['chat'].get('first_name', key),
                      'date': m['date'], 'connection': cid,
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

def process(uid, m, text):
    draft = get('draft', {})
    if text in ('/start', '/help'):
        tell(uid, HELP)
    elif text == '/chats':
        chats = get('chats', {})
        lines = [str(c['id']) + ' — ' + c['name'] + (' — opted out' if c.get('opted_out') else ' — active' if active(c) else ' — expired') for c in chats.values()]
        tell(uid, '\n'.join(lines)[-3900:] or 'No chats recorded. Now send Hi from your second account to Jovanica.')
    elif text.startswith('/target '):
        key = str(int(text.split()[1]))
        if key not in get('chats', {}):
            raise ValueError('Use an ID listed by /chats.')
        draft['target'] = key
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Recipient selected. Send one photo/video here, then /price 1.')
    elif m.get('photo') or m.get('video'):
        if m.get('media_group_id'):
            raise ValueError('Albums are not supported in this test version. Send one item separately.')
        media = {'type': 'photo', 'media': m['photo'][-1]['file_id']} if m.get('photo') else {'type': 'video', 'media': m['video']['file_id']}
        caption = m.get('caption', '')
        if len(caption) > 1024:
            raise ValueError('Caption must be at most 1024 characters.')
        draft.update(media=media, caption=caption)
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Media saved. Set price with /price 1, then /send for one recipient or /broadcast for all eligible chats.')
    elif text.startswith('/price '):
        price = int(text.split()[1])
        if not 1 <= price <= 25000:
            raise ValueError('Price must be 1–25000 Stars.')
        draft['price'] = price
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Price: ' + str(price) + ' Stars. /send for one recipient or /broadcast for all eligible chats.')
    elif text == '/caption' or text.startswith('/caption '):
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
        if get('job', {}).get('state') == 'running':
            raise ValueError('A broadcast is running. Use /status or /stopbroadcast.')
        if not all(k in draft for k in ('media', 'price')):
            raise ValueError('Upload one photo/video and set /price first.')
        candidates = []
        connections = {}
        chats = get('chats', {})
        for key, chat in chats.items():
            if not active(chat):
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
            raise ValueError('No eligible chats. Have your test account send Hi to Jovanica again.')
        draft.update(mode='broadcast', recipients=candidates, confirm=secrets.token_hex(3), expires=time.time()+300)
        put('draft', draft)
        tell(uid, 'BROADCAST FROM JOVANICA\nRecipients: ' + str(len(candidates)) +
             ' of ' + str(len(chats)) + ' recorded chats\nPrice: ' + str(draft['price']) +
             ' Stars PER RECIPIENT\nType: ' + draft['media']['type'] + '\nCaption: ' +
             draft.get('caption', '') + '\n\nThis ignores /target and sends to ALL listed eligible chats.' +
             '\nAudience is frozen now; expired/unavailable chats will be skipped or rejected.' +
             '\nConfirm within 5 minutes: /confirm ' + draft['confirm'] + '\nOr /cancel.')
    elif text == '/cancel':
        put('draft', {})
        tell(uid, 'Draft discarded.')
    elif text == '/send':
        if not all(k in draft for k in ('target', 'media', 'price')):
            raise ValueError('Choose /target, upload media, and set /price first.')
        chat = get('chats', {})[draft['target']]
        eligible(connection(chat['connection']), chat)
        code = secrets.token_hex(3)
        draft['mode'] = 'single'
        draft['confirm'] = code
        draft['expires'] = time.time() + 300
        put('draft', draft)
        tell(uid, 'SEND FROM JOVANICA\nTo: ' + chat['name'] + ' (' + str(chat['id']) + ')\nPrice: ' + str(draft['price']) + ' Stars\nType: ' + draft['media']['type'] + '\nCaption: ' + draft['caption'][:1024] + '\n\nSend /confirm ' + code + ' within 5 minutes, or /cancel.')
    elif text.startswith('/confirm '):
        if text.split()[1] != draft.get('confirm') or time.time() > draft.get('expires', 0):
            raise ValueError('Invalid/expired confirmation. Run /send or /broadcast again.')
        if draft.get('mode') == 'broadcast':
            if get('job', {}).get('state') == 'running':
                raise ValueError('A broadcast is already running.')
            job = {k: draft[k] for k in ('media', 'price', 'recipients')}
            job.update(id=secrets.token_hex(4), state='running', caption=draft.get('caption', ''), next_at=0)
            # Atomic job creation and confirmation consumption.
            with DB:
                DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('job', json.dumps(job)))
                DB.execute('INSERT OR REPLACE INTO state VALUES (?,?)', ('draft', '{}'))
            tell(uid, 'Broadcast queued for ' + str(len(job['recipients'])) + ' chats. /status for progress; /stopbroadcast to stop remaining sends.')
            return
        chat = get('chats', {})[draft['target']]
        eligible(connection(chat['connection']), chat)
        if chat.get('opted_out'):
            raise ValueError('This recipient opted out.')
        # Consume confirmation BEFORE network call: never automatically retry a paid post.
        draft.pop('confirm', None)
        put('draft', draft)
        try:
            result = api('sendPaidMedia', business_connection_id=chat['connection'],
                chat_id=chat['id'], star_count=draft['price'], media=[draft['media']],
                caption=draft['caption'][:1024], protect_content=True)
        except APIError:
            tell(uid, 'Send failed or delivery is uncertain. Check the recipient chat BEFORE trying again. Check reply permission and have recipient send Hi again.')
            return
        put('last_send', {'message_id': result['message_id'], 'chat': chat['id'], 'price': draft['price']})
        put('draft', {})
        tell(uid, 'Telegram accepted the paid post. Check your second account to verify the lock, sender, and price.')
    else:
        tell(uid, HELP)

def main():
    global DB
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN in Railway Variables.')
    path = Path(os.environ.get('DATA_DIR', '/data'))
    path.mkdir(parents=True, exist_ok=True)
    DB = sqlite3.connect(path / 'manager.sqlite3')
    DB.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    DB.commit()
    me = api('getMe')
    print('Manager started: @' + me['username'], flush=True)
    if api('getWebhookInfo').get('url'):
        raise SystemExit('This token has a webhook. Use the new dedicated bot token; do not run two services for one bot.')
    while True:
        try:
            updates = api('getUpdates', offset=get('offset', 0), timeout=0 if get('job', {}).get('state') == 'running' else 25,
                allowed_updates=['message', 'business_connection', 'business_message'])
            for update in updates:
                # Persist before processing: failure cannot replay an outbound action.
                put('offset', update['update_id'] + 1)
                try:
                    handle(update)
                except Exception as exc:
                    print('Update failed (' + type(exc).__name__ + '); no automatic send retry.', flush=True)
            try:
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
