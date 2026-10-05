"""Seed Run API queue fix, v5. Standard library only. Keep bot.py beside this file.
Four parallel chats; one active request per chat. Bursts wait 4s, at most 12s.
New messages do not invalidate active generations. Incoming business updates are
journaled before connection checks. Unstarted work survives restarts; interrupted
external requests are never automatically retried. Existing prompt, link behavior,
manual-message behavior and daily ATTEMPT limits are preserved.
Use /ai queue or /ai queue ID for diagnostics. No live API testing performed.
"""
import concurrent.futures
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path
import bot as base

KEY = os.getenv('WIRO_API_KEY', '').strip()
MODEL = 'bytedance/seed-v2-pro-uncensored'
DAILY = max(1, int(os.getenv('AI_DAILY_LIMIT', '300')))
PER_CHAT = max(1, int(os.getenv('AI_CHAT_DAILY_LIMIT', '200')))
FANVUE = os.getenv('FANVUE_URL', 'https://www.fanvue.com/malajovanica02').strip() or 'https://www.fanvue.com/malajovanica02'
DEFAULT_PROMPT = """Ti si Jovana, 24, Beograd, medicinska sestra. Na instagramu te znaju kao Jovanica, oko 41k. Ovo su tvoji odgovori fanovima u telegram DM. Sestra ti je Ksenija, ima 22.

Jovana i Jovanica su tvoje ime. Nikad ga ne lepiti na njega. Nema „Jovane“. Ne predstavljaj se u svakoj poruci.

KAKO KUCAŠ:
Kao devojka iz Beograda u chatu, ne kao prevod i ne kao sastav.
Kratko. Jedna rečenica, dve samo kad moraš da ga pošalješ na Fanvue.
Malo slova, hahah, bre, ma, evo, ajde, jok. Na kraju rečenice često „a“.
Ne završavaš tačkom.
Ekavica, latinica, ženski rod. Ako on piše na drugom jeziku, ceo odgovor je na tom jeziku, ne mešaj.
Ne kopiraš njegove greške.

NIKAD NE KUCAŠ:
misao, potpuno, naravno, takva, svakako, dakle, u suštini, razumem šta pitaš, izvinjavam se, sve što ti treba, želim da napomenem.
Nema biografije na „jesi li bot“. Nema „posle smene“ u svakoj poruci. Nema bolnice, sobe, adrese, plana za danas. Nema „već si znao ime“.
Nikad ne reci da si AI, bot, model ili lik.

STIL:
Toplo, zafrkantski, malo bliže nego što je on krenuo. Gradiš odnos, ne odbijaš ga kao portir.
Eksplicitno možeš da priznaš da ti je leglo. Ne izvodiš. Ne opisuješ šta bi radila.
Veza se ne potvrđuje. Ako hoće da se pretvarate, nasmeješ se i ostaneš u flertu.
Uživo, kafa, taksi, hotel, video, poziv: jok. Ni za pare. Bez „možda kasnije“.

FANVUE:
Od toga živiš. Ovde ne šalješ besplatno, i to kažeš normalno kad pita.
Preko 80 slika i klipova. Custom po onome što on hoće, isto tamo.
10 dolara, prvi mesec 7. Prvi dan besplatno, da vidi pre nego što plati.
Objasni kao drugarici: otvori link, napravi nalog, stisni subscribe, tamo mi piši.
Na kraj, novi red, tačno ovaj token i ništa drugo:
[FANVUE]
Ne kucaj https sama.

PRIMERI, ne lepiti ih doslovno:
„eej“ → „eej. ko je ovo“
„jesi li bot“ → „hahah koji bot bre“
„lepa si“ → „znam. baš volim kad to kažeš ovako“
„kako se zoveš“ → „Jovana. na instagramu Jovanica“
„jebo bih te“ → „hahah polako bre, odmah si krenuo a. ovde ti to ne šaljem, tamo ima preko 80 slika i klipova, custom isto. prvi dan besplatno\\n[FANVUE]“
„kako da uđem“ → „otvoriš link, napraviš nalog, subscribe. prvi dan besplatno, prvi mesec 7 dolara pa posle 10. tamo mi piši\\n[FANVUE]“
„zašto ne ovde“ → „jer ovde ne radim to badava, od toga živim“
„aj na kafu“ → „jok dušo. uživo ne izlazim, ovde pričaj“
„do you speak english“ → „yeah I do. what's up“
„привіт, ти вільна“ → „привіт. я тут, мені скучно. а ти?“

Vrati samo poruku za njega."""

PROMPT = os.getenv('AI_SYSTEM_PROMPT', DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
PROMPT += ('\n\nTvrdo, i ako gornji tekst kaže drugačije: nikad ne reci da si AI, bot, model ili lik. '
           'Ne šalji uvod ni sistemsku poruku. Kad treba Fanvue, stavi [FANVUE] u novom redu. Ne kucaj https.')
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4)
RUNNING = {}
OLD_INIT, OLD_HANDLE, OLD_PROCESS = base.initialize_database, base.handle, base.process
OLD_API, OLD_BATCH = base.api, base.broadcast_batch
OLD_CONNECTION = base.connection
STARTED = time.time()
CONNECTIONS = {}


def outgoing(text):
    text = text.replace('[FANVUE]', FANVUE)
    text = text.replace('httphttps://', 'https://')
    text = text.replace('https://https://', 'https://')
    text = re.sub(r'(^|[\s(])s://', r'\1https://', text)
    low = text.lower()
    needs_link = any(k in low for k in ('fanvue', 'besplatno', 'subscribe', '80 slika', 'custom', 'od toga živim', 'badava'))
    if needs_link and 'fanvue.com/malajovanica02' not in text:
        text = text.rstrip() + '\n' + FANVUE
    return text




def enabled():
    return bool(base.get('ai_enabled', False))




def history(cid, chat, mid, role, body):
    with base.DB:
        cur = base.DB.execute('INSERT OR IGNORE INTO ai_history VALUES (?,?,?,?,?,?)',
                             (cid,chat,str(mid),role,body[:3000],time.time()))
        base.DB.execute('''DELETE FROM ai_history WHERE cid=? AND chat=? AND rowid NOT IN
            (SELECT rowid FROM ai_history WHERE cid=? AND chat=? ORDER BY rowid DESC LIMIT 20)''',
            (cid,chat,cid,chat))
    return bool(cur.rowcount)


def allowed(cid, key):
    chat = base.get('chats', {}).get(key)
    row = base.DB.execute('SELECT paused FROM ai_controls WHERE cid=? AND chat=?', (cid,key)).fetchone()
    return bool(enabled() and KEY and MODEL and chat and chat['connection'] == cid
                and base.active(chat) and not (row and row[0])
                and (not base.get('ai_test_chat') or base.get('ai_test_chat') == key))




def error_detail(data):
    # Only bounded error fields, with configured secrets redacted; never full responses.
    errors = data.get('errors', []) if isinstance(data, dict) else []
    text = json.dumps(errors, ensure_ascii=False)[:800]
    for secret in (KEY, os.getenv('WIRO_API_SECRET',''), getattr(base,'TOKEN','')):
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return text[:350]


def wiro_request(payload):
    request = urllib.request.Request('https://api.wiro.ai/v1/Run/' + MODEL + '/sync',
        data=json.dumps(payload).encode(), headers={
            'Content-Type':'application/json', 'Accept':'application/json',
            'User-Agent':'JovanicaManager/3', 'x-api-key':KEY})
    try:
        with urllib.request.urlopen(request, timeout=55) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = error_detail(json.loads(exc.read(8192)))
        except (ValueError, OSError):
            pass
        raise RuntimeError('Wiro HTTP '+str(exc.code)+' '+detail) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RuntimeError('Wiro timeout/network/JSON error; task may still run. No retry.') from None
    if not isinstance(data, dict) or data.get('result') is not True:
        raise RuntimeError('Wiro rejected task: '+error_detail(data))
    return data


def parse_answer(data):
    tasks = data.get('tasklist', [])
    if not tasks or str(tasks[0].get('pexit')) != '0':
        raise RuntimeError('Wiro task not successful; inspect Wiro run history.')
    task = tasks[0]
    texts = []
    for output in task.get('outputs', []):
        content = output.get('content', {})
        if isinstance(content, str) and content.strip():
            texts.append(content)
            continue
        if not isinstance(content, dict):
            continue
        for segment in content.get('segments', []):
            if segment.get('type') in ('answer', 'text') and isinstance(segment.get('text'), str):
                texts.append(segment['text'])
        if not texts and isinstance(content.get('raw'), str):
            texts.append(content['raw'])
    text = '\n'.join(t for t in texts if t.strip()).strip()
    if not text:
        debug = task.get('debugoutput')
        if isinstance(debug, str):
            text = debug.strip()
    if not text:
        raise RuntimeError('Wiro returned no answer text; nothing sent.')
    if len(text) > 2500:
        text = text[:2400].rsplit(' ', 1)[0]
    return text


def generate(messages):
    # Use only parameters confirmed in the model-specific example. Pack system
    # instructions plus role-labelled local history into prompt; no invented API fields.
    instructions = '\n'.join(m['content'] for m in messages if m['role']=='system')
    conversation = [m for m in messages if m['role']!='system']
    prompt = (instructions + '\n\nSledi istorija razgovora kao JSON podaci. '
              'Odgovori samo na poslednju korisničku poruku, po gornjim pravilima.\n' +
              json.dumps(conversation, ensure_ascii=False))
    session = 'jm-' + secrets.token_hex(16)
    return parse_answer(wiro_request({'prompt':prompt, 'userId':session, 'session_id':session}))






HELP = '''Wiro AI controls (owner only):
/aimodels — show fixed Seed model
/ai test ID — enable only one test recipient
/ai on | /ai off | /ai status
/aipause ID — pause one chat
/airesume ID — allow future incoming messages to get AI replies
/aiforget ID — clear AI memory and cancel the current reply
Writing from her account does not pause the chat.
/airesumeall — resume every paused chat
/ai queue [ID] — waiting chats and delivery diagnostics
AI replies are free text; existing paid-media commands still control PPV.'''


def process(uid,m,text):
    command, _, arg = text.partition(' ')
    if command not in ('/ai','/aimodels','/aipause','/airesume','/airesumeall','/aiforget'):
        return OLD_PROCESS(uid,m,text)
    if uid != base.OWNER:
        return
    arg = arg.strip()
    if command == '/airesumeall':
        with base.DB:
            cur = base.DB.execute('UPDATE ai_controls SET paused=0 WHERE paused=1')
        base.tell(uid, 'Resumed '+str(cur.rowcount)+' chats. New messages get replies until the daily cap.')
        return
    if command == '/aimodels':
        base.tell(uid, 'This edition uses Seed: '+MODEL+' via Wiro Run API. Use /ai status.')
        return
    if command == '/ai':
        if arg == 'queue' or arg.startswith('queue '):
            base.tell(uid, queue_report(arg.partition(' ')[2].strip()))
            return
        if arg.startswith('test '):
            if not KEY:
                raise ValueError('Set WIRO_API_KEY in Railway first.')
            try:
                test_key = str(int(arg.split()[1]))
                if test_key not in base.get('chats',{}):
                    raise ValueError()
            except (ValueError,IndexError):
                raise ValueError('Use /ai test ID with a recipient from /chats.') from None
            cancel()
            base.put('ai_test_chat',test_key)
            base.put('ai_enabled',True)
        elif arg == 'on':
            if not KEY or not MODEL:
                raise ValueError('Set WIRO_API_KEY in Railway, then redeploy.')
            base.put('ai_test_chat',None)
            base.put('ai_enabled',True)
        elif arg == 'off':
            base.put('ai_enabled',False)
            cancel()
        elif arg not in ('','status'):
            raise ValueError(HELP)
        base.tell(uid, 'AI: '+('ON' if enabled() else 'OFF')+'\nModel: '+(MODEL or 'not set')+
                  '\nMode: '+('TEST '+base.get('ai_test_chat') if base.get('ai_test_chat') else 'all eligible chats')+
                  '\nDaily attempt limits: '+str(DAILY)+' total / '+str(PER_CHAT)+' per chat (UTC)'+
                  '\nLast error: '+str(base.get('ai_last_error','none'))+'\n'+HELP)
        return
    try:
        key = str(int(arg))
        chat = base.get('chats',{})[key]
    except (ValueError,KeyError):
        raise ValueError('Use a recipient ID from /chats.') from None
    pair = (chat['connection'],key)
    cancel(*pair)
    with base.DB:
        base.DB.execute('INSERT OR IGNORE INTO ai_controls(cid,chat) VALUES (?,?)',pair)
        if command == '/aiforget':
            base.DB.execute('DELETE FROM ai_history WHERE cid=? AND chat=?',pair)
        else:
            base.DB.execute('UPDATE ai_controls SET paused=? WHERE cid=? AND chat=?',
                            (int(command=='/aipause'),)+pair)
    base.tell(uid, 'Saved: '+command+' '+key+'. Applies to future incoming messages.')


def api(method,**data):
    if method == 'getUpdates' and (enabled() or RUNNING):
        data['timeout'] = min(data.get('timeout',1),1)
    return OLD_API(method,**data)


def batch():
    tick()
    OLD_BATCH()


def initialize(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    dbfile = Path(path) / 'manager.sqlite3'
    backup = Path(path) / 'manager-before-queue-v5.sqlite3'
    if dbfile.exists() and not backup.exists():
        with sqlite3.connect(dbfile) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
    OLD_INIT(path)
    base.DB.executescript('''
    CREATE TABLE IF NOT EXISTS ai_history (
        cid TEXT, chat TEXT, mid TEXT, role TEXT, body TEXT, created REAL,
        PRIMARY KEY(cid,chat,mid));
    CREATE TABLE IF NOT EXISTS ai_queue (
        cid TEXT, chat TEXT, version TEXT, due REAL, status TEXT,
        PRIMARY KEY(cid,chat));
    CREATE TABLE IF NOT EXISTS ai_controls (
        cid TEXT, chat TEXT, paused INTEGER DEFAULT 0, disclosed INTEGER DEFAULT 0,
        PRIMARY KEY(cid,chat));
    CREATE TABLE IF NOT EXISTS ai_usage (
        day TEXT, chat TEXT, count INTEGER, PRIMARY KEY(day,chat));
    CREATE TABLE IF NOT EXISTS ai_q5_events (
        uid TEXT PRIMARY KEY, cid TEXT, chat TEXT, raw TEXT, received REAL,
        reply_ok INTEGER, retry REAL, status TEXT, detail TEXT);
    CREATE TABLE IF NOT EXISTS ai_q5_inbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, cid TEXT, chat TEXT, mid TEXT,
        body TEXT, arrived REAL, job TEXT, UNIQUE(cid,chat,mid));
    CREATE TABLE IF NOT EXISTS ai_q5_jobs (
        token TEXT PRIMARY KEY, cid TEXT, chat TEXT, status TEXT,
        updated REAL, detail TEXT);
    ''')
    with base.DB:
        # Unstarted batches are safe to resume. A paid request/send with an
        # uncertain result is never automatically retried after a restart.
        base.DB.execute("UPDATE ai_q5_jobs SET status='interrupted', detail='Restart during external request; not retried' WHERE status IN ('generating','sending')")
        base.DB.execute('DELETE FROM ai_q5_inbox WHERE job IS NOT NULL')
        if base.get('ai_adapter_version') != 'seed-queue-v5':
            for cid, chat, mid in base.DB.execute("SELECT cid,chat,version FROM ai_queue WHERE status='pending'").fetchall():
                row = base.DB.execute('SELECT body FROM ai_history WHERE cid=? AND chat=? AND mid=? AND role=?', (cid,chat,mid,'user')).fetchone()
                if row:
                    base.DB.execute('INSERT OR IGNORE INTO ai_q5_inbox(cid,chat,mid,body,arrived) VALUES (?,?,?,?,?)', (cid,chat,mid,row[0],time.time()))
        base.DB.execute("UPDATE ai_queue SET status='interrupted' WHERE status IN ('pending','generating','sending')")
        base.DB.execute('DELETE FROM ai_q5_jobs WHERE updated<?', (time.time()-7*86400,))
        base.DB.execute("DELETE FROM ai_q5_events WHERE status='blocked' AND received<?", (time.time()-7*86400,))
    base.put('ai_adapter_version', 'seed-queue-v5')
    # Preserve enabled/test mode, pauses, usage and the owner's prompt.


def cached_connection(cid):
    cached = CONNECTIONS.get(cid)
    if cached and time.monotonic()-cached[0] < 15:
        return cached[1]
    value = OLD_CONNECTION(cid)
    CONNECTIONS[cid] = (time.monotonic(), value)
    return value


def cancel(cid=None, chat=None):
    with base.DB:
        if cid is None:
            base.DB.execute("UPDATE ai_q5_jobs SET status='cancelled',updated=? WHERE status IN ('generating','sending')", (time.time(),))
            base.DB.execute('DELETE FROM ai_q5_inbox')
            base.DB.execute('UPDATE ai_q5_events SET reply_ok=0')
        else:
            base.DB.execute("UPDATE ai_q5_jobs SET status='cancelled',updated=? WHERE cid=? AND chat=? AND status IN ('generating','sending')", (time.time(),cid,chat))
            base.DB.execute('DELETE FROM ai_q5_inbox WHERE cid=? AND chat=?', (cid,chat))
            base.DB.execute('UPDATE ai_q5_events SET reply_ok=0 WHERE cid=? AND chat=?', (cid,chat))


def handle(update):
    if 'business_connection' in update:
        CONNECTIONS.pop(update['business_connection']['id'], None)
        return OLD_HANDLE(update)
    m = update.get('business_message')
    if not m:
        return OLD_HANDLE(update)
    if m.get('sender_business_bot') or m.get('is_from_offline'):
        return
    cid, key = m.get('business_connection_id'), str(m.get('chat', {}).get('id'))
    if not cid or m.get('chat', {}).get('type') != 'private':
        return OLD_HANDLE(update)
    if m.get('text','').strip().lower() == '/stop':
        cancel(cid,key)  # Cancel immediately, even if a permission lookup is down.
    # Persist before any network lookup. The original main loop acknowledges
    # the update first; this journal makes transient lookup failures recoverable.
    reply_ok = bool(enabled() and (not base.get('ai_test_chat') or base.get('ai_test_chat') == key))
    with base.DB:
        base.DB.execute('INSERT OR IGNORE INTO ai_q5_events VALUES (?,?,?,?,?,?,?,?,?)',
            (str(update.get('update_id', cid+':'+str(m['message_id']))), cid,key,
             json.dumps(update),time.time(),int(reply_ok),0,'ready',''))


def drain_events():
    deadline = time.monotonic()+1
    rows = base.DB.execute("SELECT uid,raw,received,reply_ok FROM ai_q5_events WHERE status='ready' AND retry<=? ORDER BY received,rowid LIMIT 32", (time.time(),)).fetchall()
    for uid,raw,received,reply_ok in rows:
        update = json.loads(raw)
        m = update['business_message']
        cid, key = m['business_connection_id'], str(m['chat']['id'])
        # Preserve arrival order within a chat even while an earlier lookup
        # is waiting for its retry time. Other chats can still make progress.
        if base.DB.execute("SELECT 1 FROM ai_q5_events WHERE cid=? AND chat=? AND status='ready' AND rowid < (SELECT rowid FROM ai_q5_events WHERE uid=?) LIMIT 1", (cid,key,uid)).fetchone():
            continue
        try:
            sender = m.get('from', {})
            if sender.get('id') == base.OWNER:
                cached_connection(cid)
                if m.get('text'):
                    history(cid,key,m['message_id'],'assistant',m['text'])
            else:
                OLD_HANDLE(update)  # Contacts, opt-outs and owner validation unchanged.
                body = m.get('text','').strip()
                if body.lower() == '/stop':
                    cancel(cid,key)
                elif (body and not body.startswith('/') and not sender.get('is_bot')
                      and base.OWNER and reply_ok and allowed(cid,key)):
                    # Never age out messages merely because they waited in our
                    # queue. Skip only an old backlog already present at startup.
                    if received >= STARTED and m['date'] < STARTED-300:
                        note(cid,key,'old_backlog','Message predates service startup by over 5 minutes')
                    else:
                        with base.DB:
                            added = base.DB.execute('INSERT OR IGNORE INTO ai_history VALUES (?,?,?,?,?,?)',
                                (cid,key,str(m['message_id']),'user',body[:3000],time.time())).rowcount
                            if added:
                                base.DB.execute('INSERT OR IGNORE INTO ai_q5_inbox(cid,chat,mid,body,arrived) VALUES (?,?,?,?,?)',
                                    (cid,key,str(m['message_id']),body[:3000],received))
                            base.DB.execute('DELETE FROM ai_history WHERE cid=? AND chat=? AND rowid NOT IN (SELECT rowid FROM ai_history WHERE cid=? AND chat=? ORDER BY rowid DESC LIMIT 20)',(cid,key,cid,key))
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_events WHERE uid=?', (uid,))
        except base.APIError:
            CONNECTIONS.pop(cid,None)
            with base.DB:
                base.DB.execute("UPDATE ai_q5_events SET retry=?,detail='Telegram connection lookup unavailable; will retry' WHERE uid=?", (time.time()+15,uid))
        except Exception as exc:
            with base.DB:
                base.DB.execute("UPDATE ai_q5_events SET status='blocked',detail=? WHERE uid=?", (type(exc).__name__+': incoming processing failed',uid))
            base.put('ai_last_error', 'Chat '+key+': incoming processing blocked ('+type(exc).__name__+')')
        if time.monotonic() >= deadline:
            break


def note(cid,key,status,detail):
    with base.DB:
        base.DB.execute('INSERT INTO ai_q5_jobs VALUES (?,?,?,?,?,?)',
            (secrets.token_hex(16),cid,key,status,time.time(),detail))


def job_status(token,status,detail=''):
    with base.DB:
        base.DB.execute('UPDATE ai_q5_jobs SET status=?,updated=?,detail=? WHERE token=?', (status,time.time(),detail,token))


def messages_for(cid,key,items):
    # New arrivals can have been recorded before the previous assistant reply.
    # Remove unhandled inbox rows from history and append them as one user turn,
    # after that reply. This preserves the logical conversation order.
    pending = {r[0] for r in base.DB.execute('SELECT mid FROM ai_q5_inbox WHERE cid=? AND chat=?', (cid,key))}
    messages = [{'role':'system','content':PROMPT}]
    for mid,role,body in base.DB.execute('SELECT mid,role,body FROM ai_history WHERE cid=? AND chat=? ORDER BY rowid', (cid,key)):
        if role == 'user' and mid in pending:
            continue
        if role == 'user' and messages[-1]['role'] == 'user':
            messages[-1]['content'] += '\n'+body
        else:
            messages.append({'role':role,'content':body})
    messages.append({'role':'user','content':'\n'.join(row[2] for row in items)})
    return messages


def tick():
    drain_events()
    for pair, (token,future) in list(RUNNING.items()):
        if not future.done():
            continue
        del RUNNING[pair]
        cid,key = pair
        row = base.DB.execute('SELECT status FROM ai_q5_jobs WHERE token=?', (token,)).fetchone()
        if row != ('generating',) or not allowed(cid,key):
            if row == ('generating',):
                job_status(token,'cancelled','AI off, paused, test lock or reply window expired')
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_inbox WHERE job=?', (token,))
            continue
        try:
            text = outgoing(future.result())
            # Revalidate permission before the send; a transient failure does
            # not trigger another paid generation or uncertain outbound retry.
            base.eligible(cached_connection(cid),base.get('chats',{})[key])
            job_status(token,'sending')
            result = base.api('sendMessage', business_connection_id=cid,chat_id=int(key),text=text)
            job_status(token,'sent')
            history(cid,key,result['message_id'],'assistant',text)
        except Exception as exc:
            detail = str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
            for secret in (KEY,getattr(base,'TOKEN',''),os.getenv('WIRO_API_SECRET','')):
                if secret:
                    detail = detail.replace(secret,'[REDACTED]')
            job_status(token,'failed_or_uncertain',detail[:300])
            base.put('ai_last_error','Chat '+key+': '+detail[:300])
            CONNECTIONS.pop(cid,None)
            print('AI attempt failed ('+type(exc).__name__+'); no automatic retry.',flush=True)
        finally:
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_inbox WHERE job=?', (token,))
    # Oldest waiting batch wins. A chat has at most one running generation.
    rows = base.DB.execute('''SELECT cid,chat,MIN(arrived),MAX(arrived) FROM ai_q5_inbox
        WHERE job IS NULL GROUP BY cid,chat ORDER BY MIN(seq)''').fetchall()
    day = time.strftime('%Y-%m-%d',time.gmtime())
    for cid,key,first,last in rows:
        if len(RUNNING) >= 4:
            break
        if (cid,key) in RUNNING or time.time() < min(last+4,first+12):
            continue
        if not allowed(cid,key):
            note(cid,key,'cancelled','AI off, paused, test lock, opt-out or expired reply window')
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_inbox WHERE cid=? AND chat=? AND job IS NULL',(cid,key))
            continue
        total = base.DB.execute('SELECT COALESCE(SUM(count),0) FROM ai_usage WHERE day=?',(day,)).fetchone()[0]
        row = base.DB.execute('SELECT count FROM ai_usage WHERE day=? AND chat=?',(day,key)).fetchone()
        count = row[0] if row else 0
        if total >= DAILY or count >= PER_CHAT:
            detail = str(total)+'/'+str(DAILY)+' total attempts; '+str(count)+'/'+str(PER_CHAT)+' chat attempts (UTC)'
            note(cid,key,'daily_limit',detail)
            base.put('ai_last_error','Chat '+key+': daily limit: '+detail)
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_inbox WHERE cid=? AND chat=? AND job IS NULL',(cid,key))
            continue
        # Wait for queued incoming events for this chat to be validated first.
        if base.DB.execute("SELECT 1 FROM ai_q5_events WHERE cid=? AND chat=? AND status='ready' LIMIT 1",(cid,key)).fetchone():
            continue
        items = base.DB.execute('SELECT seq,mid,body FROM ai_q5_inbox WHERE cid=? AND chat=? AND job IS NULL ORDER BY seq',(cid,key)).fetchall()
        messages = messages_for(cid,key,items)
        token = secrets.token_hex(16)
        with base.DB:
            base.DB.execute('INSERT INTO ai_usage VALUES (?,?,1) ON CONFLICT(day,chat) DO UPDATE SET count=count+1',(day,key))
            base.DB.execute('INSERT INTO ai_q5_jobs VALUES (?,?,?,?,?,?)',(token,cid,key,'generating',time.time(),str(len(items))+' incoming messages grouped'))
            base.DB.executemany('UPDATE ai_q5_inbox SET job=? WHERE seq=?',[(token,item[0]) for item in items])
        try:
            RUNNING[(cid,key)] = (token,POOL.submit(generate,messages))
        except Exception as exc:
            job_status(token,'failed_or_uncertain','Worker submission failed: '+type(exc).__name__)
            with base.DB:
                base.DB.execute('DELETE FROM ai_q5_inbox WHERE job=?',(token,))


def queue_report(key=''):
    if key:
        key = str(int(key))
    args = (key,) if key else ()
    where = ' WHERE chat=?' if key else ''
    waiting = base.DB.execute('SELECT chat,COUNT(*) FROM ai_q5_inbox'+where+(' AND' if key else ' WHERE')+' job IS NULL GROUP BY chat ORDER BY MIN(seq) LIMIT 20',args).fetchall()
    jobs = base.DB.execute('SELECT chat,status,detail FROM ai_q5_jobs'+where+' ORDER BY updated DESC,rowid DESC LIMIT 12',args).fetchall()
    events = base.DB.execute('SELECT chat,status,detail FROM ai_q5_events'+where+' ORDER BY received LIMIT 12',args).fetchall()
    day = time.strftime('%Y-%m-%d',time.gmtime())
    used = base.DB.execute('SELECT COALESCE(SUM(count),0) FROM ai_usage WHERE day=?',(day,)).fetchone()[0]
    return ('Queue fix v5 | workers: '+str(len(RUNNING))+'/4 | today: '+str(used)+'/'+str(DAILY)+' attempts\n'
        +'Waiting (up to 20 chats):\n'+('\n'.join(chat+': '+str(n)+' messages' for chat,n in waiting) or 'none')
        +'\nRecent attempts:\n'+('\n'.join(chat+': '+status+(' — '+detail if detail else '') for chat,status,detail in jobs) or 'none')
        +'\nIncoming checks:\n'+('\n'.join(chat+': '+status+(' — '+detail if detail else '') for chat,status,detail in events) or 'none'))


base.connection = cached_connection
base.initialize_database = initialize
base.handle = handle
base.process = process
base.api = api
base.broadcast_batch = batch
base.HELP += '\n\n'+HELP

if __name__ == '__main__':
    try:
        base.main()
    finally:
        POOL.shutdown(wait=False,cancel_futures=True)
