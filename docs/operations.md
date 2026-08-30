# Good Listener 운영 가이드

이 문서는 **Windows 회의실 PC 한 대에서 활성 회의 하나를 운영하는 0.1.x 릴리스**를 기준으로 한다. 서버는 기본적으로 `127.0.0.1`에만 바인딩한다. 포트 전달, 공개 터널, `0.0.0.0` 바인딩은 지원 범위가 아니다.

## 운영 구조

- 브라우저는 마이크 권한, OpenAI Realtime WebRTC 연결, 실시간 자막 표시를 담당한다.
- 브라우저 MediaRecorder는 복구용 청크를 서버 ACK 전까지 IndexedDB에 평문 Blob으로 임시 보존한다. 서버가 AES-GCM 암호화 저장을 확인하면 해당 청크를 브라우저에서 삭제한다.
- 로컬 FastAPI 서버는 일반 `OPENAI_API_KEY`를 보관하고, 브라우저에 짧게 유효한 Realtime client secret만 발급한다.
- 확정 발화가 저장되면 진행 분석과 공개 웹 팩트 확인이 별도 비동기 작업으로 실행된다. 검색 중에도 새 발화 수집은 계속된다.
- 종료 시 결정, 할 일, 미결 사항과 근거 발화를 묶어 최종 회의록을 생성한다.
- OpenAI 외 모델 provider와 직접 웹 스크래퍼는 사용하지 않는다.

## 환경 변수

| 변수 | 기본값 | 용도 |
|---|---|---|
| `OPENAI_API_KEY` | 없음, 필수 | 서버 프로세스 전용 OpenAI 프로젝트 키 |
| `OPENAI_REALTIME_MODEL` | `gpt-live-transcribe` | Realtime 전사 |
| `OPENAI_ANALYSIS_MODEL` | `gpt-5.6-luna` | 실시간 진행 상태 분석 |
| `OPENAI_FACT_MODEL` | `gpt-5.6-luna` | 공개 웹 팩트 확인 |
| `OPENAI_MINUTES_MODEL` | `gpt-5.6-terra` | 종료 후 회의록 |
| `OPENAI_DIARIZE_MODEL` | `gpt-4o-transcribe-diarize` | 연결 공백 보완 전사 |
| `GOOD_LISTENER_DB_PATH` | `%LOCALAPPDATA%\GoodListener\data\good-listener.db` | 내용 필드를 암호화하는 회의 데이터베이스 |
| `GOOD_LISTENER_AUDIO_DIR` | `%LOCALAPPDATA%\GoodListener\data\audio` | 암호화된 복구용 음성 청크 |
| `GOOD_LISTENER_KEY_PATH` | `%LOCALAPPDATA%\GoodListener\data\master.key.dpapi` | Windows DPAPI로 보호된 master key |

`OPENAI_API_KEY`를 소스, `.env`가 포함된 배포물, 브라우저 저장소, URL, 명령행 인자에 넣지 않는다. 최종 배포에서는 IT 관리형 프로세스 환경이나 승인된 secret launcher로 주입한다. `scripts/run_windows.ps1`은 키가 없을 때 보안 입력을 받아 현재 자식 프로세스에만 전달하며 저장하지 않는다.

## 시작 전 점검

1. Windows 10/11 x64, Edge 또는 Chrome, 입력 마이크를 확인한다.
2. 방화벽에서 `api.openai.com:443` 아웃바운드 통신을 허용하고 시스템 시간이 맞는지 확인한다.
3. OpenAI 프로젝트에 비용 한도와 알림을 설정하고 운영 전용 key를 준비한다.
4. `%LOCALAPPDATA%\GoodListener\data`에 현재 Windows 사용자의 읽기·쓰기 권한과 충분한 여유 공간이 있는지 확인한다.
5. 전용 Windows 계정과 접근 제한된 Edge/Chrome 프로필을 사용한다. 회의실 공용 계정, 동기화된 공용 브라우저 프로필, 게스트에게 열린 프로필은 사용하지 않는다.
6. 앱을 실행한 뒤 `http://127.0.0.1:8765/health`와 화면의 연결·마이크·저장소 상태를 확인한다.
7. 참가자에게 녹음, OpenAI 전사·분석, 공개 웹 검색, 로컬 영구 보존 범위를 고지하고 명시적 동의를 받은 뒤 회의를 시작한다.

## 회의 중·종료 점검

- 녹음 상태가 항상 화면에 표시되는지 확인한다.
- `재연결 중` 상태가 길어져도 앱을 즉시 종료하지 않는다. 로컬 음성 청크가 연결 공백을 보존하고 재연결 후 보완 전사를 수행한다.
- 팩트 결과는 출처가 있어야 `확인됨/반박됨`이 되며, 그 전에는 `검증 중/근거 부족`으로 취급한다.
- 종료 후 상태가 `회의록 완료`가 될 때까지 창을 닫지 않는다.
- 음성 업로드 대기열이 0이 되기 전에 브라우저 사이트 데이터를 지우거나 프로필을 삭제하지 않는다. ACK 전 청크는 서버 백업에 포함되지 않으며 사이트 데이터 삭제 시 유실된다.
- 회의록에서 결정, 담당자, 기한의 근거 발화를 확인한다. 근거가 없으면 확정하지 않고 `미정`으로 둔다.

## 저장, 백업, 삭제

데이터는 자동 만료되지 않는다. 음성, 전사, 팩트 출처, 진행 상태, 회의록은 사용자가 명시적으로 삭제할 때까지 로컬에 영구 보존된다.

SQLite 전체 파일 암호화가 아니라 애플리케이션 계층 AES-256-GCM 암호화를 사용한다. 음성·전사·회의 문맥·분석·회의록 같은 내용은 암호화되지만, UUID, lifecycle, revision, 시각, 오디오 크기·경로 같은 운영 메타데이터는 평문으로 남는다. 따라서 Windows 계정과 데이터 폴더 접근 권한 자체도 제한해야 한다.

- 백업 단위는 `%LOCALAPPDATA%\GoodListener\data` 전체다. 앱을 완전히 종료한 상태에서 DB, `audio` 폴더, `master.key.dpapi`를 함께 백업한다.
- `master.key.dpapi`는 현재 Windows 사용자 프로필의 DPAPI에 묶여 있다. 파일만 다른 PC나 다른 Windows 계정으로 복사해서는 복호화할 수 없다.
- key 파일을 잃거나 Windows 사용자 프로필의 DPAPI 복구 수단을 잃으면 DB와 음성은 영구적으로 복호화할 수 없다. key만 따로 삭제하거나 교체하지 않는다.
- 일반 삭제는 앱의 회의별 삭제 기능을 사용한다. 해당 회의의 DB 레코드, 음성 청크, 전사, 회의록이 함께 제거돼야 한다.
- 회의별 삭제는 해당 회의의 브라우저 IndexedDB 대기 청크도 제거한다. 업로드가 계속 실패한 고아 청크는 삭제 전 회의 ID를 확인한 뒤 앱의 삭제 흐름으로 정리하며 브라우저 사이트 데이터 전체 삭제는 다른 회의의 미업로드 청크까지 잃을 수 있어 최후 수단으로만 사용한다.
- 관리자 전체 삭제는 앱을 종료하고 정확히 `%LOCALAPPDATA%\GoodListener\data`만 삭제한다. 설치 폴더 삭제나 uninstall만으로는 회의 데이터가 삭제되지 않는다.

전체 삭제 전 대상 확인 예시:

```powershell
$Target = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "GoodListener\data"))
$Expected = [IO.Path]::GetFullPath("$env:LOCALAPPDATA\GoodListener\data")
if ($Target -ne $Expected) { throw "삭제 대상 불일치: $Target" }
Get-ChildItem -LiteralPath $Target -Force
# 내용을 확인한 다음에만 실행:
# Remove-Item -LiteralPath $Target -Recurse -Force
```

## 로그와 장애 대응

- 앱은 별도 파일 로그를 만들지 않고 uvicorn stdout/stderr만 출력한다. 운영 host 또는 사내 endpoint manager가 접근 제한된 로그 위치, 용량 상한, rotation을 책임진다.
- 로그에는 API key, Realtime client secret, 원문 음성, 전체 전사, 회의록 본문을 남기지 않는다.
- 로그 식별자는 `meeting_id`, `request_id`, event sequence를 사용하고 사용자 입력은 길이·상태·해시 등 비내용 메타데이터만 기록한다.
- 운영 시 기록할 지표는 첫 자막 delta, 확정 자막, 분석·검색·회의록 지연, queue depth, 재연결 횟수, OpenAI 오류·rate limit, token 사용량이다.
- OpenAI 장애 시 분석을 중단해도 로컬 음성 보존과 화면 상태는 유지한다. 같은 데이터 경로로 앱을 재시작하고 미완료 회의를 재개한다.
- DB 또는 key 오류가 발생하면 새 key를 생성하거나 파일을 덮어쓰지 말고 데이터 디렉터리 전체를 읽기 전용 백업한 뒤 조사한다.

## 검증 명령

```powershell
./scripts/verify.ps1
```

실제 API 호출은 비용과 외부 전송을 명시적으로 허용한 운영자만 실행한다. 고정된 합성 문장만 전송하며 회의 데이터는 읽지 않는다.

```powershell
$env:OPENAI_API_KEY = "운영자가 현재 프로세스에 주입"
./scripts/verify.ps1 -LiveApi
```

## 릴리스와 롤백

- 서명된 installer, `backend/requirements.lock`, Git commit SHA를 한 릴리스 단위로 보관한다.
- 한 대에서 파일럿한 뒤 3~5대 canary, 부서 단위 순으로 확대한다.
- 롤백 전 `%LOCALAPPDATA%\GoodListener\data` 전체를 백업한다. DB 변경은 additive migration만 허용한다.
- 이전 서명 installer로 binary를 되돌리되 데이터와 key는 삭제하거나 이전 파일로 덮어쓰지 않는다.
- 롤백 시에도 EXAONE, Claude/Codex CLI 등 제거된 provider를 다시 활성화하지 않는다.
