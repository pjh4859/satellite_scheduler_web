"""
[2.6단계] 미션 플랜 배정 엔진 (TX 자원 추적 포함) — 데스크톱 버전 tab3_final_scheduler.py 이식

이번 단계에서 이식한 범위 (의도적으로 좁혔습니다):
- 데스크톱 버전은 4가지 배정 전략(Strict Sequential / Look-Ahead / Standby Fill /
  Cross-Satellite Swap)이 있었는데, 이번엔 그 중 가장 기본인 **Strict Sequential**만
  이식했습니다. 나머지 3개는 로직이 꽤 복잡해서(특히 Cross-Satellite Swap) 이후
  단계에서 필요하면 추가하는 걸로 남겨뒀습니다.
- 위성마다 "작업 목록(순서대로 처리)"을 미리 정의해두면, 각 패스가 순서대로 그
  목록의 맨 앞 작업을 수행할 수 있는지 검사합니다:
    - 고도각/지속시간이 충분한지
    - 지상국이 요구 자원(CMD/DOWN/BOTH)을 지원하는지
    - 그 지상국의 TX(커맨딩) 채널이 그 시간대에 비어있는지 (점검 일정 반영)
    - 선행 작업(pre_req)이 이미 끝났는지
  전부 통과하면 배정(Allocated), 하나라도 걸리면 대기(Bypassed)로 표시합니다.
"""


def normalize_sat_name(name):
    """'ISS (ZARYA)(25544)' 같은 표시용 이름에서 괄호 부분을 떼고 비교용 이름을 만듭니다."""
    if not name:
        return ""
    return name.split("(")[0].strip().upper()


def _build_station_caps(station_data, antenna_overrides):
    """지상국별 CMD/DOWN 지원 여부 + 시간대별 TX 용량 조회 함수를 구성합니다."""
    from scheduler_full import build_capacity_lookup

    caps = {}
    for st in station_data:
        st_name = st[0]
        is_down = (str(st[3]).strip().upper() == 'Y') if len(st) > 3 else True
        is_cmd = (str(st[4]).strip().upper() == 'Y') if len(st) > 4 else True
        base_tx = int(st[6]) if len(st) > 6 else 1
        overrides_for_station = [ov for ov in antenna_overrides if ov.get("station") == st_name]
        tx_capacity_fn = build_capacity_lookup(base_tx, overrides_for_station, "tx_capacity")
        caps[st_name] = {"cmd": is_cmd, "down": is_down, "tx_capacity_fn": tx_capacity_fn}
    return caps


def _count_tx_overlaps(tx_occupied, st_name, aos, los):
    count = 0
    for occ_aos, occ_los in tx_occupied.get(st_name, []):
        if not (occ_los <= aos or occ_aos >= los):
            count += 1
    return count


def _register_tx_usage(tx_occupied, st_name, aos, los):
    tx_occupied.setdefault(st_name, []).append((aos, los))


def _evaluate_task(task, p_el, p_dur, st_info, st_name, completed_mains, tx_occupied_count, tx_capacity):
    reasons = []

    if p_el < task["min_el"]:
        reasons.append(f"El Low ({p_el}° < {task['min_el']}°)")
    if p_dur < task["min_dur"]:
        reasons.append(f"Dur Short ({p_dur}s < {task['min_dur']}s)")

    req = task["req_cap"]
    if req == "CMD" and not st_info["cmd"]:
        reasons.append(f"GS {st_name} No CMD")
    elif req == "DOWN" and not st_info["down"]:
        reasons.append(f"GS {st_name} No DOWN")
    elif req == "BOTH":
        if not st_info["cmd"] and not st_info["down"]:
            reasons.append(f"GS {st_name} No CMD/DOWN")
        elif not st_info["cmd"]:
            reasons.append(f"GS {st_name} No CMD")
        elif not st_info["down"]:
            reasons.append(f"GS {st_name} No DOWN")

    # 💡 TX 자원 추적 (점검 일정으로 인한 시간대별 용량 변화까지 반영됨)
    if req in ("CMD", "BOTH") and tx_occupied_count >= tx_capacity:
        reasons.append(f"GS {st_name} TX Busy ({tx_occupied_count}/{tx_capacity})")

    # ⚠️ [버그 수정] pre_req는 대문자로 비교하면서 completed_mains는 원문 그대로 두면
    # (예: "Initial Contact" != "INITIAL CONTACT") 이미 끝난 선행 작업도 "Not Met"으로
    # 잘못 판정됩니다. 데스크톱 버전처럼 양쪽 다 대문자로 맞춰서 비교해야 합니다.
    pre_req = task["pre_req_main"].strip().upper()
    completed_upper = {m.strip().upper() for m in completed_mains}
    if pre_req not in ("NONE", "NULL", "") and pre_req not in completed_upper:
        reasons.append(f"Pre-req '{task['pre_req_main']}' Not Met")

    return reasons


def run_mission_schedule(raw_pass_data, raw_constraint_data, station_data, antenna_overrides=None):
    """
    :param raw_pass_data: calculate_passes()의 결과 중 selected=True인 패스들
                          (aos/los는 실제 datetime 객체여야 함)
    :param raw_constraint_data: [{"sat_id", "sequence_id", "main", "req_cap"(CMD/DOWN/BOTH/NONE),
                                   "min_el", "min_dur", "pre_req_main", "remark"(선택)}, ...]
    :param station_data: calculate_passes()에 넘긴 것과 동일한 지상국 튜플 리스트
    :param antenna_overrides: 점검 일정 리스트 (없으면 빈 리스트)
    :return: [{"station","satellite","pass_no","aos","los","duration","max_el",
                "status"(Allocated/Bypassed/Idle),"activity","remark"}, ...]
    """
    antenna_overrides = antenna_overrides or []
    station_caps = _build_station_caps(station_data, antenna_overrides)
    tx_occupied = {}

    sat_plans = {}
    for act in raw_constraint_data:
        norm = normalize_sat_name(act.get("sat_id", ""))
        if not norm:
            continue
        sat_plans.setdefault(norm, []).append(dict(act, completed=False))
    for norm in sat_plans:
        sat_plans[norm].sort(key=lambda t: t.get("sequence_id", 0))

    sat_completed_mains = {k: set() for k in sat_plans}

    sorted_passes = sorted(raw_pass_data, key=lambda p: p["aos"])
    final_schedule = []

    for p in sorted_passes:
        p_sat_norm = normalize_sat_name(p["satellite"])
        st_name = p["station"]
        p_el = float(p.get("max_el", 0))
        p_dur = float(p.get("duration", 0))
        p_aos, p_los = p["aos"], p["los"]

        st_info = station_caps.get(st_name, {"cmd": True, "down": True, "tx_capacity_fn": lambda dt: 1})
        tx_occupied_count = _count_tx_overlaps(tx_occupied, st_name, p_aos, p_los)
        tx_capacity = st_info["tx_capacity_fn"](p_aos)

        plan = sat_plans.get(p_sat_norm, [])
        uncompleted = [t for t in plan if not t["completed"]]

        row = {
            "station": st_name, "satellite": p["satellite"], "pass_no": p.get("pass_no", ""),
            "aos": p_aos.strftime("%Y-%m-%d %H:%M:%S"), "los": p_los.strftime("%Y-%m-%d %H:%M:%S"),
            "duration": p_dur, "max_el": p_el,
        }

        if not uncompleted:
            row.update(status="Idle", activity="📡 Standby / Idle Operations (모든 작업 완료됨)", remark="")
            final_schedule.append(row)
            continue

        primary_task = uncompleted[0]
        reasons = _evaluate_task(primary_task, p_el, p_dur, st_info, st_name,
                                  sat_completed_mains[p_sat_norm], tx_occupied_count, tx_capacity)

        if not reasons:
            primary_task["completed"] = True
            sat_completed_mains[p_sat_norm].add(primary_task["main"])
            if primary_task["req_cap"] in ("CMD", "BOTH"):
                _register_tx_usage(tx_occupied, st_name, p_aos, p_los)
            row.update(
                status="Allocated",
                activity=f"[{primary_task.get('sequence_id', '')}] {primary_task['main']}",
                remark=primary_task.get("remark", "")
            )
        else:
            row.update(
                status="Bypassed",
                activity=f"[{primary_task['main']}] Blocked ({', '.join(reasons)})",
                remark=primary_task.get("remark", "")
            )

        final_schedule.append(row)

    return final_schedule
