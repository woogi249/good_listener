# realtime-panel

한국어 음성 회의 실시간 4패널 + 사후 2패널 코파일럿.

## 현재 상태
- ✅ 요구사항 스펙 (`.omc/specs/deep-interview-spec-tightening.md`)
- ✅ 설계 결정문 9건 + 일관성 매트릭스 (`docs/decisions/`)
- ✅ **Realtime 4-panel MVP** — WebSocket UI, A/B/C/D 패널 트리거, 중요도 기반 글자 크기
- ✅ `claude` CLI 연결, 실패 시 `codex exec` fallback
- ✅ WAV 배치 STT + 선택적 마이크 청크 STT + AI 도메인 용어집
- ⏳ 다음: Action Item/Decision Log 사후 리포트 → 동의/로컬 전용 모드 → 패키징

## 빠른 시작

사전 요건: Python 3.10+, `claude` CLI (Pro/Max 구독 인증 완료)

### 실시간 4패널 앱
```bash
cd backend
python -m panel.realtime_app
```

브라우저에서 `http://127.0.0.1:8765` 접속.
기본값은 `--domain-profile ai`라서 Codex, Claude, Opus, OpenAI 같은 AI 고유명사 힌트와 후처리 보정이 적용됩니다.
회의 시작 전 오른쪽 준비 폼에 주제, 목표, 고유명사를 넣으면 세션 전용 STT 힌트와 패널 프롬프트 문맥으로 같이 반영됩니다.

CLI 호출 없이 UI/트리거만 빠르게 확인하려면:
```bash
python -m panel.realtime_app --mock-ai
```

용어집을 끄려면:
```bash
python -m panel.realtime_app --domain-profile none
```

### 터미널 Summarizer 경로
```bash
cd backend
python -m panel.runner ../samples/mock_transcript.txt
```

기대 출력: 발화 1~9까지는 버퍼 누적만, 10번째에 Summarizer 트리거 발동 → `claude` CLI 호출 → 1줄 50자 이내 한국어 요약.

## 프로젝트 구조
```
realtime-panel/
├── backend/           # Python 백엔드 (MVP)
│   ├── panel/
│   │   ├── realtime_app.py      # FastAPI/WebSocket 4패널 앱
│   │   ├── session.py           # 회의 세션 + 패널 실행
│   │   ├── cli_dispatcher.py    # claude CLI + codex fallback (stdin pipe)
│   │   ├── rolling_buffer.py    # 10발화 버퍼
│   │   ├── triggers.py          # 4패널 트리거 평가
│   │   ├── config.py            # 패널 설정
│   │   ├── prompts.py           # 프롬프트 로더
│   │   ├── static/              # 브라우저 UI
│   │   ├── vocabulary.py         # STT 도메인 용어 힌트/오인식 보정
│   │   └── runner.py            # 터미널/WAV 엔트리
│   ├── README.md
│   └── pyproject.toml
├── prompts/
│   └── summarizer.md            # Summarizer 프롬프트 템플릿
│   └── domain-ai.txt            # AI 회의 STT 용어 힌트
├── samples/
│   └── mock_transcript.txt      # 15발화 테스트 회의
├── docs/decisions/              # 9 ADR + CONSISTENCY
├── CLAUDE.md                    # 프로젝트 목적·제약
├── AGENTS.md                    # 에이전트 역할
└── README.md                    # 본 파일
```

## 배포 계획

- **Host**: 사용자 로컬 PC
- **CLI 호출**: host의 `claude` (Pro/Max 구독 세션 재사용)
- **공개 엔드포인트**: 선택 사항. 필요 시 Cloudflare Tunnel 등으로 별도 구성
- **Docker**: 필수 아님. MVP는 host Python 직접 실행.

상세 결정은 `docs/decisions/DR-00a~06.md` 및 `CONSISTENCY.md` 참조.

## 개발 원칙 (CLAUDE.md 발췌)
- Claude API 직접 호출 금지 → `claude` CLI subprocess만
- 유료 STT 금지 → 로컬 Whisper 우선
- 실시간 패널 출력: 1줄/50자 엄수
- 롤링 버퍼로 프롬프트 토큰 최소화 (직전 10~20 발화)
