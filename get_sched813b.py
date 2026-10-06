import httpx
from datetime import datetime
from statistics import median

h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
with httpx.Client(timeout=60, headers=h) as c:
    r = c.get('https://isr.20fl.co.il/TripsInfoProvider/GetPredictForLine', params={'operatorID':39,'lineRef':44094,'date':'2026-10-05'})
    trips = r.json()['trips']
    stop_times = {}
    for t in trips:
        dep0 = None
        for s in t['stops']:
            if s.get('stopIndex') == 1:
                try:
                    dep0 = datetime.fromisoformat(s['departureFromOrigin'].replace(' ', 'T'))
                except Exception:
                    try:
                        dep0 = datetime.fromisoformat(s['aimedArrivalTime'].replace(' ', 'T'))
                    except Exception:
                        continue
                break
        if not dep0:
            continue
        for s in t['stops']:
            try:
                arr = datetime.fromisoformat(s['aimedArrivalTime'].replace(' ', 'T'))
                sid = str(s['stopID'])
                stop_times.setdefault(sid, []).append((arr-dep0).total_seconds()/60)
            except Exception:
                continue
    rs = c.post('https://isr.20fl.co.il/TripsInfoProvider/gtfs/getRouteStopsForLines', params={'date':'2026-10-05'}, json={'lineList':[{"lineRef":44094,"rte":"8130102כ","operatorID":39}]})
    st = rs.json()['lines']['44094']
    for x in sorted(st, key=lambda k: k['index']):
        sid = str(x['stopID'])
        if sid in stop_times and stop_times[sid]:
            m = round(median(stop_times[sid]), 1)
            print(f"{x['index']}: {m} min ({sid}) {x['en']}")
