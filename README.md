# 🛰️ satellite_scheduler_web

[satellite_scheduler](https://github.com/pjh4859/satellite_scheduler) (PyQt6 데스크톱 앱)의 웹 버전입니다.

## 목표

- **UI/언어는 완전히 새로 작성**, 기존 데스크톱 앱의 **핵심 궤도 계산·스케줄링 로직(core/*.py)은 최대한 그대로 재사용**
- **Pyodide**(브라우저에서 실제 CPython을 실행하는 WebAssembly 기술)를 사용해서, skyfield/SGP4 기반 궤도 전파를 서버 없이 브라우저 안에서 직접 계산
- 최종적으로는 PWA(오프라인 캐싱)로 만들어서, 처음 한 번 로드한 뒤에는 인터넷 없이도 동작하는 것이 목표
- GitHub Pages 같은 정적 호스팅에 그대로 올릴 수 있음 (서버 불필요)

## 왜 이 방식인가

- PyQt6는 브라우저에서 못 돌아가서 UI는 필연적으로 재작성해야 함
- 대신 `core/scheduler.py` 등 순수 Python 로직은 Pyodide를 통해 **거의 코드 변경 없이** 브라우저에서 실행 가능 — 실제로 `sgp4`, `skyfield`가 Pyodide 환경에서 정상 설치·동작하는 것을 프로토타입으로 확인함 (2026-09-16 검증, ISS 궤도 계산 성공)

## 진행 단계

- [x] **1단계**: 위성 1개 + 지상국 1개, TLE 직접 입력 → 패스 계산 → 표 출력 (`py/scheduler_core.py`)
- [x] **2단계**: 다중 위성/지상국, 안테나 RX/TX 용량, 자동 충돌 해결, 간단한 근무시간 필터 이식 (`py/scheduler_full.py`)
- [x] **2.5단계**: 지상국별 개별 근무시간 규칙, 안테나 점검 일정(시간대별 용량)
- [x] **2.6단계**: TX 자원 추적 및 미션 플랜 배정 (Strict Sequential 전략만 우선 이식, `py/tab3_engine.py`)
- [ ] **3단계**: Gantt 차트(Plotly.js) / 궤도 지도(Leaflet.js) 등 시각화, 나머지 다이얼로그 기능
- [ ] **4단계**: PWA 전환 (Service Worker로 오프라인 캐싱)

## 로컬에서 열어보기

정적 파일만 있어서 별도 빌드 없이 바로 열 수 있습니다. 다만 브라우저 보안 정책상
`fetch()`로 `py/scheduler_core.py`를 읽어오려면 `file://`로 직접 열지 말고 간단한
로컬 서버를 통해 열어야 합니다:

```bash
# 이 폴더에서
python3 -m http.server 8000
# 브라우저에서 http://localhost:8000 접속
```

## GitHub Pages로 배포

저장소 Settings → Pages → Source를 `main` 브랜치 `/ (root)`로 설정하면,
`https://pjh4859.github.io/satellite_scheduler_web/` 주소로 바로 접속할 수 있습니다.

## 기술 스택

| 영역 | 기술 |
|---|---|
| 궤도 계산 엔진 | Python (skyfield, sgp4) — Pyodide로 브라우저 실행 |
| UI | HTML / CSS / Vanilla JS (초기 프로토타입, 추후 필요시 프레임워크 도입 검토) |
| 호스팅 | GitHub Pages (정적 파일만) |
