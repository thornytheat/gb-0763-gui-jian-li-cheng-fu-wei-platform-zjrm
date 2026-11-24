from collections import Counter, defaultdict
from datetime import datetime

from app.config import GAP_SECONDS
from app.manifest import canonical, digest
from app.reasons import REASONS

LOSING = ('UNKNOWN_MARK', 'ANCHOR_TIE')
OWNERS = {'capture': '报文采信岗', 'anchors': '基点复核岗',
          'chainage': '区间校准岗', 'coverage': '覆盖对账岗'}
STATUS_LABEL = {'isolated': '隔离报文', 'duplicate': '重复报文', 'matched': '采纳基点',
                'discarded': '基点取舍', 'projected': '已复位', 'unmapped': '无法复位'}


def handover(captured, anchors, located, coverage):
    """交班台账：每行报文唯一处置结论，主因串起受影响缺陷与缺证片段。

    断档先归因于窗口内丢失校准的上游失败（基点取舍、锚点冲突），查无上游
    主因才自立主因；下游未复位缺陷与覆盖缺证只记入影响范围，不再单独立案。
    台账是全量输入的纯函数，迟到记录到达后重算即更新主因与影响范围。
    """
    events_by_line = {event['line']: event for event in captured['events']}
    causes, lost, line_cause = [], [], {}

    def register(stage, reason, sources, scope=''):
        key = {'stage': stage, 'reason': reason, 'sources': sorted(sources), 'scope': scope}
        cause = {'cause_id': 'C-' + digest(canonical(key))[:12], 'reason': reason,
                 'label': REASONS[reason], 'stage': stage, 'owner': OWNERS[stage],
                 'sources': sorted(sources), 'affects': [], 'affect_lines': [], 'gaps': []}
        causes.append(cause)
        return cause

    def affect(cause, item):
        line = item['raw']['line']
        cause['affects'].append(item['event_id'])
        cause['affect_lines'].append(line)
        line_cause[line] = cause

    # 采信段：坏报文逐条立案，同编号冲突并为一案；冲突丢掉的锚点记为丢失校准。
    conflicts, singles = defaultdict(list), []
    for reject in captured['rejects']:
        raw = reject['raw']
        if reject['reason'] == 'ID_CONFLICT' and isinstance(raw, dict):
            conflicts[raw['event_id']].append(reject)
        else:
            singles.append(reject)
    for reject in singles:
        cause = register('capture', reject['reason'], [reject['line']])
        line_cause[reject['line']] = cause
    for group in conflicts.values():
        lines = [reject['line'] for reject in group]
        cause = register('capture', 'ID_CONFLICT', lines)
        for reject in group:
            line_cause[reject['line']] = cause
        anchors_lost = [reject['raw'] for reject in group
                        if isinstance(reject['raw'], dict) and reject['raw'].get('kind') == 'anchor']
        if anchors_lost:
            first = min(anchors_lost, key=lambda event: event['at'])
            lost.append((first['run'], datetime.fromisoformat(first['at']), cause))

    # 基点段：丢失校准的取舍立案并登记丢失时刻；等级让位与完全重复不立案。
    for item in anchors['discarded']:
        if item['reason'] not in LOSING:
            continue
        cause = register('anchors', item['reason'], item['sources'])
        for line in item['sources']:
            line_cause[line] = cause
            event = events_by_line.get(line)
            if event is not None:
                lost.append((event['run'], datetime.fromisoformat(event['at']), cause))

    # 区间段：每条断档先找窗口内丢失校准的上游主因。
    gap_owner = {}
    for gap in located['gaps']:
        left = events_by_line[gap['sources'][0]]
        right = events_by_line[gap['sources'][1]]
        lo = datetime.fromisoformat(left['at'])
        hi = datetime.fromisoformat(right['at'])
        window = [entry for entry in lost if entry[0] == left['run'] and lo < entry[1] < hi]
        if window:
            gap_owner[tuple(gap['sources'])] = min(window, key=lambda entry: (entry[1], entry[2]['cause_id']))[2]
        else:
            gap_owner[tuple(gap['sources'])] = None

    def ensure_gap_cause(key):
        if gap_owner[key] is None:
            gap = next(item for item in located['gaps'] if tuple(item['sources']) == key)
            gap_owner[key] = register('chainage', gap['reason'], gap['sources'])
        return gap_owner[key]

    # 未复位缺陷：继承断档原因的挂到断档（或其上游主因）名下，其余逐条立案。
    bracketless, own = [], []
    for item in located['unmapped']:
        span = tuple(item['sources'][1:])
        if len(span) == 2 and span in gap_owner:
            affect(ensure_gap_cause(span), item)
        elif item['reason'] == 'NO_BRACKET':
            bracketless.append(item)
        else:
            own.append(item)
    for item in bracketless:
        event = item['raw']
        at = datetime.fromisoformat(event['at'])
        near = [entry for entry in lost
                if entry[0] == event['run'] and abs((entry[1] - at).total_seconds()) <= GAP_SECONDS]
        if near:
            affect(min(near, key=lambda entry: (abs((entry[1] - at).total_seconds()), entry[2]['cause_id']))[2], item)
        else:
            own.append(item)
    for item in own:
        cause = register('chainage', item['reason'], [item['raw']['line']])
        affect(cause, item)

    # 覆盖段：缺证片段挂到相交断档的主因名下，无断档可指才自立主因。
    missing_parts = []
    for part in coverage['missing_parts']:
        keys = [key for key in gap_owner if set(key) <= set(part['sources'])]
        linked = []
        if keys:
            for key in keys:
                cause = ensure_gap_cause(key)
                extent = {'chain': part['chain'], 'start_mm': part['start_mm'], 'end_mm': part['end_mm']}
                if extent not in cause['gaps']:
                    cause['gaps'].append(extent)
                linked.append(cause['cause_id'])
        else:
            cause = register('coverage', 'NO_COVERAGE', [],
                             scope=f"{part['chain']}:{part['start_mm']}:{part['end_mm']}")
            cause['gaps'].append({'chain': part['chain'], 'start_mm': part['start_mm'],
                                  'end_mm': part['end_mm']})
            linked.append(cause['cause_id'])
        missing_parts.append(dict(part, causes=linked))

    for cause in causes:
        pairs = sorted(zip(cause['affect_lines'], cause['affects']))
        cause['affect_lines'] = [line for line, _ in pairs]
        cause['affects'] = [event_id for _, event_id in pairs]
    causes.sort(key=lambda cause: (min(cause['sources'], default=float('inf')),
                                   cause['reason'], cause['cause_id']))

    # 逐行处置结论：每份原始报文有且只有一条。
    repeated = {item['line']: item['kept_line'] for item in captured['repeated']}
    discarded = {}
    for item in anchors['discarded']:
        lines = item['sources'] if item['reason'] == 'ANCHOR_TIE' else item['sources'][:1]
        for line in lines:
            discarded[line] = item['reason']
    matched = {anchor['line']: anchor for anchor in anchors['anchors']}
    projected = {point['line']: point for point in located['projected']}
    unmapped = {item['raw']['line']: item for item in located['unmapped']}
    dispositions = []
    for reject in captured['rejects']:
        raw = reject['raw'] if isinstance(reject['raw'], dict) else {}
        cause = line_cause[reject['line']]
        dispositions.append({'line': reject['line'], 'event_id': raw.get('event_id'),
                             'run': raw.get('run'), 'kind': raw.get('kind'),
                             'status': 'isolated', 'label': STATUS_LABEL['isolated'],
                             'reason': reject['reason'], 'cause': cause['cause_id'],
                             'owner': cause['owner'], 'detail': None})
    for line, kept in repeated.items():
        event = events_by_line[kept]
        dispositions.append({'line': line, 'event_id': event['event_id'], 'run': event['run'],
                             'kind': event['kind'], 'status': 'duplicate',
                             'label': STATUS_LABEL['duplicate'], 'reason': None, 'cause': None,
                             'owner': None, 'detail': f'与 {kept} 行重复'})
    for event in captured['events']:
        line = event['line']
        base = {'line': line, 'event_id': event['event_id'], 'run': event['run'], 'kind': event['kind']}
        detail = f"迟到 {event['late_s']} s" if event.get('late_s') else None
        if line in matched:
            anchor = matched[line]
            note = f"{anchor['chain']} {anchor['station_mm']} mm"
            dispositions.append(dict(base, status='matched', label=STATUS_LABEL['matched'],
                                     reason=None, cause=None, owner=None,
                                     detail=note + (f" · {detail}" if detail else '')))
        elif event['kind'] == 'anchor':
            reason = discarded[line]
            cause = line_cause.get(line)
            dispositions.append(dict(base, status='discarded', label=STATUS_LABEL['discarded'],
                                     reason=reason, cause=cause and cause['cause_id'],
                                     owner=cause and cause['owner'], detail=detail))
        elif line in projected:
            point = projected[line]
            dispositions.append(dict(base, status='projected', label=STATUS_LABEL['projected'],
                                     reason=None, cause=None, owner=None,
                                     detail=f"{point['chain']} {point['station_mm']} mm"
                                            + (f" · {detail}" if detail else '')))
        else:
            item = unmapped[line]
            cause = line_cause[line]
            dispositions.append(dict(base, status='unmapped', label=STATUS_LABEL['unmapped'],
                                     reason=item['reason'], cause=cause['cause_id'],
                                     owner=cause['owner'], detail=detail))
    dispositions.sort(key=lambda row: row['line'])
    counts = Counter(row['status'] for row in dispositions)
    return {'rows': len(dispositions), 'dispositions': dispositions, 'causes': causes,
            'counts': dict(counts), 'open_causes': len(causes), 'missing_parts': missing_parts}
