# 배당가치 노트

성장·이익·지속 배당을 갖춘 국내 종목 중 배당 기준으로 저평가된 종목을 골라
선정 이유와 함께 보여주고, 보유 종목과 비교해 리밸런싱을 제안하는 개인용 웹앱입니다.

## 구조
- `collector/collect.py` — GitHub Actions가 화~토 오전 7시에 실행. 결과를 `data/`에 저장
- `config.json` — 선정 기준(시가총액, ROE, 연속 배당, 배당률, 가중치 등). 숫자만 고치면 됨
- `index.html` — 앱 화면 (후보 / 보유 / 리밸런싱 / 기록 / 설정, 좌우로 밀어 이동)
- 보유 내역은 저장소가 아니라 폰 브라우저에만 저장됨 (설정 탭에서 백업)

## 처음 설정
1. 인증키 2개 발급
   - OpenDART: https://opendart.fss.or.kr → 인증키 신청 (즉시 발급)
   - 공공데이터포털: https://www.data.go.kr → '금융위원회_주식시세정보' 활용신청 → 일반 인증키(Decoding)
2. GitHub에 새 저장소(예: divnote) 만들고 이 폴더 전체 업로드 (.github, .nojekyll 포함)
3. Settings → Secrets and variables → Actions → New repository secret
   - `DART_API_KEY` = OpenDART 키
   - `DATA_GO_KR_KEY` = 공공데이터포털 키
4. Settings → Actions → General → Workflow permissions → Read and write permissions 저장
5. Settings → Pages → Deploy from a branch → main / (root) 저장
6. Actions 탭 → collect → Run workflow (첫 실행은 캐시가 없어 15~30분)
7. 끝나면 https://<아이디>.github.io/divnote/ 를 폰 크롬에서 열고 홈 화면에 설치

## 수집 흐름
주가(전 종목) → 시가총액 필터 → 6개년 주요계정(다중회사 API) → 이익·ROE 필터
→ 6개년 배당(배당에 관한 사항) → 배당 필터 → 업종·현금흐름·최근 분기 → 점수·선정 이유

공시 데이터는 `data/cache/`에 쌓이므로 두 번째 실행부터는 주가만 새로 받고 몇 분 안에 끝납니다.
