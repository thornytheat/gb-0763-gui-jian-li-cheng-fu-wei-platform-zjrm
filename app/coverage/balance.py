from collections import defaultdict

from .intervals import intersect, measure, subtract, union


def balance(plan, segments, gaps):
    planned_by_chain = defaultdict(list)
    for item in plan:
        planned_by_chain[item['chain']].append((item['start_mm'], item['end_mm']))
    usable_by_chain = defaultdict(list)
    for span in segments:
        lo = min(span['left']['station_mm'], span['right']['station_mm'])
        hi = max(span['left']['station_mm'], span['right']['station_mm'])
        usable_by_chain[span['left']['chain']].append((lo, hi))
    tracks, missing_parts = [], []
    totals = {'planned_mm': 0, 'mapped_mm': 0, 'overlap_mm': 0}
    for chain, spans in planned_by_chain.items():
        planned = union(spans)
        usable = union(usable_by_chain.get(chain, ()))
        mapped = intersect(planned, usable)
        clipped_raw = sum(measure(intersect([span], planned)) for span in usable_by_chain.get(chain, ()))
        row = {'chain': chain, 'planned_mm': measure(planned), 'mapped_mm': measure(mapped),
               'start_mm': planned[0][0], 'end_mm': planned[-1][1],
               'mapped_parts': [tuple(part) for part in mapped]}
        tracks.append(row)
        totals['planned_mm'] += row['planned_mm']
        totals['mapped_mm'] += row['mapped_mm']
        totals['overlap_mm'] += clipped_raw - row['mapped_mm']
        for lo, hi in subtract(planned, usable):
            reasons, sources = [], []
            for gap in gaps:
                gap_lo = min(gap['start_mm'], gap['end_mm'])
                gap_hi = max(gap['start_mm'], gap['end_mm'])
                if chain in (gap['chain'], gap.get('next_chain')) and gap_lo < hi and lo < gap_hi:
                    if gap['reason'] not in reasons:
                        reasons.append(gap['reason'])
                    sources.extend(gap['sources'])
            missing_parts.append({'chain': chain, 'start_mm': lo, 'end_mm': hi,
                                  'reasons': reasons or ['NO_COVERAGE'], 'sources': sources})
    return dict(totals, missing_mm=totals['planned_mm'] - totals['mapped_mm'],
                tracks=tracks, missing_parts=missing_parts)
