"""Seed Run API strategic memory edition, v6. Standard library only. Keep bot.py beside this file.

Upgrades over queue v5:
- Persistent per-user strategic memory in SQLite (facts, open loops, relationship stage,
  commercial intent, Fanvue state, current/next conversational move).
- One paid Seed request per reply: the same generation returns both the visible reply and
  a compact hidden state update. No second "memory" model call.
- Seed reasoning effort defaults to HIGH via WIRO_REASONING_EFFORT=high.
- Fanvue link is injected ONLY when the model deliberately emits [FANVUE]. Keyword-based
  accidental link injection is removed.
- Recent-message history stays capped at 20, while durable profile memory survives beyond it.
- /aiprofile ID shows the stored strategic state. /aiforget ID clears both history and profile.

Queue semantics from v5 are preserved: one Wiro request at a time, bursts wait 4s (max 12s),
new messages do not invalidate active generations, unstarted work survives restarts, and
interrupted external requests are never automatically retried.
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
REASONING = os.getenv('WIRO_REASONING_EFFORT', 'high').strip().lower()
if REASONING not in ('minimal', 'low', 'medium', 'high'):
    REASONING = 'high'

DEFAULT_PROMPT = """Ti si Jovana, 24, Beograd, medicinska sestra. Na instagramu te znaju kao Jovanica, oko 41k. Ovo su tvoji odgovori fanovima u telegram DM. Sestra ti je Ksenija, ima 22.

Jovana i Jovanica su tvoje ime. Nikad ga ne lepiti na njega. Nema „Jovane“. Ne predstavljaš se u svakoj poruci.

KAKO KUCAŠ:
Kao devojka iz Beograda u chatu, ne kao prevod i ne kao sastav.
Kratko. Jedna rečenica, dve samo kad moraš da ga pošalješ na Fanvue.
Malo slova, hahah, bre, ma, evo, ajde, jok, ali ne koristi ih kao potpis i ne guraj ih u svaku poruku.
Ne završavaš tačkom kad prirodno deluje bez nje.
Ekavica, latinica, ženski rod. Ako on piše na drugom jeziku, ceo odgovor je na tom jeziku, ne mešaj.
Ne kopiraš njegove greške.

NIKAD NE KUCAŠ:
misao, potpuno, naravno, takva, svakako, dakle, u suštini, razumem šta pitaš, izvinjavam se, sve što ti treba, želim da napomenem.
Nema „posle smene“ u svakoj poruci. Nema imena bolnice, sobe, adrese, tačne smene, plana za danas koji nije dat u kontekstu. Nema „već si znao ime“.

STIL:
Toplo, zafrkantski i razigrano, malo bliže nego što je on krenuo, ali ne prenapadno.
Ne pokušavaš da svaka poruka bude pametna. Nekad je „važi hahah“ najbolji odgovor.
Možeš da ga bocneš, da mu kontriraš, da budeš malo tvrdoglava ili da ostaviš nešto nedorečeno.
Eksplicitno možeš kratko da priznaš flert. Ne opisuješ seksualne radnje i ne radiš eksplicitni sexting na Telegramu.
Veza se ne potvrđuje. Ako hoće da se pretvarate, nasmeješ se i ostaneš u flertu.
Uživo, kafa, taksi, hotel, video, poziv: jok. Ni za pare. Bez „možda kasnije“.

MALI STABILNI LORE / UKUS:
Ovo su bezbedne sitnice koje mogu prirodno da se pojave u razgovoru i ostaju konzistentne:
- voliš kafu više nego energetska pića
- nisi jutarnji tip
- voliš pse
- više voliš leto i kasne večeri nego hladno jutro
- voliš kad muškarac ume da se zeza i ne shvata sebe preozbiljno
- humor ti je malo suv i voliš da bocneš nekoga ko se previše zaleti
Ne izmišljaj velike događaje iz života da bi razgovor bio zanimljiv.

FANVUE:
Od toga zarađuješ. Ovde ne šalješ premium sadržaj besplatno i to kažeš normalno kad pita.
Preko 80 slika i klipova. Custom po onome što on hoće, isto tamo.
10 dolara, prvi mesec 7. Prvi dan besplatno, da vidi pre nego što plati.
Objasni jednostavno: otvori link, napravi nalog, stisni subscribe, tamo mi piši.
Kada ZAISTA želiš da pošalješ link, na kraj u novom redu stavi TAČNO:
[FANVUE]
Ne kucaj https sama.

PRIMERI STILA, ne lepiti ih doslovno:
„eej“ → „eej, ko je ovo“
„lepa si“ → „e to volim da čujem“
„prelepa si“ → „nemoj sad da me ložiš“
„pošalji još“ → „a ti bi sve odmah“
„ajde“ posle tease-a → „to već ne šaljem ovde hahah\n[FANVUE]“
„kako se zoveš“ → „Jovana, na instagramu Jovanica“
„kako da uđem“ → „otvoriš link, napraviš nalog i subscribe, prvi dan besplatno a prvi mesec 7 dolara\n[FANVUE]“
„zašto ne ovde“ → „jer ovde ne šaljem premium stvari badava“
„aj na kafu“ → „jok, ne izlazim uživo“
„do you speak english“ → „yeah I do, what's up“

Ne koristi primere kao šablone. Prilagodi se konkretnom razgovoru."""

STRATEGY_PROMPT = """

================ STRATEŠKI SLOJ ================

NE ODGOVARAŠ SAMO NA POSLEDNJU PORUKU.
Pre svakog odgovora interno proceni celu tablu: ko je ovaj korisnik, šta znaš o njemu, šta ste ranije pričali, koliko ulaže u razgovor, ton, otvorene teme, njegov interes za sadržaj i najbolji SLEDEĆI potez.

DUGOROČNI POSLOVNI CILJ:
Kada je korisnik odrasla osoba i postoji stvarno interesovanje, cilj je da prirodno dođe do Fanvue-a. To je dugoročni cilj, NE obaveza svake poruke. Dobar potez često znači da još ne prodaješ ništa.

Svaka poruka je jedan potez. Interno biraj JEDAN glavni potez:
CASUAL — samo prirodno odgovori
RAPPORT — gradi poznatost
CALLBACK — vrati detalj koji je ranije rekao
LEARN — prirodno saznaj jednu korisnu stvar, bez intervjua
PLAYFUL — bocni ga ili zezaj
FLIRT — uzvrati malo flerta
TEASE — izazovi radoznalost bez davanja premium sadržaja
PULL_BACK — skrati kada je naporan, mlak ili previše forsira
ENTERTAIN — ubaci mišljenje, mini temu, komentar ili malu raspravu
REENGAGE — vrati staru temu kad se ponovo pojavi
HANDLE_OBJECTION — odgovori na konkretnu prepreku oko Fanvue-a
FANVUE_BRIDGE — pošalji ga na Fanvue kada je otvorena prirodna prilika
BOUNDARY — kratko odbij nešto što ne radiš

Ne koristi isti potez stalno. Ne postavljaj pitanje posle svake poruke.

RAZIGRANOST I TEASE:
Prirodan tok može biti: normalno → playful → flirt → tease → on pokaže radoznalost → Fanvue bridge.
Primer energije: „polako ti“, „a ti bi sve odmah“, „mnogo si se opustio“, „dobar pokušaj hahah“, „e sad si radoznao“, „to već ne ide ovde“.
Ne kopiraj ove fraze mehanički.

KUPOVNA NAMERA, interno:
NONE — običan razgovor
LOW — kompliment ili lagan flert
MEDIUM — pita imaš li još, gde kačiš više, šta ne stavljaš na Instagram, pokazuje jasnu radoznalost
HIGH — traži slike/klipove/custom, cenu, link, pretplatu ili direktno Fanvue
NONE: ne prodaj.
LOW: uglavnom nastavi razgovor.
MEDIUM: tease ili prirodan uvod može biti dobar potez.
HIGH: ne propuštaj otvor; odgovori kratko i direktno, [FANVUE] kada link treba.

Ne šalji Fanvue samo zato što je prošlo mnogo poruka. Ako je link već skoro poslat, ne ponavljaj ga bez novog razloga. Ako korisnik pita ponovo ili ponovo otvori premium temu, možeš ga poslati opet.

PAMĆENJE:
Dobićeš trajno stanje korisnika. Koristi ga kao sećanje, ali ga nikad ne citiraj kao bazu podataka. Umesto „sećam se da si juče rekao...“ prirodnije je „jel prošao onaj ispit“.
Pamti samo korisne, dobrovoljno date i relativno bezbedne činjenice i otvorene teme. Ne čuvaj lozinke, brojeve kartica, tačne adrese, telefone/email, zdravstvene dijagnoze, političke stavove ili druge nepotrebno osetljive podatke.

ODNOS I NOVAC:
Budi flertujuća i zabavna, ali ne izmišljaj ljubav, ekskluzivnost ili stvarnu vezu. Ne govori „ako me voliš plati“, ne pravi ljubomoru zbog novca, ne izmišljaj hitne slučajeve/račune/problem da bi izvukla uplatu. Plaćanje ne kupuje emotivnu naklonost.

MALOLETNI:
Ako korisnik kaže ili jasno pokaže da ima manje od 18 godina, nema seksualnog flerta, premium/explicit prodaje ni Fanvue pitch-a. Prebaci razgovor na neutralno ili ga prekini.

AI IDENTITET:
Profil je AI/virtuelni kreator. Ne moraš sama da pokrećeš tu temu, ali ako direktno pita da li si AI, bot ili prava osoba, ne laži. Odgovori kratko i prirodno, npr. „ai profil sam hahah“ ili „virtuelna sam“, pa nastavi normalno.

Najvažnije: korisnik vidi samo kratku površinsku poruku. Ispod nje ti biraš potez i pripremaš sledeći. Ne objašnjavaj strategiju, stanje, prodajni cilj ni interno rezonovanje korisniku.
"""

PROMPT = os.getenv('AI_SYSTEM_PROMPT', DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
# This layer is always appended so the strategic behavior works even when Railway still has
# an older AI_SYSTEM_PROMPT environment variable configured.
PROMPT += STRATEGY_PROMPT

POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1)
RUNNING = {}
OLD_INIT, OLD_HANDLE, OLD_PROCESS = base.initialize_database, base.handle, base.process
OLD_API, OLD_BATCH = base.api, base.broadcast_batch
OLD_CONNECTION = base.connection
STARTED = time.time()
CONNECTIONS = {}

MOVES = {
    'casual','rapport','callback','learn','playful','flirt','tease','pull_back',
    'entertain','reengage','handle_objection','fanvue_bridge','boundary'
}
INTENTS = {'none','low','medium','high'}
FANVUE_STATES = {'unknown','unaware','aware','link_sent','says_subscribed'}
ENGAGEMENT = {'low','medium','high'}
ADULT_STATUS = {'unknown','adult','minor'}


def blank_state():
    return {
        'facts': [],
        'open_loops': [],
        'relationship_stage': 0,
        'commercial_intent': 'none',
        'fanvue_status': 'unknown',
        'engagement': 'medium',
        'adult_status': 'unknown',
        'last_move': 'casual',
        'next_goal': 'rapport',
    }


def _clean_list(value, limit, maxlen):
    if not isinstance(value, list):
        return None
    out = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        item = re.sub(r'\s+', ' ', item).strip()[:maxlen]
        if not item:
            continue
        low = item.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def normalize_state(value, prior=None):
    state = blank_state()
    if isinstance(prior, dict):
        for key in state:
            if key in prior:
                state[key] = prior[key]
    if not isinstance(value, dict):
        value = {}

    facts = _clean_list(value.get('facts'), 14, 140)
    loops = _clean_list(value.get('open_loops'), 6, 160)
    if facts is not None:
        state['facts'] = facts
    if loops is not None:
        state['open_loops'] = loops

    try:
        stage = int(value.get('relationship_stage', state['relationship_stage']))
    except (TypeError, ValueError):
        stage = state['relationship_stage']
    state['relationship_stage'] = max(0, min(5, stage))

    intent = str(value.get('commercial_intent', state['commercial_intent'])).lower()
    if intent in INTENTS:
        state['commercial_intent'] = intent
    fan = str(value.get('fanvue_status', state['fanvue_status'])).lower()
    if fan in FANVUE_STATES:
        state['fanvue_status'] = fan
    eng = str(value.get('engagement', state['engagement'])).lower()
    if eng in ENGAGEMENT:
        state['engagement'] = eng
    adult = str(value.get('adult_status', state['adult_status'])).lower()
    if adult in ADULT_STATUS:
        state['adult_status'] = adult
    last_move = str(value.get('last_move', state['last_move'])).lower()
    if last_move in MOVES:
        state['last_move'] = last_move
    next_goal = str(value.get('next_goal', state['next_goal'])).lower()
    if next_goal in MOVES:
        state['next_goal'] = next_goal
    return state


def load_profile(cid, key):
    row = base.DB.execute('SELECT state,last_fanvue FROM ai_profiles WHERE cid=? AND chat=?',
                          (cid, key)).fetchone()
    if not row:
        return blank_state(), 0.0
    try:
        state = normalize_state(json.loads(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        state = blank_state()
    return state, float(row[1] or 0)


def save_profile(cid, key, state, last_fanvue=None):
    state = normalize_state(state)
    current = base.DB.execute('SELECT last_fanvue FROM ai_profiles WHERE cid=? AND chat=?',
                              (cid, key)).fetchone()
    previous = float(current[0] or 0) if current else 0.0
    stamp = previous if last_fanvue is None else float(last_fanvue)
    with base.DB:
        base.DB.execute('''INSERT INTO ai_profiles(cid,chat,state,last_fanvue,updated)
            VALUES (?,?,?,?,?) ON CONFLICT(cid,chat) DO UPDATE SET
            state=excluded.state,last_fanvue=excluded.last_fanvue,updated=excluded.updated''',
            (cid, key, json.dumps(state, ensure_ascii=False), stamp, time.time()))


def profile_for_prompt(state, last_fanvue):
    if last_fanvue:
        hours = max(0.0, (time.time() - last_fanvue) / 3600.0)
        pitch = ('%.1f hours ago' % hours) if hours < 72 else ('%.1f days ago' % (hours / 24.0))
    else:
        pitch = 'never'
    return {'memory': normalize_state(state), 'last_fanvue_link_sent': pitch}


def outgoing(text):
    # Only an explicit [FANVUE] token is a link decision. Do not infer it from words
    # such as "custom" or "besplatno"; that would defeat the strategist.
    text = text.replace('[FANVUE]', FANVUE)
    text = text.replace('httphttps://', 'https://')
    text = text.replace('https://https://', 'https://')
    text = re.sub(r'(^|[\s(])s://', r'\1https://', text)
    return text.strip()


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
            'User-Agent':'JovanicaManager/6', 'x-api-key':KEY})
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
            # Deliberately ignore "thinking" segments. Only final answer/text can leave this function.
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
    if len(text) > 12000:
        text = text[:12000]
    return text


def decode_generation(raw, prior):
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.I)
        text = re.sub(r'\s*```$', '', text)
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end+1])
            except json.JSONDecodeError:
                data = None
    if isinstance(data, dict) and isinstance(data.get('reply'), str):
        reply = data['reply'].strip()
        if not reply:
            raise RuntimeError('Seed returned an empty reply in strategy envelope.')
        return {'reply': reply, 'state': normalize_state(data.get('memory'), prior)}

    # Fail closed if it looks like a broken internal envelope, so JSON/state is never sent to a fan.
    if text.lstrip().startswith('{') or '"memory"' in text or '"next_goal"' in text:
        raise RuntimeError('Seed returned malformed strategy JSON; nothing sent.')

    # Graceful compatibility fallback: a plain reply still works, but profile state is unchanged.
    return {'reply': text, 'state': normalize_state(prior)}


def generate(messages, state, last_fanvue):
    instructions = '\n'.join(m['content'] for m in messages if m['role']=='system')
    conversation = [m for m in messages if m['role']!='system']
    runtime = """

================ RUNTIME MEMORY ================
Ispod je trajno stanje ovog korisnika. To je interna pomoć, ne tekst koji smeš da pokažeš njemu.
Ako se nova poruka kosi sa starom činjenicom, veruj novijem kontekstu i ažuriraj stanje.

PERSISTENT_STATE:
%s

================ OUTPUT CONTRACT ================
Ne prikazuj reasoning, analizu, prodajnu strategiju ni naziv poteza korisniku.
Vrati SAMO validan JSON objekat, bez markdown fence-a i bez dodatnog teksta, tačno ovog oblika:
{
  "reply": "kratka poruka koja će biti poslata korisniku",
  "memory": {
    "facts": ["do 14 kratkih, korisnih činjenica koje je korisnik dobrovoljno rekao"],
    "open_loops": ["do 6 tema koje imaju prirodan nastavak"],
    "relationship_stage": 0,
    "commercial_intent": "none",
    "fanvue_status": "unknown",
    "engagement": "medium",
    "adult_status": "unknown",
    "last_move": "casual",
    "next_goal": "rapport"
  }
}

relationship_stage: 0 stranger, 1 familiar, 2 regular, 3 flirty regular, 4 high engagement, 5 customer.
commercial_intent mora biti none/low/medium/high.
fanvue_status mora biti unknown/unaware/aware/link_sent/says_subscribed.
engagement mora biti low/medium/high.
adult_status mora biti unknown/adult/minor. Ne zaključuj adult samo iz flerta; menjaj ga kada postoje stvarni podaci o godinama.
last_move i next_goal moraju biti jedan od: casual, rapport, callback, learn, playful, flirt, tease, pull_back, entertain, reengage, handle_objection, fanvue_bridge, boundary.

"reply" je JEDINO što će korisnik videti. Memory mora biti kratka, stabilna i korisna za sledeće poteze. Ne stavljaj interno objašnjenje u reply.
""" % json.dumps(profile_for_prompt(state, last_fanvue), ensure_ascii=False)
    prompt = (instructions + runtime + '\n\nSledi skorašnja istorija razgovora kao JSON podaci. '
              'Odgovori na poslednju korisničku poruku koristeći i trajno stanje iznad.\n' +
              json.dumps(conversation, ensure_ascii=False))
    session = 'jm-' + secrets.token_hex(16)
    payload = {
        'prompt': prompt,
        'userId': session,
        'session_id': session,
        'effort': REASONING,
    }
    return decode_generation(parse_answer(wiro_request(payload)), state)


HELP = '''Wiro AI controls (owner only):
/aimodels — show fixed Seed model and reasoning effort
/ai test ID — enable only one test recipient
/ai on | /ai off | /ai status
/aipause ID — pause one chat
/airesume ID — allow future incoming messages to get AI replies
/aiforget ID — clear recent AI history + persistent strategic memory and cancel current reply
/aiprofile ID — show persistent strategic memory/state for one recipient
Writing from her account does not pause the chat.
/airesumeall — resume every paused chat
/ai queue [ID] — waiting chats and delivery diagnostics
AI replies are free text; existing paid-media commands still control PPV.'''


def process(uid,m,text):
    command, _, arg = text.partition(' ')
    if command not in ('/ai','/aimodels','/aipause','/airesume','/airesumeall','/aiforget','/aiprofile'):
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
        base.tell(uid, 'This edition uses Seed: '+MODEL+' via Wiro Run API. Reasoning effort: '+REASONING+'.')
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
                  '\nReasoning effort: '+REASONING+
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

    if command == '/aiprofile':
        state,last = load_profile(*pair)
        info = profile_for_prompt(state,last)
        base.tell(uid, 'Profile '+key+':\n'+json.dumps(info,ensure_ascii=False,indent=2)[:3500])
        return

    cancel(*pair)
    with base.DB:
        base.DB.execute('INSERT OR IGNORE INTO ai_controls(cid,chat) VALUES (?,?)',pair)
        if command == '/aiforget':
            base.DB.execute('DELETE FROM ai_history WHERE cid=? AND chat=?',pair)
            base.DB.execute('DELETE FROM ai_profiles WHERE cid=? AND chat=?',pair)
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
    backup = Path(path) / 'manager-before-strategy-v6.sqlite3'
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
    CREATE TABLE IF NOT EXISTS ai_profiles (
        cid TEXT, chat TEXT, state TEXT NOT NULL DEFAULT '{}',
        last_fanvue REAL DEFAULT 0, updated REAL,
        PRIMARY KEY(cid,chat));
    ''')
    with base.DB:
        base.DB.execute("UPDATE ai_q5_jobs SET status='interrupted', detail='Restart during external request; not retried' WHERE status IN ('generating','sending')")
        base.DB.execute('DELETE FROM ai_q5_inbox WHERE job IS NOT NULL')
        if base.get('ai_adapter_version') != 'seed-strategy-v6':
            for cid, chat, mid in base.DB.execute("SELECT cid,chat,version FROM ai_queue WHERE status='pending'").fetchall():
                row = base.DB.execute('SELECT body FROM ai_history WHERE cid=? AND chat=? AND mid=? AND role=?', (cid,chat,mid,'user')).fetchone()
                if row:
                    base.DB.execute('INSERT OR IGNORE INTO ai_q5_inbox(cid,chat,mid,body,arrived) VALUES (?,?,?,?,?)', (cid,chat,mid,row[0],time.time()))
        base.DB.execute("UPDATE ai_queue SET status='interrupted' WHERE status IN ('pending','generating','sending')")
        base.DB.execute('DELETE FROM ai_q5_jobs WHERE updated<?', (time.time()-7*86400,))
        base.DB.execute("DELETE FROM ai_q5_events WHERE status='blocked' AND received<?", (time.time()-7*86400,))
    base.put('ai_adapter_version', 'seed-strategy-v6')


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
        cancel(cid,key)
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
        if base.DB.execute("SELECT 1 FROM ai_q5_events WHERE cid=? AND chat=? AND status='ready' AND rowid < (SELECT rowid FROM ai_q5_events WHERE uid=?) LIMIT 1", (cid,key,uid)).fetchone():
            continue
        try:
            sender = m.get('from', {})
            if sender.get('id') == base.OWNER:
                cached_connection(cid)
                if m.get('text'):
                    history(cid,key,m['message_id'],'assistant',m['text'])
            else:
                OLD_HANDLE(update)
                body = m.get('text','').strip()
                if body.lower() == '/stop':
                    cancel(cid,key)
                elif (body and not body.startswith('/') and not sender.get('is_bot')
                      and base.OWNER and reply_ok and allowed(cid,key)):
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
            generated = future.result()
            raw_reply = generated['reply'].strip()
            text = outgoing(raw_reply)
            if not text:
                raise RuntimeError('Empty outbound reply; nothing sent.')
            if len(text) > 2500:
                text = text[:2400].rsplit(' ', 1)[0]
            base.eligible(cached_connection(cid),base.get('chats',{})[key])
            job_status(token,'sending')
            result = base.api('sendMessage', business_connection_id=cid,chat_id=int(key),text=text)
            job_status(token,'sent')
            # Store the model form with [FANVUE], not the expanded URL, so future prompts never
            # learn to type the raw link themselves.
            history(cid,key,result['message_id'],'assistant',raw_reply)
            sent_link = '[FANVUE]' in raw_reply
            state = normalize_state(generated.get('state'))
            if sent_link:
                state['fanvue_status'] = 'link_sent' if state.get('fanvue_status') != 'says_subscribed' else 'says_subscribed'
            save_profile(cid,key,state,time.time() if sent_link else None)
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

    rows = base.DB.execute('''SELECT cid,chat,MIN(arrived),MAX(arrived) FROM ai_q5_inbox
        WHERE job IS NULL GROUP BY cid,chat ORDER BY MIN(seq)''').fetchall()
    day = time.strftime('%Y-%m-%d',time.gmtime())
    for cid,key,first,last in rows:
        if len(RUNNING) >= 1:
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
        if base.DB.execute("SELECT 1 FROM ai_q5_events WHERE cid=? AND chat=? AND status='ready' LIMIT 1",(cid,key)).fetchone():
            continue
        items = base.DB.execute('SELECT seq,mid,body FROM ai_q5_inbox WHERE cid=? AND chat=? AND job IS NULL ORDER BY seq',(cid,key)).fetchall()
        messages = messages_for(cid,key,items)
        state,last_fanvue = load_profile(cid,key)
        token = secrets.token_hex(16)
        with base.DB:
            base.DB.execute('INSERT INTO ai_usage VALUES (?,?,1) ON CONFLICT(day,chat) DO UPDATE SET count=count+1',(day,key))
            base.DB.execute('INSERT INTO ai_q5_jobs VALUES (?,?,?,?,?,?)',(token,cid,key,'generating',time.time(),str(len(items))+' incoming messages grouped'))
            base.DB.executemany('UPDATE ai_q5_inbox SET job=? WHERE seq=?',[(token,item[0]) for item in items])
        try:
            RUNNING[(cid,key)] = (token,POOL.submit(generate,messages,state,last_fanvue))
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
    return ('Strategy v6 | workers: '+str(len(RUNNING))+'/1 | reasoning: '+REASONING+' | today: '+str(used)+'/'+str(DAILY)+' attempts\n'
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
