# Good Listener

Good Listener는 청각 장애인과 난청 사용자가 회의 흐름을 놓치지 않도록 돕는 한국어 실시간 회의 접근성 코파일럿이다. OpenAI Realtime API로 음성을 연속 전사하고, 회의 중에는 현재 진행과 출처 기반 팩트를 차분한 화면에 표시하며, 종료 후에는 근거 발화가 연결된 회의록을 만든다.

## 제품 범위

- OpenAI API만 사용한다. EXAONE, Claude/Codex CLI, 직접 웹 스크래퍼는 제품 경로에 포함하지 않는다.
- 브라우저는 마이크와 Realtime WebRTC를 담당하고, 일반 `OPENAI_API_KEY`는 로컬 서버에만 둔다.
- 공개 웹 팩트 확인과 신규 발화 수집은 별도 비동기 작업으로 실행돼 서로를 막지 않는다.
- partial transcript는 화면에만 표시하고, 확정 발화만 진행 분석과 회의록에 사용한다.
- 회의 종료 후 결정, 할 일, 미결 사항, 팩트와 근거 발화를 구조화된 회의록으로 저장한다.
- v1 운영 단위는 Windows 회의실 PC 한 대와 활성 회의 하나다. 서버는 기본적으로 `127.0.0.1`에만 열린다.

## 데이터 흐름

```text
브라우저 마이크 ──WebRTC──> OpenAI Realtime 전사
       │                         │ partial/final transcript
       │                         ▼
       └─MediaRecorder─> IndexedDB 대기열 ──> 로컬 FastAPI 서버 ──> SQLite/.glenc
                         (ACK 전 평문 Blob)       │                 (내용 암호화)
                                                  ├─ 진행 상태 분석
                                                  ├─ 공개 웹 팩트 확인
                                                  └─ 종료 후 회의록
```

브라우저에는 서버가 발급한 짧게 유효한 Realtime client secret만 전달된다. 실시간 분석, 팩트 검색, 회의록 생성은 서버가 OpenAI Responses API로 실행한다.

## 로컬 개발

요구 사항은 Python 3.10 이상, 최신 Edge/Chrome, 입력 마이크, OpenAI 프로젝트 API key다.

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
$env:OPENAI_API_KEY = "현재 PowerShell 프로세스에만 주입"
.\.venv\Scripts\python.exe -m panel.realtime_app
```

브라우저에서 `http://127.0.0.1:8765`를 연다. 키를 코드, `.env`가 포함된 배포물, 브라우저 저장소 또는 명령행 인자에 넣지 않는다.

기본 모델은 다음과 같으며 환경변수로 명시적으로 교체할 수 있다.

| 기능 | 환경변수 | 기본 모델 |
|---|---|---|
| Realtime 전사 | `OPENAI_REALTIME_MODEL` | `gpt-live-transcribe` |
| 진행 분석 | `OPENAI_ANALYSIS_MODEL` | `gpt-5.6-luna` |
| 공개 웹 팩트 | `OPENAI_FACT_MODEL` | `gpt-5.6-luna` |
| 회의록 | `OPENAI_MINUTES_MODEL` | `gpt-5.6-terra` |
| 연결 공백 보완 전사 | `OPENAI_DIARIZE_MODEL` | `gpt-4o-transcribe-diarize` |

## 저장과 개인정보

음성, 전사, 분석 상태, 팩트 출처, 회의록은 자동 만료하지 않고 명시적으로 삭제할 때까지 `%LOCALAPPDATA%\GoodListener\data`에 로컬 영구 보존한다. SQLite의 내용 필드와 음성은 master key로 암호화하며 Windows에서는 key를 DPAPI로 보호한다. 식별자, 상태, 시각, 파일 크기 같은 운영 메타데이터는 DB에 평문으로 남는다.

연결 복구용 MediaRecorder 청크는 서버가 암호화 저장을 확인하기 전까지 브라우저 IndexedDB에 **평문 Blob으로 임시 보존**되고, 정상 ACK 뒤 삭제된다. 전용 Windows 계정과 잠금된 브라우저 프로필을 사용하고 회의실 공용 계정을 쓰지 않는다.

마이크를 켜기 전에 모든 참가자에게 다음 내용을 고지하고 명시적 동의를 받아야 한다.

- 음성과 텍스트가 OpenAI API로 전송됨
- 검증 가능한 주장이 공개 웹 검색에 사용될 수 있음
- 회의 데이터가 로컬 PC에 영구 보존됨
- 회의별 삭제 기능으로 음성·전사·회의록을 함께 삭제할 수 있음

백업, key 손실 주의, 관리자 삭제 절차는 [운영 가이드](docs/operations.md), 처리 원칙은 [개인정보·회의 데이터 정책](docs/privacy-and-data.md)을 따른다.

## 검증

```powershell
./scripts/verify.ps1
```

스크립트는 `backend\.venv`를 우선 사용하고 모든 외부 명령의 종료 코드를 검사한다. 다른 격리 환경을 쓰면 `./scripts/verify.ps1 -PythonPath C:\path\to\python.exe`로 지정한다. 전역 Python 환경의 다른 패키지 충돌이 검증 결과에 섞이지 않도록 프로젝트 전용 환경에서 실행한다.

실제 API smoke test는 비용과 외부 전송을 명시적으로 허용한 경우에만 실행한다. 이 검사는 고정된 합성 문장만 전송하고 회의 데이터는 읽지 않는다.

```powershell
./scripts/verify.ps1 -LiveApi
```

## Windows 배포

Python이 없는 PC에는 PyInstaller one-folder 빌드 또는 Inno Setup installer를 제공한다.

```powershell
./scripts/build_windows.ps1
# Inno Setup 6이 설치된 빌드 PC:
./scripts/build_windows.ps1 -BuildInstaller
```

결과는 `dist\good-listener`와 `dist\installer`에 만들어진다. 운영 배포물은 사내 코드서명 인증서로 서명하고, installer·Git commit·`backend/requirements.lock`을 같은 릴리스 단위로 보관한다. uninstall은 로컬 회의 데이터를 자동 삭제하지 않는다.

## 주요 문서

- [백엔드 실행·개발](backend/README.md)
- [운영, 백업, 장애 대응, 롤백](docs/operations.md)
- [개인정보와 영구 보존 정책](docs/privacy-and-data.md)
- [사내 출시 점검표](docs/release-checklist.md)
- [문서 구분과 과거 설계 기록](docs/README.md)

## 현재 제약

- v1은 회의실 PC 한 대에서 하나의 활성 회의만 지원한다.
- 사내 공유 서버, 다중 회의, 공개 터널, 외부 계정 로그인은 지원 범위가 아니다.
- AI 결과는 근거가 연결된 초안이다. 결정·담당자·기한이 불명확하면 `미정`으로 유지하고 사람이 최종 확인한다.
