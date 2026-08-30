# Good Listener Backend

FastAPI 서버는 회의 lifecycle, OpenAI Realtime client secret 발급, 확정 전사 저장, 비동기 진행·팩트 분석, 종료 후 회의록, 암호화 저장을 담당한다. 브라우저가 Realtime WebRTC 오디오 연결을 담당하므로 일반 `OPENAI_API_KEY`는 절대 클라이언트로 전달하지 않는다.

## 설치와 실행

잠금 파일을 사용한 Windows 개발 환경:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
$env:OPENAI_API_KEY = "현재 서버 프로세스에만 주입"
.\.venv\Scripts\python.exe -m panel.realtime_app
```

기본 주소는 `http://127.0.0.1:8765`다. 회의실 PC 단독 운영에서는 loopback 바인딩을 유지한다. 브라우저 마이크는 localhost 또는 HTTPS secure context에서만 사용해야 하므로 사내망 공유를 위해 임의로 `0.0.0.0`으로 바꾸지 않는다.

## 설정

| 변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | 없음 | 필수. 서버에서만 읽는 OpenAI 프로젝트 key |
| `OPENAI_REALTIME_MODEL` | `gpt-live-transcribe` | 실시간 전사 모델 |
| `OPENAI_ANALYSIS_MODEL` | `gpt-5.6-luna` | 회의 진행 상태 모델 |
| `OPENAI_FACT_MODEL` | `gpt-5.6-luna` | 공개 웹 팩트 모델 |
| `OPENAI_MINUTES_MODEL` | `gpt-5.6-terra` | 최종 회의록 모델 |
| `OPENAI_DIARIZE_MODEL` | `gpt-4o-transcribe-diarize` | 연결 공백 보완 전사 모델 |
| `GOOD_LISTENER_DB_PATH` | `%LOCALAPPDATA%\GoodListener\data\good-listener.db` | SQLite 경로 |
| `GOOD_LISTENER_AUDIO_DIR` | DB 옆 `audio` | 암호화 음성 경로 |
| `GOOD_LISTENER_KEY_PATH` | DB 옆 `master.key.dpapi` | DPAPI 보호 master key |
| `GOOD_LISTENER_ALLOWED_ORIGINS` | localhost/127.0.0.1 | 허용 브라우저 Origin |

Windows가 아닌 개발 환경에서는 key 파일명이 `master.key`가 되며 OS 파일 권한으로 보호한다. 운영 기준은 Windows DPAPI다.

## 실행 흐름

1. 브라우저가 bootstrap을 호출해 HttpOnly 제어 cookie를 받는다.
2. 사용자가 동의하고 회의를 만들면 서버가 meeting-scoped 상태를 생성한다.
3. 서버가 OpenAI 표준 key로 짧게 유효한 Realtime client secret을 만들고 브라우저에 전달한다.
4. 브라우저는 OpenAI와 WebRTC로 직접 오디오를 주고받고 partial transcript를 표시한다.
5. MediaRecorder 청크는 서버 ACK 전까지 브라우저 IndexedDB에 평문 Blob으로 대기하고, 서버가 암호화 저장한 뒤 브라우저에서 삭제된다. finalized transcript만 서버에 저장되며 발화 저장은 분석이나 공개 웹 검색 완료를 기다리지 않는다.
6. 종료 요청은 신규 입력을 닫고 진행 작업을 정리한 뒤 최종 회의록을 생성한다.
7. 회의별 명시 삭제는 SQLite 관계 데이터와 연결된 암호화 음성 파일을 함께 삭제한다.

상태와 요청은 `meeting_id`, event sequence, revision을 가져야 한다. 이전 세션의 늦은 비동기 결과는 현재 회의 상태를 덮어쓰지 않아야 한다.

주요 HTTP 경계:

| 경로 | 역할 |
|---|---|
| `GET /api/bootstrap` | HttpOnly 제어 cookie와 OpenAI 준비 상태 |
| `POST /api/meetings` | 외부 처리 동의가 포함된 회의 생성 |
| `POST /api/meetings/{id}/start|pause|resume|stop` | 회의 lifecycle |
| `POST /api/meetings/{id}/realtime/client-secret` | meeting-scoped Realtime client secret |
| `POST /api/meetings/{id}/audio/chunks` | 중복 안전한 암호화 복구 음성 저장 |
| `GET /api/meetings/{id}/transcript` | 확정 전사 조회 |
| `GET|PATCH /api/meetings/{id}/minutes` | 회의록 검토 |
| `POST /api/meetings/{id}/minutes/approve` | 사람이 확인한 회의록 확정 |
| `DELETE /api/meetings/{id}` | 관련 DB 데이터와 `.glenc` 음성 영구 삭제 |
| `WS /ws/meetings/{id}` | revision 기반 실시간 상태·복구 이벤트 |

## 저장과 보안

- 자동 TTL은 없다. 삭제 요청 전까지 음성, 전사, 분석, 회의록을 로컬에 영구 보존한다.
- DB의 내용 필드와 `.glenc` 음성은 암호화되고 master key는 Windows DPAPI로 보호한다. 식별자, lifecycle, 시각, 크기 같은 운영 메타데이터는 평문이다.
- `master.key.dpapi`를 분실하거나 다른 Windows 사용자로 옮기면 기존 데이터 복호화가 불가능할 수 있다.
- API key, client secret, 원문·프롬프트·회의록 본문을 로그하지 않는다.
- 별도 파일 로그는 만들지 않는다. 운영 host가 uvicorn stdout/stderr를 접근 제한된 위치에 rotation한다.
- 공개 웹 팩트 검색 결과는 출처 URL과 함께 저장하고, 출처가 없으면 `근거 부족`으로 남긴다.

자세한 백업·삭제·장애 대응은 `../docs/operations.md`를 따른다.

## 테스트

```powershell
python -m compileall -q panel
python -m pytest -q tests
python -m pip check
python -m pip_audit -r requirements.lock
```

저장소 루트에서는 다음 검증을 한 번에 실행할 수 있다.

```powershell
./scripts/verify.ps1
```

실제 OpenAI 호출은 기본 테스트에 포함되지 않는다. 승인된 운영자가 `OPENAI_API_KEY`를 현재 프로세스에 주입한 뒤에만 opt-in smoke test를 실행한다.

```powershell
./scripts/verify.ps1 -LiveApi
```

## 의존성과 패키징

- `pyproject.toml`은 직접 의존성을 정확한 버전으로 고정한다.
- `requirements.lock`은 런타임, 테스트, PyInstaller의 전이 의존성을 함께 고정한다.
- 직접 의존성을 변경한 뒤 저장소 루트에서 `./scripts/lock_dependencies.ps1`을 실행하고 변경된 lock을 검토한다.
- Python 없는 Windows PC용 빌드는 `./scripts/build_windows.ps1`로 생성한다.
- CI는 Python 3.10/3.12, Windows/Linux 테스트와 Windows PyInstaller 빌드를 수행하지만 실제 OpenAI API는 호출하지 않는다.
