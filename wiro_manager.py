"""Grok option 4 add-on for the supplied Wiro strategic-memory v6.2 manager.

SETUP
1. Keep the WORKING original manager as wiro_manager.py, alongside bot.py.
2. Put this file beside both files.
3. Change the Railway start command to: python wiro_grok.py
4. Keep WIRO_API_KEY and your other variables. No xAI key is needed.
5. Send /ai4, then /ai test ID to test one recipient; /ai on enables all.

/ai1, /ai2, /ai3, /ai4 and /aimodel 1|2|3|4 select the next generation.
Selection persists in the original SQLite database. AI_MODEL_SLOT=4 is an
optional default for databases without a saved selection.

Uses Grok 4.1 Fast through Wiro Run/sync. Grok receives no effort parameter;
WIRO_REASONING_EFFORT continues to apply to models 1-3 only. Grok uses its
provider default reasoning mode. No extra requests, retries or dependencies.
The original prompt, history, profile, limits, queue and delivery code run as-is.

Requires the working, correctly indented original Python file, not the damaged
plain-text chat paste. Its main guard must be: if __name__ == '__main__':

Reference: https://wiro.ai/models/xai/grok-4-1-fast
"""

import os
import wiro_manager as manager


manager.MODELS['4'] = {
    'name': 'Grok 4.1 Fast',
    'slug': 'xai/grok-4-1-fast',
    'user_field': 'user_id',
}

# The original module validates the default before this add-on registers slot 4.
requested_default = os.getenv('AI_MODEL_SLOT', '1').strip()
if requested_default in manager.MODELS:
    manager.DEFAULT_MODEL_SLOT = requested_default

_original_request = manager.wiro_request
_original_process = manager.process
_original_help = manager.HELP


def request(payload, model_slot):
    if model_slot == '4':
        payload = dict(payload)
        payload.pop('effort', None)
    return _original_request(payload, model_slot)


def reasoning_label(slot):
    if slot == '4':
        return 'provider default (Grok has no effort dial)'
    return manager.REASONING


def model_list_text():
    current = manager.active_model_slot()
    lines = ['AI MODELS — one active at a time:']
    for slot, model in manager.MODELS.items():
        marker = '  ← ACTIVE' if slot == current else ''
        lines.append(slot + '. ' + model['name'] + '\n   ' + model['slug'] + marker)
    lines.extend([
        '', 'Switch: /ai1 /ai2 /ai3 /ai4',
        'or: /aimodel 1|2|3|4',
        'Active reasoning: ' + reasoning_label(current),
    ])
    return '\n'.join(lines)


def process(uid, message, text):
    command, _, arg = text.partition(' ')
    switches = {'/ai' + slot: slot for slot in manager.MODELS}
    if command not in switches and command != '/aimodel':
        return _original_process(uid, message, text)
    if uid != manager.base.OWNER:
        return
    slot = switches.get(command, arg.strip())
    if slot not in manager.MODELS:
        raise ValueError('Use /aimodel 1, /aimodel 2, /aimodel 3 or /aimodel 4.')
    manager.base.put('ai_model_slot', slot)
    model = manager.MODELS[slot]
    manager.base.tell(uid,
        'AI model switched to ' + slot + ': ' + model['name'] +
        '\n' + model['slug'] +
        '\nReasoning: ' + reasoning_label(slot) +
        '\nNext generation will use this model.')


manager.HELP = _original_help.replace('1/2/3', '1/2/3/4').replace(
    '/aimodel 1|2|3', '/aimodel 1|2|3|4'
).replace(
    '/ai3 — switch to model 3: GPT-6 Luna',
    '/ai3 — switch to model 3: GPT-6 Luna\n/ai4 — switch to model 4: Grok 4.1 Fast'
) + '\nReasoning effort shown in legacy status/queue applies only to slots 1–3. Grok uses provider defaults.'

manager.base.HELP = manager.base.HELP.replace(_original_help, manager.HELP)
manager.wiro_request = request
manager.model_list_text = model_list_text
manager.process = process
manager.base.process = process


if __name__ == '__main__':
    try:
        manager.base.main()
    finally:
        manager.POOL.shutdown(wait=False, cancel_futures=True)
