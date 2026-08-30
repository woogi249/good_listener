# 0.1.x 사내 출시 점검표

출시는 아래 항목을 실제 회의실 PC와 승인된 OpenAI 프로젝트에서 통과한 경우에만 진행한다. 수치는 평균이 아니라 p95로 기록한다.

## 기능과 복구

- [ ] 동의하지 않으면 회의 생성·마이크·OpenAI client secret 발급이 차단된다.
- [ ] 검색·분석 중에도 새 오디오와 확정 발화가 누락 없이 저장된다.
- [ ] partial transcript는 저장·분석하지 않고 finalized transcript만 반영한다.
- [ ] 5초와 30초 네트워크 단절 후 재연결되며 로컬 음성 청크로 공백을 보완한다.
- [ ] 브라우저 새로고침, client secret 만료, 서버 재시작 뒤 revision 이후 이벤트만 재생돼 중복 발화가 생기지 않는다.
- [ ] 종료 중 미완료 검색은 무기한 기다리지 않고 `추가 확인 필요`로 남는다.
- [ ] 회의록을 사람이 수정·승인할 수 있고 근거 없는 담당자·기한은 `미정`이다.
- [ ] 회의별 삭제가 DB 관계 데이터와 연결된 `.glenc` 음성을 모두 제거한다.

## 성능과 품질

- [ ] 첫 자막 delta p95 1.5초 이내
- [ ] 발화 종료 후 확정 자막 p95 3초 이내
- [ ] 진행 상태 갱신 p95 10초 이내
- [ ] 팩트 후보는 1초 안에 `검증 중`, 공개 웹 판정 p95 20초 이내
- [ ] 60분 회의의 최종 회의록 p95 90초 이내
- [ ] 사내 golden set에서 이름·숫자·날짜 핵심 필드 정확도 95% 이상
- [ ] 근거 없는 결정·담당자·기한 생성률 1% 미만이며 치명적 사례 0건
- [ ] 2시간 연속 회의에서 crash, 오디오 누락, queue 무한 증가, 지속적 메모리 증가가 없다.

## 보안과 개인정보

- [ ] 일반 OpenAI key가 브라우저 응답, 개발자 도구, bundle, URL, 명령행, 로그에 나타나지 않는다.
- [ ] 서버는 `127.0.0.1`에만 열리고 허용되지 않은 Host, Origin, WebSocket cookie가 거부된다.
- [ ] DB와 음성 파일이 평문 내용을 포함하지 않고 DPAPI 보호 key 없이 복호화되지 않는다.
- [ ] 서버 ACK 전 IndexedDB 평문 청크가 정상 ACK·회의 삭제 뒤 제거되고, 전용 Windows 계정과 브라우저 프로필 접근 정책이 적용돼 있다.
- [ ] key 파일, DB, audio를 함께 백업하고 같은 Windows 사용자 프로필에서 복원 테스트를 통과한다.
- [ ] key 손실 시 자동으로 새 key를 만들어 기존 데이터를 덮어쓰지 않는다.
- [ ] uvicorn stdout/stderr에 API secret, 원문 전사, 회의록 전문이 없다.
- [ ] OpenAI 프로젝트 비용 한도·알림과 사내 데이터 처리 승인이 설정돼 있다.

## 배포

- [ ] `scripts/verify.ps1`과 승인된 환경의 `scripts/verify.ps1 -LiveApi`가 통과한다.
- [ ] `pip-audit -r backend/requirements.lock`에 알려진 취약점이 없다.
- [ ] Python이 없는 깨끗한 Windows 10/11 x64 VM에서 설치, 실행, 마이크 권한, 종료, 업데이트, uninstall을 검증한다.
- [ ] 사내 인증서로 exe와 installer를 서명하고 commit SHA, lock, SBOM을 릴리스와 함께 보관한다.
- [ ] uninstall과 binary rollback이 `%LOCALAPPDATA%\GoodListener\data`를 삭제하거나 덮어쓰지 않는다.
- [ ] 한 대 파일럿 후 3~5대 canary에서 실제 회의 20건을 통과한 뒤 확대한다.
