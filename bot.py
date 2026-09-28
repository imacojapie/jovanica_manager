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

HELP = ('Jovanica Manager v3 — PPV bundles + text\n'
        '/newppv — start a fresh photo/video bundle\n'
        'Upload an album or individual photos/videos (max 10), then /done.\n'
        '/caption TEXT — set bundle caption\n/price NUMBER — 1–25000 Stars for the WHOLE bundle\n'
        '/text MESSAGE — create a FREE text-only message\n'
        '/target NUMBER — choose one recipient\n/send — review single send\n'
        '/broadcast — review all eligible recipients\n/confirm CODE — approve\n'
        '/draft — inspect content and item count\n/cancel — discard draft\n'
        '/chats — recipients\n/stats — counts and storage\n/id — your ID\n'
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

def deliver(content, cid, chat_id):
    validate_content(content)
    if content.get('kind') == 'text':
        return api('sendMessage', business_connection_id=cid, chat_id=chat_id, text=content['text'])
    return api('sendPaidMedia', business_connection_id=cid, chat_id=chat_id,
               star_count=content['price'], media=media_items(content),
               caption=content.get('caption', ''), protect_content=True)

def initialize_database(path):
    global DB
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    dbfile = path / 'manager.sqlite3'
    existing = dbfile.exists()
    DB = sqlite3.connect(dbfile)
    if existing:
        # One backup before the first v3 start, never overwrite an existing backup.
        backup = path / 'manager-before-v3.sqlite3'
        if not backup.exists():
            with sqlite3.connect(backup) as dest:
                DB.backup(dest)
    DB.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    DB.commit()
    # No drops, deletes or reset of contacts, offsets, jobs, or drafts.


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
        result = deliver(job, row['connection'], chat['id'])
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
    elif text == '/stats':
        chats = get('chats', {})
        path = os.environ.get('DATA_DIR', '/data')
        mount = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', 'Not reported; check Railway volume settings')
        tell(uid, 'Recorded chats: ' + str(len(chats)) + '\nEligible now: ' + str(sum(active(c) for c in chats.values())) + '\nDatabase: ' + path + '/manager.sqlite3\nRailway volume mount: ' + mount + '\nExpired chats stay recorded. Telegram contacts are separate.')
    elif text == '/draft':
        tell(uid, content_description(draft) + ('\nBundle OPEN — /done when finished.' if draft.get('collecting') else ''))
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
        if get('job', {}).get('state') == 'running':
            raise ValueError('A broadcast is running. Use /status or /stopbroadcast.')
        validate_content(draft)
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
             ' of ' + str(len(chats)) + ' recorded chats\n' + content_description(draft) +
             '\n\nThis ignores /target and sends to ALL eligible chats in this frozen list.' +
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
        eligible(connection(chat['connection']), chat)
        code = secrets.token_hex(3)
        draft['mode'] = 'single'
        draft['confirm'] = code
        draft['expires'] = time.time() + 300
        put('draft', draft)
        tell(uid, 'SEND FROM JOVANICA\nTo: ' + chat['name'] + ' (' + str(chat['id']) + ')\n' +
             content_description(draft) + '\n\nSend /confirm ' + code + ' within 5 minutes, or /cancel.')
    elif text.startswith('/confirm '):
        if text.split()[1] != draft.get('confirm') or time.time() > draft.get('expires', 0):
            raise ValueError('Invalid/expired confirmation. Run /send or /broadcast again.')
        validate_content(draft)
        if draft.get('mode') == 'broadcast':
            if get('job', {}).get('state') == 'running':
                raise ValueError('A broadcast is already running.')
            job = {k: draft[k] for k in ('media', 'price', 'recipients', 'kind', 'text') if k in draft}
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
