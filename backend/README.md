# Backend MVP

실시간 4패널 UI와 터미널/WAV 파이프라인을 제공한다. AI 분석은 `claude` CLI를 기본으로 호출하고, 실패 시 `codex exec`로 fallback한다. 두 CLI는 host에 설치·인증되어 있어야 함. 실시간 마이크 STT는 기본적으로 AI 도메인 용어집(`--domain-profile ai`)을 사용한다.

## 사전 요건
- Python 3.10+
- `claude` CLI 설치 + Pro/Max 구독 인증 완료 (`claude --version`으로 확인)

## 실행

### 실시간 4패널 UI
```bash
cd backend
python -m panel.realtime_app
```

브라우저: `http://127.0.0.1:8765`
오른쪽 준비 폼에 주제, 목표, 고유명사를 입력하고 `준비`를 누르면 해당 문맥이 세션 전용 STT 힌트와 패널 프롬프트에 반영된다.

기본값은 AI 회의용 용어집이다. 끄려면:
```bash
python -m panel.realtime_app --domain-profile none
```

다른 힌트 파일을 쓰려면:
```bash
python -m panel.realtime_app --initial-prompt-file ../prompts/domain-ai.txt
```

빠른 UI 검증:
```bash
python -m panel.realtime_app --mock-ai
```

- `시작`: 회의 세션 시작
- `마이크`: 4초 청크 단위로 로컬 Whisper 전사 후 패널 트리거
- `샘플`: `samples/mock_transcript.txt`를 실시간처럼 재생
- 수동 입력: 오른쪽 입력창으로 발화를 직접 주입

### 파일 입력 (기본 테스트)
```bash
cd backend
python -m panel.runner ../samples/mock_transcript.txt
```

샘플 파일은 15발화 포함. 10발화째에 Summarizer 트리거 발동 → `claude` CLI 호출 → 1줄 요약 출력.

### 대화형 입력
```bash
cd backend
python -m panel.runner --interactive
```
한 줄씩 발화 입력. 10발화 누적 시점에 자동 요약.

### WAV 배치 (Whisper 전사 → Summarizer)
```bash
cd backend
python -m panel.runner --wav ../samples/stt_test.wav \
    --model small \
    --initial-prompt-file ../prompts/domain-crypto.txt
```
WAV를 Whisper로 전사한 뒤 각 세그먼트를 롤링버퍼에 누적. 10발화 도달 시 Summarizer 트리거. CLI 왕복 레이턴시는 `samples/cli_latency.csv`에 append (DR-02 p50/p95 원시 데이터).

- `--model`: tiny/base/small/medium/large-v3 (기본 small)
- `--initial-prompt-file`: 도메인 어휘 힌트 파일 (UTF-8). DR-07의 glossary 연결점.
- `--domain-profile ai`: AI 고유명사 hotwords + alias 보정 적용.

Windows에서는 `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` 환경변수 필요.

## 내부 구조
| 모듈 | 책임 |
|------|------|
| `panel/cli_dispatcher.py` | `claude -p -` **stdin pipe** 호출 + 실패 시 `codex exec` fallback + 1줄/50자 강제 |
| `panel/rolling_buffer.py` | deque 기반 10발화 버퍼 (DR-04 데이터 경계) |
| `panel/triggers.py` | 발화 카운터 + 쿨다운 평가 |
| `panel/realtime_app.py` | FastAPI/WebSocket 서버 + 샘플/마이크 입력 |
| `panel/session.py` | 회의 세션 상태, 패널 실행, 중요도 반영 |
| `panel/config.py` | 4패널 트리거 파라미터 |
| `panel/vocabulary.py` | AI 도메인 STT 용어 힌트 + alias 후처리 |
| `panel/prompts.py` | `prompts/*.md` 로더 + `{transcript}` 주입 |
| `panel/runner.py` | 터미널/WAV 엔트리 포인트 |

## MVP 실행 시 자연 획득되는 측정값
- **DR-02 CLI 왕복 p50 샘플**: `response.elapsed_s` — 여러 회 실행하면 히스토그램 구성 가능
- **DR-00b rate limit 조기 감지**: 실패 시 stderr 확인 (저빈도라 완전 검증엔 부족, 본격 60회 스크립트는 별도)
- **1줄/50자 준수율**: `enforce_one_line_50chars`로 강제하지만 원본 CLI 응답 길이 기록 가능

## 다음 단계 (로드맵)
- P2: D-1 Action Item / D-2 Decision Log 사후 일괄
- P3: 동의 화면 + 전체 로컬 전용 모드
- P4: `config/triggers.yaml` 외부화 + 트리거 튜닝
- P5: Docker/Electron + 선택적 공개 터널 구성
