# Agents Configuration

## 에이전트 역할 구성

### 1. Architect (설계자)
- **역할:** 전체 파이프라인 설계, 트리거 로직, CLI 디스패처 구조 결정
- **도구:** /plan, /deep-interview
- **책임:**
  - STT → 롤링 버퍼 → 트리거 → CLI 디스패처 → UI 파이프라인 설계
  - 패널별 트리거 조건 명세화
  - CLI 호출 동시성 모델 결정 (풀/큐/세션 재사용)
  - 프롬프트 버전 관리 전략

### 2. Voice Engineer (음성/STT)
- **역할:** 마이크 캡처, STT, 트랜스크립트 버퍼
- **도구:** /autopilot, /ultraqa
- **책임:**
  - 로컬 Whisper(faster-whisper/whisper.cpp) 통합
  - 한국어 STT 정확도 튜닝
  - 스트리밍 vs 청크 배치 처리 결정
  - 화자 분리(speaker diarization) 필요 여부 판단 및 구현
  - 롤링 버퍼 자료구조 (직전 N발화 유지)

### 3. CLI Orchestrator (Claude CLI 오케스트레이터)
- **역할:** `claude` CLI 서브프로세스 관리, 프롬프트 구성, 결과 스트리밍
- **도구:** /autopilot, /ultraqa
- **책임:**
  - CLI headless 호출 래퍼 구현 (`claude -p` 기반)
  - 패널별 프롬프트 템플릿 (A/B/C/D-3 + D-1/D-2)
  - 트리거 조건 평가기 (룰 기반 + 경량 휴리스틱)
  - 호출 큐 + 동시성 제어
  - CLI 출력 파싱 및 1줄/50자 제약 강제
  - 사후 리포트 생성 (D-1 Action Item + D-2 Decision Log)

### 4. Frontend Developer (UI)
- **역할:** 팀 공용 디스플레이용 4패널 UI, WebSocket 연동
- **도구:** /autopilot, /ultraqa
- **책임:**
  - 4패널 레이아웃 (가독성 우선, 회의실 원거리 시인성)
  - 실시간 갱신 애니메이션 (과도한 깜빡임 방지)
  - 회의 시작/종료 컨트롤
  - 사후 리포트 뷰 (Action Item + Decision Log)
  - 패널 on/off 토글

### 5. Packager (배포/패키징)
- **역할:** 사용자가 `앱 실행` 한 번으로 백엔드까지 기동되도록 패키징
- **도구:** /autopilot
- **책임:**
  - Electron 또는 단일 바이너리 패키징 검토
  - 로컬 Whisper 모델 번들링 또는 최초 실행 시 다운로드
  - `claude` CLI 존재 여부 체크 + 사용자 안내
  - 마이크 권한 처리

---

## 워크플로우

### Phase 0: 타당성 검증 (Spike)
핵심 불확실성을 먼저 해소하는 단계. 구현 전 필수.

```
Architect → deep-interview로 요구사항 확정
  ├─ Voice Engineer → Whisper 한국어 실사용 테스트 (지연/정확도 측정)
  └─ CLI Orchestrator → claude CLI 호출 레이턴시 측정, 동시 호출 가능성 검증
```

**통과 기준:**
- STT 지연 ≤ 3초, 한국어 WER 허용 범위
- CLI 호출 왕복 레이턴시 ≤ 5초 (1회 기준)
- 4개 패널 트리거 조건이 동시 폭주하지 않음

### Phase 1: 실시간 4패널 MVP
```
CLI Orchestrator → A Summarizer + B Fact-Checker 프롬프트/트리거
Frontend → 2패널 UI + WebSocket
Voice Engineer → 롤링 버퍼 + STT 통합
```

### Phase 2: C/D-3 추가 + 사후 리포트
```
CLI Orchestrator → C Ideator + D-3 Devil's Advocate 트리거 로직
CLI Orchestrator → D-1 Action Item + D-2 Decision Log 사후 일괄 처리
Frontend → 4패널 완성 + 리포트 뷰
```

### Phase 3: 패키징 + 운영 개선
```
Packager → 앱 실행 원클릭화
Architect → 트리거 튜닝, 프롬프트 개선
Frontend → 회의실 시인성 개선, 패널 on/off
```

---

## 병렬 작업 가능 영역

| 작업 A | 작업 B | 의존성 |
|--------|--------|--------|
| Whisper STT 파이프라인 | 4패널 UI 레이아웃 | 없음 (mock 트랜스크립트) |
| CLI 호출 래퍼 | WebSocket 서버 | 없음 |
| 패널별 프롬프트 작성 | 트리거 조건 룰 정의 | 없음 |
| D-1/D-2 사후 리포트 | 실시간 패널 로직 | 없음 |

---

## 트리거 조건 (초안)

| 패널 | 트리거 | 구현 아이디어 |
|------|--------|--------------|
| A 요약 | 매 10발화 또는 주제 전환 | 발화 카운터 + 키워드 시프트 감지 |
| B 팩트체크 | 숫자/단위/고유명사 포함 발화 | 정규식 + NER 경량 모델 |
| C 아이디어 | 3분 이상 결론 없음, 같은 단어 반복 | 타이머 + n-gram 반복도 |
| D-3 반박 | "그럼 이렇게 하자", "OK" 등 합의 신호 | 키워드 매칭 + 최근 결정 컨텍스트 |

> 초안이며 Phase 0 검증 후 조정. 과호출 방지 위해 패널별 쿨다운(예: 30초) 필수.

---

## CLI 호출 규약

- 모든 호출은 `claude -p <프롬프트>` 또는 stdin pipe 형식.
- 프롬프트 선두에 출력 제약(1줄/50자/형식) 재명시하여 일탈 방지.
- 타임아웃 설정 (예: 15초) — 초과 시 스킵하고 다음 트리거 대기.
- 실패 시 사일런트 스킵 (UI에 에러 노출 지양, 로그만 기록).
- 롤링 버퍼는 직전 N발화만 포함, 전체 회의 기록은 전송하지 않음.

---

## 보안/프라이버시 원칙

- 기본값: 회의 음성/트랜스크립트는 로컬에만 저장. 종료 시 폐기.
- CLI 프롬프트로 전송되는 범위는 롤링 버퍼 한정 (전체 회의 X).
- 민감 정보(내부망 주소, 개인정보) 필터링 규칙 Phase 2에서 검토.
- 사용자 명시 허락 없이 외부 전송 없음.
