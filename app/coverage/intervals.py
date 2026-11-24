def union(spans):
    merged = []
    for lo, hi in sorted(spans):
        if hi <= lo:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def measure(spans):
    return sum(hi - lo for lo, hi in spans)


def intersect(left, right):
    overlap = []
    for lo_l, hi_l in left:
        for lo_r, hi_r in right:
            lo, hi = max(lo_l, lo_r), min(hi_l, hi_r)
            if hi > lo:
                overlap.append([lo, hi])
    return overlap


def subtract(base, cuts):
    remaining = []
    for lo, hi in base:
        cursor = lo
        for cut_lo, cut_hi in cuts:
            if cut_hi <= cursor or cut_lo >= hi:
                continue
            if cut_lo > cursor:
                remaining.append([cursor, min(cut_lo, hi)])
            cursor = max(cursor, cut_hi)
            if cursor >= hi:
                break
        if cursor < hi:
            remaining.append([cursor, hi])
    return remaining
