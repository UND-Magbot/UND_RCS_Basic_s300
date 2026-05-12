"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import { apiFetch } from "@/lib/api";

const API = process.env.NEXT_PUBLIC_API_URL || "";

type LiveRobot = { ID: number; IP: string; SN: string; ROBOTNAME: string; ONLINE: string; [key: string]: any };
type PoiOption = { id: number; name: string; type: string };

type JackJob = {
  job_id: string;
  status: string;
  message: string;
};

type Props = {
  liveRobots: LiveRobot[];
  areaId?: number;
};

const STATUS_LABELS: Record<string, string> = {
  pending: "대기 중",
  started: "작업 시작",
  aligning: "랙 정렬 중",
  jacking_up: "잭 올리는 중",
  jacking_down: "잭 내리는 중",
  moving_to_dropoff: "드롭오프 이동 중",
  moving: "이동 중",
  charging: "충전 도킹 중",
  waiting: "대기 중",
  waiting_confirm: "출발 대기",
  waiting_confirm_return: "복귀 대기",
  waiting_next_or_return: "다음 포인트 선택",
  returning: "충전소 복귀 중",
  done: "완료",
  error: "오류",
  failed: "실패",
  running: "진행 중",
};

export function JackTestPanel({ liveRobots, areaId }: Props) {
  const [mode, setMode] = useState<"single" | "shuttle">("single");
  const [robotIp, setRobotIp] = useState("");
  const [robotId, setRobotId] = useState<number>(0);
  const [pois, setPois] = useState<PoiOption[]>([]);
  const [pickupId, setPickupId] = useState<number>(0);
  const [dropoffId, setDropoffId] = useState<number>(0);
  const [currentJob, setCurrentJob] = useState<JackJob | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const onlineRobots = liveRobots.filter((r) => r.ONLINE === "Online");
  const jackPois = pois.filter((p) => p.type === "jack");

  useEffect(() => {
    const url = areaId ? `${API}/api/map/active-pois?area_id=${areaId}` : `${API}/api/map/active-pois`;
    fetch(url)
      .then((r) => r.json())
      .then((data) => setPois(Array.isArray(data) ? data.map((p: any) => ({ id: p.id, name: p.name, type: p.poi_type || p.type })) : []))
      .catch(() => {});
  }, [areaId]);

  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, []);

  const handleRobotChange = (ip: string) => {
    setRobotIp(ip);
    const robot = onlineRobots.find((r) => r.IP === ip);
    setRobotId(robot?.ID || 0);
  };

  const handleStart = async () => {
    if (!robotIp || !robotId || !pickupId || !dropoffId) return;
    setIsStarting(true);
    try {
      const res = await fetch(`${API}/api/tasks/manual-run-pois`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ robot_id: robotId, pickup_poi_id: pickupId, dropoff_poi_id: dropoffId, manual_confirm: true }),
      });
      if (res.status === 409) {
        setCurrentJob({ job_id: "", status: "error", message: "로봇이 이미 작업 중입니다" });
        setIsStarting(false);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const result = await res.json();
      const histId = result.history_id;
      setCurrentJob({ job_id: String(histId), status: "pending", message: "작업이 시작되었습니다." });

      if (pollingRef.current) clearInterval(pollingRef.current);
      pollingRef.current = setInterval(async () => {
        try {
          // 1) jack_service 실시간 상태 조회
          if (robotIp) {
            const jobRes = await fetch(`${API}/api/robots/job-status/${robotIp}`);
            if (jobRes.ok) {
              const job = await jobRes.json();
              if (job.status && job.status !== "idle") {
                setCurrentJob({ job_id: String(histId), status: job.status, message: job.message || STATUS_LABELS[job.status] || job.status });
                return;
              }
            }
          }
          // 2) 작업 종료 후 이력에서 최종 상태 확인
          const hist = await apiFetch<{ total: number; items: any[] }>(`/api/tasks/history/all?limit=5`);
          const h = hist.items.find((item: any) => item.id === histId);
          if (h) {
            const status = h.status === "succeeded" ? "done" : h.status;
            setCurrentJob({ job_id: String(histId), status, message: h.error_message || STATUS_LABELS[status] || status });
            if (h.status === "succeeded" || h.status === "failed") {
              if (pollingRef.current) {
                clearInterval(pollingRef.current);
                pollingRef.current = null;
              }
            }
          }
        } catch {}
      }, 2000);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "시작 실패";
      setCurrentJob({ job_id: "", status: "error", message: msg });
    } finally {
      setIsStarting(false);
    }
  };

  const isRunning =
    currentJob != null &&
    currentJob.status !== "done" &&
    currentJob.status !== "error" &&
    currentJob.status !== "failed" &&
    currentJob.status !== "";

  const statusColor =
    currentJob?.status === "done"
      ? "var(--color-success)"
      : currentJob?.status === "error"
        ? "var(--color-error)"
        : "var(--color-warning)";

  const pickupName = jackPois.find((p) => p.id === pickupId)?.name || "";
  const dropoffName = jackPois.find((p) => p.id === dropoffId)?.name || "";

  return (
    <div className="jack-test-panel">
      <h3 className="jack-test-panel__title">수동 배차</h3>

      <div className="jack-test-panel__tabs" role="tablist">
        <button
          type="button"
          className={`jack-test-panel__tab${mode === "single" ? " jack-test-panel__tab--active" : ""}`}
          onClick={() => setMode("single")}
          disabled={isRunning}
        >
          단일
        </button>
        <button
          type="button"
          className={`jack-test-panel__tab${mode === "shuttle" ? " jack-test-panel__tab--active" : ""}`}
          onClick={() => setMode("shuttle")}
          disabled={isRunning}
        >
          그룹 셔틀
        </button>
      </div>

      {mode === "shuttle" ? (
        <GroupShuttleSection onlineRobots={onlineRobots} pois={pois} />
      ) : (
      <div className="jack-test-panel__form">
        <label className="jack-test-panel__label">
          로봇
          <select
            className="jack-test-panel__select"
            value={robotIp}
            onChange={(e) => handleRobotChange(e.target.value)}
            disabled={isRunning}
          >
            <option value="">선택</option>
            {onlineRobots.map((r) => (
              <option key={r.IP} value={r.IP}>
                {r.ROBOTNAME || r.SN} ({r.IP})
              </option>
            ))}
          </select>
        </label>


        <label className="jack-test-panel__label">
          픽업 위치
          <select
            className="jack-test-panel__select"
            value={pickupId}
            onChange={(e) => setPickupId(Number(e.target.value))}
            disabled={isRunning}
          >
            <option value={0}>선택</option>
            {jackPois.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </label>

        <label className="jack-test-panel__label">
          드롭오프 위치
          <select
            className="jack-test-panel__select"
            value={dropoffId}
            onChange={(e) => setDropoffId(Number(e.target.value))}
            disabled={isRunning}
          >
            <option value={0}>선택</option>
            {jackPois.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </label>

        {pickupId > 0 && dropoffId > 0 && (
          <div className="jack-test-panel__route-info">
            {pickupName} <small>(픽업)</small> → {dropoffName} <small>(드롭오프)</small>
          </div>
        )}

        <div className="jack-test-panel__buttons">
          {!isRunning ? (
            <button
              className="btn btn--primary jack-test-panel__btn"
              onClick={handleStart}
              disabled={!robotIp || !pickupId || !dropoffId || isStarting}
            >
              {isStarting ? "시작 중..." : "실행"}
            </button>
          ) : (
            <button
              className="btn btn--danger jack-test-panel__btn"
              onClick={async () => {
                try {
                  if (robotIp) {
                    await fetch(`${API}/api/robots/remote/stop-all/${robotIp}`, { method: "POST" });
                  }
                } catch {}
                if (pollingRef.current) {
                  clearInterval(pollingRef.current);
                  pollingRef.current = null;
                }
                setCurrentJob(null);
                setIsStarting(false);
              }}
            >
              중지
            </button>
          )}
        </div>
      </div>
      )}

      {mode === "single" && currentJob && (
        <div className="jack-test-panel__status" style={{ borderColor: statusColor }}>
          <div className="jack-test-panel__status-label" style={{ color: statusColor }}>
            {STATUS_LABELS[currentJob.status] || currentJob.status}
          </div>
          <div className="jack-test-panel__status-msg">{currentJob.message}</div>
          {currentJob.status === "waiting_confirm" && robotIp && (
            <button
              className="btn btn--primary jack-test-panel__btn"
              style={{ marginTop: 8 }}
              onClick={async () => {
                await fetch(`${API}/api/robots/remote/confirm/${robotIp}`, { method: "POST" });
              }}
            >출발</button>
          )}
          {currentJob.status === "waiting_next_or_return" && robotIp && (
            <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
              <div style={{ fontSize: 13, color: "var(--text-muted)", textAlign: "center" }}>다음 작업 포인트를 선택하세요</div>
              <div style={{ display: "flex", gap: 8 }}>
                <select
                  className="jack-test-panel__select"
                  style={{ flex: 1 }}
                  id="nextPoiSelect"
                >
                  {jackPois.map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
                <button
                  className="btn btn--primary"
                  style={{ width: "auto", padding: "10px 20px", fontSize: 14 }}
                  onClick={async () => {
                    const sel = document.getElementById("nextPoiSelect") as HTMLSelectElement;
                    if (sel?.value) {
                      await fetch(`${API}/api/robots/remote/next-point/${robotIp}`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ poi_id: Number(sel.value) }),
                      });
                    }
                  }}
                >다음 이동</button>
              </div>
              <button
                className="btn jack-test-panel__btn"
                style={{ background: "linear-gradient(135deg, #36dfc8, #2bb5a0)", color: "white" }}
                onClick={async () => {
                  await fetch(`${API}/api/robots/remote/return/${robotIp}`, { method: "POST" });
                }}
              >복귀</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


// ══════════════════════════════════════
// 그룹 셔틀 섹션 (다수 로봇 W↔C 무한 왕복)
// ══════════════════════════════════════

type ShuttleEntry = {
  uid: number;
  robotId: number;
  robotIp: string;
  pickupId: number;
  workId: number;
  chargerId: number;
  waitSec: number;
  maxCycles: number;  // 0 = 무한
};

type ShuttleStatus = {
  status: string;
  message: string;
  route?: string;
};

const MAX_SHUTTLE_ROBOTS = 3;
let _uidSeq = 0;
const newUid = () => ++_uidSeq;

function GroupShuttleSection({
  onlineRobots,
  pois,
}: {
  onlineRobots: LiveRobot[];
  pois: PoiOption[];
}) {
  // 픽업(W)은 standby 또는 jack, 작업(C)은 jack 모두 허용
  // (W1~W3는 보통 standby로 등록되지만 Shelves Point overlay가 있어 잭 픽업 가능)
  const pickupPois = pois.filter((p) => p.type === "standby" || p.type === "jack");
  const workPois = pois.filter((p) => p.type === "jack" || p.type === "standby");
  const chargerPois = pois.filter((p) => p.type === "charging");
  const [entries, setEntries] = useState<ShuttleEntry[]>([
    { uid: newUid(), robotId: 0, robotIp: "", pickupId: 0, workId: 0, chargerId: 0, waitSec: 0, maxCycles: 0 },
  ]);
  const [startDelaySec, setStartDelaySec] = useState<number>(5);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [statuses, setStatuses] = useState<Record<string, ShuttleStatus>>({});
  const [errorMsg, setErrorMsg] = useState<string>("");
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => () => {
    if (pollingRef.current) clearInterval(pollingRef.current);
  }, []);

  const updateEntry = (uid: number, patch: Partial<ShuttleEntry>) => {
    setEntries((prev) => prev.map((e) => (e.uid === uid ? { ...e, ...patch } : e)));
  };

  const addRow = () => {
    if (entries.length >= MAX_SHUTTLE_ROBOTS) return;
    setEntries((prev) => [
      ...prev,
      { uid: newUid(), robotId: 0, robotIp: "", pickupId: 0, workId: 0, chargerId: 0, waitSec: 0, maxCycles: 0 },
    ]);
  };

  const removeRow = (uid: number) => {
    setEntries((prev) => (prev.length <= 1 ? prev : prev.filter((e) => e.uid !== uid)));
  };

  const handleRobotChange = (uid: number, ip: string) => {
    const r = onlineRobots.find((x) => x.IP === ip);
    updateEntry(uid, { robotIp: ip, robotId: r?.ID || 0 });
  };

  const validate = (): string | null => {
    const valid = entries.filter((e) => e.robotId && e.pickupId && e.workId);
    if (valid.length === 0) return "최소 1개 행 이상 입력하세요";
    const robotIds = valid.map((e) => e.robotId);
    if (new Set(robotIds).size !== robotIds.length) return "로봇이 중복되었습니다";
    for (const e of valid) {
      if (e.pickupId === e.workId) return "픽업과 작업 POI가 동일합니다";
    }
    return null;
  };

  const startPolling = (ips: string[]) => {
    if (pollingRef.current) clearInterval(pollingRef.current);
    const fetchOnce = async () => {
      const next: Record<string, ShuttleStatus> = {};
      let anyActive = false;
      await Promise.all(
        ips.map(async (ip) => {
          try {
            const r = await fetch(`${API}/api/robots/job-status/${ip}`);
            if (r.ok) {
              const j = await r.json();
              if (j.status && j.status !== "idle") {
                next[ip] = {
                  status: j.status,
                  message: j.message || STATUS_LABELS[j.status] || j.status,
                  route: j.route,
                };
                if (!["done", "error", "failed"].includes(j.status)) {
                  anyActive = true;
                }
              } else {
                next[ip] = { status: "idle", message: "대기 중" };
              }
            }
          } catch {}
        }),
      );
      setStatuses(next);
      if (!anyActive) {
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }
        setRunning(false);
      }
    };
    fetchOnce();
    pollingRef.current = setInterval(fetchOnce, 2000);
  };

  const handleStart = async () => {
    const err = validate();
    if (err) {
      setErrorMsg(err);
      return;
    }
    setErrorMsg("");
    setBusy(true);
    const valid = entries.filter((e) => e.robotId && e.pickupId && e.workId);
    try {
      const res = await fetch(`${API}/api/tasks/group-shuttle/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entries: valid.map((e) => ({
            robot_id: e.robotId,
            pickup_poi_id: e.pickupId,
            work_poi_id: e.workId,
            wait_sec: Number(e.waitSec) || 0,
            charger_poi_id: e.chargerId || null,
            max_cycles: Math.max(0, Number(e.maxCycles) || 0),
          })),
          start_delay_sec: Math.max(0, Number(startDelaySec) || 0),
        }),
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(txt || `HTTP ${res.status}`);
      }
      setRunning(true);
      startPolling(valid.map((e) => e.robotIp));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "시작 실패";
      setErrorMsg(msg);
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    const valid = entries.filter((e) => e.robotId);
    setBusy(true);
    try {
      await fetch(`${API}/api/tasks/group-shuttle/stop`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ robot_ids: valid.map((e) => e.robotId) }),
      });
    } catch {}
    setBusy(false);
  };

  const handleEmergencyStop = async () => {
    if (!confirm("긴급 정지하시겠습니까? (충전소 복귀 없이 즉시 멈춤)")) return;
    const valid = entries.filter((e) => e.robotIp);
    setBusy(true);
    await Promise.all(
      valid.map((e) =>
        fetch(`${API}/api/robots/remote/stop-all/${e.robotIp}`, { method: "POST" }).catch(() => {}),
      ),
    );
    setBusy(false);
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
    setRunning(false);
    setStatuses({});
  };

  const usedRobotIds = new Set(entries.map((e) => e.robotId).filter(Boolean));
  const usedPickupIds = new Set(entries.map((e) => e.pickupId).filter(Boolean));
  const usedWorkIds = new Set(entries.map((e) => e.workId).filter(Boolean));
  const usedChargerIds = new Set(entries.map((e) => e.chargerId).filter(Boolean));

  return (
    <div className="jack-test-panel__form">
      {entries.map((entry, idx) => {
        const st = entry.robotIp ? statuses[entry.robotIp] : undefined;
        const stCls =
          st?.status === "done"
            ? "shuttle-row__status shuttle-row__status--done"
            : st?.status === "error" || st?.status === "failed"
              ? "shuttle-row__status shuttle-row__status--error"
              : "shuttle-row__status";
        return (
          <div key={entry.uid} className="shuttle-row">
            <div className="shuttle-row__header">
              <span>로봇 #{idx + 1}</span>
              {entries.length > 1 && !running && (
                <button
                  type="button"
                  className="shuttle-row__remove"
                  onClick={() => removeRow(entry.uid)}
                  aria-label="행 삭제"
                >
                  ×
                </button>
              )}
            </div>

            <label className="shuttle-row__field">
              로봇
              <select
                className="shuttle-row__select"
                value={entry.robotIp}
                onChange={(e) => handleRobotChange(entry.uid, e.target.value)}
                disabled={running}
              >
                <option value="">선택</option>
                {onlineRobots
                  .filter((r) => r.IP === entry.robotIp || !usedRobotIds.has(r.ID))
                  .map((r) => (
                    <option key={r.IP} value={r.IP}>
                      {r.ROBOTNAME || r.SN}
                    </option>
                  ))}
              </select>
            </label>

            <label className="shuttle-row__field">
              랙 위치 (W)
              <select
                className="shuttle-row__select"
                value={entry.pickupId}
                onChange={(e) => updateEntry(entry.uid, { pickupId: Number(e.target.value) })}
                disabled={running}
              >
                <option value={0}>선택</option>
                {pickupPois
                  .filter((p) => p.id === entry.pickupId || !usedPickupIds.has(p.id))
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>

            <label className="shuttle-row__field">
              작업 포지션 (C)
              <select
                className="shuttle-row__select"
                value={entry.workId}
                onChange={(e) => updateEntry(entry.uid, { workId: Number(e.target.value) })}
                disabled={running}
              >
                <option value={0}>선택</option>
                {workPois
                  .filter((p) => p.id === entry.workId || !usedWorkIds.has(p.id))
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>

            <label className="shuttle-row__field">
              충전소 (복귀)
              <select
                className="shuttle-row__select"
                value={entry.chargerId}
                onChange={(e) => updateEntry(entry.uid, { chargerId: Number(e.target.value) })}
                disabled={running}
              >
                <option value={0}>자동 (영역 첫 충전소)</option>
                {chargerPois
                  .filter((p) => p.id === entry.chargerId || !usedChargerIds.has(p.id))
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>

            <div style={{ display: "flex", gap: 6 }}>
              <label className="shuttle-row__field" style={{ flex: 1 }}>
                대기시간 (초)
                <input
                  type="number"
                  className="shuttle-row__input"
                  min={0}
                  step={1}
                  value={entry.waitSec}
                  onChange={(e) => updateEntry(entry.uid, { waitSec: Number(e.target.value) || 0 })}
                  disabled={running}
                />
              </label>
              <label className="shuttle-row__field" style={{ flex: 1 }}>
                반복 (0=무한)
                <input
                  type="number"
                  className="shuttle-row__input"
                  min={0}
                  step={1}
                  value={entry.maxCycles}
                  onChange={(e) => updateEntry(entry.uid, { maxCycles: Math.max(0, Number(e.target.value) || 0) })}
                  disabled={running}
                  placeholder="무한"
                />
              </label>
            </div>

            {st && st.status !== "idle" && (
              <div className={stCls}>
                <strong>{STATUS_LABELS[st.status] || st.status}</strong>
                <div style={{ marginTop: 2 }}>{st.message}</div>
              </div>
            )}
          </div>
        );
      })}

      {!running && (
        <label className="shuttle-row__field" style={{ paddingTop: 4 }}>
          시작 간격 (초) — 동시 W 진입 충돌 방지
          <input
            type="number"
            className="shuttle-row__input"
            min={0}
            step={1}
            value={startDelaySec}
            onChange={(e) => setStartDelaySec(Math.max(0, Number(e.target.value) || 0))}
          />
        </label>
      )}

      {!running && entries.length < MAX_SHUTTLE_ROBOTS && (
        <button type="button" className="shuttle-add-btn" onClick={addRow}>
          + 로봇 추가 ({entries.length}/{MAX_SHUTTLE_ROBOTS})
        </button>
      )}

      {errorMsg && (
        <div className="jack-test-panel__status" style={{ borderColor: "var(--color-error)" }}>
          <div className="jack-test-panel__status-label" style={{ color: "var(--color-error)" }}>
            오류
          </div>
          <div className="jack-test-panel__status-msg">{errorMsg}</div>
        </div>
      )}

      <div className="jack-test-panel__buttons">
        {!running ? (
          <button
            type="button"
            className="btn btn--primary jack-test-panel__btn"
            onClick={handleStart}
            disabled={busy}
          >
            {busy ? "시작 중..." : "동시 시작"}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn jack-test-panel__btn"
              style={{ background: "var(--color-warning)", color: "white" }}
              onClick={handleStop}
              disabled={busy}
            >
              정지(현 사이클 후 복귀)
            </button>
            <button
              type="button"
              className="btn btn--danger jack-test-panel__btn"
              onClick={handleEmergencyStop}
              disabled={busy}
            >
              긴급 정지
            </button>
          </>
        )}
      </div>
    </div>
  );
}
