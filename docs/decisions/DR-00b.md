# DR-00b: Claude Pro/Max 구독 Rate Limit 검증

**상태:** 작업 가설 (Phase 0 spike 전)
**작성일:** 2026-04-19
**선행 조건:** 없음 (DR-01~06 모두의 선행 필수)

---

## 1. 가정 (Assumption)

- 사용자는 Claude Pro 또는 Max 구독을 보유하며, `claude` CLI 인증이 완료된 상태다.
- Anthropic은 정확한 분당·시간당 요청 제한 수치를 공식 문서로 공개하지 않을 가능성이 높다. 따라서 제한치는 경험적 측정으로만 파악 가능하다.
- 1회 호출 평균 프롬프트 크기: 롤링 버퍼 10~20 발화 기준 약 500~2,000 토큰.
- 1회 호출 평균 출력: 1줄 50자 제약 준수 시 약 50~80 토큰.
- 실사용 예상 호출 빈도: 패널 4개 × 쿨다운 30초 가정 시 최대 약 40회/시간. 본 검증은 1.5배 마진을 더한 **60회/시간**을 기준으로 삼는다.
- 의존성: 없음. 이 DR이 통과해야 DR-01(동시 호출), DR-02(레이턴시), DR-05(트리거 빈도), AGENTS.md Phase 0/1이 의미를 가진다.

---

## 2. 작업 가설 (Working Hypothesis)

60분 동안 분당 1회(총 60회) CLI 호출을 자동 실행하면, 율 제한(rate limit) 에러 없이 전 구간에서 응답을 수신할 수 있다. 이 조건이 충족되면 "시간당 60회 호출은 Pro/Max 구독으로 지속 가능하다"고 판정하고 DR-01 이하 단계를 진행한다.

이 가설은 Phase 0 spike 실측 결과에 따라 확정 또는 기각된다.

---

## 3. 이유 (Rationale)

**분당 1회(60회/시간)로 설정한 이유**

AGENTS.md 트리거 초안 기준으로 각 패널의 최소 쿨다운은 30초다. 4개 패널이 모두 동시에 트리거되어 30초마다 재트리거되는 최악 시나리오를 계산하면 이론상 최대 8회/분(480회/시간)이지만, 실제 회의에서는 트리거 조건이 겹치는 경우가 드물고 패널별 독립 쿨다운이 작동한다. 따라서 실사용 예상치는 약 40회/시간이며, 테스트 빈도 60회/시간은 이보다 약 1.5배 높은 스트레스 조건이다.

**60분 전체 구간을 측정하는 이유**

Anthropic의 rate limit는 슬라이딩 윈도우(분 단위 또는 시간 단위) 방식을 사용할 가능성이 있다. 초반 10~20회는 통과해도 후반 40~60회 구간에서 제한이 발동할 수 있다. 전 구간 측정 없이는 "회의 후반부에 패널이 조용해지는" 현상을 사전에 감지할 수 없다.

**이 검증이 DR-01~06보다 선행해야 하는 이유**

"CLI subprocess만 사용"은 CLAUDE.md의 최상위 비용 제약이다. 이 제약이 구독 한계로 인해 실현 불가능하다면, DR-01(동시 호출 모델), DR-05(트리거 빈도 설계) 등 이하의 모든 기술 결정이 무효가 된다. 프로젝트 킬러 리스크를 먼저 제거하는 것이 합리적이다.

---

## 4. 검증 방법 (Verification)

- **측정 도구**: Python 스크립트 (`subprocess.run`으로 `claude -p` 호출 + `time.perf_counter`로 응답 시간 기록)
- **측정 횟수 (N)**: 60회 (1분 간격 자동 반복, 총 60분 연속 실행)
- **대표값**: 성공률 (성공 호출 수 / 전체 60회), 에러 메시지 원문, 실패 발생 구간 (몇 번째 호출부터인지)
- **환경 조건 (고정)**: `CONSISTENCY.md §3.7 공통 환경 조건` 참조 (유선 / 평일 낮 / 단일 계정 / 500~1,000토큰 / stdin pipe / 세트 간 30초). 변경 시 세 DR(00b/01/02) 동시 갱신 필수.
  - DR-00b 특이 조건: 측정 간격 **60초** (분당 1회), 총 60회.
- **수집 지표**:
  - 호출별 응답 시간 (ms)
  - HTTP 상태 코드 또는 CLI 종료 코드 및 에러 메시지 원문
  - 분당 성공률 추이 (10회 단위 구간별 집계)
  - 누적 총 소요 토큰 수 (CLI 출력에서 파싱 가능한 경우)
- **테스트 스크립트 스켈레톤 (의사코드)**:

```python
# 의사코드 — 실제 파일로 생성 금지, DR 문서 내 기술 전용

TOTAL_CALLS = 60
INTERVAL_SECONDS = 60
PROMPT = "<롤링 버퍼 10발화 모의 텍스트, ~500토큰>"

results = []
for i in range(TOTAL_CALLS):
    start = time.perf_counter()
    # SECURITY (CONSISTENCY.md S-1): stdin pipe로 프롬프트 전달
    # 명령줄 인자 방식(`-p PROMPT`)은 `ps aux` / `tasklist /v` 등으로 노출됨 → 금지
    result = subprocess.run(
        ["claude", "-p", "-"],
        input=PROMPT,
        capture_output=True, text=True, timeout=30
    )
    elapsed = time.perf_counter() - start
    results.append({
        "index": i + 1,
        "success": result.returncode == 0,
        "elapsed_ms": elapsed * 1000,
        "stderr": result.stderr[:200]  # 에러 메시지 앞 200자만 저장
    })
    wait_remaining = INTERVAL_SECONDS - elapsed
    if wait_remaining > 0:
        time.sleep(wait_remaining)

# 집계
success_count = sum(1 for r in results if r["success"])
print(f"성공률: {success_count}/{TOTAL_CALLS} ({success_count/TOTAL_CALLS*100:.1f}%)")
print(f"rate limit 에러 발생 구간: {[r['index'] for r in results if not r['success']]}")
```

- **통과 기준 (정량, CONSISTENCY.md C-3 일원화)**:

  | 판정 | 성공률 | rate limit 에러 | 후속 조치 |
  |------|--------|-----------------|-----------|
  | **통과** | ≥ 95% (57회 이상) | 0회 | DR-01 진행 |
  | **회색 영역** | 85~94% (51~56회) | 0회 | Fallback 플랜 A(빈도 축소) 후 재검증. DR-05 `rate_limit_headroom=1.5` 적용 |
  | **탈락** | < 85% OR | ≥ 1회 | Fallback 섹션 5 진입 (플랜 A/B/C 선택) |

  탈락·회색 영역 모두 Fallback으로 이어지며, 둘의 차이는 "전면 재검토(탈락)" vs "빈도 조정 후 재시도(회색)"이다. rate limit 에러가 1회라도 관찰되면 성공률과 무관하게 **즉시 탈락**으로 판정한다(정책적 엄격성: API 서버측 경고는 무시 불가 신호).

---

## 5. 탈락 시 대안 (Fallback)

**탈락 조건**: 성공률 < 95% 또는 rate limit 에러 1회 이상 관찰

- **플랜 A — 호출 빈도 극단 축소 (권장 우선 시도)**
  - 실시간 패널을 Summarizer(A) 1개로만 운용. Fact-Checker(B), Ideator(C), Devil's Advocate(D-3)는 사후 일괄 처리로 전환.
  - 목표 빈도: 시간당 10회 이하 (6분당 1회).
  - 트레이드오프: 실시간성 대폭 감소, 핵심 가치(회의 중 교정) 약화.
  - 재검증: 시간당 10회 조건으로 동일 스크립트 재실행 후 통과 기준 재평가.

- **플랜 B — API 직접 호출 전환 비용 산정**
  - CLAUDE.md 최상위 제약("API 직접 호출 금지") 완화를 사용자에게 명시적으로 요청.
  - 1시간 60회 호출 기준 예상 비용 계산: 입력 ~2,000토큰 × 60 + 출력 ~80토큰 × 60 = 약 12만 입력 토큰 + 4,800 출력 토큰/시간. Sonnet 기준으로 시간당 약 $0.05~0.10 예상 (비용 재계산 시 DR-02 플랜 D도 동기화 필요).
  - **API 키 관리 (플랜 B 진입 시 필수, CONSISTENCY.md S-3)**:
    - 환경변수 `ANTHROPIC_API_KEY`로만 관리. 코드·설정 파일·저장소 하드코딩 금지.
    - Windows: 사용자 수준 환경변수 또는 Windows Credential Manager 권장.
    - `.env` 파일 사용 시 `.gitignore`에 반드시 포함. 팀 공유 금지.
    - 앱 시작 시 키 존재 여부만 검증 (값 로그 금지), 미설정 시 명확한 에러 메시지로 실패.
  - 의사결정권자: 사용자. 비용 수용 여부 확인 후에만 전환 진행.

- **플랜 C — 프로젝트 중단 또는 범위 대폭 축소**
  - 플랜 A·B 모두 수용 불가 시. 실시간 분석을 포기하고 회의 종료 후 일괄 처리(D-1, D-2)만 구현하는 미니멀 버전으로 축소 검토.

- **영향받는 DR 및 재작성 범위**:
  - DR-01 (동시 호출 모델): 플랜 A 채택 시 단일 큐 모델로 단순화 가능.
  - DR-05 (트리거 빈도): 플랜 A 채택 시 쿨다운 기본값을 60초 이상으로 상향 재정의.
  - AGENTS.md Phase 1 범위: Summarizer 1개 패널 MVP로 축소.
  - CLAUDE.md 상위 제약: 플랜 B 채택 시 "API 직접 호출 금지" 조항 삭제 또는 예외 조항 추가 필요 (사용자 승인 필수).
