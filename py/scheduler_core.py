"""
[1단계 프로토타입] 웹 버전 패스 계산 엔진 (최소 기능)

원본 core/scheduler.py의 calculate_passes()를 브라우저(Pyodide) 환경에 맞게
단순화한 버전입니다. 데스크톱 버전과의 차이점:

1. 파일 시스템에서 TLE/지상국 목록을 읽지 않고, 함수 인자로 직접 받습니다
   (브라우저에는 로컬 폴더 개념이 없고, 사용자가 폼에 입력한 값을 받습니다).
2. skyfield의 timescale을 커스텀 디렉토리 없이 builtin 데이터로만 로드합니다
   (Loader(dir) 방식은 데스크톱 파일 번들링용이라 웹에는 불필요합니다).
3. 아직 다중 위성/지상국, 안테나 용량, 충돌 해결 로직은 포함하지 않았습니다
   (2단계에서 원본 calculate_passes()를 통째로 이식할 때 합류합니다).

이 파일은 index.html에서 fetch로 텍스트째 읽어와 pyodide.runPython()으로
실행됩니다 (별도 빌드 과정 없이 순수 텍스트 파일로 서빙).
"""

from datetime import timezone
from skyfield.api import load, wgs84, EarthSatellite


def calculate_single_pass_set(tle_line1, tle_line2, sat_name,
                               station_name, lat, lon,
                               start_iso, end_iso,
                               min_el_deg=10.0, min_dur_sec=60.0):
    """
    위성 1개, 지상국 1개에 대한 AOS/LOS/Duration/MaxEl 패스 목록을 계산합니다.

    :param tle_line1, tle_line2: TLE 두 줄 (문자열)
    :param sat_name: 위성 이름 (표시용)
    :param station_name: 지상국 이름 (표시용)
    :param lat, lon: 지상국 위도/경도 (도 단위, float)
    :param start_iso, end_iso: 분석 시작/종료 시각 (ISO 8601 문자열, 예: "2026-01-01T00:00:00")
    :param min_el_deg: 최소 가시 고도각
    :param min_dur_sec: 최소 패스 지속시간(초) - 이보다 짧으면 결과에서 제외
    :return: [{"station":..., "satellite":..., "aos":..., "los":..., "duration":..., "max_el":...}, ...]
             (JS로 넘기기 쉽도록 datetime은 전부 ISO 문자열로 변환된 상태)
    """
    ts = load.timescale(builtin=True)

    start_dt = _parse_iso(start_iso).replace(tzinfo=timezone.utc)
    end_dt = _parse_iso(end_iso).replace(tzinfo=timezone.utc)

    t0 = ts.from_datetime(start_dt)
    t1 = ts.from_datetime(end_dt)

    satellite = EarthSatellite(tle_line1, tle_line2, sat_name, ts)
    station = wgs84.latlon(float(lat), float(lon))

    t_events, events = satellite.find_events(station, t0, t1, altitude_degrees=min_el_deg)

    results = []
    current = {}
    for t_ev, event in zip(t_events, events):
        dt = t_ev.utc_datetime()
        if event == 0:  # AOS
            current = {"aos": dt}
        elif event == 1:  # MAX (최대 고도각 시점)
            if current:
                difference = satellite - station
                topocentric = difference.at(t_ev)
                alt, _az, _dist = topocentric.altaz()
                current["max_el"] = round(alt.degrees, 2)
        elif event == 2:  # LOS
            if current and "aos" in current:
                current["los"] = dt
                duration = (current["los"] - current["aos"]).total_seconds()
                if duration >= min_dur_sec:
                    results.append({
                        "station": station_name,
                        "satellite": sat_name,
                        "aos": current["aos"].strftime("%Y-%m-%d %H:%M:%S"),
                        "los": current["los"].strftime("%Y-%m-%d %H:%M:%S"),
                        "duration": round(duration, 1),
                        "max_el": current.get("max_el", 0.0),
                    })
                current = {}

    return results


def _parse_iso(s):
    from datetime import datetime
    # JS의 <input type="datetime-local"> 값은 "2026-01-01T00:00" 형태로 옵니다 (초 없음)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"날짜 형식을 이해할 수 없습니다: {s}")
