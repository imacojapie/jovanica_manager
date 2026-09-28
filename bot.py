"""Single-owner, single-recipient paid-media manager. Python standard library only."""
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
    pass

def api(method, **data):
    request = urllib.request.Request('https://api.telegram.org/bot' + TOKEN + '/' + method,
        data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not log exception URLs: they contain the token.
        raise APIError('Telegram HTTP ' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise APIError('Network error; delivery may be uncertain') from None
    if not result.get('ok'):
        raise APIError('Telegram rejected request')
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

HELP = ('Jovanica PPV Manager — test version\n'
        '/id — your Telegram ID\n/chats — recent incoming customer chats\n'
        '/target NUMBER — choose a chat from /chats\n'
        'Send ONE photo or video to this manager; its caption becomes the offer caption.\n'
        '/price 1 — set Stars price (1–25000)\n/send — review the offer\n'
        '/confirm CODE — send reviewed offer once\n/cancel — discard draft\n'
        'No automatic customer replies or mass broadcasts are enabled.')

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
                      'date': m['date'], 'connection': cid}
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
        lines = [str(c['id']) + ' — ' + c['name'] + (' — active' if time.time()-c['date'] < 86340 else ' — expired') for c in chats.values()]
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
        draft.update(media=media, caption=m.get('caption', ''))
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Media saved. Set price with /price 1, then /send to review.')
    elif text.startswith('/price '):
        price = int(text.split()[1])
        if not 1 <= price <= 25000:
            raise ValueError('Price must be 1–25000 Stars.')
        draft['price'] = price
        draft.pop('confirm', None)
        put('draft', draft)
        tell(uid, 'Price: ' + str(price) + ' Stars. /send to review.')
    elif text == '/cancel':
        put('draft', {})
        tell(uid, 'Draft discarded.')
    elif text == '/send':
        if not all(k in draft for k in ('target', 'media', 'price')):
            raise ValueError('Choose /target, upload media, and set /price first.')
        chat = get('chats', {})[draft['target']]
        eligible(connection(chat['connection']), chat)
        code = secrets.token_hex(3)
        draft['confirm'] = code
        draft['expires'] = time.time() + 300
        put('draft', draft)
        tell(uid, 'SEND FROM JOVANICA\nTo: ' + chat['name'] + ' (' + str(chat['id']) + ')\nPrice: ' + str(draft['price']) + ' Stars\nType: ' + draft['media']['type'] + '\nCaption: ' + draft['caption'][:1024] + '\n\nSend /confirm ' + code + ' within 5 minutes, or /cancel.')
    elif text.startswith('/confirm '):
        if text.split()[1] != draft.get('confirm') or time.time() > draft.get('expires', 0):
            raise ValueError('Invalid/expired confirmation. Run /send again.')
        chat = get('chats', {})[draft['target']]
        eligible(connection(chat['connection']), chat)
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
            updates = api('getUpdates', offset=get('offset', 0), timeout=25,
                allowed_updates=['message', 'business_connection', 'business_message'])
            for update in updates:
                # Persist before processing: failure cannot replay an outbound action.
                put('offset', update['update_id'] + 1)
                try:
                    handle(update)
                except Exception as exc:
                    print('Update failed (' + type(exc).__name__ + '); no automatic send retry.', flush=True)
        except APIError:
            print('Telegram polling unavailable; retrying.', flush=True)
            time.sleep(5)

if __name__ == '__main__':
    main()
