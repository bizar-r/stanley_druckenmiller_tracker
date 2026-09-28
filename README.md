# stanley_druckenmiller_tracker

스탠리 드러켄밀러(Duquesne Family Office, CIK 1536411)의 SEC 공시를 매일 자동으로 확인해서
포트폴리오 대시보드를 GitHub Pages로 보여주는 트래커.

## 구성

```
tracker/sec.py      EDGAR에서 공시 목록 + 13F 정보 테이블(XML) 수집·파싱 (data/filings/에 캐시)
tracker/tickers.py  CUSIP → 티커 변환 (OpenFIGI, data/cusip_tickers.json에 캐시)
tracker/prices.py   분기말 이후 주가 변동 (Yahoo Finance)
tracker/update.py   위를 묶어서 docs/data.json 생성
docs/index.html     대시보드 (data.json을 읽는 정적 페이지)
.github/workflows/update.yml   매일 07:17 KST 실행 → 데이터 커밋 → Pages 배포
```

대시보드 내용:
- 최신 13F 보유 종목 (비중, 가치, 주식 수, 전분기 대비 신규/증가/감소, 분기말 이후 주가, 추정 현재 비중)
- 종목별 분기 추이, 상위 15종목 비중, 13F 총액 추이
- 청산 종목, 최근 SEC 공시, 다음 13F 마감일

## 처음 한 번 설정

1. **Settings → Pages → Build and deployment → Source** 를 `GitHub Actions` 로 바꾸기
2. **Settings → Secrets and variables → Actions** 에 `SEC_USER_AGENT` 추가: `이름 이메일` 형식 (예: `Your Name you@gmail.com`).
   SEC는 일부 이메일 도메인을 거부함 (`users.noreply.github.com` 등). gmail.com은 통과 확인됨.
3. **Actions** 탭 → `Daily update` → `Run workflow` (첫 실행은 과거 13F 전체를 받느라 몇 분 걸림)
4. 완료되면 https://bizar-r.github.io/stanley_druckenmiller_tracker/ 에서 확인

선택 사항:
- `OPENFIGI_API_KEY`: 무료 키가 있으면 티커 변환이 빨라짐
- 티커가 비거나 틀리면 `data/ticker_overrides.json` 에 `"CUSIP": "티커"` 를 추가

## 로컬 실행

```bash
pip install -r requirements.txt
python -m tracker.update
python -m http.server -d docs 8000   # http://localhost:8000
```

## 한계

13F에는 미국 상장 주식·ETF·옵션 롱만 나오고, 분기 말 기준으로 최대 45일 뒤에 공개된다.
국채·통화·선물·공매도 같은 매크로 포지션은 인터뷰 발언 수집(다음 단계)으로 보완할 예정.
