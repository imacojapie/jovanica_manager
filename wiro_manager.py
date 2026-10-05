"""Seed Run API edition, v3. Keep original bot.py beside this file.
Run: python wiro_manager.py. Standard library only, Python 3.10+.
Railway: BOT_TOKEN, OWNER_ID, DATA_DIR unchanged; WIRO_API_KEY required.
API Key Only authentication. WIRO_MODEL is ignored: Seed is fixed in this edition.
First upgrade disables AI. /ai test ID enables only one recipient; /ai on enables all.
/ai off stops replies. /ai status shows model, mode and last error.
AI_SYSTEM_PROMPT optionally overrides the default Belgrade instructions.
FANVUE_URL is the clickable link injected at send time. Do not put the raw URL in the prompt.
Limits: AI_DAILY_LIMIT=300, AI_CHAT_DAILY_LIMIT=200 attempts, resetting at UTC midnight.
No automatic generation/send retries. Timed-out Wiro tasks may still incur charges.
Only completed structured answer segments are sent; thinking/debug text is never sent.
No disclosure banner is ever prepended to a fan reply.
Memory stays in the same SQLite database, latest 20 messages per chat. For Wiro,
each request uses a fresh session and sends limited local history to avoid duplication.
The original PPV/broadcast manager is imported unchanged. /hold affects broadcasts
only; /aipause controls conversation. Manual outgoing messages pause a chat.
Rollback: /ai off, then start python bot.py. Preserve the persistent volume.
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


def outgoing(text):
    # Do not replace s:// inside https:// — that turned the link into httphttps://.
    text = text.replace('[FANVUE]', FANVUE)
    text = text.replace('httphttps://', 'https://')
    text = text.replace('https://https://', 'https://')
    text = re.sub(r'(^|[\s(])s://', r'\1https://', text)
    low = text.lower()
    needs_link = any(k in low for k in ('fanvue', 'besplatno', 'subscribe', '80 slika', 'custom', 'od toga živim', 'badava'))
    if needs_link and 'fanvue.com/malajovanica02' not in text:
        text = text.rstrip() + '\n' + FANVUE
    return text


def initialize(path):
    dbfile = Path(path) / 'manager.sqlite3'
    backup = Path(path) / 'manager-before-ai.sqlite3'
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
    ''')
    if base.get('ai_adapter_version') != 'seed-run-v3':
        base.put('ai_last_error', 'none')
        base.put('ai_adapter_version', 'seed-run-v3')
    with base.DB:
        base.DB.execute("UPDATE ai_queue SET status='interrupted' WHERE status IN ('pending','generating','sending')")


def enabled():
    return bool(base.get('ai_enabled', False))


def cancel(cid=None, chat=None):
    with base.DB:
        if cid is None:
            base.DB.execute("UPDATE ai_queue SET status='cancelled' WHERE status IN ('pending','generating')")
        else:
            base.DB.execute("UPDATE ai_queue SET status='cancelled' WHERE cid=? AND chat=?", (cid,chat))


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


def handle(update):
    m = update.get('business_message')
    if not m:
        return OLD_HANDLE(update)
    if m.get('sender_business_bot') or m.get('is_from_offline'):
        return
    cid, key = m.get('business_connection_id'), str(m.get('chat', {}).get('id'))
    if not cid or m.get('chat', {}).get('type') != 'private':
        return OLD_HANDLE(update)
    sender = m.get('from', {})
    if sender.get('id') == base.OWNER:
        base.connection(cid)
        with base.DB:
            base.DB.execute('INSERT OR IGNORE INTO ai_controls(cid,chat) VALUES (?,?)', (cid,key))
            base.DB.execute('UPDATE ai_controls SET paused=1 WHERE cid=? AND chat=?', (cid,key))
        cancel(cid,key)
        if m.get('text'):
            history(cid,key,m['message_id'],'assistant',m['text'])
        return
    OLD_HANDLE(update)
    if sender.get('is_bot') or not base.OWNER:
        return
    text = m.get('text','').strip()
    if text.lower() == '/stop':
        cancel(cid,key)
        return
    if not text or text.startswith('/'):
        return
    if not allowed(cid,key) or not 0 <= time.time()-m['date'] < 300:
        return
    if history(cid,key,m['message_id'],'user',text):
        with base.DB:
            base.DB.execute('INSERT OR REPLACE INTO ai_queue VALUES (?,?,?,?,?)',
                            (cid,key,str(m['message_id']),time.time()+2,'pending'))


def error_detail(data):
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
    instructions = '\n'.join(m['content'] for m in messages if m['role']=='system')
    conversation = [m for m in messages if m['role']!='system']
    prompt = (instructions + '\n\nSledi istorija razgovora kao JSON podaci. '
              'Odgovori samo na poslednju korisničku poruku, po gornjim pravilima.\n' +
              json.dumps(conversation, ensure_ascii=False))
    session = 'jm-' + secrets.token_hex(16)
    return parse_answer(wiro_request({'prompt':prompt, 'userId':session, 'session_id':session}))


def mark(cid,key,version,status):
    with base.DB:
        base.DB.execute('UPDATE ai_queue SET status=? WHERE cid=? AND chat=? AND version=?',
                        (status,cid,key,version))


def tick():
    for pair, (version, future) in list(RUNNING.items()):
        if not future.done():
            continue
        del RUNNING[pair]
        cid,key = pair
        row = base.DB.execute('SELECT version,status FROM ai_queue WHERE cid=? AND chat=?',pair).fetchone()
        if row != (version,'generating'):
            base.put('ai_last_error', 'skipped send: queue was '+str(row))
            continue
        if not allowed(cid,key):
            base.put('ai_last_error', 'skipped send: chat not allowed (paused, test lock, or opted out)')
            continue
        try:
            text = outgoing(future.result())
            mark(cid,key,version,'sending')
            result = base.api('sendMessage', business_connection_id=cid, chat_id=int(key), text=text)
            mark(cid,key,version,'sent')
            history(cid,key,result['message_id'],'assistant',text)
        except Exception as exc:
            mark(cid,key,version,'failed_or_uncertain')
            base.put('ai_last_error', time.strftime('%Y-%m-%d %H:%M UTC',time.gmtime()) + ' — ' + str(exc)[:300])
            print('AI attempt failed ('+type(exc).__name__+'); no automatic retry.', flush=True)
    rows = base.DB.execute("SELECT cid,chat,version FROM ai_queue WHERE status='pending' AND due<=? ORDER BY due",(time.time(),)).fetchall()
    day = time.strftime('%Y-%m-%d',time.gmtime())
    for cid,key,version in rows:
        if len(RUNNING) >= 4:
            break
        if (cid,key) in RUNNING:
            continue
        if not allowed(cid,key):
            why = 'paused' if base.DB.execute('SELECT paused FROM ai_controls WHERE cid=? AND chat=?',(cid,key)).fetchone() else 'not allowed'
            if base.get('ai_test_chat') and base.get('ai_test_chat') != key:
                why = 'test lock is '+str(base.get('ai_test_chat'))
            mark(cid,key,version,'cancelled')
            base.put('ai_last_error', 'not queued: '+why)
            continue
        total = base.DB.execute('SELECT COALESCE(SUM(count),0) FROM ai_usage WHERE day=?',(day,)).fetchone()[0]
        row = base.DB.execute('SELECT count FROM ai_usage WHERE day=? AND chat=?',(day,key)).fetchone()
        if total >= DAILY or (row and row[0] >= PER_CHAT):
            mark(cid,key,version,'daily_limit')
            base.put('ai_last_error', 'daily limit hit: '+str(total)+'/'+str(DAILY)+' total, '+str(row[0] if row else 0)+'/'+str(PER_CHAT)+' this chat')
            continue
        messages = [{'role':'system','content':PROMPT}]
        messages += [{'role':role,'content':body} for role,body in base.DB.execute(
            'SELECT role,body FROM ai_history WHERE cid=? AND chat=? ORDER BY rowid',(cid,key))]
        with base.DB:
            base.DB.execute('INSERT INTO ai_usage VALUES (?,?,1) ON CONFLICT(day,chat) DO UPDATE SET count=count+1',(day,key))
            base.DB.execute("UPDATE ai_queue SET status='generating' WHERE cid=? AND chat=?",(cid,key))
        RUNNING[(cid,key)] = (version, POOL.submit(generate,messages))


HELP = '''Wiro AI controls (owner only):
/aimodels — show fixed Seed model
/ai test ID — enable only one test recipient
/ai on | /ai off | /ai status
/aipause ID — pause one chat
/airesume ID — allow future incoming messages to get AI replies
/aiforget ID — clear AI memory and cancel the current reply
Manual messages from your profile pause that chat until /airesume ID.
AI replies are free text; existing paid-media commands still control PPV.'''


def process(uid,m,text):
    command, _, arg = text.partition(' ')
    if command not in ('/ai','/aimodels','/aipause','/airesume','/aiforget'):
        return OLD_PROCESS(uid,m,text)
    if uid != base.OWNER:
        return
    arg = arg.strip()
    if command == '/aimodels':
        base.tell(uid, 'This edition uses Seed: '+MODEL+' via Wiro Run API. Use /ai status.')
        return
    if command == '/ai':
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
