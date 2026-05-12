"""
잭킹 작업 공용 서비스
- 로봇 REST API 헬퍼 함수
- 잭킹 흐름 실행 (align_with_rack → jack_up → to_unload_point → jack_down → standby)
"""
import logging
import threading as _threading
import time
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

ROBOT_PORT = 8090
HTTP_TIMEOUT = 10
import json as _json


def get_docking_point_coords(ip: str, charger_name: str):
    """로봇 맵에서 충전소 도킹포인트(type=36) 좌표 조회 → (x, y, yaw) 또는 None"""
    try:
        r = requests.get(f"http://{ip}:{ROBOT_PORT}/chassis/current-map", timeout=HTTP_TIMEOUT)
        map_id = r.json().get("id")
        if not map_id:
            return None
        r = requests.get(f"http://{ip}:{ROBOT_PORT}/maps/{map_id}", timeout=HTTP_TIMEOUT)
        overlays = _json.loads(r.json().get("overlays", "{}"))
        features = overlays.get("features", [])

        # 충전소(type=9)에서 도킹포인트 ID 찾기
        docking_point_id = None
        for feat in features:
            props = feat.get("properties", {})
            if str(props.get("type")) == "9" and props.get("name") == charger_name:
                docking_point_id = props.get("dockingPointId")
                break

        if not docking_point_id:
            return None

        # 도킹포인트(type=36) 좌표 조회
        for feat in features:
            if feat.get("id") == docking_point_id:
                coords = feat.get("geometry", {}).get("coordinates", [])
                props = feat.get("properties", {})
                yaw = float(props.get("yaw", 0))
                if len(coords) >= 2:
                    logger.info(f"[{ip}] '{charger_name}' 도킹포인트: ({coords[0]}, {coords[1]}, yaw={yaw})")
                    return (coords[0], coords[1], yaw)
        return None
    except Exception as e:
        logger.warning(f"[{ip}] 도킹포인트 좌표 조회 실패: {e}")
        return None
POLL_INTERVAL = 1.0
MOVE_TIMEOUT = 120

# 실행 중인 작업 추적 (robot_ip → stop flag)
_stop_flags: dict[str, bool] = {}

# 셔틀 작업 안전정지 플래그 (robot_ip → True): 현재 사이클 끝나면 충전소로 복귀
_shuttle_stop_flags: dict[str, bool] = {}

# ── W 영역 세마포어 (그룹 셔틀 데드락 차단) ──
# 한 번에 한 로봇만 W 영역(픽업 정렬/잭킹/복귀 진입)을 점유 가능.
# 다른 구간(C 이동, C 정렬/잭킹)은 락 없이 자유 진행 → 병렬성 유지.
_w_zone_lock = _threading.Lock()
_w_zone_holder: str | None = None             # 현재 점유 중인 로봇 IP
_w_zone_acquired_at: float = 0.0              # 획득 시각
_w_zone_progress_at: float = 0.0              # 마지막 진행(heartbeat) 시각
_w_zone_label: str = ""                       # 현재 단계 라벨 (디버깅용)
W_ZONE_STUCK_TIMEOUT = 240                    # 진행 정체 시 강제 해제 임계 (초)
W_ZONE_WAIT_POLL = 1.0                        # 락 대기 폴링 간격 (초)


def _w_zone_force_release_if_stuck() -> bool:
    """락 보유자가 W_ZONE_STUCK_TIMEOUT 동안 progress 갱신 없으면 강제 해제.
    True 반환 시 강제 해제됨."""
    global _w_zone_holder, _w_zone_acquired_at, _w_zone_progress_at, _w_zone_label
    if not _w_zone_holder:
        return False
    if time.time() - _w_zone_progress_at < W_ZONE_STUCK_TIMEOUT:
        return False
    stuck_ip = _w_zone_holder
    stuck_label = _w_zone_label
    held = time.time() - _w_zone_acquired_at
    logger.warning(
        f"[w-zone] 스턱 감지 — {stuck_ip} ({stuck_label}) "
        f"{held:.0f}s 보유, 마지막 진행 {time.time() - _w_zone_progress_at:.0f}s 전 → 강제 해제"
    )
    _w_zone_holder = None
    _w_zone_acquired_at = 0.0
    _w_zone_progress_at = 0.0
    _w_zone_label = ""
    return True


def acquire_w_zone(
    ip: str,
    label: str = "",
    on_status: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """W 영역 락 획득. 다른 로봇이 점유 중이면 풀릴 때까지 대기.
    긴급정지(_check_stop)는 응답.
    """
    global _w_zone_holder, _w_zone_acquired_at, _w_zone_progress_at, _w_zone_label
    waited = 0.0
    notified = False
    while True:
        _check_stop(ip)
        with _w_zone_lock:
            if _w_zone_holder is None or _w_zone_holder == ip:
                _w_zone_holder = ip
                now = time.time()
                _w_zone_acquired_at = now
                _w_zone_progress_at = now
                _w_zone_label = label
                if waited > 0:
                    logger.info(f"[w-zone] {ip} 락 획득 ({label}, {waited:.0f}s 대기 후)")
                else:
                    logger.info(f"[w-zone] {ip} 락 획득 ({label})")
                return True
            # 다른 로봇이 점유 — 스턱 체크 후 양보
            if _w_zone_force_release_if_stuck():
                continue  # 강제 해제됐으니 다음 루프에서 즉시 획득 시도
            current_holder = _w_zone_holder
            current_label = _w_zone_label
        if on_status and not notified:
            on_status("waiting", f"W 영역 점유 대기 중 ({current_holder} {current_label})")
            notified = True
        time.sleep(W_ZONE_WAIT_POLL)
        waited += W_ZONE_WAIT_POLL


def progress_w_zone(ip: str):
    """락 보유자가 단계 진행할 때 호출 — 스턱 타임아웃 리셋"""
    global _w_zone_progress_at, _w_zone_label
    if _w_zone_holder == ip:
        _w_zone_progress_at = time.time()


def update_w_zone_label(ip: str, label: str):
    """디버깅용 단계 라벨 갱신 + progress 갱신"""
    global _w_zone_label, _w_zone_progress_at
    if _w_zone_holder == ip:
        _w_zone_label = label
        _w_zone_progress_at = time.time()


def release_w_zone(ip: str):
    """W 영역 락 해제 — 자기가 보유한 경우에만"""
    global _w_zone_holder, _w_zone_acquired_at, _w_zone_progress_at, _w_zone_label
    with _w_zone_lock:
        if _w_zone_holder != ip:
            return
        held = time.time() - _w_zone_acquired_at
        logger.info(f"[w-zone] {ip} 락 해제 ({_w_zone_label}, {held:.0f}s 보유)")
        _w_zone_holder = None
        _w_zone_acquired_at = 0.0
        _w_zone_progress_at = 0.0
        _w_zone_label = ""

# 실행 중인 작업 상태 (robot_ip → job info)
_job_status: dict[str, dict] = {}

# 수동 확인 대기 (robot_ip → threading.Event)
_confirm_events: dict[str, _threading.Event] = {}

# 다음 포인트 (robot_ip → poi dict or "return")
_next_poi: dict[str, dict | str | None] = {}


def _get_standby_poi(area_id: int | None = None) -> dict | None:
    """DB에서 지정 영역 또는 최신 활성 맵의 standby POI(W1) 조회"""
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap
    db = SessionLocal()
    try:
        if area_id:
            active_map = db.query(RobotMap).filter(
                RobotMap.area_id == area_id, RobotMap.is_active == True
            ).order_by(RobotMap.id.desc()).first()
        else:
            active_map = db.query(RobotMap).filter(RobotMap.is_active == True).order_by(RobotMap.id.desc()).first()
        if not active_map:
            return None
        poi = db.query(MapPOI).filter(
            MapPOI.map_id == active_map.id,
            MapPOI.poi_type == "standby",
            MapPOI.is_active == True,
        ).first()
        if poi and poi.world_x is not None:
            return {"name": poi.name, "x": poi.world_x, "y": poi.world_y, "ori": poi.angle or 0}
        return None
    finally:
        db.close()


def wait_for_confirm(robot_ip: str, timeout: int | None = None) -> bool:
    """사용자 확인 버튼을 기다림. True=확인됨, False=타임아웃. timeout=None이면 무한 대기"""
    evt = _threading.Event()
    _confirm_events[robot_ip] = evt
    result = evt.wait(timeout=timeout)
    _confirm_events.pop(robot_ip, None)
    return result


def confirm_robot(robot_ip: str):
    """사용자가 확인 버튼을 눌렀을 때 호출"""
    evt = _confirm_events.get(robot_ip)
    if evt:
        evt.set()
        logger.info(f"[jack_service] confirm received for {robot_ip}")


def set_next_poi(robot_ip: str, poi: dict | str):
    """다음 포인트 설정 (poi dict 또는 'return') + confirm 트리거"""
    _next_poi[robot_ip] = poi
    confirm_robot(robot_ip)
    logger.info(f"[jack_service] next poi set for {robot_ip}: {poi if isinstance(poi, str) else poi.get('name')}")


def get_next_poi(robot_ip: str) -> dict | str | None:
    """다음 포인트 가져오기 (한 번 읽으면 제거)"""
    return _next_poi.pop(robot_ip, None)


def stop_robot_job(robot_ip: str):
    """특정 로봇의 진행 중인 작업에 중지 플래그 설정 + 상태 제거"""
    _stop_flags[robot_ip] = True
    _job_status.pop(robot_ip, None)
    _next_poi.pop(robot_ip, None)
    logger.info(f"[jack_service] stop flag set for {robot_ip}")


def request_shuttle_stop(robot_ip: str):
    """셔틀 안전정지 요청 — 현재 사이클이 끝난 직후 충전소로 복귀"""
    _shuttle_stop_flags[robot_ip] = True
    logger.info(f"[jack_service] shuttle stop requested for {robot_ip}")


def is_shuttle_stop_requested(robot_ip: str) -> bool:
    return bool(_shuttle_stop_flags.get(robot_ip))


def clear_shuttle_stop(robot_ip: str):
    _shuttle_stop_flags.pop(robot_ip, None)


def _check_stop(robot_ip: str):
    """중지 플래그 확인 — True면 예외 발생"""
    if _stop_flags.get(robot_ip):
        _stop_flags.pop(robot_ip, None)
        raise RuntimeError(f"작업 중지됨 (robot={robot_ip})")


def _interruptible_sleep(robot_ip: str, seconds: float):
    """중지 가능한 대기 — 1초 간격으로 stop 플래그 체크"""
    elapsed = 0.0
    while elapsed < seconds:
        _check_stop(robot_ip)
        sleep_time = min(1.0, seconds - elapsed)
        time.sleep(sleep_time)
        elapsed += sleep_time


def update_job_status(robot_ip: str, **kwargs):
    """작업 상태 업데이트"""
    if robot_ip not in _job_status:
        _job_status[robot_ip] = {}
    _job_status[robot_ip].update(kwargs)


def clear_job_status(robot_ip: str):
    """작업 상태 제거"""
    _job_status.pop(robot_ip, None)


def get_job_status(robot_ip: str) -> dict | None:
    """작업 상태 조회"""
    return _job_status.get(robot_ip)


def get_all_job_status() -> dict:
    """모든 로봇 작업 상태 조회"""
    return dict(_job_status)


# ── 로봇 REST API 헬퍼 ──

def robot_url(ip: str, path: str) -> str:
    return f"http://{ip}:{ROBOT_PORT}{path}"


def robot_get(ip: str, path: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(robot_url(ip, path), timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise


def robot_post(ip: str, path: str, json_body: dict | None = None, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.post(robot_url(ip, path), json=json_body or {}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise


def robot_patch(ip: str, path: str, json_body: dict) -> dict:
    r = requests.patch(robot_url(ip, path), json=json_body, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def create_move(ip: str, move_type: str, target_x: float, target_y: float,
                target_ori: float = 0, retries: int = 5, **extra) -> int:
    body = {
        "creator": "rcs",
        "type": move_type,
        "target_x": target_x,
        "target_y": target_y,
        "target_ori": target_ori,
        **extra,
    }
    for attempt in range(retries):
        try:
            resp = robot_post(ip, "/chassis/moves", body)
            return resp.get("id")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and attempt < retries - 1:
                logger.warning(f"[move] 400 에러, {5}초 후 재시도 ({attempt+1}/{retries})")
                time.sleep(5)
            else:
                from app.crud.activity_log import log_activity
                log_activity("robot", "move_error", f"이동 명령 실패 ({move_type}): {str(e)}", source="jack_service")
                raise


def wait_move(ip: str, move_id: int, timeout: int = MOVE_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _check_stop(ip)
        resp = robot_get(ip, f"/chassis/moves/{move_id}")
        state = resp.get("state", "")
        if state in ("succeeded", "failed", "cancelled"):
            return resp
        time.sleep(POLL_INTERVAL)
    return {"state": "timeout", "fail_message": f"Move {move_id} timed out after {timeout}s"}


def jack_up(ip: str) -> dict:
    return robot_post(ip, "/services/jack_up")


def jack_down(ip: str) -> dict:
    return robot_post(ip, "/services/jack_down")


def cancel_current_move(ip: str) -> dict:
    return robot_patch(ip, "/chassis/moves/current", {"state": "cancelled"})


JACK_WAIT_SEC = 10  # 잭 업/다운 고정 대기 시간(초)
JACK_IDLE_TIMEOUT = 30  # 잭 다운 후 로봇 idle 대기 최대 시간
def align_with_retry(ip: str, x: float, y: float, ori: float = 0) -> dict:
    """align_with_rack 시도 + 일시적 실패 시 1회 재시도.

    재시도 케이스:
    - 'jack is in up state' / 'lifted' → jack_down 호출 후 JACK_WAIT_SEC 대기 후 재시도
    - 'rack_detection_error' / 'detection_failed' / 'failed to find rack'
      → 짧은 대기(LiDAR 노이즈 가능성) 후 재시도. 영구 어긋남이면 결국 실패.
    """
    _check_stop(ip)
    move_id = create_move(ip, "align_with_rack", x, y, ori)
    result = wait_move(ip, move_id, timeout=120)
    if result.get("state") == "succeeded":
        return result

    fail_msg = (result.get("fail_message") or "").lower()

    # case 1: 잭 업 상태에서 align 시도 → jack_down 후 재시도
    if "jack" in fail_msg and ("up" in fail_msg or "lifted" in fail_msg):
        logger.warning(f"[{ip}] align 실패 — '{result.get('fail_message')}', jack_down 후 재시도")
        try:
            jack_down(ip)
        except Exception as e:
            logger.warning(f"[{ip}] 재시도용 jack_down 실패(무시): {e}")
        _interruptible_sleep(ip, JACK_WAIT_SEC)
    # case 2: 랙 감지 실패 → 짧은 대기 후 재시도 (LiDAR 노이즈 가능성)
    elif any(k in fail_msg for k in ("rack_detection", "detection_failed", "find rack", "rack_not_found", "rack not found")):
        logger.warning(f"[{ip}] align 실패 — '{result.get('fail_message')}', 3초 후 재시도(LiDAR 노이즈 가능성)")
        _interruptible_sleep(ip, 3)
    else:
        # 다른 종류 실패 — 즉시 반환
        return result

    _check_stop(ip)
    move_id = create_move(ip, "align_with_rack", x, y, ori)
    return wait_move(ip, move_id, timeout=120)


def unload_point_with_retry(
    ip: str, x: float, y: float, ori: float = 0,
    retries: int = 3, wait_between: float = 10.0,
    move_timeout: int = 240,
    on_status: Optional[Callable[[str, str], None]] = None,
    target_label: str = "",
) -> dict:
    """to_unload_point 시도. 일시적 장애(점유/장애물/충돌)나 타임아웃 발생 시
    wait_between초 간격으로 retries회까지 재시도.

    move_timeout은 단일 시도의 wait_move 타임아웃 (3대 동시 운영 시 경로 충돌 회피로
    인해 도착이 느려질 수 있어 120초 → 240초로 기본값 상향)."""
    last = None
    # 일시적 실패로 간주할 키워드 (3대 동시 운영 시 회피 누적/일시 점유)
    RETRY_KEYWORDS = (
        "occupied", "blocked", "obstacle", "collision", "stuck",
        "timed out", "timeout",
        "unreachable",            # 회피 동작으로 일시적으로 도달 불가
        "planning",               # planning_failed
        "no_path", "no path",
        "in_use", "in use",
    )
    for attempt in range(retries):
        _check_stop(ip)
        mv = create_move(ip, "to_unload_point", x, y, ori)
        result = wait_move(ip, mv, timeout=move_timeout)
        state = result.get("state", "")
        if state == "succeeded":
            return result
        last = result
        msg = (result.get("fail_message") or "").lower()
        # 재시도 조건:
        # - 명시적 키워드 매칭
        # - state가 timeout
        # - fail_message가 비어있는 실패 (3대 동시 운영 환경에서 흔히 일시적)
        retryable = (
            state == "timeout"
            or (state != "succeeded" and not msg)
            or any(k in msg for k in RETRY_KEYWORDS)
        )
        if retryable and attempt < retries - 1:
            if state == "timeout":
                reason = "타임아웃"
            elif not msg:
                reason = f"일시 실패(state={state or 'unknown'})"
            elif "unreachable" in msg or "planning" in msg or "no_path" in msg or "no path" in msg:
                reason = "경로 차단(회피 누적)"
            else:
                reason = "점유/장애물"
            logger.warning(
                f"[{ip}] to_unload_point 일시 실패 ({attempt+1}/{retries}, {reason}): "
                f"state={state!r} msg={result.get('fail_message')!r} → {wait_between}초 후 재시도"
            )
            if on_status:
                on_status(
                    "waiting",
                    f"{target_label or '목적지'} {reason} — {int(wait_between)}초 후 재시도 ({attempt+1}/{retries})",
                )
            _interruptible_sleep(ip, wait_between)
            continue
        break
    return last or {"state": "failed", "fail_message": "to_unload_point 실패"}


def wait_robot_idle(ip: str, timeout: int = JACK_IDLE_TIMEOUT):
    """로봇이 idle(현재 이동 없음) 상태가 될 때까지 폴링"""
    deadline = time.time() + timeout
    time.sleep(3)  # 최소 대기
    while time.time() < deadline:
        try:
            r = requests.get(robot_url(ip, "/chassis/moves/current"), timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                # Not found = 현재 이동 없음 = idle
                return True
            data = r.json()
            state = data.get("state", "")
            if state in ("succeeded", "failed", "cancelled", ""):
                return True
        except Exception:
            pass
        time.sleep(1)
    logger.warning(f"[jack] 로봇 idle 대기 타임아웃 ({timeout}초)")
    return False


# ── 잭킹 작업 실행 ──

def run_jack_job(
    ip: str,
    pickup: dict,
    dropoff: dict,
    standby: Optional[dict] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """잭킹 작업 동기 실행
    pickup/dropoff/standby: {"name": str, "x": float, "y": float, "ori": float}
    on_status(status, message): 상태 변경 콜백
    반환: {"status": "done"|"error", "message": str}
    """
    def _notify(status: str, message: str):
        if on_status:
            on_status(status, message)
        logger.info(f"[jack-job] {status}: {message}")

    def _fail(msg: str) -> dict:
        """에러 복구 + 에러 리턴"""
        _notify("error", msg)
        try:
            cancel_current_move(ip)
        except Exception:
            pass
        try:
            jack_down(ip)
        except Exception:
            pass
        return {"status": "error", "message": msg}

    try:
        # 1) align_with_rack (재시도 포함)
        _notify("aligning", f"픽업 위치({pickup['name']})로 랙 정렬 이동 중...")
        result = align_with_retry(ip, pickup["x"], pickup["y"], pickup.get("ori", 0))
        if result["state"] != "succeeded":
            msg = f"랙 정렬 실패: {result.get('fail_message', result['state'])}"
            _notify("error", msg)
            return _fail(msg)

        # 2) 잭 업
        _notify("jacking_up", "잭 올리는 중...")
        jack_up(ip)
        time.sleep(JACK_WAIT_SEC)

        # 3) to_unload_point
        _notify("moving_to_dropoff", f"드롭오프 위치({dropoff['name']})로 이동 중...")
        move_id = create_move(ip, "to_unload_point", dropoff["x"], dropoff["y"], dropoff.get("ori", 0))
        result = wait_move(ip, move_id, timeout=120)
        if result["state"] != "succeeded":
            msg = f"드롭오프 이동 실패: {result.get('fail_message', result['state'])}"
            _notify("error", msg)
            return _fail(msg)

        # 4) 잭 다운
        _notify("jacking_down", "잭 내리는 중...")
        jack_down(ip)
        time.sleep(JACK_WAIT_SEC)

        # 5) 대기장소 복귀
        if standby:
            _notify("returning", f"대기장소({standby['name']})로 복귀 중...")
            move_id = create_move(ip, "standard", standby["x"], standby["y"], standby.get("ori", 0))
            result = wait_move(ip, move_id)
            if result["state"] != "succeeded":
                msg = f"복귀 실패: {result.get('fail_message', result['state'])}"
                _notify("error", msg)
                return _fail(msg)

        summary = f"완료: {pickup['name']} → {dropoff['name']} → {standby['name'] if standby else '정지'}"
        _notify("done", summary)
        return {"status": "done", "message": summary}

    except Exception as e:
        msg = f"오류: {str(e)}"
        _notify("error", msg)
        return _fail(msg)


def run_route_job(
    ip: str,
    waypoints: list[dict],
    on_status: Optional[Callable[[str, str], None]] = None,
    manual_confirm: bool = False,
    skip_standby_pickup: bool = False,
    skip_standby_return: bool = False,
    is_main_floor: bool = True,
    area_id: int | None = None,
) -> dict:
    """경로 기반 작업 실행
    waypoints: [{"name", "x", "y", "ori", "waypoint_type", "poi_type", "wait_sec"}, ...]
    waypoint_type: pickup / dropoff / standby / charging
    poi_type: jack / standby / charging / general 등
    """
    total_steps = len(waypoints)
    route_names = " → ".join(w["name"] for w in waypoints)

    def _notify(status: str, message: str, step: int = 0):
        if on_status:
            on_status(status, message)
        logger.info(f"[route-job] {status}: {message}")
        update_job_status(ip,
            status=status,
            message=message,
            route=route_names,
            current_step=step,
            total_steps=total_steps,
            started_at=_job_status.get(ip, {}).get("started_at", time.time()),
        )

    def _fail(msg: str) -> dict:
        """에러 복구 + 에러 리턴"""
        _notify("error", msg)
        clear_job_status(ip)
        try:
            cancel_current_move(ip)
        except Exception:
            pass
        try:
            jack_down(ip)
        except Exception:
            pass
        from app.crud.activity_log import log_activity as _log
        _log("robot", "task_error", f"작업 실패: {msg}", source="jack_service")
        return {"status": "error", "message": msg}

    jacked_up = False  # 잭 올림 상태 추적
    update_job_status(ip, status="started", route=route_names, current_step=0,
                      total_steps=total_steps, started_at=time.time(), message="작업 시작")

    # 활동 로그 기록
    from app.crud.activity_log import log_activity
    log_activity("robot", "task_start", f"작업 시작: {route_names}", source="jack_service")

    _move_with_rack = "to_unload_point"

    try:
        # ── 위치 보정 (충전소에서 출발 시 SLAM 매칭 불량 방지) ──
        try:
            _notify("aligning", "위치 보정 중...", 0)
            requests.post(f"http://{ip}:{ROBOT_PORT}/services/start_global_positioning",
                          json={}, timeout=5)
            # 위치 보정 완료 대기 (최대 5초, 매칭되면 즉시 종료)
            for _ in range(5):
                _check_stop(ip)
                time.sleep(1)
                try:
                    import websocket as _ws, json as _json
                    ws = _ws.create_connection(f"ws://{ip}:{ROBOT_PORT}/ws/v2/topics", timeout=2)
                    deadline = time.time() + 1
                    while time.time() < deadline:
                        raw = ws.recv()
                        pkt = _json.loads(raw)
                        if pkt.get("topic") == "/slam/state" and pkt.get("lidar_matched"):
                            ws.close()
                            raise StopIteration
                    ws.close()
                except StopIteration:
                    break
                except Exception:
                    pass
            logger.info(f"[route-job] 위치 보정 완료 ({ip})")
        except Exception as e:
            logger.warning(f"[route-job] 위치 보정 실패 (무시): {e}")

        # ── 시작: 대기장소(W1)에서 랙 픽업 (첫 회차만) ──
        standby_poi = _get_standby_poi(area_id)
        if not is_main_floor:
            # 다른층: 이미 잭 업 상태 → W1 픽업 스킵
            jacked_up = True
            logger.info(f"[route-job] 다른층 모드: 잭 업 상태로 시작")
        elif standby_poi and not skip_standby_pickup:
            sname = standby_poi["name"]
            _check_stop(ip)
            _notify("aligning", f"대기장소({sname})에서 랙 픽업 중...", 0)
            result = align_with_retry(ip, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0))
            if result["state"] == "succeeded":
                _notify("jacking_up", f"대기장소({sname}) 잭 올리는 중...", 0)
                jack_up(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)
                jacked_up = True
            else:
                msg = f"대기장소({sname}) 랙 픽업 실패: {result.get('fail_message', '')}"
                return _fail(msg)

        for i, wp in enumerate(waypoints):
            name = wp["name"]
            wtype = wp["waypoint_type"]
            ptype = wp.get("poi_type", "general")
            wait_sec = wp.get("wait_sec", 0)

            if wtype == "pickup":
                if jacked_up:
                    # 잭 올린 상태 → 픽업 위치로 이동 → 잭 다운 → 물건 올림 → 잭 업
                    _notify("moving_to_dropoff", f"[{i+1}/{total_steps}] {name} 랙 배달 중...", i+1)
                    move_id = create_move(ip, _move_with_rack, wp["x"], wp["y"], wp.get("ori", 0))
                    result = wait_move(ip, move_id, timeout=120)
                    if result["state"] != "succeeded":
                        msg = f"{name} 이동 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_down", f"{name} 잭 내리는 중...", i+1)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = False

                    # 수동: 출발 버튼 누르면 자동으로 잭 업 → 출발 / 자동: wait_sec 대기
                    if manual_confirm:
                        _notify("waiting_confirm", f"{name} 물건 적재 후 출발 버튼을 눌러주세요", i+1)
                        if not wait_for_confirm(ip):
                            return _fail("출발 확인 타임아웃 (5분)")
                        _check_stop(ip)
                    elif wait_sec > 0:
                        _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                        _interruptible_sleep(ip, wait_sec)

                    # 잭 업 → 바로 출발 (출발 대기 없음)
                    _notify("aligning", f"{name} 랙 재정렬 중...", i+1)
                    result = align_with_retry(ip, wp["x"], wp["y"], wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"{name} 랙 재정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_up", f"{name} 잭 올리는 중...", i+1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = True
                else:
                    # 잭이 내려간 상태 → align_with_rack로 랙 픽업
                    _notify("aligning", f"[{i+1}/{total_steps}] {name} 랙 정렬 이동 중...", i+1)
                    result = align_with_retry(ip, wp["x"], wp["y"], wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"{name} 랙 정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    _notify("jacking_up", f"{name} 잭 올리는 중...", i+1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    jacked_up = True

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            elif wtype == "dropoff":
                # 드롭오프: 이동 → jack_down
                _notify("moving_to_dropoff", f"[{i+1}/{total_steps}] {name} 드롭오프 이동 중...", i+1)
                move_id = create_move(ip, _move_with_rack, wp["x"], wp["y"], wp.get("ori", 0))
                result = wait_move(ip, move_id, timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 드롭오프 이동 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "move_error", msg, source="jack_service")
                    return _fail(msg)

                _notify("jacking_down", f"{name} 잭 내리는 중...", i+1)
                jack_down(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)
                jacked_up = False

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            elif ptype == "charging" or wtype == "charging":
                # 충전소: 도킹포인트 좌표로 charge 명령
                _notify("charging", f"[{i+1}/{total_steps}] {name} 충전소 도킹 중...", i+1)
                cx, cy = wp["x"], wp["y"]
                cyaw = wp.get("ori", 0)
                move_id = create_move(ip, "charge", cx, cy, cyaw, charge_retry_count=3)
                result = wait_move(ip, move_id, timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 충전 도킹 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "dock_error", msg, source="jack_service")
                    return _fail(msg)

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

            else:
                # 대기/일반: standard 이동
                _notify("moving", f"[{i+1}/{total_steps}] {name} 이동 중...", i+1)
                move_id = create_move(ip, "standard", wp["x"], wp["y"], wp.get("ori", 0))
                result = wait_move(ip, move_id, timeout=120)
                if result["state"] != "succeeded":
                    msg = f"{name} 이동 실패: {result.get('fail_message', '')}"
                    log_activity("robot", "move_error", msg, source="jack_service")
                    return _fail(msg)

                if wait_sec > 0:
                    _notify("waiting", f"{name} 대기 중 ({wait_sec}초)...", i+1)
                    _interruptible_sleep(ip, wait_sec)

        # 마지막 드롭오프 후: 다음 포인트 / 복귀 선택 루프 (수동) 또는 바로 복귀 (자동)
        if (jacked_up is False or not is_main_floor) and not skip_standby_return:
            _check_stop(ip)
            standby_poi = _get_standby_poi(area_id)
            last_dropoff_wp = None
            for wp in reversed(waypoints):
                if wp["waypoint_type"] == "dropoff":
                    last_dropoff_wp = wp
                    break

            # ── 수동: 다음 포인트 / 복귀 루프 ──
            if manual_confirm and last_dropoff_wp and (standby_poi or not is_main_floor):
                current_wp = last_dropoff_wp
                extra_step = 0
                while True:
                    _check_stop(ip)
                    update_job_status(ip,
                        status="waiting_next_or_return",
                        message="다음 포인트를 선택하거나 복귀 버튼을 눌러주세요",
                        route=f"{current_wp['name']} → ?",
                        current_step=0, total_steps=1,
                        started_at=_job_status.get(ip, {}).get("started_at", time.time()),
                    )
                    if on_status:
                        on_status("waiting_next_or_return", "다음 포인트를 선택하거나 복귀 버튼을 눌러주세요")
                    if not wait_for_confirm(ip):
                        return _fail("선택 타임아웃")
                    _check_stop(ip)

                    next_poi = get_next_poi(ip)
                    if next_poi is None or next_poi == "return":
                        break

                    # 경로 업데이트
                    extra_step += 1
                    new_route = f"{current_wp['name']} → {next_poi['name']}"
                    update_job_status(ip, route=new_route, current_step=0, total_steps=2)

                    # 현재 위치에서 잭 업 → 다음 포인트 → 잭 다운
                    update_job_status(ip, status="aligning", message=f"{current_wp['name']} 랙 재정렬 중...", current_step=0)
                    _notify("aligning", f"{current_wp['name']} 랙 재정렬 중...", 0)
                    result = align_with_retry(ip, current_wp["x"], current_wp["y"], current_wp.get("ori", 0))
                    if result["state"] != "succeeded":
                        msg = f"랙 재정렬 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    update_job_status(ip, status="jacking_up", message="잭 올리는 중...", current_step=1)
                    _notify("jacking_up", "잭 올리는 중...", 1)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

                    update_job_status(ip, status="moving_to_dropoff", message=f"{next_poi['name']} 이동 중...", current_step=1)
                    _notify("moving_to_dropoff", f"{next_poi['name']} 이동 중...", 1)
                    move_id = create_move(ip, _move_with_rack, next_poi["x"], next_poi["y"], next_poi.get("ori", 0))
                    result = wait_move(ip, move_id, timeout=120)
                    if result["state"] != "succeeded":
                        msg = f"{next_poi['name']} 이동 실패: {result.get('fail_message', '')}"
                        log_activity("robot", "move_error", msg, source="jack_service")
                        return _fail(msg)

                    update_job_status(ip, status="jacking_down", message=f"{next_poi['name']} 잭 내리는 중...", current_step=2)
                    _notify("jacking_down", f"{next_poi['name']} 잭 내리는 중...", 2)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)
                    current_wp = next_poi
                    log_activity("robot", "dropoff", f"드롭오프: {next_poi['name']}", source="jack_service")

                # 복귀
                if not is_main_floor:
                    # 다른층: 잭 업만
                    _notify("aligning", f"랙 픽업 중...", total_steps)
                    result = align_with_retry(ip, current_wp["x"], current_wp["y"], current_wp.get("ori", 0))
                    if result["state"] == "succeeded":
                        _notify("jacking_up", "잭 올리는 중...", total_steps)
                        jack_up(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)
                elif is_main_floor:
                    sname = standby_poi["name"]
                    update_job_status(ip, route=f"{current_wp['name']} → {sname}", current_step=0, total_steps=2)
                    _notify("aligning", f"대기장소 이동을 위해 랙 재정렬 중...", 0)
                    result = align_with_retry(ip, current_wp["x"], current_wp["y"], current_wp.get("ori", 0))
                    if result["state"] == "succeeded":
                        _notify("jacking_up", "대기장소 이동을 위해 잭 올리는 중...", total_steps)
                        jack_up(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)

                        _notify("moving", f"대기장소({sname})로 이동 중...", total_steps)
                        move_id = create_move(ip, _move_with_rack, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0))
                        wait_move(ip, move_id, timeout=120)

                        _notify("jacking_down", f"대기장소({sname}) 잭 내리는 중...", total_steps)
                        jack_down(ip)
                        _interruptible_sleep(ip, JACK_WAIT_SEC)

            # ── 자동: 바로 복귀 (메인층만) ──
            elif standby_poi and last_dropoff_wp and is_main_floor:
                sname = standby_poi["name"]
                _notify("aligning", f"대기장소 이동을 위해 랙 재정렬 중...", total_steps)
                result = align_with_retry(ip, last_dropoff_wp["x"], last_dropoff_wp["y"], last_dropoff_wp.get("ori", 0))
                if result["state"] == "succeeded":
                    _notify("jacking_up", "대기장소 이동을 위해 잭 올리는 중...", total_steps)
                    jack_up(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

                    _notify("moving", f"대기장소({sname})로 이동 중...", total_steps)
                    move_id = create_move(ip, _move_with_rack, standby_poi["x"], standby_poi["y"], standby_poi.get("ori", 0))
                    wait_move(ip, move_id, timeout=120)

                    _notify("jacking_down", f"대기장소({sname}) 잭 내리는 중...", total_steps)
                    jack_down(ip)
                    _interruptible_sleep(ip, JACK_WAIT_SEC)

        _notify("done", f"완료: {route_names}", total_steps)
        clear_job_status(ip)
        log_activity("robot", "task_complete", f"작업 완료: {route_names}", source="jack_service")
        return {"status": "done", "message": f"완료: {route_names}"}

    except Exception as e:
        return _fail(f"오류: {str(e)}")


# ── 그룹 셔틀(왕복 반복) 작업 ──

def _list_charging_pois(area_id: int | None = None) -> list[dict]:
    """area의 충전소 POI 목록 (좌표 포함된 것만)"""
    from app.database import SessionLocal
    from app.models.map import MapPOI, RobotMap
    db = SessionLocal()
    try:
        if area_id:
            active_map = db.query(RobotMap).filter(
                RobotMap.area_id == area_id, RobotMap.is_active == True
            ).order_by(RobotMap.id.desc()).first()
            if not active_map:
                return []
            pois = db.query(MapPOI).filter(
                MapPOI.map_id == active_map.id,
                MapPOI.poi_type == "charging",
                MapPOI.is_active == True,
            ).all()
        else:
            pois = db.query(MapPOI).filter(
                MapPOI.poi_type == "charging",
                MapPOI.is_active == True,
            ).all()
        return [
            {"name": p.name, "x": p.world_x, "y": p.world_y, "ori": p.angle or 0}
            for p in pois
            if p.world_x is not None and p.world_y is not None
        ]
    finally:
        db.close()


def _get_charging_poi(area_id: int | None = None) -> dict | None:
    """area의 첫 충전소 POI (호환성 유지)"""
    pois = _list_charging_pois(area_id)
    return pois[0] if pois else None


def _get_nearest_charging_poi(area_id: int | None, ref_x: float, ref_y: float) -> dict | None:
    """ref 좌표에서 가장 가까운 충전소 POI"""
    pois = _list_charging_pois(area_id)
    if not pois:
        return None
    return min(pois, key=lambda p: (p["x"] - ref_x) ** 2 + (p["y"] - ref_y) ** 2)


def _get_robot_position_db(robot_ip: str) -> tuple[float, float] | None:
    """DB의 RobotStatus에서 로봇 현재 위치 조회 (WebSocket으로 실시간 갱신됨)"""
    from app.database import SessionLocal
    from app.models.robot import Robot
    db = SessionLocal()
    try:
        robot = db.query(Robot).filter(Robot.ip_address == robot_ip).first()
        if not robot or not robot.status:
            return None
        st = robot.status
        if st.position_x is None or st.position_y is None:
            return None
        return float(st.position_x), float(st.position_y)
    finally:
        db.close()


def _dock_to_charger(ip: str, charger: dict, on_status: Optional[Callable[[str, str], None]] = None) -> bool:
    """충전소 도킹 (standard 접근 → charge 도킹, 5회 재시도)"""
    def _n(s, m):
        if on_status:
            on_status(s, m)
        logger.info(f"[shuttle-return] {s}: {m}")

    cx, cy, cyaw = charger["x"], charger["y"], charger.get("ori", 0)
    try:
        _n("returning", f"충전소({charger['name']})로 이동 중...")
        std_move = create_move(ip, "standard", cx, cy, cyaw)
        wait_move(ip, std_move, timeout=120)
    except Exception as e:
        logger.warning(f"[shuttle-return] standard 이동 실패: {e}")
    time.sleep(3)

    for attempt in range(5):
        try:
            _n("charging", f"충전소({charger['name']}) 도킹 중... ({attempt+1}/5)")
            move_id = create_move(ip, "charge", cx, cy, cyaw, charge_retry_count=3)
            wait_move(ip, move_id, timeout=120)
            return True
        except Exception as e:
            if attempt < 4:
                logger.warning(f"[shuttle-return] 도킹 재시도 ({attempt+1}/5): {e}")
                time.sleep(5)
            else:
                logger.error(f"[shuttle-return] 도킹 실패: {e}")
                return False
    return False


def run_shuttle_job(
    ip: str,
    pickup: dict,
    work: dict,
    wait_sec: int = 0,
    area_id: int | None = None,
    charger: Optional[dict] = None,
    max_cycles: int = 0,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """그룹 셔틀 — pickup(W_N) ↔ work(C_N) 왕복 반복.

    한 사이클: W → align → jack_up → C(to_unload) → jack_down → 대기 →
              C → align → jack_up → W(to_unload) → jack_down

    max_cycles=0 → 무한 반복 (정지 요청까지)
    max_cycles>0 → N회 완료 후 자동으로 충전소 복귀

    정지 요청(shuttle_stop)은 사이클 종료 직후에만 체크 → 충전소로 복귀.
    긴급 정지(stop_robot_job)는 즉시 중단 (기존 동작).
    """
    route_names = f"{pickup['name']} ↔ {work['name']}"

    def _notify(status: str, message: str, step: int = 0, total: int = 0):
        if on_status:
            on_status(status, message)
        logger.info(f"[shuttle-job] {status}: {message}")
        update_job_status(ip,
            status=status,
            message=message,
            route=route_names,
            current_step=step,
            total_steps=total,
            started_at=_job_status.get(ip, {}).get("started_at", time.time()),
        )

    def _fail(msg: str) -> dict:
        _notify("error", msg)
        try: cancel_current_move(ip)
        except Exception: pass
        try: jack_down(ip)
        except Exception: pass
        try:
            from app.crud.activity_log import log_activity as _log
            _log("robot", "task_error", f"셔틀 실패: {msg}", source="jack_service")
        except Exception:
            pass
        clear_job_status(ip)
        clear_shuttle_stop(ip)
        release_w_zone(ip)  # 보유 중이면 해제 (다른 로봇이 진행 가능하도록)
        return {"status": "error", "message": msg}

    # 시작 전 플래그 초기화
    clear_shuttle_stop(ip)
    _stop_flags.pop(ip, None)
    update_job_status(ip, status="started", route=route_names,
                      current_step=0, total_steps=8,
                      started_at=time.time(), message="셔틀 작업 시작")

    from app.crud.activity_log import log_activity
    log_activity("robot", "shuttle_start", f"셔틀 시작: {route_names}", source="jack_service")

    cycle = 0
    try:
        while True:
            # 사이클 시작 직전: 안전정지 요청 체크
            if is_shuttle_stop_requested(ip):
                _notify("returning", "정지 요청 — 충전소 복귀 진행")
                break
            if max_cycles > 0 and cycle >= max_cycles:
                _notify("returning", f"최대 {max_cycles}회 완료 — 충전소 복귀 진행")
                break

            cycle += 1
            cycle_label = f"#{cycle}" + (f"/{max_cycles}" if max_cycles > 0 else "")

            # === [W 영역 락 획득 — 출발 구간] ===
            _notify("waiting", f"{cycle_label} W 영역 진입 대기 중...", 1, 8)
            acquire_w_zone(ip, label=f"{pickup['name']} 출발", on_status=on_status)
            try:
                # 1) W_N align
                _notify("aligning", f"{cycle_label} {pickup['name']} 랙 정렬 중...", 1, 8)
                progress_w_zone(ip)
                r = align_with_retry(ip, pickup["x"], pickup["y"], pickup.get("ori", 0))
                if r["state"] != "succeeded":
                    return _fail(f"{pickup['name']} 정렬 실패: {r.get('fail_message', r['state'])}")

                # 2) jack_up
                _notify("jacking_up", f"{cycle_label} {pickup['name']} 잭 올리는 중...", 2, 8)
                progress_w_zone(ip)
                jack_up(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)

                # to_unload_point(C) 명령 발사 직전까지만 락 보유 — 출발 명령 직후 해제
                # (이동 자체는 락 없이 진행해도 다른 로봇과 멀어지므로 안전)
                progress_w_zone(ip)
            finally:
                # W 떠나는 시점에 즉시 해제 → 다음 로봇 진입 가능
                release_w_zone(ip)

            # 3) C_N 이동 (점유/장애물 재시도) — 락 없이
            _notify("moving_to_dropoff", f"{cycle_label} {work['name']} 이동 중...", 3, 8)
            r = unload_point_with_retry(
                ip, work["x"], work["y"], work.get("ori", 0),
                retries=3, wait_between=10.0, on_status=on_status,
                target_label=work["name"],
            )
            if r["state"] != "succeeded":
                return _fail(f"{work['name']} 이동 실패: {r.get('fail_message', '')}")

            # 4) jack_down
            _notify("jacking_down", f"{cycle_label} {work['name']} 잭 내리는 중...", 4, 8)
            jack_down(ip)
            _interruptible_sleep(ip, JACK_WAIT_SEC)

            # 5) 대기시간 (긴급정지에는 응답하지만 안전정지 요청은 사이클 끝까지 무시)
            if wait_sec > 0:
                _notify("waiting", f"{cycle_label} {work['name']} 대기 중 ({wait_sec}초)...", 5, 8)
                _interruptible_sleep(ip, wait_sec)

            # 6) C_N 재정렬
            _notify("aligning", f"{cycle_label} {work['name']} 랙 재정렬 중...", 6, 8)
            r = align_with_retry(ip, work["x"], work["y"], work.get("ori", 0))
            if r["state"] != "succeeded":
                return _fail(f"{work['name']} 재정렬 실패: {r.get('fail_message', '')}")

            # 7) jack_up
            _notify("jacking_up", f"{cycle_label} {work['name']} 잭 올리는 중...", 7, 8)
            jack_up(ip)
            _interruptible_sleep(ip, JACK_WAIT_SEC)

            # === [W 영역 락 획득 — 복귀 구간] ===
            _notify("waiting", f"{cycle_label} W 영역 복귀 대기 중...", 8, 8)
            acquire_w_zone(ip, label=f"{pickup['name']} 복귀", on_status=on_status)
            try:
                # 8) W_N 복귀 (점유/장애물 재시도)
                _notify("moving_to_dropoff", f"{cycle_label} {pickup['name']} 복귀 중...", 8, 8)
                progress_w_zone(ip)
                r = unload_point_with_retry(
                    ip, pickup["x"], pickup["y"], pickup.get("ori", 0),
                    retries=3, wait_between=10.0, on_status=on_status,
                    target_label=pickup["name"],
                )
                if r["state"] != "succeeded":
                    return _fail(f"{pickup['name']} 복귀 실패: {r.get('fail_message', '')}")

                # 9) W_N jack_down
                _notify("jacking_down", f"{cycle_label} {pickup['name']} 잭 내리는 중...", 8, 8)
                progress_w_zone(ip)
                jack_down(ip)
                _interruptible_sleep(ip, JACK_WAIT_SEC)
                progress_w_zone(ip)
            finally:
                # W 잭다운 완료 — 락 해제 (다음 로봇 진입 가능)
                release_w_zone(ip)
            log_activity("robot", "shuttle_cycle", f"셔틀 사이클 {cycle} 완료: {route_names}", source="jack_service")

        # === 루프 탈출: 충전소 복귀 ===
        # 우선순위:
        #   1) 인자로 받은 charger (UI에서 명시 지정)
        #   2) 로봇 현재 위치에서 가장 가까운 충전소 (자동 폴백)
        #   3) area 첫 충전소 (최후 폴백)
        target_charger = charger
        fallback_kind = ""
        if not target_charger:
            pos = _get_robot_position_db(ip)
            if pos:
                target_charger = _get_nearest_charging_poi(area_id, pos[0], pos[1])
                fallback_kind = " (가까운 충전소 자동 선택)" if target_charger else ""
        if not target_charger:
            target_charger = _get_charging_poi(area_id)
            if target_charger and not fallback_kind:
                fallback_kind = " (영역 첫 충전소)"

        if target_charger:
            ok = _dock_to_charger(ip, target_charger, on_status=on_status)
            msg = f"셔틀 종료 ({cycle}회 완료) — 충전소({target_charger['name']}){fallback_kind} 복귀{' 완료' if ok else ' 실패'}"
        else:
            msg = f"셔틀 종료 ({cycle}회 완료) — 충전소 POI 없음, 복귀 생략"

        _notify("done", msg)
        clear_job_status(ip)
        clear_shuttle_stop(ip)
        log_activity("robot", "shuttle_complete", msg, source="jack_service")
        return {"status": "done", "message": msg}

    except RuntimeError as e:
        # 긴급정지 (stop_robot_job)
        return _fail(f"긴급정지: {str(e)}")
    except Exception as e:
        return _fail(f"오류: {str(e)}")
