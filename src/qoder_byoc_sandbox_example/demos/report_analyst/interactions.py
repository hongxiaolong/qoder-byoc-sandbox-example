"""Component handoffs shared by live observation, replay and manual selection.

An ordinal describes a directed transfer between two diagram components. The
public events remain evidence, not one ordinal per log row. Internal computation
and Work lease traffic never manufacture a component handoff.
"""
import json

TOOL_REQUESTS = {'agent.tool_use', 'agent.custom_tool_use'}
TOOL_RESULTS = {'user.tool_result', 'user.custom_tool_result', 'agent.tool_result'}
ACTIVATIONS = {'session.status_running', 'session.status_busy'}
MODEL_STARTS = {'span.model_request_start', 'session.model_request_start', 'agent.model_request_start', 'model_request_start'}
TOOL_PHASES = TOOL_REQUESTS | TOOL_RESULTS | {
    'tool_received', 'tool_started', 'tool_execution', 'tool_stage', 'tool_cancelled',
}
FLOW_KINDS = TOOL_PHASES | ACTIVATIONS | MODEL_STARTS | {'user.message', 'agent.message', 'user.interrupt', 'session.status_idle'}
TERMINAL = {'completed', 'failed', 'error', 'posted', 'denied'}

# Endpoint names match the leaf components in the diagram. Environment and
# Sandbox are grouping boundaries; they never add an extra hop by themselves.
ROUTES = {
    'input': ('input', 'session', 'input', '会话输入 → Session'),
    'activate': ('session', 'loop', 'model', 'Session → Agent Loop · 开始处理'),
    'resume': ('events', 'loop', 'events-loop', 'Events API → Agent Loop · 继续处理工具结果'),
    'request': ('loop', 'events', 'loop-events', 'Agent Loop → Events API · 发布工具调用'),
    'receive': ('events', 'worker', 'request', 'Events API → Worker · 交付工具调用'),
    'python-call': ('worker', 'python', 'python', 'Worker → Python · 提交代码'),
    'python-return': ('python', 'worker', 'python-result', 'Python → Worker · 返回执行结果'),
    'result': ('worker', 'events', 'result', 'Worker → Events API · 回传工具结果'),
    'reply': ('loop', 'session', 'reply', 'Agent Loop → Session · 回复消息'),
    'interrupt': ('input', 'session', 'input', '会话输入 → Session · 中断请求'),
    'browser': ('worker', 'browser', 'cdp', 'Worker → Browser · 网页采集'),
    'csv_transfer': ('browser', 'worker', 'csv', 'Browser → Worker · 传输 CSV'),
}
STAGE_ROUTES = {key: 'browser' for key in ('browser_connect', 'browser_filter', 'browser_download')}
STAGE_ROUTES['csv_transfer'] = 'csv_transfer'
STAGE_ROUTES.update(python_dispatch='python-call', python_return='python-return')


def interaction_catalog(db, events, *, complete=False):
    """Read QCA identities before selecting the bounded visible window."""
    messages = {event.get('message_id') for event in events if event.get('message_id')}
    messages.update(event['id'] for event in events if event.get('type') == 'user.message')
    sources = {event.get('id') for event in events}
    numbers = dict(db.execute("""SELECT id, row_number() OVER (ORDER BY seq) FROM events
        WHERE json_extract(payload,'$.type')='user.message'"""))
    grouped, rounds, first_tools, publication = {}, {}, {}, {}
    records = [(seq, identifier, json.loads(payload)) for seq, identifier, payload in
               db.execute('SELECT seq,id,payload FROM events ORDER BY seq')]
    # requires_action supplies a proven batch even in older logs without model
    # markers. Do not group requests merely because their timestamps are close.
    order = {identifier: seq for seq, identifier, _ in records}
    batches = {}
    for _, _, record in records:
        if record.get('type') == 'session.status_idle' and record.get('reason') == 'requires_action':
            tools = [tool for tool in record.get('tool_ids', []) if tool in order]
            if tools:
                batch = min(tools, key=order.get)
                for tool in tools:
                    batches.setdefault(tool, batch)
    for seq, identifier, source in records:
        kind = source.get('type')
        message = identifier if kind == 'user.message' else source.get('message_id')
        if message not in messages or message not in numbers or kind not in FLOW_KINDS:
            continue
        if kind in MODEL_STARTS:
            rounds[message] = identifier
            publication.pop(message, None)
        if kind == 'agent.message' and not source.get('text', '').strip():
            continue
        entry = grouped.setdefault(message, dict(message_id=message, number=numbers[message], steps=[]))
        if not complete and identifier not in sources:
            continue
        tool = identifier if kind in TOOL_REQUESTS else source.get('tool_use_id')
        request_group = None
        if kind in TOOL_REQUESTS:
            # The first request owns the publication identity from the outset.
            # Without model markers, consecutive calls before any result form
            # one publication phase; later requires_action metadata adds proof
            # and members without replacing an already-visible identity.
            group = rounds.get(message) or batches.get(tool) or publication.setdefault(message, identifier)
            request_group = first_tools.setdefault((message, group), identifier)
        elif kind in TOOL_RESULTS:
            rounds.pop(message, None)
            publication.pop(message, None)
        entry['steps'].append(dict(
            source_kind=kind, source_sequence=seq, tool_id=tool,
            request_group=request_group, reason=source.get('reason'),
            event_ids=['qca:' + identifier],
        ))
    return {'version': 4, 'conversations': sorted(grouped.values(), key=lambda item: item['number'])}


def _candidates(data, catalog):
    known = {event['id']: event for event in data.get('_flow_events', [])}
    known.update({event['id']: event for event in data.get('events', [])})
    messages = {entry['message_id'] for entry in catalog}
    owners, requests, results, request_keys = {}, {}, {}, {}
    completed_python = set()
    candidates, positions = {}, {}

    def add(message, key, route, event, position):
        if message not in messages:
            return
        identity = (message, key)
        if identity in candidates:
            current = candidates[identity]
            current['event_ids'] = list(dict.fromkeys(current['event_ids'] + event['event_ids']))
            current['tool_ids'] = list(dict.fromkeys(current['tool_ids'] + ([event['tool_id']] if event.get('tool_id') else [])))
            current['tool_id'] = current['tool_ids'][0] if len(current['tool_ids']) == 1 else None
            return
        source, target, flow, label = ROUTES[route]
        candidates[identity] = dict(
            id=key, kind='interaction', source_kind=event['source_kind'],
            tool_id=event.get('tool_id'), tool_ids=[event['tool_id']] if event.get('tool_id') else [],
            source=source, target=target, flow=flow,
            label=label, event_ids=list(event['event_ids']),
        )
        positions[identity] = position

    # QCA's saved order anchors cloud events. It is not a cross-host clock.
    for entry in catalog:
        message = entry['message_id']
        acted, started, closed, fresh_results = False, False, False, False
        for event in entry['steps']:
            kind, tool, seq = event['source_kind'], event.get('tool_id'), event['source_sequence']
            if tool:
                owners[tool] = message
            if kind in TOOL_REQUESTS:
                requests[tool] = seq
                acted = True
                request_keys[tool] = 'request:' + (event.get('request_group') or tool)
                add(message, request_keys[tool], 'request', event, (seq, 0, 0, 0))
            elif kind in TOOL_RESULTS:
                results[tool] = seq
                fresh_results = True
                acted = True
                add(message, 'result:' + str(tool or event['event_ids'][0]), 'result', event, (seq, 0, 0, 0))
            elif kind in MODEL_STARTS and not closed:
                if fresh_results:
                    add(message, 'resume:' + event['event_ids'][0], 'resume', event, (seq, 0, 0, 0))
                    fresh_results = False
                    started = True
                elif not started and not acted:
                    add(message, 'activate:' + message, 'activate', event, (seq, 0, 0, 0))
                    started = True
            elif kind in ACTIVATIONS and not started and not acted and not closed:
                add(message, 'activate:' + message, 'activate', event, (seq, 0, 0, 0))
                started = True
            elif kind == 'session.status_idle':
                closed = event.get('reason') not in {'requires_action', 'requires_permission'}
            elif kind in {'user.message', 'agent.message', 'user.interrupt'}:
                route = {'user.message': 'input', 'agent.message': 'reply', 'user.interrupt': 'interrupt'}[kind]
                if kind == 'agent.message':
                    acted = True
                add(message, route + ':' + event['event_ids'][0], route, event, (seq, 0, 0, 0))

    for call in data.get('execution', {}).get('events', []):
        if call.get('message_id') in messages:
            owners.setdefault(call['id'], call['message_id'])
        # Old Workers did not record interpreter dispatch. An actual subprocess
        # exit-code response proves execution; tool name or validation errors do not.
        if call.get('tool') == 'run_python' and str(call.get('output', '')).startswith('exit_code='):
            completed_python.add(call['id'])

    facts = [event for event in known.values() if event.get('kind') in TOOL_PHASES]
    facts.sort(key=lambda event: (
        event.get('time') is None, event.get('time') or event.get('observed_at') or 0, event['id'],
    ))
    explicit_dispatch = {e.get('tool_id') for e in facts if e.get('stage') == 'python_dispatch'}
    explicit_return = {e.get('tool_id') for e in facts if e.get('stage') == 'python_return'}
    for index, raw in enumerate(facts):
        kind, tool = raw['kind'], raw.get('tool_id')
        if not tool:
            continue
        message = owners.get(tool) or raw.get('message_id')
        fact = dict(source_kind=kind, tool_id=tool, event_ids=[raw['id']])
        anchor = results.get(tool, requests.get(tool, 0) + 1)
        if kind in TOOL_REQUESTS:
            add(message, request_keys.get(tool, 'request:' + tool), 'request', fact, (requests.get(tool, anchor), 0, 0, index))
        elif kind in TOOL_RESULTS:
            add(message, 'result:' + tool, 'result', fact, (anchor, 0, 0, index))
        elif kind in {'tool_received', 'tool_started'}:
            add(message, 'receive:' + tool, 'receive', fact, (anchor, -1, 0, index))
        elif kind == 'tool_execution' and tool in completed_python and raw.get('state') in TERMINAL:
            # Both legacy boundaries share a completed fact. Version 4 playback
            # keeps distinct interaction identities, never claims an earlier live dispatch.
            if tool not in explicit_dispatch:
                add(message, 'python-call:' + tool, 'python-call', fact, (anchor, -1, 1, index))
            if tool not in explicit_return:
                add(message, 'python-return:' + tool, 'python-return', fact, (anchor, -1, 3, index))
        elif kind == 'tool_stage' and raw.get('stage') in STAGE_ROUTES:
            route = STAGE_ROUTES[raw['stage']]
            python = route in {'python-call', 'python-return'}
            key = route + ':' + tool if python or route == 'browser' else 'stage:' + raw['id']
            phase = 1 if route == 'python-call' else 3 if route == 'python-return' else 2
            add(message, key, route, fact, (anchor, -1, phase, index))

    return [(identity, candidates[identity]) for identity in sorted(candidates, key=lambda key: positions[key])]


def _worker_evidence(db, data, catalog):
    """Keep bounded handoff metadata after the Worker's rolling log drops it.

    This is source evidence, separate from derived ordinals. Never retain task
    bodies, credentials, progress chatter or heartbeat records here.
    """
    db.execute('''CREATE TABLE IF NOT EXISTS component_worker_facts (
        event_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, payload TEXT NOT NULL
    )''')
    messages = {entry['message_id'] for entry in catalog}
    owners = {event['tool_id']: entry['message_id'] for entry in catalog
              for event in entry['steps'] if event.get('tool_id')}
    known = {event['id']: event for event in data.get('_flow_events', [])}
    known.update({event['id']: event for event in data.get('events', [])})
    fields = ('id', 'kind', 'category', 'role', 'message_id', 'tool_id', 'work_id',
              'state', 'time', 'observed_at', 'label', 'stage')
    for event in known.values():
        kind = event.get('kind')
        if kind not in {'tool_received', 'tool_started', 'tool_execution', 'tool_stage'}:
            continue
        if kind == 'tool_stage' and event.get('stage') not in STAGE_ROUTES:
            continue
        message = owners.get(event.get('tool_id')) or event.get('message_id')
        if message not in messages:
            continue
        fact = {key: event[key] for key in fields if event.get(key) is not None}
        fact.update(message_id=message, category='tool')
        db.execute('INSERT OR IGNORE INTO component_worker_facts VALUES (?,?,?)',
                   (event['id'], message, json.dumps(fact)))
    count = db.execute('SELECT count(*) FROM component_worker_facts').fetchone()[0]
    if count > 2000:
        db.execute('''DELETE FROM component_worker_facts WHERE rowid NOT IN
            (SELECT rowid FROM component_worker_facts ORDER BY rowid DESC LIMIT 2000)''')
        data['history_incomplete'] = True
    restored = []
    visible = {event['id'] for event in data.get('events', [])}
    for (payload,) in db.execute('SELECT payload FROM component_worker_facts ORDER BY rowid'):
        fact = json.loads(payload)
        if fact['message_id'] in messages and fact['id'] not in visible:
            restored.append(dict(fact, saved_evidence=True))
    if restored:
        data['events'] = sorted(data.get('events', []) + restored,
                              key=lambda event: event.get('time') or event.get('observed_at') or 0)
        # Restore trace rows too, without re-projecting or replacing the shared
        # ordinal ledger. Missing original task bodies remain explicitly absent.
        from .tracing import build_tracing
        data['tracing'] = build_tracing(data, data.get('api_trace'))


def _persist(db, candidates, messages):
    """Version 4 has its own ledger; old per-event ordinals cannot leak into it."""
    db.executescript('''
        CREATE TABLE IF NOT EXISTS component_v4_steps (
            message_id TEXT NOT NULL, step_key TEXT NOT NULL, number INTEGER NOT NULL,
            source_kind TEXT NOT NULL, tool_id TEXT, source TEXT NOT NULL,
            target TEXT NOT NULL, flow TEXT NOT NULL, label TEXT NOT NULL, tool_ids TEXT NOT NULL,
            PRIMARY KEY(message_id,step_key), UNIQUE(message_id,number)
        );
        CREATE TABLE IF NOT EXISTS component_v4_event_ids (
            event_id TEXT NOT NULL, message_id TEXT NOT NULL, step_key TEXT NOT NULL,
            PRIMARY KEY(event_id,message_id,step_key)
        );
    ''')
    db.execute('BEGIN IMMEDIATE')
    revision = db.execute("SELECT value FROM meta WHERE key='component_projection_revision'").fetchone()
    if revision is None or revision[0] != '4.2':
        # Only derived projection metadata is invalidated. QCA events, Worker
        # records, submissions and resource ownership remain untouched.
        db.execute('DELETE FROM component_v4_event_ids')
        db.execute('DELETE FROM component_v4_steps')
        db.execute("INSERT OR REPLACE INTO meta VALUES ('component_projection_revision','4.2')")
    for (message, key), event in candidates:
        number = db.execute('SELECT coalesce(max(number),0)+1 FROM component_v4_steps WHERE message_id=?',
                            (message,)).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO component_v4_steps VALUES (?,?,?,?,?,?,?,?,?,?)',
            (message, key, number, event['source_kind'], event.get('tool_id'),
             event['source'], event['target'], event['flow'], event['label'], json.dumps(event['tool_ids'])))
        prior = json.loads(db.execute('SELECT tool_ids FROM component_v4_steps WHERE message_id=? AND step_key=?', (message,key)).fetchone()[0])
        combined = list(dict.fromkeys(prior + event['tool_ids']))
        db.execute('UPDATE component_v4_steps SET tool_ids=?, tool_id=? WHERE message_id=? AND step_key=?',
                   (json.dumps(combined), combined[0] if len(combined)==1 else None, message,key))
        for identifier in event['event_ids']:
            db.execute('INSERT OR IGNORE INTO component_v4_event_ids VALUES (?,?,?)', (identifier, message, key))
    output = {}
    for message in messages:
        output[message] = []
        rows = db.execute('''SELECT step_key,number,source_kind,tool_id,source,target,flow,label,tool_ids
            FROM component_v4_steps WHERE message_id=? ORDER BY number''', (message,))
        for key, number, kind, tool, source, target, flow, label, tool_ids in rows:
            aliases = db.execute('''SELECT event_id FROM component_v4_event_ids
                WHERE message_id=? AND step_key=? ORDER BY rowid''', (message, key))
            output[message].append(dict(
                id=key, number=number, kind='interaction', source_kind=kind, tool_id=tool,
                source=source, target=target, flow=flow, label=label, tool_ids=json.loads(tool_ids), event_ids=[row[0] for row in aliases],
            ))
    return output


def project_interactions(data, db=None):
    """Project observed component boundaries, excluding internal state changes."""
    catalog = data.get('session', {}).get('interaction_catalog', {}).get('conversations', [])
    if db is not None:
        catalog = interaction_catalog(db, [{'message_id': entry['message_id']} for entry in catalog],
                                      complete=True)['conversations']
        _worker_evidence(db, data, catalog)
    candidates = _candidates(data, catalog)
    visible = {event['id'] for event in data.get('events', [])}
    if db is not None:
        steps = _persist(db, candidates, [entry['message_id'] for entry in catalog])
    else:
        steps = {entry['message_id']: [] for entry in catalog}
        for (message, _), event in candidates:
            steps[message].append(dict(event, number=len(steps[message]) + 1))
    conversations = []
    for entry in catalog:
        items = steps[entry['message_id']]
        loaded = [step for step in items if visible.intersection(step['event_ids'])]
        conversations.append(dict(
            message_id=entry['message_id'], number=entry['number'], total_steps=len(items), steps=loaded,
            partial=len(loaded) != len(items) or bool(data.get('history_incomplete')),
        ))
    return {'version': 4, 'conversations': conversations}
