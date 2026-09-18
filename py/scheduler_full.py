"""
[2단계] 웹 버전 전체 스케줄링 엔진 — 원본 core/scheduler.py를 브라우저(Pyodide)용으로 이식

데스크톱 버전과의 차이점 (딱 이 2가지만 다릅니다 - 나머지 로직은 100% 동일):
1. get_resource_path/Loader(dir) 방식 대신 load.timescale(builtin=True)를 바로 사용합니다.
   (데스크톱 버전은 PyInstaller로 묶었을 때 리소스 폴더를 찾으려고 저 방식을 썼는데,
    브라우저에는 그런 파일 번들 개념이 없어서 단순화했습니다. 1단계 프로토타입에서
    이 방식이 정상 동작하는 것을 이미 확인했습니다.)
2. parse_tle_from_dir() / parse_stations_from_dir()는 로컬 폴더를 읽는 함수라 제거했습니다.
   대신 JS 쪽에서 사용자가 입력한 TLE/지상국 텍스트를 파싱해서
   calculate_passes()가 원래 받던 것과 똑같은 형태(tle_data dict, station_configs list)로
   만들어 넘겨줍니다 (아래 parse_tle_text / parse_stations_text 참고).

나머지(안테나 용량, 점검 일정, 근무시간 필터, 궤도 전파 벡터화 등)는 전부
원본과 완전히 동일한 코드입니다.
"""
from datetime import datetime, timezone, timedelta
from skyfield.api import load, wgs84, EarthSatellite


# ==============================================================================
# [파싱 - 웹 버전 전용] 텍스트 입력을 tle_data / station_configs로 변환
# ==============================================================================
def parse_tle_text(raw_text):
    """
    사용자가 텍스트 영역에 붙여넣은 TLE 목록을 파싱합니다.
    형식은 표준 3줄(이름, Line1, Line2)이 반복되는 형태를 기대합니다:

        ISS (ZARYA)
        1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9994
        2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49309620 12345
        NOAA 19
        1 33591U 09005A   ...
        2 33591  99.1234 ...

    :return: {sat_name: {"lines": (line1, line2), "norad_id": str, "sat_name": str}, ...}
             (원본 calculate_passes()가 기대하는 tle_data 형식과 동일)
    """
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    satellites = {}
    idx = 0
    while idx < len(lines):
        if lines[idx].startswith("1 ") and (idx + 1) < len(lines) and lines[idx + 1].startswith("2 "):
            line1 = lines[idx]
            line2 = lines[idx + 1]
            norad_id = line1[2:7].strip()
            sat_name = lines[idx - 1].strip() if idx > 0 else f"SAT-{norad_id}"
            satellites[sat_name] = {"lines": (line1, line2), "norad_id": norad_id, "sat_name": sat_name}
            idx += 2
        else:
            idx += 1
    return satellites


def parse_stations_text(raw_text):
    """
    "이름, 위도, 경도, 다운로드가능(Y/N), 커맨딩가능(Y/N), [RX용량], [TX용량]" 형식,
    한 줄에 지상국 하나. 데스크톱 버전의 stations/*.txt 파일 포맷과 완전히 동일합니다
    (그 파일 내용을 그대로 복사해서 붙여넣어도 됩니다). #으로 시작하는 줄은 주석으로 무시합니다.

    :return: [(name, lat, lon, is_down, is_cmd, rx_capacity, tx_capacity), ...]
    """
    def _parse_capacity(raw_value, default_value=1):
        try:
            val = int(float(raw_value))
            return val if val >= 1 else default_value
        except (ValueError, TypeError):
            return default_value

    stations_list = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            name = parts[0]
            lat = float(parts[1])
            lon = float(parts[2])
            is_down = parts[3].upper() if len(parts) > 3 else "Y"
            is_cmd = parts[4].upper() if len(parts) > 4 else "Y"
            rx_capacity = _parse_capacity(parts[5]) if len(parts) > 5 else 1
            tx_capacity = _parse_capacity(parts[6]) if len(parts) > 6 else 1
            if not any(s[0] == name for s in stations_list):
                stations_list.append((name, lat, lon, is_down, is_cmd, rx_capacity, tx_capacity))
        except ValueError:
            continue
    return stations_list


# ==============================================================================
# [궤도 계산] 위성 궤도 회차 타임라인 구축 함수
# ==============================================================================
def build_orbit_sampling_times(ts, t0, t1, step_seconds=60):
    """
    💡 [성능 개선] find_orbit_starts_at_north_pole()이 쓸 60초 간격 시각 배열을
    미리 한 번만 만들어서, 위성이 여러 개일 때 매번 다시 만들지 않고 재사용하기 위한 함수.
    (t0/t1은 위성이 몇 개든 항상 동일한 분석 기간이므로, 시각 목록 자체는 위성과 무관하게 같습니다)

    :return: (dt_list, times) - dt_list는 datetime 리스트, times는 skyfield Time 벡터
    """
    start_dt = t0.utc_datetime()
    end_dt = t1.utc_datetime()

    if end_dt <= start_dt:
        return [start_dt], None

    step = timedelta(seconds=step_seconds)
    dt_list = []
    current_dt = start_dt
    while current_dt <= end_dt:
        dt_list.append(current_dt)
        current_dt += step

    times = ts.from_datetimes(dt_list)
    return dt_list, times


def find_orbit_starts_at_north_pole(satellite, ts, t0, t1, start_pass_no=1, sample_dts=None, sample_times=None):
    """
    💡 [성능 개선] 예전에는 60초 간격으로 Python 루프를 돌면서, 매 시점마다
    satellite.at()을 "한 번씩 개별 호출"했습니다. 스카이필드의 궤도 전파(SGP4)와
    좌표 변환(장동/세차 보정 등)은 호출당 고정 오버헤드가 커서, 분석 기간이 길어질수록
    (예: 1주일 = 위성 1개당 약 10,080번 호출) 이 방식이 전체 계산 시간의 90% 이상을
    차지하는 병목이었습니다 (실측: 위성 6개, 1주일 분석 기준 18.2초 중 17.3초).

    스카이필드는 여러 시각을 배열로 한 번에 넘기면 내부적으로 numpy 벡터 연산으로
    처리해서 훨씬 빠릅니다. 그래서 "60초 간격 시각 전체를 미리 리스트로 만든 뒤,
    satellite.at()을 통째로 딱 1번만 호출"하도록 바꿨습니다. 기존과 완전히 동일한
    60초 간격 시각들을 그대로 사용하므로, 계산 결과(궤도 회차 번호)는 기존과 동일하고
    속도만 빨라집니다.

    :param sample_dts: (선택) build_orbit_sampling_times()로 미리 만들어둔 datetime 리스트.
                        여러 위성에 대해 반복 호출할 때, 매번 새로 만들지 않고 재사용해서
                        추가로 속도를 높일 수 있습니다. 생략하면 이 함수 안에서 새로 만듭니다.
    :param sample_times: (선택) sample_dts와 세트로 함께 전달하는 skyfield Time 벡터.
    """
    start_dt = t0.utc_datetime()
    end_dt = t1.utc_datetime()

    if end_dt <= start_dt:
        return [(start_pass_no, start_dt)]

    if sample_dts is not None and sample_times is not None:
        dt_list, times = sample_dts, sample_times
    else:
        dt_list, times = build_orbit_sampling_times(ts, t0, t1)

    geocentric = satellite.at(times)
    lats = wgs84.subpoint(geocentric).latitude.degrees  # 시각별 위도 값이 담긴 numpy 배열

    orbit_starts = []
    orbit_counter = start_pass_no
    orbit_starts.append((orbit_counter, dt_list[0]))
    is_ascending = True

    # 위도 배열을 훑으면서 "상승 중이다가 하강으로 꺾이는 지점(북극 통과)"을 찾는 부분은
    # 원래도 순수 숫자 비교(계산량이 거의 없음)라 벡터화의 이득이 크지 않아 그대로 둡니다.
    # (병목은 propagation 자체였지 이 비교 루프가 아니었습니다)
    for i in range(1, len(lats)):
        if lats[i] < lats[i - 1] and is_ascending:
            orbit_counter += 1
            orbit_starts.append((orbit_counter, dt_list[i]))
            is_ascending = False
        elif lats[i] > lats[i - 1]:
            is_ascending = True

    return orbit_starts


def get_orbit_number(aos_time, orbit_timeline, default_start_pass=1):
    for orbit_no, start_dt in reversed(orbit_timeline):
        if aos_time >= start_dt:
            return orbit_no
    return default_start_pass


# ==============================================================================
# [공용 자원 배정 엔진] 다중 안테나(N-슬롯) 지상국 충돌 해결 헬퍼 함수
# ------------------------------------------------------------------------------
# 이 함수는 core/scheduler.py(최초 패스 계산)와 core/conflict_resolver.py
# (사용자의 수동 "Auto Resolve" 재실행) 양쪽에서 공통으로 사용됩니다.
# 그래야 "처음 계산할 때"와 "나중에 가중치 바꿔서 재계산할 때" 안테나 용량 규칙이
# 항상 동일하게 적용됩니다.
# ==============================================================================
def resolve_station_group_with_capacity(group_passes, capacity, score_fn, on_admit=None, on_evict=None,
                                         hard_reject_fn=None):
    """
    같은 지상국에서 시간이 서로 겹치는 패스 그룹(group_passes)에 대해,
    "최대 capacity개까지는 동시에 선택 가능"이라는 규칙으로 선택(selected) 여부를 결정합니다.

    [동작 방식 - 이벤트 스윕(Event Sweep) 알고리즘]
    1. 모든 패스의 AOS(시작)/LOS(종료) 시각을 하나의 타임라인으로 합쳐 시간순으로 정렬합니다.
    2. 시간순으로 훑으면서 "지금 이 순간 몇 개의 패스가 안테나를 점유 중인가"를 추적합니다.
    3. 새 패스가 시작되는 시점에:
       - 아직 빈 슬롯(안테나)이 남아있으면 -> 바로 선택(수용)
       - 슬롯이 꽉 차 있으면 -> 현재 점유 중인 패스들 중 score_fn 기준으로
         "가장 우선순위가 낮은(안 좋은) 패스"와 비교해서, 새 패스가 더 우선순위가 높다면
         기존 패스를 밀어내고(교체) 새 패스가 슬롯을 차지합니다.

    ⚠️ [알고리즘의 한계 - 정직하게 명시]
    이 방식은 "실시간 우선순위 교체" 방식의 휴리스틱(근사) 알고리즘입니다.
    이론적으로 모든 경우의 수를 따져 100% 전역 최적해를 구하는 알고리즘(예: 최소비용흐름 기반
    엄밀한 k-track 구간 스케줄링)은 아니며, 실무적으로 충분히 합리적인 근사해를
    빠르게(선형 시간) 계산하는 것을 목표로 합니다.

    :param group_passes: 같은 지상국 내에서 시간이 겹치는 패스 dict의 리스트.
                          각 dict는 'aos'(datetime), 'los'(datetime) 키를 반드시 가지고 있어야 합니다.
                          이 함수가 각 dict의 'selected' 키를 직접 갱신합니다.
    :param capacity: 이 지상국이 동시에 처리 가능한 개수 (안테나 대수 등).
                      💡 [안테나 점검 일정 지원] 정수를 그대로 줘도 되고(기존과 동일, 상시 고정 용량),
                      또는 datetime 하나를 받아 "그 시점의 용량(int)"을 반환하는 콜러블을 줄 수도 있습니다.
                      콜러블을 주면, 매 AOS 이벤트 시각마다 그 순간의 용량을 다시 조회해서 판단합니다
                      (예: 특정 기간에 안테나 1대가 점검 중이라 그 시간대만 용량이 줄어드는 경우).
                      한 패스가 진행되는 도중에 용량이 바뀌어도 이미 점유 중인 패스를 도중에
                      쫓아내진 않습니다 (수신 도중에 강제 종료하는 건 현실적이지 않으므로) -
                      새로 진입하려는 패스를 받을 때만 그 시점의 용량을 기준으로 판단합니다.
    :param score_fn: 패스 dict 1개를 받아 정렬용 튜플(tuple)을 반환하는 함수.
                      "값이 작을수록 더 우선순위가 높은(더 좋은) 패스"라는 규칙을 따라야 합니다.
                      (기존 fairness_sort_key와 동일한 규칙)
    :param on_admit: 패스가 새로 선택(수용)될 때 호출되는 콜백 함수 (선택된 pass dict를 인자로 받음).
                     주로 위성별 선택 카운트(sat_selected_counts)를 +1 하는 용도로 사용합니다.
    :param on_evict: 이미 선택되어 있던 패스가 더 우선순위 높은 패스에 밀려 탈락할 때 호출되는 콜백.
                     주로 위성별 선택 카운트를 -1 하는 용도로 사용합니다.
    :param hard_reject_fn: 패스 dict 1개를 받아 "이 패스는 슬롯이 남아돌아도 절대 선택되면 안 된다"를
                     반환하는 함수 (예: 이미 위성별 Max Pass 상한을 넘긴 경우). None이면 이 검사를 생략합니다.
                     ⚠️ [버그 수정] 예전에는 "슬롯이 남아있으면 무조건 즉시 수용"했기 때문에, 상한을
                     넘긴 위성이라도 마침 슬롯에 여유가 있으면 (score_fn 비교 없이) 그냥 선택되어버리는
                     문제가 있었습니다. hard_reject_fn이 있으면 빈 슬롯 여부와 무관하게 항상 먼저 걸러냅니다.
    :return: group_passes (참조 전달이므로 원본이 그대로 갱신되어 반환됨)
    """
    is_dynamic_capacity = callable(capacity)
    if not is_dynamic_capacity:
        capacity = max(1, int(capacity))

    # 1. 모든 패스를 일단 "미선택" 상태로 초기화
    for p in group_passes:
        p['selected'] = False

    # 2. (시각, 이벤트정렬우선순위, 패스) 형태로 이벤트 타임라인 구성
    #    같은 시각에 LOS와 AOS가 동시에 겹치면, "슬롯을 먼저 비운 뒤(LOS)" "새로 채워야(AOS)"
    #    슬롯 개수를 정확히 셀 수 있으므로 LOS(0)를 AOS(1)보다 먼저 처리하도록 정렬합니다.
    events = []
    for p in group_passes:
        events.append((p['aos'], 1, id(p), p))  # AOS: 시작(진입) 이벤트
        events.append((p['los'], 0, id(p), p))  # LOS: 종료(퇴장) 이벤트
    events.sort(key=lambda e: (e[0], e[1]))

    active = []  # 현재 슬롯을 점유 중인 패스 리스트

    for time_val, ev_type, _pid, p in events:
        if ev_type == 0:
            # --- LOS: 슬롯 반납 ---
            if p in active:
                active.remove(p)
            continue

        # --- AOS: 신규 패스의 슬롯 진입 시도 ---
        if hard_reject_fn is not None and hard_reject_fn(p):
            # 상한 초과 등으로 절대 선택 불가 - 슬롯 여유와 무관하게 항상 탈락 (selected는 이미 False)
            continue

        current_capacity = capacity(time_val) if is_dynamic_capacity else capacity
        current_capacity = max(0, int(current_capacity))  # 점검 중이라 0대(전면 불가)인 경우도 허용

        if len(active) < current_capacity:
            # 여유 슬롯이 있으므로 경합 없이 바로 수용
            p['selected'] = True
            active.append(p)
            if on_admit:
                on_admit(p)
        elif active:
            # 슬롯이 꽉 찼으므로, 현재 점유자 중 가장 우선순위가 낮은(=점수가 나쁜) 패스를 탐색
            worst_active = max(active, key=score_fn)
            if score_fn(p) < score_fn(worst_active):
                # 신규 패스가 기존 최약체보다 우선순위가 높음 -> 교체(밀어내기)
                worst_active['selected'] = False
                active.remove(worst_active)
                if on_evict:
                    on_evict(worst_active)

                p['selected'] = True
                active.append(p)
                if on_admit:
                    on_admit(p)
            # else: 신규 패스는 기존 점유자들보다 우선순위가 낮으므로 탈락 상태(selected=False) 유지

    return group_passes


# ==============================================================================
# 💡 [안테나 점검 일정 지원] 시간대별 용량 오버라이드 조회 함수 생성기
# ------------------------------------------------------------------------------
# "이 지상국은 평소엔 3대인데, 이번 주 화요일 14~18시엔 1대가 점검 중이라 2대뿐이다"
# 같은 케이스를 표현하기 위한 함수입니다. overrides 리스트에서, 주어진 시각(dt)이
# [start_dt, end_dt) 구간 안에 들어가는 항목을 찾아 그 값을 쓰고, 없으면 base_capacity를
# 그대로 씁니다. 여러 오버라이드가 겹치면 리스트의 뒤쪽(나중에 추가된 것)이 우선합니다.
# ==============================================================================
def build_capacity_lookup(base_capacity, overrides, value_key):
    """
    :param base_capacity: 평소(오버라이드가 없을 때) 용량
    :param overrides: [{"start_dt": datetime, "end_dt": datetime, value_key: int_or_None, ...}, ...]
                       value_key에 해당하는 값이 None이면 "이 오버라이드는 이 자원(RX 또는 TX)엔
                       영향 없음"이라는 뜻이라 건너뜁니다 (예: RX만 줄이고 TX는 그대로 두는 점검).
    :param value_key: overrides 안의 dict에서 용량 값을 읽어올 키 이름 (예: "rx_capacity" 또는 "tx_capacity")
    :return: datetime 하나를 받아 그 시점의 유효 용량(int)을 돌려주는 함수
    """
    base_capacity = max(1, int(base_capacity))
    if not overrides:
        return lambda dt: base_capacity

    def _strip_tz(d):
        """비교용으로만 타임존 정보를 제거합니다 (aware/naive가 섞여 들어와도 TypeError 없이 비교 가능하게)."""
        return d.replace(tzinfo=None) if d.tzinfo is not None else d

    def _lookup(dt):
        dt_cmp = _strip_tz(dt)
        effective = base_capacity
        for ov in overrides:
            val = ov.get(value_key)
            if val is None:
                continue
            if _strip_tz(ov["start_dt"]) <= dt_cmp < _strip_tz(ov["end_dt"]):
                effective = val  # 뒤에 나온 오버라이드가 우선 적용되도록 계속 덮어씀
        return effective

    return _lookup


# ==============================================================================
# 💡 [버그 수정 - 기능 통합] 시간대별 용량 초과 여부를 정확히 검사하는 공용 함수
# ------------------------------------------------------------------------------
# 이전에는 "TX 사전 경고 배너", "Import 검증", "Analytics 가동률" 세 곳이 각자
# 따로 "동시 겹침 개수"를 계산하고 있었고, 그중 TX 사전 경고와 Analytics는
# 안테나 점검 일정(시간대별 용량)을 전혀 반영하지 못하는 채로 남아있었습니다
# (점검으로 용량이 줄어든 시간대인데도 평소 용량 기준으로만 판단하는 버그).
# 이 함수 하나로 통일해서, 앞으로 이런 종류의 계산이 필요할 때 항상 정확하게
# 시간대별 용량을 반영하도록 합니다.
# ==============================================================================
def find_capacity_overflow(passes_for_one_station, capacity_fn):
    """
    같은 지상국의 '선택된' 패스 리스트를 받아서, 시간대별 유효 용량(capacity_fn)을
    초과하는 순간이 있는지 이벤트 스윕으로 검사합니다.

    :param passes_for_one_station: 이미 같은 지상국으로 필터링된 pass dict 리스트
                                    (각 dict는 'aos'/'los' datetime 키 필요)
    :param capacity_fn: datetime -> int. build_capacity_lookup()으로 만든 함수를 그대로 넣으면 됩니다.
    :return: (peak_concurrent, worst_excess) 튜플.
             worst_excess가 0이면 초과 없음. peak_concurrent는 참고용 최대 동시 개수.
    """
    if not passes_for_one_station:
        return 0, 0

    events = []
    for p in passes_for_one_station:
        events.append((p['aos'], 1))
        events.append((p['los'], 0))
    events.sort(key=lambda e: (e[0], e[1]))

    current = 0
    peak_concurrent = 0
    worst_excess = 0
    for t, ev_type in events:
        if ev_type == 0:
            current -= 1
        else:
            current += 1
            peak_concurrent = max(peak_concurrent, current)
            cap = capacity_fn(t)
            if current > cap:
                worst_excess = max(worst_excess, current - cap)
    return peak_concurrent, worst_excess


def compute_time_weighted_avg_capacity(base_capacity, overrides, value_key, window_start, window_end):
    """
    💡 [정확도 개선] 분석 기간(window_start~window_end) 동안, 점검 일정 등으로
    용량이 바뀌는 구간까지 정확히 반영한 "시간 가중 평균 용량"을 계산합니다.
    (예: 10시간 중 8시간은 3대, 2시간은 1대였다면 -> (8*3 + 2*1) / 10 = 2.6대)

    이 평균값을 가동률(%) 계산의 분모로 쓰면, 점검 기간이 섞여 있어도
    "이론상 최대 가동 가능 시간"을 더 정확하게 추정할 수 있습니다.

    :return: 시간 가중 평균 용량 (float, 최소 0.01 - 0으로 나누기 방지)
    """
    base_capacity = max(1, int(base_capacity))
    if window_end <= window_start:
        return float(base_capacity)

    def _strip_tz(d):
        return d.replace(tzinfo=None) if d.tzinfo is not None else d

    window_start = _strip_tz(window_start)
    window_end = _strip_tz(window_end)

    capacity_fn = build_capacity_lookup(base_capacity, overrides, value_key)

    # 오버라이드들의 시작/종료 시각 중 분석 기간 안에 들어오는 것들을 "구간 경계"로 모읍니다.
    # (그 경계들 사이에서는 용량이 일정하므로, 구간별로 나눠서 정확히 적분할 수 있습니다)
    boundaries = {window_start, window_end}
    for ov in (overrides or []):
        s, e = ov.get("start_dt"), ov.get("end_dt")
        if s is not None:
            s = _strip_tz(s)
            if window_start < s < window_end:
                boundaries.add(s)
        if e is not None:
            e = _strip_tz(e)
            if window_start < e < window_end:
                boundaries.add(e)

    sorted_boundaries = sorted(boundaries)
    total_weighted = 0.0
    total_duration = 0.0
    for i in range(len(sorted_boundaries) - 1):
        seg_start = sorted_boundaries[i]
        seg_end = sorted_boundaries[i + 1]
        seg_dur = (seg_end - seg_start).total_seconds()
        if seg_dur <= 0:
            continue
        seg_capacity = capacity_fn(seg_start)  # 이 구간 내내 일정하므로 시작 시각 기준으로 조회
        total_weighted += seg_dur * seg_capacity
        total_duration += seg_dur

    if total_duration <= 0:
        return float(base_capacity)
    return max(0.01, total_weighted / total_duration)


# ==============================================================================
# [스케줄링 핵심 엔진] 최소/최대 패스 제한 및 공평 배정 연산
# ==============================================================================
def calculate_passes(tle_data, station_configs, start_dt, end_dt, min_el, min_dur, 
                     start_pass_no=1, equalize_allocation=True, 
                     equalize_target_sats=None, min_pass_targets=None, max_pass_targets=None,
                     is_pass_eligible=None, antenna_overrides=None, progress_callback=None):
    """
    :param min_pass_targets: 위성별 개별 최소 보장 패스 (예: {'NEONSAT1': 0, 'NEONSAT-1A': 2})
    :param max_pass_targets: 위성별 개별 최대 제한 패스 (예: {'NEONSAT1': 5, 'NEONSAT-1A': 0(상한 없음)})
    :param is_pass_eligible: 💡 [기능2 후속 개선] pass dict 1개를 받아 "이 패스가 애초에 선택 후보가
        될 자격이 있는가"를 반환하는 콜백 함수 (예: 근무시간 안에 있는지). 기본값 None이면 전부 자격 있음으로
        간주합니다(기존 동작과 동일, 하위 호환).
        ⚠️ 왜 이게 필요한가: 예전에는 "일단 전부 경합시켜서 승자를 뽑은 뒤, 그 승자가 근무시간
        밖이면 그냥 버리는" 방식이었습니다. 이러면 같은 그룹 안에 근무시간 '안'에 있는 다른 후보가
        있어도 전혀 기회를 못 받고 그 시간대 자체가 통째로 빈 슬롯이 되어버리는 문제가 있었습니다.
        이제는 "경합을 시작하기 전에 자격 없는 후보를 먼저 걸러내고, 남은 자격 있는 후보들끼리만
        경합"하도록 바꿔서, 자격 있는 차점자가 있으면 그 후보가 정상적으로 슬롯을 차지합니다.
    :param antenna_overrides: 💡 [안테나 점검 일정 지원] 지상국별 "일시적 용량 변경" 리스트.
        [{"station": "Daejeon", "start_dt": datetime, "end_dt": datetime,
          "rx_capacity": 2 또는 None, "tx_capacity": 1 또는 None, "reason": "..."}, ...]
        기본값 None이면 오버라이드 없음(기존 동작과 완전히 동일, 하위 호환).
    :param progress_callback: 💡 [성능/UX 개선] 위성 궤도 전파가 하나 끝날 때마다
        (완료한 위성 수, 전체 위성 수, 방금 완료한 위성 이름)을 인자로 호출되는 콜백.
        UI에서 진행률 표시줄을 갱신하는 용도입니다. 기본값 None이면 그냥 호출을 생략합니다
        (기존 동작과 완전히 동일, 하위 호환 - core 로직은 UI에 의존하지 않습니다).
    """
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)

    # 💡 [웹 버전 단순화] 데스크톱 버전은 PyInstaller 번들 안의 skyfield_data 폴더를 찾아
    #    Loader(dir)로 연결했지만, 브라우저에는 그런 파일 번들이 없으므로 builtin 데이터를
    #    바로 사용합니다 (1단계 프로토타입에서 이미 정상 동작 확인함).
    ts = load.timescale(builtin=True)
    t0 = ts.from_datetime(start_dt)
    t1 = ts.from_datetime(end_dt)
    
    stations = {cfg[0]: wgs84.latlon(cfg[1], cfg[2]) for cfg in station_configs}

    # 💡 지상국별 동시 수신(RX) 가능 대수(안테나 개수) 맵 구성
    #    station_configs 튜플의 6번째 값(인덱스 5)이 RX_Capacity. 없으면(구버전 파일) 기본값 1.
    #    💡 [안테나 점검 일정 지원] 각 지상국마다 "그 시각의 유효 RX 용량"을 돌려주는 함수로 구성.
    #    오버라이드가 전혀 없으면 이 함수는 항상 기본값만 반환하므로 기존 동작과 동일합니다.
    antenna_overrides = antenna_overrides or []
    station_rx_capacity = {}
    for cfg in station_configs:
        st_name = cfg[0]
        base_rx = int(cfg[5]) if len(cfg) > 5 else 1
        overrides_for_station = [ov for ov in antenna_overrides if ov.get("station") == st_name]
        station_rx_capacity[st_name] = build_capacity_lookup(base_rx, overrides_for_station, "rx_capacity")

    raw_passes = []

    # 💡 [성능 개선] 모든 위성이 동일한 분석 기간(t0~t1)을 쓰므로, 60초 간격 시각 배열도
    #    위성마다 새로 만들 필요 없이 딱 한 번만 만들어서 재사용합니다.
    _orbit_sample_dts, _orbit_sample_times = build_orbit_sampling_times(ts, t0, t1)

    _total_satellites = len(tle_data)
    _completed_satellites = 0

    for sat_key, sat_info in tle_data.items():
        if isinstance(sat_info, dict):
            lines = sat_info['lines']
            norad_id = sat_info['norad_id']
            sat_name = sat_info['sat_name']
        else:
            lines = sat_info
            norad_id = lines[0][2:7].strip()
            sat_name = sat_key
            
        try:
            satellite = EarthSatellite(lines[0], lines[1], sat_name, ts)
        except Exception:
            continue
            
        orbit_timeline = find_orbit_starts_at_north_pole(
            satellite, ts, t0, t1, start_pass_no=start_pass_no,
            sample_dts=_orbit_sample_dts, sample_times=_orbit_sample_times
        )
            
        for gs_name, gs_loc in stations.items():
            times, events = satellite.find_events(gs_loc, t0, t1, altitude_degrees=min_el)
            current_pass = {}
            for t, event in zip(times, events):
                if event == 0:
                    current_pass['aos'] = t.utc_datetime()
                elif event == 1:
                    difference = satellite - gs_loc
                    alt, _, _ = difference.at(t).altaz()
                    current_pass['max_el'] = alt.degrees
                elif event == 2 and 'aos' in current_pass:
                    current_pass['los'] = t.utc_datetime()
                    duration = (current_pass['los'] - current_pass['aos']).total_seconds()
                    
                    if duration >= min_dur:
                        pass_no = get_orbit_number(current_pass['aos'], orbit_timeline, default_start_pass=start_pass_no)
                        raw_passes.append({
                            'satellite': f"{sat_name}({norad_id})",
                            'station': gs_name,
                            'aos': current_pass['aos'],
                            'los': current_pass['los'],
                            'duration': round(duration, 1),
                            'max_el': round(current_pass.get('max_el', 0.0), 2),
                            'conflict_group': None,
                            'selected': True,
                            'status': "Normal",
                            'pass_no': pass_no
                        })
                    current_pass = {}

        # 💡 [성능/UX 개선] 이 위성(궤도 전파 + 모든 지상국 패스 탐색)이 끝날 때마다 진행률 보고
        _completed_satellites += 1
        if progress_callback is not None:
            try:
                progress_callback(_completed_satellites, _total_satellites, sat_name)
            except Exception:
                pass  # 진행률 콜백에서 문제가 생겨도 실제 계산 자체는 절대 중단되면 안 됨

    if not raw_passes:
        return []

    raw_groups = []
    for gs_name in stations.keys():
        station_passes = [p for p in raw_passes if p['station'] == gs_name]
        station_passes.sort(key=lambda x: x['aos'])
        current_group = []
        for p in station_passes:
            if not current_group:
                current_group.append(p)
            else:
                max_los_in_group = max(x['los'] for x in current_group)
                if p['aos'] < max_los_in_group:
                    current_group.append(p)
                else:
                    raw_groups.append(current_group)
                    current_group = [p]
        if current_group:
            raw_groups.append(current_group)

    if not raw_groups:
        return []

    raw_groups.sort(key=lambda g: min(x['aos'] for x in g))

    calculated_passes = []
    group_counter = 0
    
    sat_selected_counts = {}
    for sat_key in tle_data.keys():
        clean_name = sat_key.split("(")[0].strip()
        sat_selected_counts[clean_name] = 0

    if equalize_target_sats is None:
        equalize_target_sats = set(sat_selected_counts.keys())
    else:
        equalize_target_sats = set(equalize_target_sats)

    if min_pass_targets is None: min_pass_targets = {}
    if max_pass_targets is None: max_pass_targets = {}

    # 경합 해결 및 최소/최대 패스 연산
    #
    # 💡 [안테나 동시성 지원] 이 지상국이 동시에 처리 가능한 개수(capacity_fn)를 조회합니다.
    #    raw_groups는 앞선 그룹핑 단계에서 "지상국별로" 나뉘어 만들어졌으므로,
    #    그룹 안의 모든 패스는 항상 같은 지상국 소속입니다 -> g[0]['station']으로 대표값을 봐도 안전합니다.
    #
    # 💡 [안테나 점검 일정 지원 - 구조 변경] 예전에는 "그룹 크기 <= 용량이면 무조건 전원 수용,
    #    아니면 경합"으로 미리 나눠 처리했습니다. 하지만 이제 용량이 시간에 따라 달라질 수 있어서
    #    (예: 그룹이 걸쳐 있는 구간 중 일부만 점검으로 용량이 줄어드는 경우), 그룹 크기만 보고
    #    미리 판단할 수 없습니다. 그래서 항상 resolve_station_group_with_capacity()를 거치도록
    #    통일했고, 그 결과(누가 선택됐는지)를 보고 나서 상태 문구를 붙이는 방식으로 바꿨습니다.
    for g in raw_groups:
        st_name = g[0]['station']
        capacity_fn = station_rx_capacity.get(st_name, lambda dt: 1)

        # --------------------------------------------------------------
        # 💡 [기능2 후속 개선] 경합을 시작하기 전에, 애초에 자격 없는(예: 근무시간 밖) 후보를
        #    먼저 걸러냅니다. 이렇게 하면 "자격 없는 후보가 경합에서 이겨버려서 자격 있는
        #    차점자가 기회를 못 받는" 문제가 원천적으로 발생하지 않습니다.
        #    (is_pass_eligible이 None이면 전부 자격 있음 - 기존 동작과 동일)
        # --------------------------------------------------------------
        if is_pass_eligible is not None:
            ineligible = [p for p in g if not is_pass_eligible(p)]
            eligible = [p for p in g if is_pass_eligible(p)]
        else:
            ineligible = []
            eligible = g

        for p in ineligible:
            p['selected'] = False
            p['conflict_group'] = None
            p['status'] = "Shift Hours Blocked"
        calculated_passes.extend(ineligible)  # 자격 없는 패스는 여기서 바로 결과에 반영 (아래 로직과 무관)

        if not eligible:
            # 이 그룹 전체가 자격 미달이면 더 처리할 것이 없음 (이미 위에서 결과에 반영 완료)
            continue

        # 이후 로직은 전부 "자격 있는 후보(eligible)"만을 대상으로 동작합니다.
        g = eligible
        is_multi = len(g) > 1
        if is_multi:
            group_counter += 1
        this_grp_id = group_counter if is_multi else None

        for p in g:
            p['conflict_group'] = this_grp_id
            p['status'] = None  # 아래에서 결과에 따라 채워짐 (플레이스홀더)

        def fairness_sort_key(p):
            sat_clean = p['satellite'].split("(")[0].strip()
            is_target = sat_clean in equalize_target_sats

            curr_cnt = sat_selected_counts.get(sat_clean, 0)
            min_req = min_pass_targets.get(sat_clean, 0)
            max_cap = max_pass_targets.get(sat_clean, 0)  # 0이면 상한 없음

            # (0) 이미 최대 상한 횟수(Max Cap)에 도달한 위성은 최후순위로 격하(99)
            #     (hard_reject_fn이 이미 이 경우를 걸러내지만, 방어적으로 이중 체크)
            if max_cap > 0 and curr_cnt >= max_cap:
                return (99, 99, 99, 0)

            if equalize_allocation:
                need_more = 0 if (is_target and curr_cnt < min_req) else 1
                target_flag = 0 if is_target else 1
                return (need_more, target_flag, curr_cnt, -p['duration'])
            else:
                return (0, 0, 0, -p['duration'])

        def _hard_reject(p):
            # 💡 [버그 수정] 슬롯이 남아돌아도, 이미 상한을 넘긴 위성은 절대 수용하지 않습니다.
            sat_clean = p['satellite'].split("(")[0].strip()
            curr_cnt = sat_selected_counts.get(sat_clean, 0)
            max_cap = max_pass_targets.get(sat_clean, 0)
            if max_cap > 0 and curr_cnt >= max_cap:
                p['status'] = f"Capped (Max {max_cap})"
                return True
            return False

        def _on_admit(p):
            sat_clean = p['satellite'].split("(")[0].strip()
            sat_selected_counts[sat_clean] = sat_selected_counts.get(sat_clean, 0) + 1
            # "Conflict"가 아니라 "Concurrent"로 표기하여, UI에서 진짜 경합과 구분되도록 합니다
            # (populate_table의 "Conflict" 키워드 하이라이트에 안 걸림).
            p['status'] = f"Concurrent (Grp {this_grp_id})" if is_multi else "Normal"

        def _on_evict(p):
            sat_clean = p['satellite'].split("(")[0].strip()
            sat_selected_counts[sat_clean] = max(0, sat_selected_counts.get(sat_clean, 0) - 1)
            p['status'] = f"Conflict (Grp {this_grp_id})"

        resolve_station_group_with_capacity(
            g, capacity_fn, fairness_sort_key, on_admit=_on_admit, on_evict=_on_evict,
            hard_reject_fn=_hard_reject
        )

        # 경합에서 그냥 진(=콜백을 한 번도 못 받은) 패스들의 상태를 채웁니다.
        for p in g:
            if p['status'] is None:
                # len(g)==1인데 여기 도달했다면, 상한 초과도 아니고 경합도 없었는데 못 뽑힌 것
                # -> 그 시점에 안테나 용량 자체가 0(전면 점검 중)이었다는 뜻입니다.
                p['status'] = f"Conflict (Grp {this_grp_id})" if is_multi else "No Antenna Available"

        calculated_passes.extend(g)

    calculated_passes.sort(key=lambda x: (x['aos'], x['station']))
    return calculated_passes